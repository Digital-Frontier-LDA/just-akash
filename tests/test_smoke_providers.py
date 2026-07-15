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


class _FakeClock:
    """time.monotonic() stub that advances a fixed step on each call.

    First call returns 0, then step, 2*step, ... so a loop bounded on a small
    cap_s terminates deterministically without real sleeping.
    """

    def __init__(self, step: float):
        self.t = -step
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


class TestServiceReadinessSignal:
    """The core fix: gate on the service actually SERVING, not lease 'ready'."""

    def _dep(self, services: dict, dep_state="active", lease_state="active"):
        return {
            "deployment": {"state": dep_state},
            "leases": [{"state": lease_state, "status": {"services": services}}],
        }

    # ── _service_availability ────────────────────────────────────────
    def test_availability_reads_ready_replicas(self):
        dep = self._dep({"app": {"ready_replicas": 1, "available": 0}})
        with patch.object(sp, "_api") as api:
            api.return_value.get_deployment.return_value = dep
            assert sp._service_availability("1") == (1, 1)

    def test_availability_zero_when_not_serving(self):
        dep = self._dep({"app": {"available": 0, "ready_replicas": 0}})
        with patch.object(sp, "_api") as api:
            api.return_value.get_deployment.return_value = dep
            assert sp._service_availability("1") == (0, 1)

    def test_availability_none_when_no_service_reported(self):
        # provider hasn't reported services yet -> "keep waiting", never "ready".
        with patch.object(sp, "_api") as api:
            api.return_value.get_deployment.return_value = {"leases": [{"status": {}}]}
            assert sp._service_availability("1") is None

    def test_availability_survives_malformed_status(self):
        with patch.object(sp, "_api") as api:
            api.return_value.get_deployment.return_value = {"leases": [{"status": "starting"}]}
            assert sp._service_availability("1") is None

    # ── _deployment_dead ─────────────────────────────────────────────
    def test_dead_true_for_closed_lease(self):
        with patch.object(sp, "_api") as api:
            api.return_value.get_deployment.return_value = self._dep({}, lease_state="closed")
            assert sp._deployment_dead("1") is True

    def test_dead_false_for_active(self):
        with patch.object(sp, "_api") as api:
            api.return_value.get_deployment.return_value = self._dep({"app": {"available": 0}})
            assert sp._deployment_dead("1") is False

    def test_dead_false_on_read_error(self):
        # A transient API error is not "dead" -> we keep waiting, don't fail fast.
        with patch.object(sp, "_api") as api:
            api.return_value.get_deployment.side_effect = RuntimeError("boom")
            assert sp._deployment_dead("1") is False

    # ── _wait_ready ──────────────────────────────────────────────────
    def test_wait_ready_true_when_service_available(self):
        with (
            patch.object(sp, "_dead_state", return_value=None),
            patch.object(sp, "_service_availability", return_value=(1, 1)),
            patch.object(sp.time, "sleep"),
            patch.object(sp.time, "monotonic", _FakeClock(1)),
        ):
            assert sp._wait_ready("1", cap_s=60) is True

    def test_wait_ready_fails_fast_on_terminal_state(self):
        with (
            patch.object(sp, "_dead_state", return_value="failed"),
            patch.object(sp, "_service_availability", return_value=None),
            patch.object(sp.time, "sleep"),
            patch.object(sp.time, "monotonic", _FakeClock(1)),
        ):
            assert sp._wait_ready("1", cap_s=600) is False

    def test_wait_ready_exec_fallback_when_availability_unreported(self):
        # Provider never populates availability, but a lease-shell exec works ->
        # the container IS running, so we must call it ready.
        with (
            patch.object(sp, "_dead_state", return_value=None),
            patch.object(sp, "_service_availability", return_value=None),
            patch.object(sp, "_run", return_value=_completed(stdout="ready\n")) as run,
            patch.object(sp.time, "sleep"),
            patch.object(sp.time, "monotonic", _FakeClock(30)),
        ):
            assert sp._wait_ready("1", cap_s=100) is True
            assert run.called  # the exec fallback fired

    def test_wait_ready_false_after_cap_when_never_serving(self):
        with (
            patch.object(sp, "_dead_state", return_value=None),
            patch.object(sp, "_service_availability", return_value=(0, 1)),
            patch.object(sp, "_run", return_value=_completed(stdout="", returncode=1)),
            patch.object(sp.time, "sleep"),
            patch.object(sp.time, "monotonic", _FakeClock(30)),
        ):
            assert sp._wait_ready("1", cap_s=100) is False


class TestIngressCap:
    def test_ingress_passes_when_marker_served(self):
        with (
            patch.object(sp, "_fetch", return_value=f"x{sp.INGRESS_BASELINE}y"),
            patch.object(sp.time, "sleep"),
            patch.object(sp.time, "monotonic", _FakeClock(1)),
        ):
            assert sp._check_ingress("1", "uri", cap_s=60) is True

    def test_ingress_fails_after_cap_on_503(self):
        # Route live but backend unregistered (503/404) -> not reachable within cap.
        with (
            patch.object(sp, "_fetch", return_value="503 Service Temporarily Unavailable"),
            patch.object(sp.time, "sleep"),
            patch.object(sp.time, "monotonic", _FakeClock(20)),
        ):
            assert sp._check_ingress("1", "uri", cap_s=100) is False

    def test_ingress_fails_after_cap_on_connection_error(self):
        with (
            patch.object(sp, "_fetch", side_effect=OSError("refused")),
            patch.object(sp.time, "sleep"),
            patch.object(sp.time, "monotonic", _FakeClock(20)),
        ):
            assert sp._check_ingress("1", "uri", cap_s=100) is False


class TestStreamCheck:
    def test_stream_fails_on_nonzero_exit(self):
        with patch.object(sp, "_run", return_value=_completed(stdout="line\n", returncode=2)):
            assert sp._check_stream("1", "logs") is False

    def test_stream_passes_when_output_is_readable(self):
        # exit-0 AND real output — the whole point after the JSON-frame fix.
        with patch.object(
            sp, "_run", return_value=_completed(stdout="[svc] hello\n2 events\n", returncode=0)
        ):
            assert sp._check_stream("1", "events") is True

    def test_stream_fails_when_output_is_empty(self):
        # Clean exit but NO readable lines == a blind stream (frames discarded);
        # must not read as PASS.
        with patch.object(sp, "_run", return_value=_completed(stdout="   \n\n", returncode=0)):
            assert sp._check_stream("1", "logs") is False


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

    def test_survives_malformed_lease_shapes(self):
        # Malformed provider responses must not raise -- they just mean "no
        # ingress yet". Covers a non-dict `status` (string, then list), a
        # non-dict lease, and a non-dict `services`.
        for dep in (
            {"leases": [{"status": "starting"}]},  # status is a string
            {"leases": [{"status": ["x"]}]},  # status is a list
            {"leases": ["not-a-dict"]},  # the lease itself is not a dict
            {"leases": [{"status": {"services": "nope"}}]},  # services is not a dict
        ):
            with patch.object(sp, "_api") as api:
                api.return_value.get_deployment.return_value = dep
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
            # Control the clock so the generous cap is reached in a few iterations
            # instead of busy-spinning for the real INGRESS_CAP_S seconds.
            patch.object(sp.time, "monotonic", _FakeClock(20)),
        ):
            assert sp._check_ingress("1", "uri", cap_s=100) is False


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


class TestUpdateDiagnostics:
    """Slow-vs-stuck classification on an update-cutover timeout (quorum-designed).

    The verdict stays FAIL; these only add evidence so the failure self-explains and
    a later cap change is data-driven — never turning a genuine defect green.
    """

    def test_classify_served_new(self):
        assert sp._classify_served("x probe-updated-abc y", "probe-updated-abc") == "new"

    def test_classify_served_old(self):
        assert sp._classify_served("stale content", "probe-updated-abc") == "old"

    def test_classify_served_none(self):
        assert sp._classify_served("   ", "tok") == "none"

    def test_classify_served_unreachable(self):
        assert sp._classify_served(None, "tok") == "unreachable"

    def test_in_pod_marker_new(self):
        with patch.object(sp, "_run", return_value=_completed(stdout="probe-updated-xyz\n")):
            assert sp._probe_in_pod_marker("123456", "probe-updated-xyz") == "new"

    def test_in_pod_marker_old(self):
        with patch.object(sp, "_run", return_value=_completed(stdout="probe-initial\n")):
            assert sp._probe_in_pod_marker("123456", "probe-updated-xyz") == "old"

    def test_in_pod_marker_unreachable_on_rc(self):
        with patch.object(sp, "_run", return_value=_completed(returncode=1)):
            assert sp._probe_in_pod_marker("123456", "tok") == "unreachable"

    def test_in_pod_marker_unreachable_on_raise(self):
        with patch.object(sp, "_run", side_effect=RuntimeError("boom")):
            assert sp._probe_in_pod_marker("123456", "tok") == "unreachable"

    def test_in_pod_marker_unreachable_on_empty_stdout(self):
        """rc=0 with empty/whitespace stdout is the cold-stdout race, not real 'old'."""
        with patch.object(sp, "_run", return_value=_completed(stdout="  \n")):
            assert sp._probe_in_pod_marker("123456", "tok") == "unreachable"

    def test_observe_after_cap_arrived(self):
        calls = {"n": 0}

        def probe():
            calls["n"] += 1
            return calls["n"] >= 2  # arrives on the 2nd poll

        with (
            patch.object(sp.time, "monotonic", _FakeClock(1)),
            patch.object(sp.time, "sleep"),
        ):
            eventual, after = sp._observe_after_cap(probe, window_s=60)
        assert eventual == "arrived"
        assert after is not None

    def test_observe_after_cap_never(self):
        with (
            patch.object(sp.time, "monotonic", _FakeClock(20)),
            patch.object(sp.time, "sleep"),
        ):
            assert sp._observe_after_cap(lambda: False, window_s=30) == ("never", None)

    def test_observe_after_cap_zero_window(self):
        assert sp._observe_after_cap(lambda: True, window_s=0) == ("never", None)

    def test_observe_after_cap_swallows_raising_probe(self):
        def boom():
            raise OSError("unreachable")

        with (
            patch.object(sp.time, "monotonic", _FakeClock(20)),
            patch.object(sp.time, "sleep"),
        ):
            assert sp._observe_after_cap(boom, window_s=30) == ("never", None)

    def test_record_update_timeout_slow(self):
        diag: dict = {}
        with (
            patch.object(sp, "_service_availability", return_value=(1, 1)),
            patch.object(sp, "_probe_in_pod_marker", return_value="new"),
            patch.object(sp, "_observe_after_cap", return_value=("arrived", 42)),
        ):
            sp._record_update_timeout("123456", "uri", "tok", "stale body", diag)
        assert diag["eventual"] == "arrived"
        assert diag["eventual_after_s"] == 42
        assert diag["in_pod_marker"] == "new"
        assert diag["body_at_timeout"] == "old"
        assert diag["service_at_timeout"] == "1/1"
        assert diag["fail_cap_s"] == int(sp.INGRESS_CAP_S)

    def test_record_update_timeout_stuck(self):
        diag: dict = {}
        with (
            patch.object(sp, "_service_availability", return_value=None),
            patch.object(sp, "_probe_in_pod_marker", return_value="new"),
            patch.object(sp, "_observe_after_cap", return_value=("never", None)),
        ):
            sp._record_update_timeout("123456", "uri", "tok", None, diag)
        assert diag["eventual"] == "never"
        assert diag["body_at_timeout"] == "unreachable"
        assert diag["service_at_timeout"] is None

    def test_record_update_timeout_isolates_raising_service_probe(self):
        """A raising _service_availability must NOT abort the rest of the
        classification — the pod + eventual-ingress evidence is still recorded."""
        diag: dict = {}
        with (
            patch.object(sp, "_service_availability", side_effect=RuntimeError("boom")),
            patch.object(sp, "_probe_in_pod_marker", return_value="new"),
            patch.object(sp, "_observe_after_cap", return_value=("arrived", 10)),
        ):
            sp._record_update_timeout("123456", "uri", "tok", "body", diag)
        assert diag["service_at_timeout"] is None  # probe raised -> recorded as unknown
        assert diag["in_pod_marker"] == "new"  # but the rest still captured
        assert diag["eventual"] == "arrived"

    def test_observe_after_cap_sleep_bounded_to_window(self):
        """The poll sleep must never overshoot a short window by a full interval."""
        slept: list = []
        with (
            patch.object(sp.time, "monotonic", _FakeClock(1)),
            patch.object(sp.time, "sleep", side_effect=lambda s: slept.append(s)),
        ):
            sp._observe_after_cap(lambda: False, window_s=3)
        assert slept  # it polled at least once
        # every poll is clamped to [0, 6] — never negative, never over the window
        assert all(0.0 <= s <= 6.0 for s in slept)

    def test_check_update_timeout_records_diag_and_stays_fail(self):
        diag: dict = {}
        with (
            patch.object(sp, "_run", return_value=_completed(returncode=0)),
            patch.object(sp, "_fetch", return_value="no-token-here"),
            patch.object(sp.time, "monotonic", _FakeClock(200)),  # blow past INGRESS_CAP
            patch.object(sp.time, "sleep"),
            patch.object(sp, "_record_update_timeout") as rec,
        ):
            ok = sp._check_update("123456", "/sdl", "uri", diag=diag)
        assert ok is False
        rec.assert_called_once()
        assert rec.call_args.args[4] is diag  # diag threaded to the recorder

    def test_check_update_command_fail_sets_fail_mode(self):
        diag: dict = {}
        with patch.object(sp, "_run", return_value=_completed(returncode=1, stderr="nope")):
            assert sp._check_update("123456", "/sdl", "uri", diag=diag) is False
        assert diag["fail_mode"] == "update_command"

    def test_provider_records_includes_diag_on_fail(self):
        results = {"update": "FAIL", "ready": "PASS"}
        diagnostics = {"update": {"eventual": "never", "in_pod_marker": "new"}}
        recs = sp._provider_records("prov", "123", results, {}, diagnostics)
        by = {r["feature"]: r for r in recs}
        assert by["update"]["diag"] == {"eventual": "never", "in_pod_marker": "new"}
        assert "diag" not in by["ready"]  # a passing feature carries no diag

    def test_provider_records_omits_empty_diag(self):
        recs = sp._provider_records("prov", "123", {"update": "PASS"}, {}, {"update": {}})
        by = {r["feature"]: r for r in recs}
        assert "diag" not in by["update"]

    def test_provider_records_omits_diag_when_not_failed(self):
        """diag is failure evidence — never attach it to a non-FAIL, even if populated."""
        results = {"update": "PASS"}
        diagnostics = {"update": {"eventual": "arrived"}}  # non-empty but PASS
        recs = sp._provider_records("prov", "123", results, {}, diagnostics)
        by = {r["feature"]: r for r in recs}
        assert "diag" not in by["update"]

    # --- readiness + ingress timeout diagnostics (Phase 1b) ---

    def test_availability_ready_true(self):
        with patch.object(sp, "_service_availability", return_value=(1, 1)):
            assert sp._availability_ready("123456") is True

    def test_availability_ready_false_when_zero(self):
        with patch.object(sp, "_service_availability", return_value=(0, 1)):
            assert sp._availability_ready("123456") is False

    def test_availability_ready_false_on_raise(self):
        with patch.object(sp, "_service_availability", side_effect=RuntimeError("x")):
            assert sp._availability_ready("123456") is False

    def test_exec_works_true(self):
        with patch.object(sp, "_run", return_value=_completed(stdout="ready\n")):
            assert sp._exec_works("123456") is True

    def test_exec_works_false_on_empty_stdout(self):
        # rc=0 but empty stdout is the cold-stdout race, not a live container
        with patch.object(sp, "_run", return_value=_completed(stdout="  \n")):
            assert sp._exec_works("123456") is False

    def test_exec_works_false_on_raise(self):
        with patch.object(sp, "_run", side_effect=RuntimeError("x")):
            assert sp._exec_works("123456") is False

    def test_record_ready_timeout_slow(self):
        diag: dict = {}
        with (
            patch.object(sp, "_deployment_dead", return_value=False),
            patch.object(sp, "_service_availability", return_value=(0, 1)),
            patch.object(sp, "_exec_works", return_value=False),
            patch.object(sp, "_observe_after_cap", return_value=("arrived", 30)),
        ):
            sp._record_ready_timeout("123456", diag)
        assert diag["eventual"] == "arrived"
        assert diag["eventual_after_s"] == 30
        assert diag["dead_at_timeout"] is False
        assert diag["service_at_timeout"] == "0/1"
        assert diag["exec_at_timeout"] == "unreachable"
        assert diag["fail_cap_s"] == int(sp.READY_CAP_S)

    def test_record_ready_timeout_exec_up_but_availability_unreported(self):
        """No availability, but exec works => container IS up; record eventual=arrived."""
        diag: dict = {}
        with (
            patch.object(sp, "_deployment_dead", return_value=False),
            patch.object(sp, "_service_availability", return_value=None),
            patch.object(sp, "_exec_works", return_value=True),
            patch.object(sp, "_observe_after_cap", return_value=("never", None)),
        ):
            sp._record_ready_timeout("123456", diag)
        assert diag["exec_at_timeout"] == "ok"
        assert diag["eventual"] == "arrived"  # exec proves it came up

    def test_record_ready_timeout_reports_passed_cap(self):
        """fail_cap_s must reflect the cap the check actually ran with, not the default."""
        diag: dict = {}
        with (
            patch.object(sp, "_deployment_dead", return_value=False),
            patch.object(sp, "_service_availability", return_value=(0, 1)),
            patch.object(sp, "_exec_works", return_value=False),
            patch.object(sp, "_observe_after_cap", return_value=("never", None)),
        ):
            sp._record_ready_timeout("123456", diag, cap_s=42)
        assert diag["fail_cap_s"] == 42

    def test_record_ingress_timeout_reports_passed_cap(self):
        diag: dict = {}
        with (
            patch.object(sp, "_service_availability", return_value=(1, 1)),
            patch.object(sp, "_observe_after_cap", return_value=("never", None)),
        ):
            sp._record_ingress_timeout("123456", "uri", "err", diag, cap_s=55)
        assert diag["fail_cap_s"] == 55

    def test_record_ready_timeout_isolates_raising_probes(self):
        diag: dict = {}
        with (
            patch.object(sp, "_deployment_dead", side_effect=RuntimeError("x")),
            patch.object(sp, "_service_availability", side_effect=RuntimeError("x")),
            patch.object(sp, "_exec_works", return_value=False),
            patch.object(sp, "_observe_after_cap", return_value=("never", None)),
        ):
            sp._record_ready_timeout("123456", diag)  # must not raise
        assert diag["dead_at_timeout"] is False
        assert diag["service_at_timeout"] is None
        assert diag["eventual"] == "never"

    def test_wait_ready_timeout_records_diag(self):
        diag: dict = {}
        with (
            patch.object(sp.time, "monotonic", _FakeClock(300)),  # blow past READY_CAP
            patch.object(sp.time, "sleep"),
            patch.object(sp, "_deployment_dead", return_value=False),
            patch.object(sp, "_service_availability", return_value=None),
            patch.object(sp, "_record_ready_timeout") as rec,
        ):
            assert sp._wait_ready("123456", diag=diag) is False
        rec.assert_called_once()

    def test_wait_ready_exec_probe_raise_does_not_abort(self):
        """A subprocess timeout/OSError in the exec fallback must be swallowed (via
        _exec_works), not abort the readiness wait + its diagnostics."""
        with (
            patch.object(sp.time, "monotonic", _FakeClock(40)),
            patch.object(sp.time, "sleep"),
            patch.object(sp, "_dead_state", return_value=None),
            patch.object(sp, "_service_availability", return_value=None),
            patch.object(sp, "_run", side_effect=subprocess.TimeoutExpired("cmd", 25)),
            patch.object(sp, "_record_ready_timeout"),
        ):
            assert sp._wait_ready("1", cap_s=100) is False  # must not raise

    def test_record_ingress_timeout_stuck(self):
        diag: dict = {}
        with (
            patch.object(sp, "_service_availability", return_value=(1, 1)),
            patch.object(sp, "_observe_after_cap", return_value=("never", None)),
        ):
            sp._record_ingress_timeout("123456", "uri", "404 Not Found", diag)
        assert diag["eventual"] == "never"
        assert diag["service_at_timeout"] == "1/1"
        assert diag["last_at_timeout"] == "404 Not Found"
        assert diag["fail_cap_s"] == int(sp.INGRESS_CAP_S)

    def test_check_ingress_timeout_records_diag(self):
        diag: dict = {}
        with (
            patch.object(sp, "_fetch", return_value="wrong-content"),
            patch.object(sp.time, "monotonic", _FakeClock(200)),
            patch.object(sp.time, "sleep"),
            patch.object(sp, "_record_ingress_timeout") as rec,
        ):
            assert sp._check_ingress("123456", "uri", diag=diag) is False
        rec.assert_called_once()

    def test_check_ingress_tolerates_fetch_valueerror(self):
        """A malformed URI raising ValueError in-loop must not abort — poll to timeout."""
        with (
            patch.object(sp, "_fetch", side_effect=ValueError("bad uri")),
            patch.object(sp.time, "monotonic", _FakeClock(100)),  # one poll, then cap
            patch.object(sp.time, "sleep"),
            patch.object(sp, "_record_ingress_timeout") as rec,
        ):
            assert sp._check_ingress("123456", "uri", diag={}) is False
        rec.assert_called_once()  # reached the classifier, didn't propagate

    def test_check_update_tolerates_fetch_valueerror(self):
        """A malformed URI raising ValueError must not abort — keep polling to timeout."""
        with (
            patch.object(sp, "_run", return_value=_completed(returncode=0)),
            patch.object(sp, "_fetch", side_effect=ValueError("bad uri")),
            patch.object(sp.time, "monotonic", _FakeClock(100)),  # one poll, then cap
            patch.object(sp.time, "sleep"),
            patch.object(sp, "_record_update_timeout") as rec,
        ):
            ok = sp._check_update("123456", "/sdl", "uri", diag={})
        assert ok is False
        rec.assert_called_once()  # reached the timeout classifier, didn't propagate


class TestTelemetry:
    def test_provider_records_one_per_feature(self):
        results = {"deploy": "PASS", "status": "PASS", "ready": "PASS"}
        latencies = {"deploy": 4200, "status": 120, "ready": 41000}
        recs = sp._provider_records("prov", "123", results, latencies)
        assert len(recs) == len(sp._TELEMETRY_FEATURES)
        by = {r["feature"]: r for r in recs}
        assert by["deploy"] == {
            "provider": "prov",
            "feature": "deploy",
            "outcome": "PASS",
            "latency_ms": 4200,
            "dseq": "123",
        }
        assert by["ready"]["latency_ms"] == 41000
        # a feature never reached -> outcome "-" and latency None
        assert by["ingress"]["outcome"] == "-"
        assert by["ingress"]["latency_ms"] is None

    def test_write_telemetry_appends_jsonl_and_creates_parent(self, tmp_path):
        import json as j

        target = tmp_path / "sub" / "telemetry.jsonl"  # parent dir created
        path = str(target)
        rec = {
            "provider": "p",
            "feature": "deploy",
            "outcome": "PASS",
            "latency_ms": 10,
            "dseq": "1",
        }
        sp._write_telemetry(path, "2026-07-14T00:00:00+00:00", "9.9.9", [rec])
        sp._write_telemetry(path, "2026-07-14T01:00:00+00:00", "9.9.9", [rec])  # append
        lines = target.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2  # appended, not overwritten
        assert j.loads(lines[0]) == {
            "ts": "2026-07-14T00:00:00+00:00",
            "version": "9.9.9",
            **rec,
        }

    def test_write_telemetry_best_effort_on_error(self, tmp_path, capsys):
        # An unwritable path must NOT raise — telemetry can never break the run.
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file, not a dir")
        sp._write_telemetry(str(blocker / "telemetry.jsonl"), "t", "v", [{"a": 1}])
        assert "telemetry write failed" in capsys.readouterr().out

    def test_smoke_provider_emits_records_on_nobid(self):
        records: list = []
        with (
            patch.object(sp, "_deploy", return_value=(None, "no-bid")),
            patch.object(sp, "install_signal_cleanup"),
            patch.object(sp, "robust_destroy"),
        ):
            sp.smoke_provider("prov", "/sdl", "/key", records=records)
        by = {r["feature"]: r for r in records}
        assert len(records) == len(sp._TELEMETRY_FEATURES)
        assert by["deploy"]["outcome"] == "NO-BID"
        assert by["deploy"]["latency_ms"] is not None  # deploy was timed
        assert by["status"]["outcome"] == "-"  # never reached
        assert by["status"]["latency_ms"] is None


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

    def test_service_names_survives_malformed_status(self):
        # A non-dict status/lease must not raise (it would abort the best-effort
        # sweep); it reads as "no services".
        assert sp._deployment_service_names({"leases": [{"status": "starting"}]}) == set()
        assert sp._deployment_service_names({"leases": [{"status": ["x"]}]}) == set()
        assert sp._deployment_service_names({"leases": ["not-a-dict"]}) == set()
        assert (
            sp._deployment_service_names({"leases": [{"status": {"services": "nope"}}]}) == set()
        )

    def test_not_found_error_matches_only_the_404_prefix(self):
        assert sp._is_not_found_error(RuntimeError("API Error (404): Deployment not found"))
        # A non-404 error whose body merely mentions (404) must NOT be "gone".
        assert not sp._is_not_found_error(RuntimeError("API Error (500): see ticket (404)"))
        assert not sp._is_not_found_error(RuntimeError("Connection error: timed out"))

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

    def test_min_age_zero_reaps_a_fresh_probe(self):
        # The end-of-job cleanup (--min-age 0) must reap THIS run's own fresh
        # probe, which the default 1h floor would spare. Safe because CI
        # serializes runs, so no concurrent probe exists to clobber.
        assert sp._is_orphan_probe(
            self._detail(["probe"]), self._dseq_aged(5), min_age_seconds=0, now=self.NOW
        )
        # ...but still only a probe -- a fresh runner workload is never reaped.
        assert not sp._is_orphan_probe(
            self._detail(["runner"]), self._dseq_aged(5), min_age_seconds=0, now=self.NOW
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

    def test_sweep_reports_incomplete_when_inspection_fails(self, capsys):
        # A genuine (non-404) inspection failure must flag the sweep INCOMPLETE,
        # never an all-clear -- a leak could hide behind the deployment we could
        # not inspect.
        errored = self._dseq_aged(7200)
        api = MagicMock()
        api.list_deployments.return_value = [{"dseq": errored}]
        api.get_deployment.side_effect = RuntimeError("API Error (500): server boom")
        with (
            patch.object(sp, "_api", return_value=api),
            patch.object(sp, "robust_destroy", return_value=True),
            patch.object(sp.time, "time", return_value=self.NOW),
        ):
            assert sp.sweep_orphan_probes() == []
        out = capsys.readouterr().out
        assert "INCOMPLETE" in out
        assert "no leaked probes found among inspected deployments" in out

    def test_sweep_treats_404_as_gone_not_incomplete(self, capsys):
        # A 404 means the deployment is already gone -> not a leak; the sweep is
        # still complete and reports a clean all-clear.
        gone = self._dseq_aged(7200)
        api = MagicMock()
        api.list_deployments.return_value = [{"dseq": gone}]
        api.get_deployment.side_effect = RuntimeError("API Error (404): Deployment not found")
        with (
            patch.object(sp, "_api", return_value=api),
            patch.object(sp.time, "time", return_value=self.NOW),
        ):
            assert sp.sweep_orphan_probes() == []
        out = capsys.readouterr().out
        assert "INCOMPLETE" not in out
        assert "no leaked probes found" in out

    def test_sweep_skips_a_deployment_it_cannot_inspect(self):
        good = self._dseq_aged(7200)
        bad = self._dseq_aged(7300)
        api = MagicMock()
        api.list_deployments.return_value = [{"dseq": bad}, {"dseq": good}]

        def _get(dseq):
            if dseq == bad:
                raise RuntimeError("API Error (404): gone")
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


class TestProviderRoomPreflight:
    """Pre-deploy capacity check: skip an offline/full provider (NO-ROOM), and
    fail OPEN so a stats hiccup never skips a healthy one."""

    def _prov(self, cpu=2_000_000, mem=10**13, sto=10**14, online=True):
        return {
            "isOnline": online,
            "stats": {
                "cpu": {"available": cpu},
                "memory": {"available": mem},
                "storage": {"ephemeral": {"available": sto}},
            },
        }

    def test_room_ok_with_capacity(self):
        with patch.object(sp, "_api") as api:
            api.return_value.get_provider.return_value = self._prov()
            assert sp._provider_room("p")[0] is True

    def test_no_room_when_offline(self):
        with patch.object(sp, "_api") as api:
            api.return_value.get_provider.return_value = self._prov(online=False)
            ok, reason = sp._provider_room("p")
            assert ok is False and "offline" in reason

    def test_no_room_insufficient_cpu(self):
        with patch.object(sp, "_api") as api:
            api.return_value.get_provider.return_value = self._prov(cpu=500)  # < 1000 milli
            ok, reason = sp._provider_room("p")
            assert ok is False and "cpu" in reason

    def test_no_room_insufficient_memory(self):
        with patch.object(sp, "_api") as api:
            api.return_value.get_provider.return_value = self._prov(mem=1000)
            ok, reason = sp._provider_room("p")
            assert ok is False and "memory" in reason

    def test_fail_open_when_no_stats(self):
        with patch.object(sp, "_api") as api:
            api.return_value.get_provider.return_value = {"isOnline": True}
            assert sp._provider_room("p")[0] is True

    def test_fail_open_when_not_in_registry(self):
        with patch.object(sp, "_api") as api:
            api.return_value.get_provider.return_value = None
            assert sp._provider_room("p")[0] is True

    def test_fail_open_on_api_error(self):
        with patch.object(sp, "_api") as api:
            api.return_value.get_provider.side_effect = RuntimeError("boom")
            assert sp._provider_room("p")[0] is True


class TestDeployCreditAndSkip:
    def test_deploy_402_is_no_credit(self):
        ref: dict = {"dseq": None}
        with patch.object(
            sp, "_run", return_value=_completed(stdout="API Error (402): Insufficient balance")
        ):
            dseq, note = sp._deploy("sdl", "p", ref)
        assert dseq is None and note == "no-credit"

    def test_smoke_provider_no_room_skips_without_deploying(self):
        with (
            patch.object(sp, "_provider_room", return_value=(False, "provider reports offline")),
            patch.object(sp, "install_signal_cleanup"),
            patch.object(sp, "robust_destroy"),
            patch.object(sp, "_deploy") as dep,
        ):
            res = sp.smoke_provider("p", "/sdl", "/key")
        assert res["deploy"] == "NO-ROOM"
        dep.assert_not_called()  # never spent a deploy on a full provider

    def test_smoke_provider_no_credit(self):
        with (
            patch.object(sp, "_provider_room", return_value=(True, "ok")),
            patch.object(sp, "_deploy", return_value=(None, "no-credit")),
            patch.object(sp, "install_signal_cleanup"),
            patch.object(sp, "robust_destroy"),
        ):
            res = sp.smoke_provider("p", "/sdl", "/key")
        assert res["deploy"] == "NO-CREDIT"

    def test_no_room_and_no_credit_are_not_failures(self):
        # The overall verdict trips only on "FAIL"; these skips must not.
        for status in ("NO-ROOM", "NO-CREDIT", "NO-BID"):
            row = dict.fromkeys(sp.FEATURES, "-")
            row["deploy"] = status
            assert not any(v == "FAIL" for v in row.values())


class TestFailureDiagnostics:
    """On a failure, the run must auto-dump events/logs so an intermittent
    problem (hgulk6's occasional 'lease never ready') self-documents."""

    def test_capture_dumps_status_events_logs(self, capsys):
        with (
            patch.object(sp, "_status_json", return_value={"status": "ready"}),
            patch.object(sp, "_service_availability", return_value=(0, 1)),
            patch.object(
                sp,
                "_run",
                return_value=_completed(
                    stdout="Normal Scheduled ...\nWarning FailedScheduling ...\n"
                ),
            ),
        ):
            sp._capture_diagnostics("123", "lease never became ready")
        out = capsys.readouterr().out
        assert "diagnostics: lease never became ready" in out
        assert "FailedScheduling" in out  # the kube events (the payoff) are shown

    def test_capture_never_raises_on_stream_error(self, capsys):
        with (
            patch.object(sp, "_status_json", side_effect=RuntimeError("x")),
            patch.object(sp, "_run", side_effect=RuntimeError("stream down")),
            patch.object(sp, "_service_availability", return_value=None),
        ):
            sp._capture_diagnostics("123", "reason")  # must not raise
        assert "capture failed" in capsys.readouterr().out

    def test_capture_surfaces_stderr_on_nonzero_stream(self, capsys):
        # A stream command that errors (rc!=0, detail on stderr) must show WHY,
        # not a bare "(no output)" that hides the failure.
        with (
            patch.object(sp, "_status_json", return_value={"status": "x"}),
            patch.object(sp, "_service_availability", return_value=None),
            patch.object(
                sp,
                "_run",
                return_value=_completed(
                    stdout="", stderr="Error: provider unreachable", returncode=1
                ),
            ),
        ):
            sp._capture_diagnostics("123", "reason")
        out = capsys.readouterr().out
        assert "stream errored: rc=1" in out and "provider unreachable" in out

    def test_capture_surfaces_error_even_with_partial_stdout(self, capsys):
        # Some lines AND a non-zero exit -> BOTH the lines and the error show.
        with (
            patch.object(sp, "_status_json", return_value={"status": "x"}),
            patch.object(sp, "_service_availability", return_value=None),
            patch.object(
                sp,
                "_run",
                return_value=_completed(
                    stdout="Normal Scheduled ...\n", stderr="boom", returncode=1
                ),
            ),
        ):
            sp._capture_diagnostics("123", "reason")
        out = capsys.readouterr().out
        assert "Scheduled" in out and "stream errored: rc=1" in out and "boom" in out

    def test_readiness_failure_captures_diagnostics(self):
        with (
            patch.object(sp, "_provider_room", return_value=(True, "ok")),
            patch.object(sp, "_deploy", return_value=("123", "ok")),
            patch.object(sp, "_wait_ready", return_value=False),
            patch.object(sp, "_capture_diagnostics") as cap,
            patch.object(sp, "install_signal_cleanup"),
            patch.object(sp, "robust_destroy"),
        ):
            res = sp.smoke_provider("p", "/sdl", "/key")
        assert res["deploy"] == "PASS" and res["status"] == "FAIL"  # readiness cascade
        cap.assert_called_once()
        assert "never became ready" in cap.call_args.args[1]

    def test_diagnostics_captured_only_once_across_feature_fails(self):
        # Multiple feature FAILs must still dump diagnostics only once.
        with (
            patch.object(sp, "_provider_room", return_value=(True, "ok")),
            patch.object(sp, "_deploy", return_value=("123", "ok")),
            patch.object(sp, "_wait_ready", return_value=True),
            patch.object(sp, "_wait_exec_ready", return_value=True),
            patch.object(sp, "_wait_ssh_ready", return_value=True),
            patch.object(sp, "_ingress_uri", return_value="u"),
            patch.object(sp, "_check_status", return_value=False),  # FAIL
            patch.object(sp, "_check_exec", return_value=False),  # FAIL
            patch.object(sp, "_check_inject", return_value=True),
            patch.object(sp, "_check_stream", return_value=True),
            patch.object(sp, "_check_ssh", return_value=True),
            patch.object(sp, "_check_connect", return_value=True),
            patch.object(sp, "_check_ingress", return_value=True),
            patch.object(sp, "_check_update", return_value=True),
            patch.object(sp, "_capture_diagnostics") as cap,
            patch.object(sp, "install_signal_cleanup"),
            patch.object(sp, "robust_destroy"),
        ):
            sp.smoke_provider("p", "/sdl", "/key")
        cap.assert_called_once()  # first FAIL only

    def test_diagnostics_on_ssh_never_ready(self):
        with (
            patch.object(sp, "_provider_room", return_value=(True, "ok")),
            patch.object(sp, "_deploy", return_value=("123", "ok")),
            patch.object(sp, "_wait_ready", return_value=True),
            patch.object(sp, "_wait_exec_ready", return_value=True),
            patch.object(sp, "_check_status", return_value=True),
            patch.object(sp, "_check_exec", return_value=True),
            patch.object(sp, "_check_inject", return_value=True),
            patch.object(sp, "_check_stream", return_value=True),
            patch.object(sp, "_wait_ssh_ready", return_value=False),  # sshd never up
            patch.object(sp, "_ingress_uri", return_value="u"),
            patch.object(sp, "_check_ingress", return_value=True),
            patch.object(sp, "_check_update", return_value=True),
            patch.object(sp, "_capture_diagnostics") as cap,
            patch.object(sp, "install_signal_cleanup"),
            patch.object(sp, "robust_destroy"),
        ):
            res = sp.smoke_provider("p", "/sdl", "/key")
        assert res["ssh"] == "FAIL"
        cap.assert_called_once()
        assert "ssh" in cap.call_args.args[1].lower()

    def test_diagnostics_on_no_ingress_uri(self):
        with (
            patch.object(sp, "_provider_room", return_value=(True, "ok")),
            patch.object(sp, "_deploy", return_value=("123", "ok")),
            patch.object(sp, "_wait_ready", return_value=True),
            patch.object(sp, "_wait_exec_ready", return_value=True),
            patch.object(sp, "_check_status", return_value=True),
            patch.object(sp, "_check_exec", return_value=True),
            patch.object(sp, "_check_inject", return_value=True),
            patch.object(sp, "_check_stream", return_value=True),
            patch.object(sp, "_wait_ssh_ready", return_value=True),
            patch.object(sp, "_check_ssh", return_value=True),
            patch.object(sp, "_check_connect", return_value=True),
            patch.object(sp, "_ingress_uri", return_value=None),  # no ingress URI
            patch.object(sp, "_capture_diagnostics") as cap,
            patch.object(sp, "install_signal_cleanup"),
            patch.object(sp, "robust_destroy"),
        ):
            res = sp.smoke_provider("p", "/sdl", "/key")
        assert res["ingress"] == "FAIL"
        cap.assert_called_once()
        assert "ingress" in cap.call_args.args[1].lower()


class TestLeaseDown:
    """LEASE-DOWN: a provider that accepted the bid but the lease then died on-chain
    (terminal state failed/closed). Fast-fail + a distinct FAILING outcome (quorum)."""

    def test_deployment_dead_recognizes_failed(self):
        with patch.object(sp, "_api") as api:
            api.return_value.get_deployment.return_value = {
                "deployment": {"state": "failed"},
                "leases": [],
            }
            assert sp._deployment_dead("123") is True

    def test_deployment_dead_recognizes_closed(self):
        with patch.object(sp, "_api") as api:
            api.return_value.get_deployment.return_value = {
                "deployment": {"state": "closed"},
                "leases": [],
            }
            assert sp._deployment_dead("123") is True

    def test_deployment_dead_false_when_active(self):
        with patch.object(sp, "_api") as api:
            api.return_value.get_deployment.return_value = {
                "deployment": {"state": "active"},
                "leases": [{"state": "active"}],
            }
            assert sp._deployment_dead("123") is False

    def test_wait_ready_flags_lease_down_on_terminal_state(self):
        diag: dict = {}
        with (
            patch.object(sp.time, "monotonic", _FakeClock(1)),
            patch.object(sp.time, "sleep"),
            patch.object(sp, "_dead_state", return_value="failed"),
        ):
            assert sp._wait_ready("1", cap_s=100, diag=diag) is False
        assert diag.get("fail_kind") == "lease-down"
        assert diag.get("terminal_state") == "failed"

    def test_wait_ready_insufficient_funds_is_not_lease_down(self):
        """Escrow exhaustion is OUR funding issue — dead, but NOT a LEASE-DOWN."""
        diag: dict = {}
        with (
            patch.object(sp.time, "monotonic", _FakeClock(1)),
            patch.object(sp.time, "sleep"),
            patch.object(sp, "_dead_state", return_value="insufficient_funds"),
        ):
            assert sp._wait_ready("1", cap_s=100, diag=diag) is False
        assert diag.get("terminal_state") == "insufficient_funds"
        assert diag.get("fail_kind") != "lease-down"

    def test_smoke_provider_lease_down_marks_cells_distinctly(self):
        def fake_wait_ready(*a, **kw):
            d = kw.get("diag")
            if d is not None:
                d["fail_kind"] = "lease-down"
            return False

        with (
            patch.object(sp, "_provider_room", return_value=(True, "ok")),
            patch.object(sp, "_deploy", return_value=("123", "ok")),
            patch.object(sp, "_wait_ready", side_effect=fake_wait_ready),
            patch.object(sp, "_capture_diagnostics"),
            patch.object(sp, "install_signal_cleanup"),
            patch.object(sp, "robust_destroy"),
        ):
            res = sp.smoke_provider("p", "/sdl", "/key")
        assert res["ready"] == sp.LEASE_DOWN
        assert res["deploy"] == "PASS"
        assert all(res[f] == sp.LEASE_DOWN for f in sp.FEATURES if f != "deploy")

    def test_smoke_provider_plain_ready_fail_stays_fail(self):
        """A readiness timeout with no terminal state is a plain FAIL, not LEASE-DOWN."""
        with (
            patch.object(sp, "_provider_room", return_value=(True, "ok")),
            patch.object(sp, "_deploy", return_value=("123", "ok")),
            patch.object(sp, "_wait_ready", return_value=False),  # no fail_kind set
            patch.object(sp, "_capture_diagnostics"),
            patch.object(sp, "install_signal_cleanup"),
            patch.object(sp, "robust_destroy"),
        ):
            res = sp.smoke_provider("p", "/sdl", "/key")
        assert res["ready"] == "FAIL"
        assert res["exec"] == "FAIL"
        assert sp.LEASE_DOWN not in res.values()

    def test_lease_down_is_a_failing_outcome_not_a_skip(self):
        assert sp.LEASE_DOWN in sp._FAILING_OUTCOMES
        assert sp.LEASE_DOWN not in ("-", "NO-BID", "NO-ROOM", "NO-CREDIT")

    def test_lease_down_diag_attaches_to_telemetry(self):
        # a LEASE-DOWN cell with diag must carry it (gate is _FAILING_OUTCOMES now)
        recs = sp._provider_records(
            "prov", "123", {"ready": sp.LEASE_DOWN}, {}, {"ready": {"fail_kind": "lease-down"}}
        )
        by = {r["feature"]: r for r in recs}
        assert by["ready"]["diag"] == {"fail_kind": "lease-down"}
