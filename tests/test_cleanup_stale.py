"""Tests for the stale-deployment escrow reaper (just_akash.cleanup_stale).

The classifier must be conservative: only unambiguous test residue (old probe /
old e2e backtest) is closable; everything else — real services, empty service
sets, unknown ages, young deployments — is left alone.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from just_akash import cleanup_stale as cs

NOW = time.time()


def _detail(services: list[str]) -> dict:
    return {
        "leases": [
            {"id": {"provider": "akash1prov"}, "status": {"services": {s: {} for s in services}}}
        ]
    }


def _dseq(age_seconds: float) -> str:
    return str(int((NOW - age_seconds) * 1000))


class TestClassify:
    def test_old_probe_is_stale(self):
        verdict, _, _ = cs.classify(_detail(["probe"]), _dseq(2 * 3600), NOW)
        assert verdict == "STALE-probe"

    def test_young_probe_is_spared(self):
        # A concurrent smoke run may still hold it.
        verdict, _, _ = cs.classify(_detail(["probe"]), _dseq(600), NOW)
        assert verdict == "LEAVE-young-or-unaged-probe"

    def test_old_backtest_is_stale(self):
        verdict, _, _ = cs.classify(_detail(["backtest"]), _dseq(3 * 86400), NOW)
        assert verdict == "STALE-e2e"

    def test_recent_backtest_is_spared(self):
        verdict, _, _ = cs.classify(_detail(["backtest"]), _dseq(86400), NOW)
        assert verdict == "LEAVE-recent-backtest"

    def test_unknown_age_backtest_is_spared(self):
        # Legacy block-height dseq -> unaged -> never reaped.
        verdict, _, age = cs.classify(_detail(["backtest"]), "1234567", NOW)
        assert verdict == "LEAVE-recent-backtest" and age is None

    def test_real_services_are_never_stale(self):
        for services in (["node"], ["runner"], ["train"], ["backtest", "probe"], ["app"]):
            verdict, _, _ = cs.classify(_detail(services), _dseq(30 * 86400), NOW)
            assert verdict == "LEAVE-real-or-unknown", services

    def test_a_runner_pool_needs_BOTH_the_flag_and_proven_ownership(self):
        """Opting in is necessary and NOT sufficient.

        The flag used to stand alone, resting on the operator declaring that the Console
        account hosted nothing but their own pools. That declaration was measurably false
        on the very wallet this ships against: a live read on 2026-08-12 found 11 active
        deployments, SIX of them `dfci-infra-runner` — a sibling repo's runners on the
        shared wallet. Reaping on shape plus an assertion would have destroyed them.
        """
        old = _dseq(8 * 3600)
        ours = ["just-akash-runner"]
        assert cs.classify(_detail(["runner"]), old, NOW)[0] == "LEAVE-real-or-unknown"
        assert (
            cs.classify(_detail(["runner"]), old, NOW, reap_runners=True, group_names=ours)[0]
            == "STALE-runner"
        )

    def test_the_CANARY_survives_even_with_the_flag_on_and_our_own_prefix(self):
        """⛔ THE KNOWN-NEGATIVE THIS SUITE DID NOT HAVE.

        `just-akash-canary` is a long-lived service whose 200 GiB volume was destroyed
        FOUR times by a widened prefix. Nothing in this file asserted it survives, so the
        protection was structural-by-accident rather than tested.

        It is safe for a reason worth pinning: the runner branch is entered only on
        `services == ['runner']`, and the canary's service set is `['canary']`. It is
        excluded by SERVICE IDENTITY, never by a name filter — so it survives even with
        `reap_runners=True`, even carrying OUR OWN placement prefix, and even at an age
        far past every stale floor. A prefix-based guard would not give that.
        """
        ancient = _dseq(30 * 86400)
        ours = ["just-akash-canary"]
        verdict, _, _ = cs.classify(
            _detail(["canary"]), ancient, NOW, reap_runners=True, group_names=ours
        )
        assert verdict == "LEAVE-real-or-unknown", (
            "the canary must never be classified stale: flag on, our prefix, 30 days old"
        )

    def test_a_mixed_service_set_containing_the_runner_is_NOT_reaped(self):
        """⚠ `services == ['runner']` is an EQUALITY, not a membership test, and that is
        load-bearing. A deployment that runs a runner ALONGSIDE something else is not a
        disposable pool, and must not be reaped as one."""
        old = _dseq(8 * 3600)
        for mixed in (["canary", "runner"], ["runner", "backtest"], ["runner", "consul"]):
            verdict, _, _ = cs.classify(
                _detail(mixed), old, NOW, reap_runners=True, group_names=["just-akash-runner"]
            )
            assert verdict == "LEAVE-real-or-unknown", mixed

    def test_another_repos_runner_on_the_shared_wallet_is_left_alone(self):
        """The concrete deployment this protects: `dfci-infra-runner`, six of them live
        on our wallet when this was written. Same service set, same age, not ours."""
        old = _dseq(8 * 3600)
        for foreign in (["dfci-infra-runner"], ["dfci-infra-consul"], ["akash"], ["dcloud"]):
            verdict, _, _ = cs.classify(
                _detail(["runner"]), old, NOW, reap_runners=True, group_names=foreign
            )
            assert verdict == "LEAVE-not-ours", foreign

    def test_unreadable_provenance_is_not_unowned(self):
        """Every endpoint may have failed, or the deployment may have closed under us.
        Destroying on a failed read is the same class of error as destroying on a guess —
        and this one would be silent, because a dead LCD looks like an empty answer."""
        old = _dseq(8 * 3600)
        for unknown in (None, []):
            verdict, _, _ = cs.classify(
                _detail(["runner"]), old, NOW, reap_runners=True, group_names=unknown
            )
            assert verdict == "LEAVE-unverified-runner", unknown

    def test_an_owned_runner_pool_is_still_spared_while_it_could_be_live(self):
        """A pool is long-lived BY DESIGN — `ephemeral: false` outlives a single job and a
        slow matrix runs for hours — so the probe's 1h threshold would reap running CI.
        Proving it is ours does not make it disposable."""
        for age in (600, 3 * 3600, 5 * 3600):
            verdict, _, _ = cs.classify(
                _detail(["runner"]),
                _dseq(age),
                NOW,
                reap_runners=True,
                group_names=["just-akash-runner"],
            )
            assert verdict == "LEAVE-recent-runner", age

    def test_an_unaged_runner_pool_is_never_reaped_even_when_owned(self):
        """A dseq that yields no age must not be treated as ancient."""
        verdict, _, _ = cs.classify(
            _detail(["runner"]),
            "not-a-dseq",
            NOW,
            reap_runners=True,
            group_names=["just-akash-runner"],
        )
        assert verdict == "LEAVE-recent-runner"

    def test_opting_in_does_not_widen_the_blast_radius_to_other_services(self):
        """The flag enables ONE service set. It must not become a general 'reap anything'."""
        for services in (["node"], ["train"], ["app"], ["runner", "sidecar"]):
            verdict, _, _ = cs.classify(
                _detail(services), _dseq(30 * 86400), NOW, True, ["just-akash-runner"]
            )
            assert verdict == "LEAVE-real-or-unknown", services

    def test_empty_service_set_is_unclassifiable(self):
        verdict, _, _ = cs.classify(_detail([]), _dseq(30 * 86400), NOW)
        assert verdict == "LEAVE-unclassifiable"
        verdict, _, _ = cs.classify({}, _dseq(30 * 86400), NOW)
        assert verdict == "LEAVE-unclassifiable"


def _chain_records(deployments) -> list[dict]:
    """The CHAIN's record shape — `{"deployment": {"id": {...}}}`, which is what
    `chain.list_active_deployments` returns and what the sweep now enumerates from.

    ⚠ These used to be stubbed onto `client.list_deployments`. That call was removed
    because Console's `GET /v1/deployments` does not scope to the API key: three distinct
    keys for three distinct accounts returned byte-identical bodies in the same minute,
    against a chain showing 23 / 33 / 0 active. A test that still stubbed the Console
    listing would be pinning an enumeration the sweep no longer performs — green, and
    describing nothing.
    """
    return [{"deployment": {"state": "active", "id": {"owner": "akash1me", "dseq": d}}} for d in deployments]


def _mock_client(deployments: dict[str, dict]):
    client = MagicMock()
    client.account_address.return_value = "akash1me"
    client.get_deployment.side_effect = lambda d: deployments[str(d)]
    client._records = _chain_records(deployments)
    return client


def _run(client, execute: bool) -> int:
    with (
        patch.object(cs, "AkashConsoleAPI", return_value=client),
        patch.object(cs.chain, "list_active_deployments", return_value=client._records),
        patch.object(cs.chain, "deploy_credit", return_value={"uact": 100_000_000}),
        patch.object(
            cs,
            "escrow_locked",
            return_value={"locked_uact": 50_000_000, "deployments": 2, "by_deployment": {}},
        ),
        patch.dict("os.environ", {"AKASH_API_KEY": "k"}),
        patch.object(cs.time, "sleep", lambda s: None),
    ):
        return cs.run(execute=execute, now=NOW)


class TestRun:
    def test_dry_run_closes_nothing(self, capsys):
        stale = _dseq(3 * 86400)
        client = _mock_client({stale: _detail(["backtest"])})
        assert _run(client, execute=False) == 0
        client.close_deployment.assert_not_called()
        out = capsys.readouterr().out
        assert "DRY RUN" in out and "STALE-e2e" in out

    def test_execute_closes_only_the_stale_set(self, capsys):
        stale_probe = _dseq(2 * 3600)
        stale_e2e = _dseq(3 * 86400)
        keeper_recent = _dseq(3600)
        keeper_real = _dseq(30 * 86400)
        client = _mock_client(
            {
                stale_probe: _detail(["probe"]),
                stale_e2e: _detail(["backtest"]),
                keeper_recent: _detail(["backtest"]),
                keeper_real: _detail(["node"]),
            }
        )
        assert _run(client, execute=True) == 0
        closed = {c.args[0] for c in client.close_deployment.call_args_list}
        assert closed == {stale_probe, stale_e2e}
        assert "credit AFTER" in capsys.readouterr().out

    def test_close_failure_reaps_the_rest_and_exits_nonzero(self, capsys):
        a, b = _dseq(3 * 86400), _dseq(4 * 86400)
        client = _mock_client({a: _detail(["backtest"]), b: _detail(["backtest"])})
        client.close_deployment.side_effect = [RuntimeError("API Error (500)"), {}]
        assert _run(client, execute=True) == 1
        assert client.close_deployment.call_count == 2  # kept going after the failure

    def test_unreadable_detail_is_left_alone(self, capsys):
        good = _dseq(3 * 86400)
        client = _mock_client({good: _detail(["backtest"]), "999": _detail([])})
        client.get_deployment.side_effect = lambda d: (
            (_ for _ in ()).throw(RuntimeError("API Error (500)"))
            if d == "999"
            else _detail(["backtest"])
        )
        assert _run(client, execute=True) == 0
        closed = {c.args[0] for c in client.close_deployment.call_args_list}
        assert closed == {good}


class TestChainEnumerationIsAuthoritative:
    """The sweep enumerates from the chain, and an unreadable chain is NOT an empty account.

    ⛔ The property these pin is the reason the enumeration moved. Console's listing could
    return another account's page, a short page, or a 403 — all indistinguishable from
    "this wallet is clean" to a caller whose next act is to CLOSE things.
    """

    def _run_with_chain(self, chain_result, execute=False):
        client = MagicMock()
        client.account_address.return_value = "akash1me"
        client.get_deployment.side_effect = lambda d: _detail(["probe"])
        with (
            patch.object(cs, "AkashConsoleAPI", return_value=client),
            patch.object(cs.chain, "list_active_deployments", return_value=chain_result),
            patch.object(cs.chain, "deploy_credit", return_value={"uact": 100_000_000}),
            patch.object(
                cs,
                "escrow_locked",
                return_value={"locked_uact": 0, "deployments": 0, "by_deployment": {}},
            ),
            patch.dict("os.environ", {"AKASH_API_KEY": "k"}),
            patch.object(cs.time, "sleep", lambda s: None),
        ):
            return cs.run(execute=execute, now=NOW), client

    def test_an_unreadable_chain_refuses_to_sweep(self, capsys):
        """⛔ THE LOAD-BEARING ONE. None must exit non-zero and close NOTHING — never be
        swept as an empty account."""
        rc, client = self._run_with_chain(None, execute=True)
        assert rc == 2, f"an unreadable chain returned {rc}, so a failed enumeration reads as success"
        client.close_deployment.assert_not_called()
        assert "refusing to sweep" in capsys.readouterr().err

    def test_a_genuinely_empty_account_succeeds(self, capsys):
        """Anti-vacuity partner: if [] also refused, the test above would pass while the
        sweep could never report a clean account at all."""
        rc, client = self._run_with_chain([], execute=True)
        assert rc == 0, "a readable, empty account must succeed"
        client.close_deployment.assert_not_called()
        assert "active deployments: 0" in capsys.readouterr().out

    def test_the_console_listing_is_never_called(self):
        """The whole point of the change. If this call came back, the sweep would silently
        depend on an enumeration that cannot scope to the account again."""
        _, client = self._run_with_chain(_chain_records(["1787822013544"]))
        client.list_deployments.assert_not_called()

    def test_enumeration_is_scoped_to_the_account_address(self):
        """The chain is asked about THIS key's owner, not some other address."""
        client = MagicMock()
        client.account_address.return_value = "akash1me"
        client.get_deployment.side_effect = lambda d: _detail(["probe"])
        seen = []
        with (
            patch.object(cs, "AkashConsoleAPI", return_value=client),
            patch.object(cs.chain, "list_active_deployments", side_effect=lambda o: seen.append(o) or []),
            patch.object(cs.chain, "deploy_credit", return_value={"uact": 100_000_000}),
            patch.object(
                cs,
                "escrow_locked",
                return_value={"locked_uact": 0, "deployments": 0, "by_deployment": {}},
            ),
            patch.dict("os.environ", {"AKASH_API_KEY": "k"}),
            patch.object(cs.time, "sleep", lambda s: None),
        ):
            cs.run(now=NOW)
        assert seen == ["akash1me"], f"chain was asked about {seen!r}, not the key's own owner"


class TestProtectedDseqs:
    """The never-close list holds when the classifier is wrong.

    ⛔ WHY IT IS NEEDED HERE AND NOT ONLY IN THE SIBLING SWEEPER. Two of the three closable
    classes are well-defended — a runner needs on-chain provenance, an unrecognised service
    set is LEAVE-real-or-unknown — but STALE-e2e closes on SERVICE NAME AND AGE ALONE.
    Measured against the shipped classifier: services=["backtest"] at 30 days -> STALE-e2e.
    A long-running research workload sharing a wallet with CI is indistinguishable from an
    interrupted e2e run, and the sibling sweeper destroyed a 200 GiB persistent volume four
    times learning that.
    """

    def _run(self, dseq, services, execute=True, env=None):
        client = MagicMock()
        client.account_address.return_value = "akash1me"
        client.get_deployment.side_effect = lambda d: _detail(services)
        environ = {"AKASH_API_KEY": "k"}
        environ.update(env or {})
        with (
            patch.object(cs, "AkashConsoleAPI", return_value=client),
            patch.object(cs.chain, "list_active_deployments", return_value=_chain_records([dseq])),
            patch.object(cs.chain, "deploy_credit", return_value={"uact": 100_000_000}),
            patch.object(
                cs,
                "escrow_locked",
                return_value={"locked_uact": 0, "deployments": 0, "by_deployment": {}},
            ),
            patch.dict("os.environ", environ),
            patch.object(cs.time, "sleep", lambda s: None),
        ):
            rc = cs.run(execute=execute, now=NOW)
        return rc, client

    def test_a_protected_dseq_is_not_closed_even_when_stale(self, capsys):
        stale = _dseq(30 * 86400)
        with patch.object(cs, "PROTECTED_DSEQS", frozenset({stale})):
            _, client = self._run(stale, ["backtest"])
        client.close_deployment.assert_not_called()

    def test_the_same_deployment_UNPROTECTED_is_closed(self):
        """⛔ THE ANTI-VACUITY PARTNER, AND THE ONE THAT MATTERS. Without it, a sweep that
        closed nothing at all would satisfy the test above — and "protects everything" is a
        failure mode indistinguishable from "protects the right thing" until a real leak sits
        there forever."""
        stale = _dseq(30 * 86400)
        with patch.object(cs, "PROTECTED_DSEQS", frozenset()):
            _, client = self._run(stale, ["backtest"])
        client.close_deployment.assert_called_once_with(stale)

    def test_the_protection_is_printed_never_silent(self, capsys):
        """A deployment skipped without a word is indistinguishable from one that was not
        there, which is how an over-broad allowlist hides a real leak forever."""
        stale = _dseq(30 * 86400)
        with patch.object(cs, "PROTECTED_DSEQS", frozenset({stale})):
            self._run(stale, ["backtest"])
        out = capsys.readouterr().out
        assert "PROTECTED-DSEQ" in out
        assert stale in out
        assert "PROTECTED (never-close list): 1" in out

    def test_protection_does_not_swallow_the_verdict(self, capsys):
        """The real classification must still be reported. Replacing STALE-e2e with silence
        would make the sweep unable to show that a leak-shaped thing was found and spared."""
        stale = _dseq(30 * 86400)
        with patch.object(cs, "PROTECTED_DSEQS", frozenset({stale})):
            self._run(stale, ["backtest"])
        assert "STALE-e2e" in capsys.readouterr().out

    def test_a_protected_dseq_that_is_NOT_stale_changes_nothing(self):
        """Protection is a veto on closing, not a reclassification."""
        fresh = _dseq(60)
        with patch.object(cs, "PROTECTED_DSEQS", frozenset({fresh})):
            _, client = self._run(fresh, ["app"])
        client.close_deployment.assert_not_called()

    def test_the_default_list_carries_the_research_deployment(self):
        """The default is not empty. An allowlist nobody populated protects nothing, and this
        one encodes an incident that already happened four times."""
        assert "1784532174413" in cs.PROTECTED_DSEQS

    def test_the_list_is_env_overridable(self, monkeypatch):
        monkeypatch.setenv("PROTECTED_DSEQS", "111, 222 ,333")
        import importlib

        reloaded = importlib.reload(cs)
        try:
            assert reloaded.PROTECTED_DSEQS == frozenset({"111", "222", "333"})
        finally:
            monkeypatch.delenv("PROTECTED_DSEQS", raising=False)
            importlib.reload(cs)
