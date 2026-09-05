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
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from just_akash import cleanup_stale as cs

NOW = time.time()

# Dummy Console credentials. `configured_api_keys()` only splits on [,;\n],
# strips and de-duplicates, so the VALUES are arbitrary — these are named to say
# so at a glance, in the test and in any secrets baseline that records them.
# The two separators are covered deliberately: both are in the split regex.
FAKE_KEY = "not-a-real-console-key"
FAKE_KEYS_COMMA = "not-a-real-key-1,not-a-real-key-2,not-a-real-key-3"
FAKE_KEYS_SEMICOLON = "not-a-real-key-1;not-a-real-key-2"

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
        patch.dict("os.environ", env or {"AKASH_API_KEY": FAKE_KEY}, clear=True),
        patch.object(cs.time, "sleep", lambda s: None),
    ):
        return cs.run(execute=execute, now=NOW, **kw)


def _mock_pool(key_to_address: dict[str, str], deployments: dict | None = None):
    """One client per KEY, each answering with its OWN account address.

    ⛔ The previous harness returned a single client for every key, so three
    keys resolved to one account while the suite asserted "3 wallets audited".
    It modelled one wallet three times and called it a pool — which is why the
    suite could not have caught the keys-are-not-wallets defect it was written
    to cover. A fixture that cannot express the bug cannot exclude it.
    """

    return {
        key: _mock_client(dict(deployments or {}), address=address)
        for key, address in key_to_address.items()
    }


@contextmanager
def _pool(clients: dict, env: dict):
    records = {c.account_address.return_value: c._records for c in clients.values()}
    with (
        patch.object(cs, "AkashConsoleAPI", side_effect=lambda key: clients[key]),
        patch.object(
            cs.chain, "list_active_deployments", side_effect=lambda a: records.get(a, [])
        ),
        patch.object(cs.chain, "deploy_credit", return_value={"uact": 100_000_000}),
        patch.object(
            cs,
            "escrow_locked",
            return_value={"locked_uact": 5, "deployments": 1, "by_deployment": {}},
        ),
        patch.dict("os.environ", env, clear=True),
        patch.object(cs.time, "sleep", lambda s: None),
    ):
        yield


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
            patch.dict("os.environ", {"AKASH_API_KEY": FAKE_KEY}, clear=True),
            patch.object(cs.time, "sleep", lambda s: None),
        ):
            rc = cs.run_all_wallets(execute=False, now=NOW)
        assert rc == 0
        assert "Console wallets configured: 1" in capsys.readouterr().out

    def test_audits_every_configured_wallet(self, capsys):
        """The #250 defect: reading the singular key audits one of three and
        reports green about the other two — never having looked."""
        clients = _mock_pool(
            dict(zip(FAKE_KEYS_COMMA.split(","), ("akash1a", "akash1b", "akash1c"), strict=True)),
            {_dseq(3 * 86400): _detail(["backtest"])},
        )
        with _pool(clients, {"AKASH_API_KEYS": FAKE_KEYS_COMMA}):
            rc = cs.run_all_wallets(execute=False, now=NOW)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Console wallets configured: 3" in out
        for i in (1, 2, 3):
            assert f"wallet {i}/3" in out, "every wallet must be visibly audited"

    def test_says_how_many_wallets_before_auditing_any(self, capsys):
        """'1 wallet, clean' and '3 wallets, only 1 audited' must never render
        the same way — that is the whole defect, restated."""
        clients = _mock_pool(
            dict(zip(FAKE_KEYS_SEMICOLON.split(";"), ("akash1a", "akash1b"), strict=True)),
            {_dseq(3 * 86400): _detail(["backtest"])},
        )
        with _pool(clients, {"AKASH_API_KEYS": FAKE_KEYS_SEMICOLON}):
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
        clients = _mock_pool(
            dict(zip(FAKE_KEYS_COMMA.split(","), ("akash1a", "akash1b", "akash1c"), strict=True))
        )
        with (
            _pool(clients, {"AKASH_API_KEYS": FAKE_KEYS_COMMA}),
            patch.object(cs, "run", side_effect=lambda **kw: next(calls)),
        ):
            assert cs.run_all_wallets(execute=False, now=NOW) == 2


# ── the two defects two reviewers found independently (PR #256) ──────────


class TestCapCannotInvert:
    """A bound on blast radius that EXPANDS it given a bad value is worse than
    no bound: its presence is what makes an operator believe the run is capped.
    `stale[:-1]` closes all but one — measured at 54 of 55."""

    def test_negative_cap_refuses_instead_of_closing_almost_everything(self, capsys):
        deployments = {_dseq(3 * 86400 + i): _detail(["backtest"]) for i in range(6)}
        deployments.update({_dseq(60 + i): _detail(["node"]) for i in range(6)})
        client = _mock_client(deployments)
        rc = _run(client, execute=True, max_close=-1)
        assert rc == 2, "a negative cap must refuse, not slice from the end"
        client.close_deployment.assert_not_called()
        assert "must be >= 1" in capsys.readouterr().err

    def test_zero_cap_refuses_rather_than_silently_closing_nothing(self, capsys):
        """`stale[:0]` is empty — safe by accident, but it reports a CAP while
        doing nothing, which is the silent-no-op shape this issue is about."""
        deployments = {_dseq(3 * 86400 + i): _detail(["backtest"]) for i in range(3)}
        deployments.update({_dseq(60 + i): _detail(["node"]) for i in range(3)})
        rc = _run(_mock_client(deployments), execute=True, max_close=0)
        assert rc == 2
        assert "must be >= 1" in capsys.readouterr().err

    def test_rejected_at_parse_time_too(self):
        """Two locks, because they catch different failures: this one turns a
        CLI typo into an immediate usage error; the execute-path check catches
        an in-process caller that never goes through argparse."""
        import pytest

        for bad in ["-1", "0"]:
            with pytest.raises(SystemExit):
                cs.main(["--max-close", bad])

    def test_a_valid_cap_still_parses(self):
        assert cs._positive_cap("7") == 7


class TestTripwireDenominator:
    """Read failures must not loosen a safety rail. Rows skipped before
    classify() were counted in the denominator, understating the stale fraction
    and making the tripwire LESS likely to fire exactly when the API is flaky."""

    def test_unreadable_rows_are_excluded_from_the_denominator(self, capsys):
        n = cs.MIN_AUDITED_FOR_FRACTION_RAIL + 2
        stale = {_dseq(3 * 86400 + i): _detail(["backtest"]) for i in range(n)}
        # Enough unreadable rows that counting them would drag the fraction
        # under the tripwire and let a fully-stale account through.
        unreadable = {_dseq(500 + i): None for i in range(n * 2)}
        deployments = {**stale, **unreadable}

        client = MagicMock()
        client.account_address.return_value = "akash1me"

        def _get(d):
            detail = deployments[str(d)]
            if detail is None:
                raise RuntimeError("detail read failed")
            return detail

        client.get_deployment.side_effect = _get
        client._records = [
            {"deployment": {"state": "active", "id": {"owner": "akash1me", "dseq": d}}}
            for d in deployments
        ]

        rc = _run(client, execute=True)
        assert rc == 2, (
            "unreadable rows padded the denominator and let a fully-stale "
            "account past the tripwire"
        )
        client.close_deployment.assert_not_called()
        err = capsys.readouterr().err
        assert "CLASSIFIED" in err, "the refusal must say which denominator it used"
        assert "unreadable and excluded" in err


# ── the pool-arrival guard (#259, landmine from just-akash#167) ──────────


class TestWalletsExpectedGuard:
    """A plural that silently degrades to a singular is the green-because-it-
    never-ran defect wearing a different hat: the job audits one wallet and
    reports clean about the others, indistinguishable from a healthy
    single-wallet run. The receiver cannot infer intent from an empty variable,
    so intent is declared and a mismatch is fatal."""

    def _run_pool(self, env: dict, addresses: tuple[str, ...] | None = None) -> int:
        raw = env.get("AKASH_API_KEYS", "").strip() or env.get("AKASH_API_KEY", "")
        keys = [k for k in (p.strip() for p in re.split(r"[,;]", raw)) if k]
        addrs = addresses or tuple(f"akash1w{i}" for i in range(len(keys)))
        clients = _mock_pool(
            dict(zip(keys, addrs, strict=True)), {_dseq(3 * 86400): _detail(["backtest"])}
        )
        with _pool(clients, env):
            return cs.run_all_wallets(execute=False, now=NOW)

    def test_silent_downgrade_to_one_wallet_is_fatal(self, capsys):
        """The #167 shape: a caller pinned to a ref predating the pool has
        AKASH_API_KEYS read as empty. Without this guard the run audits the
        single fallback key and reports success."""
        rc = self._run_pool(
            {"AKASH_API_KEY": FAKE_KEY, "AKASH_API_KEYS": "", "AKASH_WALLETS_EXPECTED": "3"}
        )
        assert rc == 2, "a pool that did not arrive must fail, not quietly audit one wallet"
        err = capsys.readouterr().err
        assert "AKASH_WALLETS_EXPECTED=3" in err and "1 Console wallet" in err

    def test_matching_count_proceeds(self, capsys):
        rc = self._run_pool({"AKASH_API_KEYS": FAKE_KEYS_COMMA, "AKASH_WALLETS_EXPECTED": "3"})
        assert rc == 0
        assert "Console wallets configured: 3" in capsys.readouterr().out

    def test_unset_asserts_nothing(self, capsys):
        """Today's config has no pool secret. The guard must not turn that into
        a failure — it is opt-in, declared by whoever provisions the pool."""
        assert self._run_pool({"AKASH_API_KEY": FAKE_KEY}) == 0

    def test_non_integer_expectation_is_rejected(self, capsys):
        rc = self._run_pool({"AKASH_API_KEY": FAKE_KEY, "AKASH_WALLETS_EXPECTED": "three"})
        assert rc == 2
        assert "must be an integer" in capsys.readouterr().err

    def test_more_wallets_than_expected_also_fails(self, capsys):
        """Symmetric on purpose: resolving MORE than declared means the pool is
        not what the operator thinks it is, and a reaper closing deployments
        should not act on a wallet set nobody declared."""
        rc = self._run_pool({"AKASH_API_KEYS": FAKE_KEYS_COMMA, "AKASH_WALLETS_EXPECTED": "2"})
        assert rc == 2


# ── a key is not a wallet (#261 review) ─────────────────────────────────


class TestKeysAreNotWallets:
    """The guard was named WALLETS_EXPECTED and counted KEYS.

    The repo supports several keys resolving to one account, so two aliases
    satisfied AKASH_WALLETS_EXPECTED=2 while the pool was one wallet — the
    silent shortfall the guard exists to catch, reproduced inside the guard.

    The count is the smaller half. `run()` enumerates from the CHAIN by
    address, so duplicate keys walk the IDENTICAL deployment set once per key
    with `max_close` applied per call: a cap of 25 closes 50. That is the #256
    blast-radius rail inverting again by another route, and it is why these
    tests assert on close calls and not only on printed counts.
    """

    ALIASES = "alias-a,alias-b"

    def test_two_keys_for_one_account_count_as_one_wallet(self, capsys):
        clients = _mock_pool(
            {"alias-a": "akash1same", "alias-b": "akash1same"},
            {_dseq(3 * 86400): _detail(["backtest"])},
        )
        with _pool(clients, {"AKASH_API_KEYS": self.ALIASES}):
            rc = cs.run_all_wallets(execute=False, now=NOW)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Console wallets configured: 1" in out
        assert "2 keys, 1 alias(es) of an account already in scope" in out
        assert "wallet 2/" not in out, "one account must be audited once, not once per key"

    def test_expected_two_is_not_satisfied_by_two_aliases_of_one_wallet(self, capsys):
        """The reviewer's exact case: the guard must not accept a key count."""
        clients = _mock_pool(
            {"alias-a": "akash1same", "alias-b": "akash1same"},
            {_dseq(3 * 86400): _detail(["backtest"])},
        )
        with _pool(clients, {"AKASH_API_KEYS": self.ALIASES, "AKASH_WALLETS_EXPECTED": "2"}):
            rc = cs.run_all_wallets(execute=False, now=NOW)
        assert rc == 2, "two aliases for one account is one wallet, not two"
        err = capsys.readouterr().err
        assert "1 Console wallet(s) resolved from 2 key(s)" in err

    def test_duplicate_keys_do_not_double_the_close_cap(self):
        """The rail, not the label. Both keys reach one account holding more
        stale deployments than the cap; auditing per key would close 2x."""
        stale = {
            _dseq(3 * 86400 + i): _detail(["backtest"]) for i in range(cs.MAX_CLOSE_PER_RUN + 5)
        }
        keep = {_dseq(60 + i): _detail(["node"]) for i in range(cs.MAX_CLOSE_PER_RUN + 5)}
        clients = _mock_pool({"alias-a": "akash1same", "alias-b": "akash1same"}, {**stale, **keep})
        with _pool(clients, {"AKASH_API_KEYS": self.ALIASES}):
            cs.run_all_wallets(execute=True, now=NOW)

        closed = sum(c.close_deployment.call_count for c in clients.values())
        assert closed == cs.MAX_CLOSE_PER_RUN, (
            f"cap is {cs.MAX_CLOSE_PER_RUN} per run; auditing one account once "
            f"per alias closed {closed}"
        )

    def test_an_unidentifiable_key_refuses_rather_than_guessing(self, capsys):
        """We cannot tell three wallets from one wallet three times without
        resolving every key, and one of those closes 3x the cap."""
        clients = _mock_pool(
            {"alias-a": "akash1a", "alias-b": "akash1b"},
            {_dseq(3 * 86400): _detail(["backtest"])},
        )
        clients["alias-b"].account_address.side_effect = RuntimeError("403")
        with _pool(clients, {"AKASH_API_KEYS": self.ALIASES}):
            rc = cs.run_all_wallets(execute=False, now=NOW)
        assert rc == 2
        err = capsys.readouterr().err
        assert "position(s) 2 of 2" in err
        assert "alias-b" not in err, "a refusal must never echo the credential"
        clients["alias-a"].close_deployment.assert_not_called()

    def test_resolution_happens_before_any_wallet_is_audited(self):
        """Eager by design: this function has no selection step to assert
        after — it iterates every wallet and closes as it goes, so a guard
        that fires afterwards is a post-mortem."""
        clients = _mock_pool(
            {"alias-a": "akash1a", "alias-b": "akash1b"},
            {_dseq(3 * 86400): _detail(["backtest"])},
        )
        clients["alias-b"].account_address.side_effect = RuntimeError("403")
        with _pool(clients, {"AKASH_API_KEYS": self.ALIASES}):
            assert cs.run_all_wallets(execute=True, now=NOW) == 2
        for client in clients.values():
            client.close_deployment.assert_not_called()


class TestDeclaredPoolWithNoCredential:
    """`AKASH_WALLETS_EXPECTED=N` with NO keys is the #167 scenario itself, not
    an ordinary unconfigured run. Reporting only "neither var is set" would drop
    the fact that a pool was declared — the single most diagnostic thing known
    about the failure — so the guard keeps precedence and names both facts."""

    def test_names_the_declared_pool_and_the_missing_credential(self, capsys):
        clients = _mock_pool({}, {})
        with _pool(clients, {"AKASH_WALLETS_EXPECTED": "3"}):
            rc = cs.run_all_wallets(execute=False, now=NOW)
        assert rc == 2
        err = capsys.readouterr().err
        assert "AKASH_WALLETS_EXPECTED=3" in err, "the declared intent must survive"
        assert "0 Console wallet(s) resolved from 0 key(s)" in err
        assert "Neither AKASH_API_KEY nor AKASH_API_KEYS is set" in err, (
            "the operator must still learn no credential arrived"
        )

    def test_no_keys_and_no_expectation_still_says_only_that(self, capsys):
        """Unset asserts nothing, so this stays the plain message."""
        with patch.dict("os.environ", {}, clear=True):
            assert cs.run_all_wallets(execute=False, now=NOW) == 2
        err = capsys.readouterr().err
        assert "neither AKASH_API_KEY nor AKASH_API_KEYS is set" in err
        assert "AKASH_WALLETS_EXPECTED" not in err


# ── the dry run must predict the execute run (#250) ─────────────────────


def _plan(
    stale, *, classified=100, max_close=cs.MAX_CLOSE_PER_RUN, verdicts=None, enumerated=None
):
    return cs._execution_plan(
        stale=list(stale),
        stale_ages={d: float(i) for i, d in enumerate(stale)},
        seen_verdicts=verdicts if verdicts is not None else {"STALE-e2e"},
        classified=classified,
        enumerated=enumerated if enumerated is not None else classified,
        max_close=max_close,
    )


class TestCapConstrainsAtTheBoundary:
    """⛔ This file has already produced TWO rail inversions — `--max-close -1`
    EXPANDED the cap via `stale[:-1]`, and the key/address collapse DOUBLED it
    by auditing one account once per alias. So the cap is asserted AT the
    boundary and ONE PAST IT in both directions, not merely 'somewhere in the
    middle', which is where both inversions hid.
    """

    def test_exactly_at_the_cap_is_not_capped(self):
        plan = _plan([f"d{i}" for i in range(25)], max_close=25)
        assert plan.capped is False
        assert plan.would_close == 25, "N == cap must close all N, not N-1"

    def test_one_past_the_cap_closes_exactly_the_cap(self):
        plan = _plan([f"d{i}" for i in range(26)], max_close=25)
        assert plan.capped is True
        assert plan.would_close == 25
        assert plan.closable == 26, "the pre-cap total must survive for the report"

    def test_one_under_the_cap_closes_everything(self):
        plan = _plan([f"d{i}" for i in range(24)], max_close=25)
        assert plan.capped is False
        assert plan.would_close == 24

    def test_a_cap_of_one_closes_one(self):
        plan = _plan([f"d{i}" for i in range(9)], max_close=1)
        assert plan.would_close == 1, "the smallest legal cap must still constrain"

    @pytest.mark.parametrize("bad", [0, -1, -25])
    def test_a_non_positive_cap_closes_NOTHING_rather_than_inverting(self, bad):
        """The measured inversion: `stale[:-1]` closed 54 of 55. A bound that
        EXPANDS blast radius given a bad value is worse than no bound, because
        its presence is what makes an operator believe the run is capped."""
        plan = _plan([f"d{i}" for i in range(9)], max_close=bad)
        assert plan.refusal is not None
        assert plan.would_close == 0
        assert plan.to_close == []

    def test_the_cap_takes_the_OLDEST(self):
        """A partial pass must free the escrow locked longest, or the backlog
        drains newest-first and the oldest lease never leaves."""
        stale = [f"d{i}" for i in range(30)]
        plan = cs._execution_plan(
            stale=stale,
            stale_ages={d: float(i) for i, d in enumerate(stale)},
            seen_verdicts={"STALE-e2e"},
            classified=100,
            enumerated=100,
            max_close=3,
        )
        assert plan.to_close == ["d29", "d28", "d27"]


class TestTripwireBoundary:
    def test_exactly_at_the_tripwire_does_not_refuse(self):
        """`>` not `>=`: at exactly the threshold the run proceeds. Asserted so
        a later `>=` 'tidy-up' is a test failure rather than a silent change to
        when this reaper refuses."""
        n = 100
        stale = [f"d{i}" for i in range(int(n * cs.MAX_STALE_FRACTION))]
        plan = _plan(stale, classified=n)
        assert plan.refusal is None

    def test_one_past_the_tripwire_refuses(self):
        n = 100
        stale = [f"d{i}" for i in range(int(n * cs.MAX_STALE_FRACTION) + 1)]
        plan = _plan(stale, classified=n)
        assert plan.refusal is not None and "tripwire" in plan.refusal

    def test_below_the_minimum_audited_the_fraction_rail_is_not_applied(self):
        """A small account is legitimately 100% stale; an ungated fraction rail
        deadlocks it permanently."""
        n = cs.MIN_AUDITED_FOR_FRACTION_RAIL - 1
        plan = _plan([f"d{i}" for i in range(n)], classified=n)
        assert plan.refusal is None


class TestDryRunPredictsExecute:
    """⛔ THE DRY RUN RETURNED BEFORE EVERY RAIL. It printed the CLASSIFICATION
    count and stopped, so `stale (closable): 55` did not mean 55 would close —
    with the cap it meant 25, and above the tripwire it meant zero and a
    refusal. The documented protocol is "dispatch dry-run, review the verdict
    table, then dispatch execute=true": the review step was reading a figure
    that does not predict the step it authorises."""

    def _dry(self, deployments, capsys, **kw):
        client = _mock_client(deployments)
        rc = _run(client, execute=False, **kw)
        return rc, capsys.readouterr().out, client

    def test_the_dry_run_states_what_execute_would_close(self, capsys):
        deployments = {_dseq(3 * 86400 + i): _detail(["backtest"]) for i in range(30)}
        deployments.update({_dseq(60 + i): _detail(["node"]) for i in range(30)})
        rc, out, client = self._dry(deployments, capsys)
        assert rc == 0
        assert "WOULD EXECUTE:" in out
        assert f"would close {cs.MAX_CLOSE_PER_RUN}" in out, (
            "a dry run reporting only the closable count does not predict the "
            "capped outcome it is authorising"
        )
        client.close_deployment.assert_not_called()

    def test_the_dry_run_warns_that_execute_would_refuse(self, capsys):
        """The costliest surprise: review 'N closable', dispatch execute, and
        get a refusal instead — with the backlog untouched and the operator
        believing they have acted."""
        n = cs.MIN_AUDITED_FOR_FRACTION_RAIL + 10
        deployments = {_dseq(3 * 86400 + i): _detail(["backtest"]) for i in range(n)}
        rc, out, client = self._dry(deployments, capsys)
        assert rc == 0, "reporting a would-refuse is not itself a failure"
        assert "would REFUSE" in out and "tripwire" in out
        client.close_deployment.assert_not_called()

    def test_a_dry_run_still_closes_nothing_whatever_the_plan_says(self, capsys):
        deployments = {_dseq(3 * 86400 + i): _detail(["backtest"]) for i in range(5)}
        deployments.update({_dseq(60 + i): _detail(["node"]) for i in range(5)})
        _, _, client = self._dry(deployments, capsys)
        client.close_deployment.assert_not_called()
