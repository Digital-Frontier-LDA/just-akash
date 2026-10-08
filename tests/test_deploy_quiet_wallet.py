"""openmix-wxs8 at the deploy() seam: the flag reaches wallet selection, the pre-create re-check
can swap the wallet BEFORE create_deployment, and a refusal creates nothing."""

from __future__ import annotations

import pytest

from just_akash import deploy as deploy_mod
from just_akash import wallet_pool
from just_akash.wallet_pool import WalletClientSelection


class _Stop(Exception):
    pass


class _Client:
    def __init__(self, name, created):
        self.name, self.created = name, created

    def account_address(self):
        return f"akash1{self.name}"

    def create_deployment(self, sdl_content, deposit=5.0):
        self.created.append(self.name)
        raise _Stop(self.name)

    def list_deployments(self, active_only=True):
        return []


def _sel(client, aware=True):
    return WalletClientSelection(client, client.account_address(), 10**9, 2, 2, "v1+quiet", aware)


@pytest.fixture
def sdl(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_mod, "_check_wallet_credit", lambda *a, **k: None)
    p = tmp_path / "deploy.yaml"
    p.write_text("version: '2.0'\n")
    return str(p)


def test_the_cli_flag_reaches_wallet_selection(monkeypatch, sdl):
    seen, created = {}, []

    def select(*a, **k):
        seen.update(k)
        return _sel(_Client("a", created), aware=False)

    monkeypatch.setattr(wallet_pool, "select_client_for_create", select)
    with pytest.raises(_Stop):
        deploy_mod.deploy(sdl_path=sdl, bid_wait=2, bid_wait_retry=3, quiet_wallet=True)
    assert seen["quiet"] is True and created == ["a"]


def test_no_flag_defers_to_the_env_and_skips_the_recheck(monkeypatch, sdl):
    """Default path: quiet=None reaches selection (env decides; unset = funding-only), and a
    selection that is not quiet-aware is never re-checked."""
    seen, created = {}, []

    def select(*a, **k):
        seen.update(k)
        return _sel(_Client("a", created), aware=False)

    def recheck(*a, **k):
        raise AssertionError("a funding-only selection must not be re-checked")

    monkeypatch.setattr(wallet_pool, "select_client_for_create", select)
    monkeypatch.setattr(wallet_pool, "confirm_quiet_or_reselect", recheck)
    with pytest.raises(_Stop):
        deploy_mod.deploy(sdl_path=sdl, bid_wait=2, bid_wait_retry=3)
    assert seen["quiet"] is None and created == ["a"]


def test_a_wallet_that_turned_busy_is_swapped_before_create(monkeypatch, sdl):
    created = []
    first, second = _Client("first", created), _Client("second", created)
    monkeypatch.setattr(wallet_pool, "select_client_for_create", lambda *a, **k: _sel(first))
    monkeypatch.setattr(wallet_pool, "confirm_quiet_or_reselect", lambda *a, **k: _sel(second))
    with pytest.raises(_Stop):
        deploy_mod.deploy(sdl_path=sdl, bid_wait=2, bid_wait_retry=3)
    assert created == ["second"]


def test_a_recheck_refusal_creates_nothing(monkeypatch, sdl):
    created = []

    def refuse(*a, **k):
        raise RuntimeError("no funded AND quiet Console wallet")

    monkeypatch.setattr(
        wallet_pool, "select_client_for_create", lambda *a, **k: _sel(_Client("a", created))
    )
    monkeypatch.setattr(wallet_pool, "confirm_quiet_or_reselect", refuse)
    with pytest.raises(RuntimeError, match="no funded AND quiet"):
        deploy_mod.deploy(sdl_path=sdl, bid_wait=2, bid_wait_retry=3)
    assert created == []


def test_the_recheck_runs_after_the_credit_probe_and_a_swap_reprobes(monkeypatch, sdl):
    """The LCD credit probe is a network call: it runs BEFORE the re-check (outside the window),
    and again for the new wallet when the re-check swaps it."""
    events, created = [], []
    first, second = _Client("first", created), _Client("second", created)
    monkeypatch.setattr(
        deploy_mod,
        "_check_wallet_credit",
        lambda client, deposit: events.append(f"credit:{client.name}"),
    )
    monkeypatch.setattr(wallet_pool, "select_client_for_create", lambda *a, **k: _sel(first))

    def recheck(*a, **k):
        events.append("recheck")
        return _sel(second)

    monkeypatch.setattr(wallet_pool, "confirm_quiet_or_reselect", recheck)
    with pytest.raises(_Stop):
        deploy_mod.deploy(sdl_path=sdl, bid_wait=2, bid_wait_retry=3)
    assert events == ["credit:first", "recheck", "credit:second"]
    assert created == ["second"]
