"""Offline SDK boundary tests: no Console/network/credential setup."""

import hashlib
import hmac
import io
import json
import traceback
import urllib.error
from unittest.mock import Mock

import pytest

from just_akash import ci_run_budget as budget
from just_akash.api import AkashConsoleAPI, CIConsoleAPI

PRIVATE = "SYNTH_PRIVATE_SDL_TOKEN_KEY"
SDL = f"services:\n  runner:\n    env: [{PRIVATE}]\n"
CREDENTIAL = {"console_api_key": PRIVATE, "console_origin": "https://console-api.akash.network"}


@pytest.fixture(autouse=True)
def closed_environment(monkeypatch):
    for key in (
        "GITHUB_ACTIONS",
        "AKASH_CI_BUDGET_REQUIRED",
        "AKASH_CI_BUDGET_URL",
        "AKASH_CI_BUDGET_AUDIENCE",
        "AKASH_CI_BUDGET_ACCOUNT_ID",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "urllib.request.OpenerDirector.open", Mock(side_effect=AssertionError(PRIVATE))
    )


def configure(monkeypatch):
    monkeypatch.setenv("AKASH_CI_BUDGET_URL", "https://budget.example")
    monkeypatch.setenv("AKASH_CI_BUDGET_AUDIENCE", "ci-budget-audience")
    monkeypatch.setenv("AKASH_CI_BUDGET_ACCOUNT_ID", "primary")


def success(body):
    return {
        "dseq": "123",
        "manifest": PRIVATE,
        "operation_id": body["operation_id"],
        "account_id": body["account_id"],
        "deposit_usd": body["deposit_usd"],
        "unit": budget.UNIT,
        "request_digest": budget.request_digest(body),
    }


def test_real_sdk_routes_to_one_proxy_post_without_console_key(monkeypatch, caplog):
    configure(monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    calls = []

    def exchange(request, cap):
        calls.append(request)
        assert cap == budget.MAX_RESPONSE
        assert request.full_url == "https://budget.example" + budget.CREATE_PATH
        assert request.method == "POST"
        assert request.get_header("Authorization") == "Bearer oidc-token"
        assert request.get_header("X-api-key") is None
        body = json.loads(request.data)
        assert body == {
            "operation_id": "intent-1",
            "account_id": "primary",
            "sdl_content": SDL,
            "deposit_usd": "0.5",
            "console_credential_proof": hmac.new(
                PRIVATE.encode("ascii"),
                b"akash-ci-console-create-v1\0" + budget.request_digest(body).encode("ascii"),
                hashlib.sha256,
            ).hexdigest(),
        }
        return success(body)

    monkeypatch.setattr(budget, "_exchange", exchange)
    monkeypatch.setattr(budget, "_token", lambda audience: "oidc-token")
    result = AkashConsoleAPI(PRIVATE).create_deployment(SDL, 0.5, ci_operation_id="intent-1")
    assert result == {"dseq": "123", "manifest": PRIVATE}
    assert len(calls) == 1
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("client,github", [(CIConsoleAPI, "false"), (AkashConsoleAPI, "true")])
def test_ci_cannot_disable_budget_or_create_without_config(monkeypatch, client, github):
    monkeypatch.setenv("GITHUB_ACTIONS", github)
    monkeypatch.setenv("AKASH_CI_BUDGET_ENABLED", "false")
    with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_CONFIG_REQUIRED"):
        client(PRIVATE).create_deployment(SDL, ci_operation_id="intent-1")


def test_missing_intent_refuses_before_oidc(monkeypatch):
    configure(monkeypatch)
    token = Mock(side_effect=AssertionError(PRIVATE))
    with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_INVALID_INTENT"):
        budget.CIRunBudgetClient(token_source=token).create(SDL, 1, None, **CREDENTIAL)
    token.assert_not_called()


def test_local_legacy_create_preserved(monkeypatch):
    client = AkashConsoleAPI(PRIVATE)
    request = Mock(return_value={"data": {"dseq": "123"}})
    monkeypatch.setattr(client, "_request", request)
    assert client.create_deployment(SDL, 5) == {"dseq": "123"}
    request.assert_called_once_with(
        "POST", "/v1/deployments", {"data": {"sdl": SDL, "deposit": 5}}
    )


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("POST", "/v1/deployments", {"data": {"sdl": SDL}}),
        ("POST", "/v1/deposit-deployment", {"data": {"deposit": 1}}),
        ("POST", "/v2/deployment-settings", {"data": {"autoTopUpEnabled": True}}),
        ("PATCH", "/v2/deployment-settings/123", {"data": {"autoTopUpEnabled": "false"}}),
        ("PUT", "/v2/deployment-settings/123", None),
    ],
)
def test_direct_spend_bypasses_refuse_before_transport(method, path, data):
    with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_DIRECT_MUTATION_REFUSED"):
        CIConsoleAPI(PRIVATE)._request(method, path, data)


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("GET", "/v1/deployments", None),
        ("DELETE", "/v1/deployments/123", None),
        ("PATCH", "/v2/deployment-settings/123", {"data": {"autoTopUpEnabled": False}}),
    ],
)
def test_read_cleanup_disable_transport_remains_available(monkeypatch, method, path, data):
    response = Mock()
    response.read.return_value = b'{"data":{}}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    transport = Mock(return_value=response)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", transport)
    CIConsoleAPI(PRIVATE)._request(method, path, data)
    transport.assert_called_once()


@pytest.mark.parametrize(
    "value,expected", [(1, "1"), (5.0, "5"), ("0.500000", "0.5"), ("1e-6", "0.000001")]
)
def test_exact_canonical_usd(value, expected):
    assert budget.canonical_deposit(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        0,
        -1,
        "NaN",
        "Infinity",
        "1e-7",
        "1e9999999",
        0.1 + 0.2,
        "1.000000000000000000000000001",
    ],
)
def test_invalid_amount_never_rounds(value):
    with pytest.raises(budget.CIRunBudgetError):
        budget.canonical_deposit(value)


def test_server_counter_boundary_is_exact_usd_not_uact():
    assert budget.canonical_deposit("9223372036854.775807") == "9223372036854.775807"
    with pytest.raises(budget.CIRunBudgetError):
        budget.canonical_deposit("9223372036854.775808")


@pytest.mark.parametrize(
    "status,code",
    [
        (409, "CI_BUDGET_OPERATION_REPLAY"),
        (402, "CI_BUDGET_EXHAUSTED"),
        (502, "CI_BUDGET_OUTCOME_UNKNOWN"),
        (503, "CI_BUDGET_POLICY_UNAVAILABLE"),
    ],
)
def test_real_http_refusal_terminal_without_read_retry_or_private_errors(
    monkeypatch, status, code
):
    configure(monkeypatch)
    stream = io.BytesIO(PRIVATE.encode())
    error = urllib.error.HTTPError(
        "https://budget.example/" + PRIVATE, status, PRIVATE, {}, stream
    )
    transport = Mock(side_effect=error)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", transport)
    client = budget.CIRunBudgetClient(token_source=lambda _: "oidc-token")
    with pytest.raises(budget.CIRunBudgetError) as caught:
        client.create(SDL, 1, "intent-1", **CREDENTIAL)
    assert caught.value.code == code
    assert caught.value._is_non_retryable is True
    assert PRIVATE not in "".join(traceback.format_exception(caught.value))
    assert vars(caught.value) == {"code": code}
    assert stream.closed
    transport.assert_called_once()


@pytest.mark.parametrize(
    "change",
    [
        {"request_digest": "0" * 64},
        {"operation_id": "other"},
        {"deposit_usd": "1.0"},
        {"unit": "uact"},
        {"manifest": {}},
        {"manifest": ""},
        {"dseq": "01"},
        {"extra_private": PRIVATE},
    ],
)
def test_response_binding_closed_schema(monkeypatch, change):
    configure(monkeypatch)

    def exchange(request, cap):
        result = success(json.loads(request.data))
        result.update(change)
        return result

    with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_RESPONSE_REFUSED"):
        budget.CIRunBudgetClient(exchange=exchange, token_source=lambda _: "oidc").create(
            SDL, 1, "op", **CREDENTIAL
        )


@pytest.mark.parametrize(
    "dseq,accepted", [(str(2**64 - 1), True), (str(2**64), False), ("9" * 20, False)]
)
def test_dseq_uint64_boundary_no_retry(monkeypatch, dseq, accepted):
    configure(monkeypatch)
    calls = []

    def exchange(request, cap):
        calls.append(request)
        result = success(json.loads(request.data))
        result["dseq"] = dseq
        return result

    client = budget.CIRunBudgetClient(exchange=exchange, token_source=lambda _: "oidc")
    if accepted:
        assert client.create(SDL, 1, "op", **CREDENTIAL) == {"dseq": dseq, "manifest": PRIVATE}
    else:
        with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_RESPONSE_REFUSED"):
            client.create(SDL, 1, "op", **CREDENTIAL)
    assert len(calls) == 1


def test_native_oidc_exact_audience_no_console_credential(monkeypatch):
    monkeypatch.setenv(
        "ACTIONS_ID_TOKEN_REQUEST_URL", "https://runner.actions.githubusercontent.com/id?x=1"
    )
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", PRIVATE)
    calls = []

    def exchange(request, cap):
        calls.append(request)
        assert request.full_url.endswith("?x=1&audience=exact-aud")
        assert request.method == "GET"
        assert request.get_header("Authorization") == "Bearer " + PRIVATE
        assert cap == 16384
        return {"value": "oidc"}

    monkeypatch.setattr(budget, "_exchange", exchange)
    assert budget._token("exact-aud") == "oidc"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'{"value":"a","value":"b"}',
        b'{"value":NaN}',
        b"\xff",
        b"x" * (budget.MAX_RESPONSE + 1),
    ],
)
def test_real_transport_parser_and_cap_refuse(monkeypatch, raw):
    response = Mock(status=200)
    response.read.return_value = raw
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", Mock(return_value=response))
    configure(monkeypatch)
    with pytest.raises(budget.CIRunBudgetError):
        budget.CIRunBudgetClient(token_source=lambda _: "oidc").create(SDL, 1, "op", **CREDENTIAL)
    response.read.assert_called_once_with(budget.MAX_RESPONSE + 1)


def test_redirect_closed_without_read():
    stream = Mock()
    with pytest.raises(budget.CIRunBudgetError):
        budget._NoRedirect().redirect_request(None, stream, 302, PRIVATE, {}, PRIVATE)
    stream.close.assert_called_once()
    stream.read.assert_not_called()


def test_same_intent_replay_cannot_duplicate_console_create(monkeypatch):
    configure(monkeypatch)
    physical_creates = []
    budget_posts = []

    def server(request, cap):
        body = json.loads(request.data)
        budget_posts.append(body["operation_id"])
        if body["operation_id"] in physical_creates:
            raise budget.CIRunBudgetError("CI_BUDGET_OPERATION_REPLAY")
        physical_creates.append(body["operation_id"])
        return success(body)

    monkeypatch.setattr(budget, "_exchange", server)
    monkeypatch.setattr(budget, "_token", lambda _: "oidc")
    client = CIConsoleAPI(PRIVATE)
    assert client.create_deployment(SDL, 1, ci_operation_id="stable") == {
        "dseq": "123",
        "manifest": PRIVATE,
    }
    with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_OPERATION_REPLAY"):
        client.create_deployment(SDL, 1, ci_operation_id="stable")
    assert budget_posts == ["stable", "stable"]
    assert physical_creates == ["stable"]  # Explicit server fixture, not runtime ledger proof.


@pytest.mark.parametrize(
    "origin",
    [
        "http://budget.example",
        "https://user@budget.example",
        "https://budget.example/other",
        "https://budget.example?x=1",
    ],
)
def test_config_refused_before_any_token_or_post(monkeypatch, origin):
    configure(monkeypatch)
    monkeypatch.setenv("AKASH_CI_BUDGET_URL", origin)
    token = Mock(side_effect=AssertionError(PRIVATE))
    with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_CONFIG_REQUIRED"):
        budget.CIRunBudgetClient(token_source=token).create(SDL, 1, "op", **CREDENTIAL)
    token.assert_not_called()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://attacker.example/id",
        "http://runner.actions.githubusercontent.com/id",
        "https://runner.actions.githubusercontent.com/id?audience=other",
    ],
)
def test_oidc_untrusted_origin_or_audience_refuses_without_transport(monkeypatch, endpoint):
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", endpoint)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", PRIVATE)
    with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_OIDC_REFUSED"):
        budget._token("exact-audience")


@pytest.mark.parametrize("flag", ["true", "1", "on"])
def test_unmarked_canary_process_requires_budget_before_create(monkeypatch, flag):
    monkeypatch.setenv("AKASH_CI_BUDGET_REQUIRED", flag)
    client = AkashConsoleAPI(PRIVATE)
    assert budget.ci_required(client) is True
    with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_CONFIG_REQUIRED"):
        client.create_deployment(SDL, ci_operation_id="intent")
    with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_DIRECT_MUTATION_REFUSED"):
        client._request("POST", "/v1/deployments", {"data": {"sdl": SDL}})
    with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_DIRECT_MUTATION_REFUSED"):
        client.deposit_deployment("123", 1)
    with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_DIRECT_MUTATION_REFUSED"):
        client.update_deployment_settings("123", True)


@pytest.mark.parametrize("flag", [None, "false", "0", "off"])
def test_nonci_process_false_or_unset_preserves_legacy(monkeypatch, flag):
    if flag is not None:
        monkeypatch.setenv("AKASH_CI_BUDGET_REQUIRED", flag)
    configure(monkeypatch)  # Configuration and credential presence do not establish CI mode.
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", PRIVATE)
    client = AkashConsoleAPI(PRIVATE)
    request = Mock(return_value={"data": {"dseq": "123"}})
    monkeypatch.setattr(client, "_request", request)
    assert budget.ci_required(client) is False
    assert client.create_deployment(SDL, 1) == {"dseq": "123"}
    request.assert_called_once_with(
        "POST", "/v1/deployments", {"data": {"sdl": SDL, "deposit": 1}}
    )


@pytest.mark.parametrize("flag", ["", "TRUE", " true", "False", "no", PRIVATE])
def test_malformed_process_flag_is_closed_terminal_before_transport(monkeypatch, flag, caplog):
    monkeypatch.setenv("AKASH_CI_BUDGET_REQUIRED", flag)
    with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_CONFIG_REQUIRED") as caught:
        AkashConsoleAPI(PRIVATE).create_deployment(SDL, ci_operation_id="intent")
    assert caught.value._is_non_retryable is True
    assert PRIVATE not in caplog.text + "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("flag", ["false", "0", "off"])
@pytest.mark.parametrize("kind", ["actions", "marked"])
def test_false_process_flag_cannot_override_actions_or_marked_client(monkeypatch, flag, kind):
    monkeypatch.setenv("AKASH_CI_BUDGET_REQUIRED", flag)
    if kind == "actions":
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
    client = CIConsoleAPI(PRIVATE) if kind == "marked" else AkashConsoleAPI(PRIVATE)
    assert budget.ci_required(client) is True
    with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_CONFIG_REQUIRED"):
        client.create_deployment(SDL, ci_operation_id="intent")


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("GET", "/v1/deployments", None),
        ("DELETE", "/v1/deployments/123", None),
        ("PATCH", "/v2/deployment-settings/123", {"data": {"autoTopUpEnabled": False}}),
    ],
)
def test_malformed_mode_does_not_prevent_read_cleanup_or_disable(monkeypatch, method, path, data):
    monkeypatch.setenv("AKASH_CI_BUDGET_REQUIRED", PRIVATE)
    response = Mock(status=200)
    response.read.return_value = b'{"data":{}}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    transport = Mock(return_value=response)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", transport)
    assert CIConsoleAPI(PRIVATE)._request(method, path, data) == {"data": {}}
    transport.assert_called_once()


def test_unmarked_mandatory_process_policy_refusal_has_no_fallback(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("AKASH_CI_BUDGET_REQUIRED", "true")
    monkeypatch.setattr(budget, "_token", lambda _audience: "oidc")
    stream = io.BytesIO(PRIVATE.encode())
    transport = Mock(
        side_effect=urllib.error.HTTPError("https://budget.example", 503, PRIVATE, {}, stream)
    )
    monkeypatch.setattr("urllib.request.OpenerDirector.open", transport)
    with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_POLICY_UNAVAILABLE"):
        AkashConsoleAPI(PRIVATE).create_deployment(SDL, 1, ci_operation_id="intent")
    transport.assert_called_once()
    assert transport.call_args.args[0].full_url == "https://budget.example" + budget.CREATE_PATH
    assert stream.closed


@pytest.mark.parametrize(
    "key,origin",
    [
        ("", budget.CONSOLE_ORIGIN),
        (None, budget.CONSOLE_ORIGIN),
        (" key", budget.CONSOLE_ORIGIN),
        ("key\n", budget.CONSOLE_ORIGIN),
        ("é", budget.CONSOLE_ORIGIN),
        ("k" * 4097, budget.CONSOLE_ORIGIN),
        (PRIVATE, "https://console-api.akash.network/"),
        (PRIVATE, "http://console-api.akash.network"),
        (PRIVATE, "https://another.invalid"),
    ],
)
def test_selected_console_origin_key_fail_before_oidc_or_proxy(monkeypatch, key, origin):
    configure(monkeypatch)
    token = Mock(side_effect=AssertionError(PRIVATE))
    exchange = Mock(side_effect=AssertionError(PRIVATE))
    with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_CONFIG_REQUIRED"):
        budget.CIRunBudgetClient(token_source=token, exchange=exchange).create(
            SDL, 1, "intent", console_api_key=key, console_origin=origin
        )
    token.assert_not_called()
    exchange.assert_not_called()


def test_missing_selected_credential_is_not_defaulted(monkeypatch):
    configure(monkeypatch)
    token = Mock(side_effect=AssertionError(PRIVATE))
    client = budget.CIRunBudgetClient(token_source=token)
    with pytest.raises(TypeError):
        client.create(SDL, 1, "intent")
    token.assert_not_called()


def test_sdk_uses_selected_key_not_ambient_key_and_never_returns_proof(monkeypatch, caplog):
    configure(monkeypatch)
    monkeypatch.setenv("AKASH_CI_BUDGET_REQUIRED", "true")
    monkeypatch.setenv("AKASH_API_KEY", "different-env-key")
    monkeypatch.setattr(budget, "_token", lambda _audience: "oidc")
    observed = []

    def server(request, _cap):
        body = json.loads(request.data)
        observed.append(body)
        expected = hmac.new(
            PRIVATE.encode("ascii"),
            b"akash-ci-console-create-v1\0" + budget.request_digest(body).encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        assert body["console_credential_proof"] == expected
        assert set(body) == {
            "operation_id",
            "account_id",
            "sdl_content",
            "deposit_usd",
            "console_credential_proof",
        }
        return success(body)

    monkeypatch.setattr(budget, "_exchange", server)
    result = AkashConsoleAPI(PRIVATE).create_deployment(SDL, 1, ci_operation_id="intent")
    assert set(result) == {"dseq", "manifest"}
    assert PRIVATE not in caplog.text
    assert observed[0]["console_credential_proof"] not in caplog.text
    assert observed[0]["console_credential_proof"] not in result.values()


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", "different-intent"),
        ("account_id", "different-alias"),
        ("sdl_content", SDL + "\n"),
        ("deposit_usd", "2"),
    ],
)
def test_proof_binds_every_original_intent_field(monkeypatch, field, value):
    configure(monkeypatch)
    bodies = []

    def server(request, _cap):
        body = json.loads(request.data)
        bodies.append(body)
        return success(body)

    client = budget.CIRunBudgetClient(exchange=server, token_source=lambda _: "oidc")
    client.create(SDL, 1, "intent", **CREDENTIAL)
    changed = dict(bodies[0])
    changed[field] = value
    expected = hmac.new(
        PRIVATE.encode("ascii"),
        b"akash-ci-console-create-v1\0" + budget.request_digest(changed).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    assert expected != bodies[0]["console_credential_proof"]
    changed["console_credential_proof"] = "0" * 64
    assert budget.request_digest(changed) == budget.request_digest(
        {k: v for k, v in changed.items() if k != "console_credential_proof"}
    )


def test_alias_resolver_key_mismatch_refuses_without_console_fallback(monkeypatch, caplog):
    configure(monkeypatch)
    monkeypatch.setenv("AKASH_CI_BUDGET_REQUIRED", "true")
    monkeypatch.setattr(budget, "_token", lambda _: "oidc")
    requests = []
    physical_posts = []

    def server(request, _cap):
        requests.append(request)
        body = json.loads(request.data)
        server_selected_key = b"other-account-key"
        expected = hmac.new(
            server_selected_key,
            b"akash-ci-console-create-v1\0" + budget.request_digest(body).encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, body["console_credential_proof"]):
            raise budget.CIRunBudgetError("CI_BUDGET_OUTCOME_UNKNOWN")
        physical_posts.append(body["operation_id"])
        return success(body)

    monkeypatch.setattr(budget, "_exchange", server)
    with pytest.raises(budget.CIRunBudgetError) as caught:
        AkashConsoleAPI(PRIVATE).create_deployment(SDL, 1, ci_operation_id="intent")
    assert len(requests) == 1
    assert physical_posts == []  # Explicit resolver fixture; not live account/ledger evidence.
    assert PRIVATE not in caplog.text + "".join(traceback.format_exception(caught.value))
    assert json.loads(requests[0].data)["console_credential_proof"] not in str(caught.value)


@pytest.mark.parametrize(
    "drift",
    [
        "header",
        "attribute",
        "missing",
        "duplicate_same",
        "duplicate_different",
        "case_alias_wrong",
        "invalid_headers",
    ],
)
def test_actual_client_wire_key_drift_refuses_before_token_or_post(monkeypatch, drift):
    configure(monkeypatch)
    monkeypatch.setenv("AKASH_CI_BUDGET_REQUIRED", "true")
    client = AkashConsoleAPI(PRIVATE)
    if drift == "header":
        client.headers["x-api-key"] = "different-header-key"
    elif drift == "attribute":
        client.api_key = "different-attribute-key"
    elif drift == "missing":
        del client.headers["x-api-key"]
    elif drift.startswith("duplicate"):
        client.headers["X-API-KEY"] = PRIVATE if drift == "duplicate_same" else "different"
    elif drift == "case_alias_wrong":
        del client.headers["x-api-key"]
        client.headers["X-API-KEY"] = "different-case-alias-key"
    else:
        client.headers = None
    token = Mock(side_effect=AssertionError(PRIVATE))
    exchange = Mock(side_effect=AssertionError(PRIVATE))
    monkeypatch.setattr(budget, "_token", token)
    monkeypatch.setattr(budget, "_exchange", exchange)
    with pytest.raises(budget.CIRunBudgetError, match="CI_BUDGET_CONFIG_REQUIRED"):
        client.create_deployment(SDL, 1, ci_operation_id="intent")
    token.assert_not_called()
    exchange.assert_not_called()


def test_single_matching_case_alias_is_same_wire_credential(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("AKASH_CI_BUDGET_REQUIRED", "true")
    client = AkashConsoleAPI(PRIVATE)
    client.headers["X-API-KEY"] = client.headers.pop("x-api-key")
    monkeypatch.setattr(budget, "_token", lambda _: "oidc")
    calls = []

    def server(request, _cap):
        calls.append(request)
        return success(json.loads(request.data))

    monkeypatch.setattr(budget, "_exchange", server)
    assert client.create_deployment(SDL, 1, ci_operation_id="intent") == {
        "dseq": "123",
        "manifest": PRIVATE,
    }
    assert len(calls) == 1


def test_nonci_legacy_create_does_not_gain_header_equality_requirement(monkeypatch):
    client = AkashConsoleAPI(PRIVATE)
    client.headers["x-api-key"] = "different-header-key"
    request = Mock(return_value={"data": {"dseq": "123"}})
    monkeypatch.setattr(client, "_request", request)
    assert client.create_deployment(SDL, 1) == {"dseq": "123"}
    request.assert_called_once_with(
        "POST", "/v1/deployments", {"data": {"sdl": SDL, "deposit": 1}}
    )
