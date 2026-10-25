'use strict';
const fs=require('node:fs');
const path=require('node:path');
const {need,positive,sha,canonical,hash,validate,producer}=require('./schema.cjs');
const STEP='Run the acknowledged KeyDB qualification controller';
const JOB='Provision one owned KeyDB parity qualification runner / Provision KeyDB qualification through acknowledged JIT';
function environmentContext(env, sdkSha, jobId){
 need(env.GITHUB_ACTIONS==='true'&&env.GITHUB_SERVER_URL==='https://github.com'&&env.RUNNER_ENVIRONMENT==='github-hosted'&&env.RUNNER_OS==='Linux');
 need(env.GITHUB_REPOSITORY==='Borduas-Holdings/blazing'&&env.GITHUB_REPOSITORY_ID==='1074974924');
 need(env.GITHUB_EVENT_NAME==='workflow_dispatch'&&env.GITHUB_REF==='refs/heads/codex/keydb-parity-owned-d8ab-20261024');
 need(env.GITHUB_WORKFLOW_REF==='Borduas-Holdings/blazing/.github/workflows/keydb-modules-parity.yml@refs/heads/codex/keydb-parity-owned-d8ab-20261024');
 const parse=(s)=>{need(typeof s==='string'&&/^[1-9][0-9]*$/.test(s));return positive(Number(s));};
 const context={repository:env.GITHUB_REPOSITORY,repository_id:1074974924,run_id:parse(env.GITHUB_RUN_ID),run_attempt:parse(env.GITHUB_RUN_ATTEMPT),workflow_sha:sha(env.GITHUB_WORKFLOW_SHA,40),native_head_sha:sha(env.GITHUB_SHA,40),sdk_sha:sha(sdkSha,40),producer_job_id:parse(jobId),runner_name:env.RUNNER_NAME};
 need(context.workflow_sha===context.native_head_sha&&typeof context.runner_name==='string'&&/^[\x20-\x7e]{1,128}$/.test(context.runner_name));
 return context;
}
function bound(record,c){for(const k of ['repository','repository_id','run_id','run_attempt','workflow_sha','native_head_sha','sdk_sha','producer_job_id'])need(record.binding[k]===c[k]);}
function nativeRun(value,c){
 need(value!==null&&typeof value==='object'&&value.id===c.run_id&&value.run_attempt===c.run_attempt&&value.head_sha===c.native_head_sha&&value.event==='workflow_dispatch'&&value.head_branch==='codex/keydb-parity-owned-d8ab-20261024'&&value.path==='.github/workflows/keydb-modules-parity.yml'&&value.status==='in_progress'&&value.conclusion===null);
 positive(value.id);positive(value.run_attempt);need(value.workflow_id===308813680);need(value.repository?.id===c.repository_id&&value.repository?.full_name===c.repository&&value.repository?.private===true);
 need(Array.isArray(value.referenced_workflows)&&value.referenced_workflows.filter(r=>r.path===`Digital-Frontier-LDA/just-akash/.github/workflows/runner-pool.yml@${c.sdk_sha}`&&r.sha===c.sdk_sha).length===1);
}
function nativeJob(value,c){
 need(value!==null&&typeof value==='object'&&value.id===c.producer_job_id&&value.run_id===c.run_id&&value.run_attempt===c.run_attempt&&value.head_sha===c.native_head_sha&&value.name===JOB&&value.status==='in_progress'&&value.conclusion===null);
 positive(value.id);positive(value.run_id);positive(value.run_attempt);positive(value.runner_id);need(value.runner_name===c.runner_name&&Array.isArray(value.labels)&&value.labels.every(x=>typeof x==='string')&&!value.labels.includes('self-hosted'));
 need(Array.isArray(value.steps));const steps=value.steps.filter(s=>s.name===STEP);need(steps.length===1&&steps[0].status==='in_progress'&&steps[0].conclusion===null&&typeof steps[0].started_at==='string'&&Number.isFinite(Date.parse(steps[0].started_at))&&steps[0].completed_at===null);
 return canonical({id:value.id,run_id:value.run_id,run_attempt:value.run_attempt,head_sha:value.head_sha,name:value.name,status:value.status,conclusion:value.conclusion,runner_id:value.runner_id,runner_name:value.runner_name,labels:value.labels,step:{name:steps[0].name,number:positive(steps[0].number),started_at:steps[0].started_at,status:steps[0].status,conclusion:steps[0].conclusion}});
}
function nativeArtifact(value,v,up,c){
 need(value!==null&&typeof value==='object'&&value.id===up.id&&value.name===v.name&&value.expired===false&&value.size_in_bytes===up.size&&value.digest==='sha256:'+up.digest);
 positive(value.id);positive(value.size_in_bytes);need(value.size_in_bytes<=1024*1024);
 need(typeof value.expires_at==='string'&&Number.isFinite(Date.parse(value.expires_at))&&Date.parse(value.expires_at)-Date.now()>=7*86400000);
 const run=value.workflow_run;need(run!==null&&typeof run==='object'&&run.id===c.run_id&&run.repository_id===c.repository_id&&run.head_repository_id===c.repository_id&&run.head_sha===c.native_head_sha&&run.head_branch==='codex/keydb-parity-owned-d8ab-20261024');
}
function readReceipt(directory){
 const entries=fs.readdirSync(directory,{withFileTypes:true});need(entries.length===1&&entries[0].name==='receipt.json'&&entries[0].isFile());
 const file=path.join(directory,'receipt.json');const fd=fs.openSync(file,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW);
 try{
  const a=fs.fstatSync(fd,{bigint:true});need(a.isFile()&&a.nlink===1n&&a.size>0n&&a.size<=65536n);
  const raw=fs.readFileSync(fd);const b=fs.fstatSync(fd,{bigint:true}),p=fs.lstatSync(file,{bigint:true});
  for(const k of ['dev','ino','mode','uid','nlink','size','mtimeNs','ctimeNs'])need(a[k]===b[k]&&b[k]===p[k]);
  need(raw.length===Number(a.size));return raw;
 }finally{fs.closeSync(fd);}
}
async function persistUnchecked(record,c,deps){
 const v=validate(record);bound(record,c);
 const prefix='repos/'+c.repository;
 nativeRun(await deps.native(`${prefix}/actions/runs/${c.run_id}`),c);
 const initial=nativeJob(await deps.native(`${prefix}/actions/jobs/${c.producer_job_id}`),c);
 const directory=deps.directory;
 const receipt=path.join(directory,'receipt.json');fs.writeFileSync(receipt,v.raw,{flag:'wx',mode:0o600});
 const up=await deps.artifact.uploadArtifact(v.name,[receipt],directory,{retentionDays:90,compressionLevel:0});
 positive(up.id);positive(up.size);sha(up.digest);need(up.size<=1024*1024);
 nativeArtifact(await deps.native(`${prefix}/actions/artifacts/${up.id}`),v,up,c);
 const target=path.join(directory,'readback');fs.mkdirSync(target,{mode:0o700});
 const downloaded=await deps.artifact.downloadArtifact(up.id,{path:target,expectedHash:'sha256:'+up.digest});
 need(downloaded.downloadPath===target&&downloaded.digestMismatch===false);
 const returned=readReceipt(target);need(hash(returned)===v.sha256&&returned.equals(v.raw));
 nativeRun(await deps.native(`${prefix}/actions/runs/${c.run_id}`),c);
 need(nativeJob(await deps.native(`${prefix}/actions/jobs/${c.producer_job_id}`),c)===initial);
 return {schema:'just-akash/keydb-qualification-durable-ack/v1',backend:'github-actions-artifact/v4',operation_id:record.operation_id,phase:record.phase,record_sha256:v.sha256,parent_record_sha256:record.parent_record_sha256,artifact_id:up.id,artifact_digest:'sha256:'+up.digest,record_readback_sha256:hash(returned),producer:producer(record.binding),exclusive:true,created:true};
}
async function persistReceipt(record,c,deps){
 let accepted;try{accepted=await persistUnchecked(record,c,deps);}catch{}
 if(!accepted)throw new Error('DURABLE_RECEIPT_HOLD');return accepted;
}
module.exports={environmentContext,nativeRun,nativeJob,nativeArtifact,persistReceipt,readReceipt,JOB,STEP};
