"""Every failure path in the lease-shell E2E must report what it MEASURED.

⛔ WHY THIS EXISTS. On 2026-09-06 `E2E lease-shell transport` failed on main with
exactly one diagnostic line:

    FAIL exec failed (rc=0):

`rc=0` means the command SUCCEEDED. The condition that actually failed was that
the expected string never appeared in stdout — and stdout was captured, judged,
and then discarded. So the message named a cause its own evidence refutes, hid
the value it had just tested, and pointed the reader at the one part known to
have worked. The run is gone by the time anyone reads it, so that line is the
entire diagnostic.

⇒ The rule below is self-selecting rather than a hand-written list: any
`log_fail` that reports on a subprocess result must report ALL of it. A list of
paths goes stale the moment someone adds a fourteenth; a rule derived from what
the call already mentions does not.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_MODULE = pathlib.Path(__file__).resolve().parents[1] / "just_akash" / "test_shell_e2e.py"
_SRC = _MODULE.read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)

# The three attributes of a CompletedProcess a reader needs to act. Mentioning any
# of them is what makes a failure path "about" a subprocess; mentioning some but
# not all is the defect.
_STREAMS = ("returncode", "stdout", "stderr")


def _log_fail_calls() -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(_TREE):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "log_fail"
        ):
            seg = ast.get_source_segment(_SRC, node) or ""
            out.append((node.lineno, seg))
    return out


def test_the_extractor_found_the_failure_paths():
    """A rule that matches nothing reports safety it never checked."""
    calls = _log_fail_calls()
    assert len(calls) >= 10, f"only {len(calls)} log_fail calls parsed — the extractor is broken"
    assert any(all(s in seg for s in _STREAMS) for _, seg in calls), (
        "no call reports all three streams — the extractor is matching the wrong thing"
    )


@pytest.mark.parametrize(
    "lineno,segment",
    [
        pytest.param(ln, seg, id=f"L{ln}")
        for ln, seg in _log_fail_calls()
        if any(s in seg for s in _STREAMS)
    ],
)
def test_a_failure_path_reporting_a_subprocess_reports_all_of_it(lineno, segment):
    """If it mentions one stream it must mention all three.

    ⚠ KNOWN GAP, stated rather than hidden: a path that reports an ALIAS escapes
    this rule. The permissions check did exactly that — it printed `perms`, which
    is `r.stdout.strip()`, and mentioned none of the three attributes by name, so
    a rule keyed on attribute names could not see it. It is fixed, but the next
    one written that way would also be invisible here. The durable answer is to
    keep the raw result in the message even when a parsed form is what was judged.
    """
    missing = [s for s in _STREAMS if s not in segment]
    reported = [s for s in _STREAMS if s in segment]
    assert not missing, (
        f"log_fail at line {lineno} reports {reported} "
        f"but not {missing} — a reader gets a verdict without the evidence it rests "
        f"on, and the run is gone. Report what was measured:\n{segment}"
    )
