"""Unit tests for capacity.py — the bid-based capacity oracle behind `capacity-probe`.

The oracle's contract: create a throwaway order, read real bids, and ALWAYS close the
order without ever leasing (no escrow leak, no container). These tests pin that contract
with a fake client, so the deterministic cleanup the ad-hoc probe scripts kept getting
wrong is now enforced.
"""

from __future__ import annotations

from typing import Any

import pytest

from just_akash import capacity

# A minimal deployment-create response carrying a dseq (mirrors the Console shape
# `_extract_dseq` understands).
DEP: dict[str, Any] = {"deployment": {"id": {"owner": "akash1me", "dseq": "42"}}}


def _bid(provider: str, amount: str = "100", denom: str = "uact", state: str | None = None):
    b: dict[str, Any] = {"id": {"provider": provider}, "price": {"denom": denom, "amount": amount}}
    if state is not None:
        b["state"] = state
    return b


class _FakeClient:
    def __init__(self, dep, bids_rounds, close_raises: bool = False):
        self._dep = dep
        self._rounds = list(bids_rounds)
        self._close_raises = close_raises
        self.created_sdl: str | None = None
        self.deposit: float | None = None
        self.closed: list[str] = []

    def create_deployment(self, sdl_content, deposit: float = 5.0):
        self.created_sdl = sdl_content
        self.deposit = deposit
        return self._dep

    def get_bids(self, dseq):
        return self._rounds.pop(0) if self._rounds else []

    def close_deployment(self, dseq):
        self.closed.append(dseq)
        if self._close_raises:
            raise RuntimeError("close failed")
        return {}


class TestBuildProbeSdl:
    def test_pins_model_and_count(self):
        sdl = capacity.build_probe_sdl(2, "v100")
        assert "units: 2" in sdl
        assert "nvidia: [{ model: v100 }]" in sdl

    def test_any_nvidia_when_model_omitted(self):
        assert "nvidia: []" in capacity.build_probe_sdl(1, None)

    def test_rejects_zero_count(self):
        with pytest.raises(ValueError):
            capacity.build_probe_sdl(0, "v100")


class TestCapacityProbe:
    def test_placeable_when_a_bid_arrives(self):
        c = _FakeClient(DEP, [[_bid("akash1p")]])
        res = capacity.capacity_probe(c, 2, "v100", wait_s=0)
        assert res["placeable"] is True
        assert res["bidders"] == [
            {"provider": "akash1p", "price_amount": 100.0, "price_denom": "uact"}
        ]
        assert res["gpu_model"] == "v100"
        assert c.closed == ["42"]  # order always closed, no lease created

    def test_no_bid_after_wait(self):
        c = _FakeClient(DEP, [[]])
        res = capacity.capacity_probe(c, 4, "v100", wait_s=0)
        assert res["placeable"] is False
        assert res["bidders"] == []
        assert c.closed == ["42"]

    def test_dedups_providers_and_filters(self):
        c = _FakeClient(DEP, [[_bid("akash1p"), _bid("akash1p"), _bid("akash1q")]])
        res = capacity.capacity_probe(c, 1, None, wait_s=0, provider="akash1q")
        assert [b["provider"] for b in res["bidders"]] == ["akash1q"]

    def test_skips_non_open_bids(self):
        c = _FakeClient(DEP, [[_bid("akash1p", state="closed")]])
        res = capacity.capacity_probe(c, 1, "v100", wait_s=0)
        assert res["placeable"] is False

    def test_order_closed_even_if_get_bids_raises(self):
        class _Boom(_FakeClient):
            def get_bids(self, dseq):
                raise RuntimeError("boom")

        c = _Boom(DEP, [])
        with pytest.raises(RuntimeError, match="boom"):
            capacity.capacity_probe(c, 1, "v100", wait_s=0)
        assert c.closed == ["42"]  # finally still closed the order

    def test_close_failure_is_swallowed(self):
        c = _FakeClient(DEP, [[_bid("akash1p")]], close_raises=True)
        res = capacity.capacity_probe(c, 1, "v100", wait_s=0)  # must not raise
        assert res["placeable"] is True

    def test_missing_dseq_raises_and_does_not_close(self):
        c = _FakeClient({"no": "dseq"}, [])
        with pytest.raises(RuntimeError, match="order was not created"):
            capacity.capacity_probe(c, 1, "v100", wait_s=0)
        assert c.closed == []  # nothing to close

    def test_sends_probe_sdl_with_small_deposit(self):
        c = _FakeClient(DEP, [[_bid("akash1p")]])
        capacity.capacity_probe(c, 2, "v100", wait_s=0)
        assert c.created_sdl is not None and "units: 2" in c.created_sdl
        assert c.deposit == 0.5
