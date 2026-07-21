# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.35.1] — 2026-07-21

### Fixed
- **The bounded re-deploy round can now actually recover from a provider-specific lease failure — it was guaranteed to re-pick the provider that had just failed (issue #84).** 1.33.0 taught the lease step to recover from a `404 no lease for deployment` by closing the un-leaseable order and re-creating it, but the re-selection had no provider diversification whatsoever: `failed_providers` was cleared immediately before the round, and `_poll_fresh_bid` took no `exclude` argument at all — it simply returned `min(pool, key=price)` over the unfiltered tier pool. So whenever the provider that just failed was also the cheapest bidder, the fresh order deterministically re-selected it and the single re-deploy round was spent reproducing the identical failure (observed in E2E run 29765070530, which re-picked the same provider and failed twice with the same 404). The fresh order now **de-prioritises** the provider(s) that just failed: preference order is fresh-preferred → fresh-backup → failed-preferred → failed-backup, so a provider that has not just failed always wins, tier order intact — a working BACKUP now beats a failed PREFERRED.

  Deliberately **soft, not a ban**: with n=2 we cannot prove the provider is at fault (versus Console-side order GC/propagation), and the allowlisted market is thin, so if the de-prioritised provider is the only bidder on the fresh order it is still leased — after the same courtesy window BACKUP already gets (`JUST_AKASH_REDEPLOY_BACKUP_COURTESY_S`, 20s), giving another provider a real chance to bid first. And deliberately **scoped to the 404 path**: a `stale` failure belongs to the ORDER's ~5-min bid clock, which every bid shares, so it carries no provider-specific signal — on a new order that provider is as good as any, and skipping it would shrink a thin market for nothing. The operator log names who is being skipped and that it is not a ban. The "not a ban" guarantee holds even under misconfiguration: if `JUST_AKASH_REDEPLOY_BACKUP_COURTESY_S` is set at or above `JUST_AKASH_REDEPLOY_WAIT_S` the courtesy window can never open before the poll gives up, so a de-prioritised bid seen during the wait is leased on the way out rather than silently turned into a hard ban.

## [1.35.0] — 2026-07-21

### Added
- **The Grafana last mile — the rendered metrics now ship to a fetchable URL, with the benchmark grades and the credit gauge on board.** 1.34.0 rendered the smoke telemetry into Prometheus format but the `.prom` died as a per-run CI artifact: nothing could scrape it, the hardware-benchmark stream wasn't rendered at all, and the deploy-credit gauge never ran in CI (the report job rightly holds no API key). Three changes close all three gaps. (1) `export-metrics --benchmark smoke-benchmark.jsonl` renders `just_akash_bench_*{provider}` gauges — delivered CPU rate, stability CV + the UNSTABLE verdict, throttle/steal/PSI + the UNDER-DELIVERING verdict, memory bandwidth — from each provider's **latest complete** grade, derived through the SAME `benchmark.resource_fidelity`/`stability` logic as the CLI report so dashboard and report can never disagree; absent inputs stay absent, never zero. (2) `export-metrics --credit-json FILE` reads a `balance --check --json` snapshot, so the credentialed smoke job snapshots the credit once and the unprivileged render step emits `just_akash_deploy_credit_usd` without ever holding the key (`--with-credit` still does the live query). (3) The accrue job now re-renders **`smoke-metrics.prom` onto the `telemetry` branch every run** — data and metrics land in the same commit, and any Prometheus can ingest the fleet's smoke health by fetching one stable raw URL (the DePIN-LiveAutobidder `price_server` splices it into its `/metrics`; Grafana dashboards + alert rules live in that repo). Rendered against the live accrued data: 234 samples, including onidc's CV 15.7% UNSTABLE flag and the under-delivering verdicts — and hetzner's conspicuous *absence* of grades (its probes aren't reaching the benchmark stage), which is exactly the kind of signal this exists to surface.

## [1.34.0] — 2026-07-19

### Added
- **Smoke telemetry is now Grafana-trackable — a Prometheus exporter turns the accrued JSONL into first-class metrics.** Until now the daily smoke's outcomes lived only as CI-log text and a `telemetry` git branch of JSONL: the natural errors we most want to watch (wallet out of funds → `no-credit`, no provider bid → `no-bid`, the lease died on-chain → `lease-down`, a full/offline provider → `no-room`) were invisible to any dashboard. New `just_akash/prometheus_exporter.py` (`just-akash export-metrics <jsonl> [--output f.prom] [--with-credit]`, plus a `just export-metrics` recipe) renders the SAME JSONL into Prometheus **textfile-collector** format — pure stdlib, no server, no new deps — exposing `just_akash_smoke_outcome_total{provider,feature,outcome}` (a counter, so each outcome is a trendable series for `rate()`/`increase()`), `just_akash_smoke_latency_ms{provider,feature,quantile}` (p50/p95/p99 over PASS samples, reusing `analyze_telemetry`'s percentile logic), and `just_akash_smoke_last_run_timestamp` (staleness alerting). The daily smoke's `report` job now renders and uploads a `.prom` artifact (non-gating, `if: always()`) so a scrape/pushgateway job can pick it up.
- **Deploy-credit burn-down gauge + a proactive low-credit alarm.** `export-metrics --with-credit` emits `just_akash_deploy_credit_usd{account}` (from `chain.deploy_credit(account_address())`, USD-pegged uact), so Grafana can trend and forecast when the wallet runs dry. And `balance --check --min-usd N` prints a machine-readable verdict (`CREDIT-CHECK status=OK|LOW …`, or JSON with `--json`) and exits non-zero when the remaining deploy credit is below the threshold — so a scheduled job flags a low wallet **before** deploys start returning HTTP 402.

## [1.33.0] — 2026-07-18

### Fixed
- **The `inject` check counted a known transport race as a provider failure — it reddened a scheduled run.** `inject` writes a file then reads it back with a lease-shell exec, and that readback can hit the cold-stdout race (rc=0 with EMPTY stdout: the exit-code frame arrives before the stdout frame) even though the write succeeded — so a healthy inject reads back as FAIL. Because inject does *two* round-trips and only the first was guarded, it sat at 93–97% fleet-wide while exec ran higher; today's 07:30 scheduled smoke went red with `inject: FAIL` on both bidding providers for exactly this reason. The readback now retries **only** the race signature (rc=0 + empty stdout), up to `SMOKE_INJECT_READBACK_ATTEMPTS` (default 3) with a short backoff; a nonzero rc or non-empty-but-wrong content still fails on the first read, so a genuine inject regression is never masked. This is the retry-on-empty-stdout remedy the quorum approved for the same race, applied where it was missing. Live: 25/25 inject PASS hammering one hgulk6 lease, then the lease settled clean with no escrow held.

## [1.32.0] — 2026-07-18

### Added
- **Benchmark grades are now persisted — the quality signal finally accrues.** Steps 1–2 measured hardware honesty and stability, but the numbers evaporated at the end of each run: nothing recorded them, so there was no history to score a provider against. The daily smoke now piggybacks a hardware benchmark on each healthy probe — it runs on the SAME lease *after* the feature matrix is fully recorded (so its load can never mask a feature result — the #61 concern) and *before* destroy (so it costs no extra lease or escrow). Each grade is one JSON line written to `SMOKE_BENCHMARK_FILE` and accrued to the `telemetry` branch as `smoke-benchmark.jsonl`, alongside the existing latency stream. Strictly non-gating: the benchmark only runs when the sink is set and when deploy+ready both PASS, and any failure is swallowed — a provider's smoke pass/fail is never touched by its grade. Live: a z9nr probe wrote a complete row (cpu_eps≈1229, mem 4.3 GB/s, 5 stability samples at ~4% CV, throttle/steal counters) and the lease settled clean with no escrow held. Step 3a of the provider quality/health build; the fleet-relative + own-baseline scoring analyzer (3b) reads this accrued file.

## [1.31.0] — 2026-07-18

### Added
- **Stability under sustained load — is the CPU *consistently* good, or fast-once?** A single spot benchmark can't tell a provider that peaks high then degrades (thermal, a neighbour ramping up) from a steady one. The probe now runs the cpu benchmark `_STABILITY_SAMPLES` more times (each short), and `stability()` reports mean/min/max and the coefficient of variation, flagging **UNSTABLE** when the swing exceeds the floor — high variance is also the fingerprint of a noisy neighbour on an oversubscribed host. Live: a z9nr lease read `stability steady (cv=1.9% over 5 runs)` while still `UNDER-DELIVERING` on throttle — the two signals are orthogonal and both correct. Step 2 of the provider quality/health build.

## [1.30.0] — 2026-07-18

### Fixed
- **The `benchmark` command produced no metrics on any provider — its script was never shell-interpreted.** It sent BENCH_SH down `exec()`, which uses the provider's *argv* path: the script ran, but `$()`, pipes, `;` and newlines came back literal rather than interpreted, so it emitted no `BENCH-` lines. (Shipped in #61 with unit tests only; verified now against a live provider that it returned zero metrics.) Added `LeaseShellTransport.exec_shell_script()`, which runs a script via `sh -c`, and pointed the benchmark at it. Live: the benchmark now returns real hardware (AMD EPYC 7502P, 8.4 GB/s RAM, ~1.3ms RTT, etc.).

### Added
- **Resource-honesty verdict: is the provider delivering the CPU it sold?** A provider can pass every feature check (responsive) and still hand you a fraction of a vCPU (not good) — invisible to pass/fail. The probe now snapshots the cgroup CPU-throttle counters and host steal AROUND its single-threaded cpu benchmark; `resource_fidelity()` derives `throttled_during`, `steal_pct`, and under-load CPU pressure, and the report flags **UNDER-DELIVERING** with the reason. Live proof: a fleet provider that passes 10/10 features throttled the single-threaded benchmark on every run — responsive, but capping CPU below spec. This is Step 1 of a provider quality/health assessment build.

## [1.29.0] — 2026-07-18

### Fixed
- **`inject` over lease-shell was silently writing EMPTY files — reverted the broken stdin-frame write.** #39/#28 (v1.27.0) switched the lease-shell inject from `echo <b64> | base64 -d > path` to `head -c <n>` over a `104` stdin data frame, to keep the secret out of the provider-proxy-logged URL. But that mechanism (`_exec_with_stdin_command`) does **not** actually deliver stdin to the container: measured live against **all three** providers, `head -c <n>` read **zero bytes** and wrote a **0-byte file** while `inject` reported "Injected N secret(s)" and exited 0 — silent data loss, strictly worse than the log leak it was fixing. (The #39 E2E passed falsely; the daily provider-smoke `inject` check caught it — `inject: FAIL` on all three healthy leases.) Reverted to the working `echo <b64> | base64 -d` write. Verified live: the file now lands with the correct 48 bytes and the smoke's `_inject_and_read` passes.

### Security (regression re-opened)
- Reverting the above re-introduces #39's original concern: the base64-obscured (trivially reversible) secret rides the shell command in the provider-proxy-logged URL. This is tracked for a proper re-fix once the stdin-frame path is made to actually deliver data and is validated against a live provider. A working inject that logs a reversible secret is a lesser evil than one that silently drops it.

## [1.28.0] — 2026-07-17

### Changed
- **`_get_proxy_ws_url` now rejects a plaintext `provider_proxy_url`.** `connect()` is always given a TLS context, so an `http://`/`ws://` proxy endpoint could never work — it failed opaquely deep in the websockets client. It now raises a clear `RuntimeError` naming the bad scheme, so the secret-bearing exec/inject paths can never fall back to an unencrypted socket. A `wss://` override is still accepted (it's TLS). (Hardening prompted by review on the docs PR below.)

### Docs
- Corrected the `lease_shell.py` module docstring: the default proxy is `https://console.akash.network/provider-proxy-mainnet` (converted to `wss://` at connect time), not the stale `wss://provider-proxy.akash.network/`; and the connection is a Console-hosted proxy with full TLS, not a direct-provider connection. (Issue #38 item 1.)
- Added a "SUPERSEDED — trust the shipped code" banner to the phase-07/08 design docs, which still described the abandoned direct-provider + `ssl.CERT_NONE` + `?cmd=` design. (Issue #38 item 2.)

## [1.27.0] — 2026-07-17

### Fixed (security)
- **Injected secrets no longer leak into provider-proxy logs.** `inject()` built `echo <base64> | base64 -d > path` and ran it via the shell path, which places the command in the URL's `cmd2=` — and provider-proxy logs that URL, so the base64-obscured (trivially reversible) secret landed in those logs. The write now streams the payload over a `104` stdin data frame via `_exec_with_stdin_command("head -c <n> > <path>", content_bytes)`, so the content is never part of the URL/argv (only its byte count, which is not secret). `head -c <n>` — not `cat` — because `cat` reads until stdin EOF, and provider-proxy does not translate the empty trailing stdin frame into a stdin close, so `cat > path` hangs forever; `head -c <n>` reads exactly `n` bytes and exits. `mkdir -p` and `chmod 600` are unchanged (neither carries the secret).

## [1.26.0] — 2026-07-17

### Added
- **`benchmark` command — grade what a provider ACTUALLY delivered** (vCPU throughput, RAM bandwidth, disk I/O, WAN RTT, contention), separate from the pass/fail smoke. Bounded well under the lease's cgroup (256M / 1 thread) so it never OOM-kills its own container, and never runs in the every-run smoke. Every metric degrades to *absent* rather than erroring, so a minimal image still yields the cheap signals.

### Fixed (from review — Copilot + CodeRabbit, PR #61)
- **WAN RTT reported the max, not the average.** The summary line is `min/avg/max[/mdev]`, and a positional `cut -f5` landed on max (busybox) or mdev (iputils) — skewing the grade. Now greps the numeric triple and takes field 2 (avg), robust across ping builds.
- **`disk_read` measured the page cache, not the disk.** `conv=fdatasync` flushes the write but leaves the pages resident, so the immediate read was served from RAM. Now uses `iflag=direct`; where the fs can't do O_DIRECT it honestly reports `na` instead of a cache-inflated number.
- **Remote probe output could overwrite trusted JSON metadata.** `--json` spread `**results` (from remote `BENCH-` lines) last, so a `BENCH-provider=` / `BENCH-dseq=` line could shadow the deployment-derived values. Trusted fields are now applied last.
- **`na` leaked into results as if it were a measurement.** The parser kept the `na` sentinel, contradicting the module's own contract that an unavailable metric is *absent*. Now dropped like an empty value.
- Disk artifacts use PID-unique paths plus an `EXIT`/`INT`/`TERM` trap, so a killed probe leaves no 256M file behind.

## [1.25.0] — 2026-07-17

### Changed
- **Latency-only SLO gate — the accrued telemetry view gates on p95 latency, never on reliability.** Closes out the 2026-07-14..17 accumulation burst. `aggregate()` now gives `LEASE-DOWN` its own counter, OUT of the pass/fail denominator, so the reported rate answers the actionable question ("when the lease was up, did the feature work?") instead of being deflated by provider infra the project already decided is non-gating (fleet-wide since v1.22.0). `--check` gates on **latency only**; reliability is printed as informational by design — this accrued view has no `fail_mode`/`in_pod_marker`/`eventual` context, so it structurally cannot tell a tooling regression on a healthy lease (which the per-run smoke gate already catches with full context) from demoted provider infra.
- New analyzer flags: `--min-version` (drop pre-fix rows a shipped fix made impossible) and `--quarantine` (measured and printed, never gating).
- **A `--check` with no ceilings now says so loudly.** An empty `--max-p95` gates on nothing — a valid calibration state, but indistinguishable from a live gate silently disabled by an empty `SMOKE_LATENCY_SLO_P95` env var. Since this whole telemetry effort exists because a gate that quietly stopped gating went unnoticed, it now prints `CHECK OK (GATE DISABLED — no ceilings set)` and warns on stderr rather than a bare green `CHECK OK`. (Caught in review by Copilot.)
- Workflow: the report step's ceilings + version floor + quarantine secret are wired and `continue-on-error` is removed — the gate is live.

## [1.24.0] — 2026-07-17

### Fixed
- **The leak audit trusted `just list`, which lies.** Measured: `just list` reported dseq `1784291290915` as **active** while that deployment's own record read `state=closed`, `escrow.state=closed`, `funds=0`. The collection endpoint (`GET /v1/deployments`) serves stale state; the per-deployment endpoint (`GET /v1/deployments/{dseq}`) is authoritative. `robust_destroy`'s post-destroy audit trusted the list, so a **perfectly clean destroy** printed `STILL listed after destroy — manual cleanup required` — and it fired in a real 3-provider validation run: a flake, in the suite whose whole job is to not flake. The false FAIL is the *benign* direction; the same staleness can report a deployment **GONE while its escrow is still open**, a silent leak — which is the exact thing the audit exists to catch. The audit now reads the deployment's own record and **fails closed**: only a positive "settled" reading clears it, because silence is what a leak looks like. It retries first — without that, one transient API blip would report a leak that isn't one, trading a silent-leak bug for a flaky-red one.
- A per-deployment read must name its dseq, so the audit can no longer be the static literal that made it injection-safe by construction. The **guarantee** is preserved by shell-quoting (`_run` uses `shell=True`, so an unquoted dseq is a live injection vector); the test now pins the guarantee rather than the obsolete mechanism, asserting it with a real injection payload. Substring collisions (dseq `123` vs an unrelated active `12345`) become impossible by construction rather than guarded against — we ask for *our* record instead of scanning a shared list.
- `test_audit_detects_lingering_deployment` passed **for the wrong reason**: its fixture was unparsable text, exercising the "could not confirm" path rather than the "still active" path it claimed to test.

Known gap, deliberately not widened here: `test_lifecycle.py` step 7 (`"just list — final audit"`) has the same weakness. Contrary to the report that prompted this, **no fails-closed `get_deployment` check existed anywhere** — that file has 7 steps, not 8, and never mentions escrow. New tests verified to fail against the pre-fix code (4/6).

## [1.23.0] — 2026-07-17

### Fixed
- **The smoke test was reporting healthy providers as `LEASE-DOWN`. It was our bug, not provider infra.** `_deploy` scraped the DSEQ out of deploy's output and returned `"ok"` **without ever checking the exit code**. But deploy prints the DSEQ at *create* time, long before bidding: on a no-bid it then closes the deployment itself and exits 1. The smoke took the scraped DSEQ as success, polled a deployment deploy had already closed, read `state=closed`, and blamed the provider for a lease that never existed. The `terminal state 'closed' after 6s` was just the first poll interval — the deployment had been closed ~150s earlier. The DSEQ match also short-circuited the `no-bid`/`no-credit` branches below it, making them **dead code**: telemetry shows `deploy` **PASS 116/116** and **not one `NO-BID` row** in the dataset's entire history. Measured: a real deploy pinned to a non-bidding provider exits **1**, prints `DSEQ=…`, logs `NO BID FROM 1 allowlisted provider(s)`, and closes the deployment — and `_deploy` returned `note='ok'` for it.
- **A live lease could be orphaned, draining escrow.** On the stale-bid path (issue #19) deploy closes the original order, mints a new one, leases *that*, and exits **0** — printing both dseqs. `re.search` returns the **first**, so the smoke tested the **closed** original (every feature reading `LEASE-DOWN`) while the **live** lease ran on unattended and cleanup destroyed the wrong dseq. Now uses `re.findall(...)[-1]`. Only one dseq can ever be live: the re-deploy aborts if the original's close fails, "to avoid double escrow". The exit-code gate does **not** fix this (it exits 0) and last-DSEQ does not fix the no-bid misreport — both are load-bearing.
- `dseq_ref` is now recorded whenever a DSEQ is seen **at all**, including on failure: deploy's own close is best-effort and can fail, so cleanup must still reach it. A redundant destroy is a no-op; a missed one drains real escrow.

Validated live across all 3 providers: hgulk6 (genuinely not bidding) now reports an honest `NO-BID` skip instead of a fake `LEASE-DOWN`, while aaul and z9nr both pass 10/10. Regression tests use transcripts of the real measured runs and were verified to fail against the pre-fix code.

## [1.22.0] — 2026-07-16

### Changed
- **`LEASE-DOWN` is now non-gating fleet-wide — because it's *always* provider infra, never a just-akash bug.** New evidence overturned the earlier "LEASE-DOWN gates" call: after quarantining hgulk6, a run went red because **aaul** (previously 100%) lease-downed — telemetry shows LEASE-DOWN on *both* providers, so a lease dying after the bid is accepted is a **fleet-wide** provider-fulfillment phenomenon, not one bad provider. Since just-akash deployed correctly and the *provider's* lease died on-chain, gating on it just flakes CI on any provider hiccup. So a `LEASE-DOWN` no longer fails the run for **any** provider (it stays fully visible — matrix, a `[NON-GATING]` verdict line, and telemetry — nothing masked). A **tooling regression** (a feature broken on a *healthy* lease) still gates everywhere.
  - **Safety valve (`_mass_lease_down`):** if ≥2 providers got a lease and **every** one of them LEASE-DOWNed in the *same* run, that's deterministic across the fleet — the tell-tale of a just-akash manifest/deploy bug (a malformed SDL every provider accepts then fails) rather than coincident hiccups — so it **re-gates** (exit 1, `mass_lease_down: true` in telemetry, a distinct verdict line). The ≥2 floor stops a single-provider run from degenerating back to "gate on any LEASE-DOWN". This is exactly the scenario that made CI flaky — an isolated aaul/hgulk6 lease death — now green, while a real manifest bug (all providers fail deterministically) still goes red.
  - `SMOKE_QUARANTINE_PROVIDERS` is **kept** — universal LEASE-DOWN-non-gating doesn't subsume it: the quarantine tier still owns the *update-ingress-stall* demotion, whose `in_pod_marker`/`eventual` evidence is less unambiguous than a terminal on-chain state and rightly stays opt-in. (Design: full 3-model quorum, unanimous — opencode-1 caught the ≥2-provider guard + the deterministic-manifest-bug path.)
- Tests: 1019 passing (+6) — a single/partial-fleet LEASE-DOWN is non-gating (even unquarantined), a fleet-wide simultaneous LEASE-DOWN gates, the ≥2 floor, and NO-BID providers don't count toward "all".

---

## [1.21.0] — 2026-07-16

### Added
- **Quarantine tier — a genuinely-unreliable provider can be monitored without its infra flakiness reddening CI.** The accrued data pins all remaining flakiness to one provider (hgulk6: ~8–33% lease-down + occasional update-cutover stalls — genuine hgulk6 *infrastructure* failures, unfixable from just-akash; aaul/z9nr are 100%). The smoke test conflated two purposes: catching just-akash **tooling** regressions (a feature breaking on a healthy lease — its original job) and provider **reliability** monitoring. `SMOKE_QUARANTINE_PROVIDERS` (comma-separated) now separates them: a quarantined provider is still deployed, tested, shown in the matrix, and recorded in telemetry (`quarantined: true`) — but its **provider-reliability** failures (`LEASE-DOWN`, or an update stall the diagnostics *prove* is an ingress-routing failure: new pod healthy, marker never routes) **do not gate the run**, while a **tooling regression** on it (any feature FAIL on a healthy lease, an update whose command failed, or a stale-update where `in_pod_marker=old`) **still gates**. So the CI gate stops flaking on hgulk6's genuine infra failures **without masking** — a real just-akash bug is deterministic across providers and still caught, and hgulk6's reliability stays fully visible in the matrix + SLO telemetry. (Design: full 3-model quorum, 2 unanimous rounds — a code-verified reading of the update diagnostics corrected the demote predicate so a stale-update stays gating.)
- Reliability-vs-tooling classifier `_is_reliability_failure` + the gate helper `_gating_providers`; records are now always collected in-memory (only *written* when a telemetry file is set) so the gate can read the diag.
- Tests: 1013 passing (+15) — the classifier taxonomy (LEASE-DOWN / command-fail / stale-update / ingress-stall / slow / plain-feature), and the gate demoting a quarantined provider's reliability failures while still gating its tooling regressions and every non-quarantined failure.

---

## [1.20.0] — 2026-07-15

### Fixed
- **`_deployment_dead` now recognizes the terminal `failed` state — fixing a 240s readiness waste + a mis-classified hgulk6 cascade.** A hgulk6 whole-deployment cascade (all 10 features FAIL) was root-caused: the deployment's on-chain state went to **`failed`** (the Console API maps `state ∈ {closed, failed}` → `status: down`), but the dead-state set was `{closed, insufficient_funds}` — **missing `failed`** — so readiness never fast-failed and burned the full 240s cap before cascading. Adding `failed` (a terminal on-chain state, zero false-failure risk — it is derived from on-chain state, not a flappable provider-health field, so no persistence polling is needed) makes it fail fast. No leak occurred — the `robust_destroy` audit safety-net confirmed closure.

### Added
- **Distinct `LEASE-DOWN` outcome** — a provider that *accepted the bid* and then let the lease die on-chain is a genuine reliability failure, but categorically different from a broken feature. When readiness fails on a terminal deployment state, cells now read `LEASE-DOWN` instead of 10 generic `FAIL`s (`_wait_ready` flags `diag["fail_kind"]` so the caller labels it without a second query). It **fails the run** — `_FAILING_OUTCOMES = ("FAIL", "LEASE-DOWN")` — because unlike the *pre-commitment* NO-BID / NO-ROOM / NO-CREDIT skips, the provider made and broke a fulfillment commitment; hiding hgulk6's ~8% lease-failure rate as a skip would defeat the smoke test. The verdict line tags it `[LEASE-DOWN: provider accepted the bid then the lease died]`. (Design reached by a full 3-model quorum, 2 rounds, unanimous — a verified reading of `api.py`'s status mapping flipped the one dissent.)
- Tests: 997 passing (+8) — `_deployment_dead` recognizes failed/closed, `_wait_ready` flags lease-down, `smoke_provider` marks cells `LEASE-DOWN` distinctly (vs a plain readiness FAIL), it is a failing (not skip) outcome, and its diag reaches telemetry.

---

## [1.19.0] — 2026-07-15

### Added
- **Slow-vs-stuck diagnostics extended to `_wait_ready` and `_check_ingress`** — completing the readiness/ingress/update instrumentation the quorum called for (v1.18.0 did update). The one whole-deployment cascade in the accrued data (deploy OK but the lease never became ready → all 10 features failed at once) returned a bare `False`, so we couldn't tell whether the container was SLOW (would serve with a bigger cap → widen it) or STUCK (a dead lease / unschedulable pod / a container that never serves → a genuine defect). Now, on a readiness or initial-ingress timeout, the check records into the run log **and** telemetry (`diag`), without ever flipping the FAIL: `service_at_timeout` (lease ready/total), `dead_at_timeout` (terminal lease state), `exec_at_timeout` (a one-shot lease-shell exec — and an rc=0-but-empty-stdout is treated as **not** working, so the cold-stdout race can't fake a live container), `last_at_timeout` (ingress last error), and the bounded post-cap observation (`eventual`/`eventual_after_s`/`fail_cap_s`). Every probe is exception-isolated so one failing probe can't abort the classification. The next hgulk6 cascade will say exactly whether the container was slow or never came up.
- Tests: 985 passing (+12) — availability/exec probes (incl. empty-stdout → not-working), the ready recorder classifies slow / exec-up-but-availability-unreported / all-probes-raising, `_wait_ready` + `_check_ingress` invoke their recorders on timeout, and the ingress recorder captures service + last-error.

---

## [1.18.0] — 2026-07-15

### Added
- **Slow-vs-stuck diagnostics on an update-cutover timeout — so an `update` FAIL self-explains instead of being a bare `False`.** Telemetry showed hgulk6's `update` at ~87% while it passes normally in ~24s (max 32s), so its failures aren't near-cap timeouts — they're hard stalls where a fresh, healthy pod comes up but the updated marker never routes to the ingress within the 180s cap. The check now classifies WHY without ever flipping the verdict (a genuine provider defect must stay visible — the same principle as the v1.17.0 exec fix). On an update timeout it records, into the run log **and** telemetry (`diag` field): `body_at_timeout` (what the ingress served: new/old/none/unreachable), `service_at_timeout` (lease service ready/total), **`in_pod_marker`** (best-effort exec of `printenv SMOKE_MARKER` — the one signal that splits *ingress routing lag* [pod has the new env] from *stale update* [pod still on the old env]), and a bounded **post-cap observation window** (`SMOKE_POST_CAP_OBSERVE_S`, default 90s, paid only on an already-failing run) yielding `eventual` (arrived/never) + `eventual_after_s` + `fail_cap_s`. Read it as: `eventual=arrived` → SLOW (widen the cap); `eventual=never` + `in_pod_marker=new` → ingress routing STUCK; `+ in_pod_marker=old` → the update never reached the pod. This makes cap-widening data-driven instead of blind — the next hgulk6 stall will say exactly which it is. (Design reached by a full 3-model quorum, 2 unanimous rounds.)
- Tests: 968 passing (+18) — body/in-pod/observe classifiers, the timeout recorder populates `diag` without changing the FAIL, a command-failure sets `fail_mode`, and telemetry carries `diag` only on a real failure.

---

## [1.17.0] — 2026-07-15

### Fixed
- **Exec cold-stdout race (issue #12) — fixed at the root, in the transport layer.** The provider-proxy does not guarantee the result (exit-code) frame is the last one on the wire: a stdout frame can still be in flight when the result arrives. `_pump_frames` returned the instant the exit code landed, so that trailing stdout was **dropped** — a successful exec came back `rc=0` with **empty stdout** ~5% of the time on some providers (aaul, hgulk6 in the accrued telemetry; z9nr clean). This hit **every** `exec()`/`inject()` caller, not just the smoke test's `_check_exec`. The fix keeps draining after the exit code is in hand for a short bounded window, returning early the instant the socket closes (the normal terminator), so a well-behaved command is never delayed and the trailing frame is never lost.
  - New `TransportConfig.result_grace_s` (default **0.25s**) bounds the **total** post-result drain (a monotonic deadline, not just per-recv silence, so a proxy that keeps dribbling frames can't stretch it) — chosen because the drain returns on close, so a longer window would only delay diagnosis in the pathological no-close case for zero normal-case benefit. Tunable if telemetry ever shows later frames.
  - **`flaky-pass` marker:** when the race actually fires and is caught, a one-line note goes to **stderr** (`[lease-shell] flaky-pass: drained N byte(s) … issue #12 cold-stdout race caught`) — it crosses the subprocess boundary the smoke test runs exec across, so the underlying race rate stays observable even though the symptom is gone.
  - Rejected the alternative of retrying inside `_check_exec`: that drives the *test* pass-rate to ~p³ while real users keep hitting the raw ~5% rate — masking, not fixing. (Design reached by full multi-model quorum consensus — transport-layer drain over a smoke-test retry.)
- Tests: 950 passing (+7) — trailing stdout after the result frame is emitted, the flaky-pass marker reports recovered bytes, normal ordering emits no false marker, a silent grace window after the result returns cleanly, silence *before* the result still raises the hang diagnosis, the drain switches to `result_grace_s`, and a dribbling proxy is cut off by the total grace budget.

---

## [1.16.0] — 2026-07-15

### Added
- **Auto-capture diagnostics on failure** — when a provider fails (lease never becomes ready, a feature FAILs, sshd never comes up, or no ingress URI), the run now automatically dumps the provider's lease status + **kube events** + container logs (readable since v1.11.1), so an *intermittent* problem self-documents in the run log instead of needing a live catch. The kube events are the payoff — they say WHY a pod didn't come up (`FailedScheduling`, `Insufficient cpu/memory`, `ImagePullBackOff`, `OOMKilled`, …). Captured at most once per provider (a readiness failure cascades to every feature, so one dump suffices) and best-effort (never raises, bounded by each stream's `--duration`). This turns the every-3h accumulation into a **self-diagnosing** monitor: the next occurrence of hgulk6's intermittent "lease never ready" arrives with its root-cause events attached. Validated live — a bad-image probe surfaced `Failed to pull image … not found → ErrImagePull → ImagePullBackOff`.
- Tests: 79 smoke tests (+4) — the capture dumps status/events/logs, never raises on a stream error, fires on a readiness failure, and captures at most once across multiple feature fails.

---

## [1.15.0] — 2026-07-15

### Added
- **Preflight guards so low credit or a full provider can't score a FALSE failure.** Two checks before/around the deploy:
  - **Room (proactive):** before deploying, the provider's published capacity (`get_provider().stats` — available cpu/memory/storage + `isOnline`) is checked against the probe's needs. A provider that's offline or too full is skipped as **NO-ROOM** — no wasted deploy + bid-wait, and not a failure. Fails **open**: if capacity can't be read, it proceeds and lets the bid decide, so a stats hiccup never skips a healthy provider.
  - **Credit (authoritative):** a deploy that returns HTTP **402** (insufficient Console credit — *nothing* is created on-chain, so it's free to probe) is surfaced as **NO-CREDIT**, and since that's account-wide the run stops and exits **clean (0)** as `SMOKE TEST SKIPPED`, rather than churning 402s and scoring every provider FAIL. (The Console API exposes no balance endpoint and the 402 is USD-credit-denominated, so the deploy response is the correct signal — not an on-chain AKT query.)
  - `NO-ROOM` / `NO-CREDIT` join `NO-BID` as "couldn't test" statuses (yellow, never counted as FAIL).
- Tests: 935 passing (+12) — capacity sufficiency, offline, fail-open on missing stats / registry miss / API error; 402→NO-CREDIT; NO-ROOM skips without deploying; skips never counted as failures. Room check validated live against all three providers.

---

## [1.14.0] — 2026-07-14

### Added
- **Latency SLO gate — "fail providers that are too slow", not just broken.** `analyze_telemetry` gains `--max-p95 "ready=45000,ingress=15000"`: with `--check`, a provider whose p95 for a feature exceeds the ceiling (over enough accrued runs) fails — distinct from a functional failure. It keys off the **p95 percentile over the accrued dataset** — a provider is "too slow" when it's *consistently* slow, not on one unlucky run — so noise can't trip it (NO-BID/`-` rows carry no latency and never enter the percentile). The reliability check is renamed `RELIABILITY breach`, and the two gates combine under one `--check`.
- **CI tracks *and* gates the metrics.** A new `report` job aggregates the accrued telemetry every run and prints the per-(provider, feature) percentile table in the workflow log, then runs the SLO gate. Kept **informational** (`continue-on-error`) during the accumulation window; once ~2-3 days of data has stabilized, set `SMOKE_LATENCY_SLO_P95` from the observed p99 + margin and drop `continue-on-error` to actually fail a too-slow provider.
- Tests: 923 passing (+12) — threshold parsing (incl. malformed input), latency-breach detection with the min-sample gate, and the combined `--check` (reliability + latency) exit codes. Demonstrated live: a provider with `ready` p95 40s fails a 30s ceiling while a 7s provider passes.

---

## [1.13.0] — 2026-07-14

### Added
- **Durable telemetry accrual + analysis** — the smoke telemetry (v1.12.0) now *accumulates* into a queryable dataset instead of scattering into per-run artifacts. A new isolated CI `accrue` job (`contents: write`, `needs: smoke`, `if: always()`) appends each run's JSONL to a dedicated long-lived **`telemetry` branch** — `main` is branch-protected so CI can't push to it, and keeping the data off `main` also keeps its history clean. So percentiles can be computed over weeks of runs, not a single day.
- **`analyze_telemetry`** (`uv run python -m just_akash.analyze_telemetry`, or `just smoke-telemetry-report`) aggregates the accrued data into per-(provider, feature) **success rate + p50/p95/p99 latency**, using the right tools for heavy-tailed latency (percentiles, and outlier-robust median ± k·MAD) rather than a Gaussian `avg+3σ`. It flags any feature whose p99 is creeping toward the configured cap, and — with `--check --min-samples N` — can gate on a success-rate SLO once enough data exists (the min-samples gate stops a small-sample blip from tripping). Example: ingress samples of 0.4s and 129s report p99≈128s and flag `p99>70%-of-cap`.
- Tests: 911 passing (+17) — percentile interpolation, robust median/MAD, aggregation (latency only from PASS samples), SLO min-sample gating, report flags, and JSONL parsing.

---

## [1.12.0] — 2026-07-14

### Added
- **Latency telemetry for the smoke test** — `--telemetry-file PATH` (or `SMOKE_TELEMETRY_FILE`) appends one JSON line per (provider, feature): `{ts, version, provider, feature, outcome, latency_ms, dseq}`, plus a `ready` row (time-to-serving). Pass/fail is the lagging binary; **latency is the leading signal**. The daily workflow now emits this and uploads it as a 90-day artifact — kept even when the run fails, since a red run's latencies are exactly what you want to inspect. This is the foundation for setting timeouts from observed **p99** and detecting regressions with robust stats (median ± k·MAD / success-rate SLO), rather than a fixed cliff or a Gaussian `avg+3σ` that does not fit heavy-tailed latency. One real run already shows why: `ingress` measured **0.4s on one provider and 129s on another** — a ~300× spread that only percentiles/robust limits handle correctly. Best-effort: a telemetry write failure never fails the run.
- Tests: 894 passing (+4) — record shape (incl. unreached-feature `None` latency), JSONL append + parent-dir creation, best-effort on an unwritable path, and end-to-end record emission from `smoke_provider`.

---

## [1.11.1] — 2026-07-14

### Fixed
- **`logs` and `events` now show provider output that was being silently discarded.** Providers that stream each frame as a JSON `ServiceLogMessage` / Kubernetes-event object (plain text — not the base64 that `exec` uses) had every line dropped as "undecodable (non-base64)", so `just-akash logs`/`events` printed nothing useful against them. Worse, the smoke test's `logs`/`events` checks still PASSED (they only verified the stream exited cleanly), masking the blind stream. The logs/events path now falls back to surfacing the raw text for the existing log/event formatter to render — real kube events (`Scheduled`/`Pulled`/`Created`/`Started`/`ScalingReplicaSet`) and `[service] message` log lines. Scoped strictly to logs/events: **`exec` still rejects a non-base64 frame** (its stdout is genuinely base64 binary, and surfacing a corrupt frame as text would corrupt output). The smoke `logs`/`events` checks now also require readable output, so a blind stream can no longer read as PASS. Validated live against a provider that streams JSON frames.
- Tests: 890 passing (+6) — the text fallback, base64-still-wins, `exec` still discarding non-base64, end-to-end stream surfacing of the exact JSON shapes captured from a live provider, and the stricter smoke content check.

---

## [1.11.0] — 2026-07-14

### Fixed
- **Smoke test no longer false-FAILs on provider readiness lag** (the "flaky provider" mystery). Investigating intermittent per-provider failures showed the cause was *our own impatience against fixed timeouts*, not broken providers: the failing provider hopped between runs and every failure was a readiness/timing check. Root causes, all fixed:
  - **Gate on real availability, not lease `status: ready`.** The lease flips to `ready` the moment a manifest is accepted — long before the container serves — so downstream checks ran against a not-yet-serving service. `_wait_ready` now gates on the service's reported availability (`ready_replicas`/`available` ≥ 1), with a working lease-shell exec as a fallback for providers that don't populate it, and **fails fast** on a terminal deployment state (closed / out of escrow) instead of burning the whole cap.
  - **Generous, env-tunable caps** replace short fixed poll counts: `SMOKE_READY_CAP_S` (default 240s) and `SMOKE_INGRESS_CAP_S` (default 180s). These are *ceilings* — a healthy provider still returns in seconds. Proven live: a provider whose ingress route took **129s** to propagate — 9s past the old 120s cap — now PASSES instead of failing ingress and cascading to update.
  - **The probe brings up its HTTP server before the openssh install**, so ingress readiness is decoupled from (and no longer inflated by) the slower `apk add openssh`.
  - Every readiness/ingress/update check now logs how long it actually took (`service available after Ns`, `ingress reachable after Ns`) — the first step toward latency telemetry and data-driven (percentile) timeouts.
- Tests: 884 passing (+16) — availability parsing (incl. malformed responses), terminal-state fail-fast, the exec fallback, cap exhaustion, and the ingress cap; also fixed a test-only busy-spin the new time-based loops introduced.

---

## [1.10.0] — 2026-07-13

### Added
- **In-job leak safety net for the daily smoke workflow** — the CI job could still leak an Akash deployment on a hard-kill: its `timeout-minutes` was on the **job**, so a job timeout cancelled everything and no cleanup could run, leaving a live probe until the next day's startup sweep (~24h escrow drain). Now the timeout is on the **smoke step**, and an `if: always()` **"Reap any leaked probe"** step runs after it — even on failure or cancellation — so a probe left behind (step timeout, crash, or a kill after create-on-chain but before the dseq was recorded) is destroyed **in the same run, within seconds** instead of ~24h. Only a runner-infra death (rare) still falls through to the daily startup sweep.
- **`--min-age SECONDS` on `smoke-providers`** (default 3600) — lets the end-of-job cleanup pass `--min-age 0` to reap *this* run's own fresh probe, which the 1h age floor (there to spare a concurrent run's live probe) would otherwise skip. Safe because the workflow's `concurrency` serializes runs, so no other run is ever in flight. Non-negative/finite-validated; still reaps only service-`probe` deployments, never real workloads (validated live against an account holding `train` + `runner`).
- Tests: 45 sweep tests (+1) covering the `--min-age 0` fresh-probe path (and confirming a fresh `runner` is still never reaped).

---

## [1.9.1] — 2026-07-13

### Fixed
- **`_ingress_uri` no longer crashes on a malformed lease `status`** — like the sweep's `_deployment_service_names` hardened in 1.9.0, the ingress-URI resolver read `(lease.get("status") or {}).get("services")`, which raises `AttributeError` when a provider returns a non-dict `status` (a bare string/list from a partial or malformed response). The status hop is now `isinstance`-guarded and treats anything unexpected as "no ingress yet". Impact was bounded (the smoke test's `run_check` wrapper caught it as an ingress FAIL rather than aborting the run), but it is now a clean skip. Regression test covers string/list/`services`-not-a-dict shapes.

---

## [1.9.0] — 2026-07-13

### Added
- **Self-healing orphan-probe sweep for the smoke test** — every `smoke-providers` run now sweeps first and reaps any probe that a *hard-killed* earlier run leaked (a CI job hitting `timeout-minutes` → SIGKILL, or a runner crash, can die after creating a probe lease but before its `finally`/signal-handler cleanup runs; nothing else reaps it, so it drains escrow for days until the chain closes it). Identification is surgical and fail-safe: a deployment is reaped **only** when its sole lease service is named `probe` (the name real workloads like `runner`/`train` never use) **and** it is older than an age floor derived from its ms-epoch dseq — so a probe a *concurrent* run is still holding, and every real workload, is left untouched. Runs at the start of each daily job (making it self-healing), or standalone via `--sweep-only` (`--dry-run` to report without destroying). Validated live: the sweep correctly flags zero orphans against an account holding `train` + `runner` workloads.
- Tests: full suite at 868 passing (up from 848) — 20 new tests pinning the sweep's service-name identification (including malformed provider responses), dseq-based age gate, and fail-safe classification (young probe spared, unknown-age spared, real workloads never reaped, dry-run destroys nothing and says so, an un-destroyable orphan is surfaced not hidden, a genuine inspection failure marks the sweep incomplete while a precise 404 match counts as already-gone, best-effort on API failure).

---

## [1.8.0] — 2026-07-12

### Added
- **Provider capability smoke test** — `just smoke-providers` (`python -m just_akash.smoke_providers`, PR #29). Deploys a throwaway probe to each configured provider and exercises every provider-facing feature — deploy, status, exec, inject, logs, events, SSH transport (exec/inject over `--transport ssh`), interactive `connect` (over SSH), HTTP ingress reachability, and in-place `update` — then destroys it and prints a provider × feature pass/fail matrix, exiting non-zero if any provider fails any feature. Catches a provider that bids and runs containers fine but has a broken shell/logs/exec/ingress path (the class of outage a normal rental never exercises). Defaults to the preferred tier (`AKASH_PROVIDERS`); `--all` adds the backup tier; `--provider` targets specific ones. Cleanup is guaranteed on Ctrl-C via the shared `robust_destroy` + signal handler.
- **`--service` for `exec` / `connect`** (PR #25): target a specific container on a multi-service deployment instead of silently exec-ing into whichever service the lease reports first (a warning is now logged when inference picks arbitrarily). Also stops conflating "lease not ready yet" with "ambiguous service" in the error path.
- **`--duration` for `logs` / `events`** (PR #24): a bounded, non-hanging snapshot — stream for N seconds then return cleanly, so a provider that keeps a non-follow logs/events connection open no longer blocks until the 300s recv timeout. Non-finite values (`nan`/`inf`) are rejected so the bound can't be silently disabled.

### Fixed
- **Interactive `connect` over lease-shell now works** (PRs #30, #31) — three client-side bugs kept the interactive shell from functioning, all fixed and verified end-to-end against a live provider:
  1. the shell request carried **no command**, which the provider rejects outright — it now execs an interactive `/bin/sh -i`;
  2. the `tty`/`stdin` query params were sent as `"true"/"false"`, but the provider only honors `"1"/"0"`, so a PTY was never allocated (`tty` reported "not a tty") and stdin was never opened — now sent as `"1"/"0"`;
  3. every frame sent **after** the connect message (stdin keystrokes, resize, Ctrl-C) used a bare `{type,data,isBase64}` envelope that the proxy rejects with "url/providerAddress Required", so keystrokes never reached the shell — they now carry the full connect envelope (url + providerAddress + auth). The unused `exec-with-stdin` helpers were made consistent with the same fix.
- **Lease-shell `exec` / `logs` no longer hang on a provider-side error** (PR #27): the Console provider-proxy reports failures as `type: "websocket"` frames carrying an `error` key (not `type: "error"`), which were being swallowed — so a command the provider rejected blocked for the full 300s recv timeout instead of failing. Error frames are now surfaced with the provider's message (Zod-style field detail included), a strict base64 decode stops an undecodable frame from being dispatched as output, and the recv is bounded with a clear diagnosis. Configurable via `TransportConfig.recv_timeout`.
- **`create_jwt` requested an access level the Console API rejects** (PR #28): the no-provider JWT fallback sent `access: "full"` with a `scope`, which the API answers with a 400 on every call — so it could never mint a token. It now sends `access: "scoped"` per AEP-64. (Found while diagnosing the hang above.)
- **`exec` shredded quoted commands** (PR #26): the remote command was split on spaces, so any `sh -c "…"` wrapper (i.e. anything running more than one thing) was broken apart and failed with an unterminated-quoted-string error. It is now parsed with `shlex`.
- **e2e cleanup no longer misreports a successful `destroy` as a failure** (PR #27): the check looked for the word "closed" in `just destroy` output, but the CLI prints "destroyed" — so every successful destroy was scored a failure and two redundant destroy calls fired against an already-closed deployment. The matcher is now pinned to the CLI's actual output by a test that drives the real command.
- Tests: full suite at 848 passing (up from 779).

---

## [1.7.0] — 2026-06-22

### Added
- **Full lifecycle Console-API coverage** — five new commands close the gaps between deploy and teardown:
  - `update` — update a running deployment in place via `PUT /v1/deployments/{dseq}`. Reuses the same SDL preparation as `deploy` (validation, `--image`, `--env`, SSH-key injection) but keeps the DSEQ and existing lease; no re-bid. CLI: `just-akash update --dseq <d> --sdl <f>`; recipe: `just update SDL [dseq] [image]`.
  - `logs` — stream container logs from the provider via the Console provider-proxy (`--follow`, `--tail`, `--service`). CLI: `just-akash logs`; recipe: `just logs [dseq] [follow]`.
  - `events` — stream Kubernetes events for a lease to debug startup failures (image pull, OOM, scheduling). CLI: `just-akash events`; recipe: `just events [dseq]`.
  - `add-funds` — add USD to a deployment's escrow via `POST /v1/deposit-deployment` (minimum 0.5, confirmation prompt). CLI: `just-akash add-funds --deposit <usd>`; recipe: `just add-funds AMOUNT [dseq]`.
  - `auto-topup` — show or toggle automatic escrow top-up via `/v2/deployment-settings` (GET/POST/PATCH upsert). CLI: `just-akash auto-topup [--on|--off]`; recipe: `just auto-topup [dseq] [on|off]`.
- API client: `update_deployment`, `deposit_deployment`, `get_deployment_settings`, `create_deployment_settings`, `update_deployment_settings`, `set_auto_top_up` (upsert).
- Transport: `LeaseShellTransport.stream_logs` / `stream_events` reuse the provider-proxy plumbing; tolerant log/event message formatting (JSON `ServiceLogMessage` or raw text).
- Tests: 69 new unit tests across `test_api_extensions.py`, `test_lease_stream.py`, `test_cli_extensions.py`, `test_update_flow.py`.
- **Adversarial hardening** (`/nf:harden`, 6 iterations to convergence): fixed 9 edge-case bugs in the new lifecycle code (loose 404 detection, dropped/`0` log+event messages, blank-line streaming, image-override hijacking a comment, non-bool auto-topup display, `{"data": null}` wrapper leak breaking first-time auto-topup, non-finite `add-funds` deposit) — see `harden iteration` commits.
- **Security tooling**: ruff bandit rules (`S`), a Semgrep SAST scan (`just semgrep`), and a pip-audit dependency CVE check (`just audit`), all wired into CI (`.github/workflows/security.yml`, weekly schedule for CVEs). See `SECURITY.md`.
- `deploy --gpu` now prefers a sibling `<name>-gpu<ext>` SDL variant when it exists (e.g. `app.yaml` → `app-gpu.yaml`), falling back to the named file with a warning otherwise (PR #22).
- Tests: full suite at 779 passing (up from 668), including the new lifecycle, transport-robustness, and re-deploy coverage below.

### Changed
- `create_jwt` / `create_jwt_with_provider` accept a `scope` parameter (defaults to `["shell"]`) so the same JWT path serves `shell`, `logs`, and `events`.
- SDL preparation (read → validate → image/SSH/env overrides) extracted into `deploy._prepare_sdl_content`, shared by `deploy()` and `update()`.

### Fixed
- **Order re-creation when the whole bid pool is stale** (PR #20): if every open bid expires before a lease can be created and there is no other open bid to retry, the stale order is now closed and a fresh deployment is created **once**, then re-selected (preferred bids instantly, backup bids after a short courtesy window — `JUST_AKASH_REDEPLOY_*` env config) instead of failing the deploy outright. The close-then-recreate is guarded against double escrow: a failed close is retried 3×, and if it still fails the deploy aborts with the manual-cleanup command rather than risk a second funded order.
- **Transient JWT-flap on lease creation** (PR #17, fixes #18): a Console `400 "JWT has invalid claims"` is transient, so lease creation now retries the **same** bid (distinct from the stale-bid "no longer open" retry, which advances to the next bid) before failing.
- **Log/event stream resilience** (PR #22): `logs --follow` reconnects with a fresh JWT on auth-expiry mid-stream (mirroring the interactive shell) and fails loudly after exhausting reconnect attempts instead of stopping silently; the provider-proxy recv-loop tolerates non-object JSON and malformed base64 frames instead of crashing.
- **`--env` validation** (PR #22): `deploy` / `update` reject malformed `--env` entries (missing `=`, or an empty key like `=value`) up front instead of emitting a broken SDL.
- **`inject` permission hardening fails closed** (PR #22): the SSH fallback now errors if `chmod 600` on the secret file fails, rather than reporting success with weaker-than-intended permissions.

### Security
- `inject` SSH-fallback path now `shlex.quote`s the user-supplied `--remote-path` before it reaches the remote shell, matching the lease-shell transport (prevents remote-shell metacharacter interpretation).
- `inject` SSH fallback also quotes the `$(dirname …)` command substitution so a remote path containing spaces cannot split into multiple `mkdir` arguments (PR #22).
- `SECURITY.md` documents the lease-shell `inject` base64-argv exposure window — the encoded secret is briefly visible in the **provider host's** process table while `base64 -d` runs; use trusted/audited providers for sensitive secrets (PR #22).
- Corrected `deploy --deposit` help and log line: deposits are denominated in **USD**, not AKT (verified against the Console API source).

---

## [1.6.1] — 2026-06-10

### Fixed
- **Stale-bid selection** (issue #14, PR #15): the 3-phase bid selection never checked bid *state*, so phase-3 backup fallback — which by construction fires after the phase-1+2 grace (~10 min), past the ~5-min bid TTL — always selected an expired bid and died on `POST /v1/leases` HTTP 400 "The selected bid is no longer open". Selection predicates now skip non-open bids (and log how many were skipped); bids with no `state` field are still treated as open for older API shapes.
- **Phase-2 grace cap**: while open BACKUP bids are available, the preferred-grace wait is cut at `JUST_AKASH_BACKUP_FALLBACK_S` (default 240s) so the fallback can lease backup bids *before they expire*. Full grace preserved when there is nothing to fall back to.
- **Lease stale-bid retry**: a 400 "no longer open" on lease creation triggers a bid re-fetch and retry with the next cheapest open bid (tier order preserved, failed providers excluded, max 3 attempts) before cleanup-and-raise. Non-stale lease errors keep the original fail-fast behavior.
- Tests: 14 new (`tests/test_stale_bid_selection.py`); full suite at 668 passing.

---

## [1.6.0] — 2026-05-10

### Added
- **Tiered provider selection** (issue #11): new `AKASH_PROVIDERS_BACKUP` env var and `--provider` / `--backup-provider` CLI flags. Three-phase bid-selection state machine — preferred-only patience → preferred-grace (first-wins) → backup fallback. Cheapest preferred wins when healthy; bounded `T1+T2` patience for slow preferred; cheapest backup wins when preferred fully unresponsive. Each bid tagged `[PREFERRED]` / `[BACKUP]` / `[FOREIGN]` in logs; selection log line names which phase chose the winner.
- `.env.example` ships with 3 vetted preferred providers + 10 backup providers — `cp .env.example .env` gets tiered selection out of the box.
- Tier-aware provider assertion in all three e2e tests: verifies the selected provider is in `AKASH_PROVIDERS ∪ AKASH_PROVIDERS_BACKUP`.
- `just_akash/_e2e.py` shared cleanup module: `robust_destroy()` with retry + audit, SIGINT/SIGTERM-safe `install_signal_cleanup()`, tier resolution, provider classification.
- Tests: 109 new (39 deploy state-machine + 70 cleanup helpers + e2e wiring); full suite at 653 tests, `just_akash/deploy.py` and `just_akash/_e2e.py` both at 100% line coverage.

### Changed
- **BME migration**: bid-price denom defaults from `uakt` (legacy) to `uact`. Bid responses pass through whatever denom they carry; only display fallbacks for malformed bids changed.
- SDL pricing ceiling raised from 1000 → 10000 uact (more provider headroom; cheapest-wins still applies).
- README: env-var table documents `AKASH_PROVIDERS_BACKUP`; new "Tiered providers" section with state-machine table.
- All three e2e tests now wrap post-deploy work in `try/finally` (was missing in `test_lifecycle.py` and `test_secrets_e2e.py`); `robust_destroy` retries up to 3× and audits via `just list`.

### Fixed
- **Cleanup leak: substring DSEQ collision** in audit — `dseq="123"` falsely flagged a different deployment `"12345"` as lingering. Fixed via word-boundary regex (`_dseq_in_list_output`).
- **Cleanup leak: `retries < 0` silently skipped destroy** but returned True from audit — caller saw "success", deployment lived on. Fixed via `retries = max(retries, 0)`.
- **Cleanup leak: double `install_signal_cleanup` orphaned the first dseq_ref** — second call replaced the SIGINT handler, first deployment leaked on signal. Fixed via module-level `_REGISTERED_DSEQ_REFS` registry; signal handler iterates all registered refs; `signal.signal()` invoked exactly once.
- **Cleanup leak: signal-handler reentrancy** — impatient double-Ctrl-C re-iterated the registry, multiplying destroy calls per ref. Fixed via `_HANDLER_RUNNING` guard with try/finally.
- `_log_bid_table()` now safely handles non-dict bid entries when tier-tagging.

### Acknowledgements
Hardened against 4 real cleanup-leak bugs surfaced by the adversarial /nf:harden loop.

## [1.2.0] — 2026-04-12

### Added
- `--json` flag on `list`, `status`, `close`, `close-all` commands for explicit JSON output (also auto-enables when stdout is not a TTY)
- `format_deployments_json()` for machine-readable deployment listing
- `_confirm()` helper to DRY confirmation prompts across `cli.py` and `api.py`
- `pyright` type checking in dev dependencies, CI workflow, and `just typecheck` recipe
- 20 new tests: interactive picker (arrow keys, q/ctrl-c, tags+SSH), `_confirm`, `format_deployments_json`, `get_provider` response shapes
- `just typecheck` Justfile recipe

### Changed
- Confirmation prompts now use shared `_confirm()` instead of duplicated `input()` logic
- `use_json` detection unified: `args.json or not sys.stdout.isatty()`
- Fixed 15 pyright type errors (assertions on `_extract_dseq()` `str|None` returns)

### Fixed
- All pre-existing lint issues in test files (unused imports, unsorted imports)

## [1.1.0] — 2026-04-12

### Changed
- Restructured from `scripts/` flat files to a proper `just_akash/` Python package
- All CLI invocations now use `uv run just-akash` instead of `python3 scripts/...`
- Justfile recipes updated to use the new package entry point

### Added
- `-y` / `--yes` flag on `close` and `close-all` commands to skip confirmation prompts (non-interactive mode)
- Lint recipes: `just lint`, `just fmt`, `just check`
- Secret scanning recipe: `just secrets`
- Pre-commit config (gitleaks + ruff)
- GitHub Actions CI (gitleaks, trufflehog, detect-secrets, ruff, pytest)
- Community files: LICENSE, CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md, issue/PR templates

### Fixed
- Provider registry lookup (`get_provider`) crashed silently when `/v1/providers` returned a bare list instead of a wrapped dict — now handles both response shapes correctly

## [1.0.0] — 2026-04-11

### Added
- Deploy SSH-enabled instances on Akash Network via Console API
- Two-phase bid polling: configurable `--bid-wait` (default 60s) and `--bid-wait-retry` (default 120s)
- Cheapest bid selection with allowlist filtering
- Provider diagnostics when allowed providers don't bid (on-chain status, uptime, capacity)
- SSH connectivity with auto-detected key path
- Interactive deployment picker (arrow keys) for multi-deployment environments
- Deployment tagging (DSEQ → human-readable name)
- `just` recipes for all lifecycle operations (up, connect, down, down-all, tag, ls, status, test)
- `just-akash` CLI with subcommands: `deploy`, `api`, `test`
- Timestamped log files in `.logs/just/` with start/end metadata and exit codes
- Full lifecycle integration test (up → verify → SSH → down → cleanup)
- gitleaks secret scanning with CI workflow
- TruffleHog secret scanning with CI workflow
- detect-secrets baseline scanning with CI workflow
- MIT License (Jonathan Borduas)
- Contributing guide, Code of Conduct, Security policy
- GitHub issue templates (bug report, feature request) and PR template
