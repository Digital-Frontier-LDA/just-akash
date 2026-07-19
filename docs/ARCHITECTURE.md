# Architecture

How `just-akash` is put together: the layers, the deploy state machine, and the
transport abstraction. Read alongside `PROTOCOL.md` (the wire format) and
`MODULE_REFERENCE.md` (per-module detail).

## Layers

```text
                     just <recipe>            (Justfile — logging wrapper)
                         │
                  just-akash <cmd>           (cli.main — argparse dispatch)
                         │
        ┌────────────────┼────────────────────────┐
        │                │                        │
   deploy.deploy    AkashConsoleAPI          transport.*
   (orchestration)   (urllib HTTP)        lease-shell / ssh
        │                │                        │
        │                ▼                        │
        │        console-api.akash.network        │
        │           (REST + JWT)                  │
        │                                         ▼
        └──── bids/lease ────► provider-proxy (WSS) ──► provider
```

Two surfaces sit on top of one Python package:

- **`Justfile`** — every recipe is a logging wrapper (timestamped log to
  `.logs/just/`, start/exit metadata, exit code) around a `uv run just-akash` call.
  It adds no logic; it adds observability and the `tag`/`up` conveniences.
- **`just-akash`** (`just_akash/cli.py`) — the real CLI. Argparse subcommands
  dispatch into `deploy`, `api`, or `transport`.

## The deploy state machine (`deploy.py`)

The core of the tool. A deployment is a 6-step orchestration; step 3 is a
three-phase tiered bid-selection state machine.

```text
1. prepare SDL     read → validate (sdl_validate) → image/SSH-key/env overrides
2. create deploy   POST /v1/deployments (recovers from "already exists")
3. select bid      ┌─ Phase 1: preferred-only patience  [0, T1]  (default 60s)
                   │  collect all bids; at T1 pick cheapest PREFERRED if any
                   ├─ Phase 2: preferred-grace         [T1, T1+T2] (default 120s)
                   │  the instant a PREFERRED bid appears, accept it (first-wins)
                   │  cut short once open BACKUP bids exist + grace nears 5min
                   └─ Phase 3: backup fallback
                      cheapest BACKUP from bids collected in phases 1+2
4. tier tables     log PREFERRED / BACKUP / FOREIGN breakdown
5. announce        which phase chose the winner + a ranked tier view
6. create lease    POST /v1/leases — with stale-bid retry + one bounded re-deploy
```

Properties (each pinned by a test in `tests/test_deploy.py`):

- **Cheapest-when-healthy.** Preferred responsive → cheapest preferred wins.
- **Bounded patience.** Preferred slow → wait ≤ T1+T2, then snap to first preferred.
- **Graceful degradation.** Preferred fully down → cheapest backup, no extra round trip.
- **Zero regression.** No backup tier configured → behaves as the single-tier allowlist.

Tiers come from env (`AKASH_PROVIDERS`, `AKASH_PROVIDERS_BACKUP`) or repeatable CLI
flags (`--provider`, `--backup-provider`); CLI overrides env per-tier. With no
allowlist at all, the cheapest bid from any provider wins. See `README.md` § Bid
Selection.

### Failure/recovery paths

- **stale deployment** ("already exists") → close lease-less stale deployments, retry once.
- **stale bid at lease time** ("no longer open") → re-fetch open bids, tier order, next cheapest.
- **transient Console auth flap** ("JWT has invalid claims") → retry same provider after backoff.
- **all bids stale** → one bounded **re-deploy round**: close the order, re-create, lease a fresh bid immediately (no phased patience — that's what aged the first round). Issues #14/#18/#19.

## Transport abstraction (`transport/`)

```text
Transport (ABC)              base.py — prepare / exec / inject / connect / validate
  ├── SSHTransport           ssh.py   — wraps ssh subprocess (the fallback)
  └── LeaseShellTransport    lease_shell.py — WebSocket via Console provider-proxy
make_transport(name, **kw)   __init__.py — factory → TransportConfig
```

`connect`, `exec`, `inject` default to **lease-shell** (no SSH required) and fall
back to SSH when the deployment has no active lease / provider hostUri. `logs` and
`events` are lease-shell only (no SSH equivalent).

### Lease-shell data flow (exec)

```text
exec(cmd)
  └─ _exec_loop: fetch JWT → _get_proxy_ws_url (wss://) → connect
       └─ send proxy connect message  {url: /lease/dseq/gseq/oseq/shell?cmd0=.., auth:{jwt}}
       └─ _pump_frames:
            _recv_proxy_message  (JSON envelope → base64 → [code][payload])
            _dispatch_frame:  100→stdout  101→stderr  102→exit  103→raise
            after 102: keep draining result_grace_s (cold-stdout race, issue #12)
          on auth-expiry close: loop, re-fetch JWT, retry (≤ MAX_RECONNECT_ATTEMPTS)
```

The proxy envelope is JSON: `{"type":"websocket","message":"<base64 of frame>"}`.
The provider-proxy logs the connect URL, which is why injected secrets currently
ride it as base64 (issue #39 — see SECURITY.md).

### Frame protocol (codes)

| Code | Constant | Dir | Payload |
|---|---|---|---|
| 100 | stdout | server→client | raw bytes |
| 101 | stderr | server→client | raw bytes |
| 102 | result | server→client | `{"exit_code": N}` JSON or 4-byte LE int |
| 103 | failure | server→client | UTF-8 error → `RuntimeError` |
| 104 | stdin | client→server | raw bytes |
| 105 | resize | client→server | big-endian `>HH` rows, cols |

`rc=0` is **not** a trustworthy success signal — see
`docs/exec-reliability-investigation.md`. A malformed 102 and a clean close with no
102 both now raise rather than return 0.

## The smoke + telemetry loop (`smoke_providers.py`, `analyze_telemetry.py`)

Daily drift detector (`.github/workflows/provider-smoke.yml`). For each configured
provider: deploy a throwaway probe, exercise every provider-facing feature
(status/exec/inject/logs/events/ssh/connect/ingress/update), benchmark the hardware,
destroy, emit one JSONL telemetry row per feature.

- **Outcomes:** `PASS` / `FAIL` / `LEASE-DOWN` (+ non-failures `-`/`NO-BID`/`NO-ROOM`/`NO-CREDIT`).
- **Reliability vs tooling:** LEASE-DOWN (fleet-wide provider infra) and proven
  update-cutover stalls are demoted to non-gating; a tooling regression on a healthy
  lease still gates. A mass-lease-down safety valve re-gates when every leased
  provider goes down (likely a manifest bug, not the fleet).
- **Telemetry** accrues to the `telemetry` branch; `analyze_telemetry.py` grades it
  (pass-rate is informational; p95 latency is the only gate). See
  `docs/smoke-gating-model.md`-equivalent detail in `MODULE_REFERENCE.md`.

## Where to look next

| Want to… | Read |
|---|---|
| add a CLI command | `DEVELOPING.md` |
| add a transport | `DEVELOPING.md` |
| understand the wire format | `PROTOCOL.md` |
| write/run tests | `TESTING.md` |
| debug a failure | `TROUBLESHOOTING.md` |
| look up a module's API | `MODULE_REFERENCE.md` |
