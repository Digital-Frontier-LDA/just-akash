"""Unit tests for the pure logic in just_akash.smoke_providers.

The smoke test itself is a live script (deploys real leases), so these pin only
the parts that can regress without touching the network: how deploy output is
classified, and how each feature check reads a subprocess result.
"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

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


class TestDeployMisreportsRegressions:
    """Two proven bugs, both of which made a HEALTHY provider look broken.

    Fixtures are transcripts of real measured runs, not paraphrases.
    """

    # Abridged from an actual `just-akash deploy` pinned to a non-bidding provider:
    # exit 1, but the DSEQ is printed at create time, long before bidding.
    REAL_NO_BID = (
        "[2026-07-17T11:56:29Z] Deployment created  DSEQ=1784289527633  manifest_len=812\n"
        "[2026-07-17T11:56:31Z] STEP 3: Polling for bids...\n"
        "[2026-07-17T11:59:11Z] NO BID FROM 1 allowlisted provider(s):\n"
        "[2026-07-17T11:59:12Z] Cleaning up deployment 1784289527633 (foreign bids only)...\n"
        "[2026-07-17T11:59:13Z] Deployment 1784289527633 closed after no bids received\n"
    )

    def test_no_bid_that_printed_a_dseq_is_no_bid_not_ok(self):
        """The bug: the DSEQ regex short-circuited every note below it, so a no-bid
        returned "ok". The smoke then polled a deployment deploy had already closed,
        read state=closed, and reported a provider LEASE-DOWN that never happened."""
        ref: dict = {"dseq": None}
        with patch.object(sp, "_run", return_value=_completed(self.REAL_NO_BID, returncode=1)):
            dseq, note = sp._deploy("sdl", "p", ref)
        assert note == "no-bid"
        assert dseq is None, "a closed no-bid deployment must never be handed back as a lease"

    def test_no_bid_still_records_the_dseq_for_cleanup(self):
        """deploy's own close is best-effort and can fail, so cleanup must still be
        able to reach the deployment even though the deploy failed."""
        ref: dict = {"dseq": None}
        with patch.object(sp, "_run", return_value=_completed(self.REAL_NO_BID, returncode=1)):
            sp._deploy("sdl", "p", ref)
        assert ref["dseq"] == "1784289527633"

    def test_exit_code_is_what_decides_success_not_the_printed_dseq(self):
        """Same output, only the exit code differs — that alone must flip the note."""
        ref: dict = {"dseq": None}
        with patch.object(sp, "_run", return_value=_completed(self.REAL_NO_BID, returncode=0)):
            _, ok_note = sp._deploy("sdl", "p", ref)
        with patch.object(sp, "_run", return_value=_completed(self.REAL_NO_BID, returncode=1)):
            _, bad_note = sp._deploy("sdl", "p", ref)
        assert (ok_note, bad_note) == ("ok", "no-bid")

    # The issue-#19 stale-bid path: deploy CLOSES the original order and re-creates a
    # new one, so the transcript carries two dseqs. The last is the live lease.
    REDEPLOY = (
        "[12:00:00Z] Deployment created  DSEQ=1111111111111  manifest_len=812\n"
        "[12:02:10Z] Lease attempt 1/3 hit a stale bid: re-fetching open bids...\n"
        "[12:02:11Z]   Stale order 1111111111111 closed\n"
        "[12:02:12Z]   Re-deployed: new order DSEQ=2222222222222 — fast-polling...\n"
        "[12:03:01Z] Lease created successfully!\n"
        "[12:03:01Z] DEPLOYMENT SUMMARY  DSEQ=2222222222222  provider=akashX price=10 uakt\n"
        "Deployment Summary:\n  DSEQ: 2222222222222\n"
    )

    def test_redeploy_returns_the_live_dseq_not_the_closed_original(self):
        """The bug: re.search took the FIRST dseq — the one deploy had just closed.
        The smoke then tested a dead deployment (every feature LEASE-DOWN) while the
        real lease ran on unattended, draining escrow."""
        ref: dict = {"dseq": None}
        with patch.object(sp, "_run", return_value=_completed(self.REDEPLOY, returncode=0)):
            dseq, note = sp._deploy("sdl", "p", ref)
        assert note == "ok"
        assert dseq == "2222222222222", "must return the LIVE lease, not the closed original"

    def test_redeploy_points_cleanup_at_the_live_lease(self):
        """The escrow-safety half: cleanup must target the dseq that is actually up."""
        ref: dict = {"dseq": None}
        with patch.object(sp, "_run", return_value=_completed(self.REDEPLOY, returncode=0)):
            sp._deploy("sdl", "p", ref)
        assert ref["dseq"] == "2222222222222", "cleanup aimed at the closed dseq leaks escrow"


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


class TestInjectReadbackRetry:
    """The inject readback is a lease-shell exec, so it can hit the cold-stdout race
    (rc=0 + empty stdout) even though the write succeeded. It retries ONLY that
    signature — a nonzero rc or wrong content is a real failure and never retried,
    so a genuine inject regression is never masked."""

    def test_passes_on_first_read(self):
        outs = [
            _completed(returncode=0),  # inject write
            _completed(stdout="SMOKE_SECRET=injected_ok\n"),  # readback: good
        ]
        with patch.object(sp, "_run", side_effect=outs) as run:
            assert sp._check_inject("123456") is True
        assert run.call_count == 2  # write + one readback, no retry

    def test_retries_past_a_cold_stdout_race_then_passes(self):
        outs = [
            _completed(returncode=0),  # inject write
            _completed(stdout="", returncode=0),  # readback: cold-stdout race (empty)
            _completed(stdout="SMOKE_SECRET=injected_ok\n"),  # readback: good on retry
        ]
        with (
            patch.object(sp, "_run", side_effect=outs) as run,
            patch.object(sp.time, "sleep"),
        ):
            assert sp._check_inject("123456") is True
        assert run.call_count == 3  # write + empty + good

    def test_fails_after_all_attempts_stay_empty(self):
        # write, then _INJECT_READBACK_ATTEMPTS empty readbacks
        outs = [_completed(returncode=0)] + [
            _completed(stdout="", returncode=0) for _ in range(sp._INJECT_READBACK_ATTEMPTS)
        ]
        with (
            patch.object(sp, "_run", side_effect=outs) as run,
            patch.object(sp.time, "sleep"),
        ):
            assert sp._check_inject("123456") is False
        # write + exactly _INJECT_READBACK_ATTEMPTS readbacks (bounded, no runaway)
        assert run.call_count == 1 + sp._INJECT_READBACK_ATTEMPTS

    def test_does_not_retry_a_nonzero_readback(self):
        # A real exec failure (rc!=0) is NOT the race — fail on the first read.
        outs = [
            _completed(returncode=0),  # inject write
            _completed(stdout="injected_ok\n", returncode=1),  # readback errored
        ]
        with patch.object(sp, "_run", side_effect=outs) as run:
            assert sp._check_inject("123456") is False
        assert run.call_count == 2  # no retry on a genuine failure

    def test_does_not_retry_wrong_content(self):
        # rc=0 with non-empty-but-wrong content means the file is wrong — a real
        # inject defect, not the race. Must fail immediately, not retry.
        outs = [
            _completed(returncode=0),  # inject write
            _completed(stdout="SMOKE_SECRET=tampered\n", returncode=0),  # wrong content
        ]
        with patch.object(sp, "_run", side_effect=outs) as run:
            assert sp._check_inject("123456") is False
        assert run.call_count == 2  # no retry — wrong content is a real failure

    def test_inject_write_failure_short_circuits(self):
        # If the write itself fails, never attempt a readback.
        with patch.object(sp, "_run", side_effect=[_completed(returncode=1)]) as run:
            assert sp._check_inject("123456") is False
        assert run.call_count == 1  # write only, no readback


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

    def test_provider_records_frame_shape_rides_exec_record(self):
        """frame_shape rides the exec record (pass or fail) and nowhere else."""
        results = {"exec": "PASS", "status": "PASS"}
        recs = sp._provider_records("prov", "123", results, {}, frame_shape="stdout,result")
        by = {r["feature"]: r for r in recs}
        assert by["exec"]["frame_shape"] == "stdout,result"
        assert "frame_shape" not in by["status"]  # never on a non-exec feature

    def test_provider_records_omits_frame_shape_when_absent(self):
        recs = sp._provider_records("prov", "123", {"exec": "PASS"}, {})
        assert "frame_shape" not in {r["feature"]: r for r in recs}["exec"]

    def test_frame_shape_parses_trace_line(self):
        line = (
            "[lease-shell] FRAME-TRACE shape=[result] stdout_bytes=0 "
            "recovered=0 t_result=0.042s frames=[(102, 16, 0.042)]"
        )
        assert sp._frame_trace_line("noise\n" + line + "\nmore") == line
        assert sp._frame_shape(line) == "result"
        assert sp._frame_shape(None) is None
        assert sp._frame_trace_line("no trace here") is None
        # a line that merely CONTAINS the token but lacks the stable prefix must NOT match
        assert sp._frame_trace_line("a wrapper mentions FRAME-TRACE loosely") is None

    def test_death_cause_graceful_on_dying_line(self):
        logs = [
            "PROBE-HB ts=100 up=5 mem=1/2 mpsi=avg10=0.0 cpsi=avg10=0.0 thr=0",
            "PROBE-DYING signal=TERM ts=105",  # the LAST thing the probe emitted
        ]
        out = sp._death_cause(logs, lease_down=True)
        assert out is not None and "GRACEFUL" in out and "signal=TERM" in out

    def test_death_cause_hard_kill_when_lease_down_and_only_heartbeats(self):
        logs = [
            "PROBE-HB ts=100 up=5 mem=1/2 mpsi=avg10=0.0",
            "PROBE-HB ts=105 up=10 mem=2/2 mpsi=avg10=42.0",  # pressure climbing
        ]
        out = sp._death_cause(logs, lease_down=True)
        assert out is not None
        assert "NO termination signal" in out and "hard kill" in out
        assert "ts=105" in out  # pins to the LAST heartbeat

    def test_death_cause_none_when_lease_up_feature_flake(self):
        """Heartbeats on a LIVE container (lease not down) are liveness, not a kill."""
        logs = ["PROBE-HB ts=100 up=5 mem=1/2", "PROBE-HB ts=105 up=10 mem=1/2"]
        assert sp._death_cause(logs, lease_down=False) is None

    def test_death_cause_graceful_wins_even_if_lease_up(self):
        """A dying line is unambiguous death evidence regardless of the down flag."""
        logs = ["PROBE-HB ts=100 up=5", "PROBE-DYING signal=TERM ts=106"]
        assert "GRACEFUL" in (sp._death_cause(logs, lease_down=False) or "")

    def test_death_cause_stale_dying_before_newer_heartbeats_is_not_graceful(self):
        """A PROBE-DYING followed by newer heartbeats = a restart, not the final state."""
        logs = ["PROBE-DYING signal=TERM ts=100", "PROBE-HB ts=110 up=2"]
        # lease up -> None (alive); lease down -> hard-kill on the newer heartbeat
        assert sp._death_cause(logs, lease_down=False) is None
        assert "hard kill" in (sp._death_cause(logs, lease_down=True) or "")

    def test_death_cause_none_when_uninstrumented(self):
        assert sp._death_cause(["probe-http-up", "probe-container-up"], lease_down=True) is None
        assert sp._death_cause([], lease_down=True) is None

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


class TestQuarantine:
    """A quarantined provider's PROVIDER-RELIABILITY failures (LEASE-DOWN, proven
    ingress-stall) don't gate CI, but its TOOLING regressions still do (quorum)."""

    def test_quarantined_providers_parses_env(self):
        with patch.dict("os.environ", {"SMOKE_QUARANTINE_PROVIDERS": " p1 , p2 ,"}):
            assert sp._quarantined_providers() == {"p1", "p2"}

    def test_quarantined_providers_empty_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            assert sp._quarantined_providers() == set()

    def test_service_ready(self):
        assert sp._service_ready("1/1") is True
        assert sp._service_ready("2/3") is True
        assert sp._service_ready("0/1") is False
        assert sp._service_ready(None) is False
        assert sp._service_ready("bad") is False

    # --- _is_reliability_failure taxonomy ---
    def test_lease_down_is_reliability(self):
        assert sp._is_reliability_failure("ready", sp.LEASE_DOWN, None) is True

    def test_update_command_failure_is_tooling(self):
        diag = {"fail_mode": "update_command"}
        assert sp._is_reliability_failure("update", "FAIL", diag) is False

    def test_update_eventual_arrived_is_reliability(self):
        diag = {"eventual": "arrived", "eventual_after_s": 30}
        assert sp._is_reliability_failure("update", "FAIL", diag) is True

    def test_update_stale_pod_is_tooling(self):
        # in_pod_marker=old => the update never reached the pod = genuine-bug signature
        diag = {"eventual": "never", "in_pod_marker": "old", "service_at_timeout": "1/1"}
        assert sp._is_reliability_failure("update", "FAIL", diag) is False

    def test_update_new_pod_ingress_stall_is_reliability(self):
        diag = {"eventual": "never", "in_pod_marker": "new"}
        assert sp._is_reliability_failure("update", "FAIL", diag) is True

    def test_update_unreachable_with_healthy_service_is_reliability(self):
        diag = {"eventual": "never", "in_pod_marker": "unreachable", "service_at_timeout": "1/1"}
        assert sp._is_reliability_failure("update", "FAIL", diag) is True

    def test_update_unreachable_without_healthy_service_is_tooling(self):
        diag = {"eventual": "never", "in_pod_marker": "unreachable", "service_at_timeout": "0/1"}
        assert sp._is_reliability_failure("update", "FAIL", diag) is False

    def test_plain_feature_fail_is_tooling(self):
        # a feature breaking on a healthy lease is always a tooling regression
        assert sp._is_reliability_failure("exec", "FAIL", None) is False

    # --- _gating_providers gate integration ---

    def test_gate_demotes_quarantined_lease_down(self):
        prov = "akash1hgulk6"
        row = dict.fromkeys(sp.FEATURES, sp.LEASE_DOWN)
        row["deploy"] = "PASS"
        records = [
            {
                "provider": prov,
                "feature": f,
                "outcome": row[f],
                "diag": {"fail_kind": "lease-down"},
            }
            for f in sp.FEATURES
        ]
        failed = sp._gating_providers({prov: row}, records, {prov})
        assert failed == {}  # all LEASE-DOWN cells demoted -> provider does not gate

    def test_gate_still_fails_quarantined_tooling_regression(self):
        prov = "akash1hgulk6"
        row = dict.fromkeys(sp.FEATURES, "PASS")
        row["exec"] = "FAIL"  # a feature broke on a healthy lease = tooling regression
        records = [{"provider": prov, "feature": "exec", "outcome": "FAIL"}]
        failed = sp._gating_providers({prov: row}, records, {prov})
        assert failed == {prov: ["exec"]}  # tooling regression still gates

    def test_gate_demotes_quarantined_update_ingress_stall_but_gates_stale_update(self):
        prov = "akash1hgulk6"
        # update stall: new pod healthy, ingress never routed -> demoted
        row_stall = dict.fromkeys(sp.FEATURES, "PASS")
        row_stall["update"] = "FAIL"
        recs_stall = [
            {
                "provider": prov,
                "feature": "update",
                "outcome": "FAIL",
                "diag": {"eventual": "never", "in_pod_marker": "new"},
            }
        ]
        assert sp._gating_providers({prov: row_stall}, recs_stall, {prov}) == {}
        # stale update: pod on OLD env -> genuine-bug signature -> still gates
        recs_stale = [
            {
                "provider": prov,
                "feature": "update",
                "outcome": "FAIL",
                "diag": {"eventual": "never", "in_pod_marker": "old", "service_at_timeout": "1/1"},
            }
        ]
        assert sp._gating_providers({prov: row_stall}, recs_stale, {prov}) == {prov: ["update"]}

    def test_gate_single_lease_down_is_non_gating_even_unquarantined(self):
        # LEASE-DOWN is fleet-wide provider infra -> non-gating for ANY provider now
        prov = "akash1aaul"
        row = {**dict.fromkeys(sp.FEATURES, sp.LEASE_DOWN), "deploy": "PASS"}
        assert sp._gating_providers({prov: row}, [], set()) == {}

    def test_mass_lease_down_gates_when_all_providers_leased_down(self):
        # every tested provider (>=2) LEASE-DOWNed in one run = deterministic = our bug
        rows = {
            "provA": {**dict.fromkeys(sp.FEATURES, sp.LEASE_DOWN), "deploy": "PASS"},
            "provB": {**dict.fromkeys(sp.FEATURES, sp.LEASE_DOWN), "deploy": "PASS"},
        }
        assert sp._mass_lease_down(rows) is True
        assert set(sp._gating_providers(rows, [], set())) == {"provA", "provB"}

    def test_partial_fleet_lease_down_is_non_gating(self):
        # one provider down, another healthy -> not fleet-wide -> non-gating
        rows = {
            "provA": {**dict.fromkeys(sp.FEATURES, sp.LEASE_DOWN), "deploy": "PASS"},
            "provB": dict.fromkeys(sp.FEATURES, "PASS"),
        }
        assert sp._mass_lease_down(rows) is False
        assert sp._gating_providers(rows, [], set()) == {}

    def test_mass_lease_down_needs_two_providers(self):
        rows = {"solo": {**dict.fromkeys(sp.FEATURES, sp.LEASE_DOWN), "deploy": "PASS"}}
        assert sp._mass_lease_down(rows) is False  # a lone provider must not gate

    def test_mass_lease_down_ignores_no_bid_providers(self):
        # a provider that never got a lease (NO-BID) doesn't count toward "all"
        rows = {
            "provA": {**dict.fromkeys(sp.FEATURES, sp.LEASE_DOWN), "deploy": "PASS"},
            "provB": {**dict.fromkeys(sp.FEATURES, "-"), "deploy": "NO-BID"},
        }
        assert sp._mass_lease_down(rows) is False  # only 1 provider actually leased


class TestQuarantineMainGate:
    """End-to-end main(): the gate is GREEN when a quarantined provider's reliability
    fails (shown, not masked) and RED when a real tooling regression does — the
    definitive 'the CI gate no longer flakes on hgulk6' behavior."""

    def _fake_smoke(self, hgulk6, tooling=False):
        def _f(provider, sdl, key, records=None, bench_records=None):
            if provider == hgulk6 and not tooling:
                row = {**dict.fromkeys(sp.FEATURES, sp.LEASE_DOWN), "deploy": "PASS"}
                diag = {"fail_kind": "lease-down"}
            elif provider == hgulk6 and tooling:
                row = {**dict.fromkeys(sp.FEATURES, "PASS"), "exec": "FAIL"}
                diag = None
            else:
                row = dict.fromkeys(sp.FEATURES, "PASS")
                diag = None
            if records is not None:
                for f in sp._TELEMETRY_FEATURES:
                    rec = {"provider": provider, "feature": f, "outcome": row.get(f, "-")}
                    if row.get(f) in sp._FAILING_OUTCOMES and diag:
                        rec["diag"] = diag
                    records.append(rec)
            return row

        return _f

    def _run_main(self, hgulk6, aaul, tooling=False):
        argv = ["smoke", "--provider", hgulk6, "--provider", aaul, "--no-sweep"]
        with (
            patch.object(sp.sys, "argv", argv),
            patch.dict(
                "os.environ",
                {"AKASH_API_KEY": "k", "SMOKE_QUARANTINE_PROVIDERS": hgulk6},
            ),
            patch.object(sp, "smoke_provider", side_effect=self._fake_smoke(hgulk6, tooling)),
            patch.object(sp, "_generate_keypair", return_value="/tmp/smoke-k/id"),
            patch.object(sp, "install_signal_cleanup"),
            patch.object(sp.shutil, "rmtree"),
        ):
            return sp.main()

    def test_main_green_when_quarantined_lease_down(self, capsys):
        code = self._run_main("akash1hgulk6demo", "akash1aauldemo")
        out = capsys.readouterr().out
        assert code == 0  # GREEN — a quarantined provider's LEASE-DOWN did not gate
        assert "SMOKE TEST PASSED" in out
        assert "[NON-GATING]" in out  # shown, not masked
        assert "SMOKE TEST FAILED" not in out

    def test_main_red_when_quarantined_tooling_regression(self, capsys):
        code = self._run_main("akash1hgulk6demo", "akash1aauldemo", tooling=True)
        out = capsys.readouterr().out
        assert code == 1  # RED — a real tooling bug on a quarantined provider still gates
        assert "SMOKE TEST FAILED" in out

    def _run_main_rows(self, provider_rows, quarantine=""):
        """Drive main() with an explicit {provider: row} map and quarantine env."""

        def fake_smoke(provider, sdl, key, records=None, bench_records=None):
            row = provider_rows[provider]
            if records is not None:
                for f in sp._TELEMETRY_FEATURES:
                    records.append(
                        {"provider": provider, "feature": f, "outcome": row.get(f, "-")}
                    )
            return row

        argv = ["smoke"]
        for p in provider_rows:
            argv += ["--provider", p]
        argv += ["--no-sweep"]
        with (
            patch.object(sp.sys, "argv", argv),
            patch.dict(
                "os.environ",
                {"AKASH_API_KEY": "k", "SMOKE_QUARANTINE_PROVIDERS": quarantine},
            ),
            patch.object(sp, "smoke_provider", side_effect=fake_smoke),
            patch.object(sp, "_generate_keypair", return_value="/tmp/smoke-k/id"),
            patch.object(sp, "install_signal_cleanup"),
            patch.object(sp.shutil, "rmtree"),
        ):
            return sp.main()

    def test_main_green_on_single_lease_down_unquarantined(self, capsys):
        """The exact scenario that flaked CI: aaul (unquarantined) lease-downs, z9nr
        healthy → GREEN. LEASE-DOWN is provider infra, never gates a single provider."""
        rows = {
            "akash1aauldemo": {**dict.fromkeys(sp.FEATURES, sp.LEASE_DOWN), "deploy": "PASS"},
            "akash1z9nrdemo": dict.fromkeys(sp.FEATURES, "PASS"),
        }
        code = self._run_main_rows(rows)  # no quarantine at all
        out = capsys.readouterr().out
        assert code == 0
        assert "SMOKE TEST PASSED" in out
        assert "[NON-GATING]" in out  # shown, not masked
        assert "SMOKE TEST FAILED" not in out

    def test_main_red_on_mass_lease_down(self, capsys):
        """Every provider LEASE-DOWNs in one run = deterministic = likely OUR manifest
        bug → RED with the distinct mass-lease-down verdict line."""
        rows = {
            "akash1aauldemo": {**dict.fromkeys(sp.FEATURES, sp.LEASE_DOWN), "deploy": "PASS"},
            "akash1z9nrdemo": {**dict.fromkeys(sp.FEATURES, sp.LEASE_DOWN), "deploy": "PASS"},
        }
        code = self._run_main_rows(rows)
        out = capsys.readouterr().out
        assert code == 1
        assert "fleet-wide simultaneous LEASE-DOWN" in out


class TestNoBidPhrasingCoverage:
    """Every no-bid message deploy can actually emit must classify as no-bid.

    Caught in review (Copilot, PR #62): the regex was case-sensitive "NO BID|no bid",
    so "No bids received within Ns" — deploy's message when NOTHING bid at all —
    matched neither and fell through to deploy-failed, scoring a pure market
    condition as a provider FAIL. It classified correctly only by accident, via the
    co-occurring "(no bids)" cleanup log line.
    """

    def _note(self, out: str) -> str:
        with patch.object(sp, "_run", return_value=_completed(out, returncode=1)):
            return sp._deploy("sdl", "p", {"dseq": None})[1]

    def test_no_bids_received_at_all(self):
        """deploy.py: raise RuntimeError("No bids received within {n}s. ...")"""
        assert (
            self._note("No bids received within 180s. Your SDL may be unsatisfiable.") == "no-bid"
        )

    def test_no_bid_from_allowlisted_provider(self):
        assert self._note("NO BID FROM 1 allowlisted provider(s):") == "no-bid"

    def test_none_from_our_providers(self):
        assert self._note("Received 6 bid(s) but NONE from our providers.") == "no-bid"

    def test_foreign_bids_only(self):
        assert self._note("Cleaning up deployment 1 (foreign bids only)...") == "no-bid"

    def test_classification_does_not_depend_on_the_cleanup_log_line(self):
        """The raise message ALONE must be enough. Previously the verdict hung on
        deploy's incidental "Cleaning up deployment N (no bids)" log; reword that
        log and a market no-bid would silently become a provider FAIL."""
        assert self._note("No bids received within 180s.") == "no-bid"

    def test_malformed_bids_stay_deploy_failed(self):
        """ "No VALID bids ... all bid entries were malformed" is a data/API error,
        not a market condition — it must NOT be excused as a no-bid skip."""
        assert (
            self._note("No valid bids received — all bid entries were malformed.")
            == "deploy-failed"
        )


class TestBenchmarkPiggyback:
    """Quality grading rides the smoke's live lease AFTER the feature matrix — Step 3a
    of the provider quality build. Disabled unless SMOKE_BENCHMARK_FILE is set, and
    NON-GATING: a benchmark failure never touches the smoke's pass/fail."""

    def test_disabled_without_the_env_var(self, monkeypatch):
        monkeypatch.delenv("SMOKE_BENCHMARK_FILE", raising=False)
        assert sp._benchmark_provider("123", "prov") is None

    def test_parses_the_benchmark_json_and_stamps_trusted_fields(self, monkeypatch):
        monkeypatch.setenv("SMOKE_BENCHMARK_FILE", "/tmp/x.jsonl")
        out = 'noise\n{"cpu_eps": "900", "provider": "EVIL", "dseq": "0", "complete": true}\n'
        with patch.object(sp, "_run", return_value=_completed(out, returncode=0)):
            rec = sp._benchmark_provider("123", "prov")
        assert rec is not None
        assert rec["cpu_eps"] == "900"
        assert rec["provider"] == "prov"  # our value wins over the probe's
        assert rec["dseq"] == "123"

    def test_no_json_line_returns_none(self, monkeypatch):
        monkeypatch.setenv("SMOKE_BENCHMARK_FILE", "/tmp/x.jsonl")
        with patch.object(sp, "_run", return_value=_completed("only noise\n", returncode=0)):
            assert sp._benchmark_provider("123", "prov") is None

    def test_never_raises_even_if_benchmark_blows_up(self, monkeypatch):
        monkeypatch.setenv("SMOKE_BENCHMARK_FILE", "/tmp/x.jsonl")
        with patch.object(sp, "_run", side_effect=RuntimeError("boom")):
            assert sp._benchmark_provider("123", "prov") is None  # swallowed, non-gating

    def test_piggyback_runs_only_on_a_healthy_lease(self, monkeypatch):
        # A benchmark record is appended only when deploy+ready PASS. This pins the
        # sequencing guarantee: grading happens after (not instead of) the matrix.
        monkeypatch.setenv("SMOKE_BENCHMARK_FILE", "/tmp/x.jsonl")
        bench: list = []
        with (
            patch.object(sp, "_benchmark_provider", return_value={"cpu_eps": "900"}) as mock_b,
            patch.object(sp, "_deploy", return_value=(None, "no-bid")),  # never deploys
            patch.object(sp, "robust_destroy", return_value=True),
            patch.object(sp, "install_signal_cleanup"),
        ):
            sp.smoke_provider("prov", "/sdl", "/key", bench_records=bench)
        # no-bid → no healthy lease → benchmark never runs, bench_records stays empty
        assert mock_b.call_count == 0
        assert bench == []


class TestGenerateKeypairCleanup:
    """_generate_keypair creates an UNENCRYPTED ephemeral keypair, so a failure
    partway through must not leave that key on disk."""

    def test_keygen_failure_removes_keydir(self, monkeypatch, tmp_path):
        key_dir = tmp_path / "smoke-ssh-xxx"
        key_dir.mkdir()
        (key_dir / "id_ed25519").write_text("partial-unencrypted-key")  # half-generated

        monkeypatch.setattr(sp.tempfile, "mkdtemp", lambda **kw: str(key_dir))

        def _fail(*a, **kw):
            raise subprocess.CalledProcessError(1, "ssh-keygen")

        monkeypatch.setattr(sp.subprocess, "run", _fail)

        with pytest.raises(subprocess.CalledProcessError):
            sp._generate_keypair()
        # The half-generated key directory was cleaned up — no leaked key material.
        assert not key_dir.exists()

    def test_keygen_success_sets_pubkey_and_returns_path(self, monkeypatch, tmp_path):
        key_dir = tmp_path / "smoke-ssh-ok"
        key_dir.mkdir()
        monkeypatch.setattr(sp.tempfile, "mkdtemp", lambda **kw: str(key_dir))
        monkeypatch.delenv("SSH_PUBKEY", raising=False)

        def _ok(*a, **kw):
            # ssh-keygen writes the pubkey sibling file.
            (key_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAAfake smoke-probe\n")
            return _completed(returncode=0)

        monkeypatch.setattr(sp.subprocess, "run", _ok)

        path = sp._generate_keypair()
        assert path == str(key_dir / "id_ed25519")
        assert os.environ["SSH_PUBKEY"] == "ssh-ed25519 AAAAfake smoke-probe"


class TestShimSurveyCapture:
    """Smoke-side half of the issue-#85 survey: shim occurrences must reach the
    telemetry file, or the 30-day removal condition has no data to evaluate."""

    NULL_DIAG = (
        '{"type": "akash-diag", "level": "warning", "code": "EXEC_EXIT_CODE_UNKNOWN", '
        '"message": "result frame has a null exit_code", '
        '"context": {"shape": "a null exit_code"}}'
    )

    def test_parses_the_shape_from_a_diag_line(self):
        assert sp._exit_code_shapes(self.NULL_DIAG) == {"a null exit_code"}

    def test_ignores_unrelated_stderr(self):
        noise = 'warning: something else\n{"type": "akash-diag", "code": "LEASE_DOWN"}\n'
        assert sp._exit_code_shapes(noise) == set()

    def test_malformed_json_never_raises(self):
        """A survey parser must never break a smoke check — the check's own
        verdict matters more than the survey riding along with it."""
        assert sp._exit_code_shapes('{"code": "EXEC_EXIT_CODE_UNKNOWN"') == set()
        assert sp._exit_code_shapes("") == set()

    def test_non_object_context_never_raises(self):
        """stderr is untrusted: a line can carry the right code with a scalar or
        list context. `.get` on that would raise through the no-raise contract
        and fail the smoke check this parser only rides along on."""
        for ctx in ('"a string"', "[1, 2]", "12", "null"):
            line = f'{{"type": "akash-diag", "code": "EXEC_EXIT_CODE_UNKNOWN", "context": {ctx}}}'
            assert sp._exit_code_shapes(line) == set()

    def test_shapes_accumulate_across_execs_for_a_dseq(self, monkeypatch):
        """Every exec is a survey sample, not just the `exec` check. Under-counting
        would let the clean streak run out on partial evidence."""
        monkeypatch.setattr(sp, "_EXEC_EXIT_CODE_SHAPES", {})
        sp._note_exit_code_shapes("111", self.NULL_DIAG)
        sp._note_exit_code_shapes(
            "111", self.NULL_DIAG.replace("a null exit_code", "no exit_code key")
        )
        assert sp._EXEC_EXIT_CODE_SHAPES["111"] == {"a null exit_code", "no exit_code key"}

    def test_clean_stderr_records_nothing(self, monkeypatch):
        monkeypatch.setattr(sp, "_EXEC_EXIT_CODE_SHAPES", {})
        sp._note_exit_code_shapes("111", "all good\n")
        assert "111" not in sp._EXEC_EXIT_CODE_SHAPES

    def test_telemetry_record_carries_the_shapes(self):
        recs = sp._provider_records(
            "akash1a",
            "111",
            {"exec": "PASS"},
            {"exec": 10},
            exit_code_shapes={"a null exit_code"},
        )
        exec_rec = next(r for r in recs if r["feature"] == "exec")
        assert exec_rec["exit_code_shapes"] == ["a null exit_code"]

    def test_field_is_absent_when_the_shim_never_fired(self):
        """Absence is the clean signal. A present-but-empty field would be
        indistinguishable from a pre-instrumentation record."""
        recs = sp._provider_records("akash1a", "111", {"exec": "PASS"}, {"exec": 10})
        assert all("exit_code_shapes" not in r for r in recs)


class TestMatrixTimings:
    """The run matrix shows how long each feature took, not just pass/fail.

    Pass/fail is the lagging binary; the timing is the leading signal — a feature
    that still passes but has doubled in latency is the regression you want to see
    in the table you actually read, without cross-referencing the accrued report.
    """

    PROV = "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk"

    def _render(self, capsys, rows, records):
        sp._print_matrix(rows, records)
        # Strip ANSI so assertions are about content, not colour codes.
        import re

        return re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)

    def test_sub_second_is_ms_and_over_is_seconds(self):
        assert sp._fmt_latency(267) == "267ms"
        assert sp._fmt_latency(1600) == "1.6s"
        assert sp._fmt_latency(30800) == "30.8s"

    def test_missing_or_bogus_latency_renders_blank(self):
        """A feature that was never reached has no timing — it must render empty
        rather than '0ms', which would read as 'instantaneous'."""
        for bad in (None, "abc", -1, True):
            assert sp._fmt_latency(bad) == ""

    def test_timing_appears_next_to_the_outcome(self, capsys):
        rows = {self.PROV: {"exec": "PASS"}}
        records = [{"provider": self.PROV, "feature": "exec", "latency_ms": 1600}]
        assert "PASS 1.6s" in self._render(capsys, rows, records)

    def test_failure_keeps_its_timing(self, capsys):
        """'failed after 30s' and 'failed instantly' are different problems and the
        timing is the tell — so a FAIL must not drop its latency."""
        rows = {self.PROV: {"update": "FAIL"}}
        records = [{"provider": self.PROV, "feature": "update", "latency_ms": 30800}]
        assert "FAIL 30.8s" in self._render(capsys, rows, records)

    def test_skipped_feature_shows_no_timing(self, capsys):
        rows = {self.PROV: {"deploy": "NO-BID"}}
        records = [{"provider": self.PROV, "feature": "deploy", "latency_ms": None}]
        out = self._render(capsys, rows, records)
        assert "NO-BID" in out and "0ms" not in out

    def test_matrix_still_renders_without_records(self, capsys):
        """Timings are additive: the matrix must degrade to outcomes alone rather
        than break if it is ever called without telemetry records."""
        rows = {self.PROV: dict.fromkeys(sp.FEATURES, "PASS")}
        out = self._render(capsys, rows, None)
        assert "PASS" in out

    def test_provider_label_matches_the_accrued_report(self, capsys):
        """Same 14-char truncation the accrued telemetry report uses, so a provider
        reads identically in both tables."""
        rows = {self.PROV: {"exec": "PASS"}}
        out = self._render(capsys, rows, [])
        assert "akash1hgulk6ae" in out
        assert self.PROV not in out  # not the full 44-char address

    def test_columns_stay_aligned_when_timings_differ_in_width(self, capsys):
        """354ms and 30.8s are different widths; the column must size to the widest
        cell or the table shears."""
        rows = {self.PROV: {"ingress": "PASS", "update": "PASS"}}
        records = [
            {"provider": self.PROV, "feature": "ingress", "latency_ms": 354},
            {"provider": self.PROV, "feature": "update", "latency_ms": 30800},
        ]
        lines = [ln for ln in self._render(capsys, rows, records).splitlines() if ln.strip()]
        header = next(ln for ln in lines if ln.startswith("provider"))
        row = next(ln for ln in lines if ln.startswith("akash1"))
        assert len(header) == len(row.rstrip()) or len(row.rstrip()) <= len(header)
        assert "PASS 354ms" in row and "PASS 30.8s" in row
