"""Installed SDK wrapper; local capture is not durable or native authority."""

import asyncio
import sys
from dataclasses import replace
from typing import cast

import pytest
from test_hosted_cli import OWNER, PROVIDER
from test_hosted_cli import scene as scene

from just_akash import atomic_lease_admission as hook
from just_akash import cli
from just_akash import hosted_cli_bootstrap as bootstrap
from just_akash import hosted_legacy_cli as hosted


def argv_deploy(s, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hosted-cli-bootstrap",
            "deploy",
            "--sdl",
            str(s.sdl_path),
            "--bid-wait",
            "1",
            "--bid-wait-retry",
            "1",
            "--receipt-path",
            str(s.receipt_path),
            "--receipt-operation-id",
            "operation",
            "--provider",
            PROVIDER,
        ],
    )


def install_config(scene, monkeypatch, callback):
    config = bootstrap.HostedCLIBootstrap(scene.registration(), callback)
    monkeypatch.setattr(bootstrap, "_reviewed_hosted_cli_bootstrap", lambda: config)
    argv_deploy(scene, monkeypatch)
    return config


def test_absent_source_configuration_is78_before_legacy_cli(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "main", lambda: called.append(True))
    assert bootstrap._reviewed_hosted_cli_bootstrap() is None
    assert bootstrap.main() == 78 and not called
    assert hosted._reviewed_hosted_cli() is None


@pytest.mark.parametrize("untyped", [True, {}, (), "activate"])
def test_untyped_source_configuration_is_not_authority(monkeypatch, untyped):
    called = []
    monkeypatch.setattr(cli, "main", lambda: called.append(True))
    monkeypatch.setattr(bootstrap, "_reviewed_hosted_cli_bootstrap", lambda: untyped)
    assert bootstrap.main() == 78 and not called


def test_missing_callback_refuses_as_source_configuration(scene):
    with pytest.raises(hosted.HostedCLIUnknown):
        bootstrap.HostedCLIBootstrap(
            scene.registration(),
            cast(
                bootstrap.Callable[
                    [hosted.HostedCallResult | None, bootstrap.CLICompletion, float], None
                ],
                cast(object, None),
            ),
        )
    assert not scene.console and not scene.broker.phases


@pytest.mark.parametrize("command", ["destroy", "update", "--help", None])
def test_wrong_command_never_calls_legacy_cli(scene, monkeypatch, command):
    captured = []
    install_config(scene, monkeypatch, lambda *values: captured.append(values))
    monkeypatch.setattr(sys, "argv", ["bootstrap"] + ([] if command is None else [command]))
    assert bootstrap.main() == 78 and not captured
    assert not scene.console and not scene.broker.phases


def test_expired_original_end_refuses_before_cli_and_callback(scene, monkeypatch):
    config = install_config(scene, monkeypatch, lambda *_: pytest.fail("capture not entered"))
    expired = replace(config, registration=replace(config.registration, original_end=100.0))
    monkeypatch.setattr(bootstrap, "_reviewed_hosted_cli_bootstrap", lambda: expired)
    assert bootstrap.main() == 78
    assert not scene.console and not scene.broker.phases


def test_actual_cli_deploy_exact_once_capture_before_outer_retirement(scene, monkeypatch):
    captured = []
    active = []

    def capture(result, primary, end):
        installed = hosted._reviewed_hosted_cli()
        assert type(installed) is hosted.InstalledHostedCLI and installed.result is result
        assert result.entered and result.consume_locators and not result.locator_custody_unknown
        assert primary == bootstrap.CLICompletion("system-exit", 0, False)
        assert end == 900.0
        assert hook._reviewed_hook() is None  # Inner backend already retired.
        active.append(installed)
        captured.append((result, primary, end))

    install_config(scene, monkeypatch, capture)
    with pytest.raises(SystemExit) as exit_value:
        bootstrap.main()
    assert exit_value.value.code == 0 and len(captured) == len(active) == 1
    assert scene.broker.phases == ["reserve", "consume", "outcome"]
    assert sum(path == "/v1/leases" for _, path, _ in scene.console) == 1
    assert active[0]._capture is None and hosted._reviewed_hosted_cli() is None
    assert captured[0][0].consume_locators != ()


def test_actual_lost_ack_preserves78_and_none_never_empty(scene, monkeypatch):
    scene.broker.fail_phase = "reserve"
    captured = []
    install_config(scene, monkeypatch, lambda *values: captured.append(values))
    with pytest.raises(SystemExit) as exit_value:
        bootstrap.main()
    assert exit_value.value.code == 78 and len(captured) == 1
    result, primary, _ = captured[0]
    assert result.entered and result.locator_custody_unknown and result.consume_locators is None
    assert primary.exit_status == 78 and primary.locator_unknown
    assert scene.broker.phases == ["reserve"]
    assert not any(path == "/v1/leases" for _, path, _ in scene.console)
    assert hosted._reviewed_hosted_cli() is None


@pytest.mark.parametrize("fault", ["raise", "late"])
def test_capture_fault_changes_actual_success_to78_without_replay(scene, monkeypatch, fault):
    captured = []

    def capture(result, primary, end):
        captured.append((result, primary, end))
        if fault == "raise":
            raise OSError("SECRET_CAPTURE_ERROR_SENTINEL")
        scene.clock.value = end

    install_config(scene, monkeypatch, capture)
    assert bootstrap.main() == 78 and len(captured) == 1
    assert captured[0][1].exit_status == 0 and captured[0][2] == 900.0
    assert sum(path == "/v1/leases" for _, path, _ in scene.console) == 1
    assert captured[0][0].consume_locators and hosted._reviewed_hosted_cli() is None


@pytest.mark.parametrize("status", [1, 2, 75, 78])
@pytest.mark.parametrize("sink_failure", [False, True])
def test_exact_nonzero_system_exit_is_preserved_with_capture_once(
    scene, monkeypatch, status, sink_failure
):
    # Wrapper control, not sender qualification: failure before actual CLI body.
    primary = SystemExit(status)
    seen = []

    def capture(result, observation, end):
        seen.append((result, observation, end))
        if sink_failure:
            raise OSError("SECRET_CAPTURE_ERROR_SENTINEL")

    install_config(scene, monkeypatch, capture)

    def fail():
        raise primary

    monkeypatch.setattr(cli, "main", fail)
    with pytest.raises(SystemExit) as raised:
        bootstrap.main()
    assert raised.value is primary and raised.value.code == status
    assert len(seen) == 1 and seen[0][0] is None
    assert seen[0][1] == bootstrap.CLICompletion("system-exit", status, True)
    assert hosted._reviewed_hosted_cli() is None and not scene.console


@pytest.mark.parametrize("primary", [RuntimeError("SECRET_PRIMARY"), asyncio.CancelledError()])
def test_primary_exception_or_cancel_survives_capture_failure(scene, monkeypatch, primary):
    seen = []

    def capture(*values):
        seen.append(values)
        raise OSError("SECRET_CAPTURE_ERROR_SENTINEL")

    install_config(scene, monkeypatch, capture)

    def fail():
        raise primary

    monkeypatch.setattr(cli, "main", fail)
    with pytest.raises(type(primary)) as raised:
        bootstrap.main()
    assert raised.value is primary and len(seen) == 1 and seen[0][0] is None
    assert seen[0][1].kind == (
        "cancel" if isinstance(primary, asyncio.CancelledError) else "exception"
    )
    assert hosted._reviewed_hosted_cli() is None and not scene.console


def test_return_without_entered_actual_helper_cannot_claim_success(scene, monkeypatch):
    captured = []
    install_config(scene, monkeypatch, lambda *values: captured.append(values))
    monkeypatch.setattr(cli, "main", lambda: None)
    assert bootstrap.main() == 78 and len(captured) == 1
    assert captured[0][0] is None and captured[0][1].locator_unknown
    assert hosted._reviewed_hosted_cli() is None and not scene.console


def test_source_failure_does_not_fall_through_to_cli(monkeypatch):
    called = []

    def missing():
        raise LookupError("SECRET_CONFIG_SENTINEL")

    monkeypatch.setattr(bootstrap, "_reviewed_hosted_cli_bootstrap", missing)
    monkeypatch.setattr(cli, "main", lambda: called.append(True))
    assert bootstrap.main() == 78 and not called


def test_actual_destroy_uses_same_process_and_captures_observation_not_release(scene, monkeypatch):
    captured = []
    install_config(scene, monkeypatch, lambda *values: captured.append(values))
    with pytest.raises(SystemExit) as result:
        bootstrap.main()
    assert result.value.code == 0
    wire = captured[0][0].consume_locators[0]
    scene.clock.utc = 1800001000
    registration = hosted.CleanupCLIRegistration(
        scene.broker.policy, wire, 900.0, scene.clock.utc + 300, scene.principals
    )
    captured.clear()
    config = bootstrap.HostedCLIBootstrap(registration, lambda *values: captured.append(values))
    monkeypatch.setattr(bootstrap, "_reviewed_hosted_cli_bootstrap", lambda: config)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap",
            "destroy",
            "--dseq",
            "123",
            "--expected-owner",
            OWNER,
            "--expected-group",
            "dcloud",
            "--yes",
        ],
    )
    assert bootstrap.main() == 0 and len(captured) == 1
    assert captured[0][0].cleanup_observation.execution_closed is True
    assert captured[0][1] == bootstrap.CLICompletion("return", 0, False)
    assert captured[0][0].consume_locators is None  # Not fabricated empty create debt.
    assert sum(method == "DELETE" for method, _, _ in scene.console) == 1
    assert hosted._reviewed_hosted_cli() is None


@pytest.mark.parametrize("fault", ["late", "raise"])
def test_actual_outer_retirement_uncertainty_prevents_success(scene, monkeypatch, fault):
    captured = []
    install_config(scene, monkeypatch, lambda *values: captured.append(values))
    actual_exit = hosted.InstalledHostedCLI.__exit__

    def exit_with_fault(self, *values):
        result = actual_exit(self, *values)
        if fault == "late":
            scene.clock.value = 900.0
        else:
            raise OSError("SECRET_RETIREMENT_SENTINEL")
        return result

    monkeypatch.setattr(hosted.InstalledHostedCLI, "__exit__", exit_with_fault)
    assert bootstrap.main() == 78 and len(captured) == 1
    assert captured[0][1].exit_status == 0
    assert hosted._reviewed_hosted_cli() is None
    assert sum(path == "/v1/leases" for _, path, _ in scene.console) == 1


def test_console_entry_nonzero_return_status_is_not_ignored(scene, monkeypatch):
    captured = []
    install_config(scene, monkeypatch, lambda *values: captured.append(values))
    monkeypatch.setattr(cli, "main", lambda: 7)
    with pytest.raises(SystemExit) as raised:
        bootstrap.main()
    assert raised.value.code == 7 and captured[0][1].exit_status == 7
    assert captured[0][0] is None and hosted._reviewed_hosted_cli() is None


def test_source_cancellation_before_entry_is_not_changed_to_success(monkeypatch):
    primary = asyncio.CancelledError()

    def cancelled():
        raise primary

    monkeypatch.setattr(bootstrap, "_reviewed_hosted_cli_bootstrap", cancelled)
    with pytest.raises(asyncio.CancelledError) as raised:
        bootstrap.main()
    assert raised.value is primary and hosted._reviewed_hosted_cli() is None


@pytest.mark.parametrize("kind", [asyncio.CancelledError, KeyboardInterrupt])
@pytest.mark.parametrize("phase", ["capture", "entry", "partial-entry", "retirement", "lifetime"])
def test_first_cancellation_is_preserved_and_own_registration_retires(
    scene, monkeypatch, phase, kind
):
    primary = kind()
    before = hosted._reviewed_hosted_cli
    actual_enter = hosted.InstalledHostedCLI.__enter__
    actual_exit = hosted.InstalledHostedCLI.__exit__
    actual_lifetime = hosted._lifetime
    managers = []
    captured = []
    state = {"retired": False}

    def capture(*values):
        captured.append(values)
        if phase == "capture":
            raise primary

    install_config(scene, monkeypatch, capture)

    def enter(self):
        managers.append(self)
        if phase == "entry":
            raise primary
        result = actual_enter(self)
        if phase == "partial-entry":
            raise primary
        return result

    def exit_(self, *values):
        result = actual_exit(self, *values)
        state["retired"] = True
        if phase == "retirement":
            raise primary
        return result

    def lifetime(*values):
        actual_lifetime(*values)
        if phase == "lifetime" and state["retired"]:
            raise primary

    monkeypatch.setattr(hosted.InstalledHostedCLI, "__enter__", enter)
    monkeypatch.setattr(hosted.InstalledHostedCLI, "__exit__", exit_)
    monkeypatch.setattr(hosted, "_lifetime", lifetime)
    raised = None
    try:
        bootstrap.main()
    except BaseException as error:
        raised = error
    retired = hosted._reviewed_hosted_cli is before
    import json

    scene.receipt_path.parent.joinpath("causal-finite-observation.json").write_text(
        json.dumps(
            {
                "phase": phase,
                "kind": kind.__name__,
                "same_primary": raised is primary,
                "retired_before_test_cleanup": retired,
                "capture_calls": len(captured),
                "lease_opens": sum(path == "/v1/leases" for _, path, _ in scene.console),
            }
        )
    )
    # Always retire only our actual test-owned manager; this is teardown, not
    # a fake source restoration/assertion. Preserve the precleanup observation.
    for manager in managers:
        actual_exit(manager, kind, primary, primary.__traceback__)
    assert hosted._reviewed_hosted_cli is before
    assert raised is primary
    assert retired
    assert len(captured) == (0 if phase in {"entry", "partial-entry"} else 1)
    assert sum(path == "/v1/leases" for _, path, _ in scene.console) == (
        0 if phase in {"entry", "partial-entry"} else 1
    )


@pytest.mark.parametrize("earlier", [RuntimeError("public-primary"), SystemExit(2)])
@pytest.mark.parametrize("late", [asyncio.CancelledError, KeyboardInterrupt])
def test_earlier_real_primary_wins_over_capture_cancellation(scene, monkeypatch, earlier, late):
    def capture(*_values):
        raise late()

    install_config(scene, monkeypatch, capture)

    def fail():
        raise earlier

    monkeypatch.setattr(cli, "main", fail)
    with pytest.raises(type(earlier)) as raised:
        bootstrap.main()
    assert raised.value is earlier and hosted._reviewed_hosted_cli() is None
    assert not scene.console


def test_destroy_missing_observation_is_unknown_contract():
    result = hosted.HostedCallResult("destroy", entered=True, cleanup_observation=None)
    assert bootstrap._completion(None, result).locator_unknown


def test_actual_declined_destroy_does_not_report_observed_cleanup(scene, monkeypatch):
    captures = []
    install_config(scene, monkeypatch, lambda *values: captures.append(values))
    with pytest.raises(SystemExit) as first:
        bootstrap.main()
    assert first.value.code == 0
    wire = captures[0][0].consume_locators[0]
    scene.clock.utc = 1800001000
    registration = hosted.CleanupCLIRegistration(
        scene.broker.policy, wire, 900.0, scene.clock.utc + 300, scene.principals
    )
    captures.clear()
    config = bootstrap.HostedCLIBootstrap(registration, lambda *values: captures.append(values))
    monkeypatch.setattr(bootstrap, "_reviewed_hosted_cli_bootstrap", lambda: config)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap",
            "destroy",
            "--dseq",
            "123",
            "--expected-owner",
            OWNER,
            "--expected-group",
            "dcloud",
        ],
    )
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    result = bootstrap.main()
    assert len(captures) == 1 and captures[0][0].entered
    assert captures[0][0].cleanup_observation is None
    assert not any(method == "DELETE" for method, _, _ in scene.console)
    assert hosted._reviewed_hosted_cli() is None
    assert result == 78
    assert captures[0][1].locator_unknown
