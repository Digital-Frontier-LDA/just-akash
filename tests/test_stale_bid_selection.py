"""Tests for issue #14 — stale (non-open) bids must never be selected/leased.

Covers the three fix layers:
1. `_bid_state` / `_is_open_bid` extraction and the state filter in selection.
2. The phase-2 grace cut when open BACKUP bids are available.
3. The lease-creation retry against the next open bid on a stale-bid 400.
"""

from unittest.mock import patch

import pytest

from just_akash.deploy import (
    _backup_fallback_grace_s,
    _bid_state,
    _is_open_bid,
    deploy,
)

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


def _make_bid(provider, amount, state="open", denom="uakt"):
    return {
        "id": {"provider": provider},
        "price": {"amount": amount, "denom": denom},
        "state": state,
    }


def _time_mock():
    counter = [0.0]

    def advance():
        counter[0] += 1
        return counter[0]

    return advance


def _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers=None, backup=None):
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    if providers is None:
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
    else:
        monkeypatch.setenv("AKASH_PROVIDERS", providers)
    if backup is None:
        monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
    else:
        monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", backup)
    sdl_file = tmp_path / "sdl.yaml"
    sdl_file.write_text(SDL_YAML)

    client = MockAPI.return_value
    client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
    client.create_lease.return_value = {"lease": "ok"}

    mock_time.time.side_effect = _time_mock()
    mock_time.sleep.return_value = None
    return client, str(sdl_file)


class TestBidStateHelpers:
    def test_flat_state(self):
        assert _bid_state({"state": "open"}) == "open"
        assert _bid_state({"state": "closed"}) == "closed"

    def test_nested_state(self):
        assert _bid_state({"bid": {"state": "open"}}) == "open"
        assert _bid_state({"bid": {"state": "closed"}}) == "closed"

    def test_flat_wins_over_nested(self):
        assert _bid_state({"state": "closed", "bid": {"state": "open"}}) == "closed"

    def test_missing_state(self):
        assert _bid_state({"id": {"provider": "akash1a"}}) == "?"
        assert _bid_state("not-a-dict") == "?"

    def test_is_open(self):
        assert _is_open_bid(_make_bid("akash1a", 10))
        assert not _is_open_bid(_make_bid("akash1a", 10, state="closed"))
        # No state field at all → assume open (older API shapes).
        assert _is_open_bid({"id": {"provider": "akash1a"}})

    def test_fallback_grace_env_override(self, monkeypatch):
        monkeypatch.setenv("JUST_AKASH_BACKUP_FALLBACK_S", "99")
        assert _backup_fallback_grace_s() == 99
        monkeypatch.setenv("JUST_AKASH_BACKUP_FALLBACK_S", "not-a-number")
        assert _backup_fallback_grace_s() == 240


class TestSelectionSkipsStaleBids:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_closed_cheaper_bid_loses_to_open_bid(self, MockAPI, mock_time, tmp_path, monkeypatch):
        """Phase 1: a closed bid must not win even if it is the cheapest."""
        client, sdl = _setup(
            MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1cheap,akash1live"
        )
        client.get_bids.return_value = [
            _make_bid("akash1cheap", 10, state="closed"),
            _make_bid("akash1live", 50),
        ]

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
        assert result["provider"] == "akash1live"

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_all_stale_bids_fail_selection(self, MockAPI, mock_time, tmp_path, monkeypatch):
        """If every bid has expired, selection must fail — not lease a dead bid."""
        client, sdl = _setup(
            MockAPI,
            mock_time,
            tmp_path,
            monkeypatch,
            providers="akash1pref",
            backup="akash1back",
        )
        client.get_bids.return_value = [
            _make_bid("akash1back", 10, state="closed"),
        ]

        # The bid is from an ALLOWED provider, just stale — the failure must say
        # "none are still open", not misattribute it to non-allowed providers.
        with pytest.raises(RuntimeError, match="none are still open") as exc:
            deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
        assert "NONE from our providers" not in str(exc.value)
        client.create_lease.assert_not_called()

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_all_stale_bids_no_allowlist(self, MockAPI, mock_time, tmp_path, monkeypatch):
        """With no allowlist, an all-stale bid pool still reports 'no open bids'
        rather than falling through to the non-allowed-providers message."""
        client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch)
        client.get_bids.return_value = [
            _make_bid("akash1any", 10, state="closed"),
        ]

        with pytest.raises(RuntimeError, match="none are still open") as exc:
            deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
        assert "NONE from our providers" not in str(exc.value)
        client.create_lease.assert_not_called()


class TestPhase2GraceCut:
    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_grace_cut_short_when_backup_bid_open(
        self, MockAPI, mock_time, tmp_path, monkeypatch, capsys
    ):
        """With no preferred bid and an open backup bid, phase 2 stops at the
        fallback safety mark instead of burning the full grace window."""
        monkeypatch.setenv("JUST_AKASH_BACKUP_FALLBACK_S", "10")
        client, sdl = _setup(
            MockAPI,
            mock_time,
            tmp_path,
            monkeypatch,
            providers="akash1pref",
            backup="akash1back",
        )
        client.get_bids.return_value = [_make_bid("akash1back", 20)]

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=500)
        assert result["provider"] == "akash1back"
        out = capsys.readouterr().out
        assert "Cutting preferred-grace short" in out

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_full_grace_preserved_without_backup_bids(
        self, MockAPI, mock_time, tmp_path, monkeypatch, capsys
    ):
        """No backup bid → the grace cut must not fire (full patience kept)."""
        monkeypatch.setenv("JUST_AKASH_BACKUP_FALLBACK_S", "10")
        client, sdl = _setup(
            MockAPI,
            mock_time,
            tmp_path,
            monkeypatch,
            providers="akash1pref",
            backup="akash1back",
        )
        client.get_bids.return_value = [_make_bid("akash1foreign", 20)]

        with pytest.raises(RuntimeError):
            deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=30)
        out = capsys.readouterr().out
        assert "Cutting preferred-grace short" not in out


class TestLeaseStaleRetry:
    STALE_ERR = RuntimeError(
        "API Error (400): Failed to create lease: Cannot create lease: "
        "The selected bid is no longer open. Please refresh and select an available bid."
    )

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_retries_next_open_bid_on_stale_400(self, MockAPI, mock_time, tmp_path, monkeypatch):
        """First lease POST hits a stale bid → re-fetch → lease the next open bid."""
        client, sdl = _setup(
            MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a,akash1b"
        )
        client.get_bids.return_value = [
            _make_bid("akash1a", 10),
            _make_bid("akash1b", 20),
        ]
        client.create_lease.side_effect = [self.STALE_ERR, {"lease": "ok"}]

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
        assert result["provider"] == "akash1b"
        assert client.create_lease.call_count == 2
        client.close_deployment.assert_not_called()

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_gives_up_when_no_other_open_bid(self, MockAPI, mock_time, tmp_path, monkeypatch):
        """Stale 400 with no remaining open bid → ONE re-deploy round
        (issue #19), and when the fresh order's lease is stale too → cleanup
        + raise."""
        client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a")
        client.get_bids.return_value = [_make_bid("akash1a", 10)]
        client.create_lease.side_effect = self.STALE_ERR

        with pytest.raises(RuntimeError, match="Failed to create lease"):
            deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
        # 1 lease attempt on the original order + 1 on the re-created order.
        assert client.create_lease.call_count == 2
        # Closed twice: the stale order at re-deploy, the new order at failure.
        assert client.close_deployment.call_count == 2

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_redeploys_once_when_all_bids_stale(self, MockAPI, mock_time, tmp_path, monkeypatch):
        """issue #19: every bid on the order expired (bids share the ORDER's
        ~5-min clock) → close the order, re-create it, lease a fresh bid
        immediately. Re-fetching bids on the same order can never recover."""
        client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a")
        client.create_deployment.side_effect = [
            {"dseq": "111", "manifest": "m1"},
            {"dseq": "222", "manifest": "m2"},
        ]
        client.get_bids.return_value = [_make_bid("akash1a", 10)]
        client.create_lease.side_effect = [self.STALE_ERR, {"lease": "ok"}]

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)

        assert result["dseq"] == "222"
        assert result["provider"] == "akash1a"
        assert client.create_lease.call_count == 2
        # Round 2 leases against the NEW order with the NEW manifest.
        _, round2_kwargs = client.create_lease.call_args
        assert round2_kwargs["dseq"] == "222"
        assert round2_kwargs["manifest"] == "m2"
        # Only the stale order was closed.
        client.close_deployment.assert_called_once_with("111")

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_jwt_flap_then_stale_redeploys(self, MockAPI, mock_time, tmp_path, monkeypatch):
        """The production failure chain (blazing run 27431505470): a slow
        'JWT has invalid claims' 400 ages the only bid past expiry, the
        retry hits 'no longer open' → re-deploy once → fresh bid leases."""
        client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a")
        client.create_deployment.side_effect = [
            {"dseq": "111", "manifest": "m1"},
            {"dseq": "222", "manifest": "m2"},
        ]
        client.get_bids.return_value = [_make_bid("akash1a", 10)]
        client.create_lease.side_effect = [
            TestLeaseTransientJWTRetry.JWT_ERR,
            self.STALE_ERR,
            {"lease": "ok"},
        ]

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)

        assert result["dseq"] == "222"
        assert client.create_lease.call_count == 3
        client.close_deployment.assert_called_once_with("111")

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_non_stale_lease_error_does_not_retry(self, MockAPI, mock_time, tmp_path, monkeypatch):
        """Other lease errors keep the original fail-fast + cleanup behavior."""
        client, sdl = _setup(
            MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a,akash1b"
        )
        client.get_bids.return_value = [
            _make_bid("akash1a", 10),
            _make_bid("akash1b", 20),
        ]
        client.create_lease.side_effect = RuntimeError("API Error (500): provider exploded")

        with pytest.raises(RuntimeError, match="Failed to create lease"):
            deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
        assert client.create_lease.call_count == 1
        client.close_deployment.assert_called_once_with("12345")


class TestLeaseTransientJWTRetry:
    """Console API intermittently 400s lease creation with "JWT has invalid
    claims" while the bid itself is healthy (observed 2026-06-11, ~50%
    flap rate against the same provider). Retry the SAME bid after a
    backoff — do NOT advance to the next provider."""

    JWT_ERR = RuntimeError("API Error (400): JWT has invalid claims")

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_retries_same_bid_on_jwt_claims_400(self, MockAPI, mock_time, tmp_path, monkeypatch):
        client, sdl = _setup(
            MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a,akash1b"
        )
        client.get_bids.return_value = [
            _make_bid("akash1a", 10),
            _make_bid("akash1b", 20),
        ]
        client.create_lease.side_effect = [self.JWT_ERR, {"lease": "ok"}]

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
        # Same provider on the retry — the bid was never the problem.
        assert result["provider"] == "akash1a"
        assert client.create_lease.call_count == 2
        client.close_deployment.assert_not_called()

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_gives_up_after_max_attempts(self, MockAPI, mock_time, tmp_path, monkeypatch):
        client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a")
        client.get_bids.return_value = [_make_bid("akash1a", 10)]
        client.create_lease.side_effect = self.JWT_ERR

        with pytest.raises(RuntimeError, match="Failed to create lease"):
            deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
        # 3 attempts, all on the same (only) provider, then cleanup.
        assert client.create_lease.call_count == 3
        client.close_deployment.assert_called_once_with("12345")
