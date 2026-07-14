# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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
