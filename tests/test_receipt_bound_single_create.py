"""Offline source-derived create seam, real receipt writer and Console HTTP parser.

Auction/funding imports are inert fakes; deploy stops before manifest processing.
This is not full SDK/CLI qualification. No native I/O is performed.
"""

from __future__ import annotations

import ast
import inspect
import logging
import math
import sys
import time
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from just_akash import deployment_receipt as receipt
from just_akash.api import AkashAPIError, AkashConsoleAPI
from just_akash.deployment_receipt import decode_receipt

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
SDL = "profiles:\n  placement:\n    test: {}\ndeployment:\n  runner:\n    test: {}\n"
SOURCE = Path(__file__).resolve().parents[1] / "just_akash" / "deploy.py"
deploy_module = None


@pytest.fixture(autouse=True)
def _isolated_create_seam(monkeypatch):
    global deploy_module
    function = next(
        n
        for n in ast.parse(SOURCE.read_text()).body
        if isinstance(n, ast.FunctionDef) and n.name == "deploy"
    )
    stop = next(
        i
        for i, n in enumerate(function.body)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_manifest_raw" for t in n.targets)
    )
    function.body = function.body[:stop] + [
        ast.Return(ast.Name("deployment_response", ast.Load()))
    ]
    module = types.ModuleType("_create_seam")
    module.__dict__.update(
        {
            "__package__": "just_akash",
            "math": math,
            "time": time,
            "logging": logging,
            "AkashConsoleAPI": AkashConsoleAPI,
            "AkashAPIError": AkashAPIError,
            "_resolve_selection": lambda *_args: None,
            "AuctionPolicy": lambda **_kwargs: None,
            "_resolve_tier": lambda *_args: [],
            "_log": lambda *_args: None,
            "_resolve_sdl_path": lambda path, _gpu: path,
            "_prepare_sdl_content": lambda *_args, **_kwargs: SDL,
            "_check_wallet_credit": lambda *_args: None,
            "prepare_receipt": receipt.prepare_receipt,
            "mark_submitting": receipt.mark_submitting,
            "mark_create_response_received": receipt.mark_create_response_received,
            "credential_binding_for": receipt.credential_binding_for,
            "_close_stale_for_retry": lambda *_args, **_kwargs: None,
            "_report_suspected_orphans": lambda *_args, **_kwargs: None,
            "emit": lambda *_args, **_kwargs: None,
            "_RUN_ID": "synthetic",
            "Code": types.SimpleNamespace(DEPLOY_CREATE_FAILED=1),
        }
    )
    wallet = types.ModuleType("just_akash.wallet_pool")
    wallet.select_client_for_create = lambda *_args, **kwargs: types.SimpleNamespace(
        client=kwargs["client_factory"]("synthetic"), configured_keys=1
    )
    wallet.configured_api_keys = lambda: ["synthetic-key"]
    monkeypatch.setitem(sys.modules, wallet.__name__, wallet)
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
            str(SOURCE),
            "exec",
        ),
        module.__dict__,
    )  # noqa: S102 - source-derived seam
    deploy_module = module
    yield
    deploy_module = None


def _private_dir(path):
    path.mkdir(mode=0o700)
    return path


def _receipt_arguments(_sdl, path):
    return {"receipt_path": str(path), "receipt_operation_id": "synthetic-operation"}


# These controls exercise the source-derived create seam and real Console transport.
def _single_create_case(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from just_akash.api import AkashConsoleAPI

    monkeypatch.setenv("AKASH_API_KEY", "synthetic-key")
    monkeypatch.delenv("AKASH_API_KEYS", raising=False)
    client = AkashConsoleAPI("synthetic-key")
    monkeypatch.setattr(client, "account_address", lambda: OWNER)
    monkeypatch.setattr(deploy_module, "AkashConsoleAPI", lambda *_args: client)
    cleanup = Mock()
    monkeypatch.setattr(deploy_module, "_close_stale_for_retry", cleanup)
    monkeypatch.setattr(deploy_module, "_report_suspected_orphans", Mock())
    sdl = tmp_path / "source.yaml"
    sdl.write_text(SDL)
    path = _private_dir(tmp_path / "private") / "receipt.json"
    return client, cleanup, sdl, path


def _already_exists_transport(path, snapshots=None):
    import io
    import urllib.error

    def send(request, **_kwargs):
        assert request.method == "POST"
        assert request.full_url.endswith("/v1/deployments")
        assert decode_receipt(path.read_bytes())["state"] == "submitting"
        if snapshots is not None:
            snapshots.append(path.read_bytes())
        raise urllib.error.HTTPError(
            request.full_url, 409, "synthetic", {}, io.BytesIO(b'{"message":"already exists"}')
        )

    return send


def test_receipt_already_exists_refuses_without_cleanup_or_second_http_post(tmp_path, monkeypatch):
    import just_akash.api as api

    _, cleanup, sdl, path = _single_create_case(tmp_path, monkeypatch)
    snapshots = []
    with (
        patch.object(
            api.urllib.request, "urlopen", side_effect=_already_exists_transport(path, snapshots)
        ) as send,
        pytest.raises(RuntimeError, match="Receipt-bound create already exists") as caught,
    ):
        deploy_module.deploy(str(sdl), **_receipt_arguments(SDL, path))
    assert send.call_count == 1
    cleanup.assert_not_called()
    assert caught.value.__cause__.__class__ is api.AkashAPIError
    durable = decode_receipt(path.read_bytes())
    assert durable["state"] == "submitting"
    assert "dseq" not in durable
    assert snapshots == [path.read_bytes()]


def test_non_receipt_already_exists_keeps_legacy_retry(tmp_path, monkeypatch):
    from unittest.mock import Mock

    client, cleanup, sdl, path = _single_create_case(tmp_path, monkeypatch)
    client.create_deployment = Mock(
        side_effect=[RuntimeError("already exists"), RuntimeError("synthetic stop")]
    )
    with pytest.raises(RuntimeError, match="Failed to create deployment after retry"):
        deploy_module.deploy(str(sdl))
    assert client.create_deployment.call_count == 2
    cleanup.assert_called_once()
    assert not path.exists()


def test_receipt_native_timeout_retains_original_exception_and_submitting(tmp_path, monkeypatch):
    import just_akash.api as api

    _, cleanup, sdl, path = _single_create_case(tmp_path, monkeypatch)
    lost = TimeoutError("synthetic lost acknowledgement")
    with (
        patch.object(api.urllib.request, "urlopen", side_effect=lost) as send,
        pytest.raises(TimeoutError) as caught,
    ):
        deploy_module.deploy(str(sdl), **_receipt_arguments(SDL, path))
    assert caught.value is lost
    assert send.call_count == 1
    cleanup.assert_not_called()
    assert decode_receipt(path.read_bytes())["state"] == "submitting"


@pytest.mark.parametrize("kind", ["http503", "urlerror", "body-timeout"])
def test_lower_transport_failures_do_not_retry(tmp_path, monkeypatch, kind):
    import io
    import urllib.error
    from unittest.mock import MagicMock

    import just_akash.api as api

    _, cleanup, sdl, path = _single_create_case(tmp_path, monkeypatch)
    if kind == "http503":
        effect = urllib.error.HTTPError(
            "https://console-api.akash.network/v1/deployments",
            503,
            "synthetic",
            {},
            io.BytesIO(b'{"message":"synthetic","retryable":true}'),
        )
        expected = RuntimeError
    elif kind == "urlerror":
        effect = urllib.error.URLError("synthetic")
        expected = RuntimeError
    else:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = TimeoutError("synthetic body timeout")
        response.status = 200
        effect = None
        expected = TimeoutError
    with (
        patch.object(
            api.urllib.request,
            "urlopen",
            side_effect=effect,
            return_value=response if kind == "body-timeout" else None,
        ) as send,
        pytest.raises(expected),
    ):
        deploy_module.deploy(str(sdl), **_receipt_arguments(SDL, path))
    assert send.call_count == 1
    cleanup.assert_not_called()
    assert decode_receipt(path.read_bytes())["state"] == "submitting"


def test_existing_receipt_internal_redeploy_guard_precedes_delete_and_create():
    from unittest.mock import Mock

    tree = ast.parse(SOURCE.read_text())
    outer = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "deploy")
    nested = next(
        n
        for n in ast.walk(outer)
        if isinstance(n, ast.FunctionDef) and n.name == "_redeploy_and_reselect"
    )
    client = Mock()
    namespace = {"prepared_receipt": object(), "dseq": "123", "client": client}
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[nested], type_ignores=[])),
            str(SOURCE),
            "exec",
        ),
        namespace,
    )  # noqa: S102 - actual nested guard
    with pytest.raises(RuntimeError, match="receipt mode refuses internal re-deploy"):
        namespace[nested.name]("synthetic")
    client.close_deployment.assert_not_called()
    client.create_deployment.assert_not_called()


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_stdlib_redirect_handler_never_reposts_create(code):
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        "https://example.invalid/create", data=b"synthetic", method="POST"
    )
    handler = urllib.request.HTTPRedirectHandler()
    if code in {307, 308}:
        with pytest.raises(urllib.error.HTTPError):
            handler.redirect_request(
                request, None, code, "synthetic", {}, "https://example.invalid/other"
            )
    else:
        redirected = handler.redirect_request(
            request, None, code, "synthetic", {}, "https://example.invalid/other"
        )
        assert redirected.get_method() == "GET"
        assert redirected.data is None


def test_receipt_write_refusal_prevents_native_http_post(tmp_path, monkeypatch):
    import just_akash.api as api

    _, _, sdl, path = _single_create_case(tmp_path, monkeypatch)
    with (
        patch.object(deploy_module, "mark_submitting", side_effect=OSError("synthetic storage")),
        patch.object(api.urllib.request, "urlopen") as send,
        pytest.raises(OSError),
    ):
        deploy_module.deploy(str(sdl), **_receipt_arguments(SDL, path))
    send.assert_not_called()
    assert decode_receipt(path.read_bytes())["state"] == "prepared"


def test_receipt_known_response_persists_before_later_failure(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    import just_akash.api as api

    _, cleanup, sdl, path = _single_create_case(tmp_path, monkeypatch)
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b'{"data":{"dseq":"12345","manifest":""}}'
    response.status = 200
    original = deploy_module.mark_create_response_received

    def persist_then_stop(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("synthetic post-persistence stop")

    with (
        patch.object(api.urllib.request, "urlopen", return_value=response) as send,
        patch.object(deploy_module, "mark_create_response_received", persist_then_stop),
        pytest.raises(RuntimeError, match="NON-RETRYABLE CREATE OUTCOME AMBIGUOUS"),
    ):
        deploy_module.deploy(str(sdl), **_receipt_arguments(SDL, path))
    assert send.call_count == 1
    cleanup.assert_not_called()
    durable = decode_receipt(path.read_bytes())
    assert durable["state"] == "create_response_received"
    assert durable["dseq"] == "12345"
    assert durable["authority"] == "candidate"


def test_removing_receipt_already_exists_guard_reaches_second_post(tmp_path, monkeypatch):
    import just_akash.api as api

    source = inspect.getsource(deploy_module.deploy)
    guard = (
        "            if prepared_receipt is not None:\n"
        "                raise RuntimeError(\n"
        '                    "Receipt-bound create already exists; reconcile the recorded "\n'
        '                    "operation before retrying"\n'
        "                ) from e\n"
    )
    assert source.count(guard) == 1
    namespace = dict(vars(deploy_module))
    _, cleanup, sdl, path = _single_create_case(tmp_path, monkeypatch)
    namespace.update(vars(deploy_module))
    exec(source.replace(guard, "", 1), namespace)  # noqa: S102 - effect mutation
    with (
        patch.object(
            api.urllib.request, "urlopen", side_effect=_already_exists_transport(path)
        ) as send,
        pytest.raises(RuntimeError, match="Failed to create deployment after retry"),
    ):
        namespace["deploy"](str(sdl), **_receipt_arguments(SDL, path))
    assert send.call_count == 2
    cleanup.assert_called_once()
    assert decode_receipt(path.read_bytes())["state"] == "submitting"
