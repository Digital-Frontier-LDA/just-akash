"""Unit tests for the pure logic in just_akash.smoke_providers.

The smoke test itself is a live script (deploys real leases), so these pin only
the parts that can regress without touching the network: how deploy output is
classified, and how each feature check reads a subprocess result.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

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
