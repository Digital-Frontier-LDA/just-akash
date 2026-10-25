'use strict';
const P=require('./producer-schema.cjs');
const {need,object,positive,sha,canonical,hash,akash}=P;
const ROOTS={normal:'.github/workflows/keydb-modules-parity.yml',recover:'.github/workflows/keydb-modules-parity.yml'};
const STEP='Run the acknowledged KeyDB qualification cleanup controller';
const PREFIX={normal:'Clean up owned KeyDB parity qualification / Close KeyDB qualification known identities ',recover:'Recover owned KeyDB parity qualification / Close KeyDB qualification known identities '};
function locator(v){object(v,['run_id','run_attempt','sdk_round']);positive(v.run_id);positive(v.run_attempt);need(Number.isInteger(v.sdk_round)&&v.sdk_round>=1&&v.sdk_round<=3);return v;}
function slot(v){locator(v);return `keydb-qualification-v1-${v.run_id}-a${v.run_attempt}-r${v.sdk_round}`;}
function actor(v){
 object(v,['repository','repository_id','mode','event','ref','workflow_path','workflow_sha','native_head_sha','sdk_sha','run_id','run_attempt','job_id','job_name','step_name','target_locator']);
 need(v.repository==='Borduas-Holdings/blazing'&&v.repository_id===1074974924&&Object.hasOwn(ROOTS,v.mode)&&v.workflow_path===ROOTS[v.mode]&&v.ref==='refs/heads/codex/keydb-parity-owned-d8ab-20261024');
 need(v.event==='workflow_dispatch');for(const k of ['workflow_sha','native_head_sha','sdk_sha'])sha(v[k],40);for(const k of ['run_id','run_attempt','job_id'])positive(v[k]);locator(v.target_locator);
 if(v.mode==='normal')need(v.run_id===v.target_locator.run_id&&v.run_attempt===v.target_locator.run_attempt&&v.workflow_sha===v.native_head_sha);else need(v.run_id!==v.target_locator.run_id);need(v.workflow_sha===v.native_head_sha);
 need(v.job_name===PREFIX[v.mode]+slot(v.target_locator)&&v.step_name===STEP);return v;
}
function target(producer,t){
 P.binding(producer);object(t,['owner','dseq','returned_runner_id','group_population','jit_ack_record_sha256','deployment_ack_record_sha256','lease_ack_record_sha256']);akash(t.owner);
 need(typeof t.dseq==='string'&&/^[1-9][0-9]{0,19}$/.test(t.dseq)&&BigInt(t.dseq)<=18446744073709551615n);positive(t.returned_runner_id);
 need(canonical(t.group_population)===canonical([{gseq:1,name:producer.akash_group}]));for(const k of ['jit_ack_record_sha256','deployment_ack_record_sha256'])sha(t[k]);if(t.lease_ack_record_sha256!==null)sha(t.lease_ack_record_sha256);return t;
}
function record(v){
 object(v,['schema','operation_id','kind','phase','phase_index','parent_record_sha256','actor','producer','target','target_sha256','details','authority']);need(v.schema==='just-akash/keydb-qualification-cleanup-operation/v1'&&v.authority==='candidate');actor(v.actor);target(v.producer,v.target);
 for(const k of ['run_id','run_attempt','sdk_round'])need(v.producer[k]===v.actor.target_locator[k]);need(['close','remove'].includes(v.kind)&&v.operation_id===slot(v.actor.target_locator)+'-'+v.kind&&v.target_sha256===hash(v.target));
 const phases={NO_WIRE:0,UNKNOWN:1,ACK:2};need(Object.hasOwn(phases,v.phase)&&v.phase_index===phases[v.phase]);need(v.phase==='NO_WIRE'?v.parent_record_sha256===null:typeof sha(v.parent_record_sha256)==='string');
 object(v.details,['owner','dseq','group_population','returned_runner_id','source_record_sha256',...(v.phase==='ACK'?['response_received']:[])]);
 for(const k of ['owner','dseq','group_population','returned_runner_id'])need(canonical(v.details[k])===canonical(v.target[k]));need(v.details.source_record_sha256===v.target[v.kind==='close'?'deployment_ack_record_sha256':'jit_ack_record_sha256']);if(v.phase==='ACK')need(v.details.response_received===true);
 const raw=Buffer.from(canonical(v));need(raw.length<=65536);return {record:v,raw,sha256:hash(raw),name:`${v.operation_id}-actor${v.actor.run_id}-a${v.actor.run_attempt}-j${v.actor.job_id}-${v.phase.toLowerCase()}`};
}
function request(v,a){object(v,['schema','command','actor','payload']);need(v.schema==='just-akash/keydb-qualification-cleanup-pipe/v1'&&['READ_TARGET','HISTORY','PERSIST'].includes(v.command));actor(v.actor);need(canonical(v.actor)===canonical(a));if(v.command==='READ_TARGET')need(canonical(locator(v.payload))===canonical(a.target_locator));else if(v.command==='PERSIST')need(canonical(record(v.payload).record.actor)===canonical(a));else{object(v.payload,['producer','target']);target(v.payload.producer,v.payload.target);}return v;}
function ack(v,r){
 object(v,['schema','backend','operation_id','phase','record_sha256','parent_record_sha256','artifact_id','artifact_digest','record_readback_sha256','actor','producer','target_sha256','history_sha256','created']);
 need(v.schema==='just-akash/keydb-qualification-cleanup-durable-ack/v1'&&v.backend==='github-actions-artifact/v4'&&v.created===true);
 for(const k of ['operation_id','phase','parent_record_sha256','actor','producer','target_sha256'])need(canonical(v[k])===canonical(r[k]));positive(v.artifact_id);need(/^sha256:[0-9a-f]{64}$/.test(v.artifact_digest));sha(v.history_sha256);need(v.record_sha256===hash(r)&&v.record_readback_sha256===hash(r));return v;
}
class Session{
 constructor(){this.records=[];this.held=false;this.bundle=null;}
 reserve(v){need(!this.held);const validated=record(v),prior=this.records.filter(x=>x.record.operation_id===v.operation_id).at(-1);need(prior?v.phase_index===prior.record.phase_index+1&&v.parent_record_sha256===hash(prior.record):v.phase==='NO_WIRE');
  if(prior){for(const key of ['actor','producer','target'])need(canonical(v[key])===canonical(prior.record[key]));const a={...v.details},b={...prior.record.details};delete a.response_received;delete b.response_received;need(canonical(a)===canonical(b));}
  if(v.phase==='UNKNOWN')for(const kind of ['close','remove'])need(this.records.some(x=>x.record.kind===kind&&x.record.phase==='NO_WIRE'&&x.record.target_sha256===v.target_sha256));this.held=true;return validated;
 }
 accept(v,a){ack(a,v.record);this.records.push({record:v.record,artifact_id:a.artifact_id,artifact_digest:a.artifact_digest,body_sha256:v.sha256});this.held=false;}
}
module.exports={P,need,object,positive,sha,canonical,hash,actor,target,locator,slot,record,request,ack,Session,ROOTS,STEP,PREFIX};
