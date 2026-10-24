'use strict';
const test=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const path=require('node:path');const {spawn}=require('node:child_process');
const {record,env,run,job}=require('./fixtures.cjs');const {supervise}=require('./index.cjs');const {persistReceipt,environmentContext}=require('./backend.cjs');
const ROOT=process.env.RUNNER_TEMP??process.env.TMPDIR;assert.ok(typeof ROOT==='string'&&path.isAbsolute(ROOT));if(process.platform==='darwin')assert.ok(ROOT.startsWith('/Volumes/CodexE1Work/tmp'));
const PYTHON=process.env.PYTHON??'python3';
const fixtureRoot=path.resolve(__dirname,'tests/fixture');
async function control(damage){
 const directory=fs.mkdtempSync(path.join(ROOT,'scanner-real-pipe-control-'));const phases=[];let uploads=0,nativeArtifact,currentRaw;
 fs.writeFileSync(path.join(directory,'public-record-fixture.json'),JSON.stringify(record()));
 const environment={...process.env,...env(),PUBLIC_FIXTURE_ROOT:directory,PUBLIC_EXPECTED_MANAGER:'PUBLIC_HOST_MANAGER_SENTINEL',PUBLIC_PROVENANCE_SOURCE_DIR:fs.existsSync(path.resolve(__dirname,'../../../just_akash/scanner_provenance.py'))?path.resolve(__dirname,'../../../just_akash'):path.resolve(__dirname,'../source/just_akash'),ACTIONS_RUNTIME_TOKEN:'PUBLIC_RUNTIME_SENTINEL',GITHUB_TOKEN:'PUBLIC_ARTIFACT_SENTINEL',GH_TOKEN:'PUBLIC_HOST_MANAGER_SENTINEL'};
 let requestedArgv;
 const spawnChild=(binary,argv,options)=>{requestedArgv=argv;assert.deepEqual(argv,['-I','-m','just_akash.scanner_v2_controller']);return spawn(binary,['-I','-c',`import sys;sys.path.insert(0,${JSON.stringify(fixtureRoot)});from just_akash.scanner_v2_controller import main;main()`],options);};
 const worker=async raw=>{
  const r=JSON.parse(raw);phases.push(r.phase);if(damage==='before-unknown'&&r.phase==='UNKNOWN')throw new Error('PUBLIC_SIGNED_URL_REASON_SENTINEL');
  const one=fs.mkdtempSync(path.join(directory,'phase-'));
  try{
   const artifact={async uploadArtifact(name,files){uploads++;currentRaw=fs.readFileSync(files[0]);nativeArtifact={id:100+uploads,name,size_in_bytes:500,expired:false,expires_at:new Date(Date.now()+89*86400000).toISOString(),digest:'sha256:'+'7'.repeat(64),workflow_run:{id:12345,repository_id:1071436278,head_repository_id:1071436278,head_sha:'a'.repeat(40),head_branch:'main'}};return {id:100+uploads,size:500,digest:'7'.repeat(64)};},async downloadArtifact(id,options){fs.writeFileSync(path.join(options.path,'receipt.json'),damage==='readback'?Buffer.from('{}'):currentRaw);return {downloadPath:options.path,digestMismatch:false};}};
   const native=async route=>route.includes('/artifacts/')?nativeArtifact:route.includes('/jobs/')?job():run();
   return await persistReceipt(r,environmentContext(env(),'b'.repeat(40),'777'),{directory:one,artifact,native});
  }finally{fs.rmSync(one,{recursive:true,force:true});}
 };
 try{
  if(damage)await assert.rejects(supervise({python:PYTHON,env:environment,worker,spawnChild}));else await supervise({python:PYTHON,env:environment,worker,spawnChild});
  assert.deepEqual(requestedArgv,['-I','-m','just_akash.scanner_v2_controller']);
  if(damage){assert.equal(fs.existsSync(path.join(directory,'synthetic-wire.txt')),false);assert.equal(fs.existsSync(path.join(directory,'controller-completed.txt')),false);}
  else{assert.equal(fs.readFileSync(path.join(directory,'synthetic-wire.txt'),'utf8'),'ONE_SYNTHETIC_EFFECT_NO_NETWORK\n');assert.equal(fs.readFileSync(path.join(directory,'controller-completed.txt'),'utf8'),'COMPLETE\n');assert.deepEqual(phases,['NO_WIRE','UNKNOWN','ACK']);assert.equal(uploads,3);}
 }finally{fs.rmSync(directory,{recursive:true,force:true});}
}
test('actual ScannerOperation→Pipe→Nodeparent→backend native/readbackACK gates the sole syntheticwire',()=>control(null));
test('actual hook/pipe failure at UNKNOWN never reaches synthetic effect or retry',()=>control('before-unknown'));
test('actual hook/pipe readback mismatch never reaches synthetic effect',()=>control('readback'));
