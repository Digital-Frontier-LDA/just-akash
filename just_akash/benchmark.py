#!/usr/bin/env python3
"""Benchmark what a provider ACTUALLY delivered for a lease.

The smoke test answers "does this provider work?" (feature matrix). This answers
"is the hardware any good?" — vCPU throughput, RAM bandwidth, disk I/O, WAN — so a
provider can be graded, not just pass/fail'd. It exists because the fleet shows a
stable ~6x spread in readiness latency between providers with the SAME declared
resources (1 vCPU / 1Gi / 5Gi), and pass/fail can't explain WHY.

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

# Bounded to stay far under the probe's 1 vCPU / 1Gi / 5Gi. See module docstring:
# exceeding the memory cgroup would OOM-kill the container mid-benchmark.
_MEM_MB = 256
_DISK_MB = 256
_CPU_SECONDS = 3

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
dd if=/dev/zero of="$BENCH_TMP" bs=1M count={_DISK_MB} conv=fdatasync 2>"$BENCH_TMP.w" >/dev/null
say disk_write "$(grep -oE '[0-9.]+ [MG]B/s' "$BENCH_TMP.w" 2>/dev/null | tail -1 || echo na)"
# iflag=direct bypasses the page cache, so this measures the DEVICE rather than the
# pages the write just populated (conv=fdatasync flushes but does NOT evict them).
# If the fs can't do O_DIRECT (overlayfs/tmpfs), dd errors and disk_read is honestly
# `na` — better than a cache-inflated number that would skew provider grading.
dd if="$BENCH_TMP" of=/dev/null bs=1M iflag=direct 2>"$BENCH_TMP.r" >/dev/null
say disk_read "$(grep -oE '[0-9.]+ [MG]B/s' "$BENCH_TMP.r" 2>/dev/null | tail -1 || echo na)"
rm -f "$BENCH_TMP" "$BENCH_TMP.w" "$BENCH_TMP.r"

# --- cpu + ram via sysbench (best-effort install; `na` if unavailable) ---
apk add --no-cache sysbench >/dev/null 2>&1
if command -v sysbench >/dev/null 2>&1; then
  cpu=$(sysbench cpu --time={_CPU_SECONDS} --threads=1 run 2>/dev/null \
        | grep -m1 'events per second' | cut -d: -f2 | tr -d ' ')
  say cpu_eps "${{cpu:-na}}"
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
    return "\n".join(lines)
