"""Tests for just_akash.deploy — deployment orchestrator."""

from unittest.mock import patch

import pytest

from just_akash.deploy import (
    _classify_bid,
    _fmt_price,
    _inject_env_into_sdl,
    _log_bid_table,
    deploy,
)


class TestFmtPrice:
    def test_normal(self):
        bid = {"price": {"amount": 100, "denom": "uakt"}}
        assert _fmt_price(bid) == "100.0 uakt"

    def test_usd(self):
        bid = {"price": {"amount": 5, "denom": "usd"}}
        assert _fmt_price(bid) == "5.0 usd"


class TestLogBidTable:
    def test_empty_bids(self, capsys):
        _log_bid_table([], "TEST")
        captured = capsys.readouterr()
        assert "(none)" in captured.out

    def test_with_bids(self, capsys):
        bids = [{"id": {"provider": "akash1a"}, "price": {"amount": 10, "denom": "uakt"}}]
        _log_bid_table(bids, "TEST")
        captured = capsys.readouterr()
        assert "1 bid(s)" in captured.out


SDL_YAML = """
version: "2.0"
services:
  web:
    image: python:3.13-slim
    expose:
      - port: 22
        as: 22
        to:
          - global: true
"""

SDL_WITH_SSH_PLACEHOLDER = SDL_YAML.replace(
    "image: python:3.13-slim",
    "image: python:3.13-slim\n    env:\n      - SSH_PUBKEY_B64=PLACEHOLDER_SSH_PUBKEY_B64",
)


def _make_bid(provider, amount, denom="uakt"):
    return {
        "id": {"provider": provider},
        "price": {"amount": amount, "denom": denom},
        "state": "open",
    }


def _time_mock():
    counter = [0.0]

    def advance():
        counter[0] += 1
        return counter[0]

    return advance


class TestDeployMissingApiKey:
    def test_raises_without_api_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AKASH_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="AKASH_API_KEY"):
            deploy(sdl_path=str(tmp_path / "fake.yaml"))


class TestDeploySdlNotFound:
    def test_raises_for_missing_sdl(self, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        with pytest.raises(RuntimeError, match="SDL file not found"):
            deploy(sdl_path="/nonexistent/file.yaml")


class TestDeploySshPubkeyMissing:
    def test_raises_when_ssh_placeholder_and_no_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("SSH_PUBKEY", raising=False)
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_WITH_SSH_PLACEHOLDER)
        with pytest.raises(RuntimeError, match="SSH_PUBKEY"):
            deploy(sdl_path=str(sdl_file))


class TestDeployNoDseq:
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_raises_when_no_dseq_in_response(self, MockAPI, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"manifest": "abc"}

        with pytest.raises(RuntimeError, match="No DSEQ returned"):
            deploy(sdl_path=str(sdl_file))


class TestDeployNoBids:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_raises_when_no_bids_received(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = []

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError, match="No bids received"):
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)


class TestDeployOnlyForeignBids:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_raises_when_only_foreign_bids(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1allowed")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [_make_bid("akash1foreign", 50)]

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError, match="NONE from our providers"):
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)


class TestDeploySuccess:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_successful_deployment_no_allowlist(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [_make_bid("akash1prov", 100)]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["dseq"] == "12345"
        assert result["provider"] == "akash1prov"
        assert result["price"] == 100.0
        assert result["price_denom"] == "uakt"

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_successful_deployment_with_allowlist(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1allowed,akash1other")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [
            _make_bid("akash1allowed", 100),
            _make_bid("akash1other", 80),
        ]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["provider"] == "akash1other"
        assert result["price"] == 80.0


class TestDeployImageOverride:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_image_override(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [_make_bid("akash1prov", 50)]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(
            sdl_path=str(sdl_file), image="ubuntu:22.04", bid_wait=10, bid_wait_retry=10
        )
        assert result["dseq"] == "12345"
        call_args = client.create_deployment.call_args[0][0]
        assert "ubuntu:22.04" in call_args


class TestDeploySshKeyInjection:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_ssh_key_injected(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("SSH_PUBKEY", "ssh-ed25519 AAAA test@key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_WITH_SSH_PLACEHOLDER)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [_make_bid("akash1prov", 50)]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["dseq"] == "12345"
        call_args = client.create_deployment.call_args[0][0]
        assert "PLACEHOLDER_SSH_PUBKEY_B64" not in call_args


class TestDeployCreateDeploymentFails:
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_create_deployment_error(self, MockAPI, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.side_effect = RuntimeError("API down")

        with pytest.raises(RuntimeError, match="Failed to create deployment"):
            deploy(sdl_path=str(sdl_file))


class TestDeployLeaseCreationFails:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_lease_creation_error(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [_make_bid("akash1prov", 50)]
        client.create_lease.side_effect = RuntimeError("Lease failed")

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError, match="Failed to create lease"):
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)


class TestDeployBidApiError:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_api_error_then_success(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        call_count = [0]

        def bids_side(dseq):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("temp error")
            return [_make_bid("akash1prov", 50)]

        client.get_bids.side_effect = bids_side
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["provider"] == "akash1prov"


class TestDeployNoProviderInBid:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_raises_when_bid_has_no_provider(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [
            {"price": {"amount": 10, "denom": "uakt"}, "state": "open"}
        ]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError, match="no provider address"):
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)


class TestDeployAllowedProviderNoBidWithDiagnostics:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_warns_when_allowed_provider_no_bid(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1allowed,akash1slow")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [_make_bid("akash1allowed", 100)]
        client.get_provider.return_value = {
            "isOnline": True,
            "isValidVersion": True,
            "uptime1d": 99.5,
            "stats": {
                "cpu": {"available": 10, "active": 5},
                "memory": {"available": 20, "active": 10},
            },
        }
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["provider"] == "akash1allowed"
        client.get_provider.assert_called_with("akash1slow")

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_provider_not_found_in_registry(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1allowed,akash1missing")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [_make_bid("akash1allowed", 100)]
        client.get_provider.return_value = None
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["provider"] == "akash1allowed"

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_provider_query_fails(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1allowed,akash1error")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [_make_bid("akash1allowed", 100)]
        client.get_provider.side_effect = RuntimeError("query failed")
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["provider"] == "akash1allowed"


class TestDeployRetryPhase:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_no_bids_phase1_then_bids_phase2(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        call_count = [0]

        def bids_side(dseq):
            call_count[0] += 1
            if call_count[0] <= 1:
                return []
            return [_make_bid("akash1prov", 75)]

        client.get_bids.side_effect = bids_side
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["provider"] == "akash1prov"


# ── Tiered (preferred + backup) selection ─────────────────────────────────────


class TestClassifyBid:
    def test_no_allowlist_is_accepted(self):
        assert _classify_bid("akash1any", [], []) == "ACCEPTED"

    def test_provider_in_preferred(self):
        assert _classify_bid("akash1p", ["akash1p"], ["akash1b"]) == "PREFERRED"

    def test_provider_in_backup(self):
        assert _classify_bid("akash1b", ["akash1p"], ["akash1b"]) == "BACKUP"

    def test_provider_in_neither_with_allowlist_is_foreign(self):
        assert _classify_bid("akash1other", ["akash1p"], ["akash1b"]) == "FOREIGN"

    def test_preferred_wins_when_in_both_tiers(self):
        assert _classify_bid("akash1same", ["akash1same"], ["akash1same"]) == "PREFERRED"


class TestLogBidTableTierTags:
    def test_tags_each_tier(self, capsys):
        bids = [
            _make_bid("akash1p", 100),
            _make_bid("akash1b", 80),
            _make_bid("akash1foreign", 50),
        ]
        _log_bid_table(bids, "TEST", preferred=["akash1p"], backup=["akash1b"])
        captured = capsys.readouterr()
        assert "[PREFERRED]" in captured.out
        assert "[BACKUP]" in captured.out
        assert "[FOREIGN]" in captured.out

    def test_no_tags_without_allowlist(self, capsys):
        bids = [_make_bid("akash1any", 50)]
        _log_bid_table(bids, "TEST")
        captured = capsys.readouterr()
        # Without an allowlist, no tier suffix is rendered.
        assert "[PREFERRED]" not in captured.out
        assert "[BACKUP]" not in captured.out
        assert "[FOREIGN]" not in captured.out
        assert "[ACCEPTED]" not in captured.out


class TestLateArrivalWithinWindowNoAllowlist:
    """No allowlist + no bids in phase 1 + bid arrives in phase 2 → phase-2
    selection branch (the no-allowlist mirror of phase 2 grace)."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_no_allowlist_selects_bid_arriving_late_in_window(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}

        # The bid appears late, but still inside the one collection window.
        call_count = [0]

        def bids_side(dseq):
            call_count[0] += 1
            if call_count[0] <= 3:
                return []
            return [_make_bid("akash1late", 42)]

        client.get_bids.side_effect = bids_side
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["provider"] == "akash1late"
        assert result["price"] == 42.0


class TestPhase1CheapestPreferred:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_cheapest_preferred_wins_over_cheaper_backup(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        """AC: a preferred bid in phase 1 wins regardless of cheaper backup bids."""
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1pref1,akash1pref2")
        monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", "akash1back")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [
            _make_bid("akash1back", 50),  # cheapest overall, but BACKUP
            _make_bid("akash1pref1", 200),
            _make_bid("akash1pref2", 90),  # cheapest PREFERRED -> wins
            _make_bid("akash1foreign", 10),  # cheapest absolute, but FOREIGN
        ]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["provider"] == "akash1pref2"
        assert result["price"] == 90.0


class TestPreferredArrivesLateWithinWindow:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_late_preferred_wins_over_existing_backup(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        """A preferred arriving before the deadline beats a cheaper fallback."""
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1pref")
        monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", "akash1back")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}

        # Early polls: only a cheap backup. A preferred arrives later in-window.
        call_count = [0]

        def bids_side(dseq):
            call_count[0] += 1
            if call_count[0] <= 3:
                return [_make_bid("akash1back", 40)]
            return [
                _make_bid("akash1back", 40),
                _make_bid("akash1pref", 250),
            ]

        client.get_bids.side_effect = bids_side
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["provider"] == "akash1pref"
        assert result["price"] == 250.0


class TestPhase3BackupFallback:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_cheapest_backup_when_no_preferred(self, MockAPI, mock_time, tmp_path, monkeypatch):
        """AC: when no preferred bid arrives by end of phase 2, cheapest backup
        from phases 1+2 is accepted."""
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1pref")
        monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", "akash1back1,akash1back2")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [
            _make_bid("akash1back1", 120),
            _make_bid("akash1back2", 70),  # cheapest backup -> wins
            _make_bid("akash1foreign", 30),  # absolute cheapest, but FOREIGN
        ]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["provider"] == "akash1back2"
        assert result["price"] == 70.0


class TestForeignOnlyWithBackupSet:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_fails_when_only_foreign_bids_arrive(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1pref")
        monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", "akash1back")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [_make_bid("akash1foreign", 50)]

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError, match="NONE from our providers"):
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)


class TestBackwardCompatNoBackup:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_only_foreign_with_no_backup_fails_immediately(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        """AC: zero regression — AKASH_PROVIDERS set, AKASH_PROVIDERS_BACKUP
        unset, foreign-only bids fail without entering phase 2 grace."""
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1allowed")
        monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        call_count = [0]

        def bids_side(dseq):
            call_count[0] += 1
            return [_make_bid("akash1foreign", 50)]

        client.get_bids.side_effect = bids_side

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError, match="NONE from our providers"):
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)

        # Phase 2 grace must NOT trigger when backup is unset and bids exist;
        # only phase 1 polls should run (≈5 polls for bid_wait=10s mocked).
        assert call_count[0] <= 6, (
            f"expected only phase-1 polls, got {call_count[0]} polls "
            "(phase-2 grace should not trigger when backup is unset)"
        )


class TestCliArgsOverrideEnv:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_preferred_arg_overrides_env(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1env_pref")
        monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", "akash1env_back")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        # Env-named providers bid; CLI-named ones do too. CLI wins.
        client.get_bids.return_value = [
            _make_bid("akash1env_pref", 50),  # would win under env
            _make_bid("akash1cli_pref", 120),  # wins because CLI overrides env
        ]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(
            sdl_path=str(sdl_file),
            bid_wait=10,
            bid_wait_retry=10,
            preferred_providers=["akash1cli_pref"],
            backup_providers=["akash1cli_back"],
        )
        assert result["provider"] == "akash1cli_pref"
        assert result["price"] == 120.0

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_backup_arg_overrides_env(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1pref")
        monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", "akash1env_back")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        # No preferred bid; env backup bids are now FOREIGN, CLI backup wins.
        client.get_bids.return_value = [
            _make_bid("akash1env_back", 30),  # FOREIGN under CLI override
            _make_bid("akash1cli_back", 80),  # winning CLI backup
        ]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(
            sdl_path=str(sdl_file),
            bid_wait=10,
            bid_wait_retry=10,
            backup_providers=["akash1cli_back"],
        )
        assert result["provider"] == "akash1cli_back"
        assert result["price"] == 80.0


# ── Coverage: env injection helper, stale-deployment recovery, defensive guards ─


class TestInjectEnvIntoSdl:
    def test_empty_env_vars_returns_unchanged(self):
        sdl = "version: '2.0'\nservices:\n  web:\n    image: x\n"
        assert _inject_env_into_sdl(sdl, []) == sdl

    def test_appends_when_existing_env_block(self):
        sdl = (
            "version: '2.0'\n"
            "services:\n"
            "  web:\n"
            "    image: x\n"
            "    env:\n"
            "      - KEEP=1\n"
            "    expose:\n"
            "      - port: 80\n"
        )
        out = _inject_env_into_sdl(sdl, ["NEW=2"])
        assert "- NEW=2" in out
        assert "- KEEP=1" in out

    def test_overrides_collision_in_existing_env_block(self):
        sdl = (
            "version: '2.0'\n"
            "services:\n"
            "  web:\n"
            "    image: x\n"
            "    env:\n"
            "      - DUP=old\n"
            "      - KEEP=1\n"
            "    expose:\n"
            "      - port: 80\n"
        )
        out = _inject_env_into_sdl(sdl, ["DUP=new"])
        assert "- DUP=new" in out
        assert "- DUP=old" not in out
        assert "- KEEP=1" in out

    def test_creates_env_block_above_expose_when_missing(self):
        sdl = "version: '2.0'\nservices:\n  web:\n    image: x\n    expose:\n      - port: 80\n"
        out = _inject_env_into_sdl(sdl, ["A=1", "B=2"])
        # Env block must be inserted before expose with the right indent.
        assert "    env:\n      - A=1\n      - B=2\n    expose:" in out

    def test_returns_unchanged_when_no_env_and_no_expose(self):
        sdl = "version: '2.0'\nservices:\n  web:\n    image: x\n"
        # Helper has no anchor to insert at — leaves SDL untouched.
        out = _inject_env_into_sdl(sdl, ["A=1"])
        assert out == sdl


class TestDeployEnvVarsLogged:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_env_vars_injection_runs(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [_make_bid("akash1prov", 50)]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        deploy(
            sdl_path=str(sdl_file),
            bid_wait=10,
            bid_wait_retry=10,
            env_vars=["FOO=bar"],
        )
        # Env-var injection wrote the var into the SDL sent to create_deployment.
        sent_sdl = client.create_deployment.call_args[0][0]
        assert "FOO=bar" in sent_sdl


class TestStaleDeploymentRecovery:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_already_exists_closes_stale_and_retries(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        # First call raises "already exists"; retry succeeds.
        client.create_deployment.side_effect = [
            RuntimeError("Deployment already exists"),
            {"dseq": "999", "manifest": "abc"},
        ]
        # Two stale deployments: one without lease (close it), one with lease (skip).
        client.list_deployments.return_value = [
            {"dseq": "111", "leases": []},  # closed
            {"dseq": "222", "leases": [{"id": "x"}]},  # skipped (has lease)
            {"deployment": {"dseq": "333"}},  # nested dseq, no leases → closed
        ]
        client.close_deployment.return_value = {}
        client.get_bids.return_value = [_make_bid("akash1prov", 50)]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["dseq"] == "999"
        # Closed only the lease-less stale ones (111 and 333), skipped 222.
        closed_args = [c.args[0] for c in client.close_deployment.call_args_list]
        assert "111" in closed_args
        assert "333" in closed_args
        assert "222" not in closed_args

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_already_exists_cleanup_failure_then_retry_succeeds(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.side_effect = [
            RuntimeError("Deployment already exists"),
            {"dseq": "777", "manifest": "abc"},
        ]
        client.list_deployments.side_effect = RuntimeError("list failed")
        client.get_bids.return_value = [_make_bid("akash1prov", 50)]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["dseq"] == "777"

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_already_exists_retry_also_fails(self, MockAPI, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.side_effect = [
            RuntimeError("Deployment already exists"),
            RuntimeError("still broken"),
        ]
        client.list_deployments.return_value = []

        with pytest.raises(RuntimeError, match="after retry"):
            deploy(sdl_path=str(sdl_file))


class TestLateNonDictBidSkipped:
    """The final snapshot defensively ignores non-dict entries."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_non_dict_then_preferred_arrives_within_window(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1pref")
        monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", "akash1back")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}

        # A later in-window snapshot contains a non-dict entry and a preferred bid.
        call_count = [0]

        def bids_side(dseq):
            call_count[0] += 1
            if call_count[0] <= 3:
                return [_make_bid("akash1back", 40)]
            return [
                None,
                _make_bid("akash1back", 40),
                _make_bid("akash1pref", 100),
            ]

        client.get_bids.side_effect = bids_side
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["provider"] == "akash1pref"


class TestAllMalformedBidsWithAllowlist:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_all_non_dict_bids_with_allowlist_fail(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1pref")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [None, 42, "string"]
        client.close_deployment.return_value = {}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError, match="No valid bids received"):
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_all_malformed_cleanup_failure_still_raises(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1pref")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [None]
        client.close_deployment.side_effect = RuntimeError("close failed")

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError, match="No valid bids received"):
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)


class TestCleanupFailureCascades:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_foreign_only_with_cleanup_failure(self, MockAPI, mock_time, tmp_path, monkeypatch):
        """Foreign-only bids and close_deployment raises during cleanup."""
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1allowed")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [_make_bid("akash1foreign", 50)]
        client.close_deployment.side_effect = RuntimeError("close failed")

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError, match="NONE from our providers"):
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_no_provider_bid_with_cleanup_failure(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [
            {"price": {"amount": 10, "denom": "uakt"}, "state": "open"}
        ]
        client.close_deployment.side_effect = RuntimeError("close failed")

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError, match="no provider address"):
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_lease_failure_with_cleanup_failure(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [_make_bid("akash1prov", 50)]
        client.create_lease.side_effect = RuntimeError("Lease failed")
        client.close_deployment.side_effect = RuntimeError("close failed")

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError, match="Failed to create lease"):
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_no_bids_with_cleanup_failure(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = []
        client.close_deployment.side_effect = RuntimeError("close failed")

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError, match="No bids received"):
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)


# ── Adversarial probes for the tiered selection state machine ────────────────


class TestEmptyListOverrideIsExplicit:
    """An empty list override (`preferred_providers=[]`) should NOT fall back
    to AKASH_PROVIDERS env. Behaves like 'no preferred allowlist set' even
    though env is populated. This probes _resolve_tier's None-vs-[]
    distinction and ensures CLI explicit-empty isn't silently overridden by
    env."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_empty_list_override_does_not_consult_env(
        self, MockAPI, mock_time, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        # Env says only "akash1env_pref" is preferred; explicit [] override
        # should make that classification go away (env ignored).
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1env_pref")
        monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.account_address.return_value = "akash1single"
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        # If env IS consulted (bug), only "akash1env_pref" is allowed → the
        # cheaper "akash1other" bid would be FOREIGN and the run would fail.
        # If [] correctly means "no allowlist", every bid is ACCEPTED and the
        # cheaper one wins.
        client.get_bids.return_value = [
            _make_bid("akash1env_pref", 200),
            _make_bid("akash1other", 50),
        ]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(
            sdl_path=str(sdl_file),
            bid_wait=10,
            bid_wait_retry=10,
            preferred_providers=[],
            backup_providers=[],
        )
        # No allowlist → cheapest bid wins regardless of env value.
        assert result["provider"] == "akash1other"
        assert result["price"] == 50.0
        assert result["wallet_account"] == "akash1single"
        output = capsys.readouterr().out
        assert "cheapest eligible bid after collection window" in output
        assert "cheapest preferred after collection window" not in output
        assert "Wallet: akash1single" in output


class TestWhitespaceOnlyEnvValueTreatedAsUnset:
    """Comma-only or whitespace-only AKASH_PROVIDERS must resolve to no
    allowlist. If parsing is buggy (e.g. ',' → ['', ''] without strip
    filtering), an empty-string entry could match unset providers or skew
    classification."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_comma_only_env_treated_as_no_allowlist(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        # Pathological env: just commas/whitespace.
        monkeypatch.setenv("AKASH_PROVIDERS", " , , ")
        monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", ",")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        # Two arbitrary bids. With proper trim+drop, no allowlist → cheapest
        # wins. If parsing leaks empty strings into the allowlist, the
        # has_allowlist flag flips True and these would be FOREIGN-rejected.
        client.get_bids.return_value = [
            _make_bid("akash1one", 99),
            _make_bid("akash1two", 33),
        ]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["provider"] == "akash1two"
        assert result["price"] == 33.0


class TestBackupOnlyAllowlistAcceptsBackupBid:
    """Preferred is empty, backup is set, only a backup bid arrives. The
    state machine should:
      - Phase 1: no PREFERRED can match → no selection.
      - Phase 2: enters because backup is configured. early_exit looks for
        a PREFERRED bid that can NEVER appear → polls full T2.
      - Phase 3: cheapest backup wins.
    This probes the gap where 'preferred can't ever arrive' still wastes a
    full T2 wait, and confirms the eventual selection still completes."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_backup_only_allowlist_selects_backup_after_grace(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", "akash1back")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [_make_bid("akash1back", 75)]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        # Backup wins via phase-3 fallback even with no preferred configured.
        assert result["provider"] == "akash1back"
        assert result["price"] == 75.0


class TestSimultaneousPreferredAndBackupWithinWindow:
    """A snapshot before the deadline reveals preferred and fallback together."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_simultaneous_arrival_within_window_preferred_wins(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1pref")
        monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", "akash1back")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}

        call_count = [0]

        def bids_side(dseq):
            call_count[0] += 1
            # Nothing at first; both bids appear before the window closes.
            if call_count[0] <= 3:
                return []
            # The same poll surfaces preferred AND backup. The cheaper
            # bid is the backup, but tier rules say preferred wins.
            return [
                _make_bid("akash1back", 5),
                _make_bid("akash1pref", 999),
            ]

        client.get_bids.side_effect = bids_side
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["provider"] == "akash1pref"
        assert result["price"] == 999.0


class TestBackupConfiguredForeignOnlyBidsRunFullGrace:
    """Preferred + backup configured, only foreign bids arrive. Phase 1
    finds nothing matching, phase 2 enters (backup is set), early-exit
    waits for a PREFERRED that never comes, phase 3 finds no BACKUP. The
    error path must be the foreign-bids-only message (NOT 'no bids' nor
    'no valid bids')."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_foreign_only_with_backup_set_uses_foreign_error(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1pref")
        monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", "akash1back")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        client.get_bids.return_value = [_make_bid("akash1foreign", 50)]
        client.close_deployment.return_value = {}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError) as excinfo:
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        msg = str(excinfo.value)
        assert "NONE from our providers" in msg
        assert "akash1foreign" in msg
        # Make sure we're not hitting the wrong terminal branch.
        assert "No bids received" not in msg
        assert "No valid bids" not in msg


# ── Iteration 2: deeper adversarial probes ───────────────────────────────────


class TestProviderAddressCaseSensitivity:
    """`_classify_bid` uses Python's `in` for list membership, which is
    case-sensitive string equality. A bid from `AKASH1Pref` (different case)
    against an allowlist of `akash1pref` MUST be classified FOREIGN, never
    matched by accidental case-folding. Probing both `_classify_bid` directly
    and the full deploy path (so a regression that lowercased provider
    addresses anywhere in the pipeline would surface)."""

    def test_classify_bid_is_case_sensitive(self):
        # Different case must NOT match.
        assert _classify_bid("AKASH1Pref", ["akash1pref"], []) == "FOREIGN"
        assert _classify_bid("akash1pref", ["AKASH1Pref"], []) == "FOREIGN"
        # Same case must still match.
        assert _classify_bid("akash1pref", ["akash1pref"], []) == "PREFERRED"

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_case_mismatch_bid_is_foreign_in_full_deploy(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        # Lowercase allowlist; bid arrives in mixed case.
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1pref")
        monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        # Only a case-mismatched bid arrives → should be FOREIGN, not selected.
        client.get_bids.return_value = [_make_bid("AKASH1Pref", 50)]
        client.close_deployment.return_value = {}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        with pytest.raises(RuntimeError) as excinfo:
            deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        msg = str(excinfo.value)
        # If a regression case-folds, the deploy would succeed and this would
        # never raise. The diagnostic must list the case-different provider as
        # foreign.
        assert "NONE from our providers" in msg
        assert "AKASH1Pref" in msg


class TestTiedPriceAcrossTiers:
    """Preferred and backup bids tied at the same price. Tier dominance must
    be absolute: PREFERRED wins regardless of price-tie with BACKUP. A bug
    that flat-merged tiers and used min-price across the union would pick
    either side based on iteration order."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_preferred_wins_when_tied_with_backup_price(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1pref")
        monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", "akash1back")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        # Backup listed FIRST in the bid array at price 100, foreign cheaper at
        # 50, preferred SAME price 100 listed LAST. A min()-over-union
        # implementation that treated tier as a tiebreaker (or worse, ignored
        # tier entirely) would mis-select. Correct behavior: PREFERRED wins.
        client.get_bids.return_value = [
            _make_bid("akash1back", 100),  # tied price, BACKUP tier
            _make_bid("akash1foreign", 50),  # cheapest absolute, FOREIGN
            _make_bid("akash1pref", 100),  # tied price, PREFERRED tier → wins
        ]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["provider"] == "akash1pref"
        assert result["price"] == 100.0


class TestPreferredOnlyCliOverrideBackupFromEnv:
    """CLI overrides one tier; the OTHER tier must still resolve from env.
    The complementary case to `test_backup_arg_overrides_env`: here we pass
    `preferred_providers=["akash1cli_pref"]` while leaving
    `backup_providers=None`, with AKASH_PROVIDERS_BACKUP set in env. The
    backup tier must come from env, and the env's AKASH_PROVIDERS must NOT
    bleed into the CLI-resolved preferred list."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_preferred_cli_with_backup_env(self, MockAPI, mock_time, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        # Env preferred is set but should be IGNORED (CLI overrides it).
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1env_pref")
        # Env backup is set and should be ACTIVE (no CLI override for backup).
        monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", "akash1env_back")
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        # No CLI-preferred bid arrives, so phase 3 backup fallback fires.
        # akash1env_pref should be FOREIGN (env preferred ignored under CLI).
        # akash1env_back should be BACKUP (env backup still applies).
        client.get_bids.return_value = [
            _make_bid("akash1env_pref", 10),  # FOREIGN: CLI override killed env-pref
            _make_bid("akash1env_back", 60),  # BACKUP from env → wins via phase 3
        ]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(
            sdl_path=str(sdl_file),
            bid_wait=10,
            bid_wait_retry=10,
            preferred_providers=["akash1cli_pref"],
            # backup_providers=None → falls through to AKASH_PROVIDERS_BACKUP env
        )
        # If env preferred bled into CLI (bug), akash1env_pref @ 10 would win
        # via phase 1. Correct: backup fallback selects env backup at 60.
        assert result["provider"] == "akash1env_back"
        assert result["price"] == 60.0


class TestSelectedBidIdentityMarkerInRankingLog:
    """The success-path step-5 ranking log uses `if b is selected_bid` (object
    identity) to mark the winner. A regression that rebuilt selected_bid as
    a fresh dict with equal value would silently drop the marker. Probe by
    capturing stdout and counting the marker."""

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_selected_marker_appears_exactly_once(
        self, MockAPI, mock_time, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setenv("AKASH_PROVIDERS", "akash1a,akash1b,akash1c")
        monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
        sdl_file = tmp_path / "sdl.yaml"
        sdl_file.write_text(SDL_YAML)

        client = MockAPI.return_value
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        # Three preferred bids at distinct prices so the ranking log lists
        # exactly three entries; the cheapest (akash1c @ 50) wins.
        client.get_bids.return_value = [
            _make_bid("akash1a", 200),
            _make_bid("akash1b", 100),
            _make_bid("akash1c", 50),
        ]
        client.create_lease.return_value = {"data": {"lease": "created"}}

        t = _time_mock()
        mock_time.time.side_effect = t
        mock_time.sleep.return_value = None

        result = deploy(sdl_path=str(sdl_file), bid_wait=10, bid_wait_retry=10)
        assert result["provider"] == "akash1c"

        captured = capsys.readouterr()
        # The marker must appear exactly once across the ranking log.
        # If selected_bid was rebuilt (identity broken), count would be 0.
        # If marker logic wrongly tagged multiple entries, count would be > 1.
        assert captured.out.count("<-- SELECTED") == 1, (
            f"expected exactly 1 SELECTED marker, "
            f"got {captured.out.count('<-- SELECTED')}\n"
            f"captured output:\n{captured.out}"
        )
        # And it must be on the rank line for akash1c (the actual winner).
        # Find the line with the marker and assert provider matches.
        marker_lines = [ln for ln in captured.out.splitlines() if "<-- SELECTED" in ln]
        assert len(marker_lines) == 1
        assert "akash1c" in marker_lines[0]


class TestProviderNoneInBidIsForeign:
    """When a bid dict has `id.provider == None` (vs. missing entirely),
    `_extract_provider` returns None. Under an active allowlist,
    `_classify_bid(None, [...], [...])` must return FOREIGN — the
    `if provider and provider in preferred` guard relies on truthiness of
    `provider`. A regression that dropped the truthiness check could either
    raise (None in list comparison is fine) or incorrectly classify."""

    def test_classify_bid_none_provider_is_foreign(self):
        # With allowlist set: None must be FOREIGN.
        assert _classify_bid(None, ["akash1pref"], ["akash1back"]) == "FOREIGN"
        # Empty string also FOREIGN under allowlist.
        assert _classify_bid("", ["akash1pref"], ["akash1back"]) == "FOREIGN"
        # No allowlist: ACCEPTED regardless of provider value.
        assert _classify_bid(None, [], []) == "ACCEPTED"
        assert _classify_bid("", [], []) == "ACCEPTED"
