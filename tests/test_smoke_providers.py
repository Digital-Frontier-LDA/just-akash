"""Unit tests for the pure logic in just_akash.smoke_providers.

The smoke test itself is a live script (deploys real leases), so these pin only
the parts that can regress without touching the network: how deploy output is
classified, and how each feature check reads a subprocess result.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from just_akash import smoke_providers as sp


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["x"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestDeployClassification:
    def test_parses_dseq_from_output(self):
        ref: dict = {"dseq": None}
        with patch.object(
            sp, "_run", return_value=_completed("Deployment Summary:\n  DSEQ: 123456\n")
        ):
            dseq, note = sp._deploy("sdl", "akash1prov", ref)
        assert dseq == "123456"
        assert note == "ok"
        assert ref["dseq"] == "123456"  # registered for signal cleanup

    def test_dseq_equals_form(self):
        ref: dict = {"dseq": None}
        with patch.object(
            sp, "_run", return_value=_completed("DSEQ=987 provider=akash1x price=5")
        ):
            dseq, _ = sp._deploy("sdl", "p", ref)
        assert dseq == "987"

    def test_no_bid_is_not_a_deploy_failure(self):
        ref: dict = {"dseq": None}
        with patch.object(
            sp, "_run", return_value=_completed("Received 6 bid(s) but NONE from our providers.")
        ):
            dseq, note = sp._deploy("sdl", "p", ref)
        assert dseq is None
        assert note == "no-bid"

    def test_unparsable_output_is_deploy_failed(self):
        ref: dict = {"dseq": None}
        with patch.object(sp, "_run", return_value=_completed("boom", returncode=1)):
            dseq, note = sp._deploy("sdl", "p", ref)
        assert dseq is None
        assert note == "deploy-failed"


class TestExecCheck:
    def test_exec_requires_both_zero_rc_and_token_in_stdout(self):
        # The whole reason this check exists: a cold container can return rc=0 with
        # EMPTY stdout, which must count as FAIL, not PASS.
        with patch.object(sp, "_run", return_value=_completed(stdout="", returncode=0)):
            assert sp._check_exec("123456") is False

    def test_exec_passes_when_token_echoed(self):
        with patch.object(sp, "_run", return_value=_completed(stdout="smoke-123456-ok\n")):
            assert sp._check_exec("123456") is True


class TestExecReadyGate:
    def test_gate_requires_output_not_just_success(self):
        """rc=0 with empty stdout must NOT satisfy the readiness gate."""
        with (
            patch.object(sp, "_run", return_value=_completed(stdout="", returncode=0)),
            patch.object(sp.time, "sleep"),
        ):
            assert sp._wait_exec_ready("1", attempts=2, interval=0) is False

    def test_gate_passes_once_marker_round_trips(self):
        with patch.object(sp, "_run", return_value=_completed(stdout="exec-ready-probe\n")):
            assert sp._wait_exec_ready("1", attempts=2, interval=0) is True


class TestStreamCheck:
    def test_stream_fails_on_nonzero_exit(self):
        with patch.object(sp, "_run", return_value=_completed(returncode=2)):
            assert sp._check_stream("1", "logs") is False

    def test_stream_passes_on_clean_bounded_return(self):
        with patch.object(sp, "_run", return_value=_completed(returncode=0)):
            assert sp._check_stream("1", "events") is True


class TestSshInfo:
    def test_returns_host_port_when_forwarded(self):
        with patch.object(
            sp, "_status_json", return_value={"ssh_host": "p.example", "ssh_port": 30699}
        ):
            assert sp._ssh_info("1") == ("p.example", 30699)

    def test_none_when_no_forwarded_ssh_port(self):
        with patch.object(sp, "_status_json", return_value={"status": "ready"}):
            assert sp._ssh_info("1") is None


class TestIngressUri:
    def test_extracts_first_service_uri(self):
        dep = {"leases": [{"status": {"services": {"probe": {"uris": ["abc.ingress.example"]}}}}]}
        with patch.object(sp, "_api") as api:
            api.return_value.get_deployment.return_value = dep
            assert sp._ingress_uri("1") == "abc.ingress.example"

    def test_none_when_no_uris(self):
        with patch.object(sp, "_api") as api:
            api.return_value.get_deployment.return_value = {"leases": [{"status": {}}]}
            assert sp._ingress_uri("1") is None


class TestSshCheck:
    def test_ssh_fails_when_exec_has_no_output(self):
        with patch.object(sp, "_run", return_value=_completed(stdout="", returncode=0)):
            assert sp._check_ssh("123456", "/k") is False

    def test_ssh_passes_when_exec_and_inject_succeed(self):
        # first call: ssh exec (SSH_OK); then inject + readback inside _inject_and_read
        outs = [
            _completed(stdout="SSH_OK\n"),  # exec
            _completed(returncode=0),  # inject
            _completed(stdout="SMOKE_SECRET=injected_ok\n"),  # readback
        ]
        with patch.object(sp, "_run", side_effect=outs):
            assert sp._check_ssh("123456", "/k") is True


class TestConnectCheck:
    def test_connect_passes_when_marker_echoed(self):
        done = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="CONNECT_123456\n", stderr=""
        )
        with patch.object(sp.subprocess, "run", return_value=done):
            assert sp._check_connect("123456", "/k") is True

    def test_connect_fails_on_timeout(self):
        with patch.object(sp.subprocess, "run", side_effect=subprocess.TimeoutExpired("c", 1)):
            assert sp._check_connect("123456", "/k") is False

    def test_connect_fails_when_session_errored_despite_marker(self):
        # Marker echoed but the session exited non-zero — must not count as PASS.
        done = subprocess.CompletedProcess(
            args=[], returncode=255, stdout="CONNECT_123456\n", stderr="broken pipe"
        )
        with patch.object(sp.subprocess, "run", return_value=done):
            assert sp._check_connect("123456", "/k") is False


class TestIngressCheck:
    def test_ingress_passes_when_baseline_served(self):
        with patch.object(sp, "_fetch", return_value=sp.INGRESS_BASELINE):
            assert sp._check_ingress("1", "uri") is True

    def test_ingress_fails_when_never_served(self):
        with (
            patch.object(sp, "_fetch", side_effect=OSError("refused")),
            patch.object(sp.time, "sleep"),
        ):
            assert sp._check_ingress("1", "uri") is False


class TestUpdateCheck:
    def test_update_fails_if_command_errors(self):
        with patch.object(sp, "_run", return_value=_completed(returncode=1)):
            assert sp._check_update("123456", "/sdl", "uri") is False

    def test_update_passes_when_new_marker_appears_at_ingress(self):
        with (
            patch.object(sp, "_run", return_value=_completed(returncode=0)),
            patch.object(sp, "_fetch", return_value="probe-updated-123456"),
        ):
            assert sp._check_update("123456", "/sdl", "uri") is True


class TestOrphanProbeSweep:
    """The startup sweep that reaps probes leaked by a hard-killed prior run.

    Because a real live account holds the user's own workloads (runner/train),
    the sweep must reap ONLY unambiguous probes and must never mis-identify a
    concurrent run's live probe or a real workload. These pin that contract.
    """

    NOW = 1_783_930_000.0  # a fixed "now" (~mid-2026, ms-epoch dseq era)

    def _detail(self, service_names):
        """A get_deployment() detail whose lease reports these service names."""
        return {"leases": [{"status": {"services": {n: {"name": n} for n in service_names}}}]}

    def _dseq_aged(self, seconds_old: float) -> str:
        """A ms-epoch dseq for a deployment created `seconds_old` before NOW."""
        return str(int((self.NOW - seconds_old) * 1000))

    # ── service-name extraction ──────────────────────────────────────
    def test_service_names_from_lease_status(self):
        assert sp._deployment_service_names(self._detail(["probe"])) == {"probe"}

    def test_service_names_empty_when_provider_reports_none(self):
        # Provider down / not yet reporting -> empty, must read as "unknown".
        assert sp._deployment_service_names({"leases": [{"status": {}}]}) == set()
        assert sp._deployment_service_names({}) == set()

    # ── age derived from the ms-epoch dseq ───────────────────────────
    def test_age_from_ms_dseq(self):
        assert sp._probe_age_seconds(self._dseq_aged(3600), now=self.NOW) == 3600

    def test_age_rejects_block_height_dseq(self):
        # A legacy block-height dseq must not be mis-read as a 1970s timestamp.
        assert sp._probe_age_seconds("27695426", now=self.NOW) is None

    def test_age_rejects_garbage(self):
        assert sp._probe_age_seconds("not-a-number", now=self.NOW) is None
        assert sp._probe_age_seconds(None, now=self.NOW) is None

    def test_age_rejects_future_dseq(self):
        future = str(int((self.NOW + 2 * 86_400) * 1000))
        assert sp._probe_age_seconds(future, now=self.NOW) is None

    # ── orphan classification (the safety-critical predicate) ────────
    def test_old_probe_only_service_is_an_orphan(self):
        assert sp._is_orphan_probe(
            self._detail(["probe"]), self._dseq_aged(7200), min_age_seconds=3600, now=self.NOW
        )

    def test_runner_workload_is_never_an_orphan(self):
        assert not sp._is_orphan_probe(
            self._detail(["runner"]), self._dseq_aged(7200), min_age_seconds=3600, now=self.NOW
        )

    def test_probe_plus_another_service_is_not_reaped(self):
        # Exactly {probe} required -- anything extra means "not our probe".
        assert not sp._is_orphan_probe(
            self._detail(["probe", "web"]),
            self._dseq_aged(7200),
            min_age_seconds=3600,
            now=self.NOW,
        )

    def test_young_probe_is_spared(self):
        # Could belong to a concurrent run -- must not be clobbered.
        assert not sp._is_orphan_probe(
            self._detail(["probe"]), self._dseq_aged(60), min_age_seconds=3600, now=self.NOW
        )

    def test_probe_with_unknown_age_is_spared(self):
        # probe service but un-datable dseq -> fail safe, do not reap.
        assert not sp._is_orphan_probe(
            self._detail(["probe"]), "27695426", min_age_seconds=3600, now=self.NOW
        )

    # ── the sweep itself ─────────────────────────────────────────────
    def _fake_api(self, deployments, details):
        api = MagicMock()
        api.list_deployments.return_value = deployments
        api.get_deployment.side_effect = lambda dseq: details[dseq]
        return api

    def test_sweep_reaps_only_the_old_probe(self):
        old_probe = self._dseq_aged(7200)  # reap
        young_probe = self._dseq_aged(60)  # spare (concurrent run)
        runner = self._dseq_aged(9000)  # spare (real workload)
        api = self._fake_api(
            [{"dseq": old_probe}, {"dseq": young_probe}, {"dseq": runner}],
            {
                old_probe: self._detail(["probe"]),
                young_probe: self._detail(["probe"]),
                runner: self._detail(["runner"]),
            },
        )
        with (
            patch.object(sp, "_api", return_value=api),
            patch.object(sp, "robust_destroy", return_value=True) as rd,
            patch.object(sp.time, "time", return_value=self.NOW),
        ):
            swept = sp.sweep_orphan_probes()
        assert swept == [old_probe]
        rd.assert_called_once_with(old_probe)

    def test_sweep_dry_run_destroys_nothing(self, capsys):
        old_probe = self._dseq_aged(7200)
        api = self._fake_api([{"dseq": old_probe}], {old_probe: self._detail(["probe"])})
        with (
            patch.object(sp, "_api", return_value=api),
            patch.object(sp, "robust_destroy", return_value=True) as rd,
            patch.object(sp.time, "time", return_value=self.NOW),
        ):
            swept = sp.sweep_orphan_probes(dry_run=True)
        assert swept == [old_probe]
        rd.assert_not_called()
        # Dry-run output must not claim a probe was reaped when nothing ran.
        out = capsys.readouterr().out
        assert "dry-run" in out
        assert "; reaping" not in out  # the past/active-tense form is destroy-only

    def test_sweep_survives_a_list_failure(self):
        api = MagicMock()
        api.list_deployments.side_effect = RuntimeError("api down")
        with patch.object(sp, "_api", return_value=api):
            assert sp.sweep_orphan_probes() == []

    def test_sweep_skips_a_deployment_it_cannot_inspect(self):
        good = self._dseq_aged(7200)
        bad = self._dseq_aged(7300)
        api = MagicMock()
        api.list_deployments.return_value = [{"dseq": bad}, {"dseq": good}]

        def _get(dseq):
            if dseq == bad:
                raise RuntimeError("detail fetch failed")
            return self._detail(["probe"])

        api.get_deployment.side_effect = _get
        with (
            patch.object(sp, "_api", return_value=api),
            patch.object(sp, "robust_destroy", return_value=True),
            patch.object(sp.time, "time", return_value=self.NOW),
        ):
            assert sp.sweep_orphan_probes() == [good]

    def test_sweep_counts_only_confirmed_destroys(self, capsys):
        # robust_destroy returning False (still listed) must NOT count as reaped,
        # and must NOT be reported as "no leaked probes found": an orphan was
        # detected but is still draining escrow, so it needs a human.
        old_probe = self._dseq_aged(7200)
        api = self._fake_api([{"dseq": old_probe}], {old_probe: self._detail(["probe"])})
        with (
            patch.object(sp, "_api", return_value=api),
            patch.object(sp, "robust_destroy", return_value=False),
            patch.object(sp.time, "time", return_value=self.NOW),
        ):
            assert sp.sweep_orphan_probes() == []
        out = capsys.readouterr().out
        assert "no leaked probes found" not in out
        assert "could NOT be destroyed" in out
        assert old_probe in out
