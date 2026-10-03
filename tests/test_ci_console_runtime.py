"""Exercise the real Console transport with synthetic one-job runtime secrets."""

import io
import json
import logging
import traceback
import urllib.error
from email.message import Message
from unittest.mock import MagicMock

import pytest

from just_akash.api import CONSOLE_HTTP_TIMEOUT, AkashAPIError, AkashConsoleAPI, CIConsoleAPI

JIT = "synthetic-jit-config-DO-NOT-LOG"
KEY = "synthetic-controller-key-DO-NOT-LOG"
SDL = f"services:\n  runner:\n    env:\n      - RUNNER_JIT_CONFIG={JIT}\n"


def assert_private(caplog, error=None):
    visible = caplog.text
    if error is not None:
        visible += str(error) + repr(error) + "".join(traceback.format_exception(error))
        if isinstance(error, AkashAPIError):
            visible += error.body + error.error_name
    assert JIT not in visible
    assert KEY not in visible


def test_create_sends_exact_runtime_payload_without_logging_values(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="akash.api")
    response = MagicMock()
    response.status = 200
    response.read.return_value = json.dumps({"data": {"dseq": "123", JIT: KEY}}).encode()
    response.__enter__.return_value = response
    transport = MagicMock(return_value=response)
    monkeypatch.setattr("urllib.request.urlopen", transport)

    result = CIConsoleAPI(KEY).create_deployment(SDL, deposit=0.5)

    transport.assert_called_once()
    request = transport.call_args.args[0]
    assert request.method == "POST"
    assert request.full_url == "https://console-api.akash.network/v1/deployments"
    assert request.get_header("X-api-key") == KEY
    assert json.loads(request.data) == {"data": {"sdl": SDL, "deposit": 0.5}}
    assert transport.call_args.kwargs == {"timeout": CONSOLE_HTTP_TIMEOUT}
    assert result == {"dseq": "123", JIT: KEY}
    assert "body_bytes=" in caplog.text
    assert "response_bytes=" in caplog.text
    assert_private(caplog)


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"message": SDL, "error_name": JIT}),
        json.dumps({JIT: KEY}),
        json.dumps([SDL, KEY]),
        f"<html>{SDL}{KEY}</html>",
    ],
)
def test_echoed_http_errors_are_safe_to_report_without_retry(monkeypatch, caplog, body):
    caplog.set_level(logging.DEBUG, logger="akash.api")
    transport = MagicMock(
        side_effect=urllib.error.HTTPError(
            f"https://console.invalid/{JIT}", 502, KEY, Message(), io.BytesIO(body.encode())
        )
    )
    monkeypatch.setattr("urllib.request.urlopen", transport)

    with pytest.raises(AkashAPIError) as caught:
        CIConsoleAPI(KEY).create_deployment(SDL)

    assert caught.value.status == 502
    assert caught.value.body == ""
    assert caught.value.error_name == ""
    assert not caught.value.is_upstream_timeout()
    assert "reconcile create outcome" in str(caught.value)
    transport.assert_called_once()
    assert_private(caplog, caught.value)


def test_timeout_metadata_survives_redaction_without_authorizing_retry(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="akash.api")
    body = json.dumps(
        {
            "message": SDL,
            "error_name": "origin_response_timeout",
            "retryable": True,
            "retry_after": 0,
        }
    )
    transport = MagicMock(
        side_effect=urllib.error.HTTPError(
            "https://console.invalid", 500, KEY, Message(), io.BytesIO(body.encode())
        )
    )
    monkeypatch.setattr("urllib.request.urlopen", transport)
    with pytest.raises(AkashAPIError) as caught:
        CIConsoleAPI(KEY).create_deployment(SDL)
    assert caught.value.status == 500
    assert caught.value.is_upstream_timeout()
    assert caught.value.retryable is True
    assert caught.value.retry_after == 0
    transport.assert_called_once()
    assert_private(caplog, caught.value)


@pytest.mark.parametrize(
    ("failure", "exception_type"),
    [(TimeoutError(JIT), TimeoutError), (urllib.error.URLError(KEY + JIT), RuntimeError)],
)
def test_transport_error_details_do_not_escape(monkeypatch, caplog, failure, exception_type):
    caplog.set_level(logging.DEBUG, logger="akash.api")
    transport = MagicMock(side_effect=failure)
    monkeypatch.setattr("urllib.request.urlopen", transport)
    with pytest.raises(exception_type) as caught:
        CIConsoleAPI(KEY).create_deployment(SDL)
    assert not isinstance(caught.value, AkashAPIError)
    transport.assert_called_once()
    assert_private(caplog, caught.value)


@pytest.mark.parametrize("provider", [False, True])
def test_missing_jwt_does_not_echo_response(monkeypatch, caplog, provider):
    caplog.set_level(logging.DEBUG, logger="akash.api")
    response = MagicMock()
    response.status = 200
    response.read.return_value = json.dumps({"data": {"echo": KEY + JIT}}).encode()
    response.__enter__.return_value = response
    monkeypatch.setattr("urllib.request.urlopen", MagicMock(return_value=response))
    client = CIConsoleAPI(KEY)
    with pytest.raises(RuntimeError, match="JWT token not found") as caught:
        if provider:
            client.create_jwt_with_provider("123", "akash1synthetic", scope=["logs"])
        else:
            client.create_jwt("123", scope=["logs"])
    assert_private(caplog, caught.value)


def test_legacy_error_contract_is_preserved_but_payload_logging_is_removed(monkeypatch, caplog):
    """Positive control: the compatibility client still exposes its error body."""
    caplog.set_level(logging.DEBUG, logger="akash.api")
    body = json.dumps({"message": f"already exists {JIT}"})
    monkeypatch.setattr(
        "urllib.request.urlopen",
        MagicMock(
            side_effect=urllib.error.HTTPError(
                "https://console.invalid", 409, "Conflict", Message(), io.BytesIO(body.encode())
            )
        ),
    )
    with pytest.raises(AkashAPIError) as caught:
        AkashConsoleAPI(KEY).create_deployment(SDL)
    assert str(caught.value) == f"API Error (409): already exists {JIT}"
    assert caught.value.body == body
    assert_private(caplog)
