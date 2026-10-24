# Hosted Actor + Producer cleanup evidence

This separate action runs only `python -I -m just_akash.scanner_v2_cleanup` from the
reviewed SDK installation. It does not contain that effect controller. The engine
component must independently admit its manager, signer, current native actor,
complete owner/DSEQ/group population, native returned runner identity and physical
final states before effects. No Node command accepts an effect payload or manager
credential: only READ_TARGET, HISTORY and PERSIST public messages cross FD3/FD4.

The actual current cleanup Actor is separate from historical Producer. Normal
actor root is private BB Secret Scanning/main workflow_dispatch; terminal actor
root is private BB native-scanner-v2-cleanup/main workflow_run. Both execute the
exact SDK cleanup callee and actual hosted cleanup job/step. Native current
run/latest attempt, private repository, source/head/callee and listed+direct job
identity are checked before every backend operation and renewed. Callback native
head is independently resolved; GITHUB environment is never rewritten to pretend
the producer or equate source SHA with native head. Actual protected-main actor
source is immutable for that run; no current-main-equals-old-head requirement is
invented at wire time. The normal actor run/attempt must equal its target; terminal
actor run must differ. Source/module and workflow installation remain review gates.

READ_TARGET accepts only the exact run/attempt/round locator. It resolves native
producer attempt, SDK source reference, completed non-skipped actual producer job
and step, complete artifact inventory and typed canonical bodies. Immutable JIT
and deployment chains each require NO_WIRE→UNKNOWN→ACK with exact parent hashes.
Both legacy lease and frozen22be lease_reconcile deployment ACKs are validated by
the frozen22be codec. The extra baseline/version/payload fields remain in the body
and its SHA; the operation uses the same lease slot. The adapter only exports its
unchanged binding validator. No original15 backend or1e9 module is changed.

A known DSEQ does not require a lease-binding ACK: complete contiguous lease_bind
inventory may end at absent/NO_WIRE/UNKNOWN/ACK. Bundle phase_inventory and evidence
preserve that state; nullable lease ACK fields do not mean no lease exists. Gaps,
duplicates, unexpected phase names, foreign producer-job artifacts, missing
JIT/deployment ACK or a changed inventory at renewal HOLD. The engine must still
close the exact acknowledged owned deployment under fresh independent authority
and prove full deployment/lease/escrow closure before runner removal. UNKNOWN
create without deployment ACK/DSEQ and unused-JIT-only cleanup are not supported
by this component. A late inventory/witness change holds rather than silently
rebinding an in-flight cleanup.

Each artifact is bound to native ID, same repository/run/head, immutable name,
compressed size, digest and unexpired metadata. Official @actions/artifact2.3.2
public cross-run download uses findBy and expectedHash, then requires
`digestMismatch === false`, exactly one regular receipt.json, canonical exact
body and SHA. Upload/readback similarly requires a native positive ID, size/ZIP
digest and exact body. Dependencies/lock/integrities match the reviewed original
client family. Client transport may retry the SAME immutable artifact transfer
according to its pinned library behavior; this never authorizes or repeats close,
remove, provider, create or JIT requests. Raw library output/warnings/errors are
silenced before import. No signed URL or raw server reason is returned.

HISTORY enumerates complete producer-attempt jobs and every callback run created
since that producer was created, then EVERY exact callback attempt1..latest. The
creation bound is causal; there is no age exemption or six-hour cutoff. All
matching target-specific native jobs and all target-prefixed effect artifacts must
join exact actor run/attempt/job/source/SDK context and complete phase chains.
Missing an executed handler's intent, orphan artifacts, another executing handler,
unknown source or truncation HOLD. Positive native queued/skipped nonexecution is
distinguished from executed work. Prior NO_WIRE is not a wire; prior same-kind
UNKNOWN or ACK vetoes another wire independently for close and removal.

Current actor records are recognized ONLY by exact IDs/body SHAs already ACKed in
this supervisor's held session. An untracked record from the same job is not
reconstructed as current state. Both current close/remove NO_WIRE intents must be
ACKed before either UNKNOWN. Phase reservation poisons before transport. The
engine must match its held current UNKNOWN SHA immediately before its one-use
wire latch, and perform fresh native guards itself. A new process cannot assume
an existing UNKNOWN was its own. Positive physical closed/absent proof may support
no-wire reconciliation in the separate engine; no receipt is rewritten or turned
into a repeat-wire permission.

Scopes and limits: 18-second child worker/hard parent timeout and20-second pipe
remain. Requests/receipts are bounded64KiB; phase ACK is4KiB, and new read-only
bundle/history responses explicitly64KiB. Source pack is393216bytes and internal
public cache pack262144bytes. There are at most64 commands/32phase reservations,
60 direct native metadata GETs per worker,500 jobs,1000 artifacts,200 callback runs
and10 attempts per callback run; all limits fail closed, never truncate. Official
artifact-client transport requests/retries are additional to that direct metadata
counter. Original effect clocks/budgets and original producer backend stay intact.

Native per-target workflow concurrency plus current complete native history is a
required engine/workflow precondition. Per-run artifact names are not cross-run
CAS or deletion-resistant exactly-once storage. Missing/deleted/expired receipt
history that remains visible holds; the API cannot prove the absence of an
administratively deleted entire run. No global lifetime-CAS qualification is
claimed. Native runtime token stays in JavaScript; the artifact worker receives
only runtime/ephemeral-read credentials, never GH_RUNNER_PAT, Console keys or AGE.
The fixed hosted Python controller receives its permitted manager environment but
all ACTIONS_* and artifact-read tokens are stripped. No tenant or provider receives
those credentials. Actual hosted interpreter ownership/runtime injection, real
artifact transfers, caller/concurrency integration, manager effects and physical
cleanup remain unqualified until ROOT independently reviews and runs them under
genuine capacity admission. This source component releases no operation.
