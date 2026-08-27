"""Anti-affinity shipped in the core and nothing ever passed it.

⛔ `Auction.evaluate(already_selected=...)` has existed in akash-lease-core and just-akash
called `evaluate(now=...)` only — so an N-region placement stacked on whichever provider was
cheapest, N times over. That is the failure the operator actually feels: a deployment needing
three regions cannot place because all three orders chase one provider.

⚠ THE SPREAD IS SOFT BY DESIGN, and that is the right trade here. The core's own words: it
"changes the ORDER, never the eligibility" — if an already-used provider is the only bidder it
is still taken, "taking it beats failing to place". Measured 2026-08-27, our readable providers
sat at ~93% FULL, so a HARD exclusion would convert spread into placement failure.

⛔⛔ AND ANTI-AFFINITY IS **NOT INDEPENDENT OF EMPTIEST** — measured, against the assumption.
The 2026-08-25 handoff recorded that `evaluate(already_selected=...)` "needs NO capacity data"
and was therefore "the half that works today". In akash-lease-core v0.9.0 that is FALSE: the
`taken` term lives INSIDE `if emptiest and readable:`; the else-branch is a plain
`min(pool, key=price)` with no spread term at all. So `already_selected` passed under CHEAPEST
is silently inert — which is exactly how this test first failed, three rounds all choosing
Lisbon with `reason=cheapest_preferred`.

⇒ EMPTIEST IS A PREREQUISITE FOR ANTI-AFFINITY, not a parallel lever. Turning on `--select
emptiest` is what makes the spread reachable at all.
"""

from __future__ import annotations

from pathlib import Path

from akash_lease_core import from_provider_status
from akash_lease_core.auction import PreferredSelection

from just_akash.deploy import _select_auction_bid

REPO_ROOT = Path(__file__).resolve().parents[1]

LIS, SOF, HEL = "akash1lisbon", "akash1sofia", "akash1helsinki"


def _bid(provider: str, amount: str) -> dict:
    return {
        "bid": {
            "id": {"provider": provider},
            "price": {"denom": "uakt", "amount": amount},
            "state": "open",
        }
    }


# Lisbon is strictly cheapest, so an unaided auction picks it every single round.
FLEET = [_bid(LIS, "1"), _bid(SOF, "5"), _bid(HEL, "9")]
PREFERRED = [LIS, SOF, HEL]


def _status(free: int, total: int) -> dict:
    node = {
        "allocatable": {"cpu": total, "memory": total, "storage_ephemeral": total, "gpu": 0},
        "available": {"cpu": free, "memory": free, "storage_ephemeral": free, "gpu": 0},
    }
    return {"cluster": {"inventory": {"available": {"nodes": [node]}}}}


# Equal headroom on purpose: with capacity tied, ONLY the anti-affinity term can separate
# these three. A test where the emptiest provider differs each round would pass whether or
# not `already_selected` was wired.
CAPACITY = {
    LIS: from_provider_status(_status(50, 100)),
    SOF: from_provider_status(_status(50, 100)),
    HEL: from_provider_status(_status(50, 100)),
}


def _run(**kw):
    return _select_auction_bid(
        FLEET, preferred=PREFERRED, backup=[], collection_window_seconds=10, **kw
    )


def _run_emptiest(**kw):
    """Anti-affinity only engages under EMPTIEST with readable capacity — see the header."""
    return _run(
        capacity_by_provider=CAPACITY, preferred_selection=PreferredSelection.EMPTIEST, **kw
    )


def _place_three(spread: bool) -> list[str]:
    """Three sequential placements off ONE bid snapshot — the multi-region shape."""
    chosen: list[str] = []
    for _ in range(3):
        kw = {"already_selected": frozenset(chosen)} if spread else {}
        _raw, result = _run_emptiest(**kw)
        assert result.selected is not None
        chosen.append(result.selected.provider)
    return chosen


def test_without_anti_affinity_three_placements_stack_on_one_provider() -> None:
    """⭐ THE CONTROL, and the bug. With headroom tied, every round takes the same provider."""
    assert len(set(_place_three(spread=False))) == 1


def test_anti_affinity_spreads_three_placements_across_three_providers() -> None:
    """The deliverable: N placements land on N DISTINCT providers."""
    chosen = _place_three(spread=True)
    assert len(set(chosen)) == 3, f"expected 3 distinct providers, got {chosen}"
    assert set(chosen) == {LIS, SOF, HEL}


def test_the_default_is_unchanged_when_nothing_is_passed() -> None:
    """⚠ A caller that does not track placements must be entirely unaffected."""
    _raw, result = _run()
    assert result.selected is not None
    assert result.selected.provider == LIS


def test_empty_and_none_mean_the_same_thing() -> None:
    """`frozenset()` is 'no spread requested', not 'spread against nothing'."""
    _raw, a = _run(already_selected=frozenset())
    _raw2, b = _run(already_selected=None)
    assert a.selected is not None and b.selected is not None
    assert a.selected.provider == b.selected.provider == LIS


def test_a_sole_bidder_is_still_taken_even_when_already_used() -> None:
    """⛔ THE SAFETY PROPERTY. Soft, not hard — placing beats failing to place.

    If this ever inverts, a 3-region deployment against a 1-provider market stops placing
    at all, which is strictly worse than landing twice on the same provider.
    """
    solo = [_bid(LIS, "1")]
    # ⚠ Under EMPTIEST with readable capacity — the ONLY branch where the anti-affinity term
    # runs. Asserting this on the cheapest path would pass whether or not the property held,
    # because that branch never consults `already_selected` at all.
    _raw, result = _select_auction_bid(
        solo,
        preferred=[LIS],
        backup=[],
        collection_window_seconds=10,
        capacity_by_provider={LIS: from_provider_status(_status(50, 100))},
        preferred_selection=PreferredSelection.EMPTIEST,
        already_selected=frozenset({LIS}),
    )
    assert result.selected is not None, "a sole already-used bidder must still be selectable"
    assert result.selected.provider == LIS


# ─────────────────────────────────────────────────────────────────────────────
# `--already-selected` must be REFUSED where it cannot work.
#
# ⛔ alc applies the anti-affinity penalty inside `if emptiest and readable:`. Under the
# default `cheapest` policy the addresses are parsed, threaded through `deploy()`, handed
# to the auction — and change nothing. A flag that is accepted and silently dropped is
# worse than one that is rejected: the caller believes the spread was requested.
# Raised by CodeRabbit on #216.
class TestAlreadySelectedRequiresEmptiest:
    def _run(self, argv):
        import subprocess
        import sys

        return subprocess.run(
            [sys.executable, "-m", "just_akash.cli", *argv],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )

    def test_rejected_under_the_default_policy(self):
        r = self._run(["deploy", "--sdl", "nope.yaml", "--already-selected", "akash1aaa"])
        assert r.returncode == 2, f"expected refusal, got {r.returncode}: {r.stderr[:300]}"
        assert "--already-selected needs --select emptiest" in r.stderr

    def test_rejected_under_an_explicit_cheapest(self):
        r = self._run(
            [
                "deploy",
                "--sdl",
                "nope.yaml",
                "--select",
                "cheapest",
                "--already-selected",
                "akash1aaa",
            ]
        )
        assert r.returncode == 2, r.stderr[:300]
        assert "silently ignored" in r.stderr

    def test_the_guard_does_not_fire_without_the_flag(self):
        """BOTH DIRECTIONS. A guard that refuses everything would pass the two tests
        above while breaking every ordinary deploy — the refusal has to be specific to
        the combination, not to the command."""

        r = self._run(["deploy", "--sdl", "nope.yaml", "--select", "cheapest"])
        assert "--already-selected needs" not in r.stderr, (
            "the guard fired on a command that never passed the flag"
        )

    def test_accepted_under_emptiest(self):
        """The combination the flag exists for must survive argument validation."""

        r = self._run(
            [
                "deploy",
                "--sdl",
                "nope.yaml",
                "--select",
                "emptiest",
                "--already-selected",
                "akash1aaa",
            ]
        )
        assert "--already-selected needs" not in r.stderr, (
            "the guard rejected the one policy under which the flag works"
        )
