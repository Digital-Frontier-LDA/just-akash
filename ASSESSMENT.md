# Deep Technical Assessment — just-akash v1.33.0

**Date:** 2026-07-18 · **Scope:** the v1.33.0 codebase as built (commit baseline) plus
the assessment changes in this branch (`docs-tests-deep-assessment`).

just-akash is a minimal-dependency Python CLI + `just` recipe layer for deploying and
operating workloads on [Akash Network](https://akash.network) via the Console API.
It is unusually mature for its size: a deliberate 3-phase tiered bid-selection
state machine, a no-SSH lease-shell transport, leak-proof teardown, and a daily
provider-capability smoke harness with accrued telemetry.

This assessment drove four changes on this branch: two correctness bug fixes,
targeted unit-test coverage, a local fake-Akash integration suite, and the
documentation set it anchors.

---

## 1. Headline metrics

| Metric | Value |
|---|---|
| Version | 1.33.0 |
| Source LOC | ~8.7k (`just_akash/`) |
| Runtime deps | `websockets`, `pexpect`, `pyyaml` (minimal-dependency ethos — add cautiously) |
| Python | ≥ 3.10 |
| Unit tests | ~1150 passing (1128 at baseline + this branch) |
| Honest unit coverage* | ~92% (was reported 80% — see §4) |
| Live e2e scripts | 3 (`test_lifecycle`, `test_secrets_e2e`, `test_shell_e2e`) — run in CI with secrets |
| CI jobs | lint, typecheck, unit, e2e-shell, e2e-secrets, daily provider-smoke, security, secrets |

\* Excluding the three live e2e scripts, which pytest never collects (see §4 /
`pyproject.toml [tool.coverage.run]`). The number drifts while the in-flight
`balance`/`chain.py` feature (not on this branch) lands uncovered.

## 2. Module health

| Module | LOC | Cov | Notes |
|---|---|---|---|
| `cli.py` | ~1000 | 86% | argparse dispatch for ~18 subcommands; the three command bodies fixed here (benchmark/inject/validate-sdl) went from ~0% to covered |
| `api.py` | ~1040 | 93% | `AkashConsoleAPI` over `urllib`; defensive `isinstance` for every Console shape; JWT minting (AEP-64); atomic tag store |
| `deploy.py` | ~1140 | 97% | **crown jewel** — 3-phase tiered bid selection + stale-bid/transient-auth/re-deploy recovery |
| `transport/lease_shell.py` | ~1240 | 90% | WebSocket provider-proxy relay; binary frame protocol (100–105); cold-stdout-race drain |
| `transport/ssh.py` | ~90 | 89% | SSH fallback transport |
| `smoke_providers.py` | ~1890 | 91% | daily provider capability matrix + reliability SLO + telemetry |
| `analyze_telemetry.py` | ~400 | 93% | SLO/latency grading of accrued telemetry |
| `benchmark.py` | ~350 | 100% | hardware-honesty probe parser (CPU/RAM/disk/WAN + throttle/steal) |
| `_e2e.py` | ~350 | 100% | shared `robust_destroy` + signal cleanup — exemplary |
| `sdl_validate.py` | ~85 | 96% | SDL rules incl. `signedBy` audit-authority pin |

## 3. Strengths

- **`deploy.py` is genuinely well-engineered.** The tiered selection (preferred-only
  patience → preferred-grace → backup fallback) is property-tested to ~20 cases
  including price-ties, simultaneous arrival, case-sensitivity, and CLI/env override
  semantics (`tests/test_deploy.py`).
- **Defense-in-depth everywhere.** Every Console-shape read is `isinstance`-guarded;
  tags write atomically (`tempfile` + `os.replace`); inject uses `umask 077` *and*
  explicit `chmod 600`; base64 decode is strict (`validate=True`); the proxy URL is
  rejected unless TLS.
- **Honest telemetry.** LEASE-DOWN is counted separately from pass/fail; the gate owns
  the reliability-vs-tooling distinction; a silently-disabled gate logs loudly.
- **`_e2e.py` at 100%.** The "no deployment leak" guarantee is unit-pinned with
  failure-mode scenarios, independent of the live scripts.
- **The CHANGELOG is candid** — it documents the project's own past bugs and reversions
  (e.g. v1.29.0's empty-file inject) in full.

## 4. Findings & remediation on this branch

### Correctness bugs fixed (`transport/lease_shell.py`)

Two exec-frame bugs both **false-passed as exit 0** — and rc=0 is not a trustworthy
success signal (see `docs/exec-reliability-investigation.md`):

1. **Malformed result frame → silent exit 0.** `_dispatch_frame` code 102 fell through
   to `return 0` when the payload was neither valid JSON nor a ≥4-byte int. Now raises.
2. **Clean close before any result → silent exit 0.** `_pump_frames` returned the
   caller's default (0) on `ConnectionClosedOK` with no result frame. Now raises.

Each has a regression test (`tests/test_lease_shell_exec.py`) and an end-to-end test
through the fake-Akash suite (`tests/test_integration_fake.py`).

> **Not fixed (deliberate):** `_exec_with_stdin` / `_exec_with_stdin_command` look like
> dead code but are the documented scaffolding for the open #39 secret-in-URL re-fix
> (CHANGELOG v1.29.0). They are test-covered and aspirational, not accidental — left
> in place.

### Coverage honesty

The three live e2e scripts (~540 statements) were counted in the coverage total but
never run by pytest, depressing the reported figure to 80%. `[tool.coverage.run] omit`
now excludes them, lifting the honest unit surface to ~92%.

### Targeted unit tests

- `cli.py` benchmark / inject / validate-sdl command bodies: ~0% → covered
  (`tests/test_cli_dispatch.py`, 13 tests — incl. the benchmark stdout-capture trick,
  `--env-file` parsing, and the SSH-fallback `shlex.quote` + `chmod 600` path).
- `_generate_keypair` cleanup-on-failure (security: a failed `ssh-keygen` must not
  leak the half-generated unencrypted key).
- Two `assert True` tautologies in `test_adversarial.py` replaced with real
  post-conditions (image override actually lands in the submitted SDL).

### Local fake-Akash integration suite

`tests/_fake_akash.py` + `tests/test_integration_fake.py`: a localhost Console HTTP
stub + provider-proxy WebSocket stub. The full CLI runs end-to-end in CI without
credentials — real `urllib` HTTP, real `websockets` frame protocol, the JWT fetch and
`_pump_frames` all unmodified. Closes the gap where every logs/events/benchmark test
previously mocked `_make_lease_shell`.

## 5. Security posture

- **Secret scanning:** gitleaks (pre-commit + CI + weekly history), TruffleHog (verified
  only), detect-secrets baseline — three layers.
- **SAST:** ruff bandit `S` rules (per-file-scoped for the modules that shell out by
  design), Semgrep (`p/python` + `p/security-audit`, two inherent-to-a-remote-exec-CLI
  rules excluded with documented mitigation).
- **Dependency CVEs:** pip-audit on every push/PR + weekly.
- **Known residual:** injected secrets ride the provider-proxy-logged URL as
  base64-obscured (trivially reversible) text — documented in `SECURITY.md` and
  `docs/exec-reliability-investigation.md`; the stdin-frame re-fix is open (#39).

## 6. Recommendations (prioritized, not all on this branch)

1. **Resolve #39** — make the lease-shell stdin frame actually deliver bytes, then switch
   inject off the URL-logged `echo|base64 -d` path. The scaffolding is already in place.
2. **Refresh the interactive `connect` JWT.** It's captured once; a session past the 1h
   TTL dies quietly. `_stream` already reconnects on auth-expiry — `connect` should too.
3. **Single-source the dead-state sets** — `_e2e._SETTLED_STATES` vs
   `smoke_providers._DEAD_STATES`/`_LEASE_DOWN_STATES` are "kept in sync" by comment only.
4. **Decide on `api.main()`** — `api.py` ships a second argparse CLI duplicating `cli.py`
  (~170 lines, largely uncovered). Delete or cover.
5. **Cover the streaming reconnect** — `stream_logs`/`stream_events` auth-expiry reconnect
   is the one reliability contract still untested (the exec reconnect IS covered).

---

*See `docs/` for the architecture, testing, module-reference, and troubleshooting
detail that backs this assessment.*
