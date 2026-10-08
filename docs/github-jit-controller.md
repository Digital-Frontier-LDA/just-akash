# GitHub JIT controller primitive

For a non-reusable workflow, trusted `JitPolicy` configuration binds the selected
workflow to its fixed default branch and separately binds the approved producer
source SHA. `source_workflow_branch` defaults to `main`; `master` and `deploy-env`
are also admitted for reviewed repositories that use them. Flynn's observed
default branch is `deploy-env`; supporting that fixed name is a prerequisite,
not an admission or workload qualification for Flynn. The branch in `workflows` must exactly
match this field. A master policy cannot mint into a main-restricted group, and
a matching branch never substitutes for the exact `source_workflow_revision`.
Other branch names are held. Reusable workflows retain their SHA-only policy
and cannot select either alternate branch through this field. No existing
controller profile, selected workflow, image pin or deployment changes here.

This prerequisite for [just-akash #332](https://github.com/Digital-Frontier-LDA/just-akash/issues/332)
and [df-cicd #381](https://github.com/Digital-Frontier-LDA/df-cicd/issues/381) implements
the narrow GitHub half of a one-job delivery slot. It does not enable the existing
`runner-pool.yml`, replace its legacy credentials, or authorize an Akash create.
The external admission broker and independent lifecycle owner remain necessary.

`just_akash.github_jit.mint_jit` accepts a trusted immutable `JitPolicy`, a unique
operation/attempt/slot runner name, its labels and a controller-held installation
token. The caller must authenticate the producer, resolve the immutable workload
graph, acquire admission and persist the unique mint intent before calling it.
Never retry a mint merely because the POST timed out or its response was malformed.

Immediately before the real `generate-jitconfig` POST, the primitive reads the
requested numeric group's policy through GitHub's authenticated API. It requires
selected visibility, public repositories disabled, workflow restriction enabled,
the exact immutable workflow SHA population, and precisely the expected private
repository ID and name. It reads a terminal repository page, checks totals and
duplicate identities, then reads the group again to detect policy changes during
pagination. The POST uses that same group ID. This first profile permits one
private repository; public/fork and broader populations require separate policy.

For a direct, non-reusable main workflow, set `non_reusable_workflow=True` and
pin `source_workflow_revision` separately. The group's selected workflow is the
exact repository/path at `refs/heads/main`, matching GitHub's supported branch
restriction. The trusted caller must pass the authenticated producer workflow
revision to `mint_jit`; a different or missing revision prevents the POST.
This comparison does not authenticate caller-supplied text. Reusable profiles
continue requiring an immutable workflow SHA and reject this branch-source input.

`just_akash.api.CIConsoleAPI` is the controller-side transport for secret SDL/JIT
payloads. It fixes the Console HTTPS origin, refuses redirects, omits runtime
payload logging and suppresses response/exception details that can echo secrets.
It preserves status/timeout metadata for reconciliation; it supplies neither
admission nor retry authority. Legacy callers retain their error contract.

The owner authorizes Console accounts shared across CI, production and other
purposes. Track each CI operation's exact run, create receipt, deployment/lease,
runner and attributable costs independently. Receipt-mode `deploy` retains an
`already exists` response as unresolved: it neither sweeps older deployments nor
sends a second create. Reconcile that receipt before retrying. Broad account or
service/age sweeps are not a CI cleanup path on a shared account.

All requests use the fixed `https://api.github.com` organization origin, refuse
redirects, cap individual calls at 20 seconds and bound response size at 1 MiB.
JSON duplicate keys and non-finite numbers are refused. The outer configuration and every file value must be canonical base64, and each filename must remain within the runner root. There is no automatic
mutation retry. HTTP, transport, decoding or response-binding failure after POST
raises `JitMintUnknown`; the caller retains the prior durable slot intent and
reconciles the exact runner name before any replacement. Error messages suppress
response bodies and credentials. They do not establish that no runner was created.

`JitHandoff` contains the exact returned runner ID/name and a secret encoded
configuration; its repr excludes the credential. This is an in-memory delivery
object, not a serializable journal record. It must never enter workflow outputs,
logs, artifacts, caches or the durable lifecycle journal. Deliver it once to one
runtime slot in one `count: 1` service. The lease receives no App private key,
installation token, runner-admin PAT or Console key. New attempts need a new
bootstrap after exact prior registration cleanup and unknown-create reconciliation.

`verify_group_policy` is a read-only readiness probe. Its return value is not an
admission receipt. `mint_jit` repeats the complete observation rather than trusting
an earlier probe. GitHub offers no atomic group-policy-read/mint transaction:
operational policy writers must remain controlled. The response also does not
prove a runner is online or idle. Before routing, the independent controller must
observe the exact declared ID/name/group/label population, all online and not busy.
Partial capacity remains a failed pool. Fresh scoped cleanup authentication,
unused-registration removal, durable slot reconciliation and Akash closure are
separate unfinished integration requirements.

## Live read-only readiness evidence

On 2026-10-02 an empty `dfci-grafana-canary` group (ID **5**) was created with:

- Repository **1283916135**, `Digital-Frontier-LDA/df-grafana`, private.
- Selected visibility, public repositories disabled and workflow restriction enabled.
- Only `Digital-Frontier-LDA/df-grafana/.github/workflows/ci-observability.yml@a7265393670a7d9c8042a2e997a3624a9e5ac85d`.
- Zero runners; existing groups and consumer routing were not modified.

The [recorded App probe](evidence/grafana-jit-policy-live.json) passed this module's
actual read-only policy path using Sentinel installation **144391670**, requesting
only organization runner write permission and controller repository **1285006023**.
The temporary installation token was revoked. No runner or lease was created.
The private key and token stayed in memory. This historical check is neither
current admission nor a live GitHub/Akash workload qualification. The later
consumer revision must be adopted explicitly in the group policy before its mint.

## Verification and next integration

The effect tests verify that each incorrect policy or incomplete population
removes the POST effect, the exact checked group is bound at the mutation, the
readonly result cannot bypass a fresh observation, and ambiguous response/timeout
does not produce a retry. Transport tests check fixed-origin requests, refused
redirects, strict response bounds and redacted errors. These tests exercise the
primitive's real request boundary; they are not evidence that the existing pool
workflow has adopted it.

Connect the released primitive to the authenticated durable broker and actual
pool call site under #332 and [Guardian #1061](https://github.com/Digital-Frontier-LDA/guardian-cli-claude-code/issues/1061).
Keep defaults and migration flags off until identity, admission, per-slot delivery,
independent cleanup and image/provider qualification are all observed. Grafana is
the first net-savings pilot. Its [blazing #1518](https://github.com/Borduas-Holdings/blazing/issues/1518)
and [Blazing-Back #2375](https://github.com/Borduas-Holdings/Blazing-Back/issues/2375)
references illustrate the provisioning/consumer/cleanup DAG; their reusable
credentials and existing pool behavior are not this JIT isolation contract.
