"""Did the cron actually fire, and how late? Measured, not assumed.

Stage 4 of the bid-probe migration turns on a 3h schedule and asks one question
before anyone is allowed to retire the in-cluster synthetic probe: is a GitHub
cron fit to be the fleet's ONLY bid-health trigger?

The honest answer needs two numbers, and neither can be guessed:

  DELIVERY   runs that actually happened / runs the schedule promised. GitHub
             documents scheduled workflows as best-effort and drops them under
             load; this repo's own history shows 69-92%.
  SKEW       how late a run started against its nominal slot. Measured here at
             24-211 minutes on a sibling workflow, with one 3h31m delay that
             exceeded a whole interval — i.e. a slot silently skipped.

Both feed the MTTD the operator is being asked to accept. Guessing them would
defeat the point of the stage.

    python -m just_akash.bidprobe_delivery [--jsonl bidprobe-runs.jsonl]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def nominal_slot(started: datetime, interval_h: int) -> datetime:
    """The scheduled slot a run was meant to occupy.

    A run is attributed to the most recent slot boundary at or before it
    started. Rounding to the NEAREST boundary would silently reattribute a run
    delayed past the halfway mark to the following slot — which would erase
    exactly the skew this measures and make a badly-late run look punctual.
    """
    slot_hour = (started.hour // interval_h) * interval_h
    return started.replace(hour=slot_hour, minute=0, second=0, microsecond=0)


def analyze(rows: list[dict], interval_h: int = 3) -> dict:
    """Delivery and skew over the scheduled runs in ``rows``.

    Dispatch runs are excluded from delivery: a human pressing the button says
    nothing about whether the cron fires, and counting them would inflate the
    number that the retirement decision rests on.
    """
    scheduled = [r for r in rows if r.get("event") == "schedule"]
    parsed = [(r, _parse_ts(r.get("started_at", ""))) for r in scheduled]
    parsed = [(r, t) for r, t in parsed if t is not None]

    skews: list[float] = []
    slots_seen: set[datetime] = set()
    for _row, started in parsed:
        slot = nominal_slot(started, interval_h)
        slots_seen.add(slot)
        skews.append((started - slot).total_seconds() / 60.0)

    if not parsed:
        return {
            "scheduled_runs": 0,
            "expected_slots": 0,
            "delivery_pct": None,
            "skew_p50_min": None,
            "skew_p95_min": None,
            "skew_max_min": None,
            "missed_slots": [],
        }

    first = min(t for _r, t in parsed)
    last = max(t for _r, t in parsed)
    expected: list[datetime] = []
    cur = nominal_slot(first, interval_h)
    end = nominal_slot(last, interval_h)
    while cur <= end:
        expected.append(cur)
        cur += timedelta(hours=interval_h)

    missed = sorted(s for s in expected if s not in slots_seen)
    skews.sort()

    def pct(p: float) -> float:
        if not skews:
            return 0.0
        idx = min(int(round((len(skews) - 1) * p)), len(skews) - 1)
        return round(skews[idx], 1)

    return {
        "scheduled_runs": len(parsed),
        "expected_slots": len(expected),
        "delivery_pct": round(100.0 * len(slots_seen) / len(expected), 1),
        "skew_p50_min": pct(0.50),
        "skew_p95_min": pct(0.95),
        "skew_max_min": round(max(skews), 1),
        "missed_slots": [s.isoformat() for s in missed],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--jsonl", default="bidprobe-runs.jsonl")
    ap.add_argument("--interval-hours", type=int, default=3)
    args = ap.parse_args(argv)

    try:
        with open(args.jsonl, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        print(f"no run log at {args.jsonl} — nothing measured yet", file=sys.stderr)
        return 1

    res = analyze(rows, args.interval_hours)
    if not res["scheduled_runs"]:
        print("no SCHEDULED runs recorded yet (dispatch runs do not count)")
        return 1

    print(f"scheduled runs : {res['scheduled_runs']} over {res['expected_slots']} slots")
    print(f"delivery       : {res['delivery_pct']}%")
    print(
        f"skew (min)     : p50={res['skew_p50_min']} "
        f"p95={res['skew_p95_min']} max={res['skew_max_min']}"
    )
    if res["missed_slots"]:
        print(f"missed slots   : {len(res['missed_slots'])}")
        for s in res["missed_slots"][:10]:
            print(f"  {s}")
    # A slot missed entirely is worse than a late one: at 3h cadence it doubles
    # the window in which a bid outage is invisible.
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
