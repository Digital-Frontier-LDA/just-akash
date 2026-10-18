"""AC4 (just-akash-3sq): a chartered lease is funded for its declared lifetime, not "5 USD".

deposit = max_hours x accepted bid (uact/block) x 600 blocks/h x 1.15, capped at max_act,
floored at the Console minimum 0.5. The order is created at that minimum; the rest is added
by deposit_deployment once the bid -- and so the price -- is known. A follow-up deposit
that fails CLOSES the lease (a chartered lease running on 0.5 ACT dies early and reads as a
provider fault). Auto top-up is switched OFF -- that is what makes max_hours a real expiry
-- unless the charter is prod_pinned, and the switch is read back, not trusted.

All chain calls go to the localhost fake Console (AC5).
"""

from __future__ import annotations

import pytest

from just_akash import charter as charter_mod
from just_akash import deploy as deploy_mod
from tests import _charter_harness as harness

CAPACITY = {
    "kind": "capacity",
    "served_model_name": "qwen3-32b",
    "image_digest": "sha256:" + "a" * 64,
    "canary_prompt": "say ok",
    "canary_expect": "ok",
}


@pytest.fixture
def h(monkeypatch, tmp_path):
    hh = harness.start(monkeypatch, tmp_path)
    monkeypatch.setattr(charter_mod, "BIND_RETRY_DELAY_S", 0)
    yield hh
    harness.stop(hh)


def _deploy(h, **charter_overrides):
    charter = charter_mod.load_charter(h.write_charter(**charter_overrides))
    return deploy_mod.deploy(
        sdl_path=h.write_sdl("fleet-serve-qwen"), bid_wait=1, bid_wait_retry=1, charter=charter
    )


@pytest.mark.parametrize(
    "max_hours,price,max_act,expected",
    [
        (10, 1000, 50, 6.9),  # 10 h x 1000 uact/block x 600 x 1.15 = 6.9 ACT
        (24, 1000, 50, 16.56),
        (100, 1000, 50, 50.0),  # capped at the charter budget
        (2, 10, 50, 0.5),  # 0.0138 ACT -> the Console minimum
    ],
)
def test_deposit_for(max_hours, price, max_act, expected):
    assert charter_mod.deposit_for(max_hours, price, max_act) == pytest.approx(expected)


def test_deposit_for_refuses_a_non_finite_price():
    with pytest.raises(ValueError):
        charter_mod.deposit_for(10, float("inf"), 50)


def test_the_order_is_created_at_the_minimum_and_topped_up_after_the_bid(h):
    h.set_bid_price(1000)
    result = _deploy(h)
    assert h.app.created_deposits == [0.5]
    assert h.app.deposits == [(result["dseq"], pytest.approx(6.4))]
    assert result["deposit"] == pytest.approx(6.9)
    names = h.names()
    assert names.index("create_lease") < names.index("deposit_deployment")


def test_the_credit_probe_is_sized_for_the_whole_budget(h):
    """The probe guards the WALLET, and the charter may spend up to max_act from it."""
    h.set_bid_price(1000)
    _deploy(h, max_act=12)
    assert ("credit_probe", 12.0) in h.events


def test_no_top_up_when_the_minimum_already_covers_it(h):
    h.set_bid_price(10)
    _deploy(h, max_hours=2)
    assert h.app.created_deposits == [0.5]
    assert h.app.deposits == []


def test_a_failed_follow_up_deposit_closes_the_lease(h, capsys):
    h.set_bid_price(1000)
    h.app.deposit_error = (500, "insufficient funds")
    with pytest.raises(RuntimeError, match="deposit"):
        _deploy(h)
    assert h.app.closed == ["1000"]
    assert "CHARTER_DEPOSIT_FAILED" in capsys.readouterr().err
    assert h.flynn.binds == [], "a lease that was closed must not be bound"


def test_auto_top_up_is_switched_off_and_read_back(h):
    h.set_bid_price(1000)
    result = _deploy(h)
    assert ("set_auto_top_up", result["dseq"], False) in h.events
    assert h.app.settings[result["dseq"]]["autoTopUpEnabled"] is False
    names = h.names()
    last_write = max(i for i, n in enumerate(names) if n == "set_auto_top_up")
    assert "get_deployment_settings" in names[last_write:], "the switch must be read back"


def test_a_prod_pinned_charter_switches_auto_top_up_on_and_reads_it_back(h):
    """Review (fallback, decabfe): a NEW deployment has no settings (the Console 404s them,
    which reads as off), so leaving a prod-pinned lease alone let it expire at max_hours
    like a job lease. It is switched ON explicitly, and the read-back confirms it."""
    h.set_bid_price(1000)
    assert h.app.settings == {}, "a fresh deployment starts with no settings"
    _deploy(h, prod_pinned=True, **CAPACITY)
    assert ("set_auto_top_up", "1000", True) in h.events
    assert h.app.settings["1000"]["autoTopUpEnabled"] is True


def test_a_top_up_switch_that_does_not_take_closes_the_lease(h, capsys):
    """An uncapped lease is exactly what the charter exists to prevent."""
    h.set_bid_price(1000)
    h.app.settings_ignore_writes = True
    with pytest.raises(RuntimeError, match="auto top-up"):
        _deploy(h)
    assert h.app.closed == ["1000"]
    assert "CHARTER_DEPOSIT_FAILED" in capsys.readouterr().err


def test_a_bid_not_priced_in_uact_is_refused_before_the_lease(h):
    """The formula is in uact/block. A bid in another denom cannot be sized, and guessing
    the conversion is how a budget silently becomes 10^6 x wrong."""
    h.set_bid_price(
        1000, denom="ibc/170C677610AC31DF0904FFE09CD3B5C657492170E7E52372E48756B71E56F2F1"
    )
    with pytest.raises(RuntimeError, match="uact"):
        _deploy(h)
    assert "create_lease" not in h.names()
    assert h.app.closed == ["1000"]


def test_a_retry_bid_not_priced_in_uact_is_refused_before_its_lease(h):
    """Review F2: the stale-bid retry can switch to another bid. Its denom is checked
    before THAT lease too, so the deploy refuses instead of leasing and then closing."""
    h.set_bid_price(1000)
    other = {
        "bid": {"id": {"provider": "akash1otherprovider"}},
        "id": {"provider": "akash1otherprovider"},
        "price": {"amount": 5000, "denom": "ibc/170C677610AC31DF"},
        "state": "open",
    }
    # The other bid arrives only after the first lease attempt: bids in two denoms on one
    # order cannot be ranked at selection, but a re-fetch after a stale bid can return one.
    first_lease = h.app.create_lease

    def lease_then_rebid(body):
        h.app.bids[:] = [other]
        return first_lease(body)

    h.app.create_lease = lease_then_rebid
    h.app.lease_errors = [(400, "bid no longer open")]
    with pytest.raises(RuntimeError, match="uact"):
        _deploy(h)
    assert "create_lease" not in h.names()
    assert h.names().count("create_lease_failed") == 1
    assert h.app.closed == ["1000"]
