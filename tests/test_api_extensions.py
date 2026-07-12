"""Tests for the v1.6 API-coverage additions to just_akash.api.

Covers: update_deployment (PUT), deposit_deployment, deployment-settings
(GET/POST/PATCH + set_auto_top_up upsert), and the JWT scope parameter.
"""

from unittest.mock import patch

import pytest

from just_akash.api import AkashConsoleAPI

# ── update_deployment (PUT /v1/deployments/{dseq}) ───────────────────


class TestUpdateDeployment:
    @patch.object(AkashConsoleAPI, "_request")
    def test_update_sends_put_with_sdl(self, mock_req):
        mock_req.return_value = {"data": {"dseq": "12345", "state": "active"}}
        client = AkashConsoleAPI("key")
        result = client.update_deployment("12345", "sdl-body")
        mock_req.assert_called_once_with(
            "PUT", "/v1/deployments/12345", {"data": {"sdl": "sdl-body"}}
        )
        assert result["dseq"] == "12345"

    @patch.object(AkashConsoleAPI, "_request")
    def test_update_unwraps_bare_dict(self, mock_req):
        mock_req.return_value = {"dseq": "999", "state": "active"}
        client = AkashConsoleAPI("key")
        assert client.update_deployment("999", "x")["dseq"] == "999"

    @patch.object(AkashConsoleAPI, "_request")
    def test_update_non_dict_response(self, mock_req):
        mock_req.return_value = ["unexpected"]
        client = AkashConsoleAPI("key")
        assert client.update_deployment("1", "x") == {}

    @patch.object(AkashConsoleAPI, "_request")
    def test_update_coerces_dseq_to_str(self, mock_req):
        mock_req.return_value = {"data": {}}
        client = AkashConsoleAPI("key")
        client.update_deployment(12345, "x")  # type: ignore[arg-type]
        assert mock_req.call_args[0][1] == "/v1/deployments/12345"


# ── deposit_deployment (POST /v1/deposit-deployment) ─────────────────


class TestDepositDeployment:
    @patch.object(AkashConsoleAPI, "_request")
    def test_deposit_body_and_path(self, mock_req):
        mock_req.return_value = {"data": {"dseq": "12345"}}
        client = AkashConsoleAPI("key")
        client.deposit_deployment("12345", 0.5)
        mock_req.assert_called_once_with(
            "POST", "/v1/deposit-deployment", {"data": {"dseq": "12345", "deposit": 0.5}}
        )

    @patch.object(AkashConsoleAPI, "_request")
    def test_deposit_coerces_dseq_to_str(self, mock_req):
        mock_req.return_value = {"data": {}}
        client = AkashConsoleAPI("key")
        client.deposit_deployment(12345, 2.0)  # type: ignore[arg-type]
        body = mock_req.call_args[0][2]
        assert body["data"]["dseq"] == "12345"
        assert body["data"]["deposit"] == 2.0

    @patch.object(AkashConsoleAPI, "_request")
    def test_deposit_non_dict_response(self, mock_req):
        mock_req.return_value = None
        client = AkashConsoleAPI("key")
        assert client.deposit_deployment("1", 1.0) == {}


# ── deployment-settings (auto top-up) ────────────────────────────────


class TestDeploymentSettings:
    @patch.object(AkashConsoleAPI, "_request")
    def test_get_settings_path(self, mock_req):
        mock_req.return_value = {"data": {"autoTopUpEnabled": True}}
        client = AkashConsoleAPI("key")
        result = client.get_deployment_settings("12345")
        mock_req.assert_called_once_with("GET", "/v2/deployment-settings/12345")
        assert result["autoTopUpEnabled"] is True

    @patch.object(AkashConsoleAPI, "_request")
    def test_get_settings_404_returns_empty(self, mock_req):
        mock_req.side_effect = RuntimeError("API Error (404): not found")
        client = AkashConsoleAPI("key")
        assert client.get_deployment_settings("12345") == {}

    @patch.object(AkashConsoleAPI, "_request")
    def test_get_settings_other_error_reraises(self, mock_req):
        mock_req.side_effect = RuntimeError("API Error (500): boom")
        client = AkashConsoleAPI("key")
        with pytest.raises(RuntimeError, match="500"):
            client.get_deployment_settings("12345")

    @patch.object(AkashConsoleAPI, "_request")
    def test_get_settings_non_404_with_404_substring_in_dseq_must_reraise(self, mock_req):
        # A genuine HTTP 400 whose body happens to contain "404" as a substring
        # of a dseq (e.g. 40400) must NOT be misread as "no settings yet". The
        # 404-detection uses a naive `"404" in str(e)` substring test, so this
        # error is silently swallowed and returned as {} instead of re-raising.
        mock_req.side_effect = RuntimeError("API Error (400): invalid dseq 40400")
        client = AkashConsoleAPI("key")
        with pytest.raises(RuntimeError, match="400"):
            client.get_deployment_settings("40400")

    @patch.object(AkashConsoleAPI, "_request")
    def test_get_settings_500_containing_not_found_phrase_must_reraise(self, mock_req):
        # A real server error (HTTP 500) whose body contains the words
        # "not found" (e.g. an internal lookup failure) is a hard failure that
        # must surface, not be treated as "settings unset". The casing-insensitive
        # "not found" substring check wrongly swallows it as {}.
        mock_req.side_effect = RuntimeError(
            "API Error (500): deployment record Not Found in escrow ledger"
        )
        client = AkashConsoleAPI("key")
        with pytest.raises(RuntimeError, match="500"):
            client.get_deployment_settings("12345")

    @patch.object(AkashConsoleAPI, "_request")
    def test_create_settings_body(self, mock_req):
        mock_req.return_value = {"data": {"autoTopUpEnabled": True}}
        client = AkashConsoleAPI("key")
        client.create_deployment_settings("12345", True)
        mock_req.assert_called_once_with(
            "POST",
            "/v2/deployment-settings",
            {"data": {"dseq": "12345", "autoTopUpEnabled": True}},
        )

    @patch.object(AkashConsoleAPI, "_request")
    def test_update_settings_body_and_path(self, mock_req):
        mock_req.return_value = {"data": {"autoTopUpEnabled": False}}
        client = AkashConsoleAPI("key")
        client.update_deployment_settings("12345", False)
        mock_req.assert_called_once_with(
            "PATCH",
            "/v2/deployment-settings/12345",
            {"data": {"autoTopUpEnabled": False}},
        )


class TestSetAutoTopUp:
    @patch.object(AkashConsoleAPI, "update_deployment_settings")
    @patch.object(AkashConsoleAPI, "create_deployment_settings")
    @patch.object(AkashConsoleAPI, "get_deployment_settings")
    def test_upsert_patches_when_settings_exist(self, mock_get, mock_create, mock_update):
        mock_get.return_value = {"autoTopUpEnabled": False}
        client = AkashConsoleAPI("key")
        client.set_auto_top_up("12345", True)
        mock_update.assert_called_once_with("12345", True)
        mock_create.assert_not_called()

    @patch.object(AkashConsoleAPI, "update_deployment_settings")
    @patch.object(AkashConsoleAPI, "create_deployment_settings")
    @patch.object(AkashConsoleAPI, "get_deployment_settings")
    def test_upsert_creates_when_no_settings(self, mock_get, mock_create, mock_update):
        mock_get.return_value = {}
        client = AkashConsoleAPI("key")
        client.set_auto_top_up("12345", True)
        mock_create.assert_called_once_with("12345", True)
        mock_update.assert_not_called()

    @patch.object(AkashConsoleAPI, "_request")
    def test_upsert_creates_when_settings_endpoint_returns_data_null(self, mock_req):
        # A deployment with no settings yet can be reported as {"data": null}
        # (the key is present, the value is None) rather than a 404 or an empty
        # object. get_deployment_settings unwraps data=None to the *raw wrapper*
        # {"data": None}, which is truthy, so set_auto_top_up's `if existing:`
        # routes to PATCH (update an existing record) instead of POST (create).
        # The PATCH targets a settings record that does not exist -> wrong verb,
        # and on a real server a 404. The upsert must CREATE here.
        def _route(method, endpoint, data=None):
            if method == "GET":
                return {"data": None}  # "no settings yet"
            return {"data": {"autoTopUpEnabled": True}}

        mock_req.side_effect = _route
        client = AkashConsoleAPI("key")
        client.set_auto_top_up("12345", True)
        methods = [call.args[0] for call in mock_req.call_args_list]
        # After the GET, the upsert must POST (create), never PATCH (update).
        assert "PATCH" not in methods, f"upsert wrongly PATCHed non-existent settings: {methods}"
        assert "POST" in methods

    @patch.object(AkashConsoleAPI, "_request")
    def test_settings_data_null_returns_empty_dict_not_wrapper(self, mock_req):
        # When the settings response is {"data": null}, get_deployment_settings
        # must return {} ("unset") so callers' truthiness checks behave. Instead
        # it returns the raw wrapper {"data": None} because data is None (not a
        # dict) and the fallback hands back the whole response.
        mock_req.return_value = {"data": None}
        client = AkashConsoleAPI("key")
        result = client.get_deployment_settings("12345")
        assert result == {}, f"expected {{}} for data=null, got {result!r}"
        # And the wrapper must not leak through as a truthy "settings exist".
        assert not result


# ── JWT scope parameter ──────────────────────────────────────────────


class TestJwtScope:
    @patch.object(AkashConsoleAPI, "_request")
    def test_create_jwt_defaults_to_shell(self, mock_req):
        mock_req.return_value = {"data": {"token": "jwt"}}
        client = AkashConsoleAPI("key")
        assert client.create_jwt("12345") == "jwt"
        body = mock_req.call_args[0][2]
        assert body["data"]["leases"]["scope"] == ["shell"]

    @patch.object(AkashConsoleAPI, "_request")
    def test_create_jwt_custom_scope(self, mock_req):
        mock_req.return_value = {"data": {"token": "jwt"}}
        client = AkashConsoleAPI("key")
        client.create_jwt("12345", scope=["logs"])
        body = mock_req.call_args[0][2]
        assert body["data"]["leases"]["scope"] == ["logs"]

    @patch.object(AkashConsoleAPI, "_request")
    def test_empty_scope_is_not_silently_widened_to_shell(self, mock_req):
        """An explicit empty scope must pass through, not become ["shell"].

        `scope or ["shell"]` treated `[]` as falsy and substituted shell -- so a
        caller asking for no permissions would have been handed shell access. An
        omitted scope still defaults; an explicit `[]` does not.
        """
        mock_req.return_value = {"data": {"token": "jwt"}}
        client = AkashConsoleAPI("key")
        client.create_jwt("12345", scope=[])
        assert mock_req.call_args[0][2]["data"]["leases"]["scope"] == []
        client.create_jwt_with_provider("12345", "akash1prov", scope=[])
        perm = mock_req.call_args[0][2]["data"]["leases"]["permissions"][0]
        assert perm["scope"] == []

    @patch.object(AkashConsoleAPI, "_request")
    def test_create_jwt_requests_scoped_access(self, mock_req):
        """The Console API rejects any /leases access other than scoped or granular.

        This body previously paired access "full" with a scope, which the API answers
        with a 400 ('"access" at "/leases" must be scoped') on EVERY call -- so the
        fallback could never mint a token. The old tests missed it because they mocked
        _request and only ever asserted `scope`, never `access`: the one field that was
        wrong was the one nothing looked at.
        """
        mock_req.return_value = {"data": {"token": "jwt"}}
        client = AkashConsoleAPI("key")
        client.create_jwt("12345")
        leases = mock_req.call_args[0][2]["data"]["leases"]
        assert leases["access"] == "scoped"
        # "scoped" carries a scope and must NOT name deployments (AEP-64 rejects that).
        assert "deployments" not in leases

    @patch.object(AkashConsoleAPI, "_request")
    def test_create_jwt_with_provider_uses_granular_scoped_permission(self, mock_req):
        """Provider-scoped grants are granular at the top, scoped per permission.

        AEP-64 forbids a scoped permission from naming deployments, so assert we do not
        smuggle a dseq in: that would make the token invalid, not more precise.
        """
        mock_req.return_value = {"data": {"token": "jwt"}}
        client = AkashConsoleAPI("key")
        client.create_jwt_with_provider("12345", "akash1prov", scope=["shell"])
        leases = mock_req.call_args[0][2]["data"]["leases"]
        assert leases["access"] == "granular"
        (perm,) = leases["permissions"]
        assert perm["access"] == "scoped"
        assert perm["provider"] == "akash1prov"
        assert perm["scope"] == ["shell"]
        assert "deployments" not in perm

    @patch.object(AkashConsoleAPI, "_request")
    def test_create_jwt_with_provider_custom_scope(self, mock_req):
        mock_req.return_value = {"data": {"token": "jwt"}}
        client = AkashConsoleAPI("key")
        client.create_jwt_with_provider("12345", "akash1prov", scope=["events"])
        body = mock_req.call_args[0][2]
        perm = body["data"]["leases"]["permissions"][0]
        assert perm["provider"] == "akash1prov"
        assert perm["scope"] == ["events"]

    @patch.object(AkashConsoleAPI, "_request")
    def test_create_jwt_with_provider_defaults_to_shell(self, mock_req):
        mock_req.return_value = {"data": {"token": "jwt"}}
        client = AkashConsoleAPI("key")
        client.create_jwt_with_provider("12345", "akash1prov")
        perm = mock_req.call_args[0][2]["data"]["leases"]["permissions"][0]
        assert perm["scope"] == ["shell"]
