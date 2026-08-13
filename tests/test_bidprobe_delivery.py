"""Delivery/skew measurement — the numbers the retirement decision rests on.

If these are wrong the operator is told a GitHub cron is more reliable than it
is, and the in-cluster probe gets retired on a false premise.
"""

from __future__ import annotations

from datetime import datetime, timezone

from just_akash.bidprobe_delivery import analyze, nominal_slot


def _run(started, event="schedule"):
    return {"event": event, "started_at": started, "run_id": "1"}


def test_perfect_delivery():
    rows = [_run(f"2026-08-14T{h:02d}:00:05Z") for h in (0, 3, 6, 9)]
    res = analyze(rows)
    assert res["delivery_pct"] == 100.0
    assert res["missed_slots"] == []
    assert res["skew_p50_min"] == 0.1


def test_a_missed_slot_is_counted_not_smoothed_over():
    # 03:00 never fired. At 3h cadence that doubles the window in which a bid
    # outage is invisible, so it must show up as a miss, not as a late run.
    rows = [_run("2026-08-14T00:00:05Z"), _run("2026-08-14T06:00:05Z")]
    res = analyze(rows)
    assert res["delivery_pct"] == 66.7
    assert res["missed_slots"] == ["2026-08-14T03:00:00+00:00"]


def test_skew_is_measured_against_the_slot_the_run_belongs_to():
    rows = [_run("2026-08-14T00:47:00Z")]
    assert analyze(rows)["skew_max_min"] == 47.0


def test_a_run_delayed_past_halfway_is_not_reattributed_to_the_next_slot():
    """Rounding to the NEAREST boundary would call a 2h-late run 1h early for
    the following slot — erasing the skew and inventing a miss."""
    started = datetime(2026, 8, 14, 2, 0, tzinfo=timezone.utc)  # 2h after 00:00
    assert nominal_slot(started, 3) == datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc)
    res = analyze([_run("2026-08-14T02:00:00Z")])
    assert res["skew_max_min"] == 120.0
    assert res["missed_slots"] == []


def test_dispatch_runs_do_not_inflate_delivery():
    # A human pressing the button says nothing about whether the cron fires.
    rows = [
        _run("2026-08-14T00:00:05Z"),
        _run("2026-08-14T01:11:00Z", event="workflow_dispatch"),
        _run("2026-08-14T06:00:05Z"),
    ]
    res = analyze(rows)
    assert res["scheduled_runs"] == 2
    assert res["delivery_pct"] == 66.7  # 03:00 still missing


def test_no_scheduled_runs_reports_nothing_rather_than_100_percent():
    res = analyze([_run("2026-08-14T00:00:05Z", event="workflow_dispatch")])
    assert res["scheduled_runs"] == 0
    assert res["delivery_pct"] is None


def test_unparseable_timestamps_are_dropped_not_counted_as_on_time():
    rows = [_run("2026-08-14T00:00:05Z"), _run("not-a-timestamp")]
    assert analyze(rows)["scheduled_runs"] == 1
