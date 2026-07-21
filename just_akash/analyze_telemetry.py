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
import math
import sys
from datetime import datetime, timezone

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

# issue #85 shim survey. Records below this version predate the `exit_code_shapes`
# instrumentation: their silence means "not measured", not "clean", so they can
# never count toward the streak. Bump ONLY if the instrumentation itself changes.
SHIM_SURVEY_MIN_VERSION = "1.37.0"
SHIM_REQUIRED_CLEAN_DAYS = 30  # the removal condition agreed in issue #85


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
        g = groups.setdefault(
            key,
            {"count": 0, "pass": 0, "fail": 0, "lease_down": 0, "other": 0, "latencies": []},
        )
        g["count"] += 1
        outcome = r.get("outcome")
        if outcome == "PASS":
            g["pass"] += 1
            lat = r.get("latency_ms")
            if isinstance(lat, (int, float)):
                g["latencies"].append(float(lat))
        elif outcome == "LEASE-DOWN":
            # The provider accepted the bid, then the lease died on-chain. v1.22.0
            # decided this is NON-GATING **fleet-wide** — it is always provider infra,
            # never a just-akash bug (smoke_providers skips it in the gate). Counting
            # it as a `fail` here CONTRADICTED that decision and deflated the reported
            # pass rate, so it gets its own counter and stays OUT of the pass/fail
            # denominator. The rate then answers the question that's actually
            # actionable: "when the lease was up, did the feature work?"
            g["lease_down"] += 1
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
        # Surface LEASE-DOWN in the report. It is excluded from the pass/fail rate
        # (non-gating since v1.22.0), but it IS provider-health signal — without this
        # flag a provider could show 100% pass over a tiny `att` denominator while
        # having lease-downed most of its runs, and the report would look pristine.
        if g.get("lease_down"):
            flags.append(f"LEASE-DOWN×{g['lease_down']}")
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
        try:
            val = float(ms)
        except ValueError:
            raise ValueError(
                f"bad --max-p95 entry {part!r}: {ms.strip()!r} is not a number of ms"
            ) from None
        # Reject NaN/inf/<=0: a non-finite ceiling silently disables the gate
        # (p95 > nan is always False), which is the opposite of what was asked.
        if not math.isfinite(val) or val <= 0:
            raise ValueError(
                f"bad --max-p95 entry {part!r}: ceiling must be a positive, finite ms"
            )
        out[feat.strip()] = val
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
        n_lat = g.get("n_lat", 0)
        if thr is not None and p95 is not None and n_lat >= min_samples and p95 > thr:
            out.append((provider, feature, p95, thr))
    return out


def _parse_ts(ts: object) -> float | None:
    """A telemetry ``ts`` as unix epoch seconds, or None if unparsable.

    Defined here rather than imported from ``prometheus_exporter`` because that
    module imports THIS one — the reverse import would be a cycle.
    """
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    if not isinstance(ts, str):
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def shim_survey(records: list[dict], required_days: int = SHIM_REQUIRED_CLEAN_DAYS) -> dict:
    """Evidence for the issue-#85 removal condition: zero null/missing ``exit_code``
    occurrences across all active providers over ``required_days`` consecutive days.

    Only records at or above :data:`SHIM_SURVEY_MIN_VERSION` are evidence. Older rows
    predate the instrumentation, so their silence means "not measured", not "clean" —
    counting them would let the streak start in the past and retire the shim on
    evidence that was never collected. That distinction is the whole survey.

    Returns ``{eligible, occurrences, providers, provider_hits, first_ts, last_ts,
    last_hit_ts, clean_days, required_days, clean}``. ``clean`` is the verdict: the
    condition holds and the shim can go.
    """
    floor = _version_key(SHIM_SURVEY_MIN_VERSION)
    eligible = [r for r in records if _version_key(r.get("version")) >= floor]
    stamps = [t for t in (_parse_ts(r.get("ts")) for r in eligible) if t is not None]
    provider_hits: dict[str, int] = {}
    hit_stamps: list[float] = []
    for r in eligible:
        # A list of shapes; absent/empty means the shim never fired for that probe.
        if not r.get("exit_code_shapes"):
            continue
        provider_hits[str(r.get("provider") or "?")] = (
            provider_hits.get(str(r.get("provider") or "?"), 0) + 1
        )
        ts = _parse_ts(r.get("ts"))
        if ts is not None:
            hit_stamps.append(ts)

    last_hit = max(hit_stamps) if hit_stamps else None
    last_ts = max(stamps) if stamps else None
    first_ts = min(stamps) if stamps else None
    # Clean days run from the last occurrence — or from the first instrumented
    # record when there has never been one. Measured against the newest record
    # rather than "now" so the verdict is a property of the DATA, reproducible
    # whenever it is re-run.
    since = last_hit if last_hit is not None else first_ts
    clean_days = 0.0
    if since is not None and last_ts is not None:
        clean_days = max(0.0, (last_ts - since) / 86400.0)
    return {
        "eligible": len(eligible),
        "occurrences": sum(provider_hits.values()),
        "providers": sorted({str(r.get("provider")) for r in eligible if r.get("provider")}),
        "provider_hits": provider_hits,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "last_hit_ts": last_hit,
        "clean_days": clean_days,
        "required_days": required_days,
        "clean": not provider_hits and clean_days >= required_days,
    }


def _fmt_day(ts: float | None) -> str:
    if ts is None:
        return "?"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def format_shim_survey(survey: dict) -> str:
    """Human report for :func:`shim_survey` — the answer to 'can the shim go yet?'."""
    lines = [
        f"SHIM SURVEY (issue #85) — null/missing exit_code, "
        f"instrumented records only (>= v{SHIM_SURVEY_MIN_VERSION})",
    ]
    if not survey["eligible"]:
        lines.append(
            f"  no instrumented records yet — the survey starts once a smoke run on "
            f"v{SHIM_SURVEY_MIN_VERSION}+ lands. VERDICT: KEEP THE SHIM."
        )
        return "\n".join(lines)
    lines.append(
        f"  {survey['eligible']} record(s) from {_fmt_day(survey['first_ts'])} "
        f"to {_fmt_day(survey['last_ts'])}, "
        f"{len(survey['providers'])} provider(s): {', '.join(survey['providers']) or '-'}"
    )
    if survey["provider_hits"]:
        lines.append(f"  occurrences: {survey['occurrences']}  (shim is LOAD-BEARING)")
        for provider, n in sorted(survey["provider_hits"].items(), key=lambda kv: -kv[1]):
            lines.append(f"    {provider}: {n}")
        lines.append(f"  most recent: {_fmt_day(survey['last_hit_ts'])}")
        lines.append(
            "  VERDICT: KEEP THE SHIM — real providers still send these frames. "
            "This is the evidence issue #85 wanted; decide the right semantics, "
            "do not just delete it."
        )
        return "\n".join(lines)
    lines.append(f"  occurrences: 0 over {survey['clean_days']:.1f} clean day(s)")
    if survey["clean"]:
        lines.append(
            f"  VERDICT: REMOVABLE — {survey['required_days']} consecutive clean days met. "
            "Confirm the provider list above covers the active smoke inventory, then "
            "follow the removal steps in issue #85."
        )
    else:
        remaining = survey["required_days"] - survey["clean_days"]
        lines.append(
            f"  VERDICT: NOT YET — {remaining:.1f} more clean day(s) needed "
            f"({survey['required_days']} required)."
        )
    return "\n".join(lines)


def _version_key(v: object) -> tuple[int, ...]:
    """Sortable key for a semver-ish string. Unparseable/missing sorts LOWEST so an
    unversioned legacy record is dropped by any --min-version floor rather than
    silently kept."""
    parts: list[int] = []
    for chunk in str(v or "").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            return (-1,)
        parts.append(int(digits))
    return tuple(parts) if parts else (-1,)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Aggregate smoke-test latency telemetry.")
    ap.add_argument("path", help="Path to the telemetry JSONL file")
    ap.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero ONLY on a latency breach (p95 over a --max-p95 ceiling, "
        "over >= --min-samples PASS-latency samples). Reliability breaches are "
        "PRINTED but never gate: this accrued view cannot classify them (it has no "
        "fail_mode/in_pod_marker/eventual), and the per-run smoke gate already owns "
        "that decision with the full diagnostic context.",
    )
    ap.add_argument(
        "--min-version",
        metavar="VER",
        default="",
        help='Ignore records older than this just-akash version, e.g. "1.17.0". A fixed '
        "bug's old failures cannot recur, so leaving them in understates a provider "
        "(e.g. exec reads 96%% across all versions but 100%% since the v1.17.0 fix).",
    )
    ap.add_argument(
        "--quarantine",
        metavar="PROVIDERS",
        default="",
        help="Comma-separated providers whose breaches must not gate — their failures "
        "are known provider infra (mirrors SMOKE_QUARANTINE_PROVIDERS in the smoke "
        "job). They are still measured and printed, just never fail the check.",
    )
    ap.add_argument(
        "--shim-survey",
        action="store_true",
        help="Report the issue-#85 compatibility-shim survey (null/missing exit_code "
        "occurrences per provider, and whether the 30-consecutive-clean-day removal "
        "condition is met) instead of the latency report. Never gates: the verdict is "
        "advice to a human, and removing the shim is a deliberate breaking change.",
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

    if args.shim_survey:
        # Deliberately BEFORE the --min-version filter. The survey owns its own
        # instrumentation floor (SHIM_SURVEY_MIN_VERSION); letting a caller's
        # --min-version pre-filter the input could only ever DROP instrumented
        # records, shortening the observed clean streak or hiding an occurrence
        # — and the verdict decides whether a compatibility shim gets deleted.
        print(format_shim_survey(shim_survey(records)))
        return 0

    if args.min_version:
        floor = _version_key(args.min_version)
        kept = [r for r in records if _version_key(r.get("version")) >= floor]
        print(
            f"version filter >= {args.min_version}: kept {len(kept)} of {len(records)} record(s)."
        )
        records = kept
        if not records:
            print("No records at or above that version yet.")
            return 0

    quarantined = {p.strip() for p in args.quarantine.split(",") if p.strip()}
    if quarantined:
        print(f"quarantined (measured, never gating): {', '.join(sorted(quarantined))}")

    groups = aggregate(records)
    runs = len({r.get("ts") for r in records})
    print(
        f"{len(records)} records across ~{runs} run(s), {len(groups)} (provider,feature) pairs.\n"
    )
    print(format_report(groups))

    if args.check:
        failed = False
        # Reliability is INFORMATIONAL here and must never gate. This accrued view
        # keys only on `outcome` -- it has none of the fail_mode/in_pod_marker/
        # eventual context that smoke_providers._is_reliability_failure needs, so it
        # cannot tell a tooling regression on a HEALTHY lease (which SHOULD gate, and
        # which the per-run smoke gate already catches) from provider infra the
        # project deliberately demoted (LEASE-DOWN fleet-wide in v1.22.0, quarantined
        # providers in v1.21.0). Gating on it here would red CI on exactly those.
        # Structural, not config: there is no flag that turns this into a gate.
        breaches = slo_breaches(groups, args.min_samples, args.slo)
        if breaches:
            print(
                f"\nRELIABILITY below {args.slo:.0%} (>= {args.min_samples} attempts) "
                "— informational, NOT gating (the smoke run owns reliability):"
            )
            for provider, feature, rate, count in breaches:
                print(f"  {provider} {feature}: {rate:.0%} over {count} attempts")

        try:
            thresholds = parse_thresholds(args.max_p95)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2
        if thresholds:
            slow = latency_breaches(groups, thresholds, args.min_samples)
            # A quarantined provider being slow is the same known infra we already
            # decided not to gate on -- measure and print it, never fail on it.
            gating = [b for b in slow if b[0] not in quarantined]
            if slow:
                print(f"\nTOO SLOW — p95 over ceiling (>= {args.min_samples} latency samples):")
                for provider, feature, p95, thr in slow:
                    tag = "  (quarantined — not gating)" if provider in quarantined else ""
                    print(f"  {provider} {feature}: p95 {_fmt_ms(p95)} > {_fmt_ms(thr)}{tag}")
            failed = bool(gating)
        else:
            # --check with no ceilings gates on NOTHING. That is a valid setup only
            # while thresholds are still being calibrated — but it looks identical to
            # a live gate that a misconfigured/empty SMOKE_LATENCY_SLO_P95 env var has
            # silently disabled. This whole telemetry effort exists because a gate
            # that quietly stopped gating went unnoticed, so say so LOUDLY rather than
            # print a green "CHECK OK" that hides it.
            print(
                "\nWARNING: --check is on but NO latency ceilings were provided "
                "(--max-p95 is empty) — the latency gate is DISABLED. If this run is "
                "meant to gate, SMOKE_LATENCY_SLO_P95 is unset or empty.",
                file=sys.stderr,
            )

        if failed:
            return 1
        status = "within ceilings" if thresholds else "GATE DISABLED — no ceilings set"
        print(f"\nCHECK OK ({status}; reliability is informational).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
