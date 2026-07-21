#!/usr/bin/env python3
"""Render accrued smoke telemetry as Prometheus **textfile-collector** metrics.

The daily provider smoke (``smoke_providers``) already writes one JSON line per
(provider, feature, run) with the outcome (``PASS`` / ``FAIL`` / ``LEASE-DOWN`` /
``NO-BID`` / ``NO-CREDIT`` / ``NO-ROOM`` / ``-``) and its latency. Today that data
lives only as CI-log text and a ``telemetry`` git branch of JSONL — invisible to
Grafana. This module turns the SAME JSONL into the plain-text exposition format a
Prometheus ``node_exporter`` textfile collector (or a ``pushgateway``) scrapes, so
the natural errors we care about become first-class time series:

  * ``just_akash_smoke_outcome_total{provider,feature,outcome}`` — a counter, so
    ``no-credit`` (wallet out of funds), ``no-bid`` (no provider offered), and
    ``lease-down`` (the lease died on-chain) each become a trendable series.
  * ``just_akash_smoke_latency_ms{provider,feature,quantile}`` — p50/p95/p99 over
    the PASS samples (the same percentile logic ``analyze_telemetry`` gates on).
  * ``just_akash_smoke_last_run_timestamp`` — freshness / staleness alerting.
  * ``just_akash_deploy_credit_usd{account}`` — remaining Console deploy credit in
    USD, emitted with ``--with-credit`` (live chain query) or ``--credit-json``
    (a snapshot file from ``balance --check --json``, so an uncredentialed CI job
    can still render the gauge) so Grafana can trend/forecast burn-down.
  * ``just_akash_bench_*{provider}`` — with ``--benchmark BENCH.jsonl``, the
    hardware-quality grades (``smoke-benchmark.jsonl``): delivered CPU rate, the
    stability CV, throttle/steal (the resource-honesty signals), memory bandwidth.
    Gauges from each provider's LATEST complete grade — scraped daily, Prometheus's
    own history becomes the trend.

Pure stdlib string output: it does NOT run a server. Point it at the accrued JSONL
and write the ``.prom`` file into the collector's ``textfile`` directory (an atomic
rename, per the collector contract), or emit to stdout for a pushgateway.

Because the counter is re-derived from the cumulative JSONL every run, its value is
monotonic as the file grows — exactly what ``rate()`` / ``increase()`` expect.

Usage:
    uv run just-akash export-metrics PATH.jsonl                     # -> stdout
    uv run just-akash export-metrics PATH.jsonl --output smoke.prom  # atomic file
    uv run just-akash export-metrics PATH.jsonl --with-credit        # + credit gauge
    uv run just-akash export-metrics PATH.jsonl --benchmark B.jsonl  # + bench gauges
    uv run python -m just_akash.prometheus_exporter PATH.jsonl
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime

# Reuse, don't re-implement: the JSONL reader and the percentile logic already live
# in analyze_telemetry (the SLO/latency gate), so the exporter and the gate compute
# latency identically from the identical parse. Same for the benchmark verdicts:
# resource_fidelity/stability ARE the honesty/stability logic — the exporter only
# formats what they derive (_leading_number is their unit-string parser).
from .analyze_telemetry import _percentile, load_records
from .benchmark import _leading_number, is_complete, resource_fidelity, stability

# Metric names (module constants so tests and any dashboard-as-code stay in sync).
OUTCOME_METRIC = "just_akash_smoke_outcome_total"
LATENCY_METRIC = "just_akash_smoke_latency_ms"
LAST_RUN_METRIC = "just_akash_smoke_last_run_timestamp"
CREDIT_METRIC = "just_akash_deploy_credit_usd"

# Benchmark gauge families (hardware-quality grades from smoke-benchmark.jsonl).
# One sample per provider, from its latest COMPLETE grade.
BENCH_CPU_EPS_METRIC = "just_akash_bench_cpu_events_per_s"
BENCH_CPU_CV_METRIC = "just_akash_bench_cpu_cv_pct"
BENCH_CPU_UNSTABLE_METRIC = "just_akash_bench_cpu_unstable"
BENCH_THROTTLED_METRIC = "just_akash_bench_cpu_throttled_events"
BENCH_THROTTLED_USEC_METRIC = "just_akash_bench_cpu_throttled_usec"
BENCH_STEAL_METRIC = "just_akash_bench_steal_pct"
BENCH_PSI_METRIC = "just_akash_bench_cpu_psi_load"
BENCH_UNDER_DELIVERING_METRIC = "just_akash_bench_under_delivering"
BENCH_MEM_BW_METRIC = "just_akash_bench_mem_bandwidth_mib_s"
BENCH_LAST_RUN_METRIC = "just_akash_bench_last_run_timestamp"

# The percentiles emitted for the latency summary, as (q, prometheus-quantile-label).
_QUANTILES = ((50, "0.5"), (95, "0.95"), (99, "0.99"))

# A never-reached feature is recorded with a bare "-" outcome; give it a readable
# label so the series name means something in Grafana instead of an opaque dash.
_OUTCOME_LABEL_OVERRIDE = {"-": "unreached"}


def _escape_label_value(value: str) -> str:
    """Escape a Prometheus label value: backslash, double-quote, newline (per the
    exposition-format spec). Provider addresses / feature names are already safe, but
    escaping keeps a surprising value from producing a malformed line."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_num(value: float) -> str:
    """Format a metric value: an integral value as a plain int (``5``, not ``5.0``),
    otherwise its shortest round-trippable float repr (``3.97``). Both are valid
    Prometheus sample values."""
    f = float(value)
    return str(int(f)) if f.is_integer() else repr(f)


def _is_number(value: object) -> bool:
    """True for a real int/float latency — and NOT for bool (a bool is an int
    subclass, so ``True`` would otherwise sneak in as ``1`` ms)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _outcome_label(outcome: object) -> str:
    """Normalize a raw telemetry outcome into a lowercase label value so
    ``NO-CREDIT`` / ``NO-BID`` / ``LEASE-DOWN`` read as ``no-credit`` / ``no-bid`` /
    ``lease-down``; the bare ``-`` (never reached) becomes ``unreached``."""
    raw = str(outcome) if outcome is not None else "-"
    return _OUTCOME_LABEL_OVERRIDE.get(raw, raw.lower())


def render_outcome_counters(records: list[dict]) -> list[str]:
    """``just_akash_smoke_outcome_total`` counter lines, one per
    (provider, feature, outcome), sorted for deterministic output."""
    counts: Counter[tuple[str, str, str]] = Counter()
    for r in records:
        provider = r.get("provider")
        feature = r.get("feature")
        if not provider or not feature:
            continue
        outcome = _outcome_label(r.get("outcome"))
        counts[(str(provider), str(feature), outcome)] += 1
    lines = [
        f"# HELP {OUTCOME_METRIC} Smoke-test outcomes per provider/feature/outcome "
        "(cumulative over accrued telemetry).",
        f"# TYPE {OUTCOME_METRIC} counter",
    ]
    for (provider, feature, outcome), n in sorted(counts.items()):
        labels = (
            f'provider="{_escape_label_value(provider)}",'
            f'feature="{_escape_label_value(feature)}",'
            f'outcome="{_escape_label_value(outcome)}"'
        )
        lines.append(f"{OUTCOME_METRIC}{{{labels}}} {n}")
    return lines


def render_latency_summary(records: list[dict]) -> list[str]:
    """``just_akash_smoke_latency_ms`` p50/p95/p99 lines over PASS samples.

    Latency is measured only over PASS records with a numeric ``latency_ms`` — a
    FAIL/NO-BID latency is a time-to-failure, not the feature's real cost, so mixing
    it would corrupt the percentiles (same rule ``analyze_telemetry.aggregate`` uses).
    """
    latencies: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in records:
        if r.get("outcome") != "PASS":
            continue
        provider = r.get("provider")
        feature = r.get("feature")
        value = r.get("latency_ms")
        if not provider or not feature or not _is_number(value):
            continue
        latencies[(str(provider), str(feature))].append(float(value))  # type: ignore[arg-type]
    lines = [
        f"# HELP {LATENCY_METRIC} Smoke feature latency percentiles (ms) over PASS samples.",
        f"# TYPE {LATENCY_METRIC} gauge",
    ]
    for (provider, feature), values in sorted(latencies.items()):
        for q, quantile in _QUANTILES:
            pv = _percentile(values, q)
            if pv is None:
                continue
            labels = (
                f'provider="{_escape_label_value(provider)}",'
                f'feature="{_escape_label_value(feature)}",'
                f'quantile="{quantile}"'
            )
            lines.append(f"{LATENCY_METRIC}{{{labels}}} {_fmt_num(pv)}")
    return lines


def _parse_ts(ts: object) -> float | None:
    """A telemetry ``ts`` as unix epoch seconds, or None if unparsable.

    The smoke writer stamps ``datetime.now(timezone.utc).isoformat()`` (offset-aware,
    e.g. ``2026-07-19T07:00:00.5+00:00``); tolerate a trailing ``Z`` and a raw numeric
    epoch too, so an odd row never crashes the export."""
    if _is_number(ts):
        return float(ts)  # type: ignore[arg-type]
    if not isinstance(ts, str):
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def render_last_run(records: list[dict]) -> list[str]:
    """``just_akash_smoke_last_run_timestamp`` — the most recent run's epoch, for
    staleness alerting. Empty (no metric emitted) when no row carries a parseable ts."""
    epochs = [e for e in (_parse_ts(r.get("ts")) for r in records) if e is not None]
    if not epochs:
        return []
    return [
        f"# HELP {LAST_RUN_METRIC} Unix time of the most recent smoke run.",
        f"# TYPE {LAST_RUN_METRIC} gauge",
        f"{LAST_RUN_METRIC} {_fmt_num(max(epochs))}",
    ]


def render_deploy_credit_gauge(account: str, usd: float | None) -> list[str]:
    """``just_akash_deploy_credit_usd{account}`` — remaining Console deploy credit in
    USD, so Grafana can trend and forecast credit burn-down. When ``usd`` is None
    (credit could not be read) the family is declared but carries no sample."""
    lines = [
        f"# HELP {CREDIT_METRIC} Remaining Console deploy credit in USD (uact is USD-pegged).",
        f"# TYPE {CREDIT_METRIC} gauge",
    ]
    if usd is None:
        return lines
    labels = f'{{account="{_escape_label_value(account)}"}}' if account else ""
    lines.append(f"{CREDIT_METRIC}{labels} {_fmt_num(usd)}")
    return lines


def _normalize_grade(record: dict) -> dict:
    """A benchmark row reduced to what the verdict helpers can safely consume.

    The helpers (:func:`benchmark.resource_fidelity` / :func:`benchmark.stability` /
    ``_leading_number``) are written for the probe's native ``dict[str, str]``. The
    accrued JSONL is append-only and unprotected, so a row can carry a type the
    current writer never emits (a bare JSON number, a list — schema drift or a hand
    edit). One such row must degrade to "that field is unmeasured", never crash the
    whole export: numbers are stringified, anything else non-string is dropped.
    """
    out: dict = {}
    for k, v in record.items():
        if isinstance(v, str):
            out[k] = v
        elif _is_number(v):  # excludes bool
            # Fixed-point, never repr: repr(1e-05) is "1e-05", whose exponent
            # _leading_number does not parse — it would read as 1, a silently
            # wrong magnitude rather than a safe degradation.
            out[k] = format(v, "f") if isinstance(v, float) else str(v)
    return out


def _latest_complete_grade_per_provider(records: list[dict]) -> dict[str, dict]:
    """Each provider's most recent COMPLETE benchmark grade.

    "Latest" is by parsed ``ts``; a record with an unparseable ts falls back to its
    file position (later line wins), so a clock-less row can never shadow a properly
    stamped newer one but two clock-less rows still order by accrual. Incomplete
    grades (the exec was cut short — no ``BENCH-done=1``) are partial samples that
    must not be graded, so they never become the exported gauge.
    """
    latest: dict[str, tuple[float, int, dict]] = {}
    for idx, r in enumerate(records):
        provider = r.get("provider")
        if not provider or not isinstance(provider, str) or not is_complete(r):
            continue
        epoch = _parse_ts(r.get("ts")) or 0.0
        if provider not in latest or (epoch, idx) >= latest[provider][:2]:
            latest[provider] = (epoch, idx, r)
    return {p: rec for p, (_, _, rec) in latest.items()}


def render_benchmark_metrics(records: list[dict]) -> list[str]:
    """``just_akash_bench_*{provider}`` gauges from the hardware-quality grades.

    Renders each provider's latest complete grade through the SAME verdict logic the
    CLI report uses (:func:`benchmark.resource_fidelity` / :func:`benchmark.stability`)
    so the dashboard and the report can never disagree on what "throttled" or
    "unstable" means. The benchmark contract is that an unavailable metric is ABSENT,
    never zero — so each gauge is emitted only when its input was actually measured.
    """
    grades = _latest_complete_grade_per_provider(records)

    # metric -> (help text, [(provider, value)])
    families: dict[str, tuple[str, list[tuple[str, float]]]] = {
        BENCH_CPU_EPS_METRIC: (
            "Single-threaded CPU benchmark rate (events/s) from the latest complete grade.",
            [],
        ),
        BENCH_CPU_CV_METRIC: (
            "Coefficient of variation (%) across back-to-back CPU runs — the "
            "statistical-stability signal (high = noisy neighbour / oversubscribed).",
            [],
        ),
        BENCH_CPU_UNSTABLE_METRIC: (
            "1 when the CPU stability CV exceeded the instability floor.",
            [],
        ),
        BENCH_THROTTLED_METRIC: (
            "cgroup CPU-throttle events during a single-threaded run (any > 0 means "
            "the lease is capped below the vCPU it was sold as).",
            [],
        ),
        BENCH_THROTTLED_USEC_METRIC: (
            "Total microseconds CPU-throttled during the benchmark window.",
            [],
        ),
        BENCH_STEAL_METRIC: (
            "Host CPU steal (%) over the benchmark window (>0 = VM sharing cores).",
            [],
        ),
        BENCH_PSI_METRIC: (
            "CPU pressure (PSI some avg10) measured under load.",
            [],
        ),
        BENCH_UNDER_DELIVERING_METRIC: (
            "1 when the resource-honesty verdict flagged the provider as delivering "
            "less than the resources it sold.",
            [],
        ),
        BENCH_MEM_BW_METRIC: (
            "Memory write bandwidth (MiB/s) from the latest complete grade.",
            [],
        ),
        BENCH_LAST_RUN_METRIC: (
            "Unix time of the provider's latest complete benchmark grade.",
            [],
        ),
    }

    def _add(metric: str, provider: str, value: object) -> None:
        if _is_number(value):
            families[metric][1].append((provider, float(value)))  # type: ignore[arg-type]

    for provider, raw_rec in sorted(grades.items()):
        rec = _normalize_grade(raw_rec)
        try:
            fidelity = resource_fidelity(rec)
            stab = stability(rec)
            _add(BENCH_CPU_EPS_METRIC, provider, _leading_number(rec.get("cpu_eps")))
            _add(BENCH_CPU_CV_METRIC, provider, stab.get("cpu_cv_pct"))
            if "unstable" in stab:
                _add(BENCH_CPU_UNSTABLE_METRIC, provider, int(bool(stab["unstable"])))
            _add(BENCH_THROTTLED_METRIC, provider, fidelity.get("throttled_during"))
            _add(BENCH_THROTTLED_USEC_METRIC, provider, fidelity.get("throttled_usec_during"))
            _add(BENCH_STEAL_METRIC, provider, fidelity.get("steal_pct"))
            _add(BENCH_PSI_METRIC, provider, fidelity.get("cpu_psi_load"))
            # under_delivering is only meaningful when at least one honesty input was
            # measured — resource_fidelity returns bare {under_delivering: False,
            # reasons: []} even for an empty record, which must read as unmeasured.
            if any(k in fidelity for k in ("throttled_during", "steal_pct", "cpu_psi_load")):
                _add(
                    BENCH_UNDER_DELIVERING_METRIC,
                    provider,
                    int(bool(fidelity["under_delivering"])),
                )
            _add(BENCH_MEM_BW_METRIC, provider, _leading_number(rec.get("mem_bw")))
            _add(BENCH_LAST_RUN_METRIC, provider, _parse_ts(rec.get("ts")))
        except Exception as e:  # noqa: BLE001 — one poisoned row must never kill the export
            # The accrued file is re-read in FULL every run, so letting one bad row
            # raise would freeze smoke-metrics.prom permanently (the accrue job only
            # warns on a render failure). Skip the provider's grade and keep going.
            print(
                f"export-metrics: skipping unusable benchmark grade for {provider}: {e}",
                file=sys.stderr,
            )

    lines: list[str] = []
    for metric, (help_text, samples) in families.items():
        if not samples:
            continue
        lines.append(f"# HELP {metric} {help_text}")
        lines.append(f"# TYPE {metric} gauge")
        for provider, value in samples:
            lines.append(
                f'{metric}{{provider="{_escape_label_value(provider)}"}} {_fmt_num(value)}'
            )
    return lines


def load_credit_json(path: str) -> tuple[str, float | None] | None:
    """(account, deploy_credit_usd) from a ``balance --check --json`` snapshot file.

    Lets an uncredentialed job (the CI accrue step holds no AKASH_API_KEY by design)
    still render the credit gauge from a snapshot the credentialed smoke job wrote.
    Best-effort like resolve_deploy_credit: any problem logs to stderr and returns
    None so the rest of the metrics still render.
    """
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        account = payload["account"]
        usd = payload["deploy_credit_usd"]
        if not isinstance(account, str) or not _is_number(usd):
            raise ValueError("unexpected field types")
        return account, float(usd)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"export-metrics: unusable credit snapshot {path}: {e}", file=sys.stderr)
        return None


def render_metrics(
    records: list[dict],
    *,
    credit: tuple[str, float | None] | None = None,
    benchmark_records: list[dict] | None = None,
) -> str:
    """The full textfile-collector document: outcome counters, latency percentiles,
    last-run freshness, and — when given — the deploy-credit gauge and the
    hardware-benchmark gauges."""
    blocks = [
        render_outcome_counters(records),
        render_latency_summary(records),
        render_last_run(records),
    ]
    if benchmark_records:
        blocks.append(render_benchmark_metrics(benchmark_records))
    if credit is not None:
        account, usd = credit
        blocks.append(render_deploy_credit_gauge(account, usd))
    lines: list[str] = []
    for block in blocks:
        lines.extend(block)
    return "\n".join(lines) + "\n"


def resolve_deploy_credit(api_key: str | None = None) -> tuple[str, float | None] | None:
    """(account, deploy_credit_usd) for the API-key account, or None on any failure.

    Best-effort by design: the credit gauge must never sink the whole export. Reads
    the account address from the Console JWT and the remaining credit straight off the
    public chain (the same path ``balance`` uses); a missing key or an LCD hiccup logs
    to stderr and returns None so the metrics still render without the credit series."""
    key = api_key or os.environ.get("AKASH_API_KEY")
    if not key:
        print(
            "export-metrics: --with-credit needs AKASH_API_KEY; skipping credit gauge",
            file=sys.stderr,
        )
        return None
    from . import chain
    from .api import AkashConsoleAPI

    try:
        client = AkashConsoleAPI(key)
        address = client.account_address()
        credit = chain.deploy_credit(address)
        # uact (Akash Credit Token) is the USD-pegged Console deploy currency.
        usd = chain.usd_estimate("uact", credit.get("uact", 0))
        return address, usd
    except RuntimeError as e:
        print(f"export-metrics: deploy-credit query failed: {e}", file=sys.stderr)
        return None


def write_metrics(text: str, output: str | None = None) -> None:
    """Write ``text`` to ``output`` (atomically, per the textfile-collector contract:
    write a temp file in the same dir then rename), or to stdout when no path is
    given. The atomic rename stops Prometheus from scraping a half-written file."""
    if not output:
        sys.stdout.write(text)
        return
    out_dir = os.path.dirname(os.path.abspath(output)) or "."
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".metrics-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, output)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def run(
    jsonl_path: str,
    *,
    output: str | None = None,
    with_credit: bool = False,
    benchmark_path: str | None = None,
    credit_json: str | None = None,
) -> int:
    """Read the smoke JSONL, render the metrics, write them, return an exit code."""
    try:
        records = load_records(jsonl_path)
    except OSError as e:
        print(f"Error: cannot read {jsonl_path}: {e}", file=sys.stderr)
        return 2
    benchmark_records: list[dict] | None = None
    if benchmark_path:
        # Best-effort: a missing/unreadable grades file must not sink the primary
        # latency/outcome export (the benchmark stream is optional by contract).
        try:
            benchmark_records = load_records(benchmark_path)
        except OSError as e:
            print(
                f"export-metrics: skipping benchmark file {benchmark_path}: {e}",
                file=sys.stderr,
            )
    if credit_json:
        credit = load_credit_json(credit_json)
    elif with_credit:
        credit = resolve_deploy_credit()
    else:
        credit = None
    write_metrics(
        render_metrics(records, credit=credit, benchmark_records=benchmark_records), output
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Render smoke telemetry JSONL as Prometheus textfile metrics."
    )
    ap.add_argument("path", help="Path to the smoke telemetry JSONL file")
    ap.add_argument(
        "--output",
        default=None,
        metavar="FILE",
        help="Write metrics atomically to FILE (a .prom in the collector's textfile "
        "dir); default is stdout.",
    )
    ap.add_argument(
        "--benchmark",
        default=None,
        metavar="FILE",
        help="Also render the hardware-benchmark gauges from this smoke-benchmark.jsonl.",
    )
    credit_group = ap.add_mutually_exclusive_group()
    credit_group.add_argument(
        "--with-credit",
        action="store_true",
        help=f"Also emit {CREDIT_METRIC} from the on-chain deploy credit (needs AKASH_API_KEY).",
    )
    credit_group.add_argument(
        "--credit-json",
        default=None,
        metavar="FILE",
        help=f"Also emit {CREDIT_METRIC} from a `balance --check --json` snapshot file "
        "(no API key needed).",
    )
    args = ap.parse_args(argv)
    return run(
        args.path,
        output=args.output,
        with_credit=args.with_credit,
        benchmark_path=args.benchmark,
        credit_json=args.credit_json,
    )


if __name__ == "__main__":
    sys.exit(main())
