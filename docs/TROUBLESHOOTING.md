# Troubleshooting

Failure modes indexed by what you ran, with the log line or env var that tells the
causes apart. Cross-references `exec-reliability-investigation.md` for the deep dives.

## `just deploy` fails

| Symptom | Likely cause | Fix |
|---|---|---|
| `No bids received within …s` | SDL unsatisfiable, providers offline, deposit too low, no capacity on allowed providers | check the SDL (`just-akash validate-sdl sdl/…`); lower the price ceiling; widen the allowlist |
| `Received N bid(s) but NONE from our providers` | bids arrived, all FOREIGN to your tiers | confirm `AKASH_PROVIDERS`/`AKASH_PROVIDERS_BACKUP` addresses; check provider on-chain status in the log |
| `… bid(s) from your providers … no longer open (states seen: …)` | bids aged past the ~5min expiry (issue #14) | retry — the re-deploy round usually recovers; if persistent, the providers are slow |
| `Deployment already exists` then retry fails | a stale lease-less deployment blocked the name | the recovery auto-closes lease-less stale deployments; if it persists, `just destroy <dseq>` the stale one |
| `SDL validation failed: … signedBy … only … is allowed` | a `signedBy` address isn't the audit authority | use the authority address or drop `signedBy` |

## `just exec` returns nothing / wrong exit code

`rc=0` is **not** a trustworthy success signal — two distinct causes look identical
(`rc=0` + empty stdout). See `exec-reliability-investigation.md`.

| Symptom | Cause | Fix |
|---|---|---|
| `rc=0`, empty stdout, transient | cold-stdout race (issue #12): trailing stdout dropped as the stream tears down | retry; the `flaky-pass` stderr marker shows when the drain caught it |
| `rc=0`, empty stdout, persistent, lease `closed` | exec against a closed/dead lease returns a synthetic `{"exit_code":0}` | the lease is gone — redeploy (`just status` shows `state: closed`) |
| `provider-proxy sent nothing for Ns` | the command hung (silent) past `recv_timeout` | raise `TransportConfig.recv_timeout` if the command is legitimately long |
| `provider-proxy closed the connection without sending a result` | clean close before any result frame | the session ended early; retry, and check lease liveness |
| `Failed to re-authenticate after 3 attempts` | JWT minting keeps failing | confirm `AKASH_API_KEY` is valid and the deployment is active |

## `just inject` failed / wrote the wrong thing

- **`Error: Invalid --env format`** — a `--env` value lacked `=`. Use `KEY=VALUE`.
- **`Error: No secrets to inject`** — neither `--env` nor `--env-file` given.
- **Inject reports success but the file is empty** — this was the v1.29.0 regression
  (the `head -c` stdin-frame path wrote 0 bytes). The smoke `_inject_and_read`
  readback catches it; if you hit it, you're on an old version — upgrade.
- **Secret visible in provider-proxy logs** — expected today (issue #39): the base64
  rides the connect URL. Use `--transport ssh` if the proxy URL log is in your threat
  model. The stdin-frame re-fix is tracked.

## `just connect` does nothing

- **Not a TTY** — `connect` needs an interactive terminal (raw-mode full-duplex). It
  can't be driven from a subprocess; use `exec`/`inject` for automation.
- **Session dies after ~1h** — the interactive JWT is captured once and not refreshed
  (known; the streaming path does refresh). Reconnect.
- **Windows** — lease-shell `connect` is POSIX-only; use `--transport ssh` or WSL2.

## `just benchmark` returns no metrics

- **Via `exec` it produced nothing** — fixed in v1.30.0: `BENCH_SH` is a *script* and
  must run via `exec_shell_script` (`sh -c`), not `exec`'s argv path. If you see no
  `BENCH-` lines, you're on a pre-1.30 build.
- **`exit 1` with `complete: false`** — the probe didn't emit `BENCH-done=1`. Likely a
  minimal image missing a tool; every metric degrades to absent rather than erroring,
  but a truncated run is still graded incomplete.

## `just logs` / `just events` empty

- **`Error: no active lease / provider hostUri`** — the lease isn't active yet, or the
  Console hasn't populated `hostUri`. Check `just status`; the Console populates lease
  status lazily, so retry once it's running.
- **Empty stream on a live lease** — some providers stream each frame as plain-text
  JSON, not base64; `_decode_payload`'s `text_fallback` handles this. If still empty,
  check `lease.state` and container logs out-of-band.

## Smoke / telemetry

- **`UNDER-DELIVERING`** vs **`UNSTABLE`** — orthogonal: throttle/steal (cgroup) vs
  high variance across samples (noisy neighbour). Different remediations; see the
  `diag` fields in the telemetry row.
- **A provider that passes every feature but `UNDER-DELIVER`s** — responsive but
  capping CPU below spec; non-gating (it's a quality signal, not a feature failure).
- **`GATE DISABLED — no ceilings set`** — `analyze_telemetry --check` without
  `--max-p95`. The loud warning is intentional (a silently-disabled gate once hid an
  incident).
- **`NO-CREDIT` exits 0** — the scheduled run no-ops when the wallet is exhausted
  (correct for a cron), but a chronically-low balance silently no-ops indefinitely —
  monitor it.
