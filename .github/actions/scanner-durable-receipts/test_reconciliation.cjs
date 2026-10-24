'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),path=require('node:path');
const {record}=require('./fixtures.cjs');const {validate,hash,canonical,PhaseSession}=require('./schema.cjs');
function reconciled(phase='NO_WIRE',parent=null){const r=record(phase,parent,'lease');r.kind='lease_reconcile';r.details.expected_manifest_version=Buffer.alloc(32,1).toString('base64');r.details.payload_profile='scanner-private-e71-tcp65535/v1';r.details.precreate_baseline={schema:'just-akash/scanner-precreate-baseline/v1',owner:r.details.owner,states:['active','closed'],dseqs:['1','9007199254740993']};return r;}
test('reconciliation actual backend schema agrees with Python and keeps same irreversible lease slot',()=>{
 const {spawnSync}=require('node:child_process');const values=[];let parent=null;const session=new PhaseSession();
 for(const phase of ['NO_WIRE','UNKNOWN','ACK']){const r=reconciled(phase,parent),v=session.reserve(r);session.accept(v);values.push(r);parent=v.sha256;assert.ok(r.operation_id.endsWith('-lease'));}
 const p=spawnSync(process.env.PYTHON,['-I','-B',path.join(__dirname,'tests/generate_schema_records.py')],{input:JSON.stringify(values),encoding:'utf8',timeout:10000,env:{PATH:process.env.PATH,TMPDIR:process.env.TMPDIR,PYTHONDONTWRITEBYTECODE:'1'}});
 assert.equal(p.status,0,p.stderr);const result=JSON.parse(p.stdout);for(let i=0;i<values.length;i++){assert.equal(result[i].raw,canonical(values[i]));assert.equal(result[i].sha256,hash(values[i]));}
});
test('partial,unsorted,foreign,oversize,secret-bearing or unsupported baseline/version is refused before upload',()=>{
 for(const change of [r=>r.details.precreate_baseline.dseqs.push('1'),r=>r.details.precreate_baseline.dseqs.reverse(),r=>r.details.precreate_baseline.owner='foreign',r=>r.details.precreate_baseline.extra='SYNTHETIC_SECRET',r=>r.details.precreate_baseline.states=['active'],r=>r.details.precreate_baseline.dseqs=['18446744073709551616'],r=>r.details.precreate_baseline.dseqs=Array.from({length:1001},(_,i)=>String(i+1)),r=>r.details.expected_manifest_version='abc',r=>r.details.payload_profile='legacy',r=>r.operation_id+='-new-slot']){const r=reconciled();change(r);assert.throws(()=>validate(r));}
});
test('old UNKNOWN cannot switch contract or replay the same operation phase',()=>{
 const session=new PhaseSession(),old=record('NO_WIRE',null,'lease');let prior=session.reserve(old);session.accept(prior);
 assert.throws(()=>session.reserve(reconciled('UNKNOWN',prior.sha256)));
 const oldSession=new PhaseSession();prior=oldSession.reserve(old);oldSession.accept(prior);const unknown=record('UNKNOWN',prior.sha256,'lease');prior=oldSession.reserve(unknown);oldSession.accept(prior);assert.throws(()=>oldSession.reserve(reconciled('UNKNOWN',prior.sha256)));
});
test('actual upload/native-readback boundary accepts new kind only after exact phase-body readback',async()=>{
 const fs=require('node:fs'),os=require('node:os');const {env,run,job}=require('./fixtures.cjs');const {environmentContext,persistReceipt}=require('./backend.cjs');
 const directory=fs.mkdtempSync(path.join(process.env.TMPDIR,'reconcile-backend-'));let raw,upload=0,download=0;const r=reconciled();let metadata;
 const artifact={async uploadArtifact(name,files){upload++;raw=fs.readFileSync(files[0]);metadata={id:991,name,size_in_bytes:500,digest:'sha256:'+'7'.repeat(64),expired:false,expires_at:new Date(Date.now()+89*86400000).toISOString(),workflow_run:{id:12345,repository_id:1071436278,head_repository_id:1071436278,head_branch:'main',head_sha:'a'.repeat(40)}};return {id:991,size:500,digest:'7'.repeat(64)};},async downloadArtifact(id,options){assert.equal(id,991);download++;fs.writeFileSync(path.join(options.path,'receipt.json'),raw);return {downloadPath:options.path,digestMismatch:false};}};
 const native=async route=>route.includes('/artifacts/')?metadata:route.includes('/jobs/')?job():run();
 try{const ack=await persistReceipt(r,environmentContext(env(),'b'.repeat(40),'777'),{directory,artifact,native});assert.equal(upload,1);assert.equal(download,1);assert.equal(ack.record_readback_sha256,hash(r));assert.equal(ack.operation_id,r.operation_id);}finally{fs.rmSync(directory,{recursive:true,force:true});}
});
