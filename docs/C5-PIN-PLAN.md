# akash-lease-core pin plan (C5 item 2)

Tracks the planned bump of the `akash-lease-core` wheel pin in `pyproject.toml`
from the currently pinned release to the release that carries the verified
uniformity audit.

This file exists so the bump is not forgotten once the upstream PR lands. The
prerequisite is tracked in `Digital-Frontier-LDA/akash-lease-core#13` (C5
tracking issue, sub-item 1: **Audit uniformity of bid-collection adapters**).

## Current pin (as of this PR)

```
akash-lease-core @ https://github.com/Digital-Frontier-LDA/akash-lease-core/releases/download/v0.7.0/akash_lease_core-0.7.0-py3-none-any.whl#sha256=65318a871ef04b8204f0665617acca05d5c514386dd86615f2a8c1976623cf55
```

Pinned at `pyproject.toml:23`. Released 2026-08-22 with the two-window provider
auction contract (`CHANGELOG.md` 1.43.1).

## Planned next pin

**TBD — depends on `Digital-Frontier-LDA/akash-lease-core` sub-item 1.**

The planned release will be the first `akash-lease-core` release published AFTER
the uniformity audit PR lands in `Digital-Frontier-LDA/akash-lease-core`. The
audit must demonstrate identical selection across the three downstream
consumers:

- the Console adapter (this repo, `just_akash/deploy.py`);
- the wallet path through `Blazing-Back`'s `console_api_backend`;
- `compiler/core/akash_bid_fetcher.py` (the GPU burst path).

Until that audit ships and is exercised by a release, **do not bump the pin**:
picking a non-uniformity-audited release would lock us into a non-verified
contract and silently break the "shared core" invariant that this repo's C5
review depends on.

## Bump procedure (when the uniformity-audit release ships)

1. Watch `Digital-Frontier-LDA/akash-lease-core/releases` for the first release
   whose changelog explicitly references the uniformity audit (item 1 of
   issue #13). Confirm the release notes contain the audit PR number.
2. Open a follow-up PR on this repo that:
   - updates the `akash-lease-core @ https://.../vX.Y.Z/...whl#sha256=...` line
     in `pyproject.toml:23` (both the URL and the SHA-256),
   - adds a one-line entry under a new `## [Unreleased]` heading in
     `CHANGELOG.md` citing the upstream release tag and SHA,
   - bumps `pyproject.toml` `version` to the next patch (the bump is
     downstream-only; the core release tag is the authority).
3. The PR description cross-links the new `akash-lease-core` release and the
   uniformity-audit PR.
4. CI runs the full test matrix; in particular `tests/test_deploy.py` exercises
   the live `AuctionPolicy` import path. A green run confirms the bumped wheel
   is importable.

## Why the bump is gated

- The `akash-lease-core` core is sans-I/O and pinned to a wheel URL with a
  SHA-256 digest. A silent bump would change the `AuctionPolicy` semantics
  under us, exactly the failure mode C5 item 1 was written to detect.
- A bump without the uniformity audit would lock us into a core that may not
  match the wallet path's expectations; the C5 review's central diagnosis is
  that all three downstream consumers must agree on selection.
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