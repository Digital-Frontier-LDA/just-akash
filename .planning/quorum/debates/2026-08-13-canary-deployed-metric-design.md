# Quorum Debate
Question: Canary `deployed` metric — source of truth, undeployed-provider behaviour, targets.json format, required tests
Date: 2026-08-13
Consensus: APPROVE (CONVERGED — unanimous with a dry improvement stream)
Rounds: 3

## Round 1
| Model | Position | Citations |
|---|---|---|
| Claude (ADVISORY) | Option B; bool(uri) unsound; undeployed should publish no reachability | ensure.py:353-357; collect.py:162,231,395 |
| claude-1 | BLOCK — B; publish reachable=0, increment checks_total, not unreachable | ensure.py:354-357; collect.py:392-395,162-163,231 |
| codex-1 | BLOCK — B; publish ONLY deployed=0; no reachable, no counters | collect.py:138,162,230,392; ensure.py:354 |
| copilot-1 | BLOCK — B; deployed=0 AND reachable=0; no counters | artifact §3, §4 |
| antigravity-1 | Plan (no verdict token) — B; deployed=0 and reachable=0 | collect.py merge/render/main |
| claude-z-ai | Plan — B; state MUST be pruned | collect.py merge/render |
| claude-minimax | BLOCK — B; liveness from found deps; warns unclassifiable startup branch | ensure.py:354-357; collect.py:395 |
| claude-kimi | UNAVAIL | — |

## Round 2
| Model | Position | Citations |
|---|---|---|
| Claude (ADVISORY) | Drafted plan v2 adopting OMIT | — |
| claude-1 | APPROVE; impr: main() passes deployed explicitly | collect.py:231-232,157-163,392-395 |
| codex-1 | APPROVE; impr: render() gates scrape metrics on deployed==1 | ensure.py:347-357; collect.py:157-163,230-232 |
| copilot-1 | APPROVE; impr: structural guard in render() | collect.py:232,162-163 |
| claude-z-ai | **BLOCK** — mechanism gap: merge must STORE, render must GATE | collect.py:138,231-263 |
| claude-minimax | APPROVE; impr: verify grafana rule shape; cap carry-prev | ensure.py:354-357 |
| antigravity-1 | UNAVAIL (headless read_file denied) | — |

## Round 3
| Model | Position | Citations |
|---|---|---|
| Claude (ADVISORY) | v3: mechanism specified; df-grafana rules read directly | alert.rules.yml:1215-1221,1289-1294,1471,1590,1657 |
| claude-1 | APPROVE — no remaining improvements | collect.py:129-138,149-170,215-322,392-395 |
| copilot-1 | APPROVE — improvement stream dry | collect.py:157 |
| claude-z-ai | APPROVE — R2 BLOCK resolved | collect.py:138,157-163,231-263 |
| claude-minimax | APPROVE — accepts freshness-cap rejection | collect.py:155,157-170,230-263 |
| codex-1 | UNAVAIL (stall, no output) | — |
| antigravity-1 | UNAVAIL (headless read_file denied) | — |

## Outcome
Ratified plan v3. Source of truth: `ensure.plan()` writes a `live` boolean into every
targets entry (deps→true; proven-missing→false with uri/dseq still carried; unclassifiable
→true because a lease exists). `collect.py` reads `t.get("live", bool(uri))`, the fallback
being migration-only. An undeployed provider publishes EXACTLY
`akash_canary_deployed{provider} 0` — no reachable, no scrape_seconds, no counter
movement — enforced in three places: merge() stores `deployed` by assignment before the
early return, merge() skips both counters, render() gates every per-provider scrape-derived
series on the stored field, and main() passes it explicitly.

Decisive evidence: the df-grafana paging rules `AkashCanaryUnreachable` and
`AkashCanaryScrapeTimingOut` are both LEFT-GATED on `akash_canary_scrape_seconds`, and no
`absent()` rule exists on the canary per-provider series. Omitting those series therefore
stops the page with no cross-repo change — verified, not assumed.

Also ratified: entries written for never-deployed rostered providers; the RENDERED view
scoped to the current roster; `akash_canary_providers_total` as the denominator; two
tests driving `main()` (not `merge()`); and the existing assertion pinning
`akash_canary_reachable{provider="onidc"} == 0` inverted to assert ABSENCE.

## Improvements
| Model | Suggestion | Rationale |
|---|---|---|
| codex-1 | Omit reachability + all scrape-derived series for an undeployed provider | Absence stops the existing page with no df-grafana edit |
| claude-z-ai | merge() must STORE deployed; render() must GATE on it | render cannot know which providers to skip otherwise |
| copilot-1 | Enforce the gate structurally in render() | a persisted state entry would still leak reachable=0 after the flag flips |
| claude-1 | main() passes deployed explicitly, never merge()'s default | makes the wiring visible and testable at the call site |
| claude-minimax | Verify the grafana rule is `== 0` not `absent()` before relying on OMIT | an absent()-based rule would invert the fix into a new false page |

## Implementation notes (deviations from the ratified text, and why)

1. D3(b) said "prune state to the current target set before render". The implementation
   does NOT delete: `mark_absent_undeployed()` zeroes `deployed` for a departed provider
   so its cumulative counters and history survive, and `render(roster=...)` scopes the
   published view instead. Same observable outcome — no stale `deployed 1`, no inflated
   fleet scalars — without discarding durable data.

2. D2 said `deployed=None` falls back to `reachable`. Implemented as `True`. Deferring to
   `reachable` makes an unreachable scrape indistinguishable from a never-deployed
   provider, so it takes the no-scrape-attempted branch and silently drops the outage it
   was called to record. Caught by an existing test during implementation.

3. Review (post-ratification) found two defects in the first cut, both fixed:
   a partial listing rewrote a LEGACY targets entry as an explicit `live: false`, which
   would persist a guess and silence a healthy canary for the rest of the rollout window;
   and `providers_total` counted durable state rather than the current roster, so a
   retired provider inflated the denominator forever.
