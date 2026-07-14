#!/usr/bin/env python3
"""Aggregate smoke-test latency telemetry into per-(provider, feature) stats.

Reads the JSONL emitted by ``smoke_providers --telemetry-file`` (one line per
provider/feature/run: ``{ts, version, provider, feature, outcome, latency_ms,
dseq}``) and reports, per (provider, feature): sample count, success rate, and
latency percentiles (p50/p95/p99) over the PASS samples.

Why percentiles and not mean+3σ: readiness/ingress latency is heavy-tailed
(one run's ingress was 0.4s, another's 129s). The mean+σ of a long-tailed
distribution is dominated by outliers and assumes a normal shape it doesn't
have; percentiles make no distributional assumption and answer the real
question directly ("how long to wait to cover 99% of legit runs"). Regressions
are better caught with robust limits (median ± k·MAD) than 3σ — reported here
so a threshold can later be set from real data instead of a guess.

Usage:
    uv run python -m just_akash.analyze_telemetry PATH.jsonl
    uv run python -m just_akash.analyze_telemetry PATH.jsonl --check --min-samples 20
"""

from __future__ import annotations

import argparse
import json
import sys

# Feature -> the configured cap (ms) it is bounded by, so the report can flag a
# cap that is getting tight relative to observed p99. Imported lazily to avoid a
# hard dependency when analyzing a file on a machine without the env set.
try:
    from .smoke_providers import INGRESS_CAP_S, READY_CAP_S

    _FEATURE_CAP_MS = {
        "ready": READY_CAP_S * 1000,
        "ingress": INGRESS_CAP_S * 1000,
        "update": INGRESS_CAP_S * 1000,
    }
except Exception:  # noqa: BLE001 — analysis must work standalone
    _FEATURE_CAP_MS = {}

DEFAULT_SLO = 0.95  # success-rate floor for --check
_CAP_TIGHT_FRACTION = 0.7  # flag p99 within this fraction of the cap


def load_records(path: str) -> list[dict]:
    """Parse a JSONL telemetry file, skipping blank/corrupt lines."""
    records: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
    return records


def _percentile(values: list[float], q: float) -> float | None:
    """The q-th percentile (0-100) by linear interpolation between ranks.

    Matches numpy's default ("linear") method so results line up with any later
    numpy-based analysis. Returns None for an empty input.
    """
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    idx = (len(s) - 1) * (q / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return float(s[lo] + (s[hi] - s[lo]) * frac)


def _median(values: list[float]) -> float:
    """Median of a NON-EMPTY list (always a float, unlike _percentile's None)."""
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _median_and_mad(values: list[float]) -> tuple[float, float] | None:
    """(median, MAD) — median absolute deviation, an outlier-robust spread. A
    control limit of median + k·MAD resists the fat tail that inflates σ."""
    if not values:
        return None
    med = _median(values)
    mad = _median([abs(v - med) for v in values])
    return med, mad


def aggregate(records: list[dict]) -> dict[tuple[str, str], dict]:
    """Group records by (provider, feature) into summary stats.

    Latency stats are computed over PASS samples with a numeric latency only —
    a FAIL/NO-BID latency is the time-to-failure, not the feature's real cost,
    so mixing them would corrupt the percentiles.
    """
    groups: dict[tuple[str, str], dict] = {}
    for r in records:
        provider = r.get("provider")
        feature = r.get("feature")
        if not provider or not feature:
            continue
        key = (provider, feature)
        g = groups.setdefault(key, {"count": 0, "pass": 0, "fail": 0, "other": 0, "latencies": []})
        g["count"] += 1
        outcome = r.get("outcome")
        if outcome == "PASS":
            g["pass"] += 1
            lat = r.get("latency_ms")
            if isinstance(lat, (int, float)):
                g["latencies"].append(float(lat))
        elif outcome == "FAIL":
            g["fail"] += 1
        else:
            g["other"] += 1
    for key, g in groups.items():
        lats = g["latencies"]
        # Success rate is over ATTEMPTS (pass + fail), not total records: NO-BID
        # and "-" (feature never reached) are explicitly not failures in
        # smoke_providers, so counting them in the denominator would understate a
        # provider that simply didn't bid. None when there were no real attempts.
        g["attempts"] = g["pass"] + g["fail"]
        g["pass_rate"] = (g["pass"] / g["attempts"]) if g["attempts"] else None
        g["n_lat"] = len(lats)  # latency-sample count (PASS with a number)
        g["p50"] = _percentile(lats, 50)
        g["p95"] = _percentile(lats, 95)
        g["p99"] = _percentile(lats, 99)
        g["min"] = min(lats) if lats else None
        g["max"] = max(lats) if lats else None
        g["median_mad"] = _median_and_mad(lats)
        g["cap_ms"] = _FEATURE_CAP_MS.get(key[1])
    return groups


def _fmt_ms(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v / 1000:.1f}s" if v >= 1000 else f"{int(v)}ms"


def format_report(groups: dict[tuple[str, str], dict]) -> str:
    """A human-readable table, sorted by provider then feature."""
    lines = [
        "(att = pass+fail attempts; NO-BID / '-' excluded from the rate)",
        f"{'provider':<14} {'feature':<9} {'att':>3} {'pass%':>6} "
        f"{'p50':>8} {'p95':>8} {'p99':>8} {'max':>8}  flags",
        "-" * 82,
    ]
    for (provider, feature), g in sorted(groups.items()):
        flags = []
        pr = g["pass_rate"]
        if g["attempts"] and pr is not None and pr < DEFAULT_SLO:
            flags.append(f"LOW-PASS({g['pass']}/{g['attempts']})")
        cap = g.get("cap_ms")
        p99 = g.get("p99")
        if cap and p99 is not None and p99 > _CAP_TIGHT_FRACTION * cap:
            flags.append(f"p99>{int(_CAP_TIGHT_FRACTION * 100)}%-of-cap({_fmt_ms(cap)})")
        pass_str = f"{pr * 100:>5.0f}%" if pr is not None else "  n/a"
        lines.append(
            f"{provider[:14]:<14} {feature:<9} {g['attempts']:>3} {pass_str} "
            f"{_fmt_ms(g['p50']):>8} {_fmt_ms(g['p95']):>8} "
            f"{_fmt_ms(g['p99']):>8} {_fmt_ms(g['max']):>8}  {' '.join(flags)}"
        )
    return "\n".join(lines)


def slo_breaches(
    groups: dict[tuple[str, str], dict], min_samples: int, slo: float
) -> list[tuple[str, str, float, int]]:
    """(provider, feature, pass_rate, attempts) for groups with ENOUGH real
    attempts (pass+fail, so NO-BID/- never trip it) and a pass rate below the
    SLO. The min_samples gate stops a small-sample blip from tripping — you
    cannot judge reliability from noise."""
    out = []
    for (provider, feature), g in sorted(groups.items()):
        pr = g["pass_rate"]
        if g["attempts"] >= min_samples and pr is not None and pr < slo:
            out.append((provider, feature, pr, g["attempts"]))
    return out


def parse_thresholds(spec: str) -> dict[str, float]:
    """Parse ``"ready=30000,ingress=10000"`` into ``{feature: max_ms}``. Raises
    ValueError on a malformed entry so a typo in CI config fails loudly rather
    than silently disabling the latency gate."""
    out: dict[str, float] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        feat, sep, ms = part.partition("=")
        if not sep or not feat.strip():
            raise ValueError(f"bad --max-p95 entry {part!r} (expected feature=ms)")
        out[feat.strip()] = float(ms)
    return out


def latency_breaches(
    groups: dict[tuple[str, str], dict],
    thresholds_ms: dict[str, float],
    min_samples: int,
) -> list[tuple[str, str, float, float]]:
    """(provider, feature, p95_ms, threshold_ms) for groups whose p95 latency
    exceeds the per-feature threshold, with enough latency samples.

    Keys off the p95 percentile over accrued runs — a provider is "too slow"
    when it is CONSISTENTLY slow, not on a single unlucky run — so this must run
    against the accrued dataset, not one run. NO-BID/- rows carry no latency and
    never enter the percentile, so they can't trip it."""
    out = []
    for (provider, feature), g in sorted(groups.items()):
        thr = thresholds_ms.get(feature)
        p95 = g.get("p95")
        if thr is not None and p95 is not None and g["n_lat"] >= min_samples and p95 > thr:
            out.append((provider, feature, p95, thr))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Aggregate smoke-test latency telemetry.")
    ap.add_argument("path", help="Path to the telemetry JSONL file")
    ap.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any feature with >= --min-samples is below --slo",
    )
    ap.add_argument(
        "--min-samples", type=int, default=20, help="Min samples before --check judges"
    )
    ap.add_argument("--slo", type=float, default=DEFAULT_SLO, help="Success-rate floor (0-1)")
    ap.add_argument(
        "--max-p95",
        metavar="SPEC",
        default="",
        help='Per-feature p95 latency ceiling in ms, e.g. "ready=45000,ingress=15000". '
        "With --check, a provider whose p95 for a feature exceeds it (over enough "
        "runs) fails -- the 'too slow, not broken' gate. Set from accrued p99+margin.",
    )
    args = ap.parse_args(argv)

    try:
        records = load_records(args.path)
    except OSError as e:
        print(f"Error: cannot read {args.path}: {e}", file=sys.stderr)
        return 2
    if not records:
        print(f"No telemetry records in {args.path} yet.")
        return 0

    groups = aggregate(records)
    runs = len({r.get("ts") for r in records})
    print(
        f"{len(records)} records across ~{runs} run(s), {len(groups)} (provider,feature) pairs.\n"
    )
    print(format_report(groups))

    if args.check:
        failed = False
        breaches = slo_breaches(groups, args.min_samples, args.slo)
        if breaches:
            failed = True
            print(f"\nRELIABILITY breach (>= {args.min_samples} samples, < {args.slo:.0%} pass):")
            for provider, feature, rate, count in breaches:
                print(f"  {provider} {feature}: {rate:.0%} over {count} runs")

        try:
            thresholds = parse_thresholds(args.max_p95)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2
        if thresholds:
            slow = latency_breaches(groups, thresholds, args.min_samples)
            if slow:
                failed = True
                print(f"\nTOO SLOW — p95 over ceiling (>= {args.min_samples} latency samples):")
                for provider, feature, p95, thr in slow:
                    print(f"  {provider} {feature}: p95 {_fmt_ms(p95)} > {_fmt_ms(thr)}")

        if failed:
            return 1
        print(f"\nCHECK OK (pass rate >= {args.slo:.0%}; p95 within ceilings where set).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
