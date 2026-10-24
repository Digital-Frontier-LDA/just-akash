'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{spawn}=require('node:child_process');
const I=require('./index.cjs'),B=require('./cleanup-backend.cjs'),F=require('./fixtures.cjs'),S=require('./cleanup-schema.cjs');
const root=process.env.PUBLIC_TEST_TMP_ROOT||process.env.RUNNER_TEMP;assert(root&&fs.existsSync(root));const python=process.env.PUBLIC_TEST_PYTHON;assert(python);
async function control(failure,leaseCount=3){
 const directory=fs.mkdtempSync(path.join(root,'cleanup-pipe-')),f=F.fixture(directory,{reconcile:true}),env=F.env(f.c);
 if(leaseCount<3){for(const [id,meta]of f.items)if(meta.name.includes('-lease_bind-')&&['no_wire','unknown','ack'].indexOf(meta.name.split('-').at(-1))>=leaseCount){f.items.delete(id);f.raws.delete(id);}f.jobMap.get(777).conclusion='failure';f.jobMap.get(777).steps[0].conclusion='failure';}
 Object.assign(env,{...Object.fromEntries(['PATH','SYSTEMROOT','HOME','TMPDIR'].filter(k=>process.env[k]).map(k=>[k,process.env[k]])),RUNNER_TEMP:directory,ACTIONS_RUNTIME_TOKEN:'PUBLIC_ARTIFACT_RUNTIME_NONAUTH',ACTIONS_RESULTS_URL:'https://public.invalid/',GITHUB_TOKEN:'PUBLIC_EPHEMERAL_NONAUTH',GH_TOKEN:'PUBLIC_MANAGER_NONAUTH',PUBLIC_FIXTURE_ROOT:directory,PUBLIC_CLEANUP_SOURCE_DIR:path.resolve(__dirname,'../source/just_akash'),PUBLIC_SCANNER_CONTEXT_DIR:path.resolve(__dirname,'tests/public-sdk-context/just_akash')});
 let starts=0;const calls=[];const worker=async pack=>{
  calls.push(pack.request.command);let value;
  if(pack.request.command==='READ_TARGET'){value=await B.readTarget(pack.context,f.deps);fs.writeFileSync(path.join(directory,'public-bundle.json'),S.canonical(value));}
  else if(pack.request.command==='HISTORY')value=await B.handlerHistory(pack.context,pack.bundle,pack.current_records,f.deps);
  else value=await B.persist(pack.request.payload,pack.context,pack.bundle,pack.current_records,f.deps);
  if(failure==='UNKNOWN'&&pack.request.command==='PERSIST'&&pack.request.payload.phase==='UNKNOWN')throw new Error('PUBLIC_SECRET_MUST_NOT_ESCAPE');
  if(failure==='actor'&&pack.request.command==='READ_TARGET')value.actor={...value.actor,job_id:999};return value;
 };
 const spawnChild=(exe,args,options)=>{starts++;assert.equal(exe,python);assert.deepEqual(args,['-I','-m','just_akash.scanner_v2_cleanup']);const fixture=path.join(__dirname,'tests/fixture');return spawn(exe,['-I','-c',`import sys,runpy;sys.path.insert(0,${JSON.stringify(fixture)});runpy.run_module('just_akash.scanner_v2_cleanup',run_name='__main__')`],options);};
 let accepted=false;try{await I.supervise({python,env,worker,spawnChild});accepted=true;}catch{}
 fs.writeFileSync(path.join(directory,'public-observations.json'),S.canonical({calls,starts,accepted,artifacts:[...f.items.values()].map(x=>({id:x.id,name:x.name}))}));
 return {directory,f,calls,starts,accepted};
}
test('actual Python CleanupOperation/Pipe and Node supervisor gate one synthetic wire on readback',async()=>{const x=await control();assert(x.accepted);assert.equal(x.starts,1);assert.equal(fs.readFileSync(path.join(x.directory,'synthetic-effect.txt'),'utf8'),'ONE_PUBLIC_SYNTHETIC_CLOSE_NO_NETWORK\n');assert(fs.existsSync(path.join(x.directory,'completed.txt')));assert.deepEqual(x.calls,['READ_TARGET','PERSIST','PERSIST','HISTORY','PERSIST','HISTORY','PERSIST']);});
test('lost UNKNOWN acknowledgement retains immutable phase but blocks Python effect and retry',async()=>{const x=await control('UNKNOWN');assert.equal(x.accepted,false);assert.equal(x.starts,1);assert(!fs.existsSync(path.join(x.directory,'synthetic-effect.txt')));assert([...x.f.items.values()].some(v=>v.name.includes('-close-actor')&&v.name.endsWith('-unknown')));});
test('foreign current actor returned by reader never reaches effect controller phase',async()=>{const x=await control('actor');assert.equal(x.accepted,false);assert(!fs.existsSync(path.join(x.directory,'synthetic-effect.txt')));assert.deepEqual(x.calls,['READ_TARGET']);});
test('worker receives only artifact runtime and ephemeral read token, child gets no artifact token',()=>{
 const e={RUNNER_TEMP:'/public-runner-temp',ACTIONS_RUNTIME_TOKEN:'PUBLIC_ARTIFACT',ACTIONS_RESULTS_URL:'https://public.invalid/',GH_TOKEN:'PUBLIC_MANAGER',AKASH_API_KEY:'PUBLIC_CONSOLE',SOPS_AGE_KEY:'PUBLIC_AGE',GITHUB_TOKEN:'PUBLIC_GITHUB','INPUT_GITHUB-TOKEN':'PUBLIC_READER',NODE_OPTIONS:'--bad'};
 const worker=I.workerEnvironment(e);assert(!Object.values(worker).includes('PUBLIC_MANAGER'));assert(!Object.values(worker).includes('PUBLIC_CONSOLE'));assert(!Object.values(worker).includes('PUBLIC_AGE'));assert.equal(worker.SCANNER_CLEANUP_ARTIFACT_TOKEN,'PUBLIC_READER');
 const child=I.childEnvironment(e,F.current().actor);assert.equal(child.GH_TOKEN,'PUBLIC_MANAGER');assert(!Object.keys(child).some(k=>k.startsWith('ACTIONS_')||k.startsWith('INPUT_')));assert(!('GITHUB_TOKEN'in child));assert(!('NODE_OPTIONS'in child));assert.equal(I.PHASE_MS,18000);
});

for(const count of [0,2])test('actual Python pipe preserves nullable known-DSEQ provider prefix '+count,async()=>{const x=await control(undefined,count);assert(x.accepted);assert.equal(x.starts,1);const bundle=JSON.parse(fs.readFileSync(path.join(x.directory,'public-bundle.json')));assert.equal(bundle.target.lease_ack_record_sha256,null);assert.equal(bundle.records.lease,null);assert.deepEqual(bundle.phase_inventory.lease,['NO_WIRE','UNKNOWN'].slice(0,count));assert(fs.existsSync(path.join(x.directory,'synthetic-effect.txt')));});
