"""Tests for just_akash.api — extractors, tags, API client, formatters."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from just_akash.api import (
    AkashConsoleAPI,
    _extract_bid_price,
    _extract_dseq,
    _extract_lease_provider,
    _extract_provider,
    _extract_ssh_info,
    _get_tag,
    _interactive_pick,
    _load_tags,
    _resolve_dseq,
    _save_tags,
    format_deployments_table,
)
from tests._creds import fake_api_key

# ── _extract_dseq ────────────────────────────────────


class TestExtractDseq:
    def test_direct_dseq(self):
        assert _extract_dseq({"dseq": 12345}) == "12345"

    def test_nested_deployment_id(self):
        dep = {"deployment": {"id": {"dseq": 99999}}}
        assert _extract_dseq(dep) == "99999"

    def test_missing_dseq(self):
        assert _extract_dseq({"other": "data"}) is None

    def test_empty_dict(self):
        assert _extract_dseq({}) is None

    def test_dseq_as_string(self):
        assert _extract_dseq({"dseq": "55555"}) == "55555"


# ── _extract_provider ────────────────────────────────


class TestExtractProvider:
    def test_bid_id_provider(self):
        bid = {"id": {"provider": "akash1abc"}}
        assert _extract_provider(bid) == "akash1abc"

    def test_nested_bid_id(self):
        bid = {"bid": {"id": {"provider": "akash1def"}}}
        assert _extract_provider(bid) == "akash1def"

    def test_flat_provider(self):
        bid = {"provider": "akash1ghi"}
        assert _extract_provider(bid) == "akash1ghi"

    def test_no_provider(self):
        assert _extract_provider({}) is None

    def test_id_takes_precedence_over_flat(self):
        bid = {"id": {"provider": "akash1id"}, "provider": "akash1flat"}
        assert _extract_provider(bid) == "akash1id"


# ── _extract_bid_price ───────────────────────────────


class TestExtractBidPrice:
    def test_price_dict(self):
        bid = {"price": {"amount": 100, "denom": "uakt"}}
        assert _extract_bid_price(bid) == (100.0, "uakt")

    def test_nested_bid_price(self):
        bid = {"bid": {"price": {"amount": 50, "denom": "usd"}}}
        assert _extract_bid_price(bid) == (50.0, "usd")

    def test_missing_amount(self):
        bid = {"price": {"denom": "uakt"}}
        assert _extract_bid_price(bid) == (float("inf"), "uakt")

    def test_missing_denom(self):
        bid = {"price": {"amount": 25}}
        assert _extract_bid_price(bid) == (25.0, "uact")

    def test_numeric_price(self):
        bid = {"price": 75}
        assert _extract_bid_price(bid) == (75.0, "uact")

    def test_none_price(self):
        bid = {"price": None}
        assert _extract_bid_price(bid) == (float("inf"), "uact")

    def test_empty_bid(self):
        assert _extract_bid_price({}) == (float("inf"), "uact")


# ── _extract_ssh_info ───────────────────────────────


class TestExtractSshInfo:
    def test_ssh_found(self):
        dep = {
            "leases": [
                {
                    "status": {
                        "forwarded_ports": {
                            "ssh-service": [{"port": 22, "host": "1.2.3.4", "externalPort": 12345}]
                        }
                    }
                }
            ]
        }
        result = _extract_ssh_info(dep)
        assert result == {"host": "1.2.3.4", "port": 12345, "service": "ssh-service"}

    def test_no_port_22(self):
        dep = {
            "leases": [
                {
                    "status": {
                        "forwarded_ports": {
                            "web": [{"port": 80, "host": "1.2.3.4", "externalPort": 8080}]
                        }
                    }
                }
            ]
        }
        assert _extract_ssh_info(dep) is None

    def test_no_leases(self):
        assert _extract_ssh_info({}) is None

    def test_empty_leases(self):
        assert _extract_ssh_info({"leases": []}) is None

    def test_no_status(self):
        dep = {"leases": [{"id": {}}]}
        assert _extract_ssh_info(dep) is None

    def test_multiple_leases_first_has_ssh(self):
        dep = {
            "leases": [
                {
                    "status": {
                        "forwarded_ports": {
                            "ssh": [{"port": 22, "host": "5.6.7.8", "externalPort": 2222}]
                        }
                    }
                },
                {
                    "status": {
                        "forwarded_ports": {
                            "ssh": [{"port": 22, "host": "9.10.11.12", "externalPort": 3333}]
                        }
                    }
                },
            ]
        }
        result = _extract_ssh_info(dep)
        assert result["host"] == "5.6.7.8"  # type: ignore[index]


# ── _extract_lease_provider ─────────────────────────


class TestExtractLeaseProvider:
    def test_provider_found(self):
        dep = {"leases": [{"id": {"provider": "akash1prov"}}]}
        assert _extract_lease_provider(dep) == "akash1prov"

    def test_no_provider_in_id(self):
        dep = {"leases": [{"id": {"dseq": "123"}}]}
        assert _extract_lease_provider(dep) is None

    def test_no_leases(self):
        assert _extract_lease_provider({}) is None

    def test_empty_leases(self):
        assert _extract_lease_provider({"leases": []}) is None

    def test_multiple_leases_first_wins(self):
        dep = {
            "leases": [{"id": {"provider": "akash1first"}}, {"id": {"provider": "akash1second"}}]
        }
        assert _extract_lease_provider(dep) == "akash1first"


# ── Tags ─────────────────────────────────────────────


class TestTags:
    def test_load_tags_missing_file(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        assert _load_tags() == {}

    def test_save_and_load_tags(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)
        _save_tags({"12345": "my-job"})
        loaded = _load_tags()
        assert loaded == {"12345": "my-job"}

    def test_load_tags_corrupt_file(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        tags_file.write_text("NOT JSON!!!")
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)
        assert _load_tags() == {}

    def test_get_tag_found(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)
        _save_tags({"12345": "my-job"})
        assert _get_tag("12345") == "my-job"

    def test_get_tag_not_found(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)
        _save_tags({"12345": "my-job"})
        assert _get_tag("99999") == ""

    def test_resolve_dseq_numeric(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        assert _resolve_dseq("12345") == "12345"

    def test_resolve_dseq_empty(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        assert _resolve_dseq("") == ""

    def test_resolve_dseq_tag_lookup(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)
        _save_tags({"12345": "my-job"})
        assert _resolve_dseq("my-job") == "12345"

    def test_resolve_dseq_unknown_tag(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        with pytest.raises(SystemExit):
            _resolve_dseq("nonexistent")


# ── AkashConsoleAPI ──────────────────────────────────


class TestAkashConsoleAPI:
    def test_init(self):
        key = fake_api_key()
        client = AkashConsoleAPI(key)
        assert client.api_key == key
        assert client.base_url == "https://console-api.akash.network"
        assert client.headers["x-api-key"] == key

    @patch("just_akash.api.urllib.request.urlopen")
    def test_request_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"data": {"result": "ok"}}'
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = AkashConsoleAPI("key")
        result = client._request("GET", "/v1/test")
        assert result == {"data": {"result": "ok"}}

    @patch("just_akash.api.urllib.request.urlopen")
    def test_request_empty_response(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b""
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = AkashConsoleAPI("key")
        result = client._request("GET", "/v1/test")
        assert result == {}

    @patch("just_akash.api.urllib.request.urlopen")
    def test_request_http_error_json_body(self, mock_urlopen):
        import urllib.error

        mock_err = urllib.error.HTTPError(
            url="http://test",
            code=400,
            msg="Bad Request",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
        mock_err.read = lambda: b'{"message": "invalid SDL"}'  # type: ignore[assignment]
        mock_urlopen.side_effect = mock_err

        client = AkashConsoleAPI("key")
        with pytest.raises(RuntimeError, match="API Error \\(400\\)"):
            client._request("POST", "/v1/deployments", {"data": {}})

    @patch("just_akash.api.urllib.request.urlopen")
    def test_request_http_error_non_json_body(self, mock_urlopen):
        import urllib.error

        mock_err = urllib.error.HTTPError(
            url="http://test",
            code=500,
            msg="Server Error",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
        mock_err.read = lambda: b"Internal Server Error"  # type: ignore[assignment]
        mock_urlopen.side_effect = mock_err

        client = AkashConsoleAPI("key")
        with pytest.raises(RuntimeError, match="API Error \\(500\\)"):
            client._request("GET", "/v1/test")

    @patch("just_akash.api.urllib.request.urlopen")
    def test_request_url_error(self, mock_urlopen):
        import urllib.error

        mock_urlopen.side_effect = urllib.error.URLError("connection refused")

        client = AkashConsoleAPI("key")
        with pytest.raises(RuntimeError, match="Connection error"):
            client._request("GET", "/v1/test")

    @patch.object(AkashConsoleAPI, "_request")
    def test_list_deployments_active(self, mock_req):
        mock_req.return_value = {
            "data": {
                "deployments": [
                    {"deployment": {"state": "active"}, "dseq": "1"},
                    {"deployment": {"state": "closed"}, "dseq": "2"},
                ]
            }
        }
        client = AkashConsoleAPI("key")
        result = client.list_deployments(active_only=True)
        assert len(result) == 1
        assert result[0]["dseq"] == "1"

    @patch.object(AkashConsoleAPI, "_request")
    def test_list_deployments_all(self, mock_req):
        mock_req.return_value = {
            "data": {
                "deployments": [
                    {"deployment": {"state": "active"}, "dseq": "1"},
                    {"deployment": {"state": "closed"}, "dseq": "2"},
                ]
            }
        }
        client = AkashConsoleAPI("key")
        result = client.list_deployments(active_only=False)
        assert len(result) == 2

    @patch.object(AkashConsoleAPI, "_request")
    def test_get_deployment(self, mock_req):
        mock_req.return_value = {"data": {"dseq": "12345", "state": "active"}}
        client = AkashConsoleAPI("key")
        result = client.get_deployment("12345")
        assert result["dseq"] == "12345"

    @patch.object(AkashConsoleAPI, "_request")
    def test_create_deployment(self, mock_req):
        mock_req.return_value = {"data": {"dseq": "99999", "manifest": "abc"}}
        client = AkashConsoleAPI("key")
        result = client.create_deployment("sdl-content", deposit=10.0)
        assert result["dseq"] == "99999"
        mock_req.assert_called_once_with(
            "POST", "/v1/deployments", {"data": {"sdl": "sdl-content", "deposit": 10.0}}
        )

    @patch.object(AkashConsoleAPI, "_request")
    def test_close_deployment(self, mock_req):
        mock_req.return_value = {"data": {"closed": True}}
        client = AkashConsoleAPI("key")
        client.close_deployment("12345")
        mock_req.assert_called_once_with("DELETE", "/v1/deployments/12345")

    @patch.object(AkashConsoleAPI, "close_deployment")
    @patch.object(AkashConsoleAPI, "list_deployments")
    def test_close_all_deployments(self, mock_list, mock_close):
        mock_list.return_value = [
            {"dseq": "1"},
            {"dseq": "2"},
        ]
        mock_close.return_value = {"closed": True}
        client = AkashConsoleAPI("key")
        result = client.close_all_deployments()
        assert len(result["closed"]) == 2

    @patch.object(AkashConsoleAPI, "close_deployment")
    @patch.object(AkashConsoleAPI, "list_deployments")
    def test_close_all_with_failure(self, mock_list, mock_close):
        mock_list.return_value = [{"dseq": "1"}, {"dseq": "2"}]
        mock_close.side_effect = [RuntimeError("fail"), {"closed": True}]
        client = AkashConsoleAPI("key")
        result = client.close_all_deployments()
        assert len(result["closed"]) == 1

    @patch.object(AkashConsoleAPI, "list_deployments")
    def test_close_all_skips_no_dseq(self, mock_list):
        mock_list.return_value = [{"other": "data"}]
        client = AkashConsoleAPI("key")
        result = client.close_all_deployments()
        assert result["closed"] == []

    @patch.object(AkashConsoleAPI, "_request")
    def test_get_bids_list(self, mock_req):
        mock_req.return_value = {"data": [{"id": {"provider": "akash1a"}}]}
        client = AkashConsoleAPI("key")
        result = client.get_bids("12345")
        assert len(result) == 1

    @patch.object(AkashConsoleAPI, "_request")
    def test_get_bids_nested(self, mock_req):
        mock_req.return_value = {"data": {"bids": [{"id": {"provider": "akash1b"}}]}}
        client = AkashConsoleAPI("key")
        result = client.get_bids("12345")
        assert len(result) == 1

    @patch.object(AkashConsoleAPI, "_request")
    def test_get_bids_direct_list(self, mock_req):
        mock_req.return_value = {"data": [{"id": {"provider": "akash1c"}}]}
        client = AkashConsoleAPI("key")
        result = client.get_bids("12345")
        assert result[0]["id"]["provider"] == "akash1c"

    @patch.object(AkashConsoleAPI, "_request")
    def test_get_provider_found(self, mock_req):
        mock_req.return_value = {"data": [{"owner": "akash1target", "isOnline": True}]}
        client = AkashConsoleAPI("key")
        result = client.get_provider("akash1target")
        assert result is not None
        assert result["owner"] == "akash1target"

    @patch.object(AkashConsoleAPI, "_request")
    def test_get_provider_not_found(self, mock_req):
        mock_req.return_value = {"data": [{"owner": "akash1other"}]}
        client = AkashConsoleAPI("key")
        result = client.get_provider("akash1target")
        assert result is None

    @patch.object(AkashConsoleAPI, "_request")
    def test_get_provider_error(self, mock_req):
        mock_req.side_effect = RuntimeError("api down")
        client = AkashConsoleAPI("key")
        assert client.get_provider("akash1target") is None

    @patch.object(AkashConsoleAPI, "_request")
    def test_create_lease(self, mock_req):
        mock_req.return_value = {"data": {"lease": "created"}}
        client = AkashConsoleAPI("key")
        client.create_lease("12345", "akash1prov", "manifest-str")
        mock_req.assert_called_once_with(
            "POST",
            "/v1/leases",
            {
                "manifest": "manifest-str",
                "leases": [{"dseq": "12345", "gseq": 1, "oseq": 1, "provider": "akash1prov"}],
            },
        )


# ── format_deployments_table ─────────────────────────


class TestFormatDeploymentsTable:
    def test_empty(self):
        assert format_deployments_table([]) == "No active deployments."

    def test_single_deployment(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        dep = {
            "dseq": "12345",
            "deployment": {"state": "active"},
            "leases": [{"id": {"provider": "akash1prov"}}],
        }
        table = format_deployments_table([dep])
        assert "12345" in table
        assert "active" in table
        assert "akash1prov" in table

    def test_deployment_with_ssh(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        dep = {
            "dseq": "12345",
            "deployment": {"state": "active"},
            "leases": [
                {
                    "id": {"provider": "akash1prov"},
                    "status": {
                        "forwarded_ports": {
                            "ssh": [{"port": 22, "host": "1.2.3.4", "externalPort": 2222}]
                        }
                    },
                }
            ],
        }
        table = format_deployments_table([dep])
        assert "1.2.3.4:2222" in table

    def test_deployment_with_tag(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)
        _save_tags({"12345": "my-job"})
        dep = {
            "dseq": "12345",
            "deployment": {"state": "active"},
            "leases": [],
        }
        table = format_deployments_table([dep])
        assert "my-job" in table


# ── _interactive_pick ────────────────────────────────


class TestInteractivePick:
    def test_non_tty_picks_first(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        client = AkashConsoleAPI("key")
        deployments = [{"dseq": "11111"}, {"dseq": "22222"}]
        result = _interactive_pick(deployments, client)
        assert result == "11111"

    def test_non_tty_no_dseq_raises(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        client = AkashConsoleAPI("key")
        deployments = [{"other": "data"}]
        with pytest.raises(RuntimeError, match="Could not extract dseq"):
            _interactive_pick(deployments, client)
