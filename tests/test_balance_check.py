"""CLI dispatch tests for `balance --check --min-usd N` (the low-credit alarm).

Exit-code contract: 0 when deploy credit >= --min-usd, 1 when below (so a
scheduled job can flag a low wallet BEFORE deploys start 402ing), 2 on misuse.
chain.deploy_credit is mocked; chain.usd_estimate runs for real (uact is
USD-pegged, 1e6 uact = $1), so the USD math is exercised end-to-end.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest


def _run_balance_check(monkeypatch, argv, credit):
    """Drive `cli.main()` for a balance command with a mocked account + credit.

    Returns the process exit code (``SystemExit.code``). stdout/stderr are not
    returned — callers that need the verdict read it via ``capsys``; stdout is
    non-tty under pytest, so the verdict is emitted as JSON there to parse.
    """
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.setattr(sys, "argv", argv)
    with (
        patch("just_akash.api.AkashConsoleAPI") as MockAPI,
        patch("just_akash.chain.deploy_credit", return_value=credit),
    ):
        MockAPI.return_value.account_address.return_value = "akash1me"
        from just_akash.cli import main

        with pytest.raises(SystemExit) as exc:
            main()
    return exc.value.code


class TestBalanceCheck:
    def test_exits_zero_when_credit_at_or_above_threshold(self, monkeypatch, capsys):
        # 170 ACT (170_000_000 uact) ~= $170 >= $50.
        code = _run_balance_check(
            monkeypatch,
            ["just-akash", "balance", "--check", "--min-usd", "50"],
            {"uact": 170_000_000},
        )
        assert code == 0
        verdict = json.loads(capsys.readouterr().out)
        assert verdict["status"] == "OK"
        assert verdict["deploy_credit_usd"] == 170.0
        assert verdict["min_usd"] == 50.0
        assert verdict["account"] == "akash1me"

    def test_exits_nonzero_when_credit_below_threshold(self, monkeypatch, capsys):
        # 10 ACT < $50 -> LOW, exit 1.
        code = _run_balance_check(
            monkeypatch,
            ["just-akash", "balance", "--check", "--min-usd", "50"],
            {"uact": 10_000_000},
        )
        assert code == 1
        verdict = json.loads(capsys.readouterr().out)
        assert verdict["status"] == "LOW"
        assert verdict["deploy_credit_usd"] == 10.0

    def test_empty_credit_grant_is_low(self, monkeypatch, capsys):
        # No DepositAuthorization grant -> $0 -> LOW below any positive threshold.
        code = _run_balance_check(
            monkeypatch,
            ["just-akash", "balance", "--check", "--min-usd", "5"],
            {},
        )
        assert code == 1
        assert json.loads(capsys.readouterr().out)["deploy_credit_usd"] == 0.0

    def test_machine_readable_text_verdict_when_tty(self, monkeypatch, capsys):
        """With a real TTY (forced here) the verdict is a stable grep-able line."""
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        code = _run_balance_check(
            monkeypatch,
            ["just-akash", "balance", "--check", "--min-usd", "50"],
            {"uact": 10_000_000},
        )
        assert code == 1
        out = capsys.readouterr().out
        assert "CREDIT-CHECK status=LOW" in out
        assert "deploy_credit_usd=10.00" in out
        assert "min_usd=50.00" in out

    def test_check_without_min_usd_exits_two(self, monkeypatch, capsys):
        code = _run_balance_check(
            monkeypatch,
            ["just-akash", "balance", "--check"],
            {"uact": 10_000_000},
        )
        assert code == 2
        assert "requires --min-usd" in capsys.readouterr().err
