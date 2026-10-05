"""Private SDL diagnostics never trust response/error echoes as printable text."""

from __future__ import annotations

import errno
import io
import json
import logging
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

# Synthetic credential only. Never load operator environment/files in these tests.
SECRET = "synthetic-reader-echo-canary"
PRIVATE = SDL_YAML.replace(
    "    expose:",
    f"    credentials:\n      host: docker.io\n      username: jobordu\n"
    f"      password: {SECRET}\n    expose:",
)


def _http(monkeypatch, body, status=400):
    def fail(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://console.invalid", status, SECRET, Message(), io.BytesIO(body)
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
    _http(monkeypatch, json.dumps({"message": SECRET, "error_name": SECRET}).encode())
    with pytest.raises(AkashAPIError) as caught:
        getattr(AkashConsoleAPI("fake-console-key"), method)(*args)
    error = caught.value
    assert error.status == 400 and error.body == "" and error.error_name == ""
    assert error.__cause__ is None and error.__suppress_context__
    assert SECRET not in _visible(error, caplog, capsys)


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
    _http(monkeypatch, json.dumps({"message": f"{marker}: {SECRET}"}).encode())
    with pytest.raises(AkashAPIError) as caught:
        AkashConsoleAPI("fake-console-key").create_deployment(PRIVATE)
    assert marker in str(caught.value).lower()
    assert SECRET not in _visible(caught.value, caplog, capsys)


def test_upstream_fields_are_preserved_without_trusting_echoes(monkeypatch, caplog, capsys):
    _http(
        monkeypatch,
        json.dumps(
            {
                "message": SECRET,
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
    assert SECRET not in _visible(error, caplog, capsys)


@pytest.mark.parametrize(
    "raw", [b"not-json synthetic-reader-echo-canary", b"\xffsynthetic-reader-echo-canary"]
)
def test_malformed_private_error_body_is_still_an_http_verdict(raw, monkeypatch, caplog, capsys):
    _http(monkeypatch, raw, status=500)
    with pytest.raises(AkashAPIError) as caught:
        AkashConsoleAPI("fake-console-key").create_deployment(PRIVATE)
    assert caught.value.status == 500
    assert SECRET not in _visible(caught.value, caplog, capsys)


@pytest.mark.parametrize(
    "failure,expected",
    [
        (TimeoutError(SECRET), TimeoutError),
        (TimeoutError(errno.ETIMEDOUT, SECRET), TimeoutError),
        (ConnectionResetError(errno.ECONNRESET, SECRET, SECRET), ConnectionResetError),
        (urllib.error.URLError(SECRET), RuntimeError),
        (ConnectionResetError(SECRET), ConnectionResetError),
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
    assert SECRET not in _visible(caught.value, caplog, capsys)


def test_successful_private_request_preserves_wire_and_result_not_debug_echo(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.status = 200
    response.read.return_value = json.dumps({SECRET: SECRET, "manifest": PRIVATE}).encode()
    seen = []

    def send(request, **kwargs):
        seen.append((json.loads(request.data), kwargs))
        return response

    monkeypatch.setattr("urllib.request.urlopen", send)
    result = AkashConsoleAPI("fake-console-key").create_deployment(PRIVATE)
    assert result["manifest"] == PRIVATE  # Needed unchanged for the lease request.
    assert seen[0][0]["data"]["sdl"] == PRIVATE
    assert seen[0][1]["timeout"] == 180.0
    assert SECRET not in caplog.text


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
    client.create_deployment.return_value = {"dseq": "42", "manifest": PRIVATE, SECRET: SECRET}
    client.get_bids.return_value = [_make_bid("akash1provider", 100)]
    client.create_lease.return_value = {"data": {"lease": "created"}}
    client.account_address.return_value = OWNER
    path = tmp_path / "private.yaml"
    path.write_text(PRIVATE)
    return client, str(path)


def test_private_success_does_not_print_full_create_response(private_deploy, caplog, capsys):
    client, path = private_deploy
    result = dp.deploy(path, bid_wait=2, bid_wait_retry=2)
    assert result["dseq"] == "42"
    assert client.create_lease.call_args.kwargs["manifest"] == PRIVATE
    assert SECRET not in caplog.text + str(capsys.readouterr())
    assert not active()


def test_missing_dseq_never_echoes_response_or_key_names(private_deploy, caplog, capsys):
    client, path = private_deploy
    client.create_deployment.return_value = {"manifest": PRIVATE, SECRET: SECRET}
    with pytest.raises(RuntimeError) as caught:
        dp.deploy(path)
    assert client.create_deployment.call_count == 1
    assert "NO_DSEQ_RETURNED" in capsys.readouterr().err
    assert SECRET not in _visible(caught.value, caplog, capsys)
    assert not active()


@pytest.mark.parametrize(
    "error",
    [
        AkashAPIError(SECRET, status=524, body=SECRET, retryable=True, error_name=SECRET),
        TimeoutError(SECRET),
    ],
)
def test_private_ambiguous_create_never_replays(private_deploy, error, caplog, capsys):
    client, path = private_deploy
    client.create_deployment.side_effect = error
    with pytest.raises((RuntimeError, TimeoutError)) as caught:
        dp.deploy(path)
    assert client.create_deployment.call_count == 1
    assert SECRET not in _visible(caught.value, caplog, capsys)
    assert not active()


def test_existing_already_exists_retry_is_preserved(private_deploy, monkeypatch, caplog, capsys):
    client, path = private_deploy
    client.create_deployment.side_effect = [
        RuntimeError(f"already exists {SECRET}"),
        {"dseq": "42", "manifest": PRIVATE},
    ]
    cleanup = Mock()
    monkeypatch.setattr(dp, "_close_stale_for_retry", cleanup)
    assert dp.deploy(path, bid_wait=2, bid_wait_retry=2)["dseq"] == "42"
    assert client.create_deployment.call_count == 2 and cleanup.call_count == 1
    assert SECRET not in caplog.text + str(capsys.readouterr())


def test_private_readback_and_lease_cleanup_errors_are_sanitized(private_deploy, caplog, capsys):
    client, path = private_deploy
    client.create_lease.side_effect = RuntimeError(SECRET)
    client.close_deployment.side_effect = RuntimeError(SECRET)
    with pytest.raises(RuntimeError) as caught:
        dp.deploy(path, bid_wait=2, bid_wait_retry=2)
    assert client.create_deployment.call_count == 1
    client.close_deployment.assert_called_once_with("42")
    assert SECRET not in _visible(caught.value, caplog, capsys)


def test_receipt_failure_remains_nonretryable_without_error_echo(
    private_deploy, tmp_path, monkeypatch, caplog, capsys
):
    client, path = private_deploy
    Path(path).write_text(
        SDL.replace("    image:", f"    credentials: {{password: {SECRET}}}\n    image:", 1)
    )
    parent = tmp_path / "receipt-dir"
    parent.mkdir(mode=0o700)
    receipt = parent / "receipt.json"

    def fail(*_args, **_kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(dp, "mark_create_response_received", fail)
    with pytest.raises(RuntimeError, match="NON-RETRYABLE CREATE OUTCOME AMBIGUOUS") as caught:
        dp.deploy(path, receipt_path=str(receipt), receipt_operation_id="test-create-42")
    assert client.create_deployment.call_count == 1
    assert json.loads(receipt.read_text())["state"] == "submitting"
    assert SECRET not in _visible(caught.value, caplog, capsys)


def test_private_context_resets_and_public_http_contract_stays_exact(tmp_path, monkeypatch):
    path = tmp_path / "private.yaml"
    path.write_text(PRIVATE)

    @sdl_operation
    def fail(sdl_path):
        assert active()
        raise ValueError(SECRET)

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
    Path(path).write_text(f'credentials: ["{SECRET}"\n')
    with pytest.raises(RuntimeError) as caught:
        dp.deploy(path)
    client.create_deployment.assert_not_called()
    assert SECRET not in _visible(caught.value, caplog, capsys)


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
    client.create_deployment.side_effect = RuntimeError(SECRET)
    with pytest.raises(RuntimeError) as caught:
        dp.deploy(path)
    assert client.create_deployment.call_args.args[0] == PRIVATE
    assert SECRET not in _visible(caught.value, caplog, capsys)
    assert not active()


def test_reader_env_override_is_private_even_when_base_sdl_is_public(
    private_deploy, caplog, capsys
):
    client, path = private_deploy
    Path(path).write_text(SDL_YAML)
    client.create_deployment.side_effect = RuntimeError(SECRET)
    with pytest.raises(RuntimeError) as caught:
        dp.deploy(path, env_vars=[f"DOCKERHUB_PULL_TOKEN={SECRET}"])
    assert f"DOCKERHUB_PULL_TOKEN={SECRET}" in client.create_deployment.call_args.args[0]
    assert SECRET not in _visible(caught.value, caplog, capsys)


def test_private_update_uses_same_logging_boundary_without_creating(
    private_deploy, caplog, capsys
):
    client, path = private_deploy
    client.update_deployment.side_effect = RuntimeError(SECRET)
    with pytest.raises(RuntimeError) as caught:
        dp.update("42", path)
    client.create_deployment.assert_not_called()
    assert client.update_deployment.call_args.args == ("42", PRIVATE)
    assert SECRET not in _visible(caught.value, caplog, capsys)
    assert not active()


def test_existing_private_lease_auth_retry_keeps_same_identity(private_deploy, caplog, capsys):
    client, path = private_deploy
    client.create_lease.side_effect = [
        RuntimeError(f"JWT has invalid claims: {SECRET}"),
        {"data": {"lease": "created"}},
    ]
    result = dp.deploy(path, bid_wait=2, bid_wait_retry=2)
    assert result["dseq"] == "42" and client.create_deployment.call_count == 1
    calls = [call.kwargs for call in client.create_lease.call_args_list]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert SECRET not in caplog.text + str(capsys.readouterr())


def test_existing_private_stale_bid_retry_keeps_order_and_moves_provider(
    private_deploy, caplog, capsys
):
    client, path = private_deploy
    client.get_bids.return_value = [
        _make_bid("akash1provider", 100),
        _make_bid("akash1other", 200),
    ]
    client.create_lease.side_effect = [
        RuntimeError(f"no longer open: {SECRET}"),
        {"data": {"lease": "created"}},
    ]
    result = dp.deploy(path, bid_wait=2, bid_wait_retry=2)
    assert result["dseq"] == "42" and client.create_deployment.call_count == 1
    calls = [call.kwargs for call in client.create_lease.call_args_list]
    assert [call["dseq"] for call in calls] == ["42", "42"]
    assert {call["provider"] for call in calls} == {"akash1provider", "akash1other"}
    assert calls[0]["provider"] != calls[1]["provider"]
    assert SECRET not in caplog.text + str(capsys.readouterr())


def test_existing_private_no_order_redeploy_does_not_echo_second_response(
    private_deploy, caplog, capsys
):
    client, path = private_deploy
    client.create_deployment.side_effect = [
        {"dseq": "42", "manifest": PRIVATE},
        {"manifest": PRIVATE, SECRET: SECRET},
    ]
    client.create_lease.side_effect = RuntimeError(f"no lease for deployment: {SECRET}")
    with pytest.raises(RuntimeError) as caught:
        dp.deploy(path, bid_wait=2, bid_wait_retry=2)
    assert client.create_deployment.call_count == 2  # The existing, scoped recovery only.
    client.close_deployment.assert_called_once_with("42")
    assert SECRET not in _visible(caught.value, caplog, capsys)


def test_private_response_receipt_survives_later_bid_failure(
    private_deploy, tmp_path, caplog, capsys
):
    client, path = private_deploy
    Path(path).write_text(
        SDL.replace("    image:", f"    credentials: {{password: {SECRET}}}\n    image:", 1)
    )
    client.get_bids.side_effect = RuntimeError(SECRET)
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
    assert SECRET not in receipt.read_text()
    assert SECRET not in _visible(caught.value, caplog, capsys)


def test_main_ci_runtime_client_keeps_stronger_marker_free_protection(monkeypatch, caplog, capsys):
    from just_akash.api import CIConsoleAPI

    caplog.set_level(logging.DEBUG)
    client = CIConsoleAPI("synthetic-controller-key")
    body = json.dumps({"message": f"already exists; no longer open; {SECRET}"}).encode()

    def fail(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://console.invalid", 409, SECRET, Message(), io.BytesIO(body)
        )

    monkeypatch.setattr(client, "_open_request", fail)
    with pytest.raises(AkashAPIError) as caught:
        client.create_deployment(PRIVATE)
    assert (
        str(caught.value)
        == "API Error (409): CI runtime response omitted; reconcile create outcome"
    )
    assert "already exists" not in str(caught.value)
    assert "no longer open" not in str(caught.value)
    assert caught.value.status == 409 and caught.value.body == ""
    assert SECRET not in _visible(caught.value, caplog, capsys)


def test_main_receipt_private_already_exists_refuses_replay_and_stale_sweep(
    private_deploy, tmp_path, monkeypatch, caplog, capsys
):
    client, path = private_deploy
    Path(path).write_text(
        SDL.replace("    image:", f"    credentials: {{password: {SECRET}}}\n    image:", 1)
    )
    parent = tmp_path / "receipt-dir"
    parent.mkdir(mode=0o700)
    receipt = parent / "receipt.json"
    client.create_deployment.side_effect = RuntimeError(f"already exists {SECRET}")
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
    assert SECRET not in _visible(caught.value, caplog, capsys)


@pytest.mark.parametrize("identity", [SECRET, "0", "01", "-1", "１２３", str(2**64), True, 42.0])
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
    assert SECRET not in _visible(caught.value, caplog, capsys)


def test_invalid_private_create_identity_keeps_submitting_receipt(
    private_deploy, tmp_path, caplog, capsys
):
    client, path = private_deploy
    Path(path).write_text(
        SDL.replace("    image:", f"    credentials: {{password: {SECRET}}}\n    image:", 1)
    )
    parent = tmp_path / "receipt-dir"
    parent.mkdir(mode=0o700)
    receipt = parent / "receipt.json"
    client.create_deployment.return_value = {"dseq": SECRET, "manifest": PRIVATE}
    with pytest.raises(RuntimeError, match="NON-RETRYABLE CREATE OUTCOME AMBIGUOUS") as caught:
        dp.deploy(path, receipt_path=str(receipt), receipt_operation_id="test-create-42")
    assert client.create_deployment.call_count == 1
    assert json.loads(receipt.read_text())["state"] == "submitting"
    client.get_bids.assert_not_called()
    assert SECRET not in _visible(caught.value, caplog, capsys)


def test_invalid_private_redeploy_identity_is_not_logged_or_polled(private_deploy, caplog, capsys):
    client, path = private_deploy
    client.create_deployment.side_effect = [
        {"dseq": "42", "manifest": PRIVATE},
        {"dseq": SECRET, "manifest": PRIVATE},
    ]
    client.create_lease.side_effect = RuntimeError("no lease for deployment")
    with pytest.raises(RuntimeError, match="NON-RETRYABLE CREATE OUTCOME AMBIGUOUS") as caught:
        dp.deploy(path, bid_wait=2, bid_wait_retry=2)
    assert client.create_deployment.call_count == 2
    assert {call.args[0] for call in client.get_bids.call_args_list} == {"42"}
    client.close_deployment.assert_called_once_with("42")
    assert SECRET not in _visible(caught.value, caplog, capsys)


def test_private_bid_and_final_metadata_echo_is_withheld_without_rewriting_transport(
    private_deploy, caplog, capsys
):
    client, path = private_deploy
    bid = _make_bid(SECRET, 100, denom=SECRET)
    client.get_bids.return_value = [bid]
    client.account_address.return_value = SECRET
    result = dp.deploy(path, bid_wait=2, bid_wait_retry=2)
    assert result["provider"] == SECRET
    assert client.create_lease.call_args.kwargs["provider"] == SECRET
    assert client.create_lease.call_args.kwargs["manifest"] == PRIVATE
    assert bid["id"]["provider"] == SECRET and bid["price"]["denom"] == SECRET
    assert SECRET not in caplog.text + str(capsys.readouterr())


def test_private_foreign_bid_state_echo_is_withheld_even_before_tier_filtering(
    private_deploy, caplog, capsys
):
    client, path = private_deploy
    bid = _make_bid(SECRET, 100, denom=SECRET)
    bid["state"] = SECRET
    client.get_bids.return_value = [bid]
    with pytest.raises(RuntimeError) as caught:
        dp.deploy(path, bid_wait=2, bid_wait_retry=2, preferred_providers=[OWNER])
    assert client.create_deployment.call_count == 1
    assert SECRET not in _visible(caught.value, caplog, capsys)


def test_private_bid_table_state_and_structured_diagnostic_context_do_not_echo(
    tmp_path, caplog, capsys
):
    caplog.set_level(logging.DEBUG)
    path = tmp_path / "private.yaml"
    path.write_text(PRIVATE)
    bid = _make_bid(SECRET, 100, denom=SECRET)
    bid["state"] = SECRET

    @sdl_operation
    def describe(sdl_path):
        dp._log_bid_table([bid], "TEST")
        dp.emit(
            "NO_DSEQ_RETURNED",
            "error",
            SECRET,
            provider=SECRET,
            account=SECRET,
            dseq=SECRET,
            states=[SECRET],
        )

    describe(str(path))
    assert SECRET not in caplog.text + str(capsys.readouterr())
    assert bid["state"] == SECRET


def test_private_followup_endpoint_echo_is_hidden_without_rewriting_wire(
    tmp_path, monkeypatch, caplog, capsys
):
    caplog.set_level(logging.DEBUG)
    path = tmp_path / "private.yaml"
    path.write_text(PRIVATE)
    sent = []

    def fail(request, **_kwargs):
        sent.append(request.full_url)
        raise urllib.error.HTTPError("url", 400, SECRET, Message(), io.BytesIO(SECRET.encode()))

    monkeypatch.setattr("urllib.request.urlopen", fail)

    @sdl_operation
    def read(sdl_path):
        return AkashConsoleAPI("fake-console-key")._request("GET", f"/v1/bids/{SECRET}")

    with pytest.raises(AkashAPIError) as caught:
        read(str(path))
    assert SECRET in sent[0]
    assert SECRET not in _visible(caught.value, caplog, capsys)


def test_valid_private_metadata_stays_visible_and_public_metadata_remains_unchanged(
    tmp_path, caplog, capsys
):
    from just_akash._confidential import canonical_dseq, display

    assert canonical_dseq("18446744073709551615")
    assert canonical_dseq(42)
    assert not canonical_dseq("18446744073709551616")
    path = tmp_path / "private.yaml"
    path.write_text(PRIVATE)

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
    assert display(SECRET, "address") == SECRET
    assert display(SECRET, "state") == SECRET
    assert display(SECRET, "denom") == SECRET


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
    Path(path).write_text(SDL_YAML)
    public_result = dp.deploy(path, bid_wait=2, bid_wait_retry=2, preferred_providers=candidates)
    assert public_result == private_result
    assert client.create_lease.call_args.kwargs == private_lease
    assert client.create_deployment.call_count == 1
    client.close_deployment.assert_not_called()
    assert not active()
    capsys.readouterr()
