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
| `deploy.py:137` | Close a run-attributed orphan after an ambiguous create | Reconciliation/compensation; use the originating creator capability or a durable prepared receipt |
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
- The fake Console and the API contract test return only `dseq` and `manifest`.
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
3. **Bind capability.** Mint an in-memory, operation-scoped creator capability containing the
   Console backend identity, selected client, signer owner, operation ID, and digest of the complete
   group population. It is single-use for create and may compensate only the deployment bound by
   that create. It is not later sweeper authority.
4. **Prepare journal.** Durably create the operation record in an external journal before the
   create request. It contains no credential. Failure to prepare means no create.
5. **Create once.** Submit the SDL through the capability. A timeout or gateway error is
   `create_outcome_unknown`, never proof that nothing committed and never permission to post again.
6. **Bind identity.** Require a canonical owner and DSEQ from the response, require the owner to
   equal the pre-resolved signer, construct one `DeploymentKey`, and perform an exact same-client
   readback. A mismatch or unreadable result enters compensation/reconciliation.
7. **Persist before handoff.** Append the bound `DeploymentKey`, response evidence, readback
   evidence, and complete group population to the prepared record before bid polling, lease
   creation, workflow output, or return to the caller.
8. **Handoff.** Only a durably bound record may proceed to bids and leases or be returned.
9. **Compensate.** A pre-handoff failure requests close only through the originating creator
   capability. Never reselect a wallet for this close.
10. **Prove closure.** Record `closed` only when two independent chain sources agree on the exact
    owner/DSEQ deployment, complete lease population, terminal lease states, closed deployment,
    and closed escrow. Complete, agreeing empty lease histories are valid only with the same
    deployment and escrow proof.
11. **Retain uncertainty.** Any unreadable response, response disagreement, receipt failure,
    close failure, incomplete population, or post-close proof failure remains `unresolved`. It is
    never reported as closed.

The `already exists` recovery remains outside this capability. It selects deployments created by
earlier operations and therefore requires the typed cleanup authorization and closure proof being
developed separately. A successful old-deployment cleanup licenses a new transaction; it does not
turn the cleanup selector into a creator capability.

## Console create contract

The current normalized create result is insufficient. The adapter contract must return:

```text
CreateResult
  deployment_key.owner   canonical checksum-valid 20-byte akash Bech32 account
  deployment_key.dseq    canonical positive Akash uint64 string
  manifest               optional string
  backend_operation_id   stable Console request/transaction identifier when available
```

The adapter may obtain the key from a structured create response such as
`data.deployment.id.{owner,dseq}` or from a response plus an exact same-client readback. It must not
invent an owner from a later wallet-selection pass. If both response and readback carry a field,
they must agree. A bare DSEQ must not cross the adapter boundary.

If Console cannot return an owner, the consumer must first change or version that API contract.
Treating the pre-resolved signer as though the server echoed it would turn an assumption into the
evidence the check claims to verify.

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
- accepts only the `DeploymentKey` returned and reread for that create;
- permits compensation only for that key through the same client/backend;
- cannot authorize a scheduled sweep, operator close, unrelated DSEQ, or different owner;
- survives in durable form as evidence, while the credential-bearing object never does.

## External durable journal

The journal must outlive an ephemeral GitHub runner. `.tags.json`, `/tmp`, job outputs, and an
`upload-artifact` step are not sufficient: they can disappear with the runner or never execute
after abrupt cancellation.

The sink exposes idempotent, conditional transitions:

```text
prepare(operation, signer, backend, groups, intent) -> durable revision
bind_created(operation, expected_revision, deployment_key, evidence) -> durable revision
mark_handoff(operation, expected_revision, lease/evidence) -> durable revision
request_compensation(operation, expected_revision, cause) -> durable revision
mark_closed(operation, expected_revision, proof) -> terminal revision
mark_unresolved(operation, expected_revision, evidence, cause) -> durable revision
```

`prepare` must commit before create. Its complete workload identity supplies a bounded on-chain
candidate set when create commits but the response or later journal update is lost. Absence of a
terminal transition is itself an unresolved operation; a failed post-create update cannot erase
the already durable prepared record.

There is one unresolved schema prerequisite: v0.11.1 identities do not carry a create-operation
nonce or ordinal. Initial create, retry, and redeploy inside one CI run attempt can therefore have
the same lifecycle identity. A reader that finds more than one matching deployment must retain all
of them as unresolved and close none. Unique automatic reconciliation requires a later core schema
that adds a canonical creation ordinal/nonce to every group, or an equally strong chain-native
field. Do not overload `release`, GSEQ, or a local tag to simulate it.

The sink must support compare-and-swap or equivalent revision checks, bounded reads, retention
longer than the maximum deployment lifetime, authenticated writers, and read access for the
independent recovery worker. GitHub Actions artifacts may mirror records for review, but they are
not the authoritative sink.

## Receipt states

| State | Meaning | May report success? | Recovery action |
|---|---|---:|---|
| `prepared` | Signer, backend, intent, and all groups are durable; create may not have been attempted | No | Reconcile the exact owner and complete identity candidate set before retrying |
| `create_outcome_unknown` | Create transport failed without proving non-commit | No | Scan the exact owner and complete identity; multiple candidates remain unresolved |
| `created` | Response and same-client readback agree; `DeploymentKey` is durable | No | Continue or compensate with creator capability |
| `handed_off` | Caller received a bound deployment/lease | Yes, for creation only | Later lifecycle authority owns cleanup |
| `compensation_requested` | Pre-handoff failure requires close | No | Close through creator capability, then prove |
| `closed` | Complete two-source deployment, escrow, and lease proof is durable | Yes, for closure | None |
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
| Capability is backend/owner scoped | Matching response and same client proceed | Swap client, owner, operation, or DSEQ; assert no handoff and no unrelated close |
| Returned owner is evidence | Response and readback owner match pre-resolved signer | Remove owner, return a different owner, or synthesize owner locally; all hold and compensate/reconcile |
| Canonical deployment key | Checksum-valid owner and uint64 DSEQ proceed | Corrupt owner checksum; use zero, signed, padded, overflow, boolean, or non-string DSEQ |
| Complete group population | Two or more groups with matching observed GSEQs proceed | Corrupt only the second group, omit the last group, duplicate/reorder GSEQ, or mark population incomplete |
| Prepare is before spend | Durable prepare permits create | Make `prepare` fail; assert create count is zero |
| Created receipt is before handoff | Durable bind permits bid polling/lease/return | Make `bind_created` fail; assert none of those handoff effects occurred |
| Ambiguous create is not retried | Settled non-commit may retry under a new operation | Timeout after commit; assert one POST and recovery by owner plus complete identity; two matching candidates close neither |
| Receipt failure compensates | Failed bind requests compensation through the same capability | Fail bind, then swap wallet selection; assert only the original client is called |
| Rollback failure is retained | Successful close plus proof reaches `closed` | Make close raise; assert durable unresolved record contains the exact `DeploymentKey` and all groups |
| Close text is not proof | Two sources agree on full terminal population, deployment, and escrow | Return “closed” from API while one chain source is active/unreadable; assert unresolved |
| Empty lease history is typed | Two complete empty histories plus closed deployment/escrow pass | Remove pagination completion or open escrow; assert unresolved |
| Capacity cleanup is not suppressed | Probe close and proof reach `closed` | Make the `finally` close fail; assert unresolved rather than a successful probe result alone |
| Later cleanup remains separate | Typed sweeper authorization can close an earlier operation | Pass a creator capability to an unrelated DSEQ; assert refusal even if age/name match |

## Delivery order

1. Decide the core identity follow-up for a canonical create ordinal/nonce; until then, ambiguous
   multiple-candidate reconciliation must hold every candidate.
2. Land the Console create response/readback contract and its fake-server fixtures.
3. Provision the external journal and recovery-reader contract.
4. Adopt v0.11.1 typed deployment and workload identity in readers and SDL transformation.
5. Wire the initial create and all compensation exits in `deploy.py`.
6. Wire retry and redeploy as separate operations after #322 settles its overlapping recovery path.
7. Wire the capacity probe, including its currently suppressed close failure.
8. Add workflow repository/run-attempt/class inputs and require a durable journal sink.
9. Run the complete mutation matrix, full unit suite, Ruff lint/format, Pyright, and secret scans.

No production deployment is part of these steps. Production evidence comes later from an
operator-authorized canary and must record the exact released artifact, journal transitions,
complete group readback, and verified closure or durable unresolved outcome.
