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
