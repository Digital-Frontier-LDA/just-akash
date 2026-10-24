'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const S=require('./cleanup-schema.cjs'),B=require('./cleanup-backend.cjs'),F=require('./fixtures.cjs');
const root=process.env.PUBLIC_TEST_TMP_ROOT||process.env.RUNNER_TEMP;assert(root&&fs.existsSync(root));
const temp=()=>fs.mkdtempSync(path.join(root,'cleanup-backend-'));
async function prepared(options){const d=temp(),f=F.fixture(d,options),bundle=await B.readTarget(f.c,f.deps);return {d,f,bundle};}
function detailRecord(c,bundle,kind,phase,parent=null){return F.cleanupRecord(c,bundle,kind,phase,parent);}
function prior(f,bundle,{close='NO_WIRE',remove='NO_WIRE',sameJob=false}={}){
 const c=F.current('terminal');c.actor.run_id=20000;c.actor.job_id=999;c.actor.native_head_sha='f'.repeat(40);c.actor.workflow_sha='e'.repeat(40);if(sameJob){c.actor=F.clone(f.c.actor);c.runner_name=f.c.runner_name;}
 const run=F.run(c.actor);run.status='completed';run.conclusion='failure';f.runs.set(run.id,run);const job=F.job(c.actor,c.runner_name);job.status='completed';job.conclusion='failure';job.steps[0].status='completed';job.steps[0].conclusion='failure';job.steps[0].completed_at='2026-10-24T15:02:00Z';f.jobMap.set(job.id,job);
 const output=[];for(const [kind,last]of [['close',close],['remove',remove]]){if(last===null)continue;let parent=null;for(const phase of ['NO_WIRE','UNKNOWN','ACK']){const r=detailRecord(c,bundle,kind,phase,parent);output.push(f.insert(r));parent=S.hash(r);if(phase===last)break;}}
 return output;
}
for(const reconcile of [false,true])test('real immutable reader joins '+(reconcile?'22be reconcile':'legacy')+' ACKs without caller owner/ID',async()=>{
 const {f,bundle}=await prepared({reconcile});assert.equal(bundle.target.dseq,'1234567');assert.equal(bundle.target.returned_runner_id,991);assert.equal(bundle.evidence.length,9);assert.equal(bundle.records.deployment.kind,reconcile?'lease_reconcile':'lease');if(reconcile)assert.equal(bundle.records.deployment.details.precreate_baseline.schema,'just-akash/scanner-precreate-baseline/v1');assert.notEqual(bundle.actor.sdk_sha,bundle.producer.sdk_sha);assert.equal(bundle.producer.producer_job_id,777);assert.equal(bundle.actor.job_id,888);
 const h=await B.handlerHistory(f.c,bundle,[],f.deps);assert.deepEqual(h.wire_clear,{close:true,remove:true});assert.equal(h.current_actor_records.length,0);
});
test('terminal actor keeps actual native head separate from protected source and producer',async()=>{const {f,bundle}=await prepared({mode:'terminal'});assert.equal(bundle.actor.workflow_sha,'e'.repeat(40));assert.equal(bundle.actor.native_head_sha,'d'.repeat(40));assert.equal(bundle.producer.native_head_sha,'a'.repeat(40));assert.equal(B.environmentContext(F.env(f.c),{mode:'terminal',sdk_sha:f.c.actor.sdk_sha,actor_job_id:'888',native_head_sha:'d'.repeat(40),target_run_id:'12345',target_run_attempt:'1',target_sdk_round:'1'}).actor.native_head_sha,'d'.repeat(40));});
for(const bad of [{status:'completed',conclusion:'skipped'},{runner_id:0},{runner_name:'foreign'},{head_sha:'f'.repeat(40)},{labels:['self-hosted']}])test('actual current actor rejects '+JSON.stringify(bad),async()=>{const f=F.fixture(temp());f.flags.job=bad;await assert.rejects(B.readTarget(f.c,f.deps));assert.equal(f.items.size,9);});
for(const bad of [{event:'pull_request'},{run_attempt:2},{path:'.github/workflows/other.yml'},{repository:{id:1071436278,full_name:'Borduas-Holdings/Blazing-Back',private:false}},{referenced_workflows:[]}])test('actual current actor native run refuses '+Object.keys(bad),async()=>{const f=F.fixture(temp());f.flags.run=bad;await assert.rejects(B.readTarget(f.c,f.deps));});
for(const flag of ['partial','digest','download','body'])test('immutable reader holds '+flag,async()=>{const f=F.fixture(temp());f.flags[flag]=true;await assert.rejects(B.readTarget(f.c,f.deps));});
for(const phase of ['UNKNOWN','ACK'])test('prior '+phase+' blocks only same effect and preserves pristine removal',async()=>{const {f,bundle}=await prepared();prior(f,bundle,{close:phase,remove:'NO_WIRE'});const h=await B.handlerHistory(f.c,bundle,[],f.deps);assert.deepEqual(h.wire_clear,{close:false,remove:true});assert(h.prior_actor_records.length>=3);});
test('prior executed handler missing removal intent is incomplete HOLD',async()=>{const {f,bundle}=await prepared();prior(f,bundle,{close:'ACK',remove:null});await assert.rejects(B.handlerHistory(f.c,bundle,[],f.deps));});
test('current UNKNOWN requires exact held supervisor phase, never reconstructed from name',async()=>{const {f,bundle}=await prepared();const rows=[];for(const kind of ['close','remove'])rows.push(f.insert(detailRecord(f.c,bundle,kind,'NO_WIRE')));const unknown=f.insert(detailRecord(f.c,bundle,'close','UNKNOWN',S.hash(rows[0].record)));const held=[...rows,unknown];const h=await B.handlerHistory(f.c,bundle,held,f.deps);assert.deepEqual(h.wire_clear,{close:true,remove:true});assert.equal(h.current_actor_records.length,3);const untracked=await B.handlerHistory(f.c,bundle,[],f.deps);assert.deepEqual(untracked.wire_clear,{close:false,remove:false});});
test('actual persistence requires both intents then ACKs immutable UNKNOWN before synthetic wire',async()=>{const {f,bundle}=await prepared();const held=[];let wire=0;
 for(const kind of ['close','remove']){const r=detailRecord(f.c,bundle,kind,'NO_WIRE');const ack=await B.persist(r,f.c,bundle,held,f.deps);S.ack(ack,r);held.push({record:r,artifact_id:ack.artifact_id,artifact_digest:ack.artifact_digest,body_sha256:S.hash(r)});assert(Buffer.byteLength(S.canonical(ack))+1<=4096);}
 const unknown=detailRecord(f.c,bundle,'close','UNKNOWN',S.hash(held[0].record));const ack=await B.persist(unknown,f.c,bundle,held,f.deps);S.ack(ack,unknown);held.push({record:unknown,artifact_id:ack.artifact_id,artifact_digest:ack.artifact_digest,body_sha256:S.hash(unknown)});wire++;assert.equal(wire,1);
 const done=detailRecord(f.c,bundle,'close','ACK',S.hash(unknown));S.ack(await B.persist(done,f.c,bundle,held,f.deps),done);await assert.rejects(B.persist(unknown,f.c,bundle,held,f.deps));assert.equal(wire,1);
});
test('UNKNOWN before second intent is rejected by actual backend and session',async()=>{const {f,bundle}=await prepared();const r=detailRecord(f.c,bundle,'close','NO_WIRE'),held=f.insert(r);const unknown=detailRecord(f.c,bundle,'close','UNKNOWN',S.hash(r));await assert.rejects(B.persist(unknown,f.c,bundle,[held],f.deps));const session=new S.Session();session.records=[held];assert.throws(()=>session.reserve(unknown));});
test('failed upload leaves caller phase poisoned and never reaches synthetic effect',async()=>{const {f,bundle}=await prepared();const session=new S.Session(),r=detailRecord(f.c,bundle,'close','NO_WIRE');session.reserve(r);f.flags.upload=true;await assert.rejects(B.persist(r,f.c,bundle,[],f.deps));assert.equal(session.held,true);assert.throws(()=>session.reserve(r));});
for(const count of [0,1,2,3])test('known-DSEQ canceled producer preserves exact lease prefix length '+count,async()=>{
 const f=F.fixture(temp(),{mode:'terminal',reconcile:true});for(const [id,meta]of f.items)if(meta.name.includes('-lease_bind-')&&['no_wire','unknown','ack'].indexOf(meta.name.split('-').at(-1))>=count){f.items.delete(id);f.raws.delete(id);}
 const bundle=await B.readTarget(f.c,f.deps);assert.equal(bundle.target.dseq,'1234567');assert.equal(bundle.target.returned_runner_id,991);assert.deepEqual(bundle.phase_inventory.lease,['NO_WIRE','UNKNOWN','ACK'].slice(0,count));assert.equal(bundle.evidence.length,6+count);assert.equal(bundle.records.lease===null,count<3);assert.equal(bundle.target.lease_ack_record_sha256===null,count<3);assert.equal(bundle.records.deployment.kind,'lease_reconcile');
});
for(const fault of ['gap','duplicate','unexpected-phase','foreign-job','missing-deployment-ACK'])test('complete producer inventory refuses '+fault,async()=>{
 const f=F.fixture(temp());const lease=[...f.items.values()].filter(x=>x.name.includes('-lease_bind-'));
 if(fault==='gap'){f.items.delete(lease[0].id);}
 else if(fault==='duplicate'){const copy={...lease[0],id:9999};f.items.set(copy.id,copy);f.raws.set(copy.id,f.raws.get(lease[0].id));}
 else if(fault==='unexpected-phase'){const copy={...lease[0],id:9999,name:lease[0].name.replace('no_wire','ambiguous')};f.items.set(copy.id,copy);}
 else if(fault==='foreign-job'){const copy={...lease[0],id:9999,name:lease[0].name.replace('j777','j778')};f.items.set(copy.id,copy);}
 else{const row=[...f.items.values()].find(x=>x.name.includes('-lease-j777-ack'));f.items.delete(row.id);}
 await assert.rejects(B.readTarget(f.c,f.deps));
});
test('renewal refuses a late lease phase rather than silently normalizing cached absence',async()=>{
 const f=F.fixture(temp());for(const [id,meta]of f.items)if(meta.name.includes('-lease_bind-')){f.items.delete(id);f.raws.delete(id);}const bundle=await B.readTarget(f.c,f.deps);f.insert(F.producerRecords()[6]);await assert.rejects(B.renewBundle(f.c,bundle,f.deps));
});
test('all callback attempts preserve prior UNKNOWN even when latest rerun has no executed job',async()=>{
 const {f,bundle}=await prepared();prior(f,bundle,{close:'UNKNOWN',remove:'NO_WIRE'});const old=F.clone(f.runs.get(20000));f.attempts.set('20000:1',old);const latest={...old,run_attempt:2,status:'queued',conclusion:null};f.runs.set(20000,latest);f.attempts.set('20000:2',latest);const h=await B.handlerHistory(f.c,bundle,[],f.deps);assert.deepEqual(h.wire_clear,{close:false,remove:true});assert(h.handlers.some(x=>x.run_id===20000&&x.run_attempt===1));
});
test('earlier executed callback with no remove intent cannot hide behind later queued attempt',async()=>{
 const {f,bundle}=await prepared();prior(f,bundle,{close:'NO_WIRE',remove:null});const old=F.clone(f.runs.get(20000));f.attempts.set('20000:1',old);const latest={...old,run_attempt:2,status:'queued',conclusion:null};f.runs.set(20000,latest);f.attempts.set('20000:2',latest);await assert.rejects(B.handlerHistory(f.c,bundle,[],f.deps));
});
test('another active executed handler cannot wire concurrently even with pristine intents',async()=>{
 const {f,bundle}=await prepared();prior(f,bundle,{close:'NO_WIRE',remove:'NO_WIRE'});const job=f.jobMap.get(999);job.status='in_progress';job.conclusion=null;job.steps[0].status='in_progress';job.steps[0].conclusion=null;job.steps[0].completed_at=null;await assert.rejects(B.handlerHistory(f.c,bundle,[],f.deps));
});
test('target effect artifact cannot disappear behind an unknown native job name',async()=>{
 const {f,bundle}=await prepared();prior(f,bundle,{close:'NO_WIRE',remove:'NO_WIRE'});f.jobMap.get(999).name='Unrecognized cleanup caller';await assert.rejects(B.handlerHistory(f.c,bundle,[],f.deps));
});
test('callback attempt ceiling holds instead of truncating earlier handlers',async()=>{
 const {f,bundle}=await prepared();prior(f,bundle,{close:'NO_WIRE',remove:'NO_WIRE'});f.runs.get(20000).run_attempt=11;await assert.rejects(B.handlerHistory(f.c,bundle,[],f.deps));
});
