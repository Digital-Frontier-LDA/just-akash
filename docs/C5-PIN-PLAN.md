# akash-lease-core pin plan (C5 item 2)

Records the `akash-lease-core` release selected after the shared-core audit and
the checks required for later pin changes.

The prerequisite was tracked in `Digital-Frontier-LDA/akash-lease-core#13` (C5
tracking issue, sub-item 1: **Audit uniformity of bid-collection adapters**),
which is now closed.

## Current pin (as of this PR)

```
akash-lease-core @ https://github.com/Digital-Frontier-LDA/akash-lease-core/releases/download/v0.11.1/akash_lease_core-0.11.1-py3-none-any.whl#sha256=d7484d604cdff36b4869cc987f5c8c560d04fc428ac8b27ef9ed66da3b5cf18a
```

Pinned at `pyproject.toml:23`. Released 2026-09-11, v0.11.1 contains the shared
canonical workload identity and owner/DSEQ deployment key added by upstream
PRs #35 and #36. The wheel digest above matches the GitHub release asset.

## Adoption result

The prior v0.9.0 pin predated the canonical identity contract. v0.11.1 is the
smallest released update that carries both the `idv1` workload identity API and
the checksum-valid owner/canonical uint64 DSEQ key while preserving the existing
auction, capacity, wallet, order, and lease-shell imports used by this repo.

## Bump procedure

1. Identify the exact upstream release and read every change since the current
   pin. Confirm the APIs imported by this repo remain compatible.
2. Open a follow-up PR on this repo that:
   - updates the `akash-lease-core @ https://.../vX.Y.Z/...whl#sha256=...` line
     in `pyproject.toml:23` (both the URL and the SHA-256); and
   - regenerates `uv.lock` so its package version, URL, and hash agree.
3. The PR description cross-links the new `akash-lease-core` release and the
   upstream implementation PRs.
4. CI runs the full test matrix; in particular `tests/test_deploy.py` exercises
   the live `AuctionPolicy` import path. A green run confirms the bumped wheel
   is importable.

## Why the bump is gated

- The `akash-lease-core` core is sans-I/O and pinned to a wheel URL with a
  SHA-256 digest. A silent bump would change the `AuctionPolicy` semantics
  under us, exactly the failure mode C5 item 1 was written to detect.
- Every bump must preserve agreement with the wallet and GPU paths; the C5
  review's central diagnosis is that all downstream consumers must agree on
  selection.
- This repo's SDL validation test (C5 item 1, PR #179) cites the
  `AuctionPolicy.collection_window_seconds` field by name; a non-uniformity
  release that renamed this field would invalidate that test. The uniformity
  audit is the contractual guarantee that the field name is stable.

## Cross-references

- C5 tracking, just-akash: this repo's issue tracking the structural review
  (issue #178, parent: `Digital-Frontier-LDA/just-akash#178`).
- C5 tracking, akash-lease-core: `Digital-Frontier-LDA/akash-lease-core#13`,
  sub-item 1 is the prerequisite for this PR.
- C5 review document:
  `.planning/reviews/consultant5-cicd-dx-structural-review-2026-08-22.md`,
  section "Shared auction contract" and "Addendum: provider-input boundary".
