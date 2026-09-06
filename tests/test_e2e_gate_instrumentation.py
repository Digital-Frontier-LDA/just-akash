"""Invariants of the #273 gate instrumentation.

⛔ WHY THESE ARE TESTS AND NOT COMMENTS. The instrumentation exists because nobody
can say which mechanism produces `rc=0` with empty stdout on the lease-shell exec,
and every candidate gate fix was refuted for reasoning past that gap. Its whole
value is the DISTRIBUTION it will build. Each invariant below is one a
well-meaning change would break first, while appearing to improve the test.
"""

from __future__ import annotations

import ast
import pathlib

_MODULE = pathlib.Path(__file__).resolve().parents[1] / "just_akash" / "test_shell_e2e.py"
_SRC = _MODULE.read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)

_PARENT = {}
for _node in ast.walk(_TREE):
    for _child in ast.iter_child_nodes(_node):
        _PARENT[_child] = _node


def _ancestors(node):
    while node in _PARENT:
        node = _PARENT[node]
        yield node


def _exec_call():
    """The step-4 `run(...)` that execs the probe command."""
    for node in ast.walk(_TREE):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run"
        ):
            seg = ast.get_source_segment(_SRC, node) or ""
            if "hello from lease-shell" in seg and "exec" in seg:
                return node
    return None


def test_the_exec_probe_was_found():
    """A locator that finds nothing would make every assertion below vacuous."""
    assert _exec_call() is not None, "step-4 exec call not found — re-anchor, do not delete"


def test_step_4_exec_is_one_unbiased_sample():
    """⛔ DO NOT RETRY THE EXEC. The run's pass rate IS the per-exec success rate.

    This is the only #273 signal that exists. Wrapping the exec in a retry would
    raise the observed pass rate toward 1 while changing nothing about the
    underlying fault — it would look like a fix and would destroy the measurement
    that could produce one. The readiness POLL above it is a different thing and is
    deliberately left alone.
    """
    call = _exec_call()
    assert call is not None
    looped = [a for a in _ancestors(call) if isinstance(a, (ast.For, ast.While))]
    assert not looped, (
        "the step-4 exec is inside a loop — it must remain a single unbiased sample, "
        "because the run's pass rate is the per-exec success rate and a retry destroys it"
    )


def test_the_gate_verdict_is_recorded_on_every_run():
    """A histogram built only from failures is not a histogram.

    The GATE line must be emitted unconditionally after the readiness loop. Moving it
    inside a failure branch — an easy 'reduce log noise' change — would leave the
    distribution measuring only the runs that already went wrong, which is exactly
    the population it must not be conditioned on.
    """
    gate = [
        n
        for n in ast.walk(_TREE)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "log_info"
        and "GATE" in (ast.get_source_segment(_SRC, n) or "")
    ]
    assert len(gate) == 1, f"expected exactly one GATE emission, found {len(gate)}"
    guarded = [a for a in _ancestors(gate[0]) if isinstance(a, ast.If)]
    assert not guarded, (
        "the GATE line is inside a conditional — it must be emitted on EVERY run, "
        "pass or fail, or the time-to-ready distribution is conditioned on failure"
    )


def test_the_readiness_probe_cannot_escape_past_the_gate_line():
    """⛔ A conditional is not the only way to skip an unconditional statement.

    The first version of this file checked only that the GATE emission was outside
    any `if`. It was — and `run(..., timeout=30)` still sat OUTSIDE the poll's try,
    so a `subprocess.TimeoutExpired` escaped the loop, skipped GATE entirely and
    went to cleanup. The run that most needs a verdict — the one where the status
    probe hangs — was the one that emitted none. The guard passed while the
    invariant it names was violated, which is the defect this whole PR is about.
    (Reported by CodeRabbit on #276.)

    So: the probe must be inside a `try`, and that `try` must name TimeoutExpired.
    """
    poll = None
    for node in ast.walk(_TREE):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run"
        ):
            seg = ast.get_source_segment(_SRC, node) or ""
            if "just-akash status" in seg and "--json" in seg:
                poll = node
                break
    assert poll is not None, "readiness probe not found — re-anchor, do not delete"

    tries = [a for a in _ancestors(poll) if isinstance(a, ast.Try)]
    assert tries, (
        "the readiness probe is not inside a try — a TimeoutExpired will escape the "
        "loop and skip the GATE line, so 'recorded on every run' becomes false on "
        "exactly the runs worth recording"
    )
    handled = ast.dump(tries[0])
    assert "TimeoutExpired" in handled, (
        "the enclosing try does not name TimeoutExpired, so a hung status probe "
        "still escapes past the GATE emission"
    )


def test_every_streaming_probe_is_time_bounded():
    """`logs` and `events` stream. The CLI's own help says `--duration` exists to
    avoid "hanging when the provider holds a non-follow connection open".

    Unbounded, the diagnostic battery would stall for the full subprocess timeout on
    every failure — a diagnostic costing more than the bug it explains.
    """
    fn = next(
        (
            n
            for n in ast.walk(_TREE)
            if isinstance(n, ast.FunctionDef) and n.name == "_diagnose_exec_failure"
        ),
        None,
    )
    assert fn is not None, "the diagnostic battery is gone — re-anchor, do not delete"
    body = ast.get_source_segment(_SRC, fn) or ""
    for streaming in ("just-akash logs", "just-akash events"):
        assert streaming in body, f"{streaming!r} probe missing"
        # ⛔ DEFAULT, not a bare next(). Without it a refactor that splits the
        # probe command across lines raises StopIteration — a traceback instead of
        # the assert message below, which is the one thing that tells the next
        # reader what broke and what to do. A test enforcing "say what you
        # measured" must not fail by crashing.
        line = next((ln for ln in body.splitlines() if streaming in ln), None)
        assert line is not None, (
            f"{streaming!r} is no longer on a single line — re-anchor this check "
            f"rather than deleting it; the --duration invariant still applies"
        )
        assert "--duration" in line, (
            f"{streaming!r} is invoked without --duration — it streams, so this "
            f"hangs until the subprocess timeout on every exec failure:\n{line.strip()}"
        )


def test_no_poll_and_key_absent_and_value_render_as_different_strings():
    """⛔ The three facts that actually occur must not share a string.

    Measured against `just-akash status --json` (cli.py), not assumed:
    `"status"` is always present and always a string; `"ssh_host"` is set only
    `if ssh:`, so it is OMITTED when there is no endpoint yet. That makes
    key-absent the ORDINARY negative reading for ssh_host — data, not an error —
    and collapsing it with "no poll landed" is the defect this guards.

    An earlier revision used ONE sentinel and rendered key-absent as
    `unreported`, reintroducing exactly the collapse it was written to remove.

    Imported rather than restated, so a change to the renderer is tested and not
    diverged from.
    """
    import importlib

    mod = importlib.import_module("just_akash.test_shell_e2e")
    nopoll, absent, render = mod._NOPOLL, mod._ABSENT, mod._render

    assert render(nopoll) == "unreported"
    assert render(absent) == "absent"
    assert render("ready") == "'ready'"
    assert render(None) == "null"

    # the real invariant: every outcome is mutually distinguishable
    rendered = [render(nopoll), render(absent), render(None), render("ready")]
    assert len(set(rendered)) == 4, f"two facts share a rendering: {rendered}"

    # and for ssh_host's presence formatter, absent must not read as empty
    fmt = lambda v: "present" if v else "empty"  # noqa: E731
    assert render(absent, fmt) != render({}, fmt), (
        "an ssh_host key that was never sent reads the same as one sent empty"
    )


def _assigns_to(name):
    """Every Assign/AnnAssign in the module binding `name`."""

    def targets(n):
        if isinstance(n, ast.Assign):
            return n.targets
        if isinstance(n, ast.AnnAssign):
            return [n.target]
        return []

    return [
        n
        for n in ast.walk(_TREE)
        if any(isinstance(t, ast.Name) and t.id == name for t in targets(n))
    ]


def test_the_reported_wait_is_measured_not_scheduled():
    """⛔ A duration comes off the clock WHERE IT IS REPORTED.

    Two independent versions of one error lived in this block, and both reported
    short — the reassuring direction:

      - `gate_elapsed` was snapshotted at the TOP of each poll iteration, so it
        excluded that attempt's own 30s probe. On an all-timeout run — precisely
        the hung-probe case this instrumentation exists to measure — the sample
        under-reported by a full timeout. (Reported by Copilot on #276.)
      - the timeout message computed `10 + (max_attempts - 1) * poll_interval`,
        which silently assumes the probe returns instantly: it claimed 95s for a
        wait that really runs ~635s.

    All three assertions are kept because each is satisfiable by the other bugs:
    a per-iteration snapshot still derives from `time.monotonic()`, and a value
    correctly measured at emission can still be ignored by the line below it.
    """
    assigns = _assigns_to("gate_elapsed")
    assert len(assigns) == 1, (
        f"expected exactly one gate_elapsed assignment, found {len(assigns)} — a "
        "second one is how the per-iteration snapshot comes back"
    )

    looped = [a for a in _ancestors(assigns[0]) if isinstance(a, (ast.For, ast.While))]
    assert not looped, (
        "gate_elapsed is assigned inside the poll loop, so it excludes that attempt's "
        "own probe and under-reports an all-timeout run by a full 30s timeout"
    )

    seg = ast.get_source_segment(_SRC, assigns[0]) or ""
    assert "time.monotonic()" in seg, (
        "gate_elapsed no longer comes off the clock — a duration derived from "
        "max_attempts/poll_interval assumes the probe returns instantly"
    )

    waits = [ln for ln in _SRC.splitlines() if "Lease not active after" in ln]
    assert len(waits) == 1, "the timeout message moved — re-anchor, do not delete"
    assert "gate_elapsed" in waits[0], (
        "the timeout message does not report the measured wait; arithmetic over "
        "max_attempts/poll_interval claimed 95s for a ~635s all-timeout run"
    )
