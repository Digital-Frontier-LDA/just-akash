# Akash creation transaction adoption design

Status: design only; no writer implements this contract yet  
Measured against: `origin/main` at `a2f9f634a008017da74e304670fcb245693e61a0`  
Measured: 2026-09-11

## Decision

Do not describe an `akash-lease-core` v0.11.1 pin as adoption of deployment identity.
The release is compatible with the current code, but the current writers never call its
deployment-key or workload-identity APIs. Adoption is complete only when every create enters the
transaction below and every compensation close exits through the capability created by that same
transaction.

This document is the implementation checklist. It deliberately does not change a dependency,
create or close a deployment, or alter the cleanup work in open PRs #321, #322, #325, and #328.

## Measured inventory

The initial audit counted 11 `close_deployment` calls in `just_akash/deploy.py`. Extending the scan
to every writer found a twelfth in `just_akash/capacity.py`. The complete boundary is therefore
four creates and twelve closes, not four and eleven.

| Site | Current purpose | Transaction role |
|---|---|---|
| `deploy.py:1008` | Initial deployment create | New transaction: prepare, create, bind, persist |
| `deploy.py:1025` | Retry after an `already exists` response | A separate create attempt and receipt; the cleanup that permits it uses separate sweeper authority |
| `deploy.py:1914` | Re-create an order after stale or missing bids | New transaction after the old transaction has verified closure |
| `capacity.py:129` | Create an order-only capacity probe | New transaction with the same identity and receipt rules |
| `deploy.py:137` | Close a run-attributed orphan after an ambiguous create | Reconcile first; compensate only after the prepared identity is positively bound through the originating creator capability |
| `deploy.py:274` | Close old deployments before an `already exists` retry | Later sweeper authority, not creator capability |
| `deploy.py:1523` | Compensate after no bids | Originating creator capability |
| `deploy.py:1548` | Compensate after all bids are malformed | Originating creator capability |
| `deploy.py:1561` | Compensate when bids contain no provider | Originating creator capability |
| `deploy.py:1588` | Compensate after all eligible bids expire | Originating creator capability |
| `deploy.py:1613` | Compensate after only foreign providers bid | Originating creator capability |
| `deploy.py:1756` | Compensate when the selected bid has no provider | Originating creator capability |
| `deploy.py:1891` | Close the old order before re-create | Originating capability for the old transaction, followed by complete closure proof |
| `deploy.py:1942` | Compensate when a re-created order gets no fresh bid | Originating capability for the new transaction |
| `deploy.py:2096` | Compensate after lease creation fails | Originating creator capability |
| `capacity.py:160` | Close an order-only probe in `finally` | Originating creator capability; suppressed exceptions must become unresolved records |

Additional measured facts:

- 78 test fixtures configure a create response; zero return an owner.
- The fake Console and the API contract test return only `dseq` and `manifest`. The
  [official Console contract](https://akash.network/docs/api-documentation/console-api/api-reference/)
  likewise does not echo an owner: it returns `dseq`, `manifest`, and `signTx` transaction
  evidence. An exact deployment read returns the owner later.
- A single configured wallet deliberately skips `account_address()` during selection and returns
  `account=None`. The owner is resolved only after lease creation, and failure is nonfatal.
- No production call site invokes `just_akash.workload_identity.format_identity`,
  `classify_groups`, or `transform_sdl`.
- The runner workflow writes a run ID into `group_spec.name` but writes no run attempt.
- There is no receipt or journal module.
- Compensation failures are logged but do not leave a durable unresolved record.

A temporary exact pin to the v0.11.1 wheel and digest passed 242 targeted deployment, wallet,
identity, cleanup, and pin tests. That result proves compatibility. It does not prove that any new
v0.11.1 contract is used.

## Required transaction

Every operation that may spend escrow follows these phases. A phase may be retried idempotently,
but it may not be skipped.

1. **Validate intent.** Validate workload class, repository, run and attempt or release, every SDL
   placement, deposit, and provider policy before selecting a wallet.
2. **Resolve signer.** Ask the selected Console credential for its authoritative account before
   create, including the single-wallet case. Construct a checksum-valid canonical Akash owner.
   A backend that cannot establish its owner before create is not eligible to create.
3. **Allocate and prepare.** Atomically allocate the next canonical creation-operation ordinal
   inside the lifecycle identity and durably create its prepared record. A unique constraint or
   compare-and-swap must prevent concurrent producers from receiving the same identity. Failure
   to commit the prepared record means no create.
4. **Bind capability.** Mint an in-memory, operation-scoped creator capability containing the
   Console backend identity, selected client, signer owner, operation ID, and digest of the complete
   group population. It is single-use for create and may compensate only the deployment bound by
   that create. It is not later sweeper authority.
5. **Create once.** Submit the SDL through the capability. A timeout or gateway error is
   `create_outcome_unknown`, never proof that nothing committed and never permission to post again.
6. **Bind identity.** Require a canonical DSEQ and successful transaction evidence from the
   Console response. If the response carries an owner, require it to equal the pre-resolved signer.
   Then obtain positive direct-chain evidence that the exact pre-resolved owner and returned DSEQ
   contain the complete prepared GSEQ/name population and digest. Only that same-operation binding
   constructs a `DeploymentKey`. A mismatch or unreadable result enters reconciliation and cannot
   authorize handoff or rollback.
7. **Persist before handoff.** Append the bound `DeploymentKey`, response/transaction evidence,
   direct-chain binding evidence, and complete group population to the prepared record before bid
   polling, lease creation, workflow output, or return to the caller.
8. **Handoff.** Only a durably bound record may proceed to bids and leases or be returned.
9. **Compensate.** A pre-handoff failure requests close only through the originating creator
   capability and only after the same-operation binding in phase 6. Never reselect a wallet for
   this close. If the response DSEQ cannot be bound to the prepared groups, retain the operation
   for reconciliation and close nothing.
10. **Prove execution closure.** Record execution closure only when two independent direct-chain
    sources agree on the exact owner/DSEQ deployment, complete group and lease populations, and
    terminal deployment, group, and lease states. Complete, agreeing empty lease histories are
    valid only with the same deployment and group proof.
11. **Prove settlement separately.** Record settlement only when the same evidence proves the
    deployment escrow and payments closed. Akash's
    [escrow close path](https://github.com/akash-network/node/blob/main/x/escrow/keeper/keeper.go)
    may validly close a deployment while an escrow account remains overdrawn; retain that as
    `execution_closed_unsettled` for financial recovery rather than reporting an active workload
    leak or a clean account.
12. **Retain uncertainty.** Any unreadable response, response disagreement, receipt failure,
    close failure, incomplete population, or execution-closure proof failure remains `unresolved`.
    If execution closure is already proven but settlement cannot be read, retain
    `execution_closed` with settlement unmeasured; never promote either case to `settled`.

The `already exists` recovery remains outside this capability. It selects deployments created by
earlier operations and therefore requires the typed cleanup authorization and closure proof being
developed separately. A successful old-deployment cleanup licenses a new transaction; it does not
turn the cleanup selector into a creator capability.

## Console create contract

The current normalized create result is insufficient. The official Console POST does not return
an owner, so the adapter must preserve response evidence without inventing one:

```text
CreateSubmissionResult
  dseq                    canonical positive Akash uint64 string
  manifest                optional string
  transaction_id          Console signTx transaction hash
  transaction_code        successful Console signTx code
  returned_owner          optional; when present must equal the pre-resolved owner

BoundCreateResult
  deployment_key          pre-resolved owner plus submitted DSEQ
  group_digest            complete direct-chain GSEQ/name digest matching the prepared record
  response_evidence       immutable CreateSubmissionResult digest
  binding_evidence        immutable direct-chain observation digest
```

The adapter constructs a key only after the response DSEQ and transaction evidence are positively
bound by direct-chain observation to the authoritative pre-resolved owner and the complete prepared
GSEQ/name digest. It must not resolve an owner from a later wallet-selection pass, treat the owner
as though Console echoed it, or expose a bare DSEQ as a durable receipt. A response owner, when a
backend supplies one, is an additional equality check rather than a requirement imposed on the
official Console API.

## Complete group population

Before create, the adapter parses the complete SDL and atomically transforms every placement and
every corresponding deployment reference with `akash_lease_core.workload_identity.transform_sdl`.
The identity mapping must cover exactly all placements. CI identities include repository, class,
run, run attempt, and canonical GSEQ. Payload identities include repository, class, release, and an
expiry only for staging.

The journal stores ordered `(gseq, group_spec.name)` observations and their digest. Post-create
readback supplies typed `GroupObservation` values and explicit
`PopulationCompleteness.COMPLETE`. Missing, duplicate, reordered, malformed, or conflicting groups
hold the transaction. Group zero, list position, and “first group” are never identities.

The repository-local identity implementation is older than the core API. Removing it requires
migrating readers to typed observations and explicit completeness. A compatibility re-export that
keeps passing name-only lists would preserve the duplicated semantics under a new import path.

## Creator capability

The capability is opaque and nonserializable. A receipt records only safe identifiers and hashes,
never an API key, token, authorization header, or client representation. Its invariants are:

- minted only after authoritative owner resolution and validation;
- bound to exactly one backend, owner, operation ID, and complete SDL group digest;
- permits at most one create;
- accepts only the `DeploymentKey` formed after the returned DSEQ is positively bound by direct
  chain evidence to that owner and complete prepared group digest;
- permits compensation only for that same-operation-bound key through the same client/backend;
- permits no compensation when the response, transaction evidence, or direct-chain binding is
  missing, malformed, incomplete, or conflicting;
- cannot authorize a scheduled sweep, operator close, unrelated DSEQ, or different owner;
- survives in durable form as evidence, while the credential-bearing object never does.

## External durable journal

The journal must outlive an ephemeral GitHub runner. `.tags.json`, `/tmp`, job outputs, and an
`upload-artifact` step are not sufficient: they can disappear with the runner or never execute
after abrupt cancellation.

The sink exposes idempotent, conditional transitions. Allocation and prepare are one atomic
operation so a process cannot reserve an ordinal and fail before making the unresolved operation
visible:

```text
allocate_and_prepare(lifecycle, signer, backend, groups, intent) -> operation + durable revision
mark_create_outcome_unknown(operation, expected_revision, evidence) -> durable revision
bind_created(operation, expected_revision, deployment_key, response, chain_binding) -> durable revision
mark_handoff(operation, expected_revision, lease/evidence) -> durable revision
request_compensation(operation, expected_revision, cause) -> durable revision
mark_execution_closed(operation, expected_revision, proof) -> durable revision
mark_settled(operation, expected_revision, escrow/payment proof) -> terminal revision
mark_unresolved(operation, expected_revision, evidence, cause) -> durable revision
```

`allocate_and_prepare` must commit before create. Its complete workload identity supplies a bounded
on-chain candidate set when create commits but the response or later journal update is lost.
Absence of a terminal transition is itself an unresolved operation; a failed post-create update
cannot erase the already durable prepared record.

The official Console deployment read lists active leases, so it cannot prove a terminal or
positively complete empty lease history after close. Execution-closure and settlement transitions
therefore consume direct-chain group, market, deployment, and escrow observations with explicit
population completeness. A Console DELETE result or subsequent Console 404 is transport evidence
only.

There is one unresolved schema prerequisite: v0.11.1 identities do not carry a create-operation
nonce or ordinal. Initial create, retry, and redeploy inside one CI run attempt can therefore have
the same lifecycle identity. A reader that finds more than one matching deployment must retain all
of them as unresolved and close none. Unique automatic reconciliation requires a later core schema
that adds a canonical creation ordinal/nonce to every group, or an equally strong chain-native
field. The external journal allocates that ordinal atomically with a uniqueness key covering the
owner, authenticated producer, repository, class, and lifecycle identity; it does not overload
`release`, GSEQ, or a local tag to simulate it.

The sink must support compare-and-swap or equivalent revision checks, bounded reads, retention
longer than the maximum deployment lifetime, authenticated writers, and read access for the
independent recovery worker. GitHub Actions artifacts may mirror records for review, but they are
not the authoritative sink.

## Receipt states

| State | Meaning | May report success? | Recovery action |
|---|---|---:|---|
| `prepared` | Signer, backend, intent, atomically allocated ordinal, and all groups are durable; create may not have been attempted | No | Reconcile the exact owner and complete identity candidate set before retrying |
| `create_outcome_unknown` | Create transport failed without proving non-commit | No | Scan the exact owner and complete identity; multiple candidates remain unresolved |
| `created` | Response DSEQ/transaction evidence and direct-chain owner/DSEQ/group binding agree; `DeploymentKey` is durable | No | Continue or compensate with creator capability |
| `handed_off` | Caller received a bound deployment/lease | Yes, for creation only | Later lifecycle authority owns cleanup |
| `compensation_requested` | Pre-handoff failure requires close | No | Close through creator capability, then prove |
| `execution_closed` | Complete two-source proof shows the deployment, every group, and every lease terminal; settlement is not yet measured | Yes, for workload execution only | Prove escrow/payment settlement |
| `execution_closed_unsettled` | Execution is closed but escrow or payment remains overdrawn | No clean-account claim | Retain financial recovery and alerting until settled |
| `settled` | Complete two-source proof also shows escrow and payments closed | Yes, for execution and settlement | None |
| `unresolved` | Any identity, persistence, close, reread, or proof uncertainty remains | No | Durable recovery queue; never claim closed |

## Effect-test matrix

Every source mutation must assert its target count is exactly one before execution and must assert
the intended observable population or state actually changed. A substitution that applies zero
times, or applies without changing the effect, is not evidence.

| Property | Positive/control | Required effect mutation |
|---|---|---|
| All four create sites use the transaction | Each writer produces `prepared` then `created` before its next action | Bypass each real call site independently; each mutant must create without a receipt and fail |
| Pin is adoption only when used | Core `DeploymentKey` and workload identity are reached by real writers | Change only the dependency pin; the adoption integration test must remain red |
| Signer precedes create | Valid signer creates | Move/disable signer resolution; assert create was never called |
| Capability is backend/owner scoped | Matching response and direct-chain binding proceed | Swap client, owner, operation, DSEQ, or prepared digest; assert no handoff and no unrelated close |
| Console owner is not invented | Owner-less official response plus matching direct-chain binding proceeds | Return a different optional owner or synthesize an owner as response evidence; both hold |
| Canonical deployment key | Checksum-valid owner and uint64 DSEQ proceed | Corrupt owner checksum; use zero, signed, padded, overflow, boolean, or non-string DSEQ |
| Complete group population | Two or more groups with matching observed GSEQs proceed | Corrupt only the second group, omit the last group, duplicate/reorder GSEQ, or mark population incomplete |
| Ordinal allocation and prepare are atomic | A unique prepared operation permits create | Race two allocators or make `allocate_and_prepare` fail; assert one unique ordinal and zero unjournaled creates |
| Created receipt is before handoff | Durable bind permits bid polling/lease/return | Make `bind_created` fail; assert none of those handoff effects occurred |
| Ambiguous create is not retried | Settled non-commit may retry under a new operation | Timeout after commit; assert one POST and recovery by owner plus complete identity; two matching candidates close neither |
| Receipt failure compensates only a bound operation | Failed journal bind after positive chain binding requests compensation through the same capability | Fail chain binding or corrupt the prepared digest; assert no close. Fail only journal persistence after binding, then swap wallet selection; assert only the original client closes the bound key |
| Rollback failure is retained | Successful close plus proof reaches `execution_closed`, then `settled` when escrow permits | Make close raise; assert durable unresolved record contains the exact `DeploymentKey` and all groups |
| Close text is not proof | Two sources agree on full terminal deployment/group/lease population | Return “closed” from API while one chain source is active/unreadable; assert unresolved |
| Settlement is separate | Closed deployment/groups/leases and closed escrow reach `settled` | Return overdrawn escrow; assert `execution_closed_unsettled`, never active leak or settled account |
| Empty lease history is typed | Two complete empty histories plus terminal deployment/groups pass | Remove pagination completion; assert unresolved |
| Capacity cleanup is not suppressed | Probe close and proof reach `execution_closed`; settlement is recorded separately | Make the `finally` close fail; assert unresolved rather than a successful probe result alone |
| Later cleanup remains separate | Typed sweeper authorization can close an earlier operation | Pass a creator capability to an unrelated DSEQ; assert refusal even if age/name match |

## Delivery order

1. Decide the core identity follow-up for a canonical create ordinal/nonce; until then, ambiguous
   multiple-candidate reconciliation must hold every candidate.
2. Land the Console submission-result and direct-chain binding contract and its fake-server fixtures.
3. Provision the external journal and recovery-reader contract.
4. Adopt v0.11.1 typed deployment and workload identity in readers and SDL transformation.
5. Wire the initial create and all compensation exits in `deploy.py`.
6. Wire retry and redeploy as separate operations after #322 settles its overlapping recovery path.
7. Wire the capacity probe, including its currently suppressed close failure.
8. Add workflow repository/run-attempt/class inputs and require a durable journal sink.
9. Run the complete mutation matrix, full unit suite, Ruff lint/format, Pyright, and secret scans.

No production deployment is part of these steps. Production evidence comes later from an
operator-authorized canary and must record the exact released artifact, journal transitions,
complete group readback, and separate verified execution-closure and settlement outcomes or a
durable unresolved record.
