'use strict';
const fs=require('node:fs');
// Before importing or invoking official dependencies, suppress every raw output path.
process.stdout.write=()=>true;process.stderr.write=()=>true;process.emitWarning=()=>{};
for(const key of ['log','info','warn','error','debug','trace'])console[key]=()=>{};
const https=require('node:https');const path=require('node:path');
const {need,canonical}=require('./schema.cjs');const {environmentContext,persistReceipt}=require('./backend.cjs');
function nativeGET(route,token){
 need(/^repos\/Borduas-Holdings\/blazing\/actions\/(runs\/[1-9][0-9]*|jobs\/[1-9][0-9]*|artifacts\/[1-9][0-9]*)$/.test(route));
 return new Promise((resolve,reject)=>{
  let failed=false;const rejectFixed=()=>{if(!failed){failed=true;reject(new Error('ARTIFACT_NATIVE_READ_HOLD'));}};
  const request=https.request({hostname:'api.github.com',port:443,path:'/'+route,method:'GET',agent:false,headers:{'User-Agent':'scanner-durable-receipt-v1','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','Authorization':'Bearer '+token}},response=>{
   const chunks=[];let size=0;
   response.on('data',chunk=>{size+=chunk.length;if(size>1024*1024){response.destroy();rejectFixed();}else chunks.push(chunk);});
   response.on('error',rejectFixed);response.on('aborted',rejectFixed);
   response.on('end',()=>{if(failed)return;let value;try{need(response.statusCode===200);value=JSON.parse(Buffer.concat(chunks).toString('utf8'));need(value!==null&&typeof value==='object');}catch{rejectFixed();return;}resolve(value);});
  });
  request.on('error',rejectFixed);request.setTimeout(10000,()=>{request.destroy();rejectFixed();});request.end();
 });
}
async function run(){
 let bytes=0;const chunks=[];for await(const chunk of process.stdin){bytes+=chunk.length;need(bytes<=65536);chunks.push(chunk);}
 const raw=Buffer.concat(chunks).toString('utf8');const record=JSON.parse(raw);need(canonical(record)===raw);
 const context=environmentContext(process.env,process.env.KEYDB_QUALIFICATION_RECEIPT_SDK_SHA,process.env.KEYDB_QUALIFICATION_RECEIPT_PRODUCER_JOB_ID);
 need(typeof process.env.ACTIONS_RUNTIME_TOKEN==='string'&&process.env.ACTIONS_RUNTIME_TOKEN.length>0&&typeof process.env.ACTIONS_RESULTS_URL==='string'&&process.env.ACTIONS_RESULTS_URL.startsWith('https://'));
 const token=process.env.KEYDB_QUALIFICATION_ARTIFACT_READ_TOKEN;need(typeof token==='string'&&token.length>0);
 const {DefaultArtifactClient}=require('@actions/artifact');
 const root=process.env.RUNNER_TEMP;need(typeof root==='string'&&path.isAbsolute(root));
 const directory=fs.mkdtempSync(path.join(root,'scanner-public-receipt-'));let ack;
 try{ack=await persistReceipt(record,context,{directory,artifact:new DefaultArtifactClient(),native:route=>nativeGET(route,token)});}
 finally{fs.rmSync(directory,{recursive:true,force:true});}
 fs.writeSync(1,Buffer.from(canonical(ack)+'\n'));
}
if(require.main===module)run().catch(()=>{fs.writeSync(1,Buffer.from('{"status":"HOLD"}\n'));process.exitCode=1;});
module.exports={nativeGET,run};
