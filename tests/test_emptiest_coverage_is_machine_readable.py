"""Emptiest coverage must survive as a structured diagnostic, not only as a log line.

⛔ MEASURED 2026-08-29 in Blazing-Back's `akash-runner.yml`. The consuming workflow does:

    # deploy.log is truncated per round, so without this only the final round survives
    grep -a 'akash-diag' /tmp/akash/deploy.log >> /tmp/akash/diag.jsonl

⇒ ONLY `akash-diag` lines are accumulated across rounds. The emptiest coverage was a plain
`_log(INFO, "EMPTIEST: capacity readable for N/M …")`, so it was DISCARDED for every round
but the last — on precisely the fact that distinguishes "emptiest APPLIED" from "emptiest
REQUESTED, silently fell back to cheapest".

⚠ THE WORKFLOW BELIEVED OTHERWISE. Its own comment reads: "just-akash prints `EMPTIEST:
capacity readable for N/M bidding providers` so 'requested' and 'applied' are
distinguishable in the log rather than assumed." True of one round, in a file the next
round overwrites.

⚠ WHY PARTIAL COVERAGE COUNTS, not just zero. With 1 of 3 readable the auction still ranks
a subset and reads as "emptiest applied". The caller cannot tell 3-of-3 from 1-of-3 without
the numbers, so the event fires whenever coverage is incomplete.
"""

from __future__ import annotations

import json

from just_akash._diagnostics import Code, emit


def _events(capsys) -> list[dict]:
    err = capsys.readouterr().err
    out = []
    for line in err.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "akash-diag":
                out.append(ev)
    return out


def test_the_code_exists_and_is_stable():
    """A reason code is an API for the caller bridge — it must be a stable UPPER_SNAKE name."""
    assert hasattr(Code, "SELECTION_EMPTIEST_DEGRADED"), (
        "emptiest degradation has no reason code, so a consumer cannot gate on it"
    )
    assert Code.SELECTION_EMPTIEST_DEGRADED == "SELECTION_EMPTIEST_DEGRADED"


def test_the_event_carries_the_numbers_not_just_a_message(monkeypatch, capsys):
    """A message a human reads is not a value a job can gate on."""
    monkeypatch.setenv("AKASH_DIAGNOSTICS", "1")
    emit(
        Code.SELECTION_EMPTIEST_DEGRADED,
        "warning",
        "emptiest requested; capacity unreadable for 2/3 bidding provider(s)",
        readable=1,
        bidding=3,
        fully_degraded=False,
        unreadable_providers=["akash1a", "akash1b"],
    )
    evs = _events(capsys)
    assert evs, "no akash-diag line emitted — the event would not survive the per-round truncation"
    ctx = evs[0].get("context") or {}
    for key in ("readable", "bidding", "fully_degraded", "unreadable_providers"):
        assert key in ctx, f"context lacks {key!r}; a consumer cannot compute coverage from prose"
    assert ctx["readable"] == 1 and ctx["bidding"] == 3
    assert ctx["fully_degraded"] is False, (
        "partial coverage must NOT be reported as a full fallback — 1-of-3 still ranks a subset"
    )


def test_deploy_emits_it_on_incomplete_coverage_not_only_on_zero():
    """The guard that actually pins the call site, by source, since the auction needs a chain."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "just_akash" / "deploy.py").read_text()
    assert "SELECTION_EMPTIEST_DEGRADED" in src, "deploy.py never emits the code"
    assert "if _readable < len(_capacity):" in src, (
        "the emit is not gated on INCOMPLETE coverage — gating on `not _readable` would "
        "report only total degradation and stay silent on 1-of-3"
    )
    assert "fully_degraded=not _readable" in src, (
        "the event must distinguish partial coverage from a full fallback to cheapest"
    )
