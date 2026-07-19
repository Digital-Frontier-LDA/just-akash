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
    USD, emitted with ``--with-credit`` so Grafana can trend/forecast burn-down.

Pure stdlib string output: it does NOT run a server. Point it at the accrued JSONL
and write the ``.prom`` file into the collector's ``textfile`` directory (an atomic
rename, per the collector contract), or emit to stdout for a pushgateway.

Because the counter is re-derived from the cumulative JSONL every run, its value is
monotonic as the file grows — exactly what ``rate()`` / ``increase()`` expect.

Usage:
    uv run just-akash export-metrics PATH.jsonl                     # -> stdout
    uv run just-akash export-metrics PATH.jsonl --output smoke.prom  # atomic file
    uv run just-akash export-metrics PATH.jsonl --with-credit        # + credit gauge
    uv run python -m just_akash.prometheus_exporter PATH.jsonl
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime

# Reuse, don't re-implement: the JSONL reader and the percentile logic already live
# in analyze_telemetry (the SLO/latency gate), so the exporter and the gate compute
# latency identically from the identical parse.
from .analyze_telemetry import _percentile, load_records

# Metric names (module constants so tests and any dashboard-as-code stay in sync).
OUTCOME_METRIC = "just_akash_smoke_outcome_total"
LATENCY_METRIC = "just_akash_smoke_latency_ms"
LAST_RUN_METRIC = "just_akash_smoke_last_run_timestamp"
CREDIT_METRIC = "just_akash_deploy_credit_usd"

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
    """A telemetry ``ts`` as unix epoch seconds, or None if unparseable.

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


def render_metrics(records: list[dict], *, credit: tuple[str, float | None] | None = None) -> str:
    """The full textfile-collector document: outcome counters, latency percentiles,
    last-run freshness, and (when ``credit`` is given) the deploy-credit gauge."""
    blocks = [
        render_outcome_counters(records),
        render_latency_summary(records),
        render_last_run(records),
    ]
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


def run(jsonl_path: str, *, output: str | None = None, with_credit: bool = False) -> int:
    """Read the smoke JSONL, render the metrics, write them, return an exit code."""
    try:
        records = load_records(jsonl_path)
    except OSError as e:
        print(f"Error: cannot read {jsonl_path}: {e}", file=sys.stderr)
        return 2
    credit = resolve_deploy_credit() if with_credit else None
    write_metrics(render_metrics(records, credit=credit), output)
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
        "--with-credit",
        action="store_true",
        help=f"Also emit {CREDIT_METRIC} from the on-chain deploy credit (needs AKASH_API_KEY).",
    )
    args = ap.parse_args(argv)
    return run(args.path, output=args.output, with_credit=args.with_credit)


if __name__ == "__main__":
    sys.exit(main())
