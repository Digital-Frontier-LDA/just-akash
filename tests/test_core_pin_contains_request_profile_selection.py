"""The pinned akash-lease-core actually contains what just-akash#346 relies on.

A version string or an ancestry claim is not evidence: release histories in this org have
diverged from main before. These assert the BEHAVIOUR of akash-lease-core#47 — including
the two defects its final review found — so a pin that resolves to a core without them
goes red here, not in production placement.
"""

from __future__ import annotations

import pytest
from akash_lease_core import (
    Auction,
    AuctionPolicy,
    BidObservation,
    BidRejectionReason,
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
        price=price,
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
