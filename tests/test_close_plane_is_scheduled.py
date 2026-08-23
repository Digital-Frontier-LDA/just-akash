"""The close plane is SCHEDULED — a manual backstop is no backstop.

★ THE MEASURED DEFECT (2026-08-23): 13 `just-akash-runner.<hash>` leases held 65 ACT
for up to 23.5h because the ENTIRE close plane was dispatch-only — runner-teardown
had zero callers, and BOTH reapers (cleanup-stale, close-orphans) had no schedule.
The backstops existed and were correct; nothing ever ran them. "Dry-run by default.
Dispatch once..." is a protocol for a human who does not exist at 02:00 on a Sunday.

THE FIX: cleanup-stale gets `0 */6 * * *` dry-run (the workflow is already dry-run
by default, so the cron observes and reports — promotion to execute=true stays a
human decision, preserving the 200GiB-volume lesson). close-orphans is HARDER: its
`dseqs` input is REQUIRED and there is no safe default list — a cron cannot supply
dseqs, so scheduling it as-is would either fail validation or, worse, encourage a
wildcard. It stays dispatch-only BY DESIGN, and that decision is pinned here so a
future "just add a cron to it too" does not silently reintroduce the sweep that
destroyed 14 third-party deployments.

Interval reasoning (from the incident): */6h bounds a standing bleed at one
interval of accumulation — the 14:16Z burst leaked 11 leases in 8 minutes, and 12h
was already outrun by the sibling repo's consul leak rate. 4 sweeps/day at read-only
cost. Minute OFFSET (23, not 0) to avoid stacking API burst with bid-probe's
`0 */3` and provider-canary's `5,35 * * * *`.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def _doc(name: str) -> dict:
    return yaml.safe_load((REPO / ".github" / "workflows" / name).read_text())


def _triggers(doc: dict) -> dict:
    on = doc.get("on") or doc.get(True) or {}
    return on


# ── cleanup-stale: scheduled, dry-run ⚀ preserving the human-promotion protocol ──


def test_cleanup_stale_is_scheduled():
    """★ THE FIX: the sweeper that recognizes `just-akash-*` runs WITHOUT a human.
    Dispatch-only was the defect — the workflow existed, was correct, and never ran."""
    sched = _triggers(_doc("cleanup-stale.yml")).get("schedule") or []
    assert sched, "cleanup-stale.yml has no schedule — the backstop is still manual"


def test_cleanup_stale_cron_is_six_hourly_offset():
    """*/6h (bounds a standing bleed at one interval), minute 23 (off bid-probe's
    :00 */3 and canary's :05/:35 — three crons at :00 stack API burst)."""
    sched = _triggers(_doc("cleanup-stale.yml")).get("schedule") or []
    cron = str(sched[0].get("cron", "")) if sched else ""
    assert cron.startswith("23 ") and (",18 " in cron or "*/6" in cron), (
        f"unexpected cron: {cron!r} — every 6h at :23 (explicit hour list or */6)"
    )


def test_cleanup_stale_cron_stays_dry_run():
    """⚠ The cron must observe, not destroy. The workflow is dry-run by default
    (`execute` default false); a scheduled run with no inputs inherits that default.
    If a future edit flips the default, this pins the incident lesson: this account
    hosts `bordas` research deployments, one of which was closed 4× and lost a
    200GiB volume each time. Dry-run by schedule; execute stays a human decision."""
    inputs = _triggers(_doc("cleanup-stale.yml")).get("workflow_dispatch", {}).get("inputs", {})
    assert inputs.get("execute", {}).get("default") is False, (
        f"execute no longer defaults to dry-run: {inputs!r} — a cron would close live "
        f"deployments without a human reading the verdict table"
    )


def test_cleanup_stale_keeps_dispatch_for_promotion():
    """The promotion protocol survives: dispatch once execute=true AFTER reading a
    scheduled run's verdict table. The cron adds observation; it must not remove
    the human path to action."""
    assert "workflow_dispatch" in _triggers(_doc("cleanup-stale.yml")), (
        "cleanup-stale lost its workflow_dispatch — the execute=true promotion path"
    )


# ── close-orphans: deliberately UNSCHEDULED, and pinned as deliberate ──────────


def test_close_orphans_stays_dispatch_only_and_says_why():
    """⚠ NOT an omission — a design boundary. close-orphans requires a `dseqs` input
    (verified one-by-one against MIN_CONFIRMATIONS independent LCD endpoints); a
    cron cannot supply a safe default list. Scheduling it would fail validation or
    invite a wildcard — the exact sweep shape that destroyed 14 third-party
    deployments once. It stays manual; this test exists so "add a cron to it too"
    fails loudly instead of arriving as a one-line PR."""
    doc = _doc("close-orphans.yml")
    sched = _triggers(doc).get("schedule") or []
    inputs = _triggers(doc).get("workflow_dispatch", {}).get("inputs", {})
    assert inputs.get("dseqs", {}).get("required") is True
    assert not sched, (
        "close-orphans.yml gained a schedule — it requires a caller-supplied verified "
        "dseq list; a scheduled run has none. See the 14-third-party-deployments "
        "incident in runner-teardown.yml's ownership note"
    )


# ── the schedule does not collide with the repo's other crons ──────────────────


def test_scheduled_workflows_do_not_stack_on_the_same_minute():
    """Every cron in .github/workflows: their minutes must not collide. Three crons
    firing at :00 (bid-probe already at 0 */3) would stack API burst against the
    5000/hr core budget that has already blinded this ecosystem's provisioners."""
    entries = []
    for f in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        try:
            doc = yaml.safe_load(f.read_text())
        except Exception:
            continue
        for s in _triggers(doc).get("schedule") or []:
            cron = str(s.get("cron", ""))
            if cron:
                entries.append((f.name, cron))
    # ⚠ PRE-EXISTING collisions exist on main (bid-probe 0 */3, provider-smoke 0 7,
    # secrets 0 8, security 0 6 all sit on minute 0). Retiming four live crons is a
    # separate change; THIS pin guards only what this PR adds — cleanup-stale's
    # minute (23) must be unique — so the pin is exact, not a blanket that would
    # fail today for reasons predating this change.
    others = [cron for name, cron in entries if "cleanup-stale" not in name]
    our_cron = next(c for nn, c in entries if "cleanup-stale" in nn)
    our_minute = our_cron.split()[0]
    colliding = [c for c in others if c.split()[0] == our_minute]
    assert not colliding, (
        f"cleanup-stale's cron minute {our_minute} collides with existing crons "
        f"{colliding} — API burst stacking against the shared 5000/hr budget"
    )
