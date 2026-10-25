'use strict';
const fs=require('node:fs');const path=require('node:path');const {spawn}=require('node:child_process');
const crypto=require('node:crypto');const vm=require('node:vm');const {createRequire}=require('node:module');
const CODE_PINS={"backend.cjs": "63b8bfc33ac95ce1fa45dea11b7c63f8ac4c150bea0878f8985f209c8b9e1064", "bootstrap.py": "ebfec0358ee74e3bed1f8c43ad99e74dbe19b1296602472d269ba4f545c6925d", "producer-schema.cjs": "a0594a10018392b77c1c484b8f4b85cc46d1617ff50b1d1adda1952c665034ea", "python-source.cjs": "a40f92ae36a9a6ecdca0f8a41b7abc833d5f3b5ee11620cc9f009c98e2dc201e", "schema.cjs": "b7ac6407f68ccc7883e2d27aaa7b7ec7cae3a3a03c6df8f21ed9f46ee220ee16", "worker.cjs": "3a425a99dd8ddac11df4f4313d754fe5855c2c6020bd60cf4c16772c3c6a4c16"};
function capture(name){
 const file=path.join(__dirname,name),fd=fs.openSync(file,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW);
 try{const a=fs.fstatSync(fd,{bigint:true});if(!a.isFile()||a.nlink!==1n||a.size<1n||a.size>131072n)throw new Error('ACTION_SOURCE_HOLD');const raw=fs.readFileSync(fd),b=fs.fstatSync(fd,{bigint:true}),c=fs.lstatSync(file,{bigint:true});for(const key of ['dev','ino','mode','uid','nlink','size','mtimeNs','ctimeNs'])if(a[key]!==b[key]||b[key]!==c[key])throw new Error('ACTION_SOURCE_HOLD');if(raw.length!==Number(a.size)||crypto.createHash('sha256').update(raw).digest('hex')!==CODE_PINS[name])throw new Error('ACTION_SOURCE_HOLD');return raw.toString('utf8');}finally{fs.closeSync(fd);}
}
const HELD=Object.fromEntries(Object.keys(CODE_PINS).map(name=>[name,capture(name)]));
const nativeRequire=createRequire(__filename),cache=new Map();
function held(name){
 if(cache.has(name))return cache.get(name).exports;const module={exports:{}};cache.set(name,module);
 const requested=(spec)=>spec.startsWith('./')&&Object.hasOwn(HELD,spec.slice(2))?held(spec.slice(2)):nativeRequire(spec);
 vm.compileFunction(HELD[name],['require','module','exports','__filename','__dirname'],{filename:path.join(__dirname,name)})(requested,module,module.exports,path.join(__dirname,name),__dirname);return module.exports;
}
const {need,canonical,hash,producer,PhaseSession,object,positive,RUNTIME}=held('schema.cjs');const {environmentContext}=held('backend.cjs');const PYTHON_PINS=held('python-source.cjs');
const PHASE_MS=18000,MAX_PHASES=32;
function childEnvironment(env){
 const copy={...env};for(const key of Object.keys(copy))if(key.startsWith('ACTIONS_')||key.startsWith('INPUT_')||key==='GITHUB_TOKEN'||key==='KEYDB_QUALIFICATION_ARTIFACT_READ_TOKEN'||key==='NODE_OPTIONS')delete copy[key];
 copy.KEYDB_QUALIFICATION_RECEIPT_REQUEST_FD='3';copy.KEYDB_QUALIFICATION_RECEIPT_ACK_FD='4';copy.PYTHONDONTWRITEBYTECODE='1';return copy;
}
function workerEnvironment(env,context){
 const names=['GITHUB_ACTIONS','GITHUB_SERVER_URL','RUNNER_ENVIRONMENT','RUNNER_OS','RUNNER_ARCH','GITHUB_REPOSITORY','GITHUB_REPOSITORY_ID','GITHUB_EVENT_NAME','GITHUB_REF','GITHUB_WORKFLOW_REF','GITHUB_WORKFLOW_SHA','GITHUB_SHA','GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','RUNNER_NAME','RUNNER_TEMP','GITHUB_WORKSPACE','GITHUB_RETENTION_DAYS','ACTIONS_RUNTIME_TOKEN','ACTIONS_RUNTIME_URL','ACTIONS_RESULTS_URL'];
 const copy=Object.fromEntries(names.filter(k=>typeof env[k]==='string').map(k=>[k,env[k]]));
 copy.KEYDB_QUALIFICATION_RECEIPT_SDK_SHA=context.sdk_sha;copy.KEYDB_QUALIFICATION_RECEIPT_PRODUCER_JOB_ID=String(context.producer_job_id);copy.KEYDB_QUALIFICATION_ARTIFACT_READ_TOKEN=env['INPUT_GITHUB-TOKEN'];return copy;
}
function pythonPath(env,value){
 need(typeof value==='string'&&path.isAbsolute(value)&&typeof env.RUNNER_TEMP==='string'&&path.isAbsolute(env.RUNNER_TEMP));
 const root=fs.realpathSync(env.RUNNER_TEMP),expected=path.join(root,'keydb-qualification-v1-python','bin','python');need(path.resolve(value)===expected);
 const config=path.join(root,'keydb-qualification-v1-python','pyvenv.cfg'),fd=fs.openSync(config,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW);let text;
 try{const a=fs.fstatSync(fd);need(a.isFile()&&a.size>0&&a.size<=8192&&(a.mode&0o022)===0);text=fs.readFileSync(fd,'utf8');}finally{fs.closeSync(fd);}
 const fields=new Map();for(const line of text.split(/\r?\n/)){if(!line.trim())continue;const pair=line.match(/^([^=]+?)\s*=\s*(.*)$/);need(pair!==null);const key=pair[1].trim();need(!fields.has(key));fields.set(key,pair[2].trim());}
 need(fields.get('include-system-site-packages')==='false'&&(fields.get('version_info')??fields.get('version'))==='3.13.9');
 const binary=fs.realpathSync(value),st=fs.lstatSync(binary);need(path.basename(binary)==='python3.13'&&st.isFile()&&(st.mode&0o022)===0&&(st.mode&0o111)!==0&&st.uid===process.getuid());return expected;
}

function ackFor(record,ack){
 object(ack,['schema','backend','operation_id','phase','record_sha256','parent_record_sha256','artifact_id','artifact_digest','record_readback_sha256','producer','exclusive','created']);
 need(ack.schema==='just-akash/keydb-qualification-durable-ack/v1'&&ack.backend==='github-actions-artifact/v4'&&ack.operation_id===record.operation_id&&ack.phase===record.phase&&ack.parent_record_sha256===record.parent_record_sha256&&ack.record_sha256===hash(record)&&ack.record_readback_sha256===hash(record)&&ack.exclusive===true&&ack.created===true);
 positive(ack.artifact_id);need(typeof ack.artifact_digest==='string'&&/^sha256:[a-f0-9]{64}$/.test(ack.artifact_digest)&&canonical(ack.producer)===canonical(producer(record.binding)));return ack;
}
function phaseWorker(raw,env){
 return new Promise((resolve,reject)=>{
  const child=spawn(process.execPath,['-e',"const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),crypto=require('node:crypto'),{createRequire}=require('node:module');const pins=JSON.parse(process.argv[1]),root=process.argv[2];let bytes=fs.readFileSync(3);if(bytes.length>393216)process.exit(1);const held=JSON.parse(bytes),cache=new Map(),native=createRequire(path.join(root,'index.cjs'));if(Object.keys(held).sort().join('|')!==Object.keys(pins).sort().join('|'))process.exit(1);for(const n of Object.keys(pins))if(crypto.createHash('sha256').update(held[n]).digest('hex')!==pins[n])process.exit(1);function load(n){if(cache.has(n))return cache.get(n).exports;const m={exports:{}};cache.set(n,m);const req=s=>s.startsWith('./')&&Object.hasOwn(held,s.slice(2))?load(s.slice(2)):native(s);vm.compileFunction(held[n],['require','module','exports','__filename','__dirname'],{filename:path.join(root,n)})(req,m,m.exports,path.join(root,n),root);return m.exports;}const timer=setTimeout(()=>process.exit(1),18000);load('worker.cjs').run().catch(()=>{try{fs.writeSync(1,'{\"status\":\"HOLD\"}\\n');}catch{}process.exitCode=1;}).finally(()=>clearTimeout(timer));",JSON.stringify(CODE_PINS),__dirname],{env,stdio:['pipe','pipe','ignore','pipe'],shell:false});child.stdio[3].end(JSON.stringify(HELD));let chunks=[],length=0,settled=false;
  const fail=()=>{if(settled)return;settled=true;clearTimeout(timer);child.kill('SIGKILL');reject(new Error('DURABLE_RECEIPT_HOLD'));};
  const timer=setTimeout(fail,PHASE_MS);child.on('error',fail);child.stdin.on('error',fail);
  child.stdout.on('data',chunk=>{length+=chunk.length;if(length>4096)fail();else chunks.push(chunk);});
  child.on('close',code=>{if(settled)return;clearTimeout(timer);settled=true;let ack;try{need(code===0);const body=Buffer.concat(chunks).toString('utf8');ack=JSON.parse(body);need(canonical(ack)+'\n'===body);}catch{reject(new Error('DURABLE_RECEIPT_HOLD'));return;}resolve(ack);});
  child.stdin.end(raw);
 });
}
async function supervise(options){
 const {python,env,worker=phaseWorker,spawnChild=spawn}=options;
 need(env.RUNNER_PRIVATE_PROFILE==='blazing-keydb-parity-qualification-v1');
 const context=environmentContext(env,env['INPUT_SDK-SHA'],env['INPUT_PRODUCER-JOB-ID']);
 const childEnv=childEnvironment(env);childEnv.KEYDB_QUALIFICATION_SDK_SHA=context.sdk_sha;childEnv.KEYDB_QUALIFICATION_PRODUCER_JOB_ID=String(context.producer_job_id);
 const child=spawnChild(python,['-I','-S','-c',HELD['bootstrap.py'],JSON.stringify(PYTHON_PINS)],{env:childEnv,stdio:['ignore','ignore','ignore','pipe','pipe'],shell:false});
 const session=new PhaseSession();let buffer=Buffer.alloc(0),busy=false,count=0,runtimeControls=0,failed=false;
 let active=Promise.resolve();
 const hold=()=>{if(failed)return;failed=true;child.stdio[4].destroy();child.kill('SIGTERM');setTimeout(()=>child.kill('SIGKILL'),500).unref();};
 child.on('error',hold);child.stdio[3].on('error',hold);child.stdio[4].on('error',hold);
 child.stdio[3].on('data',chunk=>{
  if(failed)return;
  try{
   need(!busy);buffer=Buffer.concat([buffer,chunk]);need(buffer.length<=65537);const end=buffer.indexOf(10);if(end<0)return;need(end===buffer.length-1&&++count<=MAX_PHASES);
   const raw=buffer.subarray(0,end).toString('utf8');buffer=Buffer.alloc(0);const record=JSON.parse(raw);need(canonical(record)===raw);
   for(const k of ['repository_id','run_id','run_attempt','workflow_sha','native_head_sha','producer_job_id','sdk_sha'])need(record.binding[k]===context[k]);
   if(record.schema===RUNTIME){const ack=session.reserveRuntime(record);runtimeControls++;child.stdio[4].write(canonical(ack)+'\n');return;}
   const v=session.reserve(record);
   busy=true;
   active=(async()=>{try{
    const workerEnv=workerEnvironment(env,context);
    const ack=ackFor(record,await worker(v.raw,workerEnv));need(!failed);session.accept(v);child.stdio[4].write(canonical(ack)+'\n');busy=false;
   }catch{hold();}})();
  }catch{hold();}
 });
 const result=await new Promise(resolve=>child.once('close',(code,signal)=>resolve({code,signal})));await active;
 need(!failed&&!busy&&buffer.length===0&&count>0);
 if(result.code===66){const terminal=session.terminal(result.code,result.signal);const ack=ackFor(terminal.record,await worker(terminal.raw,workerEnvironment(env,context)));return {acknowledged_phases:count+1,terminal_no_create:{record:terminal.record,ack}};}
 need(result.code===0&&result.signal===null&&!session.fenced);return {acknowledged_phases:count-runtimeControls,runtime_reservation_acknowledged:session.mayCreate};
}
async function main(){
 const env=process.env;const python=pythonPath(env,env['INPUT_PYTHON-PATH']);need(typeof env['INPUT_GITHUB-TOKEN']==='string'&&env['INPUT_GITHUB-TOKEN'].length>0);
 const result=await supervise({python,env});if(result.terminal_no_create){const value=result.terminal_no_create;need(typeof env.GITHUB_OUTPUT==='string');fs.appendFileSync(env.GITHUB_OUTPUT,'returned_runner_id='+value.record.details.returned_runner_id+'\nno_create_record_sha256='+value.ack.record_sha256+'\nno_create_artifact_id='+value.ack.artifact_id+'\n');throw new Error('POSITIVE_PREPARATION_FENCE_RECORDED');}
}
if(require.main===module)main().catch(()=>{fs.writeSync(2,'Scanner-v2 durable receipt HOLD; no continuation permitted\n');process.exitCode=1;});
module.exports={childEnvironment,workerEnvironment,pythonPath,ackFor,phaseWorker,supervise,PHASE_MS,MAX_PHASES};
