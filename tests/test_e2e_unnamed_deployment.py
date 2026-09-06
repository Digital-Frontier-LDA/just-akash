"""#278: a deployment the run that created it cannot name.

⛔ WHY THESE EXERCISE A REAL SUBPROCESS. The claim under test is about what
survives SIGKILL, and SIGKILL is precisely the thing no in-process fake can
model: a mock that "raises TimeoutExpired" proves nothing about whether the
child's handlers ran, whether its output was flushed, or what type the exception
carries. Each test below kills a real process and reads what is actually left.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from just_akash.test_shell_e2e import _DSEQ_ANY_RE, _DSEQ_SUMMARY_RE, _decoded

#: Long enough that the child is still alive when the parent gives up, short
#: enough that a hung test is a failed test rather than a hung suite.
_CHILD_LIFETIME = 30
_PARENT_PATIENCE = 2


def _kill_after_timeout(child_source: str) -> subprocess.TimeoutExpired:
    """Run a child that outlives the parent's patience; return the raised timeout."""
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "child.py"
        script.write_text(textwrap.dedent(child_source), encoding="utf-8")
        try:
            subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                timeout=_PARENT_PATIENCE,
            )
        except subprocess.TimeoutExpired as exc:
            return exc
    raise AssertionError("the child exited on its own — it must outlive the timeout")


def test_a_sigkilled_child_still_yields_the_dseq_it_flushed():
    """The identifier was in hand and thrown away.

    `TimeoutExpired` carries the output flushed before the kill. deploy.py's
    `_log` prints with flush=True, so `Deployment created  DSEQ=N` reaches the
    parent even when the child is killed mid-deploy. Recovering it is the
    difference between a deployment that can be closed and one that cannot be
    named at all.
    """
    exc = _kill_after_timeout(f"""
        import time
        print("[ts] Deployment created  DSEQ=1788999000111  manifest_len=42", flush=True)
        time.sleep({_CHILD_LIFETIME})
    """)
    output = _decoded(exc.stdout) + _decoded(exc.stderr)
    assert _DSEQ_ANY_RE.findall(output) == ["1788999000111"], (
        f"the flushed DSEQ did not survive the kill; got {output!r}"
    )


def test_the_timeout_carries_bytes_even_though_the_call_asked_for_text():
    """⛔ Measured, because both failure modes are silent.

    `subprocess.run(..., text=True, timeout=N)` raises with `.stdout` as BYTES.
    So `exc.stdout + exc.stderr` is a TypeError, and a `str` pattern applied to
    it matches nothing — and both look exactly like "the deployment had no
    DSEQ", which is a statement about the deployment rather than about the
    plumbing. `_decoded` exists for this and nothing else.
    """
    exc = _kill_after_timeout(f"""
        import time
        print("DSEQ=1788999000222", flush=True)
        time.sleep({_CHILD_LIFETIME})
    """)
    assert isinstance(exc.stdout, bytes), (
        "TimeoutExpired.stdout is no longer bytes — if it is now str, _decoded is "
        "harmless, but the comment explaining why it exists has become false"
    )
    assert _decoded(exc.stdout).startswith("DSEQ=1788999000222")
    assert _decoded(None) == ""


def test_the_child_is_given_no_chance_to_clean_up_after_itself():
    """⛔ THE PREMISE OF #278, pinned rather than asserted in prose.

    deploy.py has SEVEN internal cleanup sites and the lease-failure one even
    logs the DSEQ when its own close fails — so the ordinary failure path is
    well covered, and someone reading #278 could reasonably conclude the issue
    is already fixed. It is not, because on THIS path none of that code runs.

    `subprocess.run(timeout=)` calls `Popen.kill()`, which is SIGKILL on POSIX
    and cannot be caught, blocked or handled. A child holding every hook Python
    offers gets none of them.
    """
    with tempfile.TemporaryDirectory() as td:
        marks = Path(td) / "marks.txt"
        exc = _kill_after_timeout(f"""
            import atexit, signal, sys, time
            def note(tag):
                with open({str(marks)!r}, "a") as fh:
                    fh.write(tag + "\\n")
            atexit.register(lambda: note("ATEXIT"))
            for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(s, lambda n, f: note("CAUGHT"))
            try:
                note("STARTED")
                sys.stdout.flush()
                time.sleep({_CHILD_LIFETIME})
            finally:
                note("FINALLY")
        """)
        assert exc is not None
        ran = marks.read_text(encoding="utf-8").split() if marks.exists() else []
    assert ran == ["STARTED"], (
        f"the child ran cleanup after SIGKILL, which is impossible — got {ran!r}. "
        "If this ever passes with more than STARTED, the kill is no longer SIGKILL "
        "and #278's whole premise needs re-deriving."
    )


def test_the_success_pattern_is_not_widened_to_the_redeploy_line():
    """⛔ The wider pattern must stay OFF the success path.

    deploy.py emits `DSEQ=` when a deployment is created (:1104) and again for a
    re-deploy round (:1928), then prints the authoritative `DSEQ: ` summary last
    (:2116). A first-match search over the wider pattern therefore names the
    SUPERSEDED deployment. Widening one pattern and reusing it everywhere is the
    obvious tidy-up and it silently closes the wrong thing.
    """
    output = (
        "[ts] Deployment created  DSEQ=111  manifest_len=9\n"
        "[ts] Re-deployed: new order DSEQ=222 — fast-polling for fresh bids...\n"
        "  DSEQ: 222\n"
    )
    summary = _DSEQ_SUMMARY_RE.search(output)
    wider = _DSEQ_ANY_RE.search(output)
    assert summary is not None and wider is not None, "both patterns must still match"
    assert summary.group(1) == "222", (
        "the summary pattern no longer selects the authoritative summary line"
    )
    assert wider.group(1) == "111", (
        "the wider pattern's FIRST match is the superseded DSEQ — this is exactly "
        "why it is confined to the failure path, where no summary line exists"
    )


# ── the mechanism works; these pin that step 2 actually USES it ──────────────
# The deploy step cannot be executed in a unit test — it needs live Akash creds and
# spends real escrow — so its wiring is asserted structurally, the way this repo
# already pins un-runnable workflow code.

import ast  # noqa: E402

_SRC = (Path(__file__).resolve().parents[1] / "just_akash" / "test_shell_e2e.py").read_text(
    encoding="utf-8"
)
_TREE = ast.parse(_SRC)
_PARENT: dict = {}
for _n in ast.walk(_TREE):
    for _c in ast.iter_child_nodes(_n):
        _PARENT[_c] = _n


def _ancestors(node):
    while node in _PARENT:
        node = _PARENT[node]
        yield node


def _deploy_call():
    for n in ast.walk(_TREE):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "run"
            and "just up" in (ast.get_source_segment(_SRC, n) or "")
        ):
            return n
    return None


def test_the_deploy_call_cannot_escape_uncaught():
    """The exception escaped before the DSEQ was parsed AND before the cleanup.

    Two things must hold together: the call is inside a `try`, and that `try`
    names TimeoutExpired. A bare `except Exception` would satisfy the first and
    silently swallow the thing this is about.
    """
    call = _deploy_call()
    assert call is not None, "the `just up` deploy call moved — re-anchor, do not delete"
    tries = [a for a in _ancestors(call) if isinstance(a, ast.Try)]
    assert tries, "the deploy call is not inside a try — a timeout escapes the whole run"
    handler_src = " ".join(ast.get_source_segment(_SRC, h) or "" for h in tries[0].handlers)
    assert "TimeoutExpired" in handler_src, (
        "the enclosing try does not name TimeoutExpired, so a SIGKILLed deploy still "
        "escapes before the DSEQ is parsed"
    )
    assert "_decoded" in handler_src, (
        "the handler does not run the child's output through _decoded — "
        "TimeoutExpired carries BYTES, so the DSEQ is discarded on a type mismatch"
    )


def test_every_dseq_less_exit_reports_the_unnamed_deployment():
    """A `sys.exit(1)` that leaves no identifier must say so.

    Both DSEQ-less exits in step 2 are the whole issue: the run stops, and unless
    it states the window, nothing anywhere points at what it may have created.
    """
    fn = next(n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef) and n.name == "main")
    reports = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "report_unnamed_deployment"
    ]
    assert len(reports) == 2, (
        f"expected the report on BOTH DSEQ-less exits, found {len(reports)} — the "
        "rc!=0-with-nothing-recovered path and the unparsable-output path"
    )


def test_the_report_states_the_window_and_closes_nothing():
    """⛔ ESCROW BOUNDARY, pinned in the structure rather than trusted to review.

    The harness may destroy a deployment it created AND can identify. One it
    cannot identify is not its to guess at: the Console listing is not scoped
    server-side, so "close what looks unowned" closes other people's. Reporting
    is the whole remit of this function and it must stay that way.
    """
    fn = next(
        n
        for n in ast.walk(_TREE)
        if isinstance(n, ast.FunctionDef) and n.name == "report_unnamed_deployment"
    )
    # ⛔ CALLS, not a substring over the source. The first version of this test
    # searched the text and fired on the word "destroy" in this function's own
    # DOCSTRING — a guard that goes red for prose while a real call could be spelled
    # any number of ways it never checks. Break the code, not the commentary.
    called = {
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
    }
    closers = {c for c in called if "destroy" in c or "close" in c}
    assert not closers, (
        f"report_unnamed_deployment calls {sorted(closers)} — it reports a window it "
        "cannot resolve, and closing an unidentified deployment closes someone else's"
    )
    assert "created-between" in (ast.get_source_segment(_SRC, fn) or ""), (
        "the report no longer states a time window, which is the only thing that makes "
        "an unnamed deployment findable by a later owner-scoped chain query"
    )


# ── the durable copy: `just up` tees the deploy to a fixed path ──────────────

import os  # noqa: E402
import time  # noqa: E402

from just_akash import test_shell_e2e as _tse  # noqa: E402


def _tee_log(monkeypatch, tmp_path, content: str | None, *, age: float = 0.0):
    """Point the module at a temp tee-log, optionally aged into the past."""
    path = tmp_path / "akash-last-deploy.log"
    if content is not None:
        path.write_text(content, encoding="utf-8")
        if age:
            past = time.time() - age
            os.utime(path, (past, past))
    monkeypatch.setattr(_tse, "_DEPLOY_TEE_LOG", str(path))
    return path


def test_the_dseq_is_recovered_from_the_file_when_the_pipe_gave_nothing(monkeypatch, tmp_path):
    """The identifier was on disk the whole time; the run simply never looked."""
    _tee_log(monkeypatch, tmp_path, "[ts] Deployment created  DSEQ=1788999000333  x\n")
    assert _tse._dseq_from_deploy_log(time.time() - 5, wait=0.0) == "1788999000333"


def test_a_tee_log_older_than_this_run_is_treated_as_no_log_at_all(monkeypatch, tmp_path):
    """⛔ THE STALENESS GUARD, and it is the dangerous one.

    The path is FIXED, so a previous `just up` leaves a complete, well-formed,
    entirely wrong file. Without an mtime check this function would confidently
    return another run's DSEQ — which the caller then hands to `robust_destroy`.
    Recovering the wrong identifier is worse than recovering none: it closes a
    live deployment and reports success while doing it.
    """
    _tee_log(monkeypatch, tmp_path, "DSEQ=1111111111111\n", age=3600)
    assert _tse._dseq_from_deploy_log(time.time(), wait=0.0) is None


def test_an_ambiguous_tee_log_names_nothing(monkeypatch, tmp_path):
    """A re-deploy round supersedes an earlier DSEQ; the file cannot say which is live."""
    _tee_log(
        monkeypatch,
        tmp_path,
        "Deployment created  DSEQ=111\nRe-deployed: new order DSEQ=222\n",
    )
    assert _tse._dseq_from_deploy_log(time.time() - 5, wait=0.0) is None


def test_a_missing_tee_log_is_not_an_error(monkeypatch, tmp_path):
    """The deploy may have died before `tee` ever created the file."""
    _tee_log(monkeypatch, tmp_path, None)
    assert _tse._dseq_from_deploy_log(time.time(), wait=0.0) is None


def test_it_waits_because_the_deploy_outlives_the_run_that_gave_up(monkeypatch, tmp_path):
    """⛔ Reading once would read the file at its least complete.

    The timeout kills only the direct child, so the deploy keeps running and keeps
    writing. A file that has no DSEQ at the instant of the timeout may well have one
    a second later — which is the difference between a closable deployment and an
    unnamed one.
    """
    path = _tee_log(monkeypatch, tmp_path, "no dseq yet\n")
    started = time.time() - 5

    import threading

    def append_later():
        time.sleep(0.5)
        path.write_text("Deployment created  DSEQ=1788999000444\n", encoding="utf-8")

    threading.Thread(target=append_later, daemon=True).start()
    assert _tse._dseq_from_deploy_log(started, wait=5.0) == "1788999000444"
