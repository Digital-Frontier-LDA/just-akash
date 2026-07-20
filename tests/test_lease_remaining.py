"""Tests for compute_lease_runway — escrow burn-down / time-remaining calculation."""

from unittest.mock import MagicMock

import pytest

from just_akash.api import compute_lease_runway


def _deployment(escrow_uact="4850000", provider="akash1prov"):
    return {
        "deployment": {"state": "active", "dseq": "12345"},
        "leases": [{"id": {"provider": provider}}],
        "escrow_account": {"state": {"funds": [{"amount": escrow_uact, "denom": "uact"}]}},
    }


def _bids(winner_provider="akash1prov", winner_price=3.0):
    return [
        {
            "id": {"provider": winner_provider},
            "price": {"amount": winner_price, "denom": "uact"},
            "state": "open",
        },
        {
            "id": {"provider": "akash1other"},
            "price": {"amount": 5.0, "denom": "uact"},
            "state": "open",
        },
    ]


class TestComputeLeaseRunway:
    def test_computes_runway_correctly(self):
        """4.85M uact escrow, 3.0 uact/block, 6s blocks → ~2694 hours."""
        client = MagicMock()
        client.get_deployment.return_value = _deployment()
        client.get_bids.return_value = _bids()

        r = compute_lease_runway(client, "12345", block_time_s=6.0)

        assert r["dseq"] == "12345"
        assert r["provider"] == "akash1prov"
        assert r["escrow"]["amount"] == 4_850_000
        assert r["escrow"]["denom"] == "uact"
        assert r["burn_rate"]["per_block"] == 3.0
        # 3600 / 6 = 600 blocks/h; 3.0 × 600 = 1800 uact/h
        assert r["burn_rate"]["per_hour"] == 1800.0
        # 4_850_000 / 1800 ≈ 2694.4
        assert abs(r["time_remaining_hours"] - 2694.4) < 1.0
        assert "days" in r["time_remaining_display"]

    def test_usd_estimate_for_uact(self):
        client = MagicMock()
        client.get_deployment.return_value = _deployment()
        client.get_bids.return_value = _bids()

        r = compute_lease_runway(client, "12345")
        assert r["escrow"]["usd_estimate"] == 4.85  # uact is USD-pegged

    def test_raises_when_no_lease(self):
        client = MagicMock()
        client.get_deployment.return_value = {
            "deployment": {"state": "active"},
            "leases": [],
            "escrow_account": {"state": {"funds": [{"amount": "1000", "denom": "uact"}]}},
        }
        with pytest.raises(RuntimeError, match="No active lease"):
            compute_lease_runway(client, "12345")

    def test_raises_when_bid_not_found(self):
        """Lease exists but no matching bid (provider left the market)."""
        client = MagicMock()
        client.get_deployment.return_value = _deployment(provider="akash1ghost")
        client.get_bids.return_value = _bids(winner_provider="akash1prov")  # different provider

        with pytest.raises(RuntimeError, match="bid price"):
            compute_lease_runway(client, "12345")

    def test_handles_decimal_escrow_amount(self):
        """Some nodes report amounts with a decimal suffix — int-parse handles it."""
        client = MagicMock()
        client.get_deployment.return_value = _deployment(escrow_uact="4850000.000000")
        client.get_bids.return_value = _bids()

        r = compute_lease_runway(client, "12345")
        assert r["escrow"]["amount"] == 4_850_000

    def test_custom_block_time(self):
        """A faster block time (3s) doubles the burn rate → halves the runway."""
        client = MagicMock()
        client.get_deployment.return_value = _deployment()
        client.get_bids.return_value = _bids()

        r6 = compute_lease_runway(client, "12345", block_time_s=6.0)
        r3 = compute_lease_runway(client, "12345", block_time_s=3.0)

        assert r3["burn_rate"]["per_hour"] == r6["burn_rate"]["per_hour"] * 2
        assert abs(r3["time_remaining_hours"] - r6["time_remaining_hours"] / 2) < 1.0

    def test_zero_escrow_gives_zero_remaining(self):
        client = MagicMock()
        client.get_deployment.return_value = _deployment(escrow_uact="0")
        client.get_bids.return_value = _bids()

        r = compute_lease_runway(client, "12345")
        assert r["escrow"]["amount"] == 0
        assert r["time_remaining_hours"] == 0.0

    def test_block_time_zero_raises(self):
        client = MagicMock()
        client.get_deployment.return_value = _deployment()
        client.get_bids.return_value = _bids()
        with pytest.raises(RuntimeError, match="positive and finite"):
            compute_lease_runway(client, "12345", block_time_s=0.0)

    def test_block_time_negative_raises(self):
        client = MagicMock()
        client.get_deployment.return_value = _deployment()
        client.get_bids.return_value = _bids()
        with pytest.raises(RuntimeError, match="positive and finite"):
            compute_lease_runway(client, "12345", block_time_s=-1.0)

    def test_denom_mismatch_raises(self):
        """Escrow in uakt but bid price in uact → cannot compute, must raise."""
        client = MagicMock()
        client.get_deployment.return_value = {
            "deployment": {"state": "active", "dseq": "12345"},
            "leases": [{"id": {"provider": "akash1prov"}}],
            "escrow_account": {"state": {"funds": [{"amount": "1000", "denom": "uakt"}]}},
        }
        client.get_bids.return_value = _bids()  # bid price in uact
        with pytest.raises(RuntimeError, match="Denom mismatch"):
            compute_lease_runway(client, "12345")
