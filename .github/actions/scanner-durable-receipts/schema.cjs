'use strict';
const crypto = require('node:crypto');
const PROVIDERS = ['akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk','akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z','akash1z9nr23cgweu45g2jktfx95v7g2xp8qlsa3ys2x'];
const IMAGE='docker.io/digitalfrontierunipessoallda/akash-runner@sha256:e71bf4be0e3a59a57ff791b0fc3bc3a31f216f66807b6ac41f1a23346321baa4';
function need(ok){if(!ok)throw new Error('PUBLIC_RECEIPT_UNQUALIFIED');}
function object(value,keys){need(value!==null&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).sort().join('\0')===[...keys].sort().join('\0'));}
function positive(value){need(Number.isSafeInteger(value)&&value>0);return value;}
function sha(value,length=64){need(typeof value==='string'&&new RegExp('^[a-f0-9]{'+length+'}$').test(value));return value;}
function canonical(value){
 if(value===null)return 'null';
 if(typeof value==='boolean')return value?'true':'false';
 if(typeof value==='number'){need(Number.isSafeInteger(value));return JSON.stringify(value);}
 if(typeof value==='string'){need(/^[\x20-\x7e]*$/.test(value));return JSON.stringify(value);}
 if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
 need(value!==null&&typeof value==='object');return '{'+Object.keys(value).sort().map(k=>canonical(k)+':'+canonical(value[k])).join(',')+'}';
}
function hash(value){return crypto.createHash('sha256').update(Buffer.isBuffer(value)?value:Buffer.from(canonical(value))).digest('hex');}
function akash(address){
 need(typeof address==='string'&&/^akash1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{38}$/.test(address));
 const alphabet='qpzry9x8gf2tvdw0s3jn54khce6mua7l', data=[...address.slice(6)].map(c=>alphabet.indexOf(c));
 const hrp=[...Buffer.from('akash')];let chk=1;
 for(const v of [...hrp.map(x=>x>>5),0,...hrp.map(x=>x&31),...data]){
  const top=chk>>>25;chk=((chk&0x1ffffff)<<5)^v;
  const gen=[0x3b6a57b2,0x26508e6d,0x1ea119fa,0x3d4233dd,0x2a1462b3];for(let i=0;i<5;i++)if((top>>>i)&1)chk^=gen[i];
 }
 need((chk>>>0)===1);return address;
}
function binding(b){
 object(b,['repository','repository_id','workflow_path','workflow_sha','native_head_sha','sdk_sha','event','ref','run_id','run_attempt','sdk_round','producer_job_id','role','image','providers','resources','runner_name','runner_label','akash_group','runner_group_id']);
 need(b.repository==='Borduas-Holdings/Blazing-Back'&&b.repository_id===1071436278&&b.workflow_path==='.github/workflows/secret-scanning.yml');
 need(b.event==='workflow_dispatch'&&b.ref==='refs/heads/main');
 sha(b.workflow_sha,40);sha(b.native_head_sha,40);sha(b.sdk_sha,40);need(b.workflow_sha===b.native_head_sha);
 positive(b.run_id);positive(b.run_attempt);positive(b.producer_job_id);positive(b.runner_group_id);need(Number.isInteger(b.sdk_round)&&b.sdk_round>=1&&b.sdk_round<=3);
 need(b.akash_group===`borduas-th-run-${b.run_id}-a${b.run_attempt}-r${b.sdk_round}-end`);
 need(b.runner_name===`dfci-bb-th-${b.run_id}-a${b.run_attempt}-r${b.sdk_round}`&&b.runner_label===b.runner_name&&b.role==='bb-native-trufflehog-v2'&&b.image===IMAGE);
 object(b.resources,['cpu','memory','storage','count']);need(b.resources.cpu==='4'&&b.resources.memory==='16Gi'&&b.resources.storage==='96Gi'&&b.resources.count===1);
 need(Array.isArray(b.providers)&&b.providers.length===3&&new Set(b.providers).size===3&&b.providers.every(p=>PROVIDERS.includes(p)));
 const profile={};for(const key of ['role','image','resources','providers','runner_name','runner_group_id','akash_group','sdk_sha'])profile[key]=b[key];
 return hash(profile);
}
function validate(record){
 object(record,['schema','operation_id','kind','phase','phase_index','parent_record_sha256','profile_sha256','binding','details','authority']);
 need(record.schema==='just-akash/scanner-operation/v2'&&record.authority==='candidate'&&['jit','lease','lease_bind','lease_reconcile'].includes(record.kind));
 const indexes={NO_WIRE:0,UNKNOWN:1,ACK:2};need(Object.hasOwn(indexes,record.phase)&&record.phase_index===indexes[record.phase]);
 need(record.phase==='NO_WIRE'?record.parent_record_sha256===null:typeof sha(record.parent_record_sha256)==='string');
 need(binding(record.binding)===record.profile_sha256);
 const b=record.binding;need(record.operation_id===`scanner-v2-${b.repository_id}-${b.run_id}-a${b.run_attempt}-r${b.sdk_round}-${record.kind==='lease_reconcile'?'lease':record.kind}`);
 const d=record.details;
 if(record.kind==='jit'){
  object(d,['runner_name','runner_group_id','labels','request_body_sha256',...(record.phase==='ACK'?['runner_id']:[])]);
  need(d.runner_name===b.runner_name&&d.runner_group_id===b.runner_group_id&&canonical(d.labels)===canonical(['self-hosted','linux','akash',b.runner_label]));sha(d.request_body_sha256);if(record.phase==='ACK')positive(d.runner_id);
 }else if(record.kind==='lease'||record.kind==='lease_reconcile'){
  object(d,['owner','group_population','public_request_sha256','wire_request_sha256','returned_runner_id','jit_ack_record_sha256','deposit_usd',...(record.kind==='lease_reconcile'?['expected_manifest_version','precreate_baseline','payload_profile']:[]),...(record.phase==='ACK'?['dseq']:[])]);
  akash(d.owner);need(canonical(d.group_population)===canonical([{gseq:1,name:b.akash_group}]));sha(d.public_request_sha256);sha(d.wire_request_sha256);sha(d.jit_ack_record_sha256);positive(d.returned_runner_id);need(d.deposit_usd==='6');
  if(record.kind==='lease_reconcile'){
   need(typeof d.expected_manifest_version==='string');const version=Buffer.from(d.expected_manifest_version,'base64');need(version.length===32&&version.toString('base64')===d.expected_manifest_version);
   need(d.payload_profile==='scanner-private-e71-tcp65535/v1');const baseline=d.precreate_baseline;object(baseline,['schema','owner','states','dseqs']);
   need(baseline.schema==='just-akash/scanner-precreate-baseline/v1'&&baseline.owner===d.owner&&canonical(baseline.states)===canonical(['active','closed']));
   need(Array.isArray(baseline.dseqs)&&baseline.dseqs.length<=1000);let previous=0n;
   for(const value of baseline.dseqs){need(typeof value==='string'&&/^[1-9][0-9]{0,19}$/.test(value));const id=BigInt(value);need(id>previous&&id<=18446744073709551615n);previous=id;}
   need(Buffer.byteLength(canonical(baseline))<=32768);
  }
  if(record.phase==='ACK'){need(typeof d.dseq==='string'&&/^[1-9][0-9]{0,19}$/.test(d.dseq)&&BigInt(d.dseq)<=18446744073709551615n);}
 }
 if(record.kind==='lease_bind'){
  object(d,['owner','dseq','provider','gseq','returned_runner_id','jit_ack_record_sha256','deployment_ack_record_sha256','public_request_sha256','wire_request_sha256',...(record.phase==='ACK'?['lease_response_received']:[])]);
  akash(d.owner);need(typeof d.dseq==='string'&&/^[1-9][0-9]{0,19}$/.test(d.dseq)&&BigInt(d.dseq)<=18446744073709551615n);
  need(PROVIDERS.includes(d.provider)&&d.gseq===1);positive(d.returned_runner_id);sha(d.jit_ack_record_sha256);sha(d.deployment_ack_record_sha256);sha(d.public_request_sha256);sha(d.wire_request_sha256);if(record.phase==='ACK')need(d.lease_response_received===true);
 }
 const raw=Buffer.from(canonical(record));need(raw.length<=65536);return {record,raw,sha256:hash(raw),name:record.operation_id+'-j'+record.binding.producer_job_id+'-'+record.phase.toLowerCase()};
}
function producer(b){return Object.fromEntries(['repository_id','run_id','run_attempt','workflow_sha','native_head_sha','producer_job_id'].map(k=>[k,b[k]]));}
function intent(record){const d={...record.details};if(record.kind==='jit')delete d.runner_id;else if(record.kind==='lease'||record.kind==='lease_reconcile')delete d.dseq;else delete d.lease_response_received;return canonical(d);}
class PhaseSession{
 constructor(){this.records=new Map();this.held=false;}
 reserve(record){
  need(!this.held);const v=validate(record),prior=this.records.get(record.operation_id);
  need(prior?record.phase_index===prior.index+1&&record.parent_record_sha256===prior.sha256&&intent(record)===prior.intent:record.phase==='NO_WIRE');
  // Reserve before upload. Failure/unknown never permits same-phase invocation again.
  this.held=true;return v;
 }
 accept(v){this.records.set(v.record.operation_id,{index:v.record.phase_index,sha256:v.sha256,intent:intent(v.record)});this.held=false;}
}
module.exports={need,object,positive,sha,canonical,hash,akash,validate,producer,PhaseSession,IMAGE,PROVIDERS};
