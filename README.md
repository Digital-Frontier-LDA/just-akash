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

## Verifying what is installed (uv-tool installs do not self-update)

A `uv tool install --from "git+…@<rev>"` pins that rev in
`~/.local/share/uv/tools/just-akash/uv-receipt.toml`, and `uv tool upgrade`
**honors the pin: it exits 0 while delivering nothing**. A merged fix can sit
on `main` for days while every upgrade looks successful — the #168
grant-reconciliation fix was merged 5 days before the machine running it got
it, and for those 5 days `balance` reported a superseded grant as live credit.

The check nobody knew to make — installed rev vs `main`:

```bash
# `uv tool dir` resolves UV_TOOL_DIR and the platform default; the hard-coded
# ~/.local/share path is wrong on any machine that sets either.
grep -o 'rev=[a-f0-9]*' "$(uv tool dir)/just-akash/uv-receipt.toml"
git ls-remote https://github.com/Digital-Frontier-LDA/just-akash main
```

The command that always delivers is an explicit reinstall at the rev you want:

```bash
uv tool install --force --from "git+https://github.com/Digital-Frontier-LDA/just-akash@<main-sha>" just-akash
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

Deployments use a bounded preferred window followed, only when necessary, by a
bounded first-eligible fallback window. Bids stream from
`t=0` regardless of tier (Akash's auction is open; tiers are a client-side
eligibility policy). `--bid-wait` configures the complete window from 0 to 60
seconds and defaults to 60.

At the preferred deadline:

1. if any open preferred bid exists, select the cheapest preferred bid;
2. otherwise select the first eligible backup bid already observed, or continue
   polling until the first eligible bid arrives;
3. with no allowlist, the same fallback rule applies to any non-excluded provider.

The decision is implemented by the shared, sans-I/O `akash-lease-core` package,
which is also consumed by Digital Frontier's Console and wallet deployment paths.
`--bid-wait-retry` is the total bounded deadline (120 seconds by default), so the
fallback phase cannot hang indefinitely or leak an unclosed deployment.

Properties:

- **Cheapest-when-healthy.** Preferred providers responsive → cheapest preferred wins.
- **Equal opportunity.** An early bidder cannot pre-empt a cheaper provider arriving later in-window.
- **Bounded patience.** Preferred waits at most 60 seconds; total selection stays
  within the configured retry deadline.
- **Graceful degradation.** Preferred fully down → first eligible fallback wins.

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
preferred nor backup), the first eligible bid at or after the preferred deadline wins.

Each bid is tagged in the log as `[PREFERRED]`, `[BACKUP]`, or `[FOREIGN]`,
and the selection log line records the shared policy version and decision reason.

## Console wallet pool

`AKASH_API_KEYS` enables native multi-wallet selection. Separate keys with newlines,
commas, or semicolons; the existing `AKASH_API_KEY` remains a compatible single-key
fallback and is included when both variables are set.

For a new deployment, `just-akash` resolves the distinct Console accounts, reads their
on-chain `spend_limits[uact]` at one height with a two-endpoint quorum, and chooses the
richest account that can fund the requested deposit. A stale or height-unprovable LCD
does not get a vote, and two keys resolving to one account count once.

For commands against an existing DSEQ—including `status`, `update`, `exec`, and
`destroy`—the CLI probes the configured pool and uses the account that positively reads
that deployment. It never re-runs the richest-wallet decision for cleanup: balances can
change after creation, and closing with a different account returns 404 while leaving
escrow behind.

```bash
export AKASH_API_KEYS=$'key-for-wallet-a\nkey-for-wallet-b'
just-akash deploy --sdl deploy.yml
just-akash destroy --dseq 123456789 -y  # automatically finds the owning wallet
```

Independent concurrent CI runs can still choose the same richest account at once. The
reusable runner workflow classifies and retries sequence contention, but GitHub
concurrency groups are repository-scoped and cannot reserve a wallet across repositories.
A future organization-level provisioning broker may add that reservation; native ranking
remains the authoritative funding decision and DSEQ ownership remains authoritative for
cleanup.

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
| `canary/details.py` | Fetches the per-deployment detail. Summary rows carry no leases. |
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

### Four design points that are load-bearing

**A canary is identified by its service set and its lease provider — never by local state.**
`ensure.py` adopts a deployment whose services are exactly `{canary}` and whose lease is held
by that provider. Both come off the deployment itself, so any runner can work it out. The
first version matched `just-akash tag` names instead, which live in `.tags.json` in the
working copy: a GitHub runner is wiped after each job, so the tag was gone by the next run,
every provider read as missing, and with `CANARY_AUTODEPLOY` on that would have opened three
fresh leases every thirty minutes — none of which any reaper collects.

Two things follow, both deliberate. "No canary here" and "could not look" are kept apart: a
failed API read marks the details document incomplete and nothing is deployed that run, and a
lease reporting no services yet (how a canary looks for its first few minutes) also suppresses
the deploy. Waiting 30 minutes is free; a duplicate lease bills until someone notices.


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

Two switches exist, both about money:

| Variable | Purpose |
|---|---|
| `CANARY_AUTODEPLOY` | `true` to let the schedule recreate a missing canary. |
| `CANARY_MIN_CREDIT_USD` | Credit floor below which the canary declines to create leases. Default **`0` (disabled)** — see the authorization-vs-balance note above for why. |

### ⚠️ One wallet means the canary and the smoke must not run at once

They share `AKASH_API_KEY`, and that is a **single Cosmos account**. Two accounts-worth of
work, one sequence number: concurrent deploys make one of them fail with an account-sequence
mismatch, and nothing in `just_akash` retries that. The canary losing that race looks
identical to a provider refusing to bid — a fabricated fault in the very signal it exists to
produce.

So both workflows share the concurrency group **`akash-wallet`**. Renaming it in either file
silently un-serialises them. The canary's schedule is also offset to `:05`/`:35` rather than
the hour, since the smoke runs at 07:00 and can run for 40 minutes.

The cost is that a collection can queue behind a smoke run — up to ~70 minutes, once a day —
and that cost is near-zero by design: the counters are cumulative, so a late collection loses
timing precision and no events.

### ⚠️ `balance --check` reports authorization headroom, NOT available balance

Worth knowing before you read `free_usd` as money. Console issues this API key a
**DepositAuthorization** with a spend limit; `balance --check` reports
`granted − locked_in_escrow`, i.e. how much of *that authorization* is uncommitted. The
account's available balance is a different, larger number and lives on the Console side —
the on-chain `liquid` bank balance is empty, because the funds sit with the granter.

Measured 2026-08-06: `free_usd` read **$2.31** while the Console account held **$573.38**,
and a deploy at that moment succeeded on all three providers. A credit floor gating on
`free_usd` was therefore blocking deploys that work, so it now defaults to **0 (disabled)**.
The figure is still read and published every run — the information was never the problem,
the blocking was.

### ⚠️ One wallet is also one budget

The lock solves the sequence race. It does nothing about the two competing for **credit**:
the canary holds escrow permanently, the smoke needs credit to deploy at all, and a canary
recreating leases against a flapping provider could quietly starve the smoke into 402ing
every morning.

Measured from `just_akash_deploy_credit_usd` on 2026-08-05 — **$81.37, against $154.33 a week
earlier**, i.e. roughly $10/day on existing usage alone, reaching the `<$20` warning in about
six days. This is not hypothetical.

So the canary **yields**: below `CANARY_MIN_CREDIT_USD` it declines to create new leases and
says so in the run log, leaving the remaining budget to the smoke — because *"can we deploy at
all"* is the more important question, and it is the one the canary cannot answer for itself.
Canaries already running are untouched; only new leases are blocked. Deposit defaults to $2
each rather than $5, so three canaries hold $6 rather than $15 of a wallet this size.

⚠️ **What actually stops the canary being reaped is its SERVICE NAME, not its tag.**
`cleanup_stale` and the smoke startup sweep classify by service set — `{probe}` is stale
after 1h, `{backtest}` after 48h, `{}` is left alone as unclassifiable. This deployment's
service is `canary`, so it matches no stale rule.

That protection is incidental rather than declared, which makes it fragile in a specific
way: rename the service to `probe`, or add `canary` to a stale rule, and the next sweep
deletes it within the hour. The symptom would be *"the provider keeps closing our
deployment"* — the canary masquerading as the very fault it measures.

The service name carries a **second** job: it is also how the next run recognises the lease
as ours (service set `{canary}` + lease provider). So renaming it breaks two things at once
and in opposite directions — the sweep deletes the lease, or `ensure.py` stops seeing it and
deploys another alongside. Treat the name as an interface.

The `canary-<provider>` tag is a local convenience only. `just-akash tag` writes
`.tags.json` in the working copy, which a GitHub runner destroys with the job, so the tag
cannot identify anything on the next run — it was the original matching key and that is
exactly why every provider read as `NEEDS DEPLOY` forever.

## Logs

Every `just` recipe writes timestamped logs to `.logs/just/` with start/end metadata, exit codes, and full output.

## Secret Scanning

Three layers of secret detection run on every push/PR:

- **Gitleaks** — pre-commit hook + CI (full history on schedule)
- **TruffleHog** — CI (verified secrets only)
- **detect-secrets** — baseline diff check in CI

## License

[MIT](LICENSE) — Jonathan Borduas
