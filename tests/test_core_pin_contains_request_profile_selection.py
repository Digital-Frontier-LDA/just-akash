"""The pinned akash-lease-core actually contains what just-akash relies on.

A version string or an ancestry claim is not evidence: release histories in this org have
diverged from main before. These assert the BEHAVIOUR of akash-lease-core#47 — including
the two defects its final review found — so a pin that resolves to a core without them
goes red here, not in production placement.

v0.15.1 (akash-lease-core#51, closing #50) adds the contract asserted at the bottom of this
file: provider-scoped EMPTIEST degradation (L1), anti-affinity in the degraded fallback (L2),
an aggregate contradicting its node list is unreadable (L3), and an aggregate below the
request proves insufficiency without a node list (L4). The same L1/L2 are proven through
deploy() in tests/test_deploy_request_profile_call_sites.py.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from akash_lease_core import (
    Auction,
    AuctionPolicy,
    BidObservation,
    BidRejectionReason,
    CapacityFit,
    NodeCapacity,
    ProviderCapacity,
    ResourceProfile,
    SelectionReason,
    from_provider_status,
)
from akash_lease_core.auction import PreferredSelection


def _status(free: int) -> dict:
    node = {
        "allocatable": {"cpu": 10_000, "memory": 10_000, "storage_ephemeral": 10_000, "gpu": 0},
        "available": {"cpu": free, "memory": free, "storage_ephemeral": free, "gpu": 0},
    }
    return {"cluster": {"inventory": {"available": {"nodes": [node]}}}}


def _decide(policy_selection, observations):
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            preferred_providers=frozenset(o.provider for o in observations),
            preferred_selection=policy_selection,
        ),
        started_at=0,
    )
    for observation in observations:
        auction.observe(observation)
    return auction.evaluate(now=10)


def _bid(provider: str, price: str, *, gseq, profile, free: int) -> BidObservation:
    return BidObservation(
        bid_key=f"{provider}/{gseq}",
        provider=provider,
        price=Decimal(price),
        denom="uakt",
        observed_at=1,
        state="open",
        capacity=from_provider_status(_status(free)),
        gseq=gseq,
        resource_profile=profile,
    )


def test_request_profile_types_exist() -> None:
    assert ResourceProfile(cpu_millicores=1).requested_dimensions == ("cpu",)
    assert SelectionReason.EMPTIEST_REQUEST_PROFILE_UNAVAILABLE_FELL_BACK_TO_CHEAPEST
    assert BidRejectionReason.RESOURCE_PROFILE_GROUP_UNBOUND


def test_late_fix_bool_totals_are_rejected() -> None:
    """#47 final review, gap 2: `True` is an int subclass and must not count as 1 unit."""
    with pytest.raises(ValueError):
        ResourceProfile(cpu_millicores=True)  # type: ignore[arg-type]


def test_late_fix_an_unbound_profiled_bid_cannot_win_cheapest() -> None:
    """#47 final review, gap 1: a profiled bid with no gseq was selectable under CHEAPEST."""
    profile = ResourceProfile(cpu_millicores=100)
    unbound_cheapest = _bid("akash1unbound", "1", gseq=None, profile=profile, free=5000)
    bound_dearer = _bid("akash1bound", "9", gseq=1, profile=profile, free=5000)
    result = _decide(PreferredSelection.CHEAPEST, [unbound_cheapest, bound_dearer])
    assert result.selected is not None
    assert result.selected.provider == "akash1bound"
    assert any(
        r.provider == "akash1unbound"
        and r.reason is BidRejectionReason.RESOURCE_PROFILE_GROUP_UNBOUND
        for r in result.rejected
    )


def test_emptiest_rejects_a_provider_that_cannot_fit_the_aggregate() -> None:
    """The capability #346 exists to feed."""
    profile = ResourceProfile(cpu_millicores=2000)
    too_small_but_cheap = _bid("akash1small", "1", gseq=1, profile=profile, free=1500)
    fits = _bid("akash1fits", "9", gseq=1, profile=profile, free=4000)
    result = _decide(PreferredSelection.EMPTIEST, [too_small_but_cheap, fits])
    assert result.selected is not None and result.selected.provider == "akash1fits"


# ── v0.15.1 contract (akash-lease-core#51) ─────────────────────────────────────────────


def _complete(free: int) -> ProviderCapacity:
    return ProviderCapacity.from_totals(
        cpu=(free, 1000), node_capacities=(NodeCapacity(cpu_millicores_available=free),)
    )


def _partial(free: int) -> ProviderCapacity:
    """Fit is provable from the readable node; the provider-wide fraction is not."""
    return ProviderCapacity.from_totals(
        cpu=(free, 1000),
        node_capacities=(NodeCapacity(cpu_millicores_available=free), NodeCapacity()),
    )


def _emptiest(rows, *, already_selected=None):
    profile = ResourceProfile(cpu_millicores=100)
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=10,
            preferred_providers=frozenset(p for p, _, _ in rows),
            preferred_selection=PreferredSelection.EMPTIEST,
        ),
        started_at=0,
    )
    for index, (provider, price, capacity) in enumerate(rows, 1):
        auction.observe(
            BidObservation(
                bid_key=f"bid-{index}",
                provider=provider,
                price=Decimal(price),
                denom="uakt",
                observed_at=float(index),
                capacity=capacity,
                resource_profile=profile,
                gseq=1,
            )
        )
    return auction.evaluate(now=11, already_selected=already_selected)


def test_v0_15_1_L1_degradation_is_provider_scoped() -> None:
    result = _emptiest(
        [("q", "9", _complete(600)), ("r", "1", _complete(300)), ("p", "5", _partial(900))]
    )
    assert result.selected is not None and result.selected.provider == "q"
    assert result.selection_reason is SelectionReason.EMPTIEST_PREFERRED


def test_v0_15_1_L2_the_degraded_auction_keeps_anti_affinity() -> None:
    rows = [("q", "1", _complete(600)), ("r", "5", _complete(300)), ("p", "3", _partial(900))]
    result = _emptiest(rows, already_selected=frozenset({"q"}))
    assert result.selected is not None and result.selected.provider == "r"


def test_v0_15_1_L3_an_aggregate_contradicting_its_nodes_is_unreadable() -> None:
    capacity = ProviderCapacity.from_totals(
        cpu=(5_000, 32_000), node_capacities=(NodeCapacity(cpu_millicores_available=16_000),)
    )
    fit = capacity.fit(ResourceProfile(cpu_millicores=8_000))
    assert fit is CapacityFit.REQUIRED_DIMENSION_UNREADABLE


def test_v0_15_1_L4_an_aggregate_below_the_request_proves_insufficiency() -> None:
    capacity = ProviderCapacity.from_totals(cpu=(4_000, 32_000))
    fit = capacity.fit(ResourceProfile(cpu_millicores=8_000))
    assert fit is CapacityFit.INSUFFICIENT_CAPACITY


def test_v0_15_2_an_integer_above_float_range_is_unreadable_not_an_overflow() -> None:
    """akash-lease-core#53. just_akash.provider_capacity.capacity_for does not catch
    OverflowError, so the adapter itself must not raise on a 401-digit /status value."""
    huge = 10**400
    node = {
        "allocatable": {"cpu": huge, "memory": 10_000, "storage_ephemeral": 10_000, "gpu": 0},
        "available": {"cpu": huge, "memory": 10_000, "storage_ephemeral": 10_000, "gpu": 0},
    }
    capacity = from_provider_status({"cluster": {"inventory": {"available": {"nodes": [node]}}}})
    fit = capacity.fit(ResourceProfile(cpu_millicores=1000))
    assert fit is CapacityFit.REQUIRED_DIMENSION_UNREADABLE
