"""Tests for the structured-diagnostic-event contract (just_akash._diagnostics) and
its instrumentation in deploy.py (the WALLET_* precheck + the bid/lease failure codes)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from just_akash._diagnostics import Code, emit

# ── emit() envelope + gating ───────────────────────────────────────────────────


class TestEmit:
    def test_emits_json_line_to_stderr(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        emit(
            Code.WALLET_INSUFFICIENT_CREDIT,
            "error",
            "no credit",
            account="akash1me",
            deploy_credit_uact=0,
        )
        err = capsys.readouterr().err
        event = json.loads(err.strip().splitlines()[-1])
        assert event["type"] == "akash-diag"
        assert event["level"] == "error"
        assert event["code"] == "WALLET_INSUFFICIENT_CREDIT"
        assert event["message"] == "no credit"
        assert event["context"] == {"account": "akash1me", "deploy_credit_uact": 0}

    def test_dseq_included_when_passed(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        emit(Code.LEASE_CREATE_FAILED, "error", "x", dseq="12345")
        assert json.loads(capsys.readouterr().err.strip())["dseq"] == "12345"

    def test_none_context_dropped_but_zero_and_false_kept(self, monkeypatch, capsys):
        # None is dropped (compact); 0 and False are real evidence and must survive.
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        emit(
            Code.PROVIDER_NO_BID,
            "warning",
            "x",
            provider="akash1p",
            uptime1d=None,
            cpu_available=0,
            isOnline=False,
        )
        ctx = json.loads(capsys.readouterr().err.strip())["context"]
        assert "uptime1d" not in ctx
        assert ctx["cpu_available"] == 0
        assert ctx["isOnline"] is False

    def test_disabled_when_off(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "off")
        emit(Code.NO_BIDS_RECEIVED, "error", "x")
        assert capsys.readouterr().err == ""

    def test_default_enabled_when_stderr_not_a_tty(self, monkeypatch, capsys):
        # pytest captures stderr (non-tty) → diagnostics are on by default.
        monkeypatch.delenv("AKASH_DIAGNOSTICS", raising=False)
        emit(Code.NO_BIDS_RECEIVED, "error", "x")
        assert capsys.readouterr().err != ""

    def test_invalid_level_falls_back_to_warning(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        emit("SOME_CODE", "bogus-level", "x")
        assert json.loads(capsys.readouterr().err.strip())["level"] == "warning"

    def test_emit_never_raises_on_write_failure(self, monkeypatch):
        # A diagnostic failure must never break the operation it reports on.
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")

        class _BrokenStderr:
            def isatty(self):
                return False

            def write(self, _):
                raise OSError("stderr closed")

            def flush(self):
                raise OSError("stderr closed")

        with patch("just_akash._diagnostics.sys.stderr", _BrokenStderr()):
            emit(Code.NO_BIDS_RECEIVED, "error", "x")  # must not raise


class TestCode:
    def test_codes_are_stable_upper_snake(self):
        # The contract: each code is a stable identifier whose value equals its name.
        for name in dir(Code):
            if name.startswith("_"):
                continue
            val = getattr(Code, name)
            if isinstance(val, str) and val.isupper():
                assert val == name


# ── _check_wallet_credit precheck (deploy.py) ─────────────────────────────────


class TestWalletCreditCheck:
    def test_insufficient_credit_emits_error(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        client = MagicMock()
        client.account_address.return_value = "akash1me"
        with patch("just_akash.chain.deploy_credit", return_value={"uact": 0}):
            from just_akash.deploy import _check_wallet_credit

            _check_wallet_credit(client, deposit=5.0)  # must not raise
        event = json.loads(capsys.readouterr().err.strip())
        assert event["code"] == "WALLET_INSUFFICIENT_CREDIT"
        assert event["level"] == "error"
        assert event["context"]["deploy_credit_uact"] == 0

    def test_low_credit_emits_warning(self, monkeypatch, capsys):
        # 0.1 ACT << the 10-ACT threshold (deposit 5 × 2) → low, not insufficient.
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        client = MagicMock()
        client.account_address.return_value = "akash1me"
        with patch("just_akash.chain.deploy_credit", return_value={"uact": 100_000}):
            from just_akash.deploy import _check_wallet_credit

            _check_wallet_credit(client, deposit=5.0)
        event = json.loads(capsys.readouterr().err.strip())
        assert event["code"] == "WALLET_LOW_CREDIT"
        assert event["level"] == "warning"

    def test_credit_query_failure_emits_warning_and_does_not_raise(self, monkeypatch, capsys):
        # account_address failing must not abort the deploy — warn and return.
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        client = MagicMock()
        client.account_address.side_effect = RuntimeError("jwt mint failed")
        from just_akash.deploy import _check_wallet_credit

        _check_wallet_credit(client, deposit=5.0)
        event = json.loads(capsys.readouterr().err.strip())
        assert event["code"] == "WALLET_CREDIT_QUERY_FAILED"

    def test_lcd_failure_emits_query_failed(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        client = MagicMock()
        client.account_address.return_value = "akash1me"
        with patch("just_akash.chain.deploy_credit", side_effect=RuntimeError("LCD timeout")):
            from just_akash.deploy import _check_wallet_credit

            _check_wallet_credit(client, deposit=5.0)
        assert json.loads(capsys.readouterr().err.strip())["code"] == "WALLET_CREDIT_QUERY_FAILED"

    def test_healthy_credit_emits_nothing(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        client = MagicMock()
        client.account_address.return_value = "akash1me"
        with patch("just_akash.chain.deploy_credit", return_value={"uact": 50_000_000}):  # 50 ACT
            from just_akash.deploy import _check_wallet_credit

            _check_wallet_credit(client, deposit=5.0)
        assert capsys.readouterr().err == ""


# ── deploy.py instrumentation (a failure path emits the matching code) ─────────


class TestDeployInstrumentation:
    def test_no_bids_emits_structured_code(self, monkeypatch, capsys, tmp_path):
        """The no-bids failure emits a NO_BIDS_RECEIVED event alongside the raise."""
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        from tests.test_deploy import SDL_YAML, _time_mock

        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        with (
            patch("just_akash.deploy.time") as mock_time,
            patch("just_akash.deploy.AkashConsoleAPI") as MockAPI,
            patch("just_akash.deploy._check_wallet_credit"),  # precheck needs creds/chain — skip
        ):
            client = MockAPI.return_value
            client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
            client.get_bids.return_value = []
            t = _time_mock()
            mock_time.time.side_effect = t
            mock_time.sleep.return_value = None

            from just_akash.deploy import deploy

            with pytest.raises(RuntimeError, match="No bids received"):
                deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)

        # The structured event is on stderr alongside the prose.
        diag = [
            json.loads(ln)
            for ln in capsys.readouterr().err.splitlines()
            if ln.strip().startswith("{") and '"akash-diag"' in ln
        ]
        codes = [e["code"] for e in diag]
        assert "NO_BIDS_RECEIVED" in codes
        no_bids = next(e for e in diag if e["code"] == "NO_BIDS_RECEIVED")
        assert no_bids["level"] == "error"
        assert no_bids["dseq"] == "12345"
