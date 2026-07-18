# Module Reference

Per-module purpose, public surface, and the invariants worth knowing. Ordered by
the call path. Referenced by symbol name (line numbers drift).

## `cli.py` — CLI dispatch

`main()` is the `just-akash` console script (`pyproject.toml [project.scripts]`).
Argparse subcommands for ~18 operations; each branch is a thin dispatcher into
`deploy`, `api`, or `transport`.

- **`_resolve_deployment(client, dseq_arg)`** — tag → dseq resolution, else
  auto/single/interactive pick.
- **`_enrich_deployment_with_provider(client, deployment)`** — backfills
  `lease.provider.hostUri` from the provider registry so the lease-shell transport
  can find it.
- **`_make_lease_shell(client, dseq)`** — builds + validates a lease-shell transport
  for `logs`/`events` (no SSH equivalent); exits 1 with guidance if no active lease.

Invariant: lease-shell is the default transport; SSH is the fallback when
`validate()` is False. `benchmark`, `logs`, `events` are lease-shell only.

## `api.py` — Console API client

`AkashConsoleAPI(api_key, base_url=None)` — stateless REST client over `urllib`.
`base_url` defaults to `AKASH_CONSOLE_URL` env then `console-api.akash.network`.

- CRUD: `create_deployment`, `update_deployment`, `get_deployment`,
  `list_deployments`, `close_deployment`, `close_all_deployments`.
- Bids/lease: `get_bids`, `create_lease`.
- Escrow: `deposit_deployment`, `get_deployment_settings`, `create_/update_deployment_settings`,
  `set_auto_top_up` (upsert).
- JWT: `create_jwt` (scoped, owner-wide), `create_jwt_with_provider` (granular,
  provider-scoped). Both honor AEP-64.
- Extractors: `_extract_dseq`, `_extract_provider`, `_extract_bid_price`,
  `_extract_ssh_info`, `_extract_forwarded_ports`, `_extract_lease_provider` — each
  tolerates flat and nested Console shapes.
- `_unwrap_data` — pulls the dict from `{"data": …}` envelopes; `{"data": null}` → `{}`.

Invariant: **no field from the API is trusted to be the expected type** — every read
is `isinstance`-guarded. `TAGS_FILE` (`.tags.json`) is written atomically.

> Note: `api.py` also ships a legacy `api_main()` argparse CLI duplicating `cli.py`.
> Prefer `cli.py`; the legacy entry is a candidate for removal (see `ASSESSMENT.md`).

## `deploy.py` — deployment orchestrator

`deploy(...)` — the 6-step lifecycle; step 3 is the 3-phase tiered bid state machine
(see `ARCHITECTURE.md`). `update(...)` — in-place PUT, same SDL prep, no re-bid.

- `_resolve_tier(arg_value, env_name)` — CLI overrides env; empty-list override is
  *explicit* (does not fall back to env); whitespace-only entries dropped.
- `_classify_bid(provider, preferred, backup)` → `PREFERRED`/`BACKUP`/`FOREIGN`/`ACCEPTED`.
- `_is_open_bid` — state filter (issue #14); leasing a non-open bid is a guaranteed 400.
- `_backup_fallback_grace_s` — bounds phase 2 so it can't age backup bids past ~5min expiry.
- `_redeploy_and_reselect` — the one bounded re-deploy round (issue #19).

Invariant: failure paths clean up the deployment they created (no funded orphans);
cleanup failures are logged, not raised over the original error.

## `transport/base.py`, `transport/__init__.py`

`TransportConfig` dataclass (`dseq`, `api_key`, `deployment`, `console_url`,
`provider_proxy_url`, `service_name`, `recv_timeout=300`, `result_grace_s=0.25`).
`Transport` ABC: `prepare / exec / inject / connect / validate`.
`make_transport(name, **kw)` — factory.

> `console_url` and `provider_proxy_url` default to the **real** Console/proxy
> (hardcoded, not env-overridable). The local fake suite injects stub URLs at this
> seam (see `TESTING.md`).

## `transport/lease_shell.py` — WebSocket transport

`LeaseShellTransport(config)` — the default transport. Connects to the Console
provider-proxy (`wss://…/provider-proxy-mainnet`), which relays to the provider.

- `exec(command)` — argv path (`cmd0`,`cmd1`,…).
- `exec_shell_script(script)` — `sh -c` path (required for `$()`/pipes/`;`; the
  benchmark depends on it).
- `inject(remote_path, content)` — `mkdir` + `umask 077; echo <b64> | base64 -d > path`
  + `chmod 600`. **Security caveat:** the base64 rides the proxy-logged URL (#39).
- `connect()` — interactive raw-TTY session (POSIX only).
- `stream_logs` / `stream_events` — bounded streaming with auth-expiry reconnect.
- `_pump_frames`, `_dispatch_frame`, `_recv_proxy_message`, `_decode_payload` — the
  frame protocol (see `ARCHITECTURE.md`).

Invariant: **rc=0 is not trusted** — a malformed result frame and a clean close
before any result both raise (see `docs/exec-reliability-investigation.md`).

## `transport/ssh.py` — SSH fallback

`SSHTransport` wraps `api._build_ssh_cmd` / `_extract_ssh_info` / `_find_ssh_key`.
`inject` does `mkdir -p` + `cat > path` + `chmod 600` (fail-closed if chmod fails).

## `smoke_providers.py` — provider capability smoke

`main()` / `smoke_provider()` — the daily matrix. Per-feature checks (`_check_exec`,
`_check_inject`, `_check_stream`, `_check_ssh`, `_check_connect`, `_check_ingress`,
`_check_update`); reliability classification (`_is_reliability_failure`,
`_mass_lease_down`, `_gating_providers`); quarantining (`_quarantined_providers`).

### Gating model (the subtle part)

- **`_FAILING_OUTCOMES = ("FAIL", LEASE_DOWN)`**, but LEASE-DOWN is a fleet signal,
  gated only by the mass-lease-down safety valve.
- **Reliability vs tooling:** a proven provider-infra failure (LEASE-DOWN, or an
  update-cutover stall *proven* to be the new pod) is demoted to non-gating; a
  tooling regression on a healthy lease still gates.
- **Retry-on-empty** (`_INJECT_READBACK_ATTEMPTS`, default 3) retries **only** the
  `rc=0 + empty stdout` race signature; a wrong content or nonzero rc fails on first read.
- **Marker-echo:** every check requires a token echoed in stdout, never `rc==0` alone.
- **Telemetry:** one JSONL row per feature (`ts`, `version`, `provider`, `feature`,
  `outcome`, `latency_ms`, `dseq`, optional `frame_shape`/`diag`); benchmark rows go
  to a separate `SMOKE_BENCHMARK_FILE`.

## `analyze_telemetry.py` — telemetry grading

`aggregate()` → per-(provider,feature) `count`/`pass`/`fail`/`lease_down` + PASS-only
latency percentiles (p50/p95/p99). LEASE-DOWN is excluded from the pass/fail denominator.
`slo_breaches` is **informational** (the accrued view lacks diag context);
`latency_breaches` is **the only gate**. `parse_thresholds` is strict (rejects
non-numeric / ≤0 — a bad ceiling would silently disable the gate).

## `benchmark.py` — hardware honesty probe

`BENCH_SH` (POSIX sh, bounded under 1 vCPU/1Gi) → `parse_results` (`BENCH-k=v`);
`resource_fidelity` (throttle/steal/PSI → `UNDER-DELIVERING`); `stability` (CV across
samples → `UNSTABLE`); `build_json_record` (trusted fields spread **last** so a hostile
probe can't shadow `dseq`/`provider`/`complete`); `is_complete` (`BENCH-done=1`).

## `sdl_validate.py` — SDL rules

`validate_sdl(sdl_text)` → `yaml.safe_load` + `_check_signed_by`: every
`signedBy.anyOf/allOf` address must equal the audit authority, else the on-chain audit
constraint is silently a no-op. Raises `SDLValidationError`.

## `_e2e.py` — shared live-e2e helpers

`robust_destroy` (retry + audit, reads per-deployment status not the stale list),
`install_signal_cleanup` (SIGINT/SIGTERM → destroy registered dseqs),
`resolve_tiers` / `assert_provider_in_tiers`. 100% unit-covered by
`tests/test_e2e_cleanup.py`.
