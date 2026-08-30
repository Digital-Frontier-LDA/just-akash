"""CLI dispatch tests for `balance --check --min-usd N` (the low-credit alarm).

Exit-code contract: 0 when deploy credit >= --min-usd, 1 when below (so a
scheduled job can flag a low wallet BEFORE deploys start 402ing), 2 on misuse.
chain.granted_uact is mocked; chain.usd_estimate runs for real (uact is
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
        patch("just_akash.chain.granted_uact", return_value=credit.get("uact")),
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

    def test_empty_credit_grant_is_unknown(self, monkeypatch, capsys):
        # No readable DepositAuthorization grant is UNKNOWN, never a measured zero.
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        code = _run_balance_check(
            monkeypatch,
            ["just-akash", "balance", "--check", "--min-usd", "5"],
            {},
        )
        assert code == 1
        out = capsys.readouterr().out
        assert out.strip() == (
            "CREDIT-CHECK UNKNOWN reason=canonical spend_limits quorum unavailable "
            "min_usd=5.00 account=akash1me"
        )

    def test_normal_balance_unknown_is_nonfatal_json(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["just-akash", "balance"])
        with (
            patch("just_akash.api.AkashConsoleAPI") as MockAPI,
            patch("just_akash.chain.granted_uact", return_value=None),
        ):
            MockAPI.return_value.account_address.return_value = "akash1me"
            from just_akash.cli import main

            main()
        assert json.loads(capsys.readouterr().out)["status"] == "UNKNOWN"

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
    """The alarm must gate on FREE credit, not the gross grant.

    ⭐ Fix for #169: `spend_limits` (from `DepositAuthorization`) is ALREADY
    NET of locked escrow — the Cosmos authz module decrements it as the grantee
    uses escrow, so the value the chain returns is the *remaining* allowance,
    NOT the gross grant. The check therefore gates on the spend_limit value
    directly: it IS the deployable credit. The OLD check subtracted
    `locked_uact` a second time, reading `free_uact = 0` for funded wallets.

    Historical regression (pre-#169): a funded wallet could read OK while
    Console was returning 402. The root cause was a combination of (a) the
    double-subtract bug — `free = granted - locked` over-subtracts when
    `locked > granted` (which is routine — see issue body, disproof 1) — and
    (b) `locked_uact` is a LOWER bound on actual escrow (some deployments are
    unreadable; see TestIncompleteEscrowTally). With the fix, the check gates
    on `spend_limits` directly, which is what Console actually honors.
    """

    def _run(self, monkeypatch, argv, credit, deployments):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", argv)
        with (
            patch("just_akash.api.AkashConsoleAPI") as MockAPI,
            patch("just_akash.chain.granted_uact", return_value=credit.get("uact")),
        ):
            client = MockAPI.return_value
            client.account_address.return_value = "akash1me"
            client.list_deployments.return_value = deployments
            client.get_deployment.side_effect = lambda dseq, owner=None: next(
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
        """#169: spend_limits is the REMAINING allowance. 170.62 ACT in
        spend_limits means 170.62 ACT is actually deployable, regardless of how
        much sits in escrow (which is past-deposit context, not future capacity).

        The OLD test asserted LOW here because the OLD formula computed
        `free = 170.62 - 165 = 5.62` — the double-subtract. With the fix,
        `free = spend_limits = 170.62`, which is above the 100 ACT threshold,
        so the check correctly reports OK.

        This test now pins the FIXED behaviour: high escrow does not lower the
        check, because spend_limits is the deployable credit. `low` only fires
        when spend_limits itself is below the threshold (see the new test below).
        """
        deps = [self._dep(i, 5_000_000) for i in range(33)]  # 165 ACT in escrow
        code = self._run(
            monkeypatch,
            ["just-akash", "balance", "--check", "--min-usd", "100"],
            {"uact": 170_623_558},  # spend_limits = 170.62 ACT (NET)
            deps,
        )
        out = capsys.readouterr().out
        # The OLD expression would have read free_usd=5.62 and exited 1. The
        # fix gates on spend_limits directly, which is 170.62 ACT — well above
        # the 100 ACT threshold. OK is the correct verdict.
        assert code == 0, (
            "spend_limits=170.62 ACT is the remaining deployable credit, "
            "above the 100 ACT threshold; the check should report OK. The "
            "OLD expression (max(g-l, 0)) would have read free_usd=5.62 and "
            "exited 1 here — that is the double-subtract bug (#169)."
        )
        assert '"status": "OK"' in out
        assert '"free_usd": 170.62' in out  # spend_limits IS the free credit
        assert '"granted_usd": 170.62' in out
        assert '"locked_in_escrow_usd": 165.0' in out  # display only, not a subtrahend

    def test_ok_when_escrow_is_released(self, monkeypatch, capsys):
        """Same grant (170.62 ACT spend_limits), 1 small escrow lock -> OK.

        Under NET semantics, the locked amount is irrelevant to the gate:
        spend_limits IS the remaining credit, and 170.62 ACT >> 100 ACT.
        """
        code = self._run(
            monkeypatch,
            ["just-akash", "balance", "--check", "--min-usd", "100"],
            {"uact": 170_623_558},  # spend_limits = 170.62 ACT (NET)
            [self._dep(1, 5_000_000)],
        )
        out = capsys.readouterr().out
        assert code == 0
        assert '"status": "OK"' in out
        assert '"free_usd": 170.62' in out  # NET, not 165.62 (the OLD's wrong answer)
        assert '"locked_in_escrow_usd": 5.0' in out

    def test_low_when_spend_limits_below_threshold(self, monkeypatch, capsys):
        """#169 negative control: spend_limits below the threshold IS a LOW.

        With NET semantics, `low` fires when the spend_limit value itself is
        below `args.min_usd`. The OLD formula would have computed the same
        outcome here (10 ACT - 0 locked = 10 ACT < 100 ACT threshold), so this
        test passes against both the OLD and the NEW — it pins the LOW path
        while the other two tests pin the OK path under NET semantics.
        """
        code = self._run(
            monkeypatch,
            ["just-akash", "balance", "--check", "--min-usd", "100"],
            {"uact": 10_000_000},  # spend_limits = 10 ACT (below 100 ACT threshold)
            [],
        )
        out = capsys.readouterr().out
        assert code == 1, "spend_limits=10 ACT must FAIL against min_usd=100"
        assert '"status": "LOW"' in out
        assert '"free_usd": 10.0' in out
        assert '"granted_usd": 10.0' in out
        assert '"locked_in_escrow_usd": 0.0' in out


class TestIncompleteEscrowTally:
    """Incomplete escrow tallies are diagnostic, not gating — under NET semantics.

    The OLD test class (`test_unnameable_deployment_yields_UNKNOWN_not_OK` etc.)
    pinned UNKNOWN + exit 1 on omitted deployments. That made sense under the OLD
    formula `free = granted - locked`: an incomplete escrow tally made `locked`
    a LOWER bound, which made `free` an UPPER bound, so reporting OK could send
    a deployer into a 402.

    Under NET semantics (spend_limits IS the deployable credit) `free_usd`
    does not depend on `locked_uact` at all. The omission is still useful as
    a data-quality signal — the JSON payload reports
    `escrow_unreadable_deployments` and `escrow_unnameable_deployments` — but
    the gate no longer flips OK to UNKNOWN on its account. CodeRabbit on #190
    flagged this as a follow-on to the #169 fix.
    """

    @staticmethod
    def _with_escrow(monkeypatch, argv, credit, escrow):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", argv)
        with (
            patch("just_akash.api.AkashConsoleAPI") as MockAPI,
            patch("just_akash.chain.granted_uact", return_value=credit.get("uact")),
            patch("just_akash.api.escrow_locked", return_value=escrow),
        ):
            MockAPI.return_value.account_address.return_value = "akash1me"
            from just_akash.cli import main

            with pytest.raises(SystemExit) as exc:
                main()
        return exc.value.code

    def test_unnameable_deployment_is_OK_with_diagnostic(self, monkeypatch, capsys):
        """#169 follow-on: NET semantics — omission is diagnostic, not gating.

        spend_limits=170 ACT (well above the 50 ACT threshold). One deployment
        has no extractable dseq (`skipped_no_dseq: 1`). The OLD check would
        flip status to UNKNOWN and exit 1; the new check stays OK and exits
        0 because `free_usd` (== spend_limits) is exact.
        """
        code = self._with_escrow(
            monkeypatch,
            ["just-akash", "balance", "--check", "--min-usd", "50"],
            {"uact": 170_000_000},
            {
                "locked_uact": 0,
                "deployments": 1,
                "unreadable": 0,
                "skipped_no_dseq": 1,
                "by_deployment": [],
            },
        )
        verdict = json.loads(capsys.readouterr().out)
        assert verdict["status"] == "OK", (
            "omission alone is no longer a gate under NET semantics — the OLD "
            "behaviour (UNKNOWN + exit 1) was correct only when `free = granted "
            "- locked` made free an upper bound"
        )
        assert verdict["escrow_unnameable_deployments"] == 1
        # Diagnostic preserved, status preserved — operators see the data quality issue
        # without paying for it as an alarm.
        assert verdict["free_usd"] == 170.0
        assert code == 0

    def test_unreadable_deployment_is_OK_with_diagnostic(self, monkeypatch, capsys):
        """Same NET-semantics follow-on: an unreadable deployment is diagnostic.

        The OLD test asserted UNKNOWN + exit 1 here. With NET semantics,
        free_usd = spend_limits = 170 ACT regardless of escrow data quality,
        so status is OK and exit 0. `escrow_unreadable_deployments` is still
        emitted for the operator.
        """
        code = self._with_escrow(
            monkeypatch,
            ["just-akash", "balance", "--check", "--min-usd", "50"],
            {"uact": 170_000_000},
            {"locked_uact": 0, "deployments": 1, "unreadable": 1, "by_deployment": []},
        )
        verdict = json.loads(capsys.readouterr().out)
        assert verdict["status"] == "OK"
        assert verdict["escrow_unreadable_deployments"] == 1
        assert verdict["free_usd"] == 170.0
        assert code == 0

    def test_complete_tally_is_plain_OK(self, monkeypatch, capsys):
        """Sanity: a complete tally at healthy credit reads OK + exit 0.

        Regression pin against the OLD `low wins over omitted` tests below.
        """
        code = self._with_escrow(
            monkeypatch,
            ["just-akash", "balance", "--check", "--min-usd", "50"],
            {"uact": 170_000_000},
            {
                "locked_uact": 5_000_000,
                "deployments": 1,
                "unreadable": 0,
                "skipped_no_dseq": 0,
                "by_deployment": [],
            },
        )
        verdict = json.loads(capsys.readouterr().out)
        assert verdict["status"] == "OK"
        assert code == 0

    def test_low_still_gates_regardless_of_omission(self, monkeypatch, capsys):
        """LOW wins: a short spend_limits reads LOW even when escrow is also incomplete.

        Under NET semantics the gate is `spend_limits < min_usd`. Omission is
        irrelevant here — short is short.
        """
        code = self._with_escrow(
            monkeypatch,
            ["just-akash", "balance", "--check", "--min-usd", "50"],
            {"uact": 10_000_000},
            {"locked_uact": 0, "deployments": 1, "unreadable": 1, "by_deployment": []},
        )
        assert json.loads(capsys.readouterr().out)["status"] == "LOW"
        assert code != 0

    def test_a_complete_tally_above_threshold_is_OK_with_spend_limits_as_free(
        self, monkeypatch, capsys
    ):
        """#169: spend_limits is the remaining credit; locked is display-only.

        Setup: spend_limits=170 ACT (NET), 20 ACT locked in escrow. With NET
        semantics, free = spend_limits = 170 ACT (NOT 170 - 20 = 150 ACT). The
        OLD formula would have computed free=150; the fix removes the
        subtraction.
        """
        code = self._with_escrow(
            monkeypatch,
            ["just-akash", "balance", "--check", "--min-usd", "50"],
            {"uact": 170_000_000},  # spend_limits = 170 ACT (NET)
            {"locked_uact": 20_000_000, "deployments": 2, "unreadable": 0, "by_deployment": []},
        )
        verdict = json.loads(capsys.readouterr().out)
        assert verdict["status"] == "OK"
        assert verdict["escrow_unreadable_deployments"] == 0
        assert verdict["free_usd"] == 170.0, (
            "spend_limits=170 ACT IS the remaining credit; free must equal "
            "170, NOT 150 (which is what the OLD `max(g-l, 0)` would compute)."
        )
        assert code == 0
