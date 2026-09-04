"""Execute-path rails and wallet-pool scope for the escrow reaper (#250).

The reaper has run green four times a day, correctly identifying a stale set it
never closes. #250 promotes it — and a promotion that spends escrow on a shared
wallet earns rails proportionate to that.

Every rail here is tested for the property that actually matters: it REFUSES
LOUDLY and non-zero. A rail that declines quietly would reproduce the very
defect the issue is about, where "closed nothing because all is well" and
"closed nothing because something is wrong" render identically.

The pool tests cover the other half: a reaper that reads the singular
AKASH_API_KEY audits one of three wallets and reports green about the other two
forever — not because it checked, but because it never looked.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from just_akash import cleanup_stale as cs

NOW = time.time()
SRC = Path(__file__).resolve().parents[1] / "just_akash" / "cleanup_stale.py"


def _detail(services: list[str]) -> dict:
    return {
        "leases": [
            {"id": {"provider": "akash1prov"}, "status": {"services": {s: {} for s in services}}}
        ]
    }


def _dseq(age_seconds: float) -> str:
    return str(int((NOW - age_seconds) * 1000))


def _mock_client(deployments: dict[str, dict], address: str = "akash1me"):
    client = MagicMock()
    client.account_address.return_value = address
    client.get_deployment.side_effect = lambda d: deployments[str(d)]
    client._records = [
        {"deployment": {"state": "active", "id": {"owner": address, "dseq": d}}}
        for d in deployments
    ]
    return client


def _run(client, *, execute: bool, env: dict | None = None, **kw) -> int:
    with (
        patch.object(cs, "AkashConsoleAPI", return_value=client),
        patch.object(cs.chain, "list_active_deployments", return_value=client._records),
        patch.object(cs.chain, "deploy_credit", return_value={"uact": 100_000_000}),
        patch.object(
            cs,
            "escrow_locked",
            return_value={"locked_uact": 50_000_000, "deployments": 2, "by_deployment": {}},
        ),
        patch.dict("os.environ", env or {"AKASH_API_KEY": "k"}, clear=True),
        patch.object(cs.time, "sleep", lambda s: None),
    ):
        return cs.run(execute=execute, now=NOW, **kw)


# ── the verdict set is pinned to the source, not hand-maintained ─────────


class TestKnownVerdictsPin:
    def test_matches_every_verdict_classify_can_return(self):
        """A LIVE read of the source, not a frozen copy.

        If someone adds a verdict to classify() and not to KNOWN_VERDICTS, the
        execute path would refuse on it in production — correct, but a failing
        fleet is a worse place to learn that than a failing test. And if they
        add it to BOTH without thinking, this at least forces them to touch the
        set deliberately.
        """
        returned = set(re.findall(r'return "((?:STALE|LEAVE)-[a-z0-9-]+)"', SRC.read_text()))
        assert returned, "source scan found no verdicts — the regex has rotted"
        assert returned == set(cs.KNOWN_VERDICTS), (
            f"KNOWN_VERDICTS drift: source-only={sorted(returned - set(cs.KNOWN_VERDICTS))}, "
            f"constant-only={sorted(set(cs.KNOWN_VERDICTS) - returned)}"
        )

    def test_every_stale_verdict_is_known(self):
        assert set(cs.STALE_VERDICTS) <= set(cs.KNOWN_VERDICTS)


# ── rail 1: an unrecognised verdict aborts the whole execute path ────────


class TestUnknownVerdictRail:
    def test_refuses_loudly_and_closes_nothing(self, capsys):
        """Closing on a label the reaper cannot reason about is how a reaper
        starts closing the wrong thing."""
        d = _dseq(3 * 86400)
        client = _mock_client({d: _detail(["backtest"])})
        with patch.object(
            cs, "classify", return_value=("STALE-brand-new-idea", ["backtest"], 1.0)
        ):
            rc = _run(client, execute=True)
        assert rc == 2, "an unknown verdict must fail the run, not be skipped"
        client.close_deployment.assert_not_called()
        err = capsys.readouterr().err
        assert "REFUSING TO EXECUTE" in err
        assert "STALE-brand-new-idea" in err, "the refusal must name what it did not recognise"

    def test_refuses_the_whole_run_not_just_the_unknown_row(self, capsys):
        """The unknown verdict may be evidence the KNOWN ones are now being
        judged by changed rules, so a partial close is not safer — it is the
        same gamble on a smaller sample."""
        known, unknown = _dseq(3 * 86400), _dseq(4 * 86400)
        client = _mock_client({known: _detail(["backtest"]), unknown: _detail(["backtest"])})
        seq = iter([("STALE-e2e", ["backtest"], 3.0), ("STALE-who-knows", ["backtest"], 4.0)])
        with patch.object(cs, "classify", side_effect=lambda *a, **k: next(seq)):
            rc = _run(client, execute=True)
        assert rc == 2
        client.close_deployment.assert_not_called()

    def test_dry_run_is_unaffected_by_an_unknown_verdict(self, capsys):
        """Rails bind the execute path only. A dry run's job is to REPORT what
        it found, including something it does not recognise."""
        d = _dseq(3 * 86400)
        client = _mock_client({d: _detail(["backtest"])})
        with patch.object(cs, "classify", return_value=("STALE-mystery", ["backtest"], 1.0)):
            rc = _run(client, execute=False)
        assert rc == 0
        assert "DRY RUN" in capsys.readouterr().out


# ── rail 2: the shape tripwire ───────────────────────────────────────────


class TestStaleFractionRail:
    def test_refuses_when_almost_everything_looks_closable(self, capsys):
        """Not about any single row. If the classifier suddenly calls nearly
        everything garbage, the likeliest cause is the classifier."""
        n = cs.MIN_AUDITED_FOR_FRACTION_RAIL + 5
        deployments = {_dseq(3 * 86400 + i): _detail(["backtest"]) for i in range(n)}
        client = _mock_client(deployments)
        rc = _run(client, execute=True)
        assert rc == 2, f"{n}/{n} closable is above the tripwire and must refuse"
        client.close_deployment.assert_not_called()
        err = capsys.readouterr().err
        assert "REFUSING TO EXECUTE" in err
        assert "tripwire" in err

    def test_a_small_all_stale_account_is_NOT_refused(self, capsys):
        """Found by the pre-existing suite, and it is a real design point rather
        than a fixture quirk: an account with a handful of deployments that are
        ALL genuinely test residue is 100% stale and completely normal. An
        ungated fraction rail would deadlock exactly the small accounts it
        should be draining, and would do so permanently."""
        deployments = {_dseq(3 * 86400 + i): _detail(["backtest"]) for i in range(3)}
        client = _mock_client(deployments)
        rc = _run(client, execute=True)
        assert rc == 0, "a small all-stale account must still be reapable"
        assert client.close_deployment.call_count == 3

    def test_a_normal_share_passes(self, capsys):
        """Today's real split is 55/154 = 36%. A rail that fired on that would
        be removed within a week, which is worse than not having it."""
        d = {_dseq(3 * 86400): _detail(["backtest"])}
        d.update({_dseq(60 + i): _detail(["node"]) for i in range(3)})
        client = _mock_client(d)
        rc = _run(client, execute=True)
        assert rc == 0
        assert client.close_deployment.call_count == 1

    def test_dry_run_reports_rather_than_refusing(self):
        n = cs.MIN_AUDITED_FOR_FRACTION_RAIL + 5
        deployments = {_dseq(3 * 86400 + i): _detail(["backtest"]) for i in range(n)}
        assert _run(_mock_client(deployments), execute=False) == 0


# ── rail 3: the cap, oldest-first ────────────────────────────────────────


class TestCapRail:
    def _many(self, n: int) -> dict[str, dict]:
        # Distinct ages so "oldest first" is checkable, all well past the floor.
        return {_dseq(3 * 86400 + i * 3600): _detail(["backtest"]) for i in range(n)}

    def test_closes_at_most_the_cap(self, capsys):
        deployments = self._many(6)
        # Pad with keepers so the stale FRACTION stays under rail 2's tripwire —
        # otherwise this would test rail 2 by accident.
        deployments.update({_dseq(60 + i): _detail(["node"]) for i in range(4)})
        client = _mock_client(deployments)
        rc = _run(client, execute=True, max_close=2)
        assert rc == 0
        assert client.close_deployment.call_count == 2
        assert "CAP:" in capsys.readouterr().out

    def test_closes_the_OLDEST_first(self):
        """A capped pass should free the escrow that has been locked longest."""
        ages = [10 * 86400, 5 * 86400, 3 * 86400]
        stale = {_dseq(a): _detail(["backtest"]) for a in ages}
        oldest = _dseq(ages[0])
        deployments = dict(stale)
        deployments.update({_dseq(60 + i): _detail(["node"]) for i in range(4)})
        client = _mock_client(deployments)
        _run(client, execute=True, max_close=1)
        closed = [c.args[0] for c in client.close_deployment.call_args_list]
        assert closed == [oldest]

    def test_says_it_stopped_short(self, capsys):
        """Progress that hides its own incompleteness is how a backlog becomes
        invisible again — the failure this issue is about."""
        deployments = self._many(6)
        deployments.update({_dseq(60 + i): _detail(["node"]) for i in range(4)})
        _run(_mock_client(deployments), execute=True, max_close=2)
        out = capsys.readouterr().out
        assert "CAPPED" in out and "re-run" in out

    def test_under_the_cap_says_nothing_about_capping(self, capsys):
        d = {_dseq(3 * 86400): _detail(["backtest"])}
        d.update({_dseq(60 + i): _detail(["node"]) for i in range(3)})
        _run(_mock_client(d), execute=True, max_close=25)
        out = capsys.readouterr().out
        assert "CAP:" not in out and "CAPPED" not in out


# ── the wallet pool: scope, and saying what was in it ────────────────────


class TestWalletPoolScope:
    def test_single_key_behaves_exactly_as_before(self, capsys):
        """Backward compatible BY CONSTRUCTION: configured_api_keys() appends
        the singular AKASH_API_KEY, so today's CI config resolves to one key."""
        d = {_dseq(3 * 86400): _detail(["backtest"])}
        client = _mock_client(d)
        with (
            patch.object(cs, "AkashConsoleAPI", return_value=client),
            patch.object(cs.chain, "list_active_deployments", return_value=client._records),
            patch.object(cs.chain, "deploy_credit", return_value={"uact": 100_000_000}),
            patch.object(
                cs,
                "escrow_locked",
                return_value={"locked_uact": 5, "deployments": 1, "by_deployment": {}},
            ),
            patch.dict("os.environ", {"AKASH_API_KEY": "k"}, clear=True),
            patch.object(cs.time, "sleep", lambda s: None),
        ):
            rc = cs.run_all_wallets(execute=False, now=NOW)
        assert rc == 0
        assert "Console wallets configured: 1" in capsys.readouterr().out

    def test_audits_every_configured_wallet(self, capsys):
        """The #250 defect: reading the singular key audits one of three and
        reports green about the other two — never having looked."""
        d = {_dseq(3 * 86400): _detail(["backtest"])}
        client = _mock_client(d)
        with (
            patch.object(cs, "AkashConsoleAPI", return_value=client),
            patch.object(cs.chain, "list_active_deployments", return_value=client._records),
            patch.object(cs.chain, "deploy_credit", return_value={"uact": 100_000_000}),
            patch.object(
                cs,
                "escrow_locked",
                return_value={"locked_uact": 5, "deployments": 1, "by_deployment": {}},
            ),
            patch.dict("os.environ", {"AKASH_API_KEYS": "a,b,c"}, clear=True),
            patch.object(cs.time, "sleep", lambda s: None),
        ):
            rc = cs.run_all_wallets(execute=False, now=NOW)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Console wallets configured: 3" in out
        for i in (1, 2, 3):
            assert f"wallet {i}/3" in out, "every wallet must be visibly audited"

    def test_says_how_many_wallets_before_auditing_any(self, capsys):
        """'1 wallet, clean' and '3 wallets, only 1 audited' must never render
        the same way — that is the whole defect, restated."""
        d = {_dseq(3 * 86400): _detail(["backtest"])}
        client = _mock_client(d)
        with (
            patch.object(cs, "AkashConsoleAPI", return_value=client),
            patch.object(cs.chain, "list_active_deployments", return_value=client._records),
            patch.object(cs.chain, "deploy_credit", return_value={"uact": 100_000_000}),
            patch.object(
                cs,
                "escrow_locked",
                return_value={"locked_uact": 5, "deployments": 1, "by_deployment": {}},
            ),
            patch.dict("os.environ", {"AKASH_API_KEYS": "a;b"}, clear=True),
            patch.object(cs.time, "sleep", lambda s: None),
        ):
            cs.run_all_wallets(execute=False, now=NOW)
        out = capsys.readouterr().out
        assert out.index("Console wallets configured: 2") < out.index("wallet 1/2")

    def test_no_keys_at_all_refuses(self, capsys):
        with patch.dict("os.environ", {}, clear=True):
            assert cs.run_all_wallets(execute=False, now=NOW) == 2
        assert "AKASH_API_KEY" in capsys.readouterr().err

    def test_worst_exit_code_wins(self):
        """One wallet refusing on a rail must not be masked by another's
        success — an aggregate that reports the best outcome is how a partial
        failure disappears."""
        calls = iter([0, 2, 0])
        with (
            patch.object(cs, "run", side_effect=lambda **kw: next(calls)),
            patch.dict("os.environ", {"AKASH_API_KEYS": "a,b,c"}, clear=True),
        ):
            assert cs.run_all_wallets(execute=False, now=NOW) == 2
