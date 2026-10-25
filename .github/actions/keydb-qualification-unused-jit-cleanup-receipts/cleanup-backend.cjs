'use strict';
const fs=require('node:fs'),path=require('node:path');
const S=require('./cleanup-schema.cjs');const {P,need,object,positive,sha,canonical,hash,actor,target,slot,record,ROOTS,STEP}=S;
const REPO='repos/Borduas-Holdings/blazing';
const PRODUCER_JOB='Provision one owned KeyDB parity qualification runner / Provision KeyDB qualification through acknowledged JIT';
const PRODUCER_STEP='Run the acknowledged KeyDB qualification controller';
const TERMINAL=['success','failure','cancelled','timed_out'];
function stamp(v){need(typeof v==='string'&&Number.isFinite(Date.parse(v)));return v;}
function environmentContext(env,inputs){
 need(env.GITHUB_ACTIONS==='true'&&env.GITHUB_SERVER_URL==='https://github.com'&&env.RUNNER_ENVIRONMENT==='github-hosted'&&env.RUNNER_OS==='Linux'&&env.GITHUB_JOB==='cleanup');
 const number=s=>{need(typeof s==='string'&&/^[1-9][0-9]*$/.test(s));return positive(Number(s));};
 const mode=inputs.mode;need(mode==='recover'&&Object.hasOwn(ROOTS,mode)&&env.KEYDB_QUALIFICATION_CLEANUP_MODE===mode);need(env.GITHUB_WORKFLOW_REF===`Borduas-Holdings/blazing/${ROOTS[mode]}@refs/heads/codex/keydb-parity-owned-d8ab-20261024`);
 need(env.GITHUB_REPOSITORY==='Borduas-Holdings/blazing'&&env.GITHUB_REPOSITORY_ID==='1074974924'&&env.GITHUB_REF==='refs/heads/codex/keydb-parity-owned-d8ab-20261024');
 const locator={run_id:number(inputs.target_run_id),run_attempt:number(inputs.target_run_attempt),sdk_round:number(inputs.target_sdk_round)};
 const a=actor({repository:env.GITHUB_REPOSITORY,repository_id:1074974924,mode,event:env.GITHUB_EVENT_NAME,ref:env.GITHUB_REF,workflow_path:ROOTS[mode],workflow_sha:sha(env.GITHUB_WORKFLOW_SHA,40),native_head_sha:sha(inputs.native_head_sha,40),sdk_sha:sha(inputs.sdk_sha,40),run_id:number(env.GITHUB_RUN_ID),run_attempt:number(env.GITHUB_RUN_ATTEMPT),job_id:number(inputs.actor_job_id),job_name:S.PREFIX[mode]+slot(locator),step_name:STEP,target_locator:locator});
 need(env.GITHUB_SHA===a.workflow_sha&&typeof env.RUNNER_NAME==='string'&&/^[\x20-\x7e]{1,128}$/.test(env.RUNNER_NAME));return {actor:a,runner_name:env.RUNNER_NAME};
}
function runShape(v,runId,attempt,workflow,event,head,sdk,callee){
 need(v&&typeof v==='object'&&v.id===runId&&v.run_attempt===attempt&&v.path===workflow&&v.event===event&&v.head_sha===head&&v.head_branch==='codex/keydb-parity-owned-d8ab-20261024');positive(v.id);positive(v.run_attempt);need(v.workflow_id===308813680);
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
 runShape(v,a.run_id,a.run_attempt,a.workflow_path,a.event,a.native_head_sha,a.sdk_sha,'keydb-qualification-unused-jit-cleanup.yml');need(v.status==='in_progress'&&v.conclusion===null);
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
async function producerNative(locator,a,deps,returnedId=null){
 need(a.mode==='recover');const v=await deps.native(`${REPO}/actions/runs/${locator.run_id}/attempts/${locator.run_attempt}`);
 need(v?.id===locator.run_id&&v.run_attempt===locator.run_attempt&&v.path==='.github/workflows/keydb-modules-parity.yml'&&v.workflow_id===308813680&&v.event==='workflow_dispatch'&&v.head_branch==='codex/keydb-parity-owned-d8ab-20261024');sha(v.head_sha,40);
 need(v.repository?.id===1074974924&&v.repository?.private===true&&v.head_repository?.id===1074974924);stamp(v.created_at);
 need(Array.isArray(v.referenced_workflows));const refs=v.referenced_workflows.filter(x=>typeof x.path==='string'&&/^Digital-Frontier-LDA\/just-akash\/\.github\/workflows\/runner-pool.yml@[a-f0-9]{40}$/.test(x.path));need(refs.length===1&&refs[0].path.endsWith('@'+sha(refs[0].sha,40)));
 if(a.mode==='recover')need(v.status==='completed'&&TERMINAL.includes(v.conclusion));else need(['in_progress','completed'].includes(v.status));
 const listed=await jobs(deps.native,locator.run_id,locator.run_attempt);const found=listed.filter(x=>x.name===PRODUCER_JOB);need(found.length===1);
 const job=found[0];need(job.run_id===locator.run_id&&job.run_attempt===locator.run_attempt&&job.head_sha===v.head_sha&&job.status==='completed'&&TERMINAL.includes(job.conclusion)&&executed(job,PRODUCER_STEP));
 need(refs[0].sha===a.sdk_sha&&v.conclusion==='failure'&&job.conclusion==='failure');const selectedStep=job.steps.filter(s=>s.name===PRODUCER_STEP);need(selectedStep.length===1&&selectedStep[0].conclusion==='failure');const direct=await deps.native(`${REPO}/actions/jobs/${job.id}`);need(jobProjection(direct)===jobProjection(job)&&executed(direct,PRODUCER_STEP));
 if(a.mode==='recover'){
  const latest=await deps.native(`${REPO}/actions/runs/${locator.run_id}`);
  need(latest.run_attempt===locator.run_attempt&&latest.head_sha===v.head_sha&&latest.head_branch==='codex/keydb-parity-owned-d8ab-20261024'&&latest.conclusion==='failure');need(latest.id===locator.run_id&&latest.path===ROOTS.normal&&latest.event==='workflow_dispatch'&&latest.status==='completed'&&TERMINAL.includes(latest.conclusion));
  need(latest.repository?.id===1074974924&&latest.head_repository?.id===1074974924);
  const name='Run original KeyDB qualification on owned Podman runner '+slot(locator),consumer=listed.filter(x=>x.name===name);need(consumer.length<=1);
  if(!consumer.length){
   need(latest.run_attempt===locator.run_attempt&&latest.head_sha===v.head_sha&&latest.head_branch==='codex/keydb-parity-owned-d8ab-20261024');
   if(returnedId!==null){positive(returnedId);need(listed.every(x=>x.runner_id!==returnedId));}
  }else{
   const work=consumer[0];positive(work.id);need(work.run_id===locator.run_id&&work.run_attempt===locator.run_attempt&&work.head_sha===v.head_sha&&work.status==='completed'&&[...TERMINAL,'skipped'].includes(work.conclusion));
   const direct=await deps.native(`${REPO}/actions/jobs/${work.id}`);need(jobProjection(direct)===jobProjection(work));
   if(returnedId!==null&&work.runner_id!==null&&work.runner_id!==0)need(work.runner_id===returnedId);
  }
 }
 if(returnedId!==null){positive(returnedId);need(listed.every(x=>x.runner_id!==returnedId));}return {run:v,job,sdk_sha:refs[0].sha,all_jobs:listed};
}
function jobProjection(j){return canonical(Object.fromEntries(['id','run_id','run_attempt','head_sha','name','status','conclusion','runner_id','runner_name','labels','steps'].map(k=>[k,j[k]])));}
function artifactMeta(v,runId,head){
 need(v&&typeof v==='object');positive(v.id);positive(v.size_in_bytes);need(v.size_in_bytes<=1048576&&v.expired===false&&/^sha256:[0-9a-f]{64}$/.test(v.digest));stamp(v.expires_at);need(Date.parse(v.expires_at)>Date.now());
 need(v.workflow_run?.id===runId&&v.workflow_run.repository_id===1074974924&&v.workflow_run.head_repository_id===1074974924&&v.workflow_run.head_sha===head&&v.workflow_run.head_branch==='codex/keydb-parity-owned-d8ab-20261024');return v;
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
 await nativeActor(c,deps);const a=c.actor,loc=a.target_locator,n=await producerNative(loc,a,deps),all=await artifacts(deps.native,loc.run_id),evidence=[],chains={};
 for(const [kind,phases]of [['jit',['NO_WIRE','UNKNOWN','ACK']],['lease',['FENCE','NO_CREATE']]]){
  const prefix=`keydb-qualification-v1-1074974924-${loc.run_id}-a${loc.run_attempt}-r${loc.sdk_round}-${kind}-j${n.job.id}-`,broad=`keydb-qualification-v1-1074974924-${loc.run_id}-a${loc.run_attempt}-r${loc.sdk_round}-${kind}-`;
  const selected=all.filter(x=>typeof x.name==='string'&&x.name.startsWith(broad));need(selected.length===phases.length&&selected.every(x=>x.name.startsWith(prefix)&&phases.some(p=>x.name===prefix+p.toLowerCase())));const chain=[];
  for(const phase of phases){const found=selected.filter(x=>x.name===prefix+phase.toLowerCase());need(found.length===1);const m=found[0],body=await download(m,loc.run_id,n.run.head_sha,deps),v=P.validate(body.value);need(v.name===m.name&&v.record.binding.run_id===loc.run_id&&v.record.binding.run_attempt===loc.run_attempt&&v.record.binding.sdk_round===loc.sdk_round&&v.record.binding.producer_job_id===n.job.id&&v.record.binding.sdk_sha===n.sdk_sha&&v.record.binding.native_head_sha===n.run.head_sha);chain.push(v.record);evidence.push({id:m.id,name:m.name,digest:m.digest,size_in_bytes:m.size_in_bytes,record_sha256:v.sha256,kind,phase});}
  chains[kind]=chain;
 }
 const forbidden=`keydb-qualification-v1-1074974924-${loc.run_id}-a${loc.run_attempt}-r${loc.sdk_round}-lease_bind-`;need(all.every(x=>typeof x.name==='string'&&!x.name.startsWith(forbidden)));
 const [j0,j1,j]=chains.jit,[f,terminal]=chains.lease,b=j.binding;const session=new P.PhaseSession();for(const r of [j0,j1,j,f]){const v=session.reserve(r);session.accept(v);}need(canonical(f.binding)===canonical(b)&&canonical(terminal.binding)===canonical(b)&&terminal.schema===P.TERMINAL&&terminal.parent_record_sha256===hash(f)&&canonical({...f.details,child_exit_code:66,child_signal:null,create_frames_reserved:0})===canonical(terminal.details));need(j.details.runner_id===f.details.returned_runner_id&&f.details.jit_ack_record_sha256===hash(j));
 const t=target(b,{schema:'just-akash/keydb-qualification-unused-jit-target/v1',returned_runner_id:j.details.runner_id,runner_group_id:b.runner_group_id,jit_ack_record_sha256:hash(j),no_create_record_sha256:hash(terminal)});
 await nativeActor(c,deps);const fresh=await producerNative(loc,a,deps,t.returned_runner_id);need(jobProjection(fresh.job)===jobProjection(n.job)&&fresh.sdk_sha===n.sdk_sha);
 return {schema:'just-akash/keydb-qualification-unused-jit-bundle/v1',actor:a,producer:b,target:t,records:{jit:j,fence_intent:f,no_create:terminal},phase_inventory:{jit:['NO_WIRE','UNKNOWN','ACK'],lease:['FENCE','NO_CREATE'],lease_bind:[]},evidence,native:{producer_job_id:n.job.id,producer_run_id:loc.run_id,producer_attempt:loc.run_attempt,producer_head_sha:n.run.head_sha,producer_sdk_sha:n.sdk_sha,producer_created_at:n.run.created_at,producer_job_status:n.job.status,actor_native_run_id:a.run_id}};
}

async function renewBundle(c,bundle,deps){
 need(bundle&&bundle.schema==='just-akash/keydb-qualification-unused-jit-bundle/v1'&&canonical(bundle.actor)===canonical(c.actor));target(bundle.producer,bundle.target);const n=await producerNative(c.actor.target_locator,c.actor,deps,bundle.target.returned_runner_id);need(bundle.producer.producer_job_id===n.job.id&&bundle.producer.sdk_sha===n.sdk_sha&&bundle.producer.native_head_sha===n.run.head_sha);
 need(Array.isArray(bundle.evidence)&&bundle.evidence.length===5&&bundle.evidence.length<=9);const all=await artifacts(deps.native,n.run.id);for(const kind of ['jit','lease','lease_bind']){const prefix=`keydb-qualification-v1-1074974924-${n.run.id}-a${c.actor.target_locator.run_attempt}-r${c.actor.target_locator.sdk_round}-${kind}-`;const selected=all.filter(x=>typeof x.name==='string'&&x.name.startsWith(prefix));const expected=bundle.evidence.filter(x=>x.kind===kind);need(selected.length===expected.length&&selected.every(x=>expected.some(y=>x.id===y.id&&x.name===y.name&&x.digest===y.digest&&x.size_in_bytes===y.size_in_bytes)));}await Promise.all(bundle.evidence.map(async row=>{const meta=await deps.native(`${REPO}/actions/artifacts/${positive(row.id)}`);artifactMeta(meta,n.run.id,n.run.head_sha);need(meta.digest===row.digest&&meta.size_in_bytes===row.size_in_bytes&&meta.name===row.name);}));return n;
}
async function handlerHistory(c,bundle,currentRecords,deps){
 await nativeActor(c,deps);const n=await renewBundle(c,bundle,deps);const a=c.actor,t=bundle.target,b=bundle.producer;const handlers=[];
 const runs=[n.run],seenRuns=new Set([`${n.run.id}:${n.run.run_attempt}`]);const seenEffectArtifacts=new Set(),candidateArtifacts=new Map(),artifactCache=new Map();const created=encodeURIComponent('>='+stamp(n.run.created_at));const callbackRuns=await pages(deps.native,`${REPO}/actions/workflows/keydb-modules-parity.yml/runs?event=workflow_dispatch&created=${created}&per_page=100&page=`,'workflow_runs',200);
 for(const run of callbackRuns){need(run.path===ROOTS.recover&&run.event==='workflow_dispatch'&&Date.parse(stamp(run.created_at))>=Date.parse(n.run.created_at));positive(run.run_attempt);need(run.run_attempt<=10);for(let attempt=1;attempt<=run.run_attempt;attempt++){const key=`${run.id}:${attempt}`;if(!seenRuns.has(key)){runs.push({...run,run_attempt:attempt});seenRuns.add(key);}}}
 const prior=[],current=[],missing=[];const clear={remove:true};
 for(const listedRun of runs){
  const run=listedRun.id===n.run.id?listedRun:await deps.native(`${REPO}/actions/runs/${positive(listedRun.id)}/attempts/${positive(listedRun.run_attempt)}`);
  need(['id','run_attempt','head_sha','event','path'].every(k=>run[k]===listedRun[k]));
  const list=run.id===n.run.id?n.all_jobs:await jobs(deps.native,run.id,positive(run.run_attempt));let metadata=artifactCache.get(run.id);if(!metadata){metadata=await artifacts(deps.native,run.id);artifactCache.set(run.id,metadata);}const prefix=slot(a.target_locator)+'-';for(const row of metadata.filter(x=>typeof x.name==='string'&&x.name.startsWith(prefix)))candidateArtifacts.set(row.id,row);
  const names=[...Object.values(S.PREFIX),'Recover owned KeyDB parity qualification / Close KeyDB qualification known identities '].map(prefix=>prefix+slot(a.target_locator));const found=list.filter(x=>names.includes(x.name));need(found.length<=names.length&&new Set(found.map(x=>x.name)).size===found.length);if(!found.length){need(list.length>0||run.status==='queued');continue;}
  for(const job of found){const direct=await deps.native(`${REPO}/actions/jobs/${positive(job.id)}`);need(jobProjection(job)===jobProjection(direct));const did=executed(job,STEP);
  need(job.run_id===run.id&&job.run_attempt===run.run_attempt&&job.head_sha===run.head_sha);handlers.push({run_id:run.id,run_attempt:run.run_attempt,job_id:job.id,job_name:job.name,status:job.status,conclusion:job.conclusion,executed:did});
  if(!did)continue;
  const isCurrent=run.id===a.run_id&&run.run_attempt===a.run_attempt&&job.id===a.job_id;need(isCurrent||job.status==='completed');
  const selected=metadata.filter(x=>typeof x.name==='string'&&['remove'].some(kind=>x.name.startsWith(`${prefix}${kind}-actor${run.id}-a${run.run_attempt}-j${job.id}-`)));const records=[];
  for(const meta of selected){
   const held=currentRecords.find(x=>x.artifact_id===meta.id&&x.artifact_digest===meta.digest);let r;
   if(held){artifactMeta(meta,run.id,run.head_sha);r=record(held.record).record;need(hash(r)===held.body_sha256);}else{const downloaded=await download(meta,run.id,run.head_sha,deps);r=record(downloaded.value).record;}
   need(r.actor.run_id===run.id&&r.actor.run_attempt===run.run_attempt&&r.actor.job_id===job.id&&r.actor.job_name===job.name&&r.actor.native_head_sha===run.head_sha&&r.actor.workflow_path===run.path&&r.actor.event===run.event&&canonical(r.producer)===canonical(b)&&canonical(r.target)===canonical(t));
   runShape(run,r.actor.run_id,r.actor.run_attempt,r.actor.workflow_path,r.actor.event,r.actor.native_head_sha,r.actor.sdk_sha,'keydb-qualification-unused-jit-cleanup.yml');need(record(r).name===meta.name);
   const item={record:r,artifact_id:meta.id,artifact_digest:meta.digest,body_sha256:hash(r)};records.push(item);seenEffectArtifacts.add(meta.id);
   if(held){need(canonical(r.actor)===canonical(a)&&canonical(held)===canonical(item));current.push(item);}else prior.push(item);
  }
  const currentHandler=run.id===a.run_id&&run.run_attempt===a.run_attempt&&job.id===a.job_id;
  const heldOnly=records.every(x=>currentRecords.some(h=>canonical(h)===canonical(x)));
  if(currentHandler&&!heldOnly){clear.remove=false;}
  for(const kind of ['remove']){
   const chain=records.filter(x=>x.record.kind===kind).sort((x,y)=>x.record.phase_index-y.record.phase_index);
   if(!chain.length){if(!(currentHandler&&heldOnly))missing.push({run_id:run.id,job_id:job.id,kind});continue;}
   for(let i=0;i<chain.length;i++){const r=chain[i].record;need(r.phase_index===i&&r.parent_record_sha256===(i?hash(chain[i-1].record):null));if(i){const first={...chain[0].record.details},later={...r.details};delete first.response_received;delete later.response_received;need(canonical(first)===canonical(later));}}
  }
  }
 }
 need([...candidateArtifacts.keys()].every(id=>seenEffectArtifacts.has(id)));need(missing.length===0);for(const row of prior)if(row.record.phase!=='NO_WIRE')clear[row.record.kind]=false;
 // An executed handler with unrecognized/missing records is never treated as clean.
 return {schema:'just-akash/keydb-qualification-cleanup-history/v1',actor:a,producer:b,target_sha256:hash(t),complete:true,wire_clear:clear,handlers,prior_actor_records:prior,current_actor_records:current,missing:[],incomplete:false};
}
async function persist(r,c,bundle,currentRecords,deps){
 const v=record(r);need(canonical(r.actor)===canonical(c.actor)&&canonical(r.producer)===canonical(bundle.producer)&&canonical(r.target)===canonical(bundle.target));
 const chain=currentRecords.filter(x=>x.record.operation_id===r.operation_id);const last=chain.at(-1);need(last?r.phase_index===last.record.phase_index+1&&r.parent_record_sha256===hash(last.record):r.phase==='NO_WIRE');
 if(r.phase==='UNKNOWN')for(const kind of ['remove'])need(currentRecords.some(x=>x.record.kind===kind&&x.record.phase==='NO_WIRE'&&canonical(x.record.actor)===canonical(c.actor)&&x.record.target_sha256===r.target_sha256));
 let history=await handlerHistory(c,bundle,currentRecords,deps);if(r.phase==='UNKNOWN')need(history.wire_clear[r.kind]);
 const dir=fs.mkdtempSync(path.join(deps.directory,'write-')),file=path.join(dir,'receipt.json');fs.writeFileSync(file,v.raw,{flag:'wx',mode:0o600});const up=await deps.artifact.uploadArtifact(v.name,[file],dir,{retentionDays:90,compressionLevel:0});positive(up.id);positive(up.size);sha(up.digest);need(up.size<=1048576);
 const meta=await deps.native(`${REPO}/actions/artifacts/${up.id}`);artifactMeta(meta,c.actor.run_id,c.actor.native_head_sha);need(meta.name===v.name&&meta.digest==='sha256:'+up.digest&&meta.size_in_bytes===up.size);const body=await download(meta,c.actor.run_id,c.actor.native_head_sha,deps);need(body.raw.equals(v.raw));
 const accepted={record:r,artifact_id:up.id,artifact_digest:meta.digest,body_sha256:v.sha256};
 history=await handlerHistory(c,bundle,[...currentRecords,accepted],deps);if(r.phase==='UNKNOWN')need(history.wire_clear[r.kind]);await nativeActor(c,deps);
 return {schema:'just-akash/keydb-qualification-cleanup-durable-ack/v1',backend:'github-actions-artifact/v4',operation_id:r.operation_id,phase:r.phase,record_sha256:v.sha256,parent_record_sha256:r.parent_record_sha256,artifact_id:up.id,artifact_digest:meta.digest,record_readback_sha256:hash(body.raw),actor:r.actor,producer:r.producer,target_sha256:r.target_sha256,history_sha256:hash(history),created:true};
}
module.exports={environmentContext,nativeActor,producerNative,readTarget,renewBundle,handlerHistory,persist,artifactMeta,readReceipt,download,pages,jobs,artifacts,executed,jobProjection,REPO,PRODUCER_JOB,PRODUCER_STEP};
