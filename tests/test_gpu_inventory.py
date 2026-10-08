"""V100 inventory: an order-only, UNPINNED survey of who can place which V100 shape.

openmix-67w3 AC3. The bid probe pins every order to OUR providers on purpose (an
unpinned order loses the 20-bid race), so it cannot answer "who on the whole
network can place 4x V100-32GB SXM2?". This mode asks exactly that: one order
per shape, open to every provider, waits the FULL window for every bidder, and
closes the order. It never leases — the bid set is the datum.

A units:N bid means one node has N free GPUs of that shape (Akash places one
replica on one node), so the largest N a provider bids on is its free GPUs per
node for that shape — the number the bead needs and /v1/providers does not show.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from just_akash import capacity, gpu_inventory
from just_akash.gpu_inventory import (
    CONTROL,
    V100_SHAPES,
    Shape,
    build_inventory_sdl,
    exit_code,
    run_inventory,
    summarize,
)


class FakeClient:
    """Bids keyed by (units, ram, interface); records every create and close."""

    def __init__(self, bids: dict[tuple[int, str, str], list[str]]):
        self.bids = bids
        self.created: list[str] = []
        self.closed: list[str] = []
        self.leased: list[str] = []
        self._sdl: dict[str, str] = {}

    def create_deployment(self, sdl: str, deposit: float = 0.5) -> dict[str, Any]:
        dseq = str(1000 + len(self.created))
        self.created.append(dseq)
        self._sdl[dseq] = sdl
        return {"dseq": dseq, "owner": "akash1owner"}

    def get_bids(self, dseq: str) -> list[dict[str, Any]]:
        gpu = yaml.safe_load(self._sdl[dseq])["profiles"]["compute"]["probe"]["resources"]["gpu"]
        attrs = gpu["attributes"]["vendor"]["nvidia"][0]
        key = (gpu["units"], attrs.get("ram", ""), attrs.get("interface", ""))
        return [
            {
                "bid": {
                    "id": {"provider": p},
                    "state": "open",
                    "price": {"amount": "12.5", "denom": "uact"},
                },
            }
            for p in self.bids.get(key, [])
        ]

    def close_deployment(self, dseq: str) -> None:
        self.closed.append(dseq)

    def create_lease(self, *_a: Any, **_k: Any) -> None:  # pragma: no cover - must never run
        self.leased.append("LEASE")
        raise AssertionError("inventory must never lease")


def test_shapes_cover_v100_16_and_32gb_sxm2_and_pcie_up_to_8():
    assert {s.ram for s in V100_SHAPES} == {"16Gi", "32Gi"}
    assert {s.interface for s in V100_SHAPES} == {"sxm", "pcie"}
    assert {s.units for s in V100_SHAPES} == {1, 2, 4, 8}


def test_sdl_is_unpinned_and_asks_for_the_exact_shape():
    sdl = yaml.safe_load(build_inventory_sdl(Shape(4, "32Gi", "sxm")))
    gpu = sdl["profiles"]["compute"]["probe"]["resources"]["gpu"]
    assert gpu["units"] == 4
    assert gpu["attributes"]["vendor"]["nvidia"] == [
        {"model": "v100", "ram": "32Gi", "interface": "sxm"}
    ]
    # No placement attributes: an inventory pinned to one provider surveys nothing.
    assert "attributes" not in sdl["profiles"]["placement"]["dcloud"]


def test_every_bidder_is_collected_not_just_the_first(monkeypatch):
    monkeypatch.setattr(capacity.time, "sleep", lambda _s: None)
    client = FakeClient({(4, "16Gi", "sxm"): ["akash1h4i", "akash1other"]})
    records = run_inventory(client, shapes=[Shape(4, "16Gi", "sxm")], wait_s=10, poll_s=5)
    assert sorted(b["provider"] for b in records[0]["bidders"]) == ["akash1h4i", "akash1other"]


def test_every_order_is_closed_and_none_is_leased(monkeypatch):
    monkeypatch.setattr(capacity.time, "sleep", lambda _s: None)
    client = FakeClient({(1, "32Gi", "pcie"): ["akash1zen"]})
    run_inventory(client, shapes=V100_SHAPES, wait_s=5, poll_s=5)
    assert client.created and sorted(client.created) == sorted(client.closed)
    assert client.leased == []


def test_summary_reports_largest_shape_per_provider_as_gpus_per_node(monkeypatch):
    monkeypatch.setattr(capacity.time, "sleep", lambda _s: None)
    client = FakeClient(
        {
            (1, "32Gi", "pcie"): ["akash1zen"],
            (2, "32Gi", "pcie"): ["akash1zen"],
            (1, "16Gi", "sxm"): ["akash1h4i"],
            (4, "16Gi", "sxm"): ["akash1h4i"],
        }
    )
    rows = summarize(run_inventory(client, shapes=V100_SHAPES, wait_s=5, poll_s=5))
    by = {(r["provider"], r["ram"], r["interface"]): r for r in rows}
    assert by[("akash1zen", "32Gi", "pcie")]["free_gpus_per_node_at_least"] == 2
    assert by[("akash1h4i", "16Gi", "sxm")]["free_gpus_per_node_at_least"] == 4
    assert by[("akash1h4i", "16Gi", "sxm")]["bid_uact_per_block"] == 12.5


def test_a_failed_order_is_recorded_not_raised(monkeypatch):
    monkeypatch.setattr(capacity.time, "sleep", lambda _s: None)

    class Boom(FakeClient):
        def create_deployment(self, sdl: str, deposit: float = 0.5) -> dict[str, Any]:
            raise RuntimeError("rpc down")

    records = run_inventory(Boom({}), shapes=[Shape(1, "16Gi", "sxm")], wait_s=5, poll_s=5)
    assert records[0]["error"].startswith("RuntimeError")
    assert records[0]["bidders"] == []


@pytest.mark.parametrize("units", [0, -1])
def test_bad_units_refused(units):
    with pytest.raises(ValueError):
        build_inventory_sdl(Shape(units, "16Gi", "sxm"))


# ── review openmix-67w3 F6 / F9 / gaps e, f ─────────────────────────────────────


def test_the_control_asks_for_any_v100_and_runs_first():
    sdl = yaml.safe_load(build_inventory_sdl(CONTROL))
    gpu = sdl["profiles"]["compute"]["probe"]["resources"]["gpu"]
    assert gpu["units"] == 1
    assert gpu["attributes"]["vendor"]["nvidia"] == [{"model": "v100"}]
    import inspect

    default = inspect.signature(run_inventory).parameters["shapes"].default
    assert default[0] == CONTROL and set(default[1:]) == set(V100_SHAPES)


def test_the_control_is_not_a_shape_row_in_the_summary(monkeypatch):
    monkeypatch.setattr(capacity.time, "sleep", lambda _s: None)
    client = FakeClient({(1, "", ""): ["akash1zen"]})
    records = run_inventory(client, shapes=[CONTROL, Shape(1, "16Gi", "sxm")], wait_s=5, poll_s=5)
    assert records[0]["control"] is True and records[0]["bidders"]
    assert summarize(records) == []


def test_zeros_without_a_bidding_control_fail_the_run(monkeypatch):
    # The attribute-spelling case: nothing bids on ANY shape, the control included.
    monkeypatch.setattr(capacity.time, "sleep", lambda _s: None)
    records = run_inventory(FakeClient({}), shapes=[CONTROL, *V100_SHAPES], wait_s=5, poll_s=5)
    assert exit_code(records) == 2


def test_zeros_with_a_bidding_control_are_a_trusted_inventory(monkeypatch):
    monkeypatch.setattr(capacity.time, "sleep", lambda _s: None)
    client = FakeClient({(1, "", ""): ["akash1zen"]})
    records = run_inventory(client, shapes=[CONTROL, *V100_SHAPES], wait_s=5, poll_s=5)
    assert exit_code(records) == 0


def test_a_survey_where_every_order_errored_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(capacity.time, "sleep", lambda _s: None)
    monkeypatch.setenv("AKASH_API_KEY", "k")

    class Boom(FakeClient):
        def __init__(self, _key: str):
            super().__init__({})

        def create_deployment(self, sdl: str, deposit: float = 0.5) -> dict[str, Any]:
            raise RuntimeError("rpc down")

    import just_akash.api

    monkeypatch.setattr(just_akash.api, "AkashConsoleAPI", Boom)
    assert gpu_inventory.main(["--wait", "0"]) == 1


def test_collect_all_waits_the_window_and_keeps_a_late_bidder(monkeypatch):
    # gap e: bidders arrive on DIFFERENT polls. Without collect_all the probe returns on the
    # first bidder and the late one is never seen.
    monkeypatch.setattr(capacity.time, "sleep", lambda _s: None)

    class Staggered(FakeClient):
        polls = 0

        def get_bids(self, dseq: str) -> list[dict[str, Any]]:
            self.polls += 1
            provs = ["akash1early"] + (["akash1late"] if self.polls >= 3 else [])
            return [
                {
                    "bid": {
                        "id": {"provider": p},
                        "state": "open",
                        "price": {"amount": "1", "denom": "uact"},
                    }
                }
                for p in provs
            ]

    sdl = build_inventory_sdl(Shape(1, "16Gi", "sxm"))
    all_ = capacity.probe_order_sdl(Staggered({}), sdl, wait_s=20, poll_s=5, collect_all=True)
    assert sorted(b["provider"] for b in all_["bidders"]) == ["akash1early", "akash1late"]
    assert all_["waited_s"] == 20
    first = capacity.probe_order_sdl(Staggered({}), sdl, wait_s=20, poll_s=5)
    assert [b["provider"] for b in first["bidders"]] == ["akash1early"]
