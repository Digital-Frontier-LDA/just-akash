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
        # The gating value is FREE credit (granted minus escrow held by active
        # deployments), not the grant — the grant reads "healthy" while Console is
        # already returning 402. The breakdown is printed alongside so an operator
        # can see WHY free is low.
        assert "free_usd=10.00" in out
        assert "granted=10.00" in out
        assert "locked_in_escrow=0.00" in out
        assert "min_usd=50.00" in out

    def test_check_without_min_usd_exits_two(self, monkeypatch, capsys):
        code = _run_balance_check(
            monkeypatch,
            ["just-akash", "balance", "--check"],
            {"uact": 10_000_000},
        )
        assert code == 2
        assert "requires --min-usd" in capsys.readouterr().err


class TestCheckGatesOnFreeNotGrant:
    """The alarm must gate on FREE credit (granted - escrow held by active
    deployments), not the grant.

    Measured regression: 28 active deployments held 165 of a 170.62 ACT grant, so
    Console returned HTTP 402 "Insufficient balance" on a 5 ACT deploy while the
    grant still read 170.62 — the old check reported OK at the exact moment deploys
    were failing.
    """

    def _run(self, monkeypatch, argv, credit, deployments):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", argv)
        with (
            patch("just_akash.api.AkashConsoleAPI") as MockAPI,
            patch("just_akash.chain.deploy_credit", return_value=credit),
        ):
            client = MockAPI.return_value
            client.account_address.return_value = "akash1me"
            client.list_deployments.return_value = deployments
            client.get_deployment.side_effect = lambda dseq: next(
                (d for d in deployments if d["dseq"] == str(dseq)), {}
            )
            from just_akash.cli import main

            with pytest.raises(SystemExit) as exc:
                main()
        return exc.value.code

    @staticmethod
    def _dep(dseq, uact):
        return {
            "deployment": {"state": "active", "dseq": str(dseq)},
            "dseq": str(dseq),
            "escrow_account": {"state": {"funds": [{"amount": str(uact), "denom": "uact"}]}},
        }

    def test_low_when_escrow_locks_the_grant(self, monkeypatch, capsys):
        """Grant 170.62 ACT (over the 100 threshold) but 165 locked -> free 5.62 -> LOW.
        The OLD behaviour reported OK here; that was the bug."""
        deps = [self._dep(i, 5_000_000) for i in range(33)]  # 165 ACT locked
        code = self._run(
            monkeypatch,
            ["just-akash", "balance", "--check", "--min-usd", "100"],
            {"uact": 170_623_558},
            deps,
        )
        out = capsys.readouterr().out
        assert code == 1, "must FAIL: only 5.62 ACT is actually spendable"
        assert '"status": "LOW"' in out
        assert '"free_usd": 5.62' in out
        assert '"granted_usd": 170.62' in out  # the misleading number, kept for context
        assert '"locked_in_escrow_usd": 165.0' in out

    def test_ok_when_escrow_is_released(self, monkeypatch, capsys):
        """Same grant, escrow released -> free is high -> OK. Confirms the check is
        not simply always-LOW."""
        code = self._run(
            monkeypatch,
            ["just-akash", "balance", "--check", "--min-usd", "100"],
            {"uact": 170_623_558},
            [self._dep(1, 5_000_000)],
        )
        out = capsys.readouterr().out
        assert code == 0
        assert '"status": "OK"' in out
        assert '"free_usd": 165.62' in out
