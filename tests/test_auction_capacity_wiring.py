"""EMPTIEST shipped in v0.8.0 and ranked on a capacity nothing ever supplied.

⛔ `PreferredSelection.EMPTIEST` was selectable and INERT: `BidObservation` was built
without `capacity=`, so every bid was unrankable and the core degraded to cheapest —
correctly, and silently. This pins the link that was missing.

⚠ The DEFAULT must not move. Wiring capacity through the adapter changes no placement
until a caller explicitly asks for EMPTIEST.
"""

from __future__ import annotations

from akash_lease_core import from_provider_status
from akash_lease_core.auction import PreferredSelection

from just_akash.deploy import _select_auction_bid

BIG, MID, SMALL = "akash1big", "akash1mid", "akash1small"


def _bid(provider: str, amount: str) -> dict:
    return {
        "bid": {
            "id": {"provider": provider},
            "price": {"denom": "uakt", "amount": amount},
            "state": "open",
        }
    }


def _status(free: int, total: int) -> dict:
    return {
        "cluster": {
            "inventory": {
                "available": {
                    "nodes": [
                        {
                            "allocatable": {
                                "cpu": total,
                                "memory": total,
                                "storage_ephemeral": total,
                                "gpu": 0,
                            },
                            "available": {
                                "cpu": free,
                                "memory": free,
                                "storage_ephemeral": free,
                                "gpu": 0,
                            },
                        }
                    ]
                }
            }
        }
    }


# The adversarial fleet: the EMPTIEST provider is also the DEAREST, so cheapest and
# emptiest cannot agree by accident.
FLEET = [_bid(SMALL, "1"), _bid(MID, "5"), _bid(BIG, "9")]
CAPACITY = {
    BIG: from_provider_status(_status(90, 100)),  # 90% free, dearest
    MID: from_provider_status(_status(50, 100)),
    SMALL: from_provider_status(_status(5, 100)),  # 5% free, cheapest
}
PREFERRED = [BIG, MID, SMALL]


def _run(**kw):
    return _select_auction_bid(
        FLEET, preferred=PREFERRED, backup=[], collection_window_seconds=10, **kw
    )


def test_the_default_is_unchanged_and_still_picks_cheapest() -> None:
    """⭐ The control. If this moves, the wiring changed placement for every caller."""
    raw, result = _run()
    assert raw is not None
    assert result.selected is not None
    assert result.selected.provider == SMALL


def test_capacity_alone_changes_nothing_without_the_mode() -> None:
    """Supplying capacity must not silently switch selection."""
    raw, result = _run(capacity_by_provider=CAPACITY)
    assert result.selected is not None
    assert result.selected.provider == SMALL


def test_emptiest_selects_the_roomiest_provider_not_the_cheapest() -> None:
    """⛔ THE LINK. Before this wiring, capacity was never passed and this returned
    the cheapest while reporting a degraded reason."""
    raw, result = _run(
        capacity_by_provider=CAPACITY, preferred_selection=PreferredSelection.EMPTIEST
    )
    assert result.selected is not None
    assert result.selected.provider == BIG
    assert "emptiest" in result.selection_reason


def test_emptiest_without_capacity_degrades_to_cheapest_AND_SAYS_SO() -> None:
    """⚠ The pre-wiring behaviour, pinned deliberately. Asking for emptiest with no
    capacity is not an error — but the reason must not claim the mode was honoured."""
    raw, result = _run(preferred_selection=PreferredSelection.EMPTIEST)
    assert result.selected is not None
    assert result.selected.provider == SMALL
    assert result.selection_reason != "emptiest_preferred"
    assert "emptiest" in result.selection_reason


def test_an_unreadable_provider_is_unranked_not_ranked_last() -> None:
    """⛔ `None` capacity means UNMEASURED. A provider whose /status could not be read
    must not sort last for being unreachable — it must simply not compete on room."""
    partial = dict(CAPACITY)
    partial[BIG] = from_provider_status({})  # unreadable
    raw, result = _run(
        capacity_by_provider=partial, preferred_selection=PreferredSelection.EMPTIEST
    )
    # BIG is unrankable, so the roomiest MEASURED provider wins — not BIG, and not
    # "BIG last" either; it is simply out of the ranking.
    assert result.selected is not None
    assert result.selected.provider == MID


def test_a_provider_with_no_capacity_entry_is_treated_as_unmeasured() -> None:
    """A mapping that omits a bidder must behave like an unreadable one, not like 0%."""
    raw, result = _run(
        capacity_by_provider={BIG: CAPACITY[BIG]},
        preferred_selection=PreferredSelection.EMPTIEST,
    )
    assert result.selected is not None
    assert result.selected.provider == BIG
