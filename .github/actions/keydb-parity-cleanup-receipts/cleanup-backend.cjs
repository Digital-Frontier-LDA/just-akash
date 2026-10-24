'use strict';
const fs=require('node:fs'),path=require('node:path');
const S=require('./cleanup-schema.cjs');const {P,need,object,positive,sha,canonical,hash,actor,target,slot,record,ROOTS,STEP}=S;
const REPO='repos/Borduas-Holdings/blazing';
const PRODUCER_JOB='Provision one owned KeyDB module parity runner / Provision KeyDB parity through acknowledged JIT';
const PRODUCER_STEP='Run the acknowledged KeyDB parity controller';
const TERMINAL=['success','failure','cancelled','timed_out'];
function stamp(v){need(typeof v==='string'&&Number.isFinite(Date.parse(v)));return v;}
function environmentContext(env,inputs){
 need(env.GITHUB_ACTIONS==='true'&&env.GITHUB_SERVER_URL==='https://github.com'&&env.RUNNER_ENVIRONMENT==='github-hosted'&&env.RUNNER_OS==='Linux'&&env.GITHUB_JOB==='cleanup');
 const number=s=>{need(typeof s==='string'&&/^[1-9][0-9]*$/.test(s));return positive(Number(s));};
 const mode=inputs.mode;need(Object.hasOwn(ROOTS,mode));need(env.GITHUB_WORKFLOW_REF===`Borduas-Holdings/blazing/${ROOTS[mode]}@refs/heads/main`);
 need(env.GITHUB_REPOSITORY==='Borduas-Holdings/blazing'&&env.GITHUB_REPOSITORY_ID==='1074974924'&&env.GITHUB_REF==='refs/heads/main');
 const locator={run_id:number(inputs.target_run_id),run_attempt:number(inputs.target_run_attempt),sdk_round:number(inputs.target_sdk_round)};
 const a=actor({repository:env.GITHUB_REPOSITORY,repository_id:1074974924,mode,event:env.GITHUB_EVENT_NAME,ref:env.GITHUB_REF,workflow_path:ROOTS[mode],workflow_sha:sha(env.GITHUB_WORKFLOW_SHA,40),native_head_sha:sha(inputs.native_head_sha,40),sdk_sha:sha(inputs.sdk_sha,40),run_id:number(env.GITHUB_RUN_ID),run_attempt:number(env.GITHUB_RUN_ATTEMPT),job_id:number(inputs.actor_job_id),job_name:S.PREFIX+slot(locator),step_name:STEP,target_locator:locator});
 need(env.GITHUB_SHA===a.workflow_sha&&typeof env.RUNNER_NAME==='string'&&/^[\x20-\x7e]{1,128}$/.test(env.RUNNER_NAME));return {actor:a,runner_name:env.RUNNER_NAME};
}
function runShape(v,runId,attempt,workflow,event,head,sdk,callee){
 need(v&&typeof v==='object'&&v.id===runId&&v.run_attempt===attempt&&v.path===workflow&&v.event===event&&v.head_sha===head&&v.head_branch==='main');positive(v.id);positive(v.run_attempt);positive(v.workflow_id);
 need(v.repository?.id===1074974924&&v.repository?.full_name==='Borduas-Holdings/blazing'&&v.repository?.private===true&&v.head_repository?.id===1074974924);
 need(Array.isArray(v.referenced_workflows)&&v.referenced_workflows.filter(r=>r.path===`Digital-Frontier-LDA/just-akash/.github/workflows/${callee}@${sdk}`&&r.sha===sdk).length===1);stamp(v.created_at);return v;
}
function activeJob(v,a,runnerName){
 need(v?.id===a.job_id&&v.run_id===a.run_id&&v.run_attempt===a.run_attempt&&v.head_sha===a.native_head_sha&&v.name===a.job_name&&v.status==='in_progress'&&v.conclusion===null);positive(v.id);positive(v.run_attempt);positive(v.runner_id);
 need(v.runner_name===runnerName&&Array.isArray(v.labels)&&v.labels.length>0&&v.labels.every(x=>typeof x==='string')&&!v.labels.includes('self-hosted'));
 need(Array.isArray(v.steps));const step=v.steps.filter(x=>x.name===STEP);need(step.length===1&&step[0].status==='in_progress'&&step[0].conclusion===null&&step[0].completed_at===null);stamp(step[0].started_at);positive(step[0].number);
 return canonical({id:v.id,run_id:v.run_id,run_attempt:v.run_attempt,head_sha:v.head_sha,name:v.name,status:v.status,conclusion:v.conclusion,runner_id:v.runner_id,runner_name:v.runner_name,labels:v.labels,step:{name:step[0].name,number:step[0].number,started_at:step[0].started_at,status:step[0].status}});
}
async function pages(native,route,key,cap){
 const rows=[];let total=null;
 for(let page=1;page<=Math.ceil(cap/100);page++){
  const value=await native(route+page);need(value&&typeof value==='object'&&Number.isInteger(value.total_count)&&value.total_count>=0&&value.total_count<=cap&&(total===null||total===value.total_count));total=value.total_count;need(Array.isArray(value[key])&&value[key].length<=100);rows.push(...value[key]);
  if(rows.length===total)break;need(value[key].length===100&&rows.length<total);
 }
 need(rows.length===total&&new Set(rows.map(x=>positive(x.id))).size===rows.length);return rows;
}
async function jobs(native,runId,attempt){return pages(native,`${REPO}/actions/runs/${runId}/attempts/${attempt}/jobs?per_page=100&page=`,'jobs',500);}
async function artifacts(native,runId){return pages(native,`${REPO}/actions/runs/${runId}/artifacts?per_page=100&page=`,'artifacts',1000);}
async function nativeActor(c,deps){
 const a=actor(c.actor);const v=await deps.native(`${REPO}/actions/runs/${a.run_id}`);
 runShape(v,a.run_id,a.run_attempt,a.workflow_path,a.event,a.native_head_sha,a.sdk_sha,'keydb-parity-cleanup.yml');need(v.status==='in_progress'&&v.conclusion===null);
 const listed=await jobs(deps.native,a.run_id,a.run_attempt);const matches=listed.filter(x=>x.id===a.job_id);need(matches.length===1);const projection=activeJob(matches[0],a,c.runner_name);need(listed.filter(x=>x.name===a.job_name).length===1);
 need(activeJob(await deps.native(`${REPO}/actions/jobs/${a.job_id}`),a,c.runner_name)===projection);return v;
}
function executed(v,name){
 need(v&&typeof v==='object'&&Array.isArray(v.steps));positive(v.id);positive(v.run_id);positive(v.run_attempt);
 if((v.status==='queued'||(v.status==='completed'&&v.conclusion==='skipped'))&&(!v.runner_id||v.runner_id===0)&&v.steps.every(x=>!x.started_at))return false;
 const selected=v.steps.filter(x=>x.name===name);need(selected.length===1);
 if(selected[0].status==='completed'&&selected[0].conclusion==='skipped'&&!selected[0].started_at)return false;
 positive(v.runner_id);need(typeof v.runner_name==='string'&&v.runner_name.length>0&&Array.isArray(v.labels)&&!v.labels.includes('self-hosted'));stamp(selected[0].started_at);
 need((v.status==='completed'&&TERMINAL.includes(v.conclusion)&&selected[0].status==='completed'&&TERMINAL.includes(selected[0].conclusion))||(v.status==='in_progress'&&v.conclusion===null&&selected[0].status==='in_progress'));
 if(v.status==='completed')stamp(selected[0].completed_at);return true;
}
async function producerNative(locator,a,deps){
 const v=await deps.native(`${REPO}/actions/runs/${locator.run_id}/attempts/${locator.run_attempt}`);
 need(v?.id===locator.run_id&&v.run_attempt===locator.run_attempt&&v.path==='.github/workflows/keydb-modules-parity.yml'&&v.event==='workflow_dispatch'&&v.head_branch==='main');sha(v.head_sha,40);
 need(v.repository?.id===1074974924&&v.repository?.private===true&&v.head_repository?.id===1074974924);stamp(v.created_at);
 need(Array.isArray(v.referenced_workflows));const refs=v.referenced_workflows.filter(x=>typeof x.path==='string'&&/^Digital-Frontier-LDA\/just-akash\/\.github\/workflows\/runner-pool.yml@[a-f0-9]{40}$/.test(x.path));need(refs.length===1&&refs[0].path.endsWith('@'+sha(refs[0].sha,40)));
 if(a.mode==='terminal')need(v.status==='completed'&&TERMINAL.includes(v.conclusion));else need(['in_progress','completed'].includes(v.status));
 const listed=await jobs(deps.native,locator.run_id,locator.run_attempt);const found=listed.filter(x=>x.name===PRODUCER_JOB);need(found.length===1);
 const job=found[0];need(job.run_id===locator.run_id&&job.run_attempt===locator.run_attempt&&job.head_sha===v.head_sha&&job.status==='completed'&&TERMINAL.includes(job.conclusion)&&executed(job,PRODUCER_STEP));
 const direct=await deps.native(`${REPO}/actions/jobs/${job.id}`);need(jobProjection(direct)===jobProjection(job)&&executed(direct,PRODUCER_STEP));
 return {run:v,job,sdk_sha:refs[0].sha,all_jobs:listed};
}
function jobProjection(j){return canonical(Object.fromEntries(['id','run_id','run_attempt','head_sha','name','status','conclusion','runner_id','runner_name','labels','steps'].map(k=>[k,j[k]])));}
function artifactMeta(v,runId,head){
 need(v&&typeof v==='object');positive(v.id);positive(v.size_in_bytes);need(v.size_in_bytes<=1048576&&v.expired===false&&/^sha256:[0-9a-f]{64}$/.test(v.digest));stamp(v.expires_at);need(Date.parse(v.expires_at)>Date.now());
 need(v.workflow_run?.id===runId&&v.workflow_run.repository_id===1074974924&&v.workflow_run.head_repository_id===1074974924&&v.workflow_run.head_sha===head&&v.workflow_run.head_branch==='main');return v;
}
function readReceipt(dir){
 const entries=fs.readdirSync(dir,{withFileTypes:true});need(entries.length===1&&entries[0].name==='receipt.json'&&entries[0].isFile());const file=path.join(dir,'receipt.json'),fd=fs.openSync(file,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW);
 try{const a=fs.fstatSync(fd,{bigint:true});need(a.isFile()&&a.nlink===1n&&a.size>0n&&a.size<=65536n);const raw=fs.readFileSync(fd),b=fs.fstatSync(fd,{bigint:true}),p=fs.lstatSync(file,{bigint:true});for(const k of ['dev','ino','mode','uid','nlink','size','mtimeNs','ctimeNs'])need(a[k]===b[k]&&b[k]===p[k]);need(raw.length===Number(a.size));return raw;}finally{fs.closeSync(fd);}
}
async function download(meta,runId,head,deps){
 artifactMeta(meta,runId,head);const fresh=await deps.native(`${REPO}/actions/artifacts/${meta.id}`);artifactMeta(fresh,runId,head);need(['id','name','size_in_bytes','digest'].every(k=>fresh[k]===meta[k]));
 const dir=fs.mkdtempSync(path.join(deps.directory,'read-'));const options={path:dir,expectedHash:meta.digest,findBy:{repositoryOwner:'Borduas-Holdings',repositoryName:'blazing',workflowRunId:runId,token:deps.token}};
 const downloaded=await deps.artifact.downloadArtifact(meta.id,options);need(downloaded.downloadPath===dir&&downloaded.digestMismatch===false);const raw=readReceipt(dir);const value=JSON.parse(raw.toString('utf8'));need(canonical(value)===raw.toString('utf8'));return {value,raw};
}
async function readTarget(c,deps){
 await nativeActor(c,deps);const a=c.actor,loc=a.target_locator;const native=await producerNative(loc,a,deps);const all=await artifacts(deps.native,loc.run_id);const map={},chains={},inventory={};const evidence=[];
 for(const kind of ['jit','lease','lease_bind']){
  const session=new P.PhaseSession();const chain=[];let gap=false;const expectedPrefix=`keydb-parity-v1-1074974924-${loc.run_id}-a${loc.run_attempt}-r${loc.sdk_round}-${kind}-j${native.job.id}-`;const broaderPrefix=`keydb-parity-v1-1074974924-${loc.run_id}-a${loc.run_attempt}-r${loc.sdk_round}-${kind}-`;
  for(const m of all.filter(x=>typeof x.name==='string'&&x.name.startsWith(broaderPrefix)))need(m.name.startsWith(expectedPrefix)&&['no_wire','unknown','ack'].includes(m.name.slice(expectedPrefix.length)));
  for(const phase of ['NO_WIRE','UNKNOWN','ACK']){
   const op=`keydb-parity-v1-1074974924-${loc.run_id}-a${loc.run_attempt}-r${loc.sdk_round}-${kind}`;const name=`${op}-j${native.job.id}-${phase.toLowerCase()}`;const matched=all.filter(x=>x.name===name);need(matched.length<=1);if(!matched.length){need(kind==='lease_bind');gap=true;continue;}need(!gap);
   const meta=matched[0],body=await download(meta,loc.run_id,native.run.head_sha,deps);const v=P.validate(body.value);need(v.name===name&&(v.record.kind===kind||(kind==='lease'&&v.record.kind==='lease_reconcile'))&&v.record.phase===phase&&v.record.binding.producer_job_id===native.job.id&&v.record.binding.sdk_sha===native.sdk_sha);
   need(v.record.binding.run_id===loc.run_id&&v.record.binding.run_attempt===loc.run_attempt&&v.record.binding.sdk_round===loc.sdk_round&&v.record.binding.native_head_sha===native.run.head_sha);
   const reserved=session.reserve(v.record);session.accept(reserved);chain.push(v.record);evidence.push({id:meta.id,name:meta.name,digest:meta.digest,size_in_bytes:meta.size_in_bytes,record_sha256:v.sha256,kind,phase});
  }
  chains[kind]=chain;inventory[kind==='lease'?'deployment':kind==='lease_bind'?'lease':kind]=chain.map(x=>x.phase);map[kind]=chain.length===3?chain[2]:null;
 }
 const j=map.jit,d=map.lease,l=map.lease_bind,b=j.binding;need(canonical(b)===canonical(d.binding));
 need(j.details.runner_id===d.details.returned_runner_id&&d.details.jit_ack_record_sha256===hash(j));for(const leasePhase of chains.lease_bind){need(canonical(leasePhase.binding)===canonical(b)&&j.details.runner_id===leasePhase.details.returned_runner_id&&d.details.owner===leasePhase.details.owner&&d.details.dseq===leasePhase.details.dseq&&leasePhase.details.jit_ack_record_sha256===hash(j)&&leasePhase.details.deployment_ack_record_sha256===hash(d)&&d.details.public_request_sha256===leasePhase.details.public_request_sha256&&d.details.wire_request_sha256===leasePhase.details.wire_request_sha256);}
 const t=target(b,{owner:d.details.owner,dseq:d.details.dseq,returned_runner_id:j.details.runner_id,group_population:d.details.group_population,jit_ack_record_sha256:hash(j),deployment_ack_record_sha256:hash(d),lease_ack_record_sha256:l?hash(l):null});
 await nativeActor(c,deps);const renew=await producerNative(loc,a,deps);need(renew.sdk_sha===native.sdk_sha&&jobProjection(renew.job)===jobProjection(native.job));
 return {schema:'just-akash/keydb-parity-cleanup-target/v1',actor:a,producer:b,target:t,records:{jit:j,deployment:d,lease:l},phase_inventory:inventory,evidence,native:{producer_job_id:native.job.id,producer_run_id:loc.run_id,producer_attempt:loc.run_attempt,producer_head_sha:native.run.head_sha,producer_sdk_sha:native.sdk_sha,producer_created_at:native.run.created_at,producer_job_status:native.job.status,actor_native_run_id:a.run_id}};
}
async function renewBundle(c,bundle,deps){
 need(bundle&&bundle.schema==='just-akash/keydb-parity-cleanup-target/v1'&&canonical(bundle.actor)===canonical(c.actor));target(bundle.producer,bundle.target);const n=await producerNative(c.actor.target_locator,c.actor,deps);need(bundle.producer.producer_job_id===n.job.id&&bundle.producer.sdk_sha===n.sdk_sha&&bundle.producer.native_head_sha===n.run.head_sha);
 need(Array.isArray(bundle.evidence)&&bundle.evidence.length>=6&&bundle.evidence.length<=9);const all=await artifacts(deps.native,n.run.id);for(const kind of ['jit','lease','lease_bind']){const prefix=`keydb-parity-v1-1074974924-${n.run.id}-a${c.actor.target_locator.run_attempt}-r${c.actor.target_locator.sdk_round}-${kind}-`;const selected=all.filter(x=>typeof x.name==='string'&&x.name.startsWith(prefix));const expected=bundle.evidence.filter(x=>x.kind===kind);need(selected.length===expected.length&&selected.every(x=>expected.some(y=>x.id===y.id&&x.name===y.name&&x.digest===y.digest&&x.size_in_bytes===y.size_in_bytes)));}await Promise.all(bundle.evidence.map(async row=>{const meta=await deps.native(`${REPO}/actions/artifacts/${positive(row.id)}`);artifactMeta(meta,n.run.id,n.run.head_sha);need(meta.digest===row.digest&&meta.size_in_bytes===row.size_in_bytes&&meta.name===row.name);}));return n;
}
async function handlerHistory(c,bundle,currentRecords,deps){
 await nativeActor(c,deps);const n=await renewBundle(c,bundle,deps);const a=c.actor,t=bundle.target,b=bundle.producer;const handlers=[];
 const runs=[n.run];const seenEffectArtifacts=new Set(),candidateArtifacts=new Map(),artifactCache=new Map();const created=encodeURIComponent('>='+stamp(n.run.created_at));const callbackRuns=await pages(deps.native,`${REPO}/actions/workflows/keydb-parity-finished.yml/runs?event=workflow_run&created=${created}&per_page=100&page=`,'workflow_runs',200);
 for(const run of callbackRuns){need(run.path===ROOTS.terminal&&run.event==='workflow_run'&&Date.parse(stamp(run.created_at))>=Date.parse(n.run.created_at));positive(run.run_attempt);need(run.run_attempt<=10);for(let attempt=1;attempt<=run.run_attempt;attempt++)runs.push({...run,run_attempt:attempt});}
 const prior=[],current=[],missing=[];const clear={close:true,remove:true};
 for(const listedRun of runs){
  const run=listedRun.id===n.run.id?listedRun:await deps.native(`${REPO}/actions/runs/${positive(listedRun.id)}/attempts/${positive(listedRun.run_attempt)}`);
  need(['id','run_attempt','head_sha','event','path'].every(k=>run[k]===listedRun[k]));
  const list=run.id===n.run.id?n.all_jobs:await jobs(deps.native,run.id,positive(run.run_attempt));let metadata=artifactCache.get(run.id);if(!metadata){metadata=await artifacts(deps.native,run.id);artifactCache.set(run.id,metadata);}const prefix=slot(a.target_locator)+'-';for(const row of metadata.filter(x=>typeof x.name==='string'&&x.name.startsWith(prefix)))candidateArtifacts.set(row.id,row);
  const found=list.filter(x=>x.name===a.job_name);need(found.length<=1);if(!found.length){need(list.length>0||run.status==='queued');continue;}
  const job=found[0];const direct=await deps.native(`${REPO}/actions/jobs/${positive(job.id)}`);need(jobProjection(job)===jobProjection(direct));const did=executed(job,STEP);
  need(job.run_id===run.id&&job.run_attempt===run.run_attempt&&job.head_sha===run.head_sha);handlers.push({run_id:run.id,run_attempt:run.run_attempt,job_id:job.id,job_name:job.name,status:job.status,conclusion:job.conclusion,executed:did});
  if(!did)continue;
  const isCurrent=run.id===a.run_id&&run.run_attempt===a.run_attempt&&job.id===a.job_id;need(isCurrent||job.status==='completed');
  const selected=metadata.filter(x=>typeof x.name==='string'&&['close','remove'].some(kind=>x.name.startsWith(`${prefix}${kind}-actor${run.id}-a${run.run_attempt}-j${job.id}-`)));const records=[];
  for(const meta of selected){
   const held=currentRecords.find(x=>x.artifact_id===meta.id&&x.artifact_digest===meta.digest);let r;
   if(held){artifactMeta(meta,run.id,run.head_sha);r=record(held.record).record;need(hash(r)===held.body_sha256);}else{const downloaded=await download(meta,run.id,run.head_sha,deps);r=record(downloaded.value).record;}
   need(r.actor.run_id===run.id&&r.actor.run_attempt===run.run_attempt&&r.actor.job_id===job.id&&r.actor.job_name===job.name&&r.actor.native_head_sha===run.head_sha&&r.actor.workflow_path===run.path&&r.actor.event===run.event&&canonical(r.producer)===canonical(b)&&canonical(r.target)===canonical(t));
   runShape(run,r.actor.run_id,r.actor.run_attempt,r.actor.workflow_path,r.actor.event,r.actor.native_head_sha,r.actor.sdk_sha,'keydb-parity-cleanup.yml');need(record(r).name===meta.name);
   const item={record:r,artifact_id:meta.id,artifact_digest:meta.digest,body_sha256:hash(r)};records.push(item);seenEffectArtifacts.add(meta.id);
   if(held){need(canonical(r.actor)===canonical(a)&&canonical(held)===canonical(item));current.push(item);}else prior.push(item);
  }
  const currentHandler=run.id===a.run_id&&run.run_attempt===a.run_attempt&&job.id===a.job_id;
  const heldOnly=records.every(x=>currentRecords.some(h=>canonical(h)===canonical(x)));
  if(currentHandler&&!heldOnly){clear.close=false;clear.remove=false;}
  for(const kind of ['close','remove']){
   const chain=records.filter(x=>x.record.kind===kind).sort((x,y)=>x.record.phase_index-y.record.phase_index);
   if(!chain.length){if(!(currentHandler&&heldOnly))missing.push({run_id:run.id,job_id:job.id,kind});continue;}
   for(let i=0;i<chain.length;i++){const r=chain[i].record;need(r.phase_index===i&&r.parent_record_sha256===(i?hash(chain[i-1].record):null));if(i){const first={...chain[0].record.details},later={...r.details};delete first.response_received;delete later.response_received;need(canonical(first)===canonical(later));}}
  }
 }
 need([...candidateArtifacts.keys()].every(id=>seenEffectArtifacts.has(id)));need(missing.length===0);for(const row of prior)if(row.record.phase!=='NO_WIRE')clear[row.record.kind]=false;
 // An executed handler with unrecognized/missing records is never treated as clean.
 return {schema:'just-akash/keydb-parity-cleanup-history/v1',actor:a,producer:b,target_sha256:hash(t),complete:true,wire_clear:clear,handlers,prior_actor_records:prior,current_actor_records:current,missing:[],incomplete:false};
}
async function persist(r,c,bundle,currentRecords,deps){
 const v=record(r);need(canonical(r.actor)===canonical(c.actor)&&canonical(r.producer)===canonical(bundle.producer)&&canonical(r.target)===canonical(bundle.target));
 const chain=currentRecords.filter(x=>x.record.operation_id===r.operation_id);const last=chain.at(-1);need(last?r.phase_index===last.record.phase_index+1&&r.parent_record_sha256===hash(last.record):r.phase==='NO_WIRE');
 if(r.phase==='UNKNOWN')for(const kind of ['close','remove'])need(currentRecords.some(x=>x.record.kind===kind&&x.record.phase==='NO_WIRE'&&canonical(x.record.actor)===canonical(c.actor)&&x.record.target_sha256===r.target_sha256));
 let history=await handlerHistory(c,bundle,currentRecords,deps);if(r.phase==='UNKNOWN')need(history.wire_clear[r.kind]);
 const dir=fs.mkdtempSync(path.join(deps.directory,'write-')),file=path.join(dir,'receipt.json');fs.writeFileSync(file,v.raw,{flag:'wx',mode:0o600});const up=await deps.artifact.uploadArtifact(v.name,[file],dir,{retentionDays:90,compressionLevel:0});positive(up.id);positive(up.size);sha(up.digest);need(up.size<=1048576);
 const meta=await deps.native(`${REPO}/actions/artifacts/${up.id}`);artifactMeta(meta,c.actor.run_id,c.actor.native_head_sha);need(meta.name===v.name&&meta.digest==='sha256:'+up.digest&&meta.size_in_bytes===up.size);const body=await download(meta,c.actor.run_id,c.actor.native_head_sha,deps);need(body.raw.equals(v.raw));
 const accepted={record:r,artifact_id:up.id,artifact_digest:meta.digest,body_sha256:v.sha256};
 history=await handlerHistory(c,bundle,[...currentRecords,accepted],deps);if(r.phase==='UNKNOWN')need(history.wire_clear[r.kind]);await nativeActor(c,deps);
 return {schema:'just-akash/keydb-parity-cleanup-durable-ack/v1',backend:'github-actions-artifact/v4',operation_id:r.operation_id,phase:r.phase,record_sha256:v.sha256,parent_record_sha256:r.parent_record_sha256,artifact_id:up.id,artifact_digest:meta.digest,record_readback_sha256:hash(body.raw),actor:r.actor,producer:r.producer,target_sha256:r.target_sha256,history_sha256:hash(history),created:true};
}
module.exports={environmentContext,nativeActor,producerNative,readTarget,renewBundle,handlerHistory,persist,artifactMeta,readReceipt,download,pages,jobs,artifacts,executed,jobProjection,REPO,PRODUCER_JOB,PRODUCER_STEP};
