"""The bid-probe's time budget is DERIVED from the pair count — pin it.

The workflow's step timeout is hand-computed from
``len(eligible_pairs()) x (wait + confirm delay + retry wait)``. #257 took the
fleet from 9 pairs to 12 and that arithmetic silently stopped holding.

Why this is worth a test rather than a comment: an under-provisioned budget
does not fail loudly. The step is KILLED mid-run, and bid-probe.yml's own job
comment says a cancelled job skips the ``always()``-reap step and leaks every
order still open. So the failure mode of a stale constant here is an escrow
leak, discovered later by the cleanup reaper — the same not-measured-vs-clean
shape this repo keeps finding, with money attached.

These tests read the workflow as text on purpose. Parsing it into semantics
would let a restructure pass while the human-readable comment — the thing an
operator actually reads before changing the cadence — drifted out of date.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from just_akash.bid_probe import PROVIDERS, SCENARIOS, eligible_pairs

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "bid-probe.yml"

# Mirrors the CLI defaults the workflow passes: --wait 45, --retry-delay 60,
# and the confirming re-probe waits a second full --wait.
WAIT_S = 45
CONFIRM_DELAY_S = 60
RETRY_WAIT_S = 45
WORST_CASE_PER_PAIR_S = WAIT_S + CONFIRM_DELAY_S + RETRY_WAIT_S


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW.read_text()


def test_documented_pair_count_matches_reality(workflow_text: str):
    """The comment says `N x (...)`. N must be the real pair count."""
    m = re.search(r"#\s*(\d+) x \(\d+s wait", workflow_text)
    assert m, "the worst-case arithmetic comment is missing or reshaped"
    documented = int(m.group(1))
    assert documented == len(eligible_pairs()), (
        f"workflow budget is computed for {documented} pairs but "
        f"eligible_pairs() yields {len(eligible_pairs())}. Adding a scenario or "
        "a provider capability changes the run's worst-case duration; update "
        "the comment AND the step timeout together."
    )


def test_step_timeout_covers_the_worst_case(workflow_text: str):
    """Not just documented — actually sufficient, with room for tx overhead."""
    m = re.search(r"- name: Run bid probe\n\s+timeout-minutes: (\d+)", workflow_text)
    assert m, "could not find the bid-probe step timeout"
    step_minutes = int(m.group(1))
    worst_case_minutes = len(eligible_pairs()) * WORST_CASE_PER_PAIR_S / 60
    assert step_minutes >= worst_case_minutes, (
        f"step timeout {step_minutes}m is under the {worst_case_minutes:.1f}m "
        "worst case; a kill mid-run leaks every open order"
    )
    # Headroom must SCALE WITH PAIRS, not be a flat number: order create/close
    # overhead is paid once per pair, so a constant margin silently thins out
    # as scenarios are added. A flat 5m allowance let a stale 35m budget pass
    # this very test while under-provisioning the 12-pair run it was reviewing.
    #
    # 45s/pair is a SAFETY MARGIN, not a measurement of overhead. Measured
    # 2026-09-05 over the last 20 nine-pair runs: median 9.7m, max 23.4m job
    # total against a 22.5m modelled worst case — so the model is calibrated
    # and real runs do reach it. Scaled linearly to 12 pairs the observed max
    # becomes ~31m, which a 35m step would clip. The consequence of clipping
    # is not a red run, it is a leaked order, so the margin is deliberate.
    required_headroom_minutes = len(eligible_pairs()) * 45 / 60
    assert step_minutes - worst_case_minutes >= required_headroom_minutes, (
        f"{step_minutes - worst_case_minutes:.1f}m of headroom over the "
        f"{worst_case_minutes:.1f}m worst case, but {required_headroom_minutes:.1f}m "
        f"is required for {len(eligible_pairs())} pairs — raise the step timeout"
    )


def test_reap_step_still_has_a_window(workflow_text: str):
    """The job backstop must exceed the step timeout, or the always()-reap
    never runs and the leak the reap exists to prevent happens instead."""
    job = re.search(r"^    timeout-minutes: (\d+)$", workflow_text, re.M)
    step = re.search(r"- name: Run bid probe\n\s+timeout-minutes: (\d+)", workflow_text)
    assert job and step
    assert int(job.group(1)) - int(step.group(1)) >= 10, (
        "fewer than 10 min between the step timeout and the job backstop: a "
        "slow step leaves the always()-reap step no time to close open orders"
    )


def test_every_capability_names_a_real_scenario():
    """eligible_pairs() already raises on this, but assert it stays true for
    the shipped config rather than only for hand-built fixtures."""
    for provider in PROVIDERS:
        unknown = provider.capabilities - set(SCENARIOS)
        assert not unknown, f"{provider.cluster} lists unknown capability {unknown}"


def test_nodeport_is_probed_on_every_provider():
    """#257: a capability listed only where we already believe it works cannot
    detect a provider LOSING it. Every provider is asked."""
    asked = {t.cluster for t, s in eligible_pairs() if s.name == "nodeport"}
    assert asked == {p.cluster for p in PROVIDERS}, (
        "nodeport must be probed on every provider — narrowing it to the "
        "clusters known to serve it reproduces the #257 blind spot"
    )
