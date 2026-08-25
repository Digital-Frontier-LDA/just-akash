"""`--select emptiest` must reach the auction, and must cost NOTHING when unused.

⛔ THE HOT PATH IS THE CONTRACT. `capacity_by_provider()` opens one HTTP connection per
bidding provider. On a 16-bidder auction that is 16 round-trips inside a bid loop that
already has a deadline. The default path must not pay it, and "I read the code and it
looked guarded" is not a check — `test_the_default_path_makes_no_capacity_calls`
observes the call itself.

⛔ AND THE FEATURE MUST ACTUALLY FIRE. The previous defect in this area was the exact
opposite failure: EMPTIEST was selectable, reached the core, and ranked on a capacity
nobody supplied — inert, silent, and green. A test that only proves the default is
untouched would pass just as happily against a `--select` flag wired to nothing.
Both directions are asserted here.
"""

from __future__ import annotations

import pytest

from just_akash import deploy as deploy_mod
from just_akash import wallet_pool


class _StopBeforeLease(Exception):
    """Sentinel: the deploy reached provider resolution, i.e. selection is done."""


def _bid(provider: str, amount: str) -> dict:
    return {
        "bid": {
            "id": {"provider": provider},
            "price": {"denom": "uakt", "amount": amount},
            "state": "open",
        }
    }


CHEAP, DEAR = "akash1cheap", "akash1dear"


class _FakeClient:
    def account_address(self):
        return "akash1fake"

    def create_deployment(self, sdl_content, deposit=5.0):
        return {"dseq": "1234567890", "manifest": "{}"}

    def get_bids(self, dseq):
        return [_bid(CHEAP, "1"), _bid(DEAR, "9")]

    def create_lease(self, *a, **k):
        # The first call after the auction resolves (STEP 6). Stopping here keeps the
        # test off the lease/manifest/deploy chain without stubbing all of it — and
        # everything this test asserts has already happened by now.
        raise _StopBeforeLease("create_lease")

    def get_provider(self, address):
        return {"host_uri": "https://provider.example:8443"}

    def close_deployment(self, dseq):
        return {"ok": True}

    def list_deployments(self, active_only=True):
        return []


class _FakeWallet:
    client = _FakeClient()
    name = "FAKE"
    configured_keys = 1
    slot = "AKASH_CONSOLE"


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Drive deploy() to the auction and record every capacity fetch."""
    calls: list[list[str]] = []

    def _record(addresses, **kwargs):
        calls.append(list(addresses))
        return {}

    monkeypatch.setattr(deploy_mod, "capacity_by_provider", _record)
    # ⚠ deploy() imports this INSIDE the function body (deploy.py:764), so it is not an
    #   attribute of the deploy module. Patching there raises AttributeError — which is
    #   how this harness first failed, uniformly, including its own control.
    monkeypatch.setattr(wallet_pool, "select_client_for_create", lambda *a, **k: _FakeWallet())
    # No allowlist: every bid is ACCEPTED, so the auction resolves in the first window.
    monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
    monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)

    sdl = tmp_path / "deploy.yaml"
    sdl.write_text("version: '2.0'\n")

    def run(**kwargs):
        with pytest.raises(_StopBeforeLease):
            deploy_mod.deploy(sdl_path=str(sdl), bid_wait=2, bid_wait_retry=3, **kwargs)
        return calls

    return run


def test_the_default_path_makes_no_capacity_calls(harness):
    """⛔ Not "the default still picks cheapest" — that would pass with the fetch armed
    and merely ignored. This asserts the round-trips are never MADE."""
    assert harness() == []


def test_select_cheapest_explicitly_also_makes_no_capacity_calls(harness):
    assert harness(select="cheapest") == []


def test_select_emptiest_fetches_capacity_for_exactly_the_bidding_providers(harness):
    calls = harness(select="emptiest")
    assert len(calls) == 1, f"expected ONE fetch for the whole auction, got {len(calls)}"
    # Not a subset check: probing a provider that did not bid is wasted latency, and
    # missing one that did is the inert failure this feature already had once.
    assert sorted(calls[0]) == sorted([CHEAP, DEAR])


def test_an_unknown_select_value_raises_before_any_deployment_is_created(monkeypatch, tmp_path):
    """A typo must not silently deliver the default placement policy — and must not
    buy a deployment on the way to saying so.

    ⛔ THIS TEST CHANGED THE CODE. Resolving the mode beside its use put the raise
    AFTER `create_deployment`: a mistyped `--select` created a deployment, paid the
    deposit, and only then failed. Argument validation is free and now runs first.
    """
    created: list[str] = []

    class _Tripwire(_FakeClient):
        def create_deployment(self, sdl_content, deposit=5.0):
            created.append("spent")
            return super().create_deployment(sdl_content, deposit=deposit)

    class _W:
        client = _Tripwire()
        name = "FAKE"
        configured_keys = 1
        slot = "AKASH_CONSOLE"

    monkeypatch.setattr(wallet_pool, "select_client_for_create", lambda *a, **k: _W())
    sdl = tmp_path / "deploy.yaml"
    sdl.write_text("version: '2.0'\n")

    with pytest.raises(ValueError, match="unknown --select"):
        deploy_mod.deploy(sdl_path=str(sdl), bid_wait=2, bid_wait_retry=3, select="emptyest")

    assert created == [], "a mistyped --select must not create (and pay for) a deployment"
