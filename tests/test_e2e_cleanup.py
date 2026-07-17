"""Tests for just_akash._e2e — cleanup orchestration helpers.

These helpers are critical for "no deployment leak" guarantees. The live e2e
scripts that import them only exercise happy paths against the real Akash
network, so unit tests here pin the failure-mode behavior we depend on:
robust_destroy retries, audit-on-success, signal-handler integration, and
tier resolution from env vars.
"""

from __future__ import annotations

import signal
import subprocess
from unittest.mock import patch

import pytest

from just_akash._e2e import (
    _destroy_succeeded,
    _reset_signal_cleanup_for_tests,
    assert_provider_in_tiers,
    classify_provider,
    install_signal_cleanup,
    resolve_tiers,
    robust_destroy,
)


@pytest.fixture(autouse=True)
def _reset_e2e_state():
    """Each test starts with a clean signal registry."""
    _reset_signal_cleanup_for_tests()
    yield
    _reset_signal_cleanup_for_tests()


def _completed(
    returncode: int = 0,
    stdout: str | None = "",
    stderr: str | None = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["mock"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ── classify_provider / assert_provider_in_tiers ──────────────────────────────


class TestClassifyProvider:
    def test_empty_provider_is_unknown(self):
        assert classify_provider("", ["a"], ["b"]) == "unknown"

    def test_preferred_match(self):
        assert classify_provider("akash1pref", ["akash1pref"], []) == "preferred"

    def test_backup_match(self):
        assert classify_provider("akash1back", [], ["akash1back"]) == "backup"

    def test_neither_is_foreign(self):
        assert classify_provider("akash1other", ["akash1a"], ["akash1b"]) == "foreign"

    def test_preferred_wins_when_in_both(self):
        # Defensive guarantee: a provider listed in both tiers should be
        # classified as preferred (the higher tier).
        assert classify_provider("akash1same", ["akash1same"], ["akash1same"]) == "preferred"


class TestAssertProviderInTiers:
    def test_no_allowlist_accepts_anything(self, capsys):
        assert assert_provider_in_tiers("akash1any", [], []) is True

    def test_no_allowlist_accepts_none_provider(self, capsys):
        # When no tiers configured, even a missing provider passes (matches
        # deploy.py's "any provider" mode).
        assert assert_provider_in_tiers(None, [], []) is True

    def test_preferred_provider_passes(self, capsys):
        assert assert_provider_in_tiers("akash1pref", ["akash1pref"], ["akash1back"]) is True
        assert "PREFERRED" in capsys.readouterr().out

    def test_backup_provider_passes_with_info_log(self, capsys):
        assert assert_provider_in_tiers("akash1back", ["akash1pref"], ["akash1back"]) is True
        out = capsys.readouterr().out
        assert "BACKUP" in out

    def test_foreign_provider_fails(self, capsys):
        assert assert_provider_in_tiers("akash1foreign", ["akash1pref"], ["akash1back"]) is False
        assert "NOT in any tier" in capsys.readouterr().out

    def test_none_provider_with_allowlist_fails(self, capsys):
        assert assert_provider_in_tiers(None, ["akash1pref"], []) is False


# ── resolve_tiers ─────────────────────────────────────────────────────────────


class TestResolveTiers:
    def test_both_unset(self, monkeypatch):
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
        pref, backup, union = resolve_tiers()
        assert pref == []
        assert backup == []
        assert union == []

    def test_both_set(self, monkeypatch):
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1a,akash1b")
        monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", "akash1c")
        pref, backup, union = resolve_tiers()
        assert pref == ["akash1a", "akash1b"]
        assert backup == ["akash1c"]
        assert union == ["akash1a", "akash1b", "akash1c"]

    def test_whitespace_and_empty_entries_stripped(self, monkeypatch):
        monkeypatch.setenv("AKASH_PROVIDERS", " akash1a , , akash1b ")
        monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", " , ,")
        pref, backup, _ = resolve_tiers()
        assert pref == ["akash1a", "akash1b"]
        assert backup == []


# ── robust_destroy ────────────────────────────────────────────────────────────


class TestRobustDestroy:
    def test_empty_dseq_is_noop(self):
        # No subprocess calls should be made for empty/None dseq.
        with patch("just_akash._e2e.subprocess.run") as mock_run:
            assert robust_destroy("") is True
            assert robust_destroy("") is True
            mock_run.assert_not_called()

    def test_first_try_success(self):
        with patch("just_akash._e2e.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout="Deployment 12345 closed"),  # destroy
                _completed(0, stdout='{"state": "closed"}'),  # authoritative audit: settled
            ]
            assert robust_destroy("12345") is True
            assert mock_run.call_count == 2

    def test_retry_after_first_failure(self):
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep") as _sleep,
        ):
            mock_run.side_effect = [
                _completed(1, stderr="API down"),  # 1st destroy fails
                _completed(0, stdout="Deployment 12345 closed"),  # 2nd destroy ok
                _completed(0, stdout='{"state": "closed"}'),  # authoritative audit: settled
            ]
            assert robust_destroy("12345", retries=2) is True
            assert mock_run.call_count == 3

    def test_all_retries_fail_returns_false(self):
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep") as _sleep,
        ):
            mock_run.side_effect = [
                _completed(1, stderr="fail 1"),
                _completed(1, stderr="fail 2"),
                _completed(1, stderr="fail 3"),
                # audit still runs after retries exhausted
                _completed(0, stdout='{"state": "active"}'),
            ]
            assert robust_destroy("12345", retries=2) is False

    def test_audit_detects_lingering_deployment(self):
        # Destroy reports success, but `just list` shows the DSEQ is still
        # present — audit must flip the result to False so the caller knows
        # to flag/manual-clean.
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep") as _sleep,
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),  # destroy
                _completed(0, stdout='{"state": "active"}'),  # authoritative: STILL ACTIVE
            ]
            assert robust_destroy("12345") is False

    def test_audit_disabled_skips_list(self):
        with patch("just_akash._e2e.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _completed(0, stdout="closed"),
            ]
            assert robust_destroy("12345", audit=False) is True
            assert mock_run.call_count == 1

    def test_destroy_raises_is_caught(self):
        # Cleanup must NEVER raise — exceptions in subprocess are swallowed,
        # logged, and counted as a failed attempt.
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep") as _sleep,
        ):
            mock_run.side_effect = [
                subprocess.TimeoutExpired(cmd="just destroy", timeout=60),
                _completed(0, stdout="closed"),  # 2nd attempt succeeds
                _completed(0, stdout='{"state": "closed"}'),  # authoritative audit
            ]
            assert robust_destroy("12345") is True

    def test_audit_subprocess_raise_returns_false(self):
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep") as _sleep,
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),
                subprocess.TimeoutExpired(cmd="just list", timeout=30),
            ]
            assert robust_destroy("12345") is False


# ── install_signal_cleanup ────────────────────────────────────────────────────


class TestInstallSignalCleanup:
    def test_handler_calls_destroy_with_current_dseq(self, monkeypatch):
        # Capture installed handler, invoke it, verify it routes through
        # robust_destroy and exits with 130.
        handlers: dict = {}

        def fake_signal(sig, handler):
            handlers[sig] = handler

        monkeypatch.setattr(signal, "signal", fake_signal)

        dseq_ref: dict = {"dseq": "9999"}
        install_signal_cleanup(dseq_ref)

        assert signal.SIGINT in handlers
        assert signal.SIGTERM in handlers

        with (
            patch("just_akash._e2e.robust_destroy") as mock_destroy,
            pytest.raises(SystemExit) as exc,
        ):
            handlers[signal.SIGINT](signal.SIGINT, None)
        assert exc.value.code == 130
        mock_destroy.assert_called_once_with("9999", retries=1, audit=True)

    def test_handler_skips_destroy_when_dseq_unset(self, monkeypatch, capsys):
        handlers: dict = {}
        monkeypatch.setattr(signal, "signal", lambda sig, h: handlers.__setitem__(sig, h))

        dseq_ref: dict = {"dseq": None}
        install_signal_cleanup(dseq_ref)

        with patch("just_akash._e2e.robust_destroy") as mock_destroy, pytest.raises(SystemExit):
            handlers[signal.SIGINT](signal.SIGINT, None)
        mock_destroy.assert_not_called()
        assert "nothing to clean up" in capsys.readouterr().out


# ── Adversarial leak-prevention tests ────────────────────────────────────────
#
# These pin behaviors that, if changed silently, would let deployments leak
# (or falsely claim a leak). Each test maps to a specific gap in _e2e.py.


class TestDestroySuccessMatchesTheRealCLI:
    """The matcher and the CLI's actual wording must agree.

    They did not. robust_destroy looked for "closed"; the CLI prints "destroyed".
    So every successful destroy was scored a failure, two redundant destroys fired
    against an already-closed deployment, and each E2E run printed three red FAILs
    while the audit quietly passed. The unit tests missed it because their fixtures
    asserted against a hand-written "Deployment 12345 closed" that the CLI has never
    printed -- self-consistent, and wrong about reality.

    So don't hand-write the output here. Run the real CLI, capture what it really
    says, and feed THAT to the matcher.
    """

    @patch("just_akash.api._save_tags")
    @patch("just_akash.api._load_tags", return_value={})
    @patch("just_akash.api._resolve_dseq", return_value="12345")
    @patch("just_akash.api._get_tag", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_real_cli_success_output_is_recognized(
        self, MockAPI, mock_tag, mock_resolve, mock_load, mock_save, monkeypatch, capsys
    ):
        import sys

        from just_akash.cli import main

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr("builtins.input", lambda _: "y")
        monkeypatch.setattr(sys, "argv", ["just-akash", "destroy", "--dseq", "12345"])
        main()

        captured = capsys.readouterr()
        assert _destroy_succeeded(_completed(0, stdout=captured.out, stderr=captured.err)), (
            f"robust_destroy does not recognize the CLI's own success message "
            f"({captured.out.strip()!r}). Add the new wording to "
            f"_DESTROY_SUCCESS_WORDS -- otherwise every successful destroy is "
            f"scored a failure and retried against an already-closed deployment."
        )

    @patch("just_akash.api._resolve_dseq", return_value="12345")
    @patch("just_akash.api._get_tag", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_real_cli_cancel_output_is_not_recognized(
        self, MockAPI, mock_tag, mock_resolve, monkeypatch, capsys
    ):
        """A declined confirmation exits 0 but destroys nothing -- must not pass."""
        import sys

        from just_akash.cli import main

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr("builtins.input", lambda _: "n")
        monkeypatch.setattr(sys, "argv", ["just-akash", "destroy", "--dseq", "12345"])
        main()

        captured = capsys.readouterr()
        assert not _destroy_succeeded(_completed(0, stdout=captured.out, stderr=captured.err))

    def test_silent_success_is_still_not_trusted(self):
        """Exit 0 with no output stays a failure -- that contract is deliberate."""
        assert not _destroy_succeeded(_completed(0, stdout="", stderr=""))

    def test_nonzero_exit_is_never_success(self):
        assert not _destroy_succeeded(_completed(1, stdout="Deployment 12345 destroyed."))


class TestRobustDestroyAdversarial:
    """Adversarial cases targeting leak-detection gaps in robust_destroy."""

    def test_silent_success_treated_as_failure_then_audit_clears(self):
        """Gap #1: pin the strict 'closed'-keyword contract.

        If `just destroy` ever exits 0 with empty/silent output (already-closed,
        or a future CLI version that prints nothing), the destroy attempt is
        currently classified as FAILED — even though the deployment is gone.
        Behavior: all `retries+1` destroy attempts run, but the post-destroy
        audit (which reads the deployment's own record) saves the day and
        returns True.

        This pins two things:
          1. Silent-success is NOT auto-trusted (must see "closed" keyword).
          2. The audit is the safety net that prevents a false leak alarm.

        If a future change loosens the keyword check (e.g. accepts returncode==0
        alone), this test will fail and force re-review of the leak contract.
        """
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            # 3 destroy attempts (retries=2 → range(1, 4)) all "silent success"
            # — exit 0 with empty stdout/stderr. The audit then confirms it settled.
            mock_run.side_effect = [
                _completed(0, stdout="", stderr=""),  # destroy attempt 1
                _completed(0, stdout="", stderr=""),  # destroy attempt 2
                _completed(0, stdout="", stderr=""),  # destroy attempt 3
                _completed(0, stdout='{"state": "closed"}'),  # authoritative audit: settled
            ]
            # Audit clears it → True. But we ran 3 destroys (treating each as
            # failure). If the keyword check is loosened, only 1 destroy will
            # run and call_count will drop to 2 — failing this assertion.
            assert robust_destroy("12345", retries=2) is True
            assert mock_run.call_count == 4, (
                "Expected 3 destroy attempts (silent success rejected) + 1 "
                f"audit. Got {mock_run.call_count}. If this test fails with "
                "fewer calls, the 'closed'-keyword contract was loosened — "
                "review whether silent-success should now imply success."
            )

    def test_audit_cannot_confuse_a_dseq_with_a_longer_one(self):
        """A substring collision (dseq '123' vs an unrelated active '12345') is now
        impossible by construction, not merely guarded against: the audit asks for
        OUR deployment's own record instead of scanning a shared list, so another
        deployment's state can never enter the answer. (`_dseq_in_list_output` keeps
        its own word-boundary tests — this pins the structural property.)
        """
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="Deployment 123 closed"),
                _completed(0, stdout='{"dseq": "123", "state": "closed"}'),
            ]
            assert robust_destroy("123") is True
            audit_cmd = mock_run.call_args_list[1].args[0]
            assert "123" in audit_cmd and "12345" not in audit_cmd

    # test_audit_still_detects_exact_dseq_match was REMOVED here. It pinned
    # word-boundary matching of `just list` output at the robust_destroy level, which
    # the audit no longer does at all — it reads the deployment's own record. The
    # test kept passing, but via the "could not confirm" path (its fixture is not
    # JSON), while claiming to prove regex matching: a green assertion about
    # behaviour that no longer exists. The word-boundary contract is still pinned
    # directly on _dseq_in_list_output (TestDseqWordBoundaryRegexEdges), and
    # test_audit_cannot_confuse_a_dseq_with_a_longer_one covers the structural
    # property that replaced it.

    def test_negative_retries_clamped_to_one_attempt(self):
        """retries<0 must NEVER skip the destroy loop. Clamp to 0 retries
        (i.e. exactly 1 attempt). Otherwise a caller mistake silently leaks.
        """
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),  # destroy attempt 1 (clamped)
                _completed(0, stdout='{"state": "closed"}'),  # authoritative audit
            ]
            assert robust_destroy("12345", retries=-1) is True
            # Exactly 1 destroy + 1 audit = 2 calls. Destroy MUST run.
            assert mock_run.call_count == 2
            destroy_called = any(
                "just destroy" in str(call.args[0])
                for call in mock_run.call_args_list
                if call.args
            )
            assert destroy_called, "retries=-1 must still issue `just destroy`"

    def test_retries_zero_attempts_destroy_exactly_once(self):
        """Gap #5 boundary: retries=0 should still try destroy once.

        `range(1, 0 + 2)` = `range(1, 2)` = [1] → exactly 1 destroy attempt.
        Pin this so a future off-by-one in the retry math is caught.
        """
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),  # destroy attempt 1
                _completed(0, stdout='{"state": "closed"}'),  # authoritative audit
            ]
            assert robust_destroy("12345", retries=0) is True
            assert mock_run.call_count == 2, (
                f"retries=0 must produce exactly 1 destroy + 1 audit = 2 "
                f"subprocess calls; got {mock_run.call_count}. Off-by-one in "
                "`range(1, retries + 2)`?"
            )


class TestInstallSignalCleanupAdversarial:
    """Adversarial cases for install_signal_cleanup state ownership."""

    def test_double_install_cleans_all_registered_dseqs(self, monkeypatch):
        """Both deployments must be cleaned on SIGINT, regardless of order.

        If a process creates two deployments and installs cleanup for each,
        a single signal must destroy BOTH. Replacing the handler on the
        second install would orphan the first deployment.
        """
        handlers: dict = {}
        monkeypatch.setattr(signal, "signal", lambda sig, h: handlers.__setitem__(sig, h))

        dseq_ref_a: dict = {"dseq": "AAAA"}
        install_signal_cleanup(dseq_ref_a)

        dseq_ref_b: dict = {"dseq": "BBBB"}
        install_signal_cleanup(dseq_ref_b)

        with (
            patch("just_akash._e2e.robust_destroy") as mock_destroy,
            pytest.raises(SystemExit) as exc,
        ):
            handlers[signal.SIGINT](signal.SIGINT, None)
        assert exc.value.code == 130

        called_dseqs = {call.args[0] for call in mock_destroy.call_args_list}
        assert called_dseqs == {"AAAA", "BBBB"}, (
            f"Expected BOTH dseqs cleaned, got {called_dseqs}. The first "
            "install_signal_cleanup must not be orphaned by a later one."
        )

    def test_double_install_does_not_re_register_signals(self, monkeypatch):
        """signal.signal must be called once for SIGINT and once for SIGTERM,
        even after multiple install_signal_cleanup calls. Re-registering
        risks confusing OS-level handler state.
        """
        signal_calls: list = []
        monkeypatch.setattr(
            signal,
            "signal",
            lambda sig, h: signal_calls.append(sig),
        )
        install_signal_cleanup({"dseq": "X"})
        install_signal_cleanup({"dseq": "Y"})
        install_signal_cleanup({"dseq": "Z"})
        # First install registers SIGINT + SIGTERM. Subsequent calls just
        # append to the registry — they don't reinstall the OS handler.
        assert signal_calls.count(signal.SIGINT) == 1
        assert signal_calls.count(signal.SIGTERM) == 1

    def test_same_ref_installed_twice_is_deduped(self, monkeypatch):
        """Re-installing the SAME dseq_ref must not double-destroy it."""
        handlers: dict = {}
        monkeypatch.setattr(signal, "signal", lambda sig, h: handlers.__setitem__(sig, h))

        ref = {"dseq": "ONLY"}
        install_signal_cleanup(ref)
        install_signal_cleanup(ref)  # same dict identity → no-op append

        with patch("just_akash._e2e.robust_destroy") as mock_destroy, pytest.raises(SystemExit):
            handlers[signal.SIGINT](signal.SIGINT, None)
        assert mock_destroy.call_count == 1


# ── Iter-2 adversarial tests ─────────────────────────────────────────────────
#
# Iter 1 fixed: word-boundary DSEQ match, retries<0 clamping, multi-install
# signal cleanup. Iter 2 probes the surface iter 1 didn't touch.


class TestSignalHandlerReentrancy:
    """Reentrancy guard: a second signal during cleanup is a no-op.

    The first signal "wins" and is allowed to finish. Without the guard,
    impatient double-Ctrl-C re-enters the handler recursively, causing every
    registered ref to be destroyed once per re-entry level.
    """

    def test_reentrant_handler_is_a_noop(self, monkeypatch):
        """A signal arriving while a previous cleanup is in progress must
        NOT re-iterate the registry. The inner handler call returns without
        invoking robust_destroy again.
        """
        handlers: dict = {}
        monkeypatch.setattr(signal, "signal", lambda sig, h: handlers.__setitem__(sig, h))

        from just_akash._e2e import _signal_handler

        ref = {"dseq": "ONCE"}
        install_signal_cleanup(ref)

        depth = {"n": 0}

        def maybe_reenter(dseq, **_kw):
            depth["n"] += 1
            if depth["n"] == 1:
                # Simulate a 2nd SIGINT arriving DURING the first cleanup.
                # The guard must short-circuit: this call returns immediately,
                # robust_destroy is NOT invoked a second time, depth stays 1.
                _signal_handler(signal.SIGINT, None)

        with (
            patch("just_akash._e2e.robust_destroy", side_effect=maybe_reenter),
            pytest.raises(SystemExit) as exc,
        ):
            handlers[signal.SIGINT](signal.SIGINT, None)
        assert exc.value.code == 130
        assert depth["n"] == 1, (
            "Reentrancy guard must make a re-entered handler a no-op. "
            f"Got {depth['n']} destroy calls (expected 1)."
        )

    def test_guard_resets_after_handler_returns(self, monkeypatch):
        """After a normal cleanup completes, a SUBSEQUENT signal (from a
        new cleanup cycle) must be honored — the guard isn't permanently
        latched.
        """
        import just_akash._e2e as e2e_mod

        assert e2e_mod._HANDLER_RUNNING is False

        handlers: dict = {}
        monkeypatch.setattr(signal, "signal", lambda sig, h: handlers.__setitem__(sig, h))
        install_signal_cleanup({"dseq": "X"})

        with patch("just_akash._e2e.robust_destroy"), pytest.raises(SystemExit):
            handlers[signal.SIGINT](signal.SIGINT, None)

        # Guard is reset by the finally clause before sys.exit fires.
        assert e2e_mod._HANDLER_RUNNING is False, (
            "Guard must reset in `finally` so a fresh signal cycle later "
            "in the process can run cleanup again."
        )


class TestSignalHandlerSelectiveCleanup:
    """Gap #2: registry accumulates across long-running processes.

    The dseq_ref for a successfully-cleaned deployment has its `dseq` field
    wiped (set to None) by the e2e scripts. The handler skips refs with no
    dseq via `if dseq`. Pin that contract: if 3 refs are registered and one
    is wiped, the handler destroys exactly the 2 remaining ones — not the
    completed one.
    """

    def test_handler_skips_wiped_refs_among_many(self, monkeypatch):
        handlers: dict = {}
        monkeypatch.setattr(signal, "signal", lambda sig, h: handlers.__setitem__(sig, h))

        ref_a = {"dseq": "AAA"}
        ref_done: dict = {"dseq": "DONE"}
        ref_b = {"dseq": "BBB"}
        install_signal_cleanup(ref_a)
        install_signal_cleanup(ref_done)
        install_signal_cleanup(ref_b)

        # Simulate ref_done having been cleanly destroyed before the signal
        # arrived: the e2e script wipes its dseq to None.
        ref_done["dseq"] = None

        with patch("just_akash._e2e.robust_destroy") as mock_destroy, pytest.raises(SystemExit):
            handlers[signal.SIGINT](signal.SIGINT, None)

        called = {call.args[0] for call in mock_destroy.call_args_list}
        assert called == {"AAA", "BBB"}, (
            f"Handler destroyed {called}; expected only 'AAA' and 'BBB'. "
            "If 'DONE' appears, the wiped-ref skip contract was lost — "
            "completed deployments will be re-destroyed (wasting tokens, "
            "potentially erroring on already-closed deployments)."
        )

    def test_handler_with_all_refs_wiped_is_safe(self, monkeypatch, capsys):
        """All registered refs have already been cleaned (dseq=None). Handler
        must NOT call robust_destroy at all, and must still exit 130 cleanly
        with the 'nothing to clean up' info log.
        """
        handlers: dict = {}
        monkeypatch.setattr(signal, "signal", lambda sig, h: handlers.__setitem__(sig, h))

        ref1 = {"dseq": None}
        ref2 = {"dseq": ""}  # also falsy — pin the `or ""` contract
        install_signal_cleanup(ref1)
        install_signal_cleanup(ref2)

        with (
            patch("just_akash._e2e.robust_destroy") as mock_destroy,
            pytest.raises(SystemExit) as exc,
        ):
            handlers[signal.SIGINT](signal.SIGINT, None)
        assert exc.value.code == 130
        mock_destroy.assert_not_called()
        assert "nothing to clean up" in capsys.readouterr().out


class TestDseqWordBoundaryRegexEdges:
    """Gap #5: pin word-boundary regex behavior at string edges and adjacent
    digit/non-digit transitions. The tests below exercise _dseq_in_list_output
    directly so a future regex tweak (e.g. switching to plain `\\b` which
    matches between digit/letter, or accidentally widening the lookaround)
    is caught without needing the full robust_destroy harness.
    """

    @pytest.fixture
    def fn(self):
        from just_akash._e2e import _dseq_in_list_output

        return _dseq_in_list_output

    def test_dseq_alone_matches(self, fn):
        assert fn("12345", "12345") is True

    def test_dseq_at_start_of_string(self, fn):
        assert fn("12345", "12345 closed") is True

    def test_dseq_at_end_of_string_no_newline(self, fn):
        assert fn("12345", "active 12345") is True

    def test_dseq_at_end_of_string_with_newline(self, fn):
        assert fn("12345", "active 12345\n") is True

    def test_dseq_only_followed_by_newline(self, fn):
        assert fn("12345", "\n12345\n") is True

    def test_dseq_as_prefix_substring_does_not_match(self, fn):
        # dseq=123 vs list output containing only 12345 — must NOT match.
        assert fn("123", "deployment 12345 active") is False

    def test_dseq_as_suffix_substring_does_not_match(self, fn):
        # dseq=345 vs list output containing only 12345 — must NOT match.
        assert fn("345", "deployment 12345 active") is False

    def test_dseq_as_inner_substring_does_not_match(self, fn):
        # dseq=234 vs list output containing only 12345 — must NOT match.
        assert fn("234", "deployment 12345 active") is False

    def test_dseq_adjacent_to_other_dseq_with_space(self, fn):
        # "123 12345" — looking for 12345; the 123 next to it must not
        # confuse the lookbehind. Space is non-digit so 12345 matches.
        assert fn("12345", "123 12345") is True

    def test_dseq_adjacent_to_other_dseq_searching_shorter(self, fn):
        # "123 12345" — looking for 123. The 123 alone matches (followed by
        # space), even though 12345 contains 123 as a prefix.
        assert fn("123", "123 12345") is True

    def test_dseq_with_regex_metacharacters_is_escaped(self, fn):
        # DSEQs are numeric in production, but the function takes str. If
        # someone passes "123." (dot = regex any-char), re.escape must
        # neutralize it: "123." should match LITERAL "123." only, not
        # "1234" or "123X".
        assert fn("123.", "deployment 123. closed") is True
        assert fn("123.", "deployment 1234 closed") is False
        assert fn("123.", "deployment 123X closed") is False

    def test_empty_dseq_returns_false(self, fn):
        # Pin the early-return contract: empty dseq never matches anything.
        assert fn("", "anything 12345 here") is False
        assert fn("", "") is False

    def test_audit_handles_none_stdout_gracefully(self):
        """If subprocess somehow returns None stdout (text=False mode, weird
        process death), _dseq_in_list_output raises TypeError on re.search.
        robust_destroy's outer try/except Exception must catch it → False.
        Pin this so a refactor that narrows the except (e.g. to ValueError)
        gets caught.
        """
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),  # destroy
                _completed(0, stdout=None),  # audit returns None stdout
            ]
            # re.search(pattern, None) raises TypeError → outer except catches
            # → returns False (treat unknown audit as failure to be safe).
            assert robust_destroy("12345") is False


class TestRunShellInjectionContract:
    """Gap #8: `_run` uses `shell=True` and f-string interpolation of dseq.

    Today: dseq="12345 ; rm -rf /tmp/foo" would execute the second command.
    DSEQs from real Akash are always numeric so this isn't an active exploit,
    but it IS a security latent. Pin the current contract so a future hardening
    (shlex.quote / list-of-args) has a clear regression target — if someone
    decides to quote dseq, this test will fail and force a deliberate update.

    NOTE: this test does NOT execute the injected payload. It only inspects
    the cmd string passed to subprocess.run.
    """

    def test_dseq_is_passed_unquoted_to_shell_today(self):
        """Pin: today, dseq is interpolated raw into the shell command.

        If/when the implementation switches to shlex.quote or argv list,
        update this test (and add a corresponding test that injection no
        longer works).
        """
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),
                _completed(0, stdout=""),
            ]
            # The injection payload — adversarial input.
            payload = "12345 ; echo PWNED"
            robust_destroy(payload, audit=True)

            destroy_call = mock_run.call_args_list[0]
            cmd = destroy_call.args[0]
            # Today the cmd contains the raw payload — no shell-escaping.
            assert cmd == f"just destroy {payload}", (
                f"Expected raw interpolation today; got {cmd!r}. If this "
                "fails because dseq is now shell-quoted, that's a SECURITY "
                "WIN — update this test to assert the quoted form, AND add "
                "a positive test that '; echo PWNED' is no longer parsed "
                "as a separate command."
            )
            # Confirm shell=True is still used (the reason injection works).
            assert destroy_call.kwargs.get("shell") is True, (
                "shell=True is required for the current f-string command "
                "format. If shell=False, the payload would be safe — "
                "update this test to reflect the new contract."
            )

    def test_audit_command_quotes_the_dseq_so_it_stays_injection_safe(self):
        """The audit can no longer be a static literal — it must name the deployment
        it is auditing, because `just list` cannot be trusted to say whether escrow
        is held. The injection-safety GUARANTEE is preserved by quoting: _run uses
        shell=True, so an unquoted dseq would be a live injection vector.
        """
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),
                _completed(0, stdout='{"state": "closed"}'),
            ]
            robust_destroy("12345; rm -rf /tmp/pwned")
            audit_cmd = mock_run.call_args_list[1].args[0]
            assert "'12345; rm -rf /tmp/pwned'" in audit_cmd, (
                f"the dseq must be shell-quoted in the audit command; got: {audit_cmd}. "
                "Unquoted user input under shell=True is an injection vector."
            )


# ── Iter-3 adversarial tests ─────────────────────────────────────────────────
#
# Iter 1+2 pinned: word-boundary DSEQ match, retries<0 clamping, multi-install
# accumulation, reentrancy guard, selective-skip of wiped refs. Iter 3 targets
# the surface those iters left untouched: registry-mutation-during-cleanup
# (defensive copy contract), exit-code stability across SIGTERM, audit=False
# combined with destroy failure (trust-the-destroy contract), handler print
# format, and assert_provider_in_tiers empty-string contract.


class TestSignalHandlerRegistryMutationDuringCleanup:
    """Angle #3: pin the defensive-copy contract for the live registry.

    `_signal_handler` iterates `list(_REGISTERED_DSEQ_REFS)` — a snapshot at
    handler entry. If `robust_destroy` (or any callee transitively) registers
    a NEW dseq_ref via `install_signal_cleanup` mid-cleanup, that new ref is
    NOT visited by the in-flight handler.

    This is a deliberate design choice (snapshot iteration) but worth pinning
    so a future "switch to live iteration" change is conscious — live iteration
    over a mutating list either skips entries (CPython slice semantics) or
    raises depending on the loop form. Either is a regression risk.
    """

    def test_ref_registered_during_cleanup_is_not_visited(self, monkeypatch):
        """A nested install_signal_cleanup during destroy() must NOT cause the
        new ref to be cleaned up by the same in-flight handler call.

        Rationale: the snapshot is taken at handler entry. New refs added
        after that will be cleaned up by a SUBSEQUENT signal — or by normal
        finally-block cleanup — not retroactively by the current signal.
        """
        handlers: dict = {}
        monkeypatch.setattr(signal, "signal", lambda sig, h: handlers.__setitem__(sig, h))

        ref_orig = {"dseq": "ORIG"}
        install_signal_cleanup(ref_orig)

        # Track every dseq passed to robust_destroy. Side effect on the FIRST
        # call: register a NEW ref mid-cleanup, simulating something that
        # transitively triggers another `install_signal_cleanup`.
        seen: list = []
        new_ref = {"dseq": "MID_CLEANUP"}

        def fake_destroy(dseq, **_kw):
            seen.append(dseq)
            if dseq == "ORIG":
                # Register a new ref while the handler is still running.
                # Must NOT be picked up by the current iteration.
                install_signal_cleanup(new_ref)

        with (
            patch("just_akash._e2e.robust_destroy", side_effect=fake_destroy),
            pytest.raises(SystemExit) as exc,
        ):
            handlers[signal.SIGINT](signal.SIGINT, None)
        assert exc.value.code == 130

        # Snapshot taken at entry only had ORIG. MID_CLEANUP was added during
        # the loop but the snapshot is `list(...)` — a frozen copy.
        assert seen == ["ORIG"], (
            f"Expected only ORIG to be cleaned by the in-flight handler; got "
            f"{seen}. If MID_CLEANUP appears, the defensive snapshot was "
            "removed and the handler now iterates the live list. That's a "
            "design change worth reviewing — refs registered mid-cleanup may "
            "be visited or skipped depending on Python list-iteration "
            "semantics, neither of which is obviously correct."
        )

        # The new ref IS still in the registry — a subsequent signal would
        # find it. (We don't fire a second signal here; that's tested
        # separately by guard-reset tests.)
        from just_akash._e2e import _REGISTERED_DSEQ_REFS

        assert new_ref in _REGISTERED_DSEQ_REFS, (
            "The mid-cleanup-registered ref must remain in the registry so "
            "a follow-up signal can clean it up."
        )


class TestSignalHandlerExitCodeStability:
    """Angle #2: pin that the handler always exits 130 — even for SIGTERM.

    Today's handler does `sys.exit(130)` regardless of `signum`. Conventional
    exit codes are 128+signum (SIGINT=130, SIGTERM=143). Choosing 130 for both
    is a simplification — pin it so a future "signum-aware exit" change is
    deliberate and reviewed (e.g. some CI systems treat 130 specially as
    user-interrupt vs. 143 as orchestrator-kill).
    """

    def test_sigterm_also_exits_130(self, monkeypatch):
        handlers: dict = {}
        monkeypatch.setattr(signal, "signal", lambda sig, h: handlers.__setitem__(sig, h))

        install_signal_cleanup({"dseq": "X"})

        with patch("just_akash._e2e.robust_destroy"), pytest.raises(SystemExit) as exc:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        # NOT 143 (128+SIGTERM=15). Today's contract: 130 for any signal.
        # If this fails with 143, someone made the exit code signum-aware —
        # that's a behavior change deserving a deliberate test update.
        assert exc.value.code == 130, (
            f"Today the handler exits 130 for ANY signal (SIGTERM included); "
            f"got {exc.value.code}. If signum-aware exit codes were added, "
            "update this test AND document the new contract."
        )

    def test_handler_log_includes_signal_name(self, monkeypatch, capsys):
        """Angle #6: pin the handler's print format — signal name appears in
        the INTERRUPTED line. A future change that drops the signal name (e.g.
        "INTERRUPTED — running cleanup..." with no sig info) loses operator
        visibility into WHICH signal triggered cleanup. Pin the current
        format so the change is deliberate.
        """
        handlers: dict = {}
        monkeypatch.setattr(signal, "signal", lambda sig, h: handlers.__setitem__(sig, h))

        install_signal_cleanup({"dseq": "X"})

        with patch("just_akash._e2e.robust_destroy"), pytest.raises(SystemExit):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        out = capsys.readouterr().out
        # Pin: signal name appears, in uppercase form (signal.Signals.name).
        assert "SIGTERM" in out, (
            f"Handler must surface the signal name in its log; output was: "
            f"{out!r}. Operators rely on this to distinguish CI-kill (SIGTERM) "
            "from user-Ctrl-C (SIGINT) in retro debugging."
        )
        assert "INTERRUPTED" in out, (
            "Handler must surface the 'INTERRUPTED' marker so log scanners "
            "can detect cleanup-from-signal vs. normal cleanup."
        )

    def test_handler_log_includes_ansi_color_codes_today(self, monkeypatch, capsys):
        """Angle #6: pin that ANSI color codes are emitted unconditionally
        today — no isatty() / NO_COLOR check. If a future change adds
        color-suppression, this test will fail and force a deliberate update
        (and a new positive test for the no-color path).
        """
        handlers: dict = {}
        monkeypatch.setattr(signal, "signal", lambda sig, h: handlers.__setitem__(sig, h))

        install_signal_cleanup({"dseq": "X"})

        with patch("just_akash._e2e.robust_destroy"), pytest.raises(SystemExit):
            handlers[signal.SIGINT](signal.SIGINT, None)
        out = capsys.readouterr().out
        # \033[91m is RED, \033[0m is RESET. Today they're always present.
        assert "\033[91m" in out, (
            "Handler unconditionally emits ANSI red for the INTERRUPTED tag. "
            "If color is now isatty-gated or NO_COLOR-gated, update this "
            "test to assert the gating behavior."
        )
        assert "\033[0m" in out, "RESET escape must follow the colored token"


class TestRobustDestroyAuditFalseTrustsDestroyResult:
    """Angle #8: pin the deliberate `audit=False` contract.

    With `audit=False`, robust_destroy returns True UNCONDITIONALLY after the
    destroy loop — even if every destroy attempt returned a non-zero exit
    code. This is a *deliberate* design: callers that pass `audit=False` are
    asserting "I trust the destroy command, don't pay the audit roundtrip."

    This test pins that contract so a future "tighten the loop" change (e.g.
    return False on destroy failure even with audit=False) is conscious. If
    the contract DOES change, the signal handler — which calls robust_destroy
    with audit=True — is unaffected, but any caller that uses audit=False
    needs review.
    """

    def test_audit_false_returns_true_even_when_all_destroys_fail(self):
        """Pin: audit=False + every destroy fails → still returns True.

        This LOOKS like a leak bug (failed destroy reported as success) but
        is the documented contract: audit=False means "don't validate."
        Callers needing leak-detection MUST use audit=True (the default).
        """
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(1, stderr="destroy fail 1"),
                _completed(1, stderr="destroy fail 2"),
                _completed(1, stderr="destroy fail 3"),
            ]
            # All 3 destroy attempts (retries=2 → 3 attempts) fail. With
            # audit=False, no list call is made. Result: True.
            assert robust_destroy("12345", retries=2, audit=False) is True, (
                "audit=False contract: trust the destroy result, return True. "
                "If this fails (returns False), the contract was tightened — "
                "review every audit=False caller and update this test."
            )
            assert mock_run.call_count == 3, (
                "audit=False must NOT issue the audit probe at all; "
                f"got {mock_run.call_count} subprocess calls (expected 3 "
                "destroys, 0 audits)."
            )

    def test_audit_false_with_negative_retries_still_runs_destroy_once(self):
        """Combined boundary: audit=False AND retries=-1.

        retries=-1 is clamped to 0 (1 destroy attempt). audit=False skips
        the audit. Even with the destroy failing, returns True. Pins both
        the clamping and the audit=False trust contract simultaneously.
        """
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(1, stderr="destroy failed"),
            ]
            assert robust_destroy("12345", retries=-1, audit=False) is True
            assert mock_run.call_count == 1, (
                "retries=-1 clamps to 1 attempt; audit=False skips audit. "
                f"Expected exactly 1 subprocess call; got {mock_run.call_count}."
            )

    def test_audit_true_with_negative_retries_and_destroy_failure_returns_false(self):
        """Counterpoint to the audit=False test: with audit=True (default),
        a failed destroy + lingering DSEQ in audit MUST return False. Confirms
        the audit-as-safety-net contract isn't accidentally bypassed by the
        retries-clamping logic.
        """
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(1, stderr="destroy failed"),  # 1 attempt (clamped)
                _completed(0, stdout='{"state": "active"}'),  # audit: still here
            ]
            assert robust_destroy("12345", retries=-1, audit=True) is False, (
                "When the destroy fails AND the audit shows the DSEQ is "
                "still listed, robust_destroy must return False — even with "
                "the retries-clamping path. The audit safety net must always "
                "fire when audit=True."
            )


class TestAssertProviderInTiersEmptyString:
    """Angle #7: pin the empty-string-with-allowlist failure contract.

    With at least one tier configured, an empty-string provider must FAIL
    the assertion. The function flows through classify_provider which returns
    "unknown" for empty strings, falling out to the False return.

    This is a critical contract: if a deploy fails to populate the provider
    field but the dict still has a key, the empty string must not silently
    pass when an allowlist is set.
    """

    def test_empty_string_provider_with_preferred_set_fails(self, capsys):
        assert assert_provider_in_tiers("", ["akash1pref"], []) is False, (
            "Empty provider with an allowlist configured MUST fail. Otherwise "
            "a deploy that didn't set the provider field would silently bypass "
            "the tier check."
        )
        out = capsys.readouterr().out
        assert "NOT in any tier" in out

    def test_empty_string_provider_with_backup_set_fails(self, capsys):
        assert assert_provider_in_tiers("", [], ["akash1back"]) is False

    def test_empty_string_provider_with_both_tiers_set_fails(self, capsys):
        assert assert_provider_in_tiers("", ["akash1pref"], ["akash1back"]) is False


# ── Iter-4 adversarial tests ─────────────────────────────────────────────────
#
# Iter 1+2 fixed real bugs (substring DSEQ collision, retries<0 silent skip,
# double-install orphan, signal-handler reentrancy). Iter 3 added contract
# pins. Iter 4 probes the surfaces those iters didn't touch:
#   - KeyboardInterrupt propagation through robust_destroy (BaseException
#     escapes `except Exception`)
#   - Timeout constants for cleanup subprocess calls (cleanup must not hang)
#   - Operator-facing log content on the success path (attempt number)
#   - Failure-log content for tier-miss (must include both lists for debug)
#   - Falsy-dseq disambiguation (None/0 vs the string "0")


class TestRobustDestroyKeyboardInterruptPropagates:
    """Angle #1: pin that KeyboardInterrupt propagates OUT of robust_destroy.

    `except Exception` does NOT catch BaseException subclasses (KeyboardInterrupt,
    SystemExit). This is intentional: if a user Ctrl-Cs the process while the
    OS-level SIGINT handler is the Python default (i.e. `install_signal_cleanup`
    was never called, OR the caller is using robust_destroy outside the
    signal-handler installation flow), Python raises KeyboardInterrupt at the
    next bytecode boundary — including during `subprocess.run`'s blocking wait.

    Today's contract: that KeyboardInterrupt bubbles up uncaught, letting the
    interpreter's normal interrupt semantics fire. If a future "harden cleanup"
    refactor widens the except to `except BaseException`, that would SWALLOW
    Ctrl-C inside cleanup loops — leaving the user unable to interrupt a
    misbehaving destroy retry loop. Pin the current narrow-catch contract.
    """

    def test_keyboard_interrupt_during_destroy_subprocess_propagates(self):
        """A KeyboardInterrupt raised by subprocess.run (default SIGINT path)
        must NOT be swallowed by robust_destroy's `except Exception`.
        """
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = KeyboardInterrupt()
            with pytest.raises(KeyboardInterrupt):
                robust_destroy("12345", retries=2)
            # Critical: only ONE subprocess.run call before the KeyboardInterrupt
            # propagates. The retry loop must NOT swallow it and continue
            # retrying — that would defeat user interrupt.
            assert mock_run.call_count == 1, (
                f"KeyboardInterrupt must abort the retry loop immediately; "
                f"got {mock_run.call_count} subprocess calls. If >1, the "
                "except clause was widened to BaseException — Ctrl-C is now "
                "uninterruptible inside cleanup. Revert."
            )

    def test_system_exit_during_destroy_also_propagates(self):
        """Sister contract: SystemExit (BaseException) likewise propagates.

        If subprocess.run somehow raises SystemExit (e.g. a future patch in
        a test harness), robust_destroy must NOT swallow it. Pinning both
        BaseException subclasses guards the narrow-except contract.
        """
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = SystemExit(42)
            with pytest.raises(SystemExit) as exc:
                robust_destroy("12345", retries=2)
            assert exc.value.code == 42
            assert mock_run.call_count == 1


class TestRobustDestroyTimeoutContract:
    """Angle #2: pin the timeout constants for cleanup subprocess calls.

    `robust_destroy` passes `timeout=60` to `just destroy` and `timeout=30`
    to `just list`. These are part of the cleanup contract: cleanup must not
    hang forever, otherwise a misbehaving Akash CLI could wedge the entire
    test suite (or, worse, a CI pipeline with a deployment leak still pending).

    If a future refactor makes timeouts configurable / parameterized, the
    DEFAULTS must remain bounded. Pin the current values so the change is
    deliberate.
    """

    def test_destroy_subprocess_uses_60s_timeout(self):
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),
                _completed(0, stdout=""),
            ]
            robust_destroy("12345")
            destroy_call = mock_run.call_args_list[0]
            # _run forwards timeout via kwargs to subprocess.run.
            assert destroy_call.kwargs.get("timeout") == 60, (
                f"Destroy subprocess must have a bounded 60s timeout; got "
                f"{destroy_call.kwargs.get('timeout')!r}. If None or larger, "
                "cleanup can hang indefinitely on a stuck `just destroy` — "
                "leak window grows. Revert to the 60s contract."
            )

    def test_audit_list_subprocess_uses_30s_timeout(self):
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),
                _completed(0, stdout=""),
            ]
            robust_destroy("12345")
            audit_call = mock_run.call_args_list[1]
            assert audit_call.kwargs.get("timeout") == 30, (
                f"Audit `just list` must have a bounded 30s timeout; got "
                f"{audit_call.kwargs.get('timeout')!r}. The audit is read-only "
                "and should be FAST — a slow audit means we can't confirm "
                "no-leak in a reasonable window."
            )


class TestRobustDestroySuccessLogContent:
    """Angle #5: pin the operator-facing success log includes attempt number.

    On first-try success, the log reads `Deployment {dseq} closed (attempt 1)`.
    The attempt number is part of the audit trail — operators reviewing CI
    logs use it to distinguish "destroyed cleanly" from "needed retries"
    (which signals provider flakiness worth investigating).

    Pin both the dseq and the attempt-1 marker so a future log-rewrite is
    deliberate.
    """

    def test_first_try_success_log_includes_dseq_and_attempt_one(self, capsys):
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="Deployment 12345 closed"),
                _completed(0, stdout='{"state": "closed"}'),
            ]
            assert robust_destroy("12345") is True
            out = capsys.readouterr().out
            assert "12345" in out, "Success log must include the DSEQ for operator traceability."
            assert "attempt 1" in out, (
                f"Success log must include attempt number; output was: {out!r}. "
                "Operators rely on 'attempt N' to detect flaky destroy paths."
            )

    def test_retry_success_log_includes_higher_attempt_number(self, capsys):
        """When success requires a retry, the log must reflect the actual
        attempt number (NOT always 1). This pins that the attempt counter is
        a live value, not a hardcoded format string.
        """
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(1, stderr="API down"),  # attempt 1 fails
                _completed(0, stdout="Deployment 12345 closed"),  # attempt 2 ok
                _completed(0, stdout='{"state": "closed"}'),  # authoritative audit
            ]
            assert robust_destroy("12345", retries=2) is True
            out = capsys.readouterr().out
            assert "attempt 2" in out, (
                f"Retry success must log the correct attempt number; got: {out!r}. "
                "Hardcoding 'attempt 1' would mask retry occurrences."
            )


class TestAssertProviderInTiersFailureLogContent:
    """Angle #3: pin that the tier-miss FAIL log surfaces both tier lists.

    When a foreign provider is detected, the e2e script aborts and the
    operator must debug why. The fail log includes `preferred=[...] backup=[...]`
    so the operator can immediately see what was configured vs. what was
    selected — without grepping env vars or re-running with verbose mode.

    Pin this so a future "shorter log" refactor doesn't strip the lists and
    leave operators flying blind.
    """

    def test_foreign_provider_log_includes_preferred_and_backup_lists(self, capsys):
        preferred = ["akash1pref-a", "akash1pref-b"]
        backup = ["akash1back-c"]
        result = assert_provider_in_tiers("akash1foreign", preferred, backup)
        assert result is False
        out = capsys.readouterr().out
        # Both list reprs must appear so operators can debug from the log alone.
        assert "akash1pref-a" in out and "akash1pref-b" in out, (
            f"Failure log must include preferred list contents; got: {out!r}."
        )
        assert "akash1back-c" in out, (
            f"Failure log must include backup list contents; got: {out!r}. "
            "Operators rely on this to debug tier-miss without re-running."
        )
        assert "preferred=" in out and "backup=" in out, (
            f"Failure log must label the lists with 'preferred=' and 'backup=' "
            f"keys for log-scanner regex stability; got: {out!r}."
        )


class TestRobustDestroyFalsyDseqDisambiguation:
    """Angle #7: pin the `if not dseq` early-return contract for falsy values.

    `if not dseq: return True` — this triggers for dseq=None, dseq="", dseq=0,
    dseq=False, etc. ALL falsy values are treated as "nothing to destroy."
    The string "0" is truthy in Python and DOES go through the destroy path
    (a zero-DSEQ doesn't exist in real Akash, but the contract is "any
    non-empty string is a real DSEQ").

    Pin both sides:
      - falsy values (None, 0, False) → noop, no subprocess call
      - the string "0" → real destroy path
    so a future refactor that tightens to `if dseq is None` (only None is
    noop) doesn't accidentally try to destroy "" or 0.
    """

    def test_dseq_none_is_noop(self):
        with patch("just_akash._e2e.subprocess.run") as mock_run:
            assert robust_destroy(None) is True  # type: ignore[arg-type]
            mock_run.assert_not_called()

    def test_dseq_zero_int_is_noop(self):
        """dseq=0 (int, falsy) must NOT trigger subprocess. Today's contract
        is `if not dseq`, which catches 0. If a future change tightens to
        `if dseq is None or dseq == ""`, 0 would suddenly invoke `just
        destroy 0` — confusing the CLI for no real benefit.
        """
        with patch("just_akash._e2e.subprocess.run") as mock_run:
            assert robust_destroy(0) is True  # type: ignore[arg-type]
            mock_run.assert_not_called()

    def test_dseq_false_is_noop(self):
        with patch("just_akash._e2e.subprocess.run") as mock_run:
            assert robust_destroy(False) is True  # type: ignore[arg-type]
            mock_run.assert_not_called()

    def test_dseq_string_zero_is_real_destroy(self):
        """The string "0" is truthy in Python — it's a valid (if unusual)
        DSEQ string. It MUST go through the full destroy path. Pin this so a
        future "treat all-zero DSEQs as noop" optimization isn't snuck in.
        """
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),
                _completed(0, stdout='{"state": "closed"}'),
            ]
            assert robust_destroy("0") is True
            assert mock_run.call_count == 2, (
                f"dseq='0' (truthy string) must trigger destroy + audit = 2 "
                f"calls; got {mock_run.call_count}. If 0, the falsy check was "
                "widened to also reject the string '0' — that's a contract "
                "change worth reviewing."
            )
            # Verify the destroy command was actually issued for "0".
            destroy_cmd = mock_run.call_args_list[0].args[0]
            assert "just destroy 0" in destroy_cmd, (
                f"dseq='0' must produce `just destroy 0`; got {destroy_cmd!r}."
            )


class TestAuditReadsTheAuthoritativeRecordNotTheList:
    """`just list` lies. Measured 2026-07-17: it reported dseq 1784291290915 as
    active while that deployment's OWN record read state=closed, escrow=closed,
    funds=0. The audit used to trust the list, which caused a spurious
    "STILL listed — manual cleanup required" FAIL on a perfectly clean destroy.

    The staleness cuts both ways, and the other way is the dangerous one: the list
    can report a deployment GONE while its escrow is still open — a silent leak,
    which is the exact thing the audit exists to catch. Only the per-deployment
    record decides whether funds are held.
    """

    def _audit_cmd(self, mock_run):
        return mock_run.call_args_list[1].args[0]

    def test_audit_does_not_shell_out_to_just_list(self):
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),
                _completed(0, stdout='{"state": "closed"}'),
            ]
            robust_destroy("12345")
            assert self._audit_cmd(mock_run) != "just list"
            assert "status" in self._audit_cmd(mock_run)

    def test_a_stale_list_can_no_longer_cause_a_spurious_leak_report(self):
        """The exact 2026-07-17 flake: destroy succeeds, the list still shows the
        dseq, but the authoritative record says closed. Must PASS, not cry leak."""
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="Deployment 1784291290915 closed"),
                _completed(0, stdout='{"dseq": "1784291290915", "state": "closed"}'),
            ]
            assert robust_destroy("1784291290915") is True

    def test_failed_state_is_settled_too(self):
        """`failed` is terminal on-chain: settled, holding no escrow."""
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),
                _completed(0, stdout='{"state": "failed"}'),
            ]
            assert robust_destroy("12345") is True

    def test_unreadable_record_fails_closed_as_a_possible_leak(self, capsys):
        """If we cannot positively confirm settlement, we must NOT report success:
        silence is exactly what a leak looks like."""
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),
                _completed(1, stderr="API 503"),
                _completed(1, stderr="API 503"),
                _completed(1, stderr="API 503"),
            ]
            assert robust_destroy("12345") is False
            assert "could not confirm" in capsys.readouterr().out.lower()

    def test_a_transient_blip_does_not_cry_leak(self):
        """Fails-closed only AFTER retries — otherwise one API blip would report a
        leak that isn't one, trading a silent-leak bug for a flaky-red one."""
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),
                _completed(1, stderr="transient 503"),  # blip
                _completed(0, stdout='{"state": "closed"}'),  # recovers
            ]
            assert robust_destroy("12345") is True

    def test_still_active_is_reported_as_a_leak(self):
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),
                _completed(0, stdout='{"state": "active"}'),
            ]
            assert robust_destroy("12345") is False


class TestAuditNeverRaisesFromCleanup:
    """robust_destroy runs from a finally block AND from the signal handler, so its
    contract is that it never raises. Caught in review (Copilot, PR #63): the
    authoritative audit was added OUTSIDE any try/except, so an exception there — a
    Ctrl-C landing in the pre-audit sleep, say — would escape and abort cleanup,
    which is the exact failure the audit exists to prevent.
    """

    def test_a_raising_sleep_does_not_escape(self):
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep", side_effect=RuntimeError("clock blew up")),
        ):
            mock_run.side_effect = [_completed(0, stdout="closed")]
            # Must NOT raise, and must fail closed rather than claim success.
            assert robust_destroy("12345") is False

    def test_keyboardinterrupt_deliberately_still_propagates(self):
        """Scope of "never raises": Exception, NOT BaseException — matching the
        destroy loop above it, which has always caught only Exception. A user
        hammering Ctrl-C must be able to abort; swallowing KeyboardInterrupt here
        would trap them in a cleanup they are explicitly trying to escape. Pinned so
        nobody "hardens" this into a bare except and silently removes that escape.
        """
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep", side_effect=KeyboardInterrupt),
        ):
            mock_run.side_effect = [_completed(0, stdout="closed")]
            with pytest.raises(KeyboardInterrupt):
                robust_destroy("12345")

    def test_a_raising_probe_does_not_escape(self, capsys):
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
            patch("just_akash._e2e._confirm_settled", side_effect=RuntimeError("boom")),
        ):
            mock_run.side_effect = [_completed(0, stdout="closed")]
            assert robust_destroy("12345") is False
            assert "possible leak" in capsys.readouterr().out.lower()

    def test_the_manual_verify_hint_is_shell_quoted(self, capsys):
        """The hint is copy-pasted by an operator into a shell, so it must not carry
        an unquoted dseq any more than the probe command does."""
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),
                _completed(1, stderr="503"),
                _completed(1, stderr="503"),
                _completed(1, stderr="503"),
            ]
            robust_destroy("123; rm -rf /tmp/x")
            assert "'123; rm -rf /tmp/x'" in capsys.readouterr().out


class TestUnknownStateIsNotAClaimOfLife:
    """Caught in review (Copilot, PR #63): _confirm_settled returned False for ANY
    non-settled state, so an unrecognised value made the audit print "STILL ACTIVE
    after destroy" — a claim we cannot support. The fail-closed OUTCOME was right;
    the message was a lie. Open states are now an allowlist, and anything unknown
    reports "could not confirm" instead.
    """

    def _run_audit(self, state_json: str, capsys):
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [_completed(0, stdout="closed")] + [
                _completed(0, stdout=state_json) for _ in range(3)
            ]
            result = robust_destroy("12345")
        return result, capsys.readouterr().out

    def test_unknown_state_says_could_not_confirm_not_still_active(self, capsys):
        result, out = self._run_audit('{"state": "some_new_state"}', capsys)
        assert result is False, "must still fail closed"
        assert "could not confirm" in out.lower()
        assert "STILL ACTIVE" not in out, "we cannot claim it is active — we don't know"

    def test_active_still_positively_reports_still_active(self, capsys):
        result, out = self._run_audit('{"state": "active"}', capsys)
        assert result is False
        assert "STILL ACTIVE" in out

    def test_insufficient_funds_is_settled(self, capsys):
        """The escrow is what ran out — there is nothing left to leak. Terminal in
        smoke_providers._DEAD_STATES too; kept in sync."""
        result, out = self._run_audit('{"state": "insufficient_funds"}', capsys)
        assert result is True
        assert "settled" in out.lower()

    def test_missing_state_field_is_unknown_not_settled(self, capsys):
        result, out = self._run_audit('{"dseq": "12345"}', capsys)
        assert result is False
        assert "could not confirm" in out.lower()


class TestAuditPollsBecauseCloseIsNotInstant:
    """The per-deployment record is authoritative but NOT instantaneous: a close
    takes ~6-12s to reflect, so a just-destroyed deployment keeps reading `active`.

    Reading once and calling that "STILL ACTIVE" fails a perfectly clean destroy.
    That is not hypothetical — it broke the lease-shell E2E on this branch:

        [7/7] Cleanup: destroy DSEQ=1784294163119
          PASS Deployment 1784294163119 closed (attempt 1)
          FAIL Audit: deployment 1784294163119 STILL ACTIVE after destroy

    So `active` inside the window means "not settled YET"; only `active` that
    PERSISTS through the whole window is a real leak.
    """

    def test_active_then_closed_is_a_clean_destroy_not_a_leak(self):
        """The exact E2E regression: destroy works, the close has not reflected yet."""
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),  # destroy
                _completed(0, stdout='{"state": "active"}'),  # close not reflected yet
                _completed(0, stdout='{"state": "active"}'),  # ...still lagging
                _completed(0, stdout='{"state": "closed"}'),  # settles
            ]
            assert robust_destroy("1784294163119") is True

    def test_persistently_active_is_still_reported_as_a_leak(self, capsys):
        """The poll must not become a way to wish a real leak away."""
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [_completed(0, stdout="closed")] + [
                _completed(0, stdout='{"state": "active"}') for _ in range(8)
            ]
            assert robust_destroy("12345") is False
            assert "STILL ACTIVE" in capsys.readouterr().out

    def test_a_settled_read_returns_immediately_without_burning_the_window(self):
        """The common case (already settled) must not pay the full poll."""
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),
                _completed(0, stdout='{"state": "closed"}'),
            ]
            assert robust_destroy("12345") is True
            assert mock_run.call_count == 2, "settled on the first probe = 1 destroy + 1 probe"

    def test_blips_then_settled_still_passes(self):
        """A transient API failure mid-poll must not end the window early."""
        with (
            patch("just_akash._e2e.subprocess.run") as mock_run,
            patch("just_akash._e2e.time.sleep"),
        ):
            mock_run.side_effect = [
                _completed(0, stdout="closed"),
                _completed(1, stderr="503"),
                _completed(0, stdout="not json at all"),
                _completed(0, stdout='{"state": "closed"}'),
            ]
            assert robust_destroy("12345") is True
