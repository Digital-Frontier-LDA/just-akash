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


def test_absent_and_null_and_present_render_as_three_different_strings():
    """⛔ `unreported` and `null` are DIFFERENT FACTS and must not share a string.

    "we never obtained a parseable reading" and "we read the document and the field
    was empty" are the two states the GATE line exists to tell apart — the second is
    a legitimate provider reading and the evidence #273 is waiting on. `None`,
    `False` and `0` all collapse them, which is why the renderer takes a sentinel
    rather than defaulting to None.

    Imported rather than restated, so a change to the renderer is tested and not
    diverged from.
    """
    import importlib

    mod = importlib.import_module("just_akash.test_shell_e2e")
    unset, render = mod._UNSET, mod._render

    assert render(unset) == "unreported"
    assert render(None) == "null"
    assert render("ready") == "'ready'"
    # the three must be mutually distinguishable, which is the actual invariant
    assert len({render(unset), render(None), render("ready")}) == 3

    # and the ssh_host variant must not map an absent key onto the same string as
    # a present-but-falsy value — the "must not render as False" rule
    truthy = render({"host": "h"}, lambda v: "present" if v else "empty")
    falsy = render({}, lambda v: "present" if v else "empty")
    assert render(unset) != falsy, "an absent ssh_host reads the same as an empty one"
    assert truthy != falsy
