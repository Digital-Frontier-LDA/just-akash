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
| `PROVIDER_STATUS_QUERY_FAILED` | On-chain status query failed (couldn't classify). |
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
| `EXEC_EXIT_CODE_UNKNOWN` | A result frame carried a null or absent `exit_code`, so the command's real exit status is **unknown**; the temporary shim reported 0. `context.shape` is `a null exit_code` or `no exit_code key`. |

### `EXEC_EXIT_CODE_UNKNOWN` and the issue-#85 survey

This code is not only a Sentry signal — it is the *measurement instrument* for
retiring the compatibility shim. The removal condition in
[#85](https://github.com/Digital-Frontier-LDA/just-akash/issues/85) is "zero
occurrences across all active providers over 30 consecutive days", which is a
count per provider over time; the human log line beside this event cannot be
counted, so without the structured event the condition could never be evaluated.

The chain:

1. `LeaseShellTransport._dispatch_frame` emits the event when the shim fires.
2. The provider smoke parses it from every exec's stderr and records the shapes
   on the run's `exec` telemetry row as `exit_code_shapes` (field **absent** when
   the shim never fired — that absence is the clean signal).
3. `analyze-telemetry --shim-survey` reports occurrences per provider and the
   clean-day streak, and prints the verdict.

```bash
just smoke-shim-survey          # against the live accrued telemetry branch
```

Records below `SHIM_SURVEY_MIN_VERSION` are **not** evidence: they predate the
instrumentation, so their silence means "not measured", not "clean". Counting
them would start the 30-day clock in the past and retire the shim on evidence
that was never collected.

## Caller bridge — turning events into GitHub `::error` / Sentry

just-akash emits the events; the **caller** ships them. Example for a GitHub Actions
workflow (`akash-runner.yml`):

```bash
# Run deploy; capture stderr, grep for diagnostic events, emit ::error annotations.
set -o pipefail  # so $? reflects just-akash's exit, not tee's
uv run just-akash deploy --sdl sdl/app.yaml 2>&1 | tee /tmp/deploy.log
deploy_rc=$?
grep '"type":"akash-diag"' /tmp/deploy.log | while read -r line; do
  code=$(echo "$line" | jq -r '.code')
  msg=$(echo "$line" | jq -r '.message')
  echo "::error title=$code::$msg"
done
exit $deploy_rc  # preserve the deploy's exit status (don't let the annotation loop mask it)
```

For Sentry: pipe stderr through a thin `sentry-sdk` shim that captures each `akash-diag`
line as a structured event with `code` as the fingerprint (so repeats de-duplicate).
