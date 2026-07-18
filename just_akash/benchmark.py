#!/usr/bin/env python3
"""Benchmark what a provider ACTUALLY delivered for a lease.

The smoke test answers "does this provider work?" (feature matrix). This answers
"is the hardware any good?" — vCPU throughput, RAM bandwidth, disk I/O, WAN — so a
provider can be graded, not just pass/fail'd. It exists because the fleet shows a
stable ~6x spread in readiness latency between providers with the SAME declared
resources (1 vCPU / 1Gi / 5Gi), and pass/fail can't explain WHY.

RESOURCE HONESTY (delivered vs promised): a provider can be perfectly RESPONSIVE
(passes every feature) and still hand you a fraction of the vCPU you paid for. The
probe snapshots the cgroup CPU-throttle counters and host steal AROUND its single-
threaded cpu benchmark; :func:`resource_fidelity` turns those into a delivered-vs-
promised verdict (being throttled while running ONE thread on a lease sold as
>=1 vCPU is the tell-tale of oversubscription). Measured live: a fleet provider
that passed 10/10 features throttled the benchmark on every run.

Design constraints (3-model quorum, unanimous):

* **Bounded WELL under the lease's cgroup limits.** The obvious mistake is a big
  RAM benchmark: `sysbench memory --memory-total-size=4G` inside a 1Gi cgroup gets
  OOM-killed, which *self-inflicts* the very lease-death the probe exists to
  measure and poisons the data. Everything here is capped at 256M and one thread.
* **Never in the every-run smoke.** Benchmark load makes a feature failure
  unattributable ("the provider, or our own stress?") and inflates the daily run.
  This is a separate, on-demand command.
* **Report per-dimension; no composite score.** Providers specialise, and a single
  blended number hides the dimension that actually regressed. Grading needs
  several samples per provider before it means anything.

The probe script is POSIX sh, runs inside the lease over the normal exec path, and
emits ``BENCH-key=value`` lines that :func:`parse_results` turns into a dict. It
never fails the run over a missing capability (no sysbench, no PSI, a blocked
network): the contract is that an unavailable metric is simply **absent** from the
result dict — either the probe printed ``na`` or an empty value that the parser
drops. Callers must treat "absent" as unmeasured, never as zero. A partial
benchmark is still useful, so completeness is reported separately via
:func:`is_complete` (the probe's final ``BENCH-done=1``).
"""

from __future__ import annotations

import re
import statistics

# Bounded to stay far under the probe's 1 vCPU / 1Gi / 5Gi. See module docstring:
# exceeding the memory cgroup would OOM-kill the container mid-benchmark.
_MEM_MB = 256
_DISK_MB = 256
_CPU_SECONDS = 3

# Resource-honesty verdict thresholds. ANY throttling during a single-threaded run
# is already damning, so throttled has no tolerance; steal and under-load PSI get a
# small floor since a few percent is normal scheduler noise, not oversubscription.
_STEAL_PCT_LIMIT = 5.0
_CPU_PSI_LOAD_LIMIT = 10.0

# Stability: run the cpu benchmark this many times back-to-back and score the spread.
# A dedicated/steady host holds within a few percent; a noisy or oversubscribed one
# swings, so a coefficient of variation above this floor reads as UNSTABLE.
_STABILITY_SAMPLES = 5
_CV_LIMIT = 15.0

# One shell program, exec'd into the lease. Keep it dependency-light: sysbench is
# apk-added best-effort, and every probe degrades to `na` instead of erroring, so a
# minimal image still yields the cheap metrics (cpuinfo/PSI/RTT).
BENCH_SH = f"""
set +e
say() {{ echo "BENCH-$1=$2"; }}

# --- static facts (free) ---
say ncpu "$(nproc 2>/dev/null || echo na)"
# NB: `|| echo na` cannot fire on a pipeline (the last stage succeeds on empty
# input), so these yield "" when unavailable — the parser drops empties, and
# absent means unmeasured. See the module docstring's contract.
say cpu_model "$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ *//')"
say cpu_mhz "$(grep -m1 'cpu MHz' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | tr -d ' ')"
say kernel "$(uname -r 2>/dev/null || echo na)"
say mem_limit "$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo na)"
say cpu_max "$(cat /sys/fs/cgroup/cpu.max 2>/dev/null || echo na)"

# --- contention (free, and the highest-signal health metric) ---
say mem_psi "$(grep '^some' /proc/pressure/memory 2>/dev/null | cut -d' ' -f2 || echo na)"
say cpu_psi "$(grep '^some' /proc/pressure/cpu 2>/dev/null | cut -d' ' -f2 || echo na)"
say io_psi "$(grep '^some' /proc/pressure/io 2>/dev/null | cut -d' ' -f2 || echo na)"
say throttled "$(grep nr_throttled /sys/fs/cgroup/cpu.stat 2>/dev/null | cut -d' ' -f2 || echo na)"

# --- network: RTT to two anycast anchors (cheap, no payload) ---
for host in 1.1.1.1 8.8.8.8; do
  # The summary reports min/avg/max[/mdev]; we want AVG (2nd field). Grep the
  # numeric triple and cut field 2, rather than a positional cut on the whole
  # line: the field index differs by ping build (busybox "1/2/3 ms" vs iputils
  # "1/2/3/4 ms"), so a fixed `cut -f5` grabs max/mdev, not avg.
  rtt=$(ping -c 3 -W 2 "$host" 2>/dev/null \
        | grep -oE '[0-9.]+/[0-9.]+/[0-9.]+' | head -1 | cut -d/ -f2)
  say "rtt_$host" "${{rtt:-na}}"
done

# --- disk: bounded sequential write/read ---
# PID-unique paths + a trap: each probe already runs in its own lease/container so
# these are never shared BETWEEN probes, but the trap still guarantees a killed
# probe leaves no artifact behind inside its container.
BENCH_TMP="/tmp/.bench.$$"
trap 'rm -f "$BENCH_TMP" "$BENCH_TMP.w" "$BENCH_TMP.r"' EXIT INT TERM
# NB: no `|| echo na` on these — `tail` exits 0 even on empty input, so the fallback
# can't fire on a pipeline (same gotcha the cpuinfo lines above document). When the
# grep finds no rate the value is simply empty, which the parser drops → absent =
# unmeasured, exactly the contract we want.
dd if=/dev/zero of="$BENCH_TMP" bs=1M count={_DISK_MB} conv=fdatasync 2>"$BENCH_TMP.w" >/dev/null
say disk_write "$(grep -oE '[0-9.]+ [MG]B/s' "$BENCH_TMP.w" 2>/dev/null | tail -1)"
# iflag=direct bypasses the page cache, so this measures the DEVICE rather than the
# pages the write just populated (conv=fdatasync flushes but does NOT evict them).
# If the fs can't do O_DIRECT (overlayfs/tmpfs), dd errors, the grep finds no rate,
# and disk_read is absent — better than a cache-inflated number that skews grading.
dd if="$BENCH_TMP" of=/dev/null bs=1M iflag=direct 2>"$BENCH_TMP.r" >/dev/null
say disk_read "$(grep -oE '[0-9.]+ [MG]B/s' "$BENCH_TMP.r" 2>/dev/null | tail -1)"
rm -f "$BENCH_TMP" "$BENCH_TMP.w" "$BENCH_TMP.r"

# --- cpu + ram via sysbench (best-effort install; `na` if unavailable) ---
apk add --no-cache sysbench >/dev/null 2>&1
if command -v sysbench >/dev/null 2>&1; then
  # RESOURCE HONESTY (delivered vs promised): snapshot the cgroup CPU-throttle
  # counters and host steal, run a SINGLE-THREADED cpu benchmark, snapshot again.
  # The idle throttled/PSI above says little; what's damning is being throttled
  # WHILE running one thread on a lease sold as >=1 vCPU — that means the provider
  # is capping you below what you paid for. steal>0 means the host is a VM sharing
  # CPU. Emitted raw (pre/post); parse_results computes the deltas.
  say thr_pre "$(grep nr_throttled /sys/fs/cgroup/cpu.stat 2>/dev/null | cut -d' ' -f2)"
  say thrus_pre "$(grep throttled_usec /sys/fs/cgroup/cpu.stat 2>/dev/null | cut -d' ' -f2)"
  say steal_pre "$(awk '/^cpu /{{print $9}}' /proc/stat 2>/dev/null)"
  say cputot_pre "$(awk '/^cpu /{{t=0;for(i=2;i<=NF;i++)t+=$i;print t}}' /proc/stat 2>/dev/null)"
  cpu=$(sysbench cpu --time={_CPU_SECONDS} --threads=1 run 2>/dev/null \
        | grep -m1 'events per second' | cut -d: -f2 | tr -d ' ')
  say cpu_eps "${{cpu:-na}}"
  say thr_post "$(grep nr_throttled /sys/fs/cgroup/cpu.stat 2>/dev/null | cut -d' ' -f2)"
  say thrus_post "$(grep throttled_usec /sys/fs/cgroup/cpu.stat 2>/dev/null | cut -d' ' -f2)"
  say steal_post "$(awk '/^cpu /{{print $9}}' /proc/stat 2>/dev/null)"
  say cputot_post "$(awk '/^cpu /{{t=0;for(i=2;i<=NF;i++)t+=$i;print t}}' /proc/stat 2>/dev/null)"
  say cpu_psi_load "$(grep '^some' /proc/pressure/cpu 2>/dev/null | cut -d' ' -f2)"
  # STABILITY under sustained load: run the cpu benchmark several more times, each
  # short, and emit every sample. A provider that's fast ONCE but degrades (thermal,
  # a neighbor ramping up) is worse than a steady one; the parser scores the spread.
  _samples=""
  _n=0
  while [ $_n -lt {_STABILITY_SAMPLES} ]; do
    _s=$(sysbench cpu --time=1 --threads=1 run 2>/dev/null \
         | grep -m1 'events per second' | cut -d: -f2 | tr -d ' ')
    _samples="$_samples $_s"
    _n=$((_n + 1))
  done
  say cpu_samples "$_samples"
  # --memory-total-size is the TOTAL streamed, not resident -- but keep it small
  # anyway; a big value here is what OOM-kills the container.
  mem=$(sysbench memory --memory-block-size=1M --memory-total-size={_MEM_MB}M \
        --memory-oper=write run 2>/dev/null \
        | grep -m1 'transferred' | grep -oE '[0-9.]+ MiB/sec' | head -1)
  say mem_bw "${{mem:-na}}"
else
  say cpu_eps na
  say mem_bw na
fi
say done 1
"""

_LINE = re.compile(r"^BENCH-([A-Za-z0-9_.]+)=(.*)$")


def parse_results(stdout: str) -> dict[str, str]:
    """Turn the probe's ``BENCH-key=value`` lines into a dict.

    Tolerates interleaved noise (apk/sysbench chatter, the pod-name prefix the
    logs path adds) by matching the marker anywhere on a line, and drops empty
    values so a missing metric reads as absent rather than "".
    """
    out: dict[str, str] = {}
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        # The exec path may prefix lines; find the marker wherever it starts.
        idx = line.find("BENCH-")
        if idx >= 0:
            line = line[idx:]
        m = _LINE.match(line)
        if m:
            key, value = m.group(1), m.group(2).strip()
            # Drop both empties AND the explicit "na" sentinel: the contract (module
            # docstring) is that an unavailable metric is simply ABSENT, however the
            # probe spelled it. Letting "na" through would leak a non-measurement into
            # formatted output and JSON as if it were a value.
            if value and value != "na":
                out[key] = value
    return out


def is_complete(results: dict[str, str]) -> bool:
    """Did the probe run to the end? ``BENCH-done=1`` is the last line, so its
    absence means the exec was cut short (timeout/kill) and the numbers are a
    partial sample that must not be graded."""
    return results.get("done") == "1"


def _leading_number(value: str | None) -> float | None:
    """The metric's number, from ``avg10=12.34`` (→12.34) or a bare ``900.5``.

    PSI fields are ``key=value`` (e.g. ``avg10=12.34``), so a naive "first number"
    match would grab the ``10`` out of the KEY. Take the number after the last ``=``.
    """
    if not value:
        return None
    m = re.search(r"[0-9]+\.?[0-9]*", value.rsplit("=", 1)[-1])
    return float(m.group()) if m else None


def resource_fidelity(results: dict[str, str]) -> dict:
    """Did the provider deliver the CPU it sold? Derived from the under-load snapshots.

    A provider can pass every feature check and still hand you a fraction of the vCPU
    you paid for — invisible to pass/fail. This turns the raw pre/post counters the
    probe captured around its single-threaded cpu benchmark into three delivered-vs-
    promised signals, plus a verdict:

    * ``throttled_during`` — cgroup CPU-throttle events across the run. Any throttling
      of ONE thread on a lease sold as >=1 vCPU means you're being capped below spec.
    * ``throttled_usec_during`` — total microseconds throttled across the run (how
      LONG you were capped, not just how many times).
    * ``steal_pct`` — host CPU stolen by the hypervisor over the window. >0 means the
      host is a VM sharing cores (bare-metal is ~0).
    * ``cpu_psi_load`` — CPU pressure (PSI ``some avg10``) measured under load.

    Returns the derived metrics that were computable, plus ``under_delivering`` (bool)
    and human-readable ``reasons``. Absent inputs are simply omitted — never guessed.
    """

    def _delta(pre_key: str, post_key: str) -> int | None:
        pre, post = results.get(pre_key), results.get(post_key)
        try:
            return int(post) - int(pre)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    out: dict = {}
    thr = _delta("thr_pre", "thr_post")
    thrus = _delta("thrus_pre", "thrus_post")
    steal_d = _delta("steal_pre", "steal_post")
    tot_d = _delta("cputot_pre", "cputot_post")
    psi = _leading_number(results.get("cpu_psi_load"))

    if thr is not None:
        out["throttled_during"] = thr
    if thrus is not None:
        out["throttled_usec_during"] = thrus
    steal_pct = None
    if steal_d is not None and tot_d and tot_d > 0:
        steal_pct = round(steal_d / tot_d * 100, 2)
        out["steal_pct"] = steal_pct
    if psi is not None:
        out["cpu_psi_load"] = psi

    reasons: list[str] = []
    if thr and thr > 0:
        reasons.append(f"CPU-throttled {thr}x during a single-threaded run")
    if steal_pct is not None and steal_pct > _STEAL_PCT_LIMIT:
        reasons.append(f"host CPU steal {steal_pct}%")
    if psi is not None and psi > _CPU_PSI_LOAD_LIMIT:
        reasons.append(f"CPU pressure {psi} under load")
    out["under_delivering"] = bool(reasons)
    out["reasons"] = reasons
    return out


def stability(results: dict[str, str]) -> dict:
    """How steady is the CPU across back-to-back runs? The "consistently good" signal.

    A single spot benchmark can't tell a provider that's fast ONCE from one that
    holds up — a host that peaks high then degrades (thermal, a neighbor ramping up)
    is worse than a steady one. From the ``cpu_samples`` the probe collected, this
    reports mean / min / max and the coefficient of variation (spread as a % of the
    mean), and flags ``unstable`` when the swing exceeds the floor. High variance is
    also the fingerprint of a noisy neighbor on an oversubscribed host.

    Returns ``{}`` when there are fewer than two usable samples — nothing to compare.
    """
    vals: list[float] = []
    for tok in (results.get("cpu_samples") or "").split():
        try:
            vals.append(float(tok))
        except ValueError:
            continue
    if len(vals) < 2:
        return {}
    mean = statistics.fmean(vals)
    cv = round(statistics.pstdev(vals) / mean * 100, 1) if mean else None
    out: dict = {
        "cpu_samples": vals,
        "cpu_mean": round(mean, 1),
        "cpu_min": min(vals),
        "cpu_max": max(vals),
        "cpu_cv_pct": cv,
    }
    out["unstable"] = cv is not None and cv > _CV_LIMIT
    return out


def build_json_record(dseq: str, provider: str, results: dict[str, str]) -> dict:
    """Merge the remote probe's metrics with locally-known, trusted metadata.

    ``results`` comes from remote ``BENCH-`` output, so the trusted fields
    (``dseq`` / ``provider`` / ``complete``) are spread LAST: a hostile or buggy
    probe emitting ``BENCH-provider=`` / ``BENCH-dseq=`` / ``BENCH-complete=`` must
    not be able to shadow the values we actually know. Extracted from the CLI so
    this invariant is unit-testable without driving the whole deploy/exec flow.
    """
    return {**results, "dseq": dseq, "provider": provider, "complete": is_complete(results)}


def format_results(provider: str, results: dict[str, str]) -> str:
    """A compact human-readable block, grouped by dimension."""
    if not results:
        return f"{provider}: no benchmark output"
    groups: list[tuple[str, list[str]]] = [
        ("cpu", ["cpu_eps", "ncpu", "cpu_model", "cpu_mhz", "cpu_max"]),
        ("memory", ["mem_bw", "mem_limit"]),
        ("disk", ["disk_write", "disk_read"]),
        ("network", ["rtt_1.1.1.1", "rtt_8.8.8.8"]),
        ("contention", ["cpu_psi", "mem_psi", "io_psi", "throttled"]),
        ("host", ["kernel"]),
    ]
    lines = [f"== {provider} =="]
    if not is_complete(results):
        lines.append("  WARNING: incomplete (no BENCH-done) — treat as a partial sample")
    for title, keys in groups:
        present = [(k, results[k]) for k in keys if k in results]
        if present:
            lines.append(f"  {title:11s} " + "  ".join(f"{k}={v}" for k, v in present))

    # Resource-honesty verdict: is the provider delivering the CPU it sold?
    fid = resource_fidelity(results)
    measured = [(k, fid[k]) for k in ("throttled_during", "steal_pct", "cpu_psi_load") if k in fid]
    if measured:
        detail = "  ".join(f"{k}={v}" for k, v in measured)
        if fid["under_delivering"]:
            verdict = f"UNDER-DELIVERING ({'; '.join(fid['reasons'])})"
        else:
            verdict = "OK (delivering the CPU it sold)"
        lines.append(f"  {'fidelity':11s} {verdict}  {detail}")

    # Stability: is the CPU consistently good, or fast-once-then-degrades?
    stab = stability(results)
    if stab:
        tag = "UNSTABLE" if stab["unstable"] else "steady"
        lines.append(
            f"  {'stability':11s} {tag} (cv={stab['cpu_cv_pct']}% over {len(stab['cpu_samples'])} "
            f"runs)  mean={stab['cpu_mean']}  min={stab['cpu_min']}  max={stab['cpu_max']}"
        )
    return "\n".join(lines)
