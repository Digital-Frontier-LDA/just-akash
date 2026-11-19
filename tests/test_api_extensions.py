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


class TestSetAutoTopUpVerifies:
    """The write is not evidence. Three canaries were created on 2026-08-08 with
    "Auto top-up enabled" logged for each, and all three had expired 13-15h later on a
    $2.00 escrow deposit -- and nothing could distinguish "on but insufficient" from
    "never really on", because the only evidence was a message we printed ourselves."""

    @patch.object(AkashConsoleAPI, "update_deployment_settings")
    @patch.object(AkashConsoleAPI, "get_deployment_settings")
    def test_raises_when_the_write_is_not_honoured(self, mock_get, mock_update):
        """The server accepts the PATCH and still reports the setting off."""
        mock_get.side_effect = [{"autoTopUpEnabled": False}, {"autoTopUpEnabled": False}]
        client = AkashConsoleAPI("key")
        with pytest.raises(RuntimeError, match="did not take"):
            client.set_auto_top_up("12345", True)

    @patch.object(AkashConsoleAPI, "update_deployment_settings")
    @patch.object(AkashConsoleAPI, "get_deployment_settings")
    def test_a_string_true_is_not_a_true(self, mock_get, mock_update):
        """This API is known to answer with the STRING "false" where a boolean belongs.
        A server loose enough to do that is not one whose "true" can be believed either,
        and bool("false") is True -- the read path was already hardened against exactly
        this. Consistency matters more than optimism here: an unparseable confirmation
        is not a confirmation."""
        mock_get.side_effect = [{"autoTopUpEnabled": False}, {"autoTopUpEnabled": "true"}]
        client = AkashConsoleAPI("key")
        with pytest.raises(RuntimeError, match="did not take"):
            client.set_auto_top_up("12345", True)

    @patch.object(AkashConsoleAPI, "update_deployment_settings")
    @patch.object(AkashConsoleAPI, "get_deployment_settings")
    def test_missing_settings_after_the_write_is_a_failure(self, mock_get, mock_update):
        """No settings record at all after enabling is not "probably fine"."""
        mock_get.side_effect = [{"autoTopUpEnabled": False}, {}]
        client = AkashConsoleAPI("key")
        with pytest.raises(RuntimeError, match="did not take"):
            client.set_auto_top_up("12345", True)

    @patch.object(AkashConsoleAPI, "update_deployment_settings")
    @patch.object(AkashConsoleAPI, "get_deployment_settings")
    def test_disabling_is_verified_too(self, mock_get, mock_update):
        """--off that silently stays on keeps spending money."""
        mock_get.side_effect = [{"autoTopUpEnabled": True}, {"autoTopUpEnabled": True}]
        client = AkashConsoleAPI("key")
        with pytest.raises(RuntimeError, match="did not take"):
            client.set_auto_top_up("12345", False)
        mock_get.side_effect = [{"autoTopUpEnabled": True}, {"autoTopUpEnabled": False}]
        client.set_auto_top_up("12345", False)  # must not raise

    @pytest.mark.parametrize("garbage", ["false", "true", None, 0, ""])
    @patch.object(AkashConsoleAPI, "update_deployment_settings")
    @patch.object(AkashConsoleAPI, "get_deployment_settings")
    def test_disable_needs_a_real_false_not_merely_a_non_true(
        self, mock_get, mock_update, garbage
    ):
        """The first version of the check demanded a real True to confirm an ENABLE but
        accepted anything non-True as a confirmed DISABLE, so "false", "true" and None
        all passed as verified. Same defect the method exists to remove, in the direction
        nobody was looking."""
        mock_get.side_effect = [{"autoTopUpEnabled": True}, {"autoTopUpEnabled": garbage}]
        client = AkashConsoleAPI("key")
        with pytest.raises(RuntimeError, match="did not take"):
            client.set_auto_top_up("12345", False)

    @patch.object(AkashConsoleAPI, "update_deployment_settings")
    @patch.object(AkashConsoleAPI, "get_deployment_settings")
    def test_the_message_names_the_right_consequence(self, mock_get, mock_update):
        """A failed disable does not drain escrow -- it keeps spending. Reusing the enable
        wording would send the reader after the wrong problem."""
        mock_get.side_effect = [{"autoTopUpEnabled": True}, {"autoTopUpEnabled": True}]
        client = AkashConsoleAPI("key")
        with pytest.raises(RuntimeError, match="keep charging"):
            client.set_auto_top_up("12345", False)
        mock_get.side_effect = [{"autoTopUpEnabled": False}, {"autoTopUpEnabled": False}]
        with pytest.raises(RuntimeError, match="escrow will drain"):
            client.set_auto_top_up("12345", True)

    @patch.object(AkashConsoleAPI, "update_deployment_settings")
    @patch.object(AkashConsoleAPI, "get_deployment_settings")
    def test_returns_the_write_result_when_verified(self, mock_get, mock_update):
        mock_update.return_value = {"autoTopUpEnabled": True, "dseq": "12345"}
        mock_get.side_effect = [{"autoTopUpEnabled": False}, {"autoTopUpEnabled": True}]
        client = AkashConsoleAPI("key")
        assert client.set_auto_top_up("12345", True)["autoTopUpEnabled"] is True


class TestSetAutoTopUp:
    @patch.object(AkashConsoleAPI, "update_deployment_settings")
    @patch.object(AkashConsoleAPI, "create_deployment_settings")
    @patch.object(AkashConsoleAPI, "get_deployment_settings")
    def test_upsert_patches_when_settings_exist(self, mock_get, mock_create, mock_update):
        # Two GETs now: the existence probe, then the verification read-back.
        mock_get.side_effect = [{"autoTopUpEnabled": False}, {"autoTopUpEnabled": True}]
        client = AkashConsoleAPI("key")
        client.set_auto_top_up("12345", True)
        mock_update.assert_called_once_with("12345", True)
        mock_create.assert_not_called()

    @patch.object(AkashConsoleAPI, "update_deployment_settings")
    @patch.object(AkashConsoleAPI, "create_deployment_settings")
    @patch.object(AkashConsoleAPI, "get_deployment_settings")
    def test_upsert_creates_when_no_settings(self, mock_get, mock_create, mock_update):
        mock_get.side_effect = [{}, {"autoTopUpEnabled": True}]
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
        gets = {"n": 0}

        def _route(method, endpoint, data=None):
            if method == "GET":
                gets["n"] += 1
                # First GET is the existence probe ("no settings yet"); the second is
                # the verification read-back, by which point the POST has landed.
                return {"data": None} if gets["n"] == 1 else {"data": {"autoTopUpEnabled": True}}
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


PROV = "akash1prov0000000000000000000000000000000000"


class TestJwtScope:
    """openmix-q2mfu: every JWT is ``granular`` and names its deployment (dseq, scope,
    services) under one provider. The Console refuses a project-limited key the old
    top-level ``scoped`` grant and the provider-wide permission (403 "Name the
    deployments the token is for", live 2026-11-19)."""

    PROV = PROV

    @staticmethod
    def _deployment(body):
        leases = body["data"]["leases"]
        assert leases["access"] == "granular"
        (perm,) = leases["permissions"]
        assert perm["access"] == "granular"
        (dep,) = perm["deployments"]
        return perm["provider"], dep

    @patch.object(AkashConsoleAPI, "_request")
    def test_create_jwt_defaults_to_shell(self, mock_req):
        mock_req.return_value = {"data": {"token": "jwt"}}
        client = AkashConsoleAPI("key")
        assert client.create_jwt("12345", provider=self.PROV, services=["web"]) == "jwt"
        _, dep = self._deployment(mock_req.call_args[0][2])
        assert dep["scope"] == ["shell"]

    @patch.object(AkashConsoleAPI, "_request")
    def test_create_jwt_custom_scope(self, mock_req):
        mock_req.return_value = {"data": {"token": "jwt"}}
        client = AkashConsoleAPI("key")
        client.create_jwt("12345", scope=["logs"], provider=self.PROV, services=["web"])
        _, dep = self._deployment(mock_req.call_args[0][2])
        assert dep["scope"] == ["logs"]

    @patch.object(AkashConsoleAPI, "_request")
    def test_empty_scope_is_not_silently_widened_to_shell(self, mock_req):
        """An explicit empty scope must pass through, not become ["shell"]."""
        mock_req.return_value = {"data": {"token": "jwt"}}
        client = AkashConsoleAPI("key")
        client.create_jwt("12345", scope=[], provider=self.PROV, services=["web"])
        assert self._deployment(mock_req.call_args[0][2])[1]["scope"] == []
        client.create_jwt_with_provider("12345", self.PROV, scope=[], services=["web"])
        assert self._deployment(mock_req.call_args[0][2])[1]["scope"] == []

    @patch.object(AkashConsoleAPI, "_request")
    def test_create_jwt_names_the_dseq_as_an_integer(self, mock_req):
        """The schema's dseq is an integer >= 1; the token is bound to it."""
        mock_req.return_value = {"data": {"token": "jwt"}}
        client = AkashConsoleAPI("key")
        client.create_jwt("12345", ttl=300, provider=self.PROV, services=["web", "db"])
        body = mock_req.call_args[0][2]
        assert body["data"]["ttl"] == 300
        provider, dep = self._deployment(body)
        assert provider == self.PROV
        assert dep == {"dseq": 12345, "scope": ["shell"], "services": ["web", "db"]}

    @pytest.mark.parametrize("dseq", ["0", "", "x1", "-1", None, True])
    @patch.object(AkashConsoleAPI, "_request")
    def test_there_is_no_dseq_less_jwt(self, mock_req, dseq):
        client = AkashConsoleAPI("key")
        with pytest.raises(ValueError, match="names the deployment"):
            client.create_jwt(dseq, provider=self.PROV, services=["web"])
        mock_req.assert_not_called()

    @patch.object(AkashConsoleAPI, "_request")
    def test_provider_and_services_are_read_from_the_lease_when_not_given(self, mock_req):
        lease = {
            "id": {"provider": self.PROV, "gseq": 1, "oseq": 2},
            "status": {"services": {"web": {}, "worker": {}}},
        }
        mock_req.side_effect = [{"data": {"leases": [lease]}}, {"data": {"token": "jwt"}}]
        client = AkashConsoleAPI("key")
        assert client.create_jwt("12345", scope=["status"]) == "jwt"
        assert mock_req.call_args_list[0][0][:2] == ("GET", "/v1/deployments/12345")
        provider, dep = self._deployment(mock_req.call_args_list[1][0][2])
        assert provider == self.PROV
        assert dep == {
            "dseq": 12345,
            "gseq": 1,
            "oseq": 2,
            "scope": ["status"],
            "services": ["web", "worker"],
        }

    @pytest.mark.parametrize(
        "leases",
        [
            [],
            [{"id": {}}],
            [{"id": {"provider": PROV}, "status": {"services": {}}}],
        ],
        ids=["no-lease", "no-provider", "no-service"],
    )
    @patch.object(AkashConsoleAPI, "_request")
    def test_a_lease_that_cannot_be_named_mints_nothing(self, mock_req, leases):
        mock_req.return_value = {"data": {"leases": leases}}
        client = AkashConsoleAPI("key")
        with pytest.raises(RuntimeError, match="no token can name"):
            client.create_jwt("12345")
        assert [c[0][0] for c in mock_req.call_args_list] == ["GET"]

    @patch.object(AkashConsoleAPI, "_request")
    def test_create_jwt_with_provider_is_the_same_granular_grant(self, mock_req):
        """It used to send a provider-wide ``scoped`` permission naming no deployment."""
        mock_req.return_value = {"data": {"token": "jwt"}}
        client = AkashConsoleAPI("key")
        client.create_jwt_with_provider("12345", self.PROV, scope=["events"], services=["web"])
        provider, dep = self._deployment(mock_req.call_args[0][2])
        assert provider == self.PROV
        assert dep == {"dseq": 12345, "scope": ["events"], "services": ["web"]}

    @patch.object(AkashConsoleAPI, "_request")
    def test_create_jwt_with_provider_defaults_to_shell(self, mock_req):
        mock_req.return_value = {"data": {"token": "jwt"}}
        client = AkashConsoleAPI("key")
        client.create_jwt_with_provider("12345", self.PROV, services=["web"])
        assert self._deployment(mock_req.call_args[0][2])[1]["scope"] == ["shell"]


# ── account_address (the key's own account; no JWT) ─────────────────


ME = {"data": {"userId": "u-1"}}
ADDR = "akash1" + "me".ljust(38, "0")


class TestAccountAddress:
    """openmix-q2mfu: account_address() reads GET /v1/user/me then GET /v1/wallets and mints
    no JWT (the dseq-0 token it used to mint is refused by the Console)."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_returns_the_users_wallet_address_and_mints_nothing(self, mock_req):
        mock_req.side_effect = [ME, {"data": [{"userId": "u-1", "address": ADDR}]}]
        assert AkashConsoleAPI("key").account_address() == ADDR
        calls = [c[0][:2] for c in mock_req.call_args_list]
        assert calls == [("GET", "/v1/user/me"), ("GET", "/v1/wallets?userId=u-1")]

    @pytest.mark.parametrize("me", [{}, {"data": {}}, {"data": {"userId": ""}}, []])
    @patch.object(AkashConsoleAPI, "_request")
    def test_no_user_raises(self, mock_req, me):
        mock_req.side_effect = [me]
        with pytest.raises(RuntimeError, match="named no user"):
            AkashConsoleAPI("key").account_address()

    @pytest.mark.parametrize(
        "wallets",
        [
            [],
            [{"userId": "someone-else", "address": ADDR}],
            [
                {"userId": "u-1", "address": ADDR},
                {"userId": "u-1", "address": "akash1" + "two".ljust(38, "0")},
            ],
        ],
        ids=["none", "another-users", "two"],
    )
    @patch.object(AkashConsoleAPI, "_request")
    def test_not_exactly_one_wallet_raises(self, mock_req, wallets):
        mock_req.side_effect = [ME, {"data": wallets}]
        with pytest.raises(RuntimeError, match="not one"):
            AkashConsoleAPI("key").account_address()

    @pytest.mark.parametrize("address", ["notakash1", "akash1short", None, 7])
    @patch.object(AkashConsoleAPI, "_request")
    def test_a_non_akash_address_raises(self, mock_req, address):
        mock_req.side_effect = [ME, {"data": [{"userId": "u-1", "address": address}]}]
        with pytest.raises(RuntimeError, match="no akash1 account"):
            AkashConsoleAPI("key").account_address()
