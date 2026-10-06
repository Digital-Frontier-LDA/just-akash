"""Private SDL diagnostics never trust response/error echoes as printable text."""

from __future__ import annotations

import errno
import io
import json
import logging
import os
import traceback
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import Mock

import pytest

from just_akash import deploy as dp
from just_akash._confidential import active, credential_content, sdl_operation
from just_akash.api import AkashAPIError, AkashConsoleAPI
from tests.test_deploy import SDL_YAML, _make_bid, _time_mock
from tests.test_deployment_receipt import OWNER, SDL

# Non-authenticating synthetic echo canary, not a credential.
# Never load operator environment/files; preserve every echo assertion and wire test.
ECHO_CANARY = "synthetic-reader-echo-canary"
PRIVATE = SDL_YAML.replace(
    "    expose:",
    f"    credentials:\n      host: docker.io\n      username: jobordu\n"
    f"      password: {ECHO_CANARY}\n    expose:",
)


def _write_sdl_fixture(path: Path | str, content: str) -> None:
    """Exercise real SDL reads with synthetic bytes in an owner-only fixture file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(content)


def test_sdl_fixture_create_and_overwrite_preserve_bytes_and_owner_only_mode(tmp_path):
    path = tmp_path / "private.yaml"
    _write_sdl_fixture(path, PRIVATE)
    assert path.read_text() == PRIVATE and path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    malformed = f'credentials: ["{ECHO_CANARY}"\n'
    _write_sdl_fixture(path, malformed)
    assert path.read_text() == malformed and path.stat().st_mode & 0o777 == 0o600


def _http(monkeypatch, body, status=400):
    def fail(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://console.invalid", status, ECHO_CANARY, Message(), io.BytesIO(body)
        )

    monkeypatch.setattr("urllib.request.urlopen", fail)


def _visible(error, caplog, capsys):
    captured = capsys.readouterr()
    return (
        caplog.text
        + captured.out
        + captured.err
        + "".join(traceback.format_exception(error))
        + repr(error.args)
        + repr(vars(error))
    )


@pytest.mark.parametrize(
    "content",
    [
        PRIVATE,
        '"credent\\u0069als": {password: sentinel}',
        "credentials: {password: sentinel}\ncredentials: null",
        "value: &private {credentials: {password: sentinel}}\nalias: *private",
        "env: [DOCKERHUB_PULL_TOKEN=sentinel]",
        "env: {DOCKERHUB_PULL_TOKEN: sentinel}",
        '{"credentials":{"password":"sentinel"}}',
        "credentials: [malformed",
    ],
)
def test_private_yaml_nodes_cannot_be_hidden_by_alias_quoting_or_duplicates(content):
    assert credential_content(content)


def test_public_and_recursive_alias_nodes_are_bounded():
    assert not credential_content(SDL_YAML)
    assert not credential_content("value: &recursive [*recursive]")


@pytest.mark.parametrize(
    "method,args",
    [
        ("create_deployment", (PRIVATE,)),
        ("update_deployment", ("42", PRIVATE)),
        ("create_lease", ("42", "akash1provider", PRIVATE)),
    ],
)
def test_direct_sdl_and_manifest_calls_withhold_body_args_and_http_cause(
    method, args, monkeypatch, caplog, capsys
):
    caplog.set_level(logging.DEBUG)
    _http(monkeypatch, json.dumps({"message": ECHO_CANARY, "error_name": ECHO_CANARY}).encode())
    with pytest.raises(AkashAPIError) as caught:
        getattr(AkashConsoleAPI("fake-console-key"), method)(*args)
    error = caught.value
    assert error.status == 400 and error.body == "" and error.error_name == ""
    assert error.__cause__ is None and error.__suppress_context__
    assert ECHO_CANARY not in _visible(error, caplog, capsys)


@pytest.mark.parametrize(
    "marker",
    [
        "already exists",
        "no longer open",
        "no lease for deployment",
        "jwt has invalid claims",
        "insufficient credit",
        "insufficient balance",
        "payment required",
    ],
)
def test_fixed_retry_classification_survives_without_arbitrary_remote_prose(
    marker, monkeypatch, caplog, capsys
):
    _http(monkeypatch, json.dumps({"message": f"{marker}: {ECHO_CANARY}"}).encode())
    with pytest.raises(AkashAPIError) as caught:
        AkashConsoleAPI("fake-console-key").create_deployment(PRIVATE)
    assert marker in str(caught.value).lower()
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)


def test_upstream_fields_are_preserved_without_trusting_echoes(monkeypatch, caplog, capsys):
    _http(
        monkeypatch,
        json.dumps(
            {
                "message": ECHO_CANARY,
                "retryable": True,
                "retry_after": 0,
                "error_name": "origin_response_timeout",
            }
        ).encode(),
        status=524,
    )
    with pytest.raises(AkashAPIError) as caught:
        AkashConsoleAPI("fake-console-key").create_deployment(PRIVATE)
    error = caught.value
    assert error.status == 524 and error.is_upstream_timeout()
    assert error.retryable is True and error.retry_after == 0
    assert ECHO_CANARY not in _visible(error, caplog, capsys)


@pytest.mark.parametrize(
    "raw", [b"not-json synthetic-reader-echo-canary", b"\xffsynthetic-reader-echo-canary"]
)
def test_malformed_private_error_body_is_still_an_http_verdict(raw, monkeypatch, caplog, capsys):
    _http(monkeypatch, raw, status=500)
    with pytest.raises(AkashAPIError) as caught:
        AkashConsoleAPI("fake-console-key").create_deployment(PRIVATE)
    assert caught.value.status == 500
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)


@pytest.mark.parametrize(
    "failure,expected",
    [
        (TimeoutError(ECHO_CANARY), TimeoutError),
        (TimeoutError(errno.ETIMEDOUT, ECHO_CANARY), TimeoutError),
        (ConnectionResetError(errno.ECONNRESET, ECHO_CANARY, ECHO_CANARY), ConnectionResetError),
        (urllib.error.URLError(ECHO_CANARY), RuntimeError),
        (ConnectionResetError(ECHO_CANARY), ConnectionResetError),
    ],
)
def test_private_socket_failures_keep_type_and_hide_remote_echo(
    failure, expected, monkeypatch, caplog, capsys
):
    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(expected) as caught:
        AkashConsoleAPI("fake-console-key").create_deployment(PRIVATE)
    assert not isinstance(caught.value, AkashAPIError)
    if isinstance(caught.value, OSError):
        assert caught.value.errno == failure.errno
    if isinstance(failure, urllib.error.URLError):
        assert str(caught.value).startswith("Connection error:")
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)


def test_successful_private_request_preserves_wire_and_result_not_debug_echo(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.status = 200
    response.read.return_value = json.dumps(
        {ECHO_CANARY: ECHO_CANARY, "manifest": PRIVATE}
    ).encode()
    seen = []

    def send(request, **kwargs):
        seen.append((json.loads(request.data), kwargs))
        return response

    monkeypatch.setattr("urllib.request.urlopen", send)
    result = AkashConsoleAPI("fake-console-key").create_deployment(PRIVATE)
    assert result["manifest"] == PRIVATE  # Needed unchanged for the lease request.
    assert seen[0][0]["data"]["sdl"] == PRIVATE
    assert seen[0][1]["timeout"] == 180.0
    assert ECHO_CANARY not in caplog.text


@pytest.fixture
def private_deploy(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("AKASH_API_KEY", "fake-console-key")
    monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
    monkeypatch.delenv("AKASH_API_KEY_POOL", raising=False)
    monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
    monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
    monkeypatch.setattr(dp, "_check_wallet_credit", lambda *_args: None)
    monkeypatch.setattr(dp, "_report_suspected_orphans", Mock())
    monkeypatch.setattr(dp.time, "time", _time_mock())
    monkeypatch.setattr(dp.time, "sleep", lambda *_args: None)
    client = Mock()
    monkeypatch.setattr(dp, "AkashConsoleAPI", Mock(return_value=client))
    client.create_deployment.return_value = {
        "dseq": "42",
        "manifest": PRIVATE,
        ECHO_CANARY: ECHO_CANARY,
    }
    client.get_bids.return_value = [_make_bid("akash1provider", 100)]
    client.create_lease.return_value = {"data": {"lease": "created"}}
    client.account_address.return_value = OWNER
    path = tmp_path / "private.yaml"
    _write_sdl_fixture(path, PRIVATE)
    return client, str(path)


def test_private_success_does_not_print_full_create_response(private_deploy, caplog, capsys):
    client, path = private_deploy
    result = dp.deploy(path, bid_wait=2, bid_wait_retry=2)
    assert result["dseq"] == "42"
    assert client.create_lease.call_args.kwargs["manifest"] == PRIVATE
    assert ECHO_CANARY not in caplog.text + str(capsys.readouterr())
    assert not active()


def test_missing_dseq_never_echoes_response_or_key_names(private_deploy, caplog, capsys):
    client, path = private_deploy
    client.create_deployment.return_value = {"manifest": PRIVATE, ECHO_CANARY: ECHO_CANARY}
    with pytest.raises(RuntimeError) as caught:
        dp.deploy(path)
    assert client.create_deployment.call_count == 1
    assert "NO_DSEQ_RETURNED" in capsys.readouterr().err
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)
    assert not active()


@pytest.mark.parametrize(
    "error",
    [
        AkashAPIError(
            ECHO_CANARY, status=524, body=ECHO_CANARY, retryable=True, error_name=ECHO_CANARY
        ),
        TimeoutError(ECHO_CANARY),
    ],
)
def test_private_ambiguous_create_never_replays(private_deploy, error, caplog, capsys):
    client, path = private_deploy
    client.create_deployment.side_effect = error
    with pytest.raises((RuntimeError, TimeoutError)) as caught:
        dp.deploy(path)
    assert client.create_deployment.call_count == 1
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)
    assert not active()


def test_existing_already_exists_retry_is_preserved(private_deploy, monkeypatch, caplog, capsys):
    client, path = private_deploy
    client.create_deployment.side_effect = [
        RuntimeError(f"already exists {ECHO_CANARY}"),
        {"dseq": "42", "manifest": PRIVATE},
    ]
    cleanup = Mock()
    monkeypatch.setattr(dp, "_close_stale_for_retry", cleanup)
    assert dp.deploy(path, bid_wait=2, bid_wait_retry=2)["dseq"] == "42"
    assert client.create_deployment.call_count == 2 and cleanup.call_count == 1
    assert ECHO_CANARY not in caplog.text + str(capsys.readouterr())


def test_private_readback_and_lease_cleanup_errors_are_sanitized(private_deploy, caplog, capsys):
    client, path = private_deploy
    client.create_lease.side_effect = RuntimeError(ECHO_CANARY)
    client.close_deployment.side_effect = RuntimeError(ECHO_CANARY)
    with pytest.raises(RuntimeError) as caught:
        dp.deploy(path, bid_wait=2, bid_wait_retry=2)
    assert client.create_deployment.call_count == 1
    client.close_deployment.assert_called_once_with("42")
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)


def test_receipt_failure_remains_nonretryable_without_error_echo(
    private_deploy, tmp_path, monkeypatch, caplog, capsys
):
    client, path = private_deploy
    _write_sdl_fixture(
        path,
        SDL.replace("    image:", f"    credentials: {{password: {ECHO_CANARY}}}\n    image:", 1),
    )
    parent = tmp_path / "receipt-dir"
    parent.mkdir(mode=0o700)
    receipt = parent / "receipt.json"

    def fail(*_args, **_kwargs):
        raise RuntimeError(ECHO_CANARY)

    monkeypatch.setattr(dp, "mark_create_response_received", fail)
    with pytest.raises(RuntimeError, match="NON-RETRYABLE CREATE OUTCOME AMBIGUOUS") as caught:
        dp.deploy(path, receipt_path=str(receipt), receipt_operation_id="test-create-42")
    assert client.create_deployment.call_count == 1
    assert json.loads(receipt.read_text())["state"] == "submitting"
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)


def test_private_context_resets_and_public_http_contract_stays_exact(tmp_path, monkeypatch):
    path = tmp_path / "private.yaml"
    _write_sdl_fixture(path, PRIVATE)

    @sdl_operation
    def fail(sdl_path):
        assert active()
        raise ValueError(ECHO_CANARY)

    with pytest.raises(ValueError):
        fail(str(path))
    assert not active()
    _http(monkeypatch, b'{"message":"already exists"}', status=409)
    with pytest.raises(AkashAPIError) as caught:
        AkashConsoleAPI("fake-console-key").create_deployment(SDL_YAML)
    assert str(caught.value) == "API Error (409): already exists"
    assert caught.value.body == '{"message":"already exists"}'


def test_private_validation_error_cannot_echo_parser_source(private_deploy, caplog, capsys):
    client, path = private_deploy
    _write_sdl_fixture(path, f'credentials: ["{ECHO_CANARY}"\n')
    with pytest.raises(RuntimeError) as caught:
        dp.deploy(path)
    client.create_deployment.assert_not_called()
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)


def test_actual_read_private_bytes_win_over_public_preflight_snapshot(
    private_deploy, monkeypatch, caplog, capsys
):
    client, path = private_deploy
    original = Path.read_text

    def public_snapshot(self, *args, **kwargs):
        if str(self) == path:
            return SDL_YAML  # File changed after the logging preflight.
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", public_snapshot)
    client.create_deployment.side_effect = RuntimeError(ECHO_CANARY)
    with pytest.raises(RuntimeError) as caught:
        dp.deploy(path)
    assert client.create_deployment.call_args.args[0] == PRIVATE
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)
    assert not active()


def test_reader_env_override_is_private_even_when_base_sdl_is_public(
    private_deploy, caplog, capsys
):
    client, path = private_deploy
    _write_sdl_fixture(path, SDL_YAML)
    client.create_deployment.side_effect = RuntimeError(ECHO_CANARY)
    with pytest.raises(RuntimeError) as caught:
        dp.deploy(path, env_vars=[f"DOCKERHUB_PULL_TOKEN={ECHO_CANARY}"])
    assert f"DOCKERHUB_PULL_TOKEN={ECHO_CANARY}" in client.create_deployment.call_args.args[0]
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)


def test_private_update_uses_same_logging_boundary_without_creating(
    private_deploy, caplog, capsys
):
    client, path = private_deploy
    client.update_deployment.side_effect = RuntimeError(ECHO_CANARY)
    with pytest.raises(RuntimeError) as caught:
        dp.update("42", path)
    client.create_deployment.assert_not_called()
    assert client.update_deployment.call_args.args == ("42", PRIVATE)
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)
    assert not active()


def test_existing_private_lease_auth_retry_keeps_same_identity(private_deploy, caplog, capsys):
    client, path = private_deploy
    client.create_lease.side_effect = [
        RuntimeError(f"JWT has invalid claims: {ECHO_CANARY}"),
        {"data": {"lease": "created"}},
    ]
    result = dp.deploy(path, bid_wait=2, bid_wait_retry=2)
    assert result["dseq"] == "42" and client.create_deployment.call_count == 1
    calls = [call.kwargs for call in client.create_lease.call_args_list]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert ECHO_CANARY not in caplog.text + str(capsys.readouterr())


def test_existing_private_stale_bid_retry_keeps_order_and_moves_provider(
    private_deploy, caplog, capsys
):
    client, path = private_deploy
    client.get_bids.return_value = [
        _make_bid("akash1provider", 100),
        _make_bid("akash1other", 200),
    ]
    client.create_lease.side_effect = [
        RuntimeError(f"no longer open: {ECHO_CANARY}"),
        {"data": {"lease": "created"}},
    ]
    result = dp.deploy(path, bid_wait=2, bid_wait_retry=2)
    assert result["dseq"] == "42" and client.create_deployment.call_count == 1
    calls = [call.kwargs for call in client.create_lease.call_args_list]
    assert [call["dseq"] for call in calls] == ["42", "42"]
    assert {call["provider"] for call in calls} == {"akash1provider", "akash1other"}
    assert calls[0]["provider"] != calls[1]["provider"]
    assert ECHO_CANARY not in caplog.text + str(capsys.readouterr())


def test_existing_private_no_order_redeploy_does_not_echo_second_response(
    private_deploy, caplog, capsys
):
    client, path = private_deploy
    client.create_deployment.side_effect = [
        {"dseq": "42", "manifest": PRIVATE},
        {"manifest": PRIVATE, ECHO_CANARY: ECHO_CANARY},
    ]
    client.create_lease.side_effect = RuntimeError(f"no lease for deployment: {ECHO_CANARY}")
    with pytest.raises(RuntimeError) as caught:
        dp.deploy(path, bid_wait=2, bid_wait_retry=2)
    assert client.create_deployment.call_count == 2  # The existing, scoped recovery only.
    client.close_deployment.assert_called_once_with("42")
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)


def test_private_response_receipt_survives_later_bid_failure(
    private_deploy, tmp_path, caplog, capsys
):
    client, path = private_deploy
    _write_sdl_fixture(
        path,
        SDL.replace("    image:", f"    credentials: {{password: {ECHO_CANARY}}}\n    image:", 1),
    )
    client.get_bids.side_effect = RuntimeError(ECHO_CANARY)
    parent = tmp_path / "receipt-dir"
    parent.mkdir(mode=0o700)
    receipt = parent / "receipt.json"
    with pytest.raises(RuntimeError) as caught:
        dp.deploy(
            path,
            bid_wait=2,
            bid_wait_retry=2,
            receipt_path=str(receipt),
            receipt_operation_id="test-create-42",
        )
    record = json.loads(receipt.read_text())
    assert record["state"] == "create_response_received" and record["dseq"] == "42"
    assert client.create_deployment.call_count == 1
    assert ECHO_CANARY not in receipt.read_text()
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)


def test_main_ci_runtime_client_keeps_stronger_marker_free_protection(monkeypatch, caplog, capsys):
    from just_akash import ci_run_budget
    from just_akash.api import CIConsoleAPI
    from just_akash.ci_run_budget import CIRunBudgetError

    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("AKASH_CI_BUDGET_URL", "https://budget.invalid")
    monkeypatch.setenv("AKASH_CI_BUDGET_AUDIENCE", "ci-budget")
    monkeypatch.setenv("AKASH_CI_BUDGET_ACCOUNT_ID", "primary")
    monkeypatch.setattr(ci_run_budget, "_token", lambda _audience: "synthetic-oidc-token")
    client = CIConsoleAPI("synthetic-controller-key")
    body = json.dumps({"message": f"already exists; no longer open; {ECHO_CANARY}"}).encode()

    def fail(_opener, request, **_kwargs):
        assert request.full_url == "https://budget.invalid/v1/akash-ci-budget/creates"
        assert request.get_header("X-api-key") is None
        assert request.get_header("Authorization") == "Bearer synthetic-oidc-token"
        assert json.loads(request.data)["sdl_content"] == PRIVATE
        raise urllib.error.HTTPError(
            "https://budget.invalid", 409, ECHO_CANARY, Message(), io.BytesIO(body)
        )

    monkeypatch.setattr("urllib.request.OpenerDirector.open", fail)
    with pytest.raises(CIRunBudgetError) as caught:
        client.create_deployment(PRIVATE, ci_operation_id="stable-intent")
    assert str(caught.value) == "CI_BUDGET_OPERATION_REPLAY"
    assert "already exists" not in str(caught.value)
    assert "no longer open" not in str(caught.value)
    assert caught.value._is_non_retryable is True
    assert vars(caught.value) == {"code": "CI_BUDGET_OPERATION_REPLAY"}
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)


def test_main_receipt_private_already_exists_refuses_replay_and_stale_sweep(
    private_deploy, tmp_path, monkeypatch, caplog, capsys
):
    client, path = private_deploy
    _write_sdl_fixture(
        path,
        SDL.replace("    image:", f"    credentials: {{password: {ECHO_CANARY}}}\n    image:", 1),
    )
    parent = tmp_path / "receipt-dir"
    parent.mkdir(mode=0o700)
    receipt = parent / "receipt.json"
    client.create_deployment.side_effect = RuntimeError(f"already exists {ECHO_CANARY}")
    cleanup = Mock()
    monkeypatch.setattr(dp, "_close_stale_for_retry", cleanup)
    with pytest.raises(RuntimeError, match="Receipt-bound create already exists") as caught:
        dp.deploy(path, receipt_path=str(receipt), receipt_operation_id="test-create-42")
    assert str(caught.value) == (
        "Receipt-bound create already exists; reconcile the recorded operation before retrying"
    )
    assert client.create_deployment.call_count == 1
    cleanup.assert_not_called()
    assert json.loads(receipt.read_text())["state"] == "submitting"
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)


@pytest.mark.parametrize(
    "identity", [ECHO_CANARY, "0", "01", "-1", "１２３", str(2**64), True, 42.0]
)
def test_malformed_private_create_identity_stops_before_logs_poll_or_replay(
    private_deploy, identity, caplog, capsys
):
    client, path = private_deploy
    client.create_deployment.return_value = {"dseq": identity, "manifest": PRIVATE}
    with pytest.raises(RuntimeError, match="NON-RETRYABLE CREATE OUTCOME AMBIGUOUS") as caught:
        dp.deploy(path)
    assert client.create_deployment.call_count == 1
    client.get_bids.assert_not_called()
    client.create_lease.assert_not_called()
    client.close_deployment.assert_not_called()  # No guessed cleanup identity.
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)


def test_invalid_private_create_identity_keeps_submitting_receipt(
    private_deploy, tmp_path, caplog, capsys
):
    client, path = private_deploy
    _write_sdl_fixture(
        path,
        SDL.replace("    image:", f"    credentials: {{password: {ECHO_CANARY}}}\n    image:", 1),
    )
    parent = tmp_path / "receipt-dir"
    parent.mkdir(mode=0o700)
    receipt = parent / "receipt.json"
    client.create_deployment.return_value = {"dseq": ECHO_CANARY, "manifest": PRIVATE}
    with pytest.raises(RuntimeError, match="NON-RETRYABLE CREATE OUTCOME AMBIGUOUS") as caught:
        dp.deploy(path, receipt_path=str(receipt), receipt_operation_id="test-create-42")
    assert client.create_deployment.call_count == 1
    assert json.loads(receipt.read_text())["state"] == "submitting"
    client.get_bids.assert_not_called()
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)


def test_invalid_private_redeploy_identity_is_not_logged_or_polled(private_deploy, caplog, capsys):
    client, path = private_deploy
    client.create_deployment.side_effect = [
        {"dseq": "42", "manifest": PRIVATE},
        {"dseq": ECHO_CANARY, "manifest": PRIVATE},
    ]
    client.create_lease.side_effect = RuntimeError("no lease for deployment")
    with pytest.raises(RuntimeError, match="NON-RETRYABLE CREATE OUTCOME AMBIGUOUS") as caught:
        dp.deploy(path, bid_wait=2, bid_wait_retry=2)
    assert client.create_deployment.call_count == 2
    assert {call.args[0] for call in client.get_bids.call_args_list} == {"42"}
    client.close_deployment.assert_called_once_with("42")
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)


def test_private_bid_and_final_metadata_echo_is_withheld_without_rewriting_transport(
    private_deploy, caplog, capsys
):
    client, path = private_deploy
    bid = _make_bid(ECHO_CANARY, 100, denom=ECHO_CANARY)
    client.get_bids.return_value = [bid]
    client.account_address.return_value = ECHO_CANARY
    result = dp.deploy(path, bid_wait=2, bid_wait_retry=2)
    assert result["provider"] == ECHO_CANARY
    assert client.create_lease.call_args.kwargs["provider"] == ECHO_CANARY
    assert client.create_lease.call_args.kwargs["manifest"] == PRIVATE
    assert bid["id"]["provider"] == ECHO_CANARY and bid["price"]["denom"] == ECHO_CANARY
    assert ECHO_CANARY not in caplog.text + str(capsys.readouterr())


def test_private_foreign_bid_state_echo_is_withheld_even_before_tier_filtering(
    private_deploy, caplog, capsys
):
    client, path = private_deploy
    bid = _make_bid(ECHO_CANARY, 100, denom=ECHO_CANARY)
    bid["state"] = ECHO_CANARY
    client.get_bids.return_value = [bid]
    with pytest.raises(RuntimeError) as caught:
        dp.deploy(path, bid_wait=2, bid_wait_retry=2, preferred_providers=[OWNER])
    assert client.create_deployment.call_count == 1
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)


def test_private_bid_table_state_and_structured_diagnostic_context_do_not_echo(
    tmp_path, caplog, capsys
):
    caplog.set_level(logging.DEBUG)
    path = tmp_path / "private.yaml"
    _write_sdl_fixture(path, PRIVATE)
    bid = _make_bid(ECHO_CANARY, 100, denom=ECHO_CANARY)
    bid["state"] = ECHO_CANARY

    @sdl_operation
    def describe(sdl_path):
        dp._log_bid_table([bid], "TEST")
        dp.emit(
            "NO_DSEQ_RETURNED",
            "error",
            ECHO_CANARY,
            provider=ECHO_CANARY,
            account=ECHO_CANARY,
            dseq=ECHO_CANARY,
            states=[ECHO_CANARY],
        )

    describe(str(path))
    assert ECHO_CANARY not in caplog.text + str(capsys.readouterr())
    assert bid["state"] == ECHO_CANARY


def test_private_followup_endpoint_echo_is_hidden_without_rewriting_wire(
    tmp_path, monkeypatch, caplog, capsys
):
    caplog.set_level(logging.DEBUG)
    path = tmp_path / "private.yaml"
    _write_sdl_fixture(path, PRIVATE)
    sent = []

    def fail(request, **_kwargs):
        sent.append(request.full_url)
        raise urllib.error.HTTPError(
            "url", 400, ECHO_CANARY, Message(), io.BytesIO(ECHO_CANARY.encode())
        )

    monkeypatch.setattr("urllib.request.urlopen", fail)

    @sdl_operation
    def read(sdl_path):
        return AkashConsoleAPI("fake-console-key")._request("GET", f"/v1/bids/{ECHO_CANARY}")

    with pytest.raises(AkashAPIError) as caught:
        read(str(path))
    assert ECHO_CANARY in sent[0]
    assert ECHO_CANARY not in _visible(caught.value, caplog, capsys)


def test_valid_private_metadata_stays_visible_and_public_metadata_remains_unchanged(
    tmp_path, caplog, capsys
):
    from just_akash._confidential import canonical_dseq, display

    assert canonical_dseq("18446744073709551615")
    assert canonical_dseq(42)
    assert not canonical_dseq("18446744073709551616")
    path = tmp_path / "private.yaml"
    _write_sdl_fixture(path, PRIVATE)

    @sdl_operation
    def describe(sdl_path):
        assert display("42", "dseq") == "42"
        assert display(OWNER, "address") == OWNER
        assert display("open", "state") == "open"
        assert display("uact", "denom") == "uact"
        dp.emit("LEASE_CREATE_FAILED", "error", "fixed", dseq="42", provider=OWNER)

    describe(str(path))
    output = capsys.readouterr().err
    assert json.loads(output)["dseq"] == "42"
    assert json.loads(output)["context"]["provider"] == OWNER
    assert not active()
    assert display(ECHO_CANARY, "address") == ECHO_CANARY
    assert display(ECHO_CANARY, "state") == ECHO_CANARY
    assert display(ECHO_CANARY, "denom") == ECHO_CANARY


def test_private_and_public_auctions_keep_identical_selection_and_lease_wire(
    private_deploy, monkeypatch, capsys
):
    client, path = private_deploy
    candidates = [
        "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk",
        "akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z",
    ]
    client.get_bids.return_value = [
        _make_bid(p, 100 + index) for index, p in enumerate(candidates)
    ]
    private_result = dp.deploy(path, bid_wait=2, bid_wait_retry=2, preferred_providers=candidates)
    private_lease = dict(client.create_lease.call_args.kwargs)
    assert client.create_deployment.call_count == 1
    assert private_lease["manifest"] == PRIVATE
    assert private_lease["provider"] in candidates
    client.reset_mock()
    monkeypatch.setattr(dp.time, "time", _time_mock())
    _write_sdl_fixture(path, SDL_YAML)
    public_result = dp.deploy(path, bid_wait=2, bid_wait_retry=2, preferred_providers=candidates)
    assert public_result == private_result
    assert client.create_lease.call_args.kwargs == private_lease
    assert client.create_deployment.call_count == 1
    client.close_deployment.assert_not_called()
    assert not active()
    capsys.readouterr()
