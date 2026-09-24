"""Lock the unreachable-retry properties of runner-teardown.yml (#404).

Each test names the failure it prevents, because a guard whose reason is not
written down gets "simplified" away by the next reader. The previous wrapper
treated OWNER_LOOKUP_UNREACHABLE (exit 75) as a hard one-shot failure: a
single chain source that timed out made the step exit 1 immediately and
LEAVE the lease ACTIVE. After #404, an exit 75 is a transient verdict —
the step retries with backoff so a momentary outage doesn't burn an
entire lease's worth of state.

YAML-shape guards sit at the top of the file; behavioural guards
(extracting the run: body, stubbing the CLI on PATH, executing the bash
against a scripted exit-code sequence) sit at the bottom. Both shapes
are needed: a YAML guard cannot see `|| OWNER_RC=$?` skip-on-success bugs
(#404 review); a behavioural guard that does not also assert the YAML
shape would let a copy-pasted retry loop in a neighbouring step survive.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import yaml

WF_PATH = Path(
    os.environ.get(
        "RUNNER_TEARDOWN_WF",
        Path(__file__).resolve().parents[1] / ".github/workflows/runner-teardown.yml",
    )
)
SRC = WF_PATH.read_text(encoding="utf-8")


def _resolve_owner_step_run() -> str:
    """Return the bash literal of the close-the-lease step that calls resolve-owner."""
    doc = yaml.safe_load(SRC)
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run", "")
            if "resolve-owner" in run and "RESOLVE_ARGS" in run:
                return run
    raise AssertionError(
        "resolve-owner step not found in runner-teardown.yml — "
        "either renamed or removed; update this test in lockstep"
    )


# ---------------------------------------------------------------------------
# YAML-shape guards. Cheap; lock the high-level invariants.
# ---------------------------------------------------------------------------


def test_resolve_owner_step_retries_on_exit_75_with_backoff():
    """OWNER_LOOKUP_UNREACHABLE is a transient verdict (#404).

    ⛔ Without retry-on-75, a single source that timed out during a chain
    outage would have the step exit 1 immediately, leaving the lease ACTIVE
    and not retrying. The wrapper must keep trying with bounded backoff so a
    momentary outage doesn't burn the lease.
    """
    run = _resolve_owner_step_run()
    # The retry loop reads ``OWNER_RC`` (resolve-owner exit code) and
    # continues on 75; the bounded-budget backoffs are a static array.
    assert "OWNER_RC=75" in run or "OWNER_RC -eq 75" in run, (
        "the resolve-owner retry loop must special-case exit 75 "
        "(OWNER_LOOKUP_UNREACHABLE); a flat `|| OWNER_RC=$?` would not retry"
    )
    assert "RESOLVE_TRIES_MAX" in run, (
        "the retry loop must have a bounded max-tries — an unbounded retry "
        "would burn the step budget and never surface a real outage"
    )
    assert "sleep" in run, (
        "the retry loop must back off between attempts — retrying a fresh "
        "transport-bound source 3 times in a row has the same outcome as "
        "retrying 0 times"
    )


def test_resolve_owner_step_resets_owner_rc_inside_the_loop():
    """The OWNER_RC reset must live INSIDE the while-body, not before it (#404 review).

    ⛔ On a successful attempt the ``|| OWNER_RC=$?`` is skipped (the ``||``
    only fires on FAILURE), so OWNER_RC keeps its previous value. With the
    reset BEFORE the loop, a 75-then-0 sequence makes OWNER_RC stay at 75,
    ``[ 75 -eq 0 ]`` is false, and the loop burns every try of the budget on
    what was actually a transient-then-recovers case. The fix is to reset
    OWNER_RC=0 as the first statement inside the loop body.
    """
    run = _resolve_owner_step_run()
    # Find the retry loop bounds: from OWNER_RC=0 (loop init) through done.
    loop_start = run.index("RESOLVE_TRIES_MAX=3")
    loop_end = run.index("done", loop_start)
    loop_body = run[loop_start:loop_end]
    # The reset must appear AFTER the loop's open (after RESOLVE_TRIES_MAX)
    # but BEFORE the resolve-owner invocation. ``|| OWNER_RC=$?`` is the
    # smoking gun — the reset must be earlier in the same block.
    resolve_owner_idx = loop_body.index("resolve-owner")
    reset_idx = loop_body.index("OWNER_RC=0")
    assert reset_idx < resolve_owner_idx, (
        "the OWNER_RC=0 reset must happen BEFORE the resolve-owner "
        "invocation inside the loop body, not before the loop's open. A "
        "pre-loop reset is skipped on the success path of `|| OWNER_RC=$?` "
        "and the loop burns every try of the budget on a transient "
        "75-then-0 sequence."
    )


def test_resolve_owner_step_does_not_retry_on_other_exit_codes():
    """A real disagreement / wrong-group / unknown verdict exits 1, no retry.

    ⛔ The retry-on-75 must NOT also retry on exit 1: a wrong group or
    unreadable chain response is a permanent verdict for THIS dseq. Retrying
    it costs wall-clock and re-reads the same answer.
    """
    run = _resolve_owner_step_run()
    # The loop body distinguishes 75 from "anything else" with an explicit
    # break; if the conditional were absent, every failure would retry.
    assert "RESOLVE_TRIES_MAX" in run
    # The pattern is: retry on 75, break otherwise.
    assert "-eq 75" in run, (
        "the retry condition must be a literal `OWNER_RC -eq 75` "
        "comparison; a bare `|| OWNER_RC=$?` retries everything"
    )


def test_workflow_yaml_parses():
    """The workflow file must remain valid YAML; a typo here breaks every step."""
    yaml.safe_load(SRC)


def test_workflow_has_no_orphan_unreachable_branch_in_resolve_step():
    """The unreachable retry must NOT have been left dangling in the wrong step.

    The retry logic lives in the resolve-owner step — adding a duplicate
    retry block in another step would mean a real outage double-retries
    across two loops and consumes the step budget twice.
    """
    run = _resolve_owner_step_run()
    # Exactly ONE backoff array declaration in the resolve step. A copy-pasted
    # second loop would declare RESOLVE_BACKOFFS twice.
    assert run.count("RESOLVE_BACKOFFS=(") == 1, (
        "the resolve-owner step must declare exactly one backoff array; a "
        "second declaration means the retry loop was copy-pasted into a "
        "neighbouring step"
    )


# ---------------------------------------------------------------------------
# Behavioural guards. Extract the resolve-owner's retry-loop section of the
# bash, stub the JA CLI on PATH, run it against scripted exit-code sequences.
# These guards see what YAML guards cannot: the success-on-Nth-attempt path
# where `|| OWNER_RC=$?` is skipped and the previous OWNER_RC leaks through.
# ---------------------------------------------------------------------------


def _extract_retry_loop() -> str:
    """Pull the OWNER_RC=0...done retry block + the OWNER_RC propagation.

    The retry loop ends at ``done``, but the script's exit code is set
    AFTER the loop by the OWNER_RC propagation block:
    ``if [ "$OWNER_RC" -ne 0 ]; then ... exit 1; fi``. Without that
    tail, the script exits 0 even when the resolver failed — making
    every behavioural test report success-on-failure. The slice ends
    at the ``fi`` that closes the OWNER_RC ``if`` block, NOT at the
    ``exit 1`` inside it (which would leave the if-block unclosed and
    cause a bash syntax error).
    """
    run = _resolve_owner_step_run()
    start = run.index("OWNER_RC=0\nRESOLVE_TRIES=")
    loop_end = run.index("\ndone\n", start) + len("\ndone")
    # The OWNER_RC propagation block is the first ``if [ "$OWNER_RC"``
    # after the loop. Slice through its closing ``fi`` so the extracted
    # block is a syntactically complete if/then/fi.
    if_start = run.find('if [ "$OWNER_RC"', loop_end)
    if if_start < 0:
        raise AssertionError(
            'no `if [ "$OWNER_RC"` after the retry loop — the OWNER_RC '
            "propagation block has been removed; the loop is now a no-op "
            "on failure"
        )
    # Find the matching `fi` for the propagation block. The block ends
    # at the first standalone ``\nfi\n`` after the if's `then`.
    then_idx = run.find("then", if_start)
    fi_end = run.find("\nfi\n", then_idx)
    if fi_end < 0:
        raise AssertionError(
            "no `fi` after the OWNER_RC if-block — the if-block was "
            "left unclosed; the propagation tail has been broken"
        )
    tail_end = fi_end + len("\nfi")
    raw = run[start:tail_end]
    return textwrap.dedent(raw)


def _run_loop(
    script_body: str,
    exit_codes: list[int],
    tmp_path: Path,
) -> tuple[int, list[int], str]:
    """Run ``script_body`` with a fake ``ja`` that returns exit_codes in order.

    Returns (script_rc, attempts, captured_stderr). ``script_rc`` is the
    script's exit code (the bash ``exit`` code from ``break`` / fall-through).
    ``attempts`` records how many times the fake ``ja`` was invoked.
    ``captured_stderr`` is everything the script wrote to stderr — the
    ::warning annotations surface here.
    """
    fake_bindir = tmp_path / "bin"
    fake_bindir.mkdir()
    ja_log = tmp_path / "ja.log"
    ja_log.write_text("")  # touch
    ja_script = fake_bindir / "ja"
    codes_str = " ".join(str(c) for c in exit_codes)
    ja_script.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$@" >> "$JA_LOG"\n'
        'idx=$(wc -l < "$JA_LOG")\n'
        f"exit_codes=({codes_str})\n"
        'rc="${exit_codes[$((idx-1))]:-1}"\n'
        'exit "$rc"\n'
    )
    ja_script.chmod(0o755)

    # Use a tiny backoff array so the test runs in <1s; replace the 1/4/16
    # real-budget values with 0/0/0 for the test. The retry-LOGIC is what we
    # are exercising — the actual wall-clock backoff is asserted by the YAML
    # shape (sleep + bounded backoffs) elsewhere.
    script = textwrap.dedent(
        f"""
        set -euo pipefail
        export PATH={fake_bindir}:$PATH
        export JA_LOG={ja_log}
        JA=(ja)
        RESOLVE_ARGS=()
        DSEQ=12345
        RESOLVE_BACKOFFS=(0 0 0)
        """
    ).lstrip() + script_body.replace("RESOLVE_BACKOFFS=(1 4 16)", "RESOLVE_BACKOFFS=(0 0 0)")

    script_path = tmp_path / "loop.sh"
    script_path.write_text(script)
    script_path.chmod(0o644)
    completed = subprocess.run(
        ["bash", str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    attempts_count = sum(
        1 for line in ja_log.read_text().splitlines() if line.strip() == "resolve-owner"
    )
    # ``attempts_count`` is the call count; the exit_codes list tells us
    # which code each attempt returned.
    return completed.returncode, [attempts_count], completed.stderr


def test_loop_proceeds_when_first_attempt_recovers(tmp_path):
    """Sequence (75, 0): a transient outage followed by success must succeed.

    ⛔ This is the test that would have caught the OWNER_RC-leak bug (#404
    review). On attempt 1, resolve-owner exits 75 (OWNER_LOOKUP_UNREACHABLE);
    on attempt 2, it exits 0 (success). With the reset INSIDE the loop, the
    second attempt resets OWNER_RC=0 and the ``[ 0 -eq 0 ] && break`` runs;
    the script falls through with OWNER_RC=0.

    Without the reset (the bug), OWNER_RC=75 from attempt 1 leaks through,
    the break never runs, and the script exits 75 after burning the budget.
    """
    body = _extract_retry_loop()
    rc, attempts, stderr = _run_loop(body, [75, 0], tmp_path)
    assert rc == 0, (
        f"75-then-0 must succeed (OWNER_RC reset inside the loop). Got rc={rc}, "
        f"attempts={attempts}, stderr={stderr!r}"
    )
    assert attempts == [2], (
        f"75-then-0 must take exactly 2 attempts — attempt 2 succeeds and "
        f"the loop breaks. Got attempts={attempts}"
    )


def test_loop_fails_after_three_75s(tmp_path):
    """Sequence (75, 75, 75): a durable outage must fail after exactly 3 tries."""
    body = _extract_retry_loop()
    rc, attempts, stderr = _run_loop(body, [75, 75, 75], tmp_path)
    assert attempts == [3], (
        f"75,75,75 must take exactly 3 attempts — RESOLVE_TRIES_MAX=3. Got attempts={attempts}"
    )
    assert rc != 0, (
        f"three 75s must propagate a non-zero exit so the step fails. "
        f"Got rc={rc}, stderr={stderr!r}"
    )


def test_loop_fails_fast_on_exit_1(tmp_path):
    """Sequence (1): a real disagreement / wrong-group / unreadable must NOT retry.

    ⛔ Exit 1 is a permanent verdict for THIS dseq. Retrying it costs
    wall-clock and re-reads the same answer. Exactly 1 attempt.
    """
    body = _extract_retry_loop()
    rc, attempts, stderr = _run_loop(body, [1], tmp_path)
    assert attempts == [1], f"exit 1 must fail fast — exactly 1 attempt. Got attempts={attempts}"
    assert rc != 0, f"exit 1 must propagate. Got rc={rc}, stderr={stderr!r}"


def test_loop_succeeds_on_first_attempt(tmp_path):
    """Sequence (0): a clean first attempt must take exactly 1 attempt.

    ⛔ A regression that retries on success would burn 2 attempts on every
    teardown — costs wall-clock, leaks ::warning annotations to the log,
    and could mask a later real failure by hiding the success path.
    """
    body = _extract_retry_loop()
    rc, attempts, stderr = _run_loop(body, [0], tmp_path)
    assert attempts == [1], (
        f"exit 0 must take exactly 1 attempt — no retry needed. Got attempts={attempts}"
    )
    assert rc == 0, f"exit 0 must propagate success. Got rc={rc}, stderr={stderr!r}"


# ---------------------------------------------------------------------------
# Mutation guards. The behaviour guards above prove the FIX; the mutations
# below prove the tests would catch a regression that re-introduced the bug.
# ---------------------------------------------------------------------------


def test_mutation_removing_in_loop_owner_rc_reset_fails_75_then_0(tmp_path):
    """Deleting the OWNER_RC=0 reset inside the loop must turn (a) red.

    The bug TEAMLEAD reproduced: with the reset only BEFORE the loop,
    attempt 1 sets OWNER_RC=75 (from `|| OWNER_RC=$?`), attempt 2 succeeds
    but the `||` is skipped so OWNER_RC stays 75, `[ 75 -eq 0 ]` is false,
    and the loop burns every try of the budget. Removing the in-loop reset
    makes the (75, 0) sequence fail — which is what the test must catch.
    """
    body = _extract_retry_loop()
    # The reset inside the loop body looks like:
    #     OWNER_RC=0
    #     : >/tmp/owner.json
    # Drop the OWNER_RC=0 line. The :>/tmp/owner.json still runs so the
    # rest of the loop is unaffected, isolating the regression.
    mutated = body.replace(
        "  OWNER_RC=0\n  : >/tmp/owner.json",
        "  : >/tmp/owner.json",
    )
    assert "OWNER_RC=0" in mutated, (
        "the loop still has the pre-loop OWNER_RC=0 (sanity); without it "
        "the mutation is indistinguishable from a full deletion"
    )
    # Run the mutated loop against (75, 0). With the bug, attempts=[3]
    # (the budget is exhausted) and rc=1. Without the bug, attempts=[2]
    # and rc=0.
    rc, attempts, stderr = _run_loop(mutated, [75, 0], tmp_path)
    # The guard: this mutation MUST NOT succeed. A working reset gives
    # (attempts=[2], rc=0); a broken reset gives (attempts=[3], rc=1).
    assert not (rc == 0 and attempts == [2]), (
        f"the in-loop OWNER_RC=0 reset is missing — this is the (75, 0) "
        f"sequence TEAMLEAD reproduced. rc={rc}, attempts={attempts}. "
        f"A working reset makes (75, 0) succeed in 2 attempts."
    )


def test_mutation_removing_eq_75_condition_retries_on_exit_1(tmp_path):
    """Removing the `-eq 75` guard must turn (c) red.

    A loop that retries on ANY non-zero exit would retry (1) instead of
    failing fast. The mutation: drop the `-eq 75` clause so the only
    retry condition is "we haven't burned the budget". (1) becomes a
    retry-loop of three identical failures instead of one clean exit-1.
    """
    body = _extract_retry_loop()
    # The retry condition reads:
    #   if [ "$OWNER_RC" -eq 75 ] && [ "$RESOLVE_TRIES" -lt "$RESOLVE_TRIES_MAX" ]; then
    # Removing `-eq 75` makes any non-zero rc retry. Replace the literal.
    mutated = body.replace(
        '[ "$OWNER_RC" -eq 75 ]',
        '[ -n "$OWNER_RC" ]',
    )
    rc, attempts, stderr = _run_loop(mutated, [1], tmp_path)
    # With the `-eq 75` guard removed, the loop must retry the (1) and burn
    # through the 3-attempt budget — exactly the bug the guard prevents.
    assert attempts == [3], (
        f"removing the `-eq 75` guard makes the loop retry exit 1 "
        f"(it retries 3 times instead of failing fast). Got attempts={attempts}; "
        f"a working guard makes (1) take exactly 1 attempt."
    )
