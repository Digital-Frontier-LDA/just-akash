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

from just_akash.bid_probe import PROVIDERS, SCENARIOS, _build_parser, eligible_pairs

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "bid-probe.yml"

# ⛔ DERIVED, NEVER RE-DECLARED. An earlier cut of this file hard-coded 45/60/45
# and asserted in a comment that the workflow "passes --wait 45". It does not:
# the workflow passes ONLY --retry-delay (plus the out-paths), so `--wait` comes
# from the argparse default and IS the operative value on every scheduled run.
# Re-declaring the numbers here would have been the same hand-copied-constant
# defect this file exists to catch, one layer up. So each is read from the place
# that actually decides it.
_PARSER = _build_parser()


def _cli_default(dest: str) -> int:
    """A CLI default, through argparse's PUBLIC api.

    `parser._actions` is private and has changed shape across CPython versions.
    A guard that breaks on a Python upgrade stops guarding at exactly the moment
    nobody is looking at it — and this one's failure mode is a leaked order.
    """

    value = _PARSER.get_default(dest)
    assert value is not None, f"--{dest.replace('_', '-')} has no default to derive from"
    return int(value)


def _workflow_text() -> str:
    # encoding pinned: this workflow carries non-ASCII (⛔) and the platform
    # default would raise UnicodeDecodeError under a non-UTF-8 locale.
    return WORKFLOW.read_text(encoding="utf-8")


def _confirm_delay_s(text: str) -> int:
    """The retry delay a SCHEDULED run actually uses.

    Not the argparse default: a cron run supplies no inputs, so the workflow's
    own shell fallback `RETRY="${RETRY_DELAY_INPUT:-60}"` is what reaches the
    CLI. test_the_two_retry_defaults_agree pins them together so a divergence
    is a failure rather than a silent change of meaning.
    """
    m = re.search(r'RETRY="\$\{RETRY_DELAY_INPUT:-(\d+)\}"', text)
    assert m, "could not find the workflow's retry-delay fallback"
    return int(m.group(1))


def _cli_invocation(text: str) -> str:
    """The full shell command the workflow runs, continuations included.

    Deliberately NOT a fixed-size window into the file. An earlier cut sliced
    400 characters, which is one more constant chosen by hand in a file about
    constants chosen by hand: an invocation that grew past it would hide a
    later `--wait` and the guard would pass while blind.
    """

    marker = "python -m just_akash.bid_probe"
    start = text.find(marker)
    assert start != -1, (
        "could not find the bid-probe CLI invocation in the workflow. If the "
        "command was renamed or restructured, this guard is no longer reading "
        "the command that actually runs — fix the marker, do not delete this."
    )
    lines = text[start:].splitlines()
    collected = [lines[0]]
    for line in lines[1:]:
        if not collected[-1].rstrip().endswith("\\"):
            break
        collected.append(line)
    return "\n".join(collected)


def _worst_case_per_pair_s(text: str) -> int:
    # run_probe re-probes a NO-BID with wait_s=wait_s (verified in source), so a
    # retried pair costs a full wait, the confirm delay, then another full wait.
    wait = _cli_default("wait")
    return wait + _confirm_delay_s(text) + wait


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return _workflow_text()


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
    worst_case_minutes = len(eligible_pairs()) * _worst_case_per_pair_s(workflow_text) / 60
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


def test_the_workflow_does_not_pass_wait(workflow_text: str):
    """The budget's `--wait` term comes from the argparse default because the
    workflow never overrides it. If that ever changes, the model must read the
    passed value instead — and this assertion is how anyone finds out."""
    invocation = _cli_invocation(workflow_text)
    assert "--wait" not in invocation, (
        "the workflow now passes --wait, so the argparse default is no longer "
        "the operative value — derive the budget from the passed value"
    )


def test_the_two_retry_defaults_agree(workflow_text: str):
    """The workflow's shell fallback and the CLI default are separate values
    that happen to match. A divergence would make the model quietly wrong for
    whichever caller it did not describe, so pin them together."""
    assert _confirm_delay_s(workflow_text) == _cli_default("retry_delay"), (
        "the workflow's RETRY_DELAY_INPUT fallback and --retry-delay's argparse "
        "default have diverged; the budget arithmetic describes only one of them"
    )


def test_workflow_is_readable_under_a_non_utf8_locale():
    """The workflow carries non-ASCII; an unencoded read_text() would raise
    UnicodeDecodeError wherever the platform default is not UTF-8."""
    raw = WORKFLOW.read_bytes()
    assert not raw.decode("utf-8").isascii(), "guard is vacuous if the file is pure ASCII"
    assert 'encoding="utf-8"' in Path(__file__).read_text(encoding="utf-8"), (
        "this suite must pin the encoding when reading the workflow"
    )
