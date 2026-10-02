# Finalized execution-closure observation

This is the read-only consumer adapter for `akash-lease-core` v0.15.2's
`ExecutionClosure`. It is a prerequisite slice for
[just-akash#332](https://github.com/Digital-Frontier-LDA/just-akash/issues/332),
[core#39](https://github.com/Digital-Frontier-LDA/akash-lease-core/issues/39), and
[the savings rollout](https://github.com/Digital-Frontier-LDA/df-cicd/issues/381).
It does not activate a runner migration or replace the current teardown path.

## Invocation

Use the exact owner, DSEQ, complete ordered creation group population and successful
close transaction hash retained by the controller. The operation ID correlates the
observation with that operation; this command does not authenticate its provenance.

```sh
just-akash verify-finalized-closed \
  --owner "$RECORDED_OWNER" \
  --dseq "$RECORDED_DSEQ" \
  --operation-id "$RECORDED_OPERATION_ID" \
  --groups-json "$RECORDED_COMPLETE_GROUPS_JSON" \
  --close-tx-hash "$RECORDED_CLOSE_TX_HASH"
```

No Console key or signer is needed. There is no endpoint override. Explicitly setting
`AKASH_REST_URL`, even to an empty string, refuses this path. The existing immutable
source registry identifies operators, gateway/cache ancestry, chain ID, tip-minus-two
finality, freshness and height skew. Redirects are refused for every observation.

The adapter first runs the existing complete signed-creation proof. That establishes
the complete GSEQ/name population, agreeing fresh common block and exact creation
height. It then requires, on both registered sources:

1. A successful signed `MsgCloseDeployment` for the exact owner/DSEQ. The reported
   transaction hash, decoded message and inclusion height must match the complete
   raw/decoded inclusion block. The inclusion block may not predate creation or be
   later than the common finalized observation.
2. Closed deployment and every closed group at that exact finalized height, with
   the same complete creation population.
3. All lease pages at that height, with a stable explicit total, exact owner/DSEQ,
   known GSEQ, checksum-valid provider identities, no duplicate leases and an
   agreeing end marker. Both sources must agree on every lease identity and state.
   Empty histories are accepted only with this positive completeness evidence and
   the closed deployment/group observations.
4. Completion before the identity observation expires. Pinned state requests must
   echo `x-cosmos-block-height`; an ignored height request is a refusal.

The success JSON contains `execution_closed: true`, the core's canonical typed
closure envelope, and the evidence whose canonical bytes match its evidence digest.
Failure produces `execution_closed: false` and exit 1. Endpoint error bodies are
not emitted. There is no blind create/close retry in this command; propagation or
availability failures remain unverified and require a fresh bounded observation.

## Settlement and adoption boundary

`settlement_proven` is always false. Escrow state is retained only as diagnostic
evidence. Closed, overdrawn, open or unreadable escrow cannot manufacture a complete
payment-settlement proof. Verified execution closure and financial settlement release
different containment populations in the core; this command releases neither.

The local evidence schema is `just-akash-finalized-execution-closure/v1`. Its lease
digest uses the sorted exact owner/DSEQ/GSEQ/OSEQ/BSEQ/provider/state tuples in the
report. This is not a claim of the standard's future cross-adapter broker wire
conformance. The typed closure envelope uses the pinned core serialization.

Runtime adoption still requires an authenticated external durable journal and
owner-wide atomic admission/redemption, controller-retained close transaction
metadata, atomic evidence/reservation transitions, protected recovery and live
release qualification. The legacy `verify-closed` command and existing callers
retain their behavior; a green legacy verdict cannot satisfy the new qualification.
Do not mark a consumer migrated or remove its hosted fallback based on this slice.

## Recorded read-only chain check

[The recorded report](evidence/finalized-closure-blazing-37014155266.json) observes
the runner deployment from [Blazing run 37014155266](https://github.com/Borduas-Holdings/blazing/actions/runs/37014155266).
Both registered operators proved successful close inclusion at height 28,886,415
and complete terminal state at height 28,888,641. The real lease uses `bseq: 0`;
zero is valid for that field and is retained canonically. The evidence digest is
`89f728fcd29a8449783347c6cdc915fbe1feeb51183e9a9413b726c3e4143e7f`.
No deployment, runner or close was created by this read-only check. Its expired
observation is historical validation, not current authority, payment settlement,
Digital-Frontier pool qualification or the required post-release observation manifest.

## Security findings

Source responses and caller inputs are untrusted. Subject, transaction, populations,
source independence, height and freshness all have explicit refusal paths. This
adapter has no destructive authority. Producer authentication belongs to the broker.

## Checks performed

Tests exercise the real creation readers and core envelope, complete multipage lease
histories, stale/aliased sources, invalid or foreign close transactions, raw/decoded
block disagreement, omitted second groups, invalid/missing height echoes and an
actual HTTP redirect with zero requests to the redirected path. An effect mutation
that accepts active leases changes the closure verdict and is detected. The CLI
test proves the actual command invokes this adapter and reports failures without
printing caller-controlled exception text. Run the normal Ruff, Pyright and unit CI.

## Residual risk

The registry remains a trusted policy input, and these registered node observations
are not a locally verified light-client proof. Slow or unavailable/archive-pruned
sources can refuse valid closures. An operation ID supplied to this read-only command
is not a broker attestation. Real endpoint/image/lifecycle qualification is pending.

## Recommendation

Keep migration admission gated. Integrate this envelope into the durable transaction
and independent cleanup before claiming verified runner cleanup. Preserve unknown
outcomes and financial exposure until their respective evidence commits atomically.
