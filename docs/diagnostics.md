# Diagnostics — structured "why it didn't happen" events

Every diagnosable reason a deploy/bid/lease didn't happen is emitted as a JSON-line event
to **stderr** with a stable reason code + evidence context. just-akash stays dependency-free
and **additive** — the operation's behavior never changes; the caller decides whether to act.

## Envelope

```json
{"type":"akash-diag","level":"error","code":"PROVIDER_NO_BID",
 "message":"provider looks healthy on-chain but did not bid: akash1hgulk…",
 "dseq":"12345",
 "context":{"provider":"akash1hgulk…","tier":"preferred","isOnline":true,
            "isValidVersion":true,"uptime1d":99.5,"cpu_available":1000}}
```

| Field | Description |
|---|---|
| `type` | Always `"akash-diag"` — filter key for shippers. |
| `level` | `"warning"` (risk/degradation; op continues) or `"error"` (the operation failed). |
| `code` | Stable `UPPER_SNAKE` reason code (the taxonomy below). |
| `message` | One-line human summary. |
| `dseq` | Present when a deployment is in flight. |
| `context` | Evidence dict (provider, on-chain status, credit amounts, bid states…). |

## Gating

Emit when stderr is not a tty (CI/pipes) **or** `AKASH_DIAGNOSTICS` is `1`/`json`/`true`.
Silent in an interactive terminal (humans keep the existing `_log`/`Error:` prose).
`AKASH_DIAGNOSTICS=off` forces it off.

## Reason-code taxonomy

### Wallet / credit (pre-deploy — the Blazing-Back #1 disambiguator)

| Code | Level | Condition |
|---|---|---|
| `WALLET_INSUFFICIENT_CREDIT` | error | Deploy credit is 0 / no grant → deploy will 402. |
| `WALLET_LOW_CREDIT` | warning | Credit below threshold (deposit × 2); may still succeed. |
| `WALLET_CREDIT_QUERY_FAILED` | warning | LCD unreachable or identity probe failed; couldn't confirm. |

### Provider health (from the no-bid on-chain block)

| Code | Condition |
|---|---|
| `PROVIDER_OFFLINE` | `isOnline: false`. |
| `PROVIDER_INVALID_VERSION` | `isValidVersion: false`. |
| `PROVIDER_NO_CAPACITY` | cpu/mem available too low for the SDL (reserved). |
| `PROVIDER_NO_BID` | Healthy on-chain but didn't bid (catch-all). |
| `PROVIDER_UNKNOWN` | Not in the provider registry. |

### Deploy lifecycle (one per `deploy.py` failure path)

| Code | Condition |
|---|---|
| `NO_BIDS_RECEIVED` | Zero bids within the polling window. |
| `BIDS_FOREIGN_ONLY` | Bids arrived but only from non-allowed providers. |
| `BIDS_STALE` | Bids from allowed providers but all expired (not `open`). |
| `BIDS_MALFORMED` | All bid entries failed schema validation. |
| `DEPLOY_CREATE_FAILED` | `create_deployment` raised. |
| `NO_DSEQ_RETURNED` | Create succeeded but no DSEQ in response. |
| `LEASE_CREATE_FAILED` | Lease creation failed (incl. 404 "no lease for deployment"). |
| `REDEPLOY_FAILED` | The issue-#19 re-deploy round exhausted. |
| `SDL_ERROR` | SDL validation/missing/placeholder (reserved). |
| `CONFIG_ERROR` | Missing API key / bad deposit / bad `--env` (reserved). |

### Reliability (post-deploy / smoke — "external sweep reaps the lease")

| Code | Condition |
|---|---|
| `LEASE_DOWN` | Lease died after creation (provider eviction / sweep / node loss). |

## Caller bridge — turning events into GitHub `::error` / Sentry

just-akash emits the events; the **caller** ships them. Example for a GitHub Actions
workflow (`akash-runner.yml`):

```bash
# Run deploy; capture stderr, grep for diagnostic events, emit ::error annotations.
uv run just-akash deploy --sdl sdl/app.yaml 2>&1 | tee /tmp/deploy.log
grep '"type":"akash-diag"' /tmp/deploy.log | while read -r line; do
  code=$(echo "$line" | jq -r '.code')
  msg=$(echo "$line" | jq -r '.message')
  echo "::error title=$code::$msg"
done
```

For Sentry: pipe stderr through a thin `sentry-sdk` shim that captures each `akash-diag`
line as a structured event with `code` as the fingerprint (so repeats de-duplicate).
