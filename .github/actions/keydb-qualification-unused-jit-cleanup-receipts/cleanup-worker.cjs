'use strict';
const fs=require('node:fs');
process.stdout.write=()=>true;process.stderr.write=()=>true;process.emitWarning=()=>{};
for(const key of ['log','info','warn','error','debug','trace'])console[key]=()=>{};
const https=require('node:https'),path=require('node:path');
const S=require('./cleanup-schema.cjs'),B=require('./cleanup-backend.cjs');
const {need,canonical,request,positive}=S;
function allowed(route){
 const u=new URL('https://api.github.com/'+route);need(u.origin==='https://api.github.com'&&u.pathname.startsWith('/repos/Borduas-Holdings/blazing/actions/'));
 const suffix=u.pathname.slice('/repos/Borduas-Holdings/blazing/actions/'.length);
 if(/^(runs\/[1-9][0-9]*(\/attempts\/[1-9][0-9]*)?|jobs\/[1-9][0-9]*|artifacts\/[1-9][0-9]*)$/.test(suffix))need(u.search==='');
 else if(/^runs\/[1-9][0-9]*\/(attempts\/[1-9][0-9]*\/jobs|artifacts)$/.test(suffix)){
  need([...u.searchParams.keys()].sort().join('|')==='page|per_page'&&u.searchParams.get('per_page')==='100'&&/^(?:[1-9]|10)$/.test(u.searchParams.get('page')));
 }else if(suffix==='workflows/keydb-modules-parity.yml/runs'){
  need([...u.searchParams.keys()].sort().join('|')==='created|event|page|per_page'&&u.searchParams.get('event')==='workflow_dispatch'&&u.searchParams.get('per_page')==='100'&&/^[12]$/.test(u.searchParams.get('page'))&&/^>=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$/.test(u.searchParams.get('created')));
 }else need(false);return u;
}
function nativeClient(token){let count=0;return route=>{
 need(++count<=60);const u=allowed(route);
 return new Promise((resolve,reject)=>{
  let done=false;const fail=()=>{if(!done){done=true;reject(new Error('CLEANUP_NATIVE_HOLD'));}};
  const req=https.request({hostname:'api.github.com',port:443,path:u.pathname+u.search,method:'GET',agent:false,headers:{'User-Agent':'keydb-qualification-cleanup-receipt-v1','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','Authorization':'Bearer '+token}},r=>{
   let size=0;const chunks=[];r.on('data',chunk=>{size+=chunk.length;if(size>2097152){r.destroy();fail();}else chunks.push(chunk);});r.on('error',fail);r.on('aborted',fail);
   r.on('end',()=>{if(done)return;let value;try{need(r.statusCode===200);value=JSON.parse(Buffer.concat(chunks).toString('utf8'));need(value&&typeof value==='object');}catch{fail();return;}done=true;resolve(value);});
  });req.on('error',fail);req.setTimeout(10000,()=>{req.destroy();fail();});req.end();
 });
};}
async function run(){
 const chunks=[];let size=0;for await(const chunk of process.stdin){size+=chunk.length;need(size<=262144);chunks.push(chunk);}
 const raw=Buffer.concat(chunks).toString('utf8'),pack=JSON.parse(raw);need(canonical(pack)===raw);S.object(pack,['request','context','bundle','current_records']);
 const c=pack.context;S.object(c,['actor','runner_name']);S.actor(c.actor);need(typeof c.runner_name==='string');const req=request(pack.request,c.actor);need(Array.isArray(pack.current_records));
 need(typeof process.env.ACTIONS_RUNTIME_TOKEN==='string'&&process.env.ACTIONS_RUNTIME_TOKEN.length>0&&typeof process.env.ACTIONS_RESULTS_URL==='string'&&process.env.ACTIONS_RESULTS_URL.startsWith('https://'));
 const token=process.env.KEYDB_QUALIFICATION_CLEANUP_ARTIFACT_TOKEN;need(typeof token==='string'&&token.length>0);
 const {DefaultArtifactClient}=require('@actions/artifact');const root=process.env.RUNNER_TEMP;need(typeof root==='string'&&path.isAbsolute(root));
 const dir=fs.mkdtempSync(path.join(root,'keydb-qualification-cleanup-public-'));let result;
 try{
  const deps={directory:dir,token,artifact:new DefaultArtifactClient(),native:nativeClient(token)};
  if(req.command==='READ_TARGET'){need(pack.bundle===null&&pack.current_records.length===0);result=await B.readTarget(c,deps);}
  else{
   need(pack.bundle!==null&&canonical(pack.bundle.producer)===canonical(req.command==='HISTORY'?req.payload.producer:req.payload.producer)&&canonical(pack.bundle.target)===canonical(req.payload.target));
   if(req.command==='HISTORY')result=await B.handlerHistory(c,pack.bundle,pack.current_records,deps);
   else result=await B.persist(req.payload,c,pack.bundle,pack.current_records,deps);
  }
 }finally{fs.rmSync(dir,{recursive:true,force:true});}
 const out=Buffer.from(canonical(result)+'\n');need(out.length<=(req.command==='PERSIST'?4096:65536));fs.writeSync(1,out);
}
if(require.main===module)run().catch(()=>{fs.writeSync(1,'{"status":"HOLD"}\n');process.exitCode=1;});
module.exports={allowed,nativeClient,run};
