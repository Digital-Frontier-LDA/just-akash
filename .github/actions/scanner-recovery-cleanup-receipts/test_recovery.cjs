'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const S=require('./cleanup-schema.cjs'),B=require('./cleanup-backend.cjs'),F=require('./fixtures.cjs');
const {source,witness,effect,incomplete,prepared,heldIntents,nextActor}=require('./recovery-fixtures.cjs');
const root=process.env.PUBLIC_TEST_TMP_ROOT||process.env.RUNNER_TEMP;assert(root&&fs.existsSync(root));
const temp=()=>fs.mkdtempSync(path.join(root,'recovery-reader-'));
for(const reconcile of [false,true])test('real reader preserves '+(reconcile?'22be':'legacy')+' known ACK path',async()=>{const f=F.fixture(temp(),{reconcile});const b=await B.readTarget(f.c,f.deps);assert.equal(b.schema,'just-akash/scanner-cleanup-target/v1');assert.equal(b.target.dseq,'1234567');assert.equal(b.target.deployment_ack_record_sha256,S.hash(b.records.deployment));});
test('real reader returns original UNKNOWN, never a guessed DSEQ or fake ACK',async()=>{const {f,source:s}=await source();assert.equal(s.schema,S.SOURCE);assert.equal(s.target,null);assert.equal(s.records.deployment.phase,'UNKNOWN');assert(!Object.hasOwn(s.records.deployment.details,'dseq'));assert.equal(s.evidence.length,5);assert.equal(f.items.size,5);assert.deepEqual(s.phase_inventory.lease,[]);});
for(const fault of ['legacy','missing-jit','normal','late-attempt','lease-prefix','gap','duplicate','partial'])test('UNKNOWN reader refuses '+fault,async()=>{const f=incomplete(F.fixture(temp(),{mode:fault==='normal'?'normal':'terminal',reconcile:fault!=='legacy'}));
 if(fault==='missing-jit'){const row=[...f.items.values()].find(x=>x.name.includes('-jit-j777-ack'));f.items.delete(row.id);}
 if(fault==='late-attempt')f.runs.get(12345).run_attempt=2;
 if(fault==='lease-prefix')f.insert(F.producerRecords(true)[6]);
 if(fault==='gap'){const row=[...f.items.values()].find(x=>x.name.includes('-lease-j777-no_wire'));f.items.delete(row.id);}
 if(fault==='duplicate'){const row=[...f.items.values()].find(x=>x.name.includes('-lease-j777-unknown'));f.items.set(9999,{...row,id:9999});}
 if(fault==='partial')f.flags.partial=true;
 await assert.rejects(B.readTarget(f.c,f.deps));
});
test('derivation is actually uploaded/read back before both intents and UNKNOWN, original remains exact',async()=>{
 const {f,source:s,w,bundle:b}=await prepared();const original=S.canonical(s.records.deployment);assert.equal(b.derivation.record.authority,'READONLY_DERIVATION');assert.equal(b.derivation.body_sha256,S.hash(w));assert.equal(b.target.deployment_unknown_record_sha256,S.hash(s.records.deployment));assert(!Object.hasOwn(b.target,'deployment_ack_record_sha256'));
 const held=await heldIntents(f,b),r=effect(f.c,b,'close','UNKNOWN',S.hash(held[0].record)),a=await B.persist(r,f.c,b,held,f.deps);S.ack(a,r);assert.equal(r.operation_id,'scanner-v2-12345-a1-r1-close');assert.equal(r.details.source_record_sha256,S.hash(s.records.deployment));assert.equal(S.canonical(s.records.deployment),original);assert(f.items.has(b.derivation.artifact_id));
});
for(const flag of ['upload','download','body','digest'])test('failed derivation '+flag+' never yields effect target or intent',async()=>{const {f,source:s}=await source();const w=witness(s);f.flags[flag]=true;await assert.rejects(B.admitDerived(f.c,s,w,f.deps));assert([...f.items.values()].every(x=>!x.name.startsWith('scanner-v2-12345-a1-r1-close')));});
for(const field of ['owner','dseq','expected_manifest_version','precreate_baseline_sha256','deployment_unknown_record_sha256'])test('witness refuses substituted '+field,async()=>{const {f,source:s}=await source(),w=F.clone(witness(s));w.target[field]=field==='dseq'?'999':'0'.repeat(64);assert.throws(()=>S.witness(s,w));assert.equal(f.items.size,5);});
test('both intents and held derivation are required by actual Session',async()=>{const {f,bundle:b}=await prepared();const session=new S.Session();session.bundle=b;const r=effect(f.c,b,'close'),a=await B.persist(r,f.c,b,[],f.deps);session.accept(session.reserve(r),a);assert.throws(()=>session.reserve(effect(f.c,b,'close','UNKNOWN',S.hash(r))));const other=effect(f.c,b,'remove');other.derivation_evidence.artifact_id++;assert.throws(()=>new S.Session().reserve(other));});
test('derivation evidence mutation is rejected by actual immutable history reader',async()=>{const {f,bundle:b}=await prepared();const held=await heldIntents(f,b);const original=f.raws.get(b.derivation.artifact_id);f.raws.set(b.derivation.artifact_id,Buffer.from('{}'));await assert.rejects(B.handlerHistory(f.c,b,held,f.deps));f.raws.set(b.derivation.artifact_id,original);assert.deepEqual((await B.handlerHistory(f.c,b,held,f.deps)).wire_clear,{close:true,remove:true});});
test('prior recovered UNKNOWN blocks same slot after genuine late ACK, pristine removal stays independent',async()=>{
 const {f,bundle:b}=await prepared(),held=await heldIntents(f,b);const r=effect(f.c,b,'close','UNKNOWN',S.hash(held[0].record)),a=await B.persist(r,f.c,b,held,f.deps);assert.equal(a.phase,'UNKNOWN');const c=nextActor(f);f.insert(F.producerRecords(true)[5]);const known=await B.readTarget(c,f.deps);assert.equal(known.schema,'just-akash/scanner-cleanup-target/v1');assert(S.compatible(b.target,known));const h=await B.handlerHistory(c,known,[],f.deps);assert.deepEqual(h.wire_clear,{close:false,remove:true});assert.equal(h.prior_actor_records.length,3);const wrong=F.clone(known);wrong.records.deployment.parent_record_sha256='9'.repeat(64);assert.equal(S.compatible(b.target,wrong),false);
});
test('late ACK while same recovery handler is running holds renewal, never normalizes UNKNOWN',async()=>{const {f,bundle:b}=await prepared();f.insert(F.producerRecords(true)[5]);await assert.rejects(B.renewBundle(f.c,b,f.deps));});
test('new action typed public protocol does not accept arbitrary effects or legacy pipe envelopes',async()=>{const c=F.current('terminal');assert.throws(()=>S.request({schema:'just-akash/scanner-cleanup-pipe/v1',command:'READ_TARGET',actor:c.actor,payload:c.actor.target_locator},c.actor));assert.throws(()=>S.request({schema:'just-akash/scanner-recovery-cleanup-pipe/v1',command:'CREATE',actor:c.actor,payload:{}},c.actor));});
test('all six actual effect persistence phases fit the unchanged per-worker sixty native GET ceiling',async()=>{
 const f=incomplete(F.fixture(temp(),{mode:'terminal',reconcile:true})),native=f.deps.native,counts=[];let count=0;f.deps.native=async route=>{assert(++count<=60,'fixed native phase budget exceeded');return native(route);};
 async function phase(fn){count=0;const out=await fn();counts.push(count);return out;}
 const s=await phase(()=>B.readTarget(f.c,f.deps)),b=await phase(()=>B.admitDerived(f.c,s,witness(s),f.deps)),held=[];
 for(const [kind,phases]of [['close',['NO_WIRE']],['remove',['NO_WIRE']],['close',['UNKNOWN','ACK']],['remove',['UNKNOWN','ACK']]])for(const p of phases){const prior=held.filter(x=>x.record.kind===kind).at(-1);const r=effect(f.c,b,kind,p,prior?S.hash(prior.record):null),a=await phase(()=>B.persist(r,f.c,b,held,f.deps));S.ack(a,r);held.push({record:r,artifact_id:a.artifact_id,artifact_digest:a.artifact_digest,body_sha256:S.hash(r)});}
 assert.equal(held.length,6);assert(counts.every(n=>n<=60));assert(counts[1]>=24);fs.writeFileSync(path.join(root,'public-native-phase-counts.json'),S.canonical({phases:counts,ceiling:60}));
});
