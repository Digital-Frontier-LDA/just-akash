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


def _mock_client(deployments: dict[str, dict]):
    client = MagicMock()
    client.account_address.return_value = "akash1me"
    client.list_deployments.return_value = [
        {"deployment": {"state": "active", "id": {"dseq": d}}} for d in deployments
    ]
    client.get_deployment.side_effect = lambda d: deployments[str(d)]
    return client


def _run(client, execute: bool) -> int:
    with (
        patch.object(cs, "AkashConsoleAPI", return_value=client),
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
