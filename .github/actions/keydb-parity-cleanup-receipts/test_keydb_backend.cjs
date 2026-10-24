'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const S=require('./cleanup-schema.cjs'),B=require('./cleanup-backend.cjs'),F=require('./fixtures.cjs');
const root=process.env.PUBLIC_TEST_TMP_ROOT;assert(root&&path.isAbsolute(root));
async function prepared(mode='normal'){const d=fs.mkdtempSync(path.join(root,'keydb-backend-'));const f=F.fixture(d,{mode});return {f,bundle:await B.readTarget(f.c,f.deps)};}
for(const mode of ['normal','terminal'])test('actual KeyDB '+mode+' reader keeps producer and actor distinct',async()=>{
 const {f,bundle}=await prepared(mode);assert.equal(bundle.producer.repository,'Borduas-Holdings/blazing');assert.equal(bundle.producer.role,'blazing-keydb-modules-parity-v1');assert.equal(bundle.target.returned_runner_id,991);assert.notEqual(bundle.actor.sdk_sha,bundle.producer.sdk_sha);const h=await B.handlerHistory(f.c,bundle,[],f.deps);assert.deepEqual(h.wire_clear,{close:true,remove:true});
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
 for(const run of f.runs.values())for(const ref of run.referenced_workflows||[])if(ref.path.includes('/keydb-parity-cleanup.yml@')){ref.path=ref.path.split('@')[0]+'@'+f.b.sdk_sha;ref.sha=f.b.sdk_sha;}
 const bundle=await B.readTarget(f.c,f.deps);assert.equal(bundle.actor.sdk_sha,bundle.producer.sdk_sha);assert.equal(bundle.producer.role,'blazing-keydb-modules-parity-v1');
});
