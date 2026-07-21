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
        # Distinct dseqs for the original vs re-created order, so the close
        # assertions are unambiguous about which order was torn down when.
        client.create_deployment.side_effect = [
            {"dseq": "111", "manifest": "m1"},
            {"dseq": "222", "manifest": "m2"},
        ]
        client.get_bids.return_value = [_make_bid("akash1a", 10)]
        client.create_lease.side_effect = self.STALE_ERR

        with pytest.raises(RuntimeError, match="Failed to create lease"):
            deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
        # 1 lease attempt on the original order + 1 on the re-created order.
        assert client.create_lease.call_count == 2
        # Closed twice: the stale order (111) at re-deploy, the new order (222)
        # when its lease is stale too.
        assert client.close_deployment.call_count == 2
        assert client.close_deployment.call_args_list[0].args == ("111",)
        assert client.close_deployment.call_args_list[1].args == ("222",)

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

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_redeploy_preserves_deposit(self, MockAPI, mock_time, tmp_path, monkeypatch):
        """The re-created order must be funded with the SAME --deposit as the
        original (issue #19 follow-up); otherwise re-deploy silently falls back
        to the default escrow."""
        client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a")
        client.create_deployment.side_effect = [
            {"dseq": "111", "manifest": "m1"},
            {"dseq": "222", "manifest": "m2"},
        ]
        client.get_bids.return_value = [_make_bid("akash1a", 10)]
        client.create_lease.side_effect = [self.STALE_ERR, {"lease": "ok"}]

        deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5, deposit=12.0)

        assert client.create_deployment.call_count == 2
        # Both the original order and the re-deploy use the same deposit.
        for call in client.create_deployment.call_args_list:
            assert call.kwargs.get("deposit") == 12.0

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_redeploy_no_fresh_bid_cleans_up_and_raises(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        """If the re-created order never yields an open allowlisted bid, the
        fast-poll times out → the new order is cleaned up and the failure is
        surfaced with an accurate cause."""
        client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a")
        client.create_deployment.side_effect = [
            {"dseq": "111", "manifest": "m1"},
            {"dseq": "222", "manifest": "m2"},
        ]
        # Order 111 has a bid (drives selection + the stale lease); the
        # re-created order 222 yields nothing.
        client.get_bids.side_effect = lambda d: [_make_bid("akash1a", 10)] if d == "111" else []
        client.create_lease.side_effect = self.STALE_ERR

        with pytest.raises(RuntimeError, match="no fresh open bid on re-created order 222"):
            deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)

        assert client.create_lease.call_count == 1  # round 2 never reached
        # Closed twice: stale order 111 at re-deploy, new order 222 on timeout.
        assert client.close_deployment.call_count == 2
        assert client.close_deployment.call_args_list[0].args == ("111",)
        assert client.close_deployment.call_args_list[1].args == ("222",)

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_redeploy_retries_close_then_proceeds(self, MockAPI, mock_time, tmp_path, monkeypatch):
        """A transient close failure is retried; once the close succeeds the
        re-deploy proceeds normally and leases the fresh order."""
        client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a")
        client.create_deployment.side_effect = [
            {"dseq": "111", "manifest": "m1"},
            {"dseq": "222", "manifest": "m2"},
        ]
        client.get_bids.return_value = [_make_bid("akash1a", 10)]
        client.create_lease.side_effect = [self.STALE_ERR, {"lease": "ok"}]
        # First close attempt flaps, second succeeds.
        client.close_deployment.side_effect = [RuntimeError("transient flap"), None]

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)

        assert result["dseq"] == "222"
        assert client.create_deployment.call_count == 2  # re-deploy happened
        assert client.close_deployment.call_count == 2  # 1 failed + 1 success

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_redeploy_aborts_when_close_persistently_fails(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        """If the stale order can't be closed after retries, abort rather than
        double-fund: do NOT create a second order, and surface the orphaned
        dseq with an actionable remediation."""
        client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a")
        client.create_deployment.side_effect = [
            {"dseq": "111", "manifest": "m1"},
            {"dseq": "222", "manifest": "m2"},
        ]
        client.get_bids.return_value = [_make_bid("akash1a", 10)]
        client.create_lease.side_effect = self.STALE_ERR
        client.close_deployment.side_effect = RuntimeError("close exploded")

        with pytest.raises(RuntimeError, match="just-akash destroy --dseq 111"):
            deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)

        # Aborted before re-deploy: only the ORIGINAL order was ever created,
        # so no second (double-funded) order exists.
        assert client.create_deployment.call_count == 1
        assert client.close_deployment.call_count == 3  # 3 retries, all failed


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

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_redeploy_selects_backup_bid_on_recreated_order(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        """On the re-created order, when only a BACKUP bid is available, the
        fast-poll's courtesy-window branch selects it (covers _poll_fresh_bid's
        backup path). Courtesy=0 accepts the backup immediately."""
        monkeypatch.setenv("JUST_AKASH_REDEPLOY_BACKUP_COURTESY_S", "0")
        client, sdl = _setup(
            MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1pref", backup="akash1back"
        )
        client.create_deployment.side_effect = [
            {"dseq": "111", "manifest": "m1"},
            {"dseq": "222", "manifest": "m2"},
        ]

        def _bids(d):
            # Original order: a preferred bid (drives selection). Re-created
            # order: only a backup bid.
            return [_make_bid("akash1pref", 10)] if d == "111" else [_make_bid("akash1back", 50)]

        client.get_bids.side_effect = _bids
        stale_err = RuntimeError(
            "API Error (400): Failed to create lease: Cannot create lease: "
            "The selected bid is no longer open. Please refresh and select an available bid."
        )
        client.create_lease.side_effect = [stale_err, {"lease": "ok"}]

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)

        assert result["dseq"] == "222"
        assert result["provider"] == "akash1back"  # backup tier selected post-redeploy
        client.close_deployment.assert_called_once_with("111")


class TestLeaseNoOrder404Retry:
    """The lease-CREATE 404 'no lease for deployment' (the hgulk CI flake — order
    becomes un-leaseable during the bid-wait) is recovered by the issue-#19
    re-deploy round, NOT a terminal failure. Unlike a stale bid it skips the
    same-order bid re-fetch (the order is gone) and goes straight to re-deploy."""

    NO_ORDER_ERR = RuntimeError("API Error (404): no lease for deployment\n")

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_redeploys_on_no_lease_404(self, MockAPI, mock_time, tmp_path, monkeypatch):
        """404 on the first order → close it, re-create, lease the fresh bid."""
        client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a")
        client.create_deployment.side_effect = [
            {"dseq": "111", "manifest": "m1"},
            {"dseq": "222", "manifest": "m2"},
        ]
        client.get_bids.return_value = [_make_bid("akash1a", 10)]
        client.create_lease.side_effect = [self.NO_ORDER_ERR, {"lease": "ok"}]

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)

        assert result["dseq"] == "222"
        assert result["provider"] == "akash1a"
        assert client.create_lease.call_count == 2
        # The gone order was closed; the fresh order was leased.
        client.close_deployment.assert_called_once_with("111")
        _, round2_kwargs = client.create_lease.call_args
        assert round2_kwargs["dseq"] == "222"

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_404_skips_same_order_bid_refetch(
        self, MockAPI, mock_time, tmp_path, monkeypatch, capsys
    ):
        """A 404 must NOT trigger the stale-bid same-order re-fetch (which would log
        're-fetching open bids') — it goes straight to re-deploy, logging the
        no_order reason. Assert on the log so the skip is actually proven, not just
        inferred from a re-deploy having happened."""
        client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a")
        client.create_deployment.side_effect = [
            {"dseq": "111", "manifest": "m1"},
            {"dseq": "222", "manifest": "m2"},
        ]
        client.get_bids.return_value = [_make_bid("akash1a", 10)]
        client.create_lease.side_effect = [self.NO_ORDER_ERR, {"lease": "ok"}]

        deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
        out = capsys.readouterr().out
        # The stale-bid same-order re-fetch was NOT taken:
        assert "re-fetching open bids" not in out
        # The no_order reason is in the re-deploy log (proves the 404 path):
        assert "order un-leaseable (404" in out

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_404_then_redeploy_also_fails_is_terminal(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        """If the re-created order ALSO 404s, it's a terminal failure (one bounded
        re-deploy round, like issue #19) — both orders are torn down."""
        client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a")
        client.create_deployment.side_effect = [
            {"dseq": "111", "manifest": "m1"},
            {"dseq": "222", "manifest": "m2"},
        ]
        client.get_bids.return_value = [_make_bid("akash1a", 10)]
        client.create_lease.side_effect = self.NO_ORDER_ERR  # always 404

        with pytest.raises(RuntimeError, match="Failed to create lease"):
            deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
        assert client.create_lease.call_count == 2  # one attempt per order
        assert client.close_deployment.call_count == 2  # both orders torn down


class TestRedeployProviderDiversification:
    """issue #84 — the bounded re-deploy round must not deterministically re-pick
    the provider that just failed. `_poll_fresh_bid` takes the cheapest bid, so
    whenever the failed provider is also the cheapest, the retry is guaranteed to
    reproduce the failure and the one re-deploy round is spent for nothing.

    The correction is SOFT (de-prioritise, not exclude) and scoped to the 404
    path: a `stale` failure belongs to the order's shared bid clock, not to the
    provider.
    """

    NO_ORDER_ERR = RuntimeError("API Error (404): no lease for deployment\n")
    STALE_ERR = RuntimeError(
        "API Error (400): Failed to create lease: Cannot create lease: "
        "The selected bid is no longer open. Please refresh and select an available bid."
    )

    @staticmethod
    def _two_orders(client):
        client.create_deployment.side_effect = [
            {"dseq": "111", "manifest": "m1"},
            {"dseq": "222", "manifest": "m2"},
        ]

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_404_redeploy_prefers_a_different_provider(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        """The regression from run 29765070530: both providers bid on the fresh
        order and the failed one is CHEAPER, so the pre-fix code re-picked it.
        The fresh order must lease the other provider instead."""
        client, sdl = _setup(
            MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1bad,akash1good"
        )
        self._two_orders(client)
        # akash1bad is cheapest on BOTH orders — it wins initial selection, and
        # pre-fix it would win the re-selection too.
        client.get_bids.return_value = [_make_bid("akash1bad", 10), _make_bid("akash1good", 50)]
        client.create_lease.side_effect = [self.NO_ORDER_ERR, {"lease": "ok"}]

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)

        assert result["dseq"] == "222"
        assert result["provider"] == "akash1good"  # NOT the cheapest — the one that works

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_404_redeploy_falls_back_to_the_failed_provider_when_sole_bidder(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        """De-prioritise, don't ban. With n=2 evidence we can't prove the provider
        is at fault (versus Console-side order GC), and the allowlisted market is
        thin — so when it is the ONLY bidder on the fresh order, lease it rather
        than fail the deploy outright."""
        client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a")
        self._two_orders(client)
        client.get_bids.return_value = [_make_bid("akash1a", 10)]
        client.create_lease.side_effect = [self.NO_ORDER_ERR, {"lease": "ok"}]

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)

        assert result["dseq"] == "222"
        assert result["provider"] == "akash1a"  # leased anyway — soft, not a ban

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_soft_skip_survives_a_courtesy_window_longer_than_the_wait(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        """Misconfiguration must not silently promote the soft skip to a hard ban.

        The fallback branch is gated on `elapsed >= courtesy_s`, but the poll
        loop gives up at `elapsed >= wait_s` — so with courtesy >= wait the
        fallback could never fire, and a sole de-prioritised bidder would fail
        the deploy even though a leasable bid was sitting right there (and the
        'still leasable if nothing else bids' log would be a lie).
        """
        monkeypatch.setenv("JUST_AKASH_REDEPLOY_WAIT_S", "10")
        monkeypatch.setenv("JUST_AKASH_REDEPLOY_BACKUP_COURTESY_S", "999")
        client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a")
        self._two_orders(client)
        client.get_bids.return_value = [_make_bid("akash1a", 10)]
        client.create_lease.side_effect = [self.NO_ORDER_ERR, {"lease": "ok"}]

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)

        assert result["dseq"] == "222"
        assert result["provider"] == "akash1a"  # leased, not banned by a bad window

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_misconfigured_window_fallback_stays_tier_first(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        """The misconfiguration fallback must not quietly become the one place
        that ignores tier order.

        Tier order only decides among providers that ALL just failed, so both
        bidders have to be de-prioritised for this to mean anything: a stale
        400 on the PREFERRED bid banks it in `failed_providers` and advances to
        the BACKUP, whose 404 then carries BOTH into the re-deploy round. The
        BACKUP is cheaper; the PREFERRED one still wins.
        """
        monkeypatch.setenv("JUST_AKASH_REDEPLOY_WAIT_S", "10")
        monkeypatch.setenv("JUST_AKASH_REDEPLOY_BACKUP_COURTESY_S", "999")
        client, sdl = _setup(
            MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1pref", backup="akash1back"
        )
        self._two_orders(client)
        # akash1back is cheaper, but akash1pref is the higher tier.
        client.get_bids.return_value = [_make_bid("akash1pref", 90), _make_bid("akash1back", 5)]
        client.create_lease.side_effect = [
            self.STALE_ERR,  # akash1pref -> failed_providers
            self.NO_ORDER_ERR,  # akash1back -> re-deploy, both de-prioritised
            {"lease": "ok"},
        ]

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)

        assert result["provider"] == "akash1pref"  # tier beats price, as everywhere else

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_misconfigured_window_still_prefers_a_provider_that_did_not_fail(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        """Tier order decides only AMONG failed providers — it must not outrank
        the soft skip itself. Here only the PREFERRED provider failed, so the
        fresh BACKUP wins the fallback, exactly as it does on the normal
        courtesy path (`..._prefers_fresh_backup_over_failed_preferred`)."""
        monkeypatch.setenv("JUST_AKASH_REDEPLOY_WAIT_S", "10")
        monkeypatch.setenv("JUST_AKASH_REDEPLOY_BACKUP_COURTESY_S", "999")
        client, sdl = _setup(
            MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1pref", backup="akash1back"
        )
        self._two_orders(client)
        # The failed provider is also the CHEAPER one, so price can't explain a pass.
        client.get_bids.return_value = [_make_bid("akash1pref", 5), _make_bid("akash1back", 90)]
        client.create_lease.side_effect = [self.NO_ORDER_ERR, {"lease": "ok"}]

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)

        assert result["provider"] == "akash1back"  # not-just-failed beats tier AND price

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_transient_bid_fetch_failure_does_not_erase_the_fallback(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        """A `get_bids` flap on a late poll must not clear the recorded bids and
        re-create the ban this fallback exists to prevent — only non-empty polls
        overwrite what was seen."""
        monkeypatch.setenv("JUST_AKASH_REDEPLOY_WAIT_S", "10")
        monkeypatch.setenv("JUST_AKASH_REDEPLOY_BACKUP_COURTESY_S", "999")
        client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a")
        self._two_orders(client)
        seen = {"n": 0}

        def _bids(d):
            if d != "222":
                return [_make_bid("akash1a", 10)]
            # Fresh order: bids on the first poll, then the Console flaps for
            # every remaining poll in the window.
            seen["n"] += 1
            if seen["n"] == 1:
                return [_make_bid("akash1a", 10)]
            raise RuntimeError("API Error (503): upstream unavailable")

        client.get_bids.side_effect = _bids
        client.create_lease.side_effect = [self.NO_ORDER_ERR, {"lease": "ok"}]

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)

        assert seen["n"] > 1  # the flap really did happen after the good poll
        assert result["provider"] == "akash1a"

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_404_redeploy_prefers_fresh_backup_over_failed_preferred(
        self, MockAPI, mock_time, tmp_path, monkeypatch
    ):
        """A provider that has not just failed beats one that has, even across a
        tier drop: the failed PREFERRED provider yields to a working BACKUP."""
        monkeypatch.setenv("JUST_AKASH_REDEPLOY_BACKUP_COURTESY_S", "0")
        client, sdl = _setup(
            MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1pref", backup="akash1back"
        )
        self._two_orders(client)
        client.get_bids.return_value = [_make_bid("akash1pref", 10), _make_bid("akash1back", 50)]
        client.create_lease.side_effect = [self.NO_ORDER_ERR, {"lease": "ok"}]

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)

        assert result["provider"] == "akash1back"

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_404_logs_the_deprioritised_provider(
        self, MockAPI, mock_time, tmp_path, monkeypatch, capsys
    ):
        """The operator log names who is being skipped and that it's not a ban —
        otherwise a re-deploy that changes provider looks arbitrary."""
        client, sdl = _setup(
            MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1bad,akash1good"
        )
        self._two_orders(client)
        client.get_bids.return_value = [_make_bid("akash1bad", 10), _make_bid("akash1good", 50)]
        client.create_lease.side_effect = [self.NO_ORDER_ERR, {"lease": "ok"}]

        deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
        out = capsys.readouterr().out
        assert "Preferring a provider other than akash1bad" in out
        assert "still leasable if nothing else bids" in out

    @patch("just_akash.deploy.time")
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_stale_redeploy_does_not_deprioritize(
        self, MockAPI, mock_time, tmp_path, monkeypatch, capsys
    ):
        """The `stale` path must keep re-picking the cheapest bid, including the
        provider whose bid expired. Bids share the ORDER's ~5-min clock, so
        expiry carries no provider-specific signal — on a NEW order that provider
        is as good as any, and skipping it would shrink a thin market for nothing.
        """
        client, sdl = _setup(
            MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1a,akash1b"
        )
        self._two_orders(client)
        expired = {"yet": False}

        def _bids(d):
            # Fresh order: both bid, akash1a cheapest.
            if d == "222":
                return [_make_bid("akash1a", 10), _make_bid("akash1b", 50)]
            # Original order: open through selection, then every bid expires the
            # moment the lease is attempted — so the same-order re-fetch finds
            # nothing and the re-deploy round is the only way forward.
            state = "closed" if expired["yet"] else "open"
            return [_make_bid("akash1a", 10, state=state), _make_bid("akash1b", 50, state=state)]

        def _lease(**kwargs):
            if kwargs["dseq"] == "111":
                expired["yet"] = True
                raise self.STALE_ERR
            return {"lease": "ok"}

        client.get_bids.side_effect = _bids
        client.create_lease.side_effect = _lease

        result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)

        assert result["dseq"] == "222"
        assert result["provider"] == "akash1a"  # cheapest, NOT skipped
        assert "Preferring a provider other than" not in capsys.readouterr().out
