'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const S=require('./cleanup-schema.cjs'),B=require('./cleanup-backend.cjs'),F=require('./fixtures.cjs');
const root=process.env.PUBLIC_TEST_TMP_ROOT;assert(root&&path.isAbsolute(root));
async function prepared(mode='normal'){const d=fs.mkdtempSync(path.join(root,'keydb-backend-'));const f=F.fixture(d,{mode});return {f,bundle:await B.readTarget(f.c,f.deps)};}
for(const mode of ['normal','recover'])test('actual KeyDB '+mode+' reader keeps producer and actor distinct',async()=>{
 const {f,bundle}=await prepared(mode);assert.equal(bundle.producer.repository,'Borduas-Holdings/blazing');assert.equal(bundle.producer.role,'blazing-keydb-parity-qualification-v1');assert.equal(bundle.target.returned_runner_id,991);assert.notEqual(bundle.actor.sdk_sha,bundle.producer.sdk_sha);const h=await B.handlerHistory(f.c,bundle,[],f.deps);assert.deepEqual(h.wire_clear,{close:true,remove:true});
});
test('both own-family NO_WIRE intents precede held UNKNOWN and effect',async()=>{
 const {f,bundle}=await prepared();const held=[];
 const first=F.cleanupRecord(f.c,bundle,'close','NO_WIRE');const a=await B.persist(first,f.c,bundle,held,f.deps);S.ack(a,first);held.push({record:first,artifact_id:a.artifact_id,artifact_digest:a.artifact_digest,body_sha256:S.hash(first)});
 const unknown=F.cleanupRecord(f.c,bundle,'close','UNKNOWN',S.hash(first));await assert.rejects(B.persist(unknown,f.c,bundle,held,f.deps));
 const second=F.cleanupRecord(f.c,bundle,'remove','NO_WIRE');const b=await B.persist(second,f.c,bundle,held,f.deps);held.push({record:second,artifact_id:b.artifact_id,artifact_digest:b.artifact_digest,body_sha256:S.hash(second)});
 const u=await B.persist(unknown,f.c,bundle,held,f.deps);held.push({record:unknown,artifact_id:u.artifact_id,artifact_digest:u.artifact_digest,body_sha256:S.hash(unknown)});
 const current=await B.handlerHistory(f.c,bundle,held,f.deps);assert.deepEqual(current.wire_clear,{close:true,remove:true});const lost=await B.handlerHistory(f.c,bundle,[],f.deps);assert.deepEqual(lost.wire_clear,{close:false,remove:false});
});
test('BB actor or producer cannot borrow new KeyDB authority',async()=>{
 const {f,bundle}=await prepared();const actor=F.clone(f.c.actor);actor.repository='Borduas-Holdings/Blazing-Back';actor.repository_id=1071436278;assert.throws(()=>S.actor(actor));const binding=F.clone(bundle.producer);binding.role='bb-native-trufflehog-v2';assert.throws(()=>S.target(binding,bundle.target));
});
test('known target history does not infer absence from partial artifacts',async()=>{
 const d=fs.mkdtempSync(path.join(root,'keydb-incomplete-'));const f=F.fixture(d);f.flags.partial=true;await assert.rejects(B.readTarget(f.c,f.deps));
});
test('same immutable SDK revision can supply both producer and cleanup',async()=>{
 const d=fs.mkdtempSync(path.join(root,'keydb-same-sdk-'));const f=F.fixture(d);f.c.actor.sdk_sha=f.b.sdk_sha;
 for(const run of f.runs.values())for(const ref of run.referenced_workflows||[])if(ref.path.includes('/keydb-qualification-cleanup.yml@')){ref.path=ref.path.split('@')[0]+'@'+f.b.sdk_sha;ref.sha=f.b.sdk_sha;}
 const bundle=await B.readTarget(f.c,f.deps);assert.equal(bundle.actor.sdk_sha,bundle.producer.sdk_sha);assert.equal(bundle.producer.role,'blazing-keydb-parity-qualification-v1');
});
for(const mode of ['normal','recover'])test('actual environment entry admits only genuine manual '+mode,()=>{
 const c=F.current(mode),env=F.env(c),inputs={mode,sdk_sha:c.actor.sdk_sha,actor_job_id:String(c.actor.job_id),native_head_sha:c.actor.native_head_sha,target_run_id:String(c.actor.target_locator.run_id),target_run_attempt:'1',target_sdk_round:'1'};
 assert.deepEqual(B.environmentContext(env,inputs).actor,c.actor);
 assert.throws(()=>B.environmentContext({...env,GITHUB_EVENT_NAME:'workflow_run'},inputs));
 assert.throws(()=>B.environmentContext({...env,GITHUB_REF:'refs/heads/main'},inputs));
 assert.throws(()=>B.environmentContext({...env,KEYDB_QUALIFICATION_CLEANUP_MODE:mode==='normal'?'recover':'normal'},inputs));
});
async function priorNormal(f,bundle,withUnknown){
 const c=F.current('normal');c.actor.job_id=889;const job=F.job(c.actor,c.runner_name);job.status='completed';job.conclusion='failure';job.steps[0].status='completed';job.steps[0].conclusion='failure';job.steps[0].completed_at='2026-10-24T15:00:59Z';f.jobMap.set(889,job);
 f.runs.get(c.actor.run_id).referenced_workflows.push({path:`Digital-Frontier-LDA/just-akash/.github/workflows/keydb-qualification-cleanup.yml@${c.actor.sdk_sha}`,sha:c.actor.sdk_sha});
 if(withUnknown){const first=F.cleanupRecord(c,bundle,'close');f.insert(first);f.insert(F.cleanupRecord(c,bundle,'remove'));f.insert(F.cleanupRecord(c,bundle,'close','UNKNOWN',S.hash(first)));}
}
test('manual recovery inventories normal and manual actors and retains prior UNKNOWN veto',async()=>{
 const {f,bundle}=await prepared('recover');await priorNormal(f,bundle,true);const h=await B.handlerHistory(f.c,bundle,[],f.deps);
 assert.equal(h.wire_clear.close,false);assert.equal(h.prior_actor_records.length,3);assert.equal(h.handlers.length,2);assert.equal(new Set(h.handlers.map(x=>x.run_id)).size,2);
});
test('executed normal actor with missing history still vetoes manual recovery',async()=>{
 const {f,bundle}=await prepared('recover');await priorNormal(f,bundle,false);await assert.rejects(B.handlerHistory(f.c,bundle,[],f.deps));
});
test('actual worker accepts only new manual history route and held index imports exact source',()=>{
 const cp=require('node:child_process'),worker=path.join(__dirname,'cleanup-worker.cjs'),index=path.join(__dirname,'index.cjs');
 const script="const fs=require('node:fs'),assert=require('node:assert/strict'),W=require(process.argv[1]);const route='repos/Borduas-Holdings/blazing/actions/workflows/keydb-modules-parity.yml/runs?event=workflow_dispatch&created=%3E%3D2026-10-24T15%3A00%3A00Z&per_page=100&page=1';assert.equal(W.allowed(route).hostname,'api.github.com');assert.throws(()=>W.allowed(route.replace('workflow_dispatch','workflow_run')));require(process.argv[2]);fs.writeSync(1,'PUBLIC_ROUTE_CAPTURE_PASS');";
 assert.equal(cp.execFileSync(process.execPath,['-e',script,worker,index],{encoding:'utf8',timeout:5000}),'PUBLIC_ROUTE_CAPTURE_PASS');
});
for(const change of ['nonterminal','laterAttempt','otherAssignment'])test('real immutable reader refuses absent consumer with '+change,async()=>{
 const directory=fs.mkdtempSync(path.join(root,'qualification-absence-')),f=F.fixture(directory,{mode:'recover'});
 if(change==='nonterminal')f.runs.get(12345).status='in_progress';
 if(change==='laterAttempt'){
  const original=F.clone(f.runs.get(12345));f.attempts.set('12345:1',original);f.runs.get(12345).run_attempt=2;
 }
 if(change==='otherAssignment')f.jobMap.set(889,{id:889,run_id:12345,run_attempt:1,head_sha:f.b.native_head_sha,name:'PUBLIC_UNEXPECTED_ASSIGNED_JOB',status:'completed',conclusion:'success',runner_id:991,runner_name:'PUBLIC_OWNED_991',steps:[]});
 await assert.rejects(B.readTarget(f.c,f.deps));
});
