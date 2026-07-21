"""Tests for escrow_locked — free vs granted deploy credit.

Regression context: `balance` reported the GRANT (what Console authorized) with no
account of escrow held by active deployments. Measured live: 28 active deployments
held 165 of a 170.62 ACT grant, so Console returned HTTP 402 "Insufficient balance"
on a 5 ACT deploy while `balance` still read a healthy 170.62. The grant alone says
"healthy" at the exact moment deploys start failing; free = granted - locked is the
number that predicts whether the next deploy succeeds.
"""

from unittest.mock import MagicMock

from just_akash.api import escrow_locked


def _deployment(dseq, escrow_uact=None, denom="uact"):
    """A list_deployments row; escrow_uact=None means no escrow funds."""
    funds = [] if escrow_uact is None else [{"amount": str(escrow_uact), "denom": denom}]
    return {
        "deployment": {"state": "active", "dseq": str(dseq)},
        "dseq": str(dseq),
        "escrow_account": {"state": {"funds": funds}},
    }


def _client(deployments):
    c = MagicMock()
    c.list_deployments.return_value = deployments
    c.get_deployment.side_effect = lambda dseq: next(
        (d for d in deployments if d["dseq"] == str(dseq)), {}
    )
    return c


class TestEscrowLocked:
    def test_sums_escrow_across_deployments(self):
        c = _client(
            [_deployment(1, 5_000_000), _deployment(2, 5_000_000), _deployment(3, 30_000_000)]
        )
        r = escrow_locked(c)
        assert r["locked_uact"] == 40_000_000
        assert r["deployments"] == 3
        assert r["unreadable"] == 0

    def test_the_real_402_scenario(self):
        """The measured case: 165 ACT locked against a 170.62 ACT grant leaves 5.62
        free — below a 5 ACT deposit + fees, which is why Console returned 402 while
        the grant still read 170.62."""
        deployments = [_deployment(i, 5_000_000) for i in range(24)]  # 120 ACT runners
        deployments.append(_deployment(99, 30_000_000))  # 30 ACT fanout
        deployments += [_deployment(200 + i, 5_000_000) for i in range(3)]  # 15 ACT no-lease
        r = escrow_locked(_client(deployments))
        granted = 170_623_558
        free = granted - r["locked_uact"]
        assert r["locked_uact"] == 165_000_000
        assert free == 5_623_558  # 5.62 ACT — the grant said 170.62
        assert free < 5_000_000 + 1_000_000  # under a 5 ACT deposit + headroom

    def test_no_deployments_means_nothing_locked(self):
        r = escrow_locked(_client([]))
        assert r["locked_uact"] == 0
        assert r["deployments"] == 0

    def test_ignores_non_uact_denoms(self):
        """Only uact (the USD-pegged Console deploy currency) counts against the
        uact grant — an AKT-denominated escrow must not be subtracted from it."""
        c = _client([_deployment(1, 5_000_000), _deployment(2, 9_999_999, denom="uakt")])
        assert escrow_locked(c)["locked_uact"] == 5_000_000

    def test_deployment_with_no_escrow_counts_as_zero(self):
        c = _client([_deployment(1, None), _deployment(2, 5_000_000)])
        r = escrow_locked(c)
        assert r["locked_uact"] == 5_000_000
        assert r["deployments"] == 2

    def test_unreadable_deployment_is_skipped_not_fatal(self):
        """A deployment whose detail errors must not abort the tally — the sum
        becomes a lower bound, flagged via `unreadable`."""
        deployments = [_deployment(1, 5_000_000), _deployment(2, 5_000_000)]
        c = _client(deployments)

        def _get(dseq):
            if str(dseq) == "2":
                raise RuntimeError("API Error (500): boom")
            return deployments[0]

        c.get_deployment.side_effect = _get
        r = escrow_locked(c)
        assert r["locked_uact"] == 5_000_000  # lower bound
        assert r["unreadable"] == 1

    def test_tolerates_decimal_and_malformed_amounts(self):
        c = _client([_deployment(1, "5000000.000000"), _deployment(2, "notanumber")])
        assert escrow_locked(c)["locked_uact"] == 5_000_000
