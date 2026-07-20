# Lease-Shell `exec` Reliability Investigation

**Status:** complete · **Date:** 2026-07-16 · **Author:** just-akash maintainers
**Method:** upstream code trace + in-process frame instrumentation + live A/B on two providers + multi-model quorum review (2 rounds, unanimous)

---

## 1. Executive summary

The daily provider smoke test flags `exec` as "failing" when a lease-shell command returns
`rc==0` but with **empty stdout**. Telemetry attributed this to a ~5 % "cold-stdout race"
(issue #12) and an early hypothesis blamed a **frame reorder** in the Console provider-proxy
(the exit-code frame overtaking the stdout frame).

This investigation set out to find the *root cause* rather than document the workaround. It
reached three evidence-backed conclusions, and **corrected two of its own hypotheses along the
way**:

1. **The reorder hypothesis is refuted.** Frame order is preserved end-to-end for the
   lease-shell path as implemented today (verified in code *and* in 240/240 clean control
   execs). A genuine trailing-stdout loss is a **drop**, not a reorder.
2. **`rc==0` is not a trustworthy success signal** for lease-shell exec. It can occur with empty
   stdout in *at least two* distinct, real situations: (A) a transient SPDY/CRI stream-teardown
   drop of trailing stdout, and (B) exec against a **closed/dead lease**, which returns a
   *synthetic* `{"exit_code":0}` with no output and no failure frame. The smoke test's
   marker-echo check (require the echoed token in stdout, **not** `rc==0` alone) is therefore
   essential — and was validated against a live reproduction of (B).
3. **A provider/protocol defect is worth escalating:** exec against a closed lease returns a
   misleading success instead of an error/failure frame.

Two hypotheses were **falsified by following the evidence**: the frame-reorder mechanism (refuted
by code), and a mid-investigation claim that a *healthy* provider was dropping all stdout (refuted
by a container-liveness check — the lease was actually closed). Both corrections are documented
below in full; they are the substance of the finding, not footnotes.

---

## 2. Background: the lease-shell exec path

`just-akash exec` runs a command inside a leased container over a WebSocket relayed by the Akash
**Console provider-proxy**. Frames are length-prefixed with a leading code byte:

| code | meaning |
|-----:|---------|
| 100  | stdout  |
| 101  | stderr  |
| 102  | result (JSON exit code, e.g. `{"exit_code":0}`) |
| 103  | failure |
| 104  | stdin   |
| 105  | terminal resize |

The command is transmitted as URL query params (`cmd0`, `cmd1`, …) in the connect message —
it is **exec of an argv vector**, not a shell interpreting a string (this detail matters in §5).

---

## 3. Root-cause code trace (why "reorder" is refuted)

Tracing the full path across three upstream repos:

- **Console provider-proxy** (`apps/provider-proxy/.../WebsocketServer.ts`, `linkSockets`): a
  transparent 1:1 relay — each provider frame is forwarded to the client with a single `ws.send`,
  no buffering, batching, or reordering.
- **Provider gateway** (`akash-network/provider`, `gateway/rest/router.go`): the result(102)
  frame is written **only after** `remotecommand.StreamWithContext` returns; stdout/stderr/result
  writes share one mutex, so writes are serialized.
- **client-go v0.34.1** (`tools/remotecommand`, `v4.stream` — v5 delegates to it): the SPDY
  executor does `wg.Wait()` on the stdout/stderr copy goroutines **before** `return <-errorChan`,
  i.e. it drains stdout to the writer *before* the exit code is available to be framed.
- **node/wsutil** (`akash-network/node`, `util/wsutil`): the WebSocket writer is synchronous
  (`WriteMessage` under a mutex).

**Conclusion (F1):** for the lease-shell dispatch path as implemented in these versions, there is
no point at which the exit-code frame can overtake a trailing stdout frame. The observable
"empty stdout" is a **dropped** stdout frame, not a reordered one. The documented Kubernetes
SPDY/CRI stream-teardown races (`kubernetes#60140`, `kubernetes#124571`; `moby#45689`,
`containerd#3118`) are the mechanism by which a fast-exiting command's *final* stdout can be lost
as the stream half-closes.

> **Scope (per quorum):** F1 is established for **this implementation/version** and the observed
> control population. It is not asserted as a universal guarantee across every provider build or
> future protocol version (e.g. a WebSocket-exec transport could differ).

---

## 4. Instrumentation & method

All measurements are **in-process** against the real transport (no subprocess buffering):

- **Recv frame tracer:** monkeypatch `LeaseShellTransport._recv_proxy_message` to record every
  frame's `(code, length, monotonic-time)`; capture per-exec stdout by redirecting `sys.stdout`
  to a `BytesIO`; flag any exec with `rc==0` but the marker missing.
- **Send capture:** monkeypatch `_build_proxy_connect_msg` (whose return value is passed verbatim
  to `ws.send`) to record the exact **outbound** command bytes.
- **Out-of-band liveness (never touches the shell endpoint):** `_service_availability` (lease
  status), the container **logs** endpoint, the k8s **events** endpoint, and the raw
  `lease.state`.
- **Two fresh probes** deployed from an identical `alpine:3.20` SDL
  (`sh -c "echo probe-up; sleep infinity"`), exercised through the **same client code path** —
  one control provider, one suspect provider.

---

## 5. Evidence

### 5.1 Control provider (healthy)

| command | rc | stdout | frame shape |
|---|---|---|---|
| `echo HELLO_WORLD` | 0 | `HELLO_WORLD\n` | `[stdout(100), result(102)]` |
| `whoami` | 0 | `root\n` | `[stdout(100), result(102)]` |
| 240× fast `echo` (200+40) | 0 | correct every time | always `[stdout, result]` |

- **0 / 240** empty-stdout — the transient race (A) did **not** reproduce on this provider.
- Liveness: `lease.state=active`, services `available=1`, logs show `[probe-…] probe-up`.

### 5.2 Suspect provider

| command | rc | stdout | stderr | frame shape |
|---|---|---|---|---|
| `echo`, `whoami`, `cat /etc/os-release`, `seq 1 200`, 64 KB blob, `sleep 1; echo`, `printf`, `hostname; id` | 0 | **empty** | **empty** | **`[result(102)]` only** |

- Every command → a single 16-byte result frame, decoded literally as `{"exit_code":0}\n`.
  **No stdout(100) frame ever; no failure(103) frame ever.** 100 % deterministic, timing-independent
  (`echo` vs `echo; sleep 0.2` both empty).
- **Outbound bytes identical to control** (`whoami` → `{cmd0:whoami}`; refutes "client didn't
  transmit / malformed send").
- Liveness (three independent out-of-band paths, all corroborating):
  `lease.state=**CLOSED**`, `services=None`, **logs empty**, **events empty**.

### 5.3 The two self-corrections

- **`exit 7` is a contaminated test.** Because the provider execs argv `["exit","7"]` and `exit`
  is a shell *builtin* (there is no `/bin/exit`), it is "exec-not-found" on **both** providers —
  not "the shell exited 7." The earlier "`exit 7`→rc=0 proves the command didn't run" argument was
  wrong. The robust evidence is the **real binaries** (`whoami`/`cat`/`seq`), which produce output
  on the control and nothing on the suspect.
- **The suspect is not a "healthy provider dropping stdout."** The mid-investigation claim that a
  live container was silently dropping all output was **falsified** by the liveness check: the
  lease is **closed**. There is no running container — which is why exec, logs, and events are all
  empty. Exec against the dead lease returns a *synthetic* `{"exit_code":0}`.

---

## 6. Diagnosis (ratified)

Two **distinct** failure modes produce the same `rc==0 + empty-stdout` symptom:

**(A) Transient stdout-teardown drop — issue #12.** The command *runs*; its trailing stdout is
occasionally dropped as the SPDY/CRI stream tears down on a fast-exiting command
(`kubernetes#60140/#124571`). Telemetry-observed (~5 % on two providers) and code-consistent;
**not** reproduced live this session (control was 240/240 clean, so the current control-provider
generation does not exhibit it at this volume).

**(B) Closed-lease fake success — reproduced this session.** exec against a **closed/dead lease**
returns a synthetic `{"exit_code":0}` with no output and **no failure(103) frame**. Directly
proven: outbound command identical to control, yet `lease.state=closed`, `services=None`, and logs
& events both empty across three independent endpoints.

> **Scope (per quorum):** the *directly proven* claim is "exec against this closed lease returns a
> fake success." That the provider *accepted a bid and then closed the lease without ever running
> the workload* is *inferred* from the empty logs (`probe-up` never appeared) and the closed lease
> state; the **cause** of the closure is not determined and is not claimed here.

### Corollary — why the smoke design is correct (F2)

`_check_exec` requires the **echoed marker in stdout**, not `rc==0` alone. Both (A) and (B) return
`rc==0` while nothing useful came back, so `rc==0` alone would **false-pass** a broken provider.
The marker-echo requirement catches both and is validated by the live reproduction of (B).
Note the **client exec path itself has no empty-stdout guard** — this rc-trust gap is a
protocol-level property; today only the smoke harness compensates for it.

### Escalation (F3)

Returning a synthetic `{"exit_code":0}` for exec against a closed lease — instead of a
failure(103)/error — is a **provider/protocol correctness defect** worth escalating: it makes
`rc==0` an unreliable health signal and can mask a non-functional lease as success.

---

## 7. Remediation

| # | Action | Rationale | Status |
|---|--------|-----------|--------|
| 1 | **Marker-echo check** in `_check_exec` (require token in stdout, not `rc==0`) | Catches both (A) and (B); `rc==0` is not trustworthy | **shipped** |
| 2 | **Retry-on-empty** (2–3 attempts, short backoff) | Transient (A) recovers on a later attempt; persistent (B) fails all attempts → cleanly separates transient from real breakage | **shipped** (v1.17.0+) |
| 3 | **Drain trailing stdout** after the result frame | Recovers a late stdout frame for (A) where possible | **shipped** (#49) |
| 4 | **`flaky-pass` telemetry marker** for retried-but-passed execs | Preserves visibility into how often (A) fires; do **not** let retries hide the raw first-attempt failure | **shipped** |
| 5 | **Fast-fail lease-state precheck** — read `lease.state`/`_service_availability` and fail fast with a distinct "lease not active" diagnostic instead of exhausting retries | A closed lease never recovers on retry; classifies (B) immediately, avoids ~2×45 s wasted round-trips, surfaces it distinctly in telemetry | **proposed** (quorum) |
| 6 | **Quarantine** persistently-broken providers from the reliability SLO — **manual**, gated on ≥2 consecutive full-run failures (all retries exhausted) | Keeps a broken provider monitored without reddening CI on a single transient hiccup; never auto-quarantine on one retry-exhaustion | **partial** (`_quarantined_providers` exists) |

**Guardrail:** retries are a *classification/gating* mechanism, not a root-cause fix for (A). The
raw first-attempt outcome and the `flaky-pass` rate must stay visible so a genuine provider
regression is never silently absorbed.

---

## 8. Quorum review

Diagnosis and methodology were reviewed by a 3-model quorum (codex-1/gpt-5.4,
opencode-1/grok-code-fast-1, copilot-1/gpt-4.1) over two rounds:

- **Round 1** (original diagnosis): 3/3 APPROVE-with-improvements. All three independently
  demanded proof of command-transmission and container-liveness before asserting "the command
  never runs." Acting on that demand **overturned** the "healthy provider drops stdout" framing
  and produced the corrected closed-lease diagnosis.
- **Round 2** (corrected diagnosis): **3/3 APPROVE, 0 BLOCK** (unanimous). Remaining feedback was
  scoping-only and is incorporated above (F1 version-scoping; F3 proven-vs-inferred split; the
  client-path rc-trust note; remediation #5). Full transcript:
  `.planning/quorum/debates/2026-07-16-exec-diagnosis-review.md`.

The quorum's core contribution: it caught a recv-only evidence gap that, once closed, corrected
the diagnosis. That is the process working as intended.

---

## 9. Reproduction (for maintainers)

```
# Deploy an identical probe to a control provider and a suspect provider:
#   alpine:3.20, args: sh -c "echo probe-up; sleep infinity"
# For each, via the SAME client code path, capture per-exec frames and out-of-band liveness.

# 1) recv frame trace — healthy path is always [stdout(100), result(102)]
uv run python frame_trace.py <dseq> 200            # expect 200/200 clean on a healthy lease

# 2) diagnostic battery — real binaries (whoami/cat/seq) must produce stdout on a live lease
uv run python diag_battery.py <dseq> <service>

# 3) liveness (out-of-band, never touches the shell endpoint)
#    lease.state, _service_availability(dseq), `just-akash logs --duration 12`, `events`
#    A closed lease => exec/logs/events all empty + a synthetic {"exit_code":0} result frame.
```

*(Instrumentation scripts live in the investigation scratch space; the key ones —
`frame_trace.py`, `frame_trace_ab.py`, `diag_battery.py` — capture recv frames, run the fast/delay
A/B, and characterize exactly what a provider drops.)*

---

## 10. Appendix — references

- **Issue #12** — exec cold-stdout race (this repo).
- **kubernetes#60140**, **kubernetes#124571** — SPDY/remotecommand stream-teardown stdout loss.
- **moby#45689**, **containerd#3118** — CRI "process exit ≠ pipe drain" race.
- **KEP-4006** — WebSocket exec for client↔apiserver (1.30); apiserver↔kubelet and Akash's own
  `NewSPDYExecutor` hop remain SPDY.
- Provider addresses are anonymized (control / suspect) per repository policy; `AKASH_PROVIDERS`
  is a secret.
