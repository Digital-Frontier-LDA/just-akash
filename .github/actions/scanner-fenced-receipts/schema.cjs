'use strict';
const P=require('./producer-schema.cjs');const {need,object,positive,sha,canonical,hash}=P;
const PROFILE='bb-native-trufflehog-v2-fenced',INTENT='just-akash/scanner-no-create-intent/v1',TERMINAL='just-akash/scanner-no-create-terminal/v1',EXIT=66;
const RUNTIME='just-akash/scanner-may-create-reservation/v1',RUNTIME_ACK='just-akash/scanner-may-create-ack/v1';
function runtimeReservation(r){
 object(r,['schema','operation_id','binding','producer_profile','returned_runner_id','jit_ack_record_sha256','state']);P.binding(r.binding);const b=r.binding;
 need(r.schema===RUNTIME&&r.producer_profile===PROFILE&&r.state==='MAY_CREATE'&&r.operation_id===`scanner-v2-${b.repository_id}-${b.run_id}-a${b.run_attempt}-r${b.sdk_round}-lease`);positive(r.returned_runner_id);sha(r.jit_ack_record_sha256);need(Buffer.byteLength(canonical(r))<=65536);return r;
}
function runtimeAck(r){return {schema:RUNTIME_ACK,operation_id:r.operation_id,request_sha256:hash(r),binding_sha256:hash(r.binding),state:'MAY_CREATE',acknowledged:true,scope:'CURRENT_PARENT_SESSION_ONLY'};}
function fence(r){
 object(r,['schema','operation_id','kind','phase','phase_index','parent_record_sha256','profile_sha256','binding','details','authority']);const terminal=r.schema===TERMINAL;need(terminal||r.schema===INTENT);const b=r.binding;need(r.kind==='prelease_fence'&&r.authority==='candidate'&&r.profile_sha256===P.binding(b)&&r.operation_id===`scanner-v2-${b.repository_id}-${b.run_id}-a${b.run_attempt}-r${b.sdk_round}-lease`);need(r.phase===(terminal?'NO_CREATE':'FENCE')&&r.phase_index===(terminal?1:0));sha(r.parent_record_sha256);const d=r.details;object(d,['producer_profile','returned_runner_id','jit_ack_record_sha256','stage','create_state',...(terminal?['child_exit_code','child_signal','create_frames_reserved']:[])]);positive(d.returned_runner_id);sha(d.jit_ack_record_sha256);need(d.producer_profile===PROFILE&&['guest_contract','private_scratch','reader_path_preflight'].includes(d.stage)&&d.create_state==='FENCED');if(terminal)need(d.child_exit_code===EXIT&&d.child_signal===null&&d.create_frames_reserved===0);else need(r.parent_record_sha256===d.jit_ack_record_sha256);const raw=Buffer.from(canonical(r));need(raw.length<=65536);return {record:r,raw,sha256:hash(raw),name:r.operation_id+'-j'+b.producer_job_id+'-'+r.phase.toLowerCase()};
}
function validate(r){return r.schema===INTENT||r.schema===TERMINAL?fence(r):P.validate(r);}
class PhaseSession{
 constructor(){this.original=new P.PhaseSession();this.held=false;this.log=[];this.intent=null;this.fenced=false;this.createFrames=0;this.mayCreate=false;}
 reserveRuntime(r){
  runtimeReservation(r);need(!this.held&&!this.fenced&&!this.mayCreate&&this.createFrames===0&&this.intent===null);
  const jit=this.log.filter(x=>x.kind==='jit'&&x.phase==='ACK');need(jit.length===1&&this.log.length===3&&canonical(jit[0].binding)===canonical(r.binding)&&jit[0].details.runner_id===r.returned_runner_id&&hash(jit[0])===r.jit_ack_record_sha256);
  // Irreversible before a control ACK is sent; no durability/CAS is claimed.
  this.mayCreate=true;return runtimeAck(r);
 }
 reserve(r){
  need(!this.held);if(r.schema===INTENT){const v=fence(r);need(!this.fenced&&!this.mayCreate&&this.createFrames===0&&this.intent===null);const jit=this.log.filter(x=>x.kind==='jit'&&x.phase==='ACK');need(jit.length===1&&this.log.length===3&&canonical(jit[0].binding)===canonical(r.binding)&&jit[0].details.runner_id===r.details.returned_runner_id&&hash(jit[0])===r.details.jit_ack_record_sha256);this.fenced=true;this.held=true;return v;}
  need(r.schema==='just-akash/scanner-operation/v2'&&!this.fenced&&['jit','lease_reconcile','lease_bind'].includes(r.kind));if(r.kind!=='jit'){need(this.mayCreate);this.createFrames++;}else need(!this.mayCreate);this.held=true;return this.original.reserve(r);
 }
 accept(v){need(this.held);if(v.record.schema===INTENT)this.intent=v.record;else this.original.accept(v);this.log.push(v.record);this.held=false;}
 terminal(code,signal){need(code===EXIT&&signal===null&&!this.held&&this.fenced&&!this.mayCreate&&this.intent!==null&&this.createFrames===0);return fence({...this.intent,schema:TERMINAL,phase:'NO_CREATE',phase_index:1,parent_record_sha256:hash(this.intent),details:{...this.intent.details,child_exit_code:code,child_signal:signal,create_frames_reserved:this.createFrames}});}
}
module.exports={...P,validate,PhaseSession,fence,runtimeReservation,runtimeAck,RUNTIME,RUNTIME_ACK,PROFILE,INTENT,TERMINAL,EXIT};
