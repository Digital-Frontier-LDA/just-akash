# just-akash

Justfile recipes + Python CLI for deploying on [Akash Network](https://akash.network) via the Console API.

Self-contained — clone, configure `.env`, and run.

> **Maintenance & ownership.** As of June 2026, `just-akash` is maintained by
> [Digital Frontier](https://github.com/Digital-Frontier-LDA) (MIT-licensed). It's part of our
> commitment to making Akash enterprise-ready — adding robustness to the deployment lifecycle and
> security to post-deploy operations (no-SSH lease-shell exec, off-SDL secret injection).

## Documentation

- [**ASSESSMENT.md**](ASSESSMENT.md) — deep technical assessment: module health, strengths, findings, recommendations.
- [**docs/ARCHITECTURE.md**](docs/ARCHITECTURE.md) — layers, the 3-phase bid-selection state machine, the transport abstraction.
- [**docs/MODULE_REFERENCE.md**](docs/MODULE_REFERENCE.md) — per-module purpose, public API, invariants.
- [**docs/DEVELOPING.md**](docs/DEVELOPING.md) — contributor setup, recipes, adding a command/transport, release flow.
- [**docs/TESTING.md**](docs/TESTING.md) — unit vs live-e2e vs the local fake-Akash integration suite.
- [**docs/TROUBLESHOOTING.md**](docs/TROUBLESHOOTING.md) — failure modes indexed by command.
- [**docs/PROTOCOL.md**](docs/PROTOCOL.md) — the lease-shell WebSocket frame protocol.
- [**docs/diagnostics.md**](docs/diagnostics.md) — structured diagnostic events (reason codes for Sentry/CI: wallet credit, provider health, bid/lease failures).
- [**docs/exec-reliability-investigation.md**](docs/exec-reliability-investigation.md) — root-cause of the `rc=0`+empty-stdout symptom.

## What's New

- **Full lifecycle API coverage** — five new commands round out the deploy→operate→maintain loop:
  - `update` — revise a running deployment in place (`PUT /v1/deployments/{dseq}`); keeps the DSEQ and lease, no re-bid.
  - `logs` — stream container logs from the provider (`--follow`, `--tail`, `--service`).
  - `events` — stream Kubernetes events to debug why a deployment won't start.
  - `add-funds` — top up a deployment's escrow in USD (`POST /v1/deposit-deployment`).
  - `auto-topup` — show or toggle automatic escrow top-up (`/v2/deployment-settings`).
- **Tiered provider selection** — preferred + backup allowlists with a 3-phase bid-selection state machine (`AKASH_PROVIDERS_BACKUP` env var, `--provider` / `--backup-provider` CLI flags). See [Bid Selection](#bid-selection).
- **BME migration** — bid-price denom defaults updated from `uakt` (legacy) to `uact`.
- **Hardened e2e cleanup** — `robust_destroy()` with retry + audit, SIGINT/SIGTERM-safe handler, no-leak guarantee on multi-deployment runs.
- **Extensive unit + e2e test suite**; `just_akash/deploy.py` and `just_akash/_e2e.py` at 100% line coverage.

## Prerequisites

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) (Python package runner)
- [`just`](https://github.com/casey/just) command runner (optional, but recommended)

## Setup

```bash
git clone https://github.com/Digital-Frontier-LDA/just-akash
cd just-akash
cp .env.example .env
# Edit .env — add your API key, providers, SSH pubkey
uv sync --dev           # install package + dev tools (ruff)
uv run pre-commit install   # install gitleaks + ruff hooks
```

## Usage

### With `just` (recommended)

| Command | Usage | Purpose |
|---|---|---|
| `just deploy [sdl] [image]` | `just deploy` | Deploy with custom SDL/image |
| `just up [tag]` | `just up my-web-app` | Deploy SSH instance + optional tag |
| `just update SDL [dseq] [image]` | `just update sdl/app.yaml akash-node` | Update a deployment in place (no re-bid, keeps DSEQ/lease) |
| `just connect [dseq] [transport]` | `just connect 12345 ssh` | Connect to a running instance (lease-shell default) |
| `just exec [dseq] "cmd" [transport]` | `just exec 12345 "ls -la"` | Execute a remote command |
| `just inject [dseq] [env-file] [transport]` | `just inject 12345 .env.secrets` | Inject secrets (lease-shell default) |
| `just logs [dseq] [follow]` | `just logs akash-node follow` | Stream container logs (provider-proxy) |
| `just events [dseq]` | `just events akash-node` | Stream Kubernetes events (debug startup) |
| `just add-funds AMOUNT [dseq]` | `just add-funds 5 akash-node` | Add USD to escrow (min 0.5) |
| `just auto-topup [dseq] [on\|off]` | `just auto-topup akash-node on` | Show / toggle auto escrow top-up |
| `just destroy [dseq]` | `just destroy 12345` | Destroy an instance |
| `just destroy-all` | `just destroy-all` | Destroy all instances |
| `just list` | `just list` | List active instances |
| `just status [dseq]` | `just status 12345` | Show instance details |
| `just tag [dseq] [name]` | `just tag 12345 my-db` | Tag a deployment with a name |
| `just test-shell` | `just test-shell` | E2E lease-shell transport test (deploy/exec/inject/cleanup) |
| `just test-secrets` | `just test-secrets` | E2E secrets injection test (SSH inject + lease-shell cross-check) |
| `just lint` | `just lint` | Ruff lint + format check (incl. bandit `S` security rules) |
| `just secrets` | `just secrets` | Gitleaks secret scan |
| `just semgrep` | `just semgrep` | Semgrep SAST scan |
| `just audit` | `just audit` | Dependency CVE audit (pip-audit) |

Transport: `connect`, `exec`, and `inject` default to `lease-shell`. Pass `ssh` as the last argument to force SSH: `just exec 12345 "cmd" ssh`.

### DSEQs vs Tags

**DSEQ** (Deployment Sequence) is the unique numeric ID assigned by Akash when you create a deployment.

**Tags** are human-readable names you can assign to DSEQs for easier management.

```bash
just up my-web-app         # Deploy and tag as "my-web-app"
just status my-web-app     # Check status using tag
just connect my-web-app    # Connect in using tag
just destroy my-web-app    # Destroy using tag
```

### Secrets Injection

Inject secrets into a running deployment — **no SSH required** (lease-shell is the default).

```bash
# From a file (lease-shell, default)
just inject "" .env.secrets

# Force SSH transport
just inject 12345 .env.secrets ssh

# Or with inline CLI args
uv run just-akash inject --dseq 12345 --env SECRET_KEY=abc --env DB_PASS=xyz

# From a file
uv run just-akash inject --dseq 12345 --env-file .env.secrets
```

Secrets are written to `/run/secrets/.env` (or custom `--remote-path`) with `chmod 600`.

### With `uv run` (direct CLI)

```bash
# Deploy
uv run just-akash deploy --sdl sdl/cpu-backtest-ssh.yaml

# Deploy with env vars (provider-visible)
uv run just-akash deploy --sdl sdl/cpu-backtest-ssh.yaml --env REGION=us-east

# Update an existing deployment in place (new SDL/image, same DSEQ + lease)
uv run just-akash update --dseq 12345 --sdl sdl/cpu-backtest-ssh.yaml --image repo/app:v2

# Connect / exec / inject
uv run just-akash connect --dseq 12345
uv run just-akash exec --dseq 12345 "echo hello"
uv run just-akash inject --dseq 12345 --env-file .env.secrets

# Force SSH transport
uv run just-akash exec --dseq 12345 --transport ssh "echo hello"
uv run just-akash inject --dseq 12345 --transport ssh --env-file .env.secrets

# Stream logs (snapshot or --follow) and Kubernetes events
uv run just-akash logs --dseq 12345 --tail 200
uv run just-akash logs --dseq 12345 --follow --service web
uv run just-akash events --dseq 12345

# Escrow: add USD funds, or toggle automatic top-up
uv run just-akash add-funds --dseq 12345 --deposit 5
uv run just-akash auto-topup --dseq 12345 --on
uv run just-akash auto-topup --dseq 12345        # show current setting

# List / status / destroy
uv run just-akash list
uv run just-akash status --dseq 12345
uv run just-akash destroy --dseq 12345
uv run just-akash tag --dseq 12345 --name my-job
```

## Run a personal Akash LCD/RPC node

`just up-akash-node` deploys a cosmos-omnibus node (chain `akashnet-2`) that
exposes a REST/LCD endpoint on port 1317, Tendermint RPC on 26657, and gRPC on
9090. It bootstraps from the official Akash snapshot
(`snapshots.akash.network/akashnet-2/latest`, refreshed hourly) and runs
`PRUNING=nothing` from there, so it's archival **going forward** from the
snapshot's height. No publicly-hosted Akash archive snapshot exists at the
moment — for older historical heights, an alternate LCD is still needed.

```bash
just up-akash-node              # deploy and tag "akash-node"
just status akash-node          # see provider URIs (boot ~15-25 min)
just akash-node-lcd             # prints the LCD URL once provisioned
just down-akash-node            # destroy when done
```

After the LCD URL is available:

```bash
akash-wallet-audit --api-base http://<host>:1317
```

The boot timeline is roughly 3-5 min to stream the ~10 GB lz4 snapshot, 10-15
min to extract it in-process, plus a short catch-up sync. The default SDL
asks for 4 vCPU / 16 GiB RAM / 250 GiB beta3 persistent storage with a price
ceiling of `100000 uact` per block — providers bid down from there.

The LCD is exposed publicly with no auth (fine for read-only queries; put a
proxy in front if this becomes a long-running node).

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `AKASH_API_KEY` | Yes | Console API key |
| `AKASH_PROVIDERS` | No | Comma-separated allowlist of **preferred** provider addresses (empty = accept any) |
| `AKASH_PROVIDERS_BACKUP` | No | Comma-separated allowlist of **backup** providers used only when no preferred bids arrive |
| `SSH_PUBKEY` | For SSH SDL | SSH public key (injected into container) |
| `AKASH_CONSOLE_URL` | No | Console API base URL (default: `https://console-api.akash.network`) |
| `AKASH_DEBUG` | No | Set to `1` for verbose API/deploy logging |

## Transports

`exec`, `inject`, and `connect` support two transports:

### Lease-shell (default)

Uses the Akash Console WebSocket proxy (`wss://console.akash.network/provider-proxy-mainnet`) to relay commands to the provider. **No SSH required.** The proxy connects to the provider using a JWT with provider-scoped permissions.

```bash
just exec 12345 "echo hello"              # lease-shell (default)
just inject 12345 .env.secrets          # lease-shell (default)
```

### SSH

Traditional SSH connection to the container. Requires an SSH-enabled SDL and `SSH_PUBKEY` configured.

```bash
just exec 12345 "echo hello" ssh        # force SSH
just inject 12345 .env.secrets ssh      # force SSH
```

## Bid Selection

Deployments use a three-phase tiered bid-selection state machine. Bids stream
in from `t=0` regardless of tier (Akash's auction is open; the tier is a
client-side filter).

| Phase | Window | Behavior on bid arrival | Decision at window end |
|---|---|---|---|
| 1. Preferred-only patience | `[0, T1]` (`--bid-wait`, default 60s) | Collect all bids; do not select yet | If any **preferred** bid collected → pick **cheapest preferred** and stop |
| 2. Preferred-grace | `[T1, T1+T2]` (`--bid-wait-retry`, default 120s) | Continue collecting; the moment a **preferred** bid appears, accept it **immediately** (first-wins) | If still no preferred → fall through |
| 3. Backup fallback | end of phase 2 | — | Pick **cheapest backup** from bids collected across phases 1+2 |

Properties:

- **Cheapest-when-healthy.** Preferred providers responsive → cheapest preferred wins.
- **Bounded patience.** Preferred slow but alive → wait at most `T1+T2`, then snap to first preferred.
- **Graceful degradation.** Preferred fully down → cheapest backup wins, no extra round trip.

### Tiered providers

Two tiers configure which providers are eligible:

```bash
# env-var form
export AKASH_PROVIDERS=akash1pref1,akash1pref2          # preferred (tier 1)
export AKASH_PROVIDERS_BACKUP=akash1back1,akash1back2   # backup (tier 2)

# CLI override (repeatable, overrides env when set)
uv run just-akash deploy \
  --provider akash1pref1 --provider akash1pref2 \
  --backup-provider akash1back1
```

When `AKASH_PROVIDERS_BACKUP` is unset, deploy behaves identically to the
single-tier allowlist (zero regression). With no allowlist at all (neither
preferred nor backup), the cheapest bid from any provider wins.

Each bid is tagged in the log as `[PREFERRED]`, `[BACKUP]`, or `[FOREIGN]`,
and the selection log line names which phase chose the winner.

## Persistent provider canary

The smoke test answers **"can I deploy right now?"** — it runs once a day and then
deliberately erases itself (`robust_destroy()`, the SIGINT handler, the no-leak guarantee).
That is correct for what it measures, and it makes an entire class of failure invisible,
because that class only appears over **time**:

- a provider closing the deployment on Tuesday afternoon
- a container restarted at 03:00
- egress that stops resolving DNS for twenty minutes
- an ingress that goes dark while the container is perfectly healthy

None of those happen inside a five-minute window at 07:00 UTC. A customer meets all of
them, because a customer's deployment **stays up**. The canary is the deployment that
stays up — one per provider, kept alive, measured from the inside.

| Piece | What it is |
|---|---|
| `canary/canary.py` | The agent that runs *inside* the lease. stdlib only, serves `/metrics`. |
| `canary/collect.py` | Scrapes each canary's ingress and keeps the durable counters. |
| `canary/ensure.py` | Decides which providers still have a live canary. Never deploys. |
| `sdl/canary.yaml` | The deployment. Minimal footprint, **not** throwaway. |
| `.github/workflows/provider-canary.yml` | Keeps them alive, collects every 30m, publishes. |

### What it measures, and why each is only visible from inside

| Signal | Metric |
|---|---|
| provider closed our deployment | `akash_canary_lease_replacements_total` |
| container restarted | `akash_canary_restarts_total` |
| customer can't reach the app | `akash_canary_reachable` / `akash_canary_unreachable_checks_total` |
| the app can't reach out | `akash_canary_egress_fail_total`, `akash_canary_dns_fail_total` |
| storage as the workload feels it | `akash_canary_disk_write_seconds` (fsync latency) |
| CPU contention from the customer's side | `akash_canary_sched_jitter_seconds` |

Published to the `telemetry` branch as `canary-metrics.prom`, next to the smoke data.

### Three design points that are load-bearing

**The agent cannot count its own restarts.** A restart wipes it, so a self-counter would
always read zero. It emits a `boot_id` that changes every process start and the *collector*
does the counting by diffing it across scrapes.

**A redeploy is not a restart.** When a lapsed lease is recreated the container is new, so
its `boot_id` changes too. The targets file carries the `dseq`; if that changed, the change
is attributed to a lease replacement and never to a restart. A provider closing our
deployment and a provider restarting our container are different faults, and conflating
them would destroy the signal in exactly the case where telling them apart matters.

**The counters are cumulative, so the 30-minute cadence loses timing precision but never
loses events.** Twelve egress failures between two collections still arrive as twelve.
That is what makes a cheap sampling interval acceptable instead of going blind between runs.

### Turning it on

The deploy step is the only one that spends money and is gated deliberately: on a schedule
it runs only when the `CANARY_AUTODEPLOY` repository variable is `true`, so merging the
workflow cannot start opening leases on its own. Bootstrap it by dispatching the workflow
once, confirm the leases and telemetry look right, then set the variable.

**There is nothing new to configure.** The canary deploys from the same
Console-API wallet as everything else here (`AKASH_API_KEY`, loaded from SOPS by the same
`sops-env` action), and it targets the same providers the smoke test does — it reads
`AKASH_PROVIDERS` directly rather than taking its own copy of that list. Provider addresses
are mapped to fleet names by `PROVIDER_NAMES` in `canary/ensure.py`.

That is deliberate. An earlier draft asked for a `CANARY_PROVIDER_WALLETS` variable
duplicating the address list; two copies drift, and the canary and the smoke would then be
measuring different fleets while both looked correctly configured. The name was also
misleading — every entry is a *provider's* address, not one of our wallets. We have one
wallet, and `just_akash_deploy_credit_usd` (already alerted on in df-grafana) is its credit.

One switch exists, and only because it spends money:

| Variable | Purpose |
|---|---|
| `CANARY_AUTODEPLOY` | `true` to let the schedule recreate a missing canary. |

⚠️ **What actually stops the canary being reaped is its SERVICE NAME, not its tag.**
`cleanup_stale` and the smoke startup sweep classify by service set — `{probe}` is stale
after 1h, `{backtest}` after 48h, `{}` is left alone as unclassifiable. This deployment's
service is `canary`, so it matches no stale rule.

That protection is incidental rather than declared, which makes it fragile in a specific
way: rename the service to `probe`, or add `canary` to a stale rule, and the next sweep
deletes it within the hour. The symptom would be *"the provider keeps closing our
deployment"* — the canary masquerading as the very fault it measures.

The `canary-<provider>` tag is separately load-bearing, but for **adoption**: it is how the
next run finds the lease in `just-akash list` and knows which `dseq` it is looking at.

## Logs

Every `just` recipe writes timestamped logs to `.logs/just/` with start/end metadata, exit codes, and full output.

## Secret Scanning

Three layers of secret detection run on every push/PR:

- **Gitleaks** — pre-commit hook + CI (full history on schedule)
- **TruffleHog** — CI (verified secrets only)
- **detect-secrets** — baseline diff check in CI

## License

[MIT](LICENSE) — Jonathan Borduas
