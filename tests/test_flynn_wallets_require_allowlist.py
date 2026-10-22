"""A Flynn wallet never deploys without a provider allowlist (TEAMLEAD-flynn, openmix-zgt4r PR-B).

just-akash refuses a FOREIGN bid only when an allowlist is set; with none it takes ANY bid. Flynn's
fleet leases reached third-party providers that way (9 leases, 2026-10-05..08). PR-A set the
allowlist on every Flynn caller (OpenMix #1666, inference-benchmark #123, just-akash #475); this
makes it mandatory: a deploy from a Flynn wallet with no allowlist is refused BEFORE anything is
created, naming AKASH_PROVIDERS and the allowed values. An account that cannot be resolved is
treated as Flynn's (fail closed). Other wallets are unchanged.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from just_akash import deploy as deploy_mod
from just_akash.deploy import _wallet_account as REAL_WALLET_ACCOUNT  # before conftest patches
from just_akash.deploy import deploy
from tests.test_deploy import SDL_YAML, _make_bid, _time_mock

CONSOLE_3 = "akash14n4rkmz64rn0tey0r5g07l8q5x0fh2h4hu44kt"
H4I = "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk"
CONSOLE_2 = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
OTHER = "akash1someoneelsexxxxxxxxxxxxxxxxxxxxxxxxx"


def _run(MockAPI, mock_time, tmp_path, monkeypatch, account, providers=None):
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
    if providers is None:
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
    else:
        monkeypatch.setenv("AKASH_PROVIDERS", providers)
    monkeypatch.setattr(deploy_mod, "_wallet_account", lambda wallet, client: account)
    sdl = tmp_path / "sdl.yaml"
    sdl.write_text(SDL_YAML)
    client = MockAPI.return_value
    client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
    client.get_bids.return_value = [_make_bid(H4I, 50)]
    client.create_lease.return_value = {"data": {"lease": "created"}}
    mock_time.time.side_effect = _time_mock()
    mock_time.sleep.return_value = None
    return client, sdl


@pytest.mark.parametrize("account", [CONSOLE_3, None])
@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_a_flynn_or_unknown_wallet_without_an_allowlist_is_refused_before_create(
    MockAPI, mock_time, tmp_path, monkeypatch, account
):
    client, sdl = _run(MockAPI, mock_time, tmp_path, monkeypatch, account)
    with pytest.raises(RuntimeError) as err:
        deploy(sdl_path=str(sdl), bid_wait=10, bid_wait_retry=10)
    msg = str(err.value)
    assert "AKASH_PROVIDERS" in msg and H4I in msg, msg
    client.create_deployment.assert_not_called()


@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_a_flynn_wallet_with_an_allowlist_deploys(MockAPI, mock_time, tmp_path, monkeypatch):
    client, sdl = _run(MockAPI, mock_time, tmp_path, monkeypatch, CONSOLE_3, providers=H4I)
    result = deploy(sdl_path=str(sdl), bid_wait=10, bid_wait_retry=10)
    assert result["provider"] == H4I


@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_another_wallet_without_an_allowlist_is_unchanged(
    MockAPI, mock_time, tmp_path, monkeypatch
):
    client, sdl = _run(
        MockAPI, mock_time, tmp_path, monkeypatch, "akash1someoneelsexxxxxxxxxxxxxxxxxxxxxxxxx"
    )
    result = deploy(sdl_path=str(sdl), bid_wait=10, bid_wait_retry=10)
    assert result["provider"] == H4I


def test_the_flynn_wallets_and_providers_are_the_known_ones():
    assert {
        "akash1cklqag0e0v794msgqkr6uwf7sr6zyy42gm78f0",  # console-1
        "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee",  # console-2
        CONSOLE_3,  # console-3
    } == deploy_mod.FLYNN_WALLETS
    assert H4I in deploy_mod.FLYNN_OWN_PROVIDERS.values()


# openmix-15 (stand-in review): every test above replaces `_wallet_account`, so a fail-OPEN
# mutant of its except branch passed them all. These run the REAL function: one API key (the pool
# names no account), so the account comes from the mocked client's `account_address()`.


def _real(MockAPI, mock_time, tmp_path, monkeypatch, resolved):
    client, sdl = _run(MockAPI, mock_time, tmp_path, monkeypatch, None)
    monkeypatch.setattr(deploy_mod, "_wallet_account", REAL_WALLET_ACCOUNT)
    monkeypatch.delenv("AKASH_API_KEYS", raising=False)
    if isinstance(resolved, Exception):
        client.account_address.side_effect = resolved
    else:
        client.account_address.return_value = resolved
    return client, sdl


@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_real_an_unresolvable_account_is_refused_before_create_and_says_why(
    MockAPI, mock_time, tmp_path, monkeypatch, caplog
):
    client, sdl = _real(MockAPI, mock_time, tmp_path, monkeypatch, RuntimeError("jwt down"))
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(RuntimeError, match="could not be determined"),
    ):
        deploy(sdl_path=str(sdl), bid_wait=10, bid_wait_retry=10)
    client.create_deployment.assert_not_called()
    assert any(
        r.levelno == logging.WARNING and "could not be resolved" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize("resolved", [CONSOLE_2, "", "not-an-address", None])
@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_real_a_flynn_or_non_address_account_is_refused(
    MockAPI, mock_time, tmp_path, monkeypatch, resolved
):
    client, sdl = _real(MockAPI, mock_time, tmp_path, monkeypatch, resolved)
    with pytest.raises(RuntimeError, match="AKASH_PROVIDERS"):
        deploy(sdl_path=str(sdl), bid_wait=10, bid_wait_retry=10)
    client.create_deployment.assert_not_called()


@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_real_someone_elses_key_still_deploys_without_an_allowlist(
    MockAPI, mock_time, tmp_path, monkeypatch
):
    client, sdl = _real(MockAPI, mock_time, tmp_path, monkeypatch, OTHER)
    result = deploy(sdl_path=str(sdl), bid_wait=10, bid_wait_retry=10)
    assert result["provider"] == H4I
    client.account_address.assert_called()


@pytest.mark.parametrize("pooled", ["", "not-an-address", None, 7])
def test_real_a_pooled_value_that_is_not_an_address_falls_back_to_the_key(pooled):
    """Only an akash1 address counts as the pool's choice; anything else asks the key itself."""
    client = SimpleNamespace(account_address=lambda: CONSOLE_2)
    assert REAL_WALLET_ACCOUNT(SimpleNamespace(account=pooled), client) == CONSOLE_2


def test_real_a_pooled_address_wins():
    client = SimpleNamespace(account_address=lambda: OTHER)
    assert REAL_WALLET_ACCOUNT(SimpleNamespace(account=CONSOLE_2), client) == CONSOLE_2


# CodeRabbit (#477): the quiet-wallet re-check can swap the wallet AFTER the guard. The guard
# must hold for the wallet that actually creates: a swap onto a Flynn wallet with no allowlist
# is refused before the receipt and before create_deployment.


class _Stop(Exception):
    pass


class _QClient:
    def __init__(self, account, created):
        self.account, self.created = account, created

    def account_address(self):
        return self.account

    def create_deployment(self, sdl_content, deposit=5.0):
        self.created.append(self.account)
        raise _Stop(self.account)

    def list_deployments(self, active_only=True):
        return []


def _qsel(client):
    from just_akash.wallet_pool import WalletClientSelection

    return WalletClientSelection(client, client.account, 10**9, 2, 2, "v1+quiet", True)


def _swap(monkeypatch, tmp_path, first, second, providers=None):
    from just_akash import wallet_pool

    monkeypatch.setattr(deploy_mod, "_wallet_account", REAL_WALLET_ACCOUNT)
    monkeypatch.setattr(deploy_mod, "_check_wallet_credit", lambda *a, **k: None)
    monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
    if providers is None:
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
    else:
        monkeypatch.setenv("AKASH_PROVIDERS", providers)
    monkeypatch.setattr(wallet_pool, "select_client_for_create", lambda *a, **k: _qsel(first))
    monkeypatch.setattr(wallet_pool, "confirm_quiet_or_reselect", lambda *a, **k: _qsel(second))
    p = tmp_path / "deploy.yaml"
    p.write_text("version: '2.0'\n")
    return str(p)


def test_a_quiet_recheck_that_swaps_onto_a_flynn_wallet_is_refused(monkeypatch, tmp_path):
    created = []
    sdl = _swap(monkeypatch, tmp_path, _QClient(OTHER, created), _QClient(CONSOLE_2, created))
    with pytest.raises(RuntimeError, match="No provider allowlist"):
        deploy(sdl_path=sdl, bid_wait=2, bid_wait_retry=3)
    assert created == []


def test_a_swap_onto_a_flynn_wallet_with_an_allowlist_creates(monkeypatch, tmp_path):
    created = []
    sdl = _swap(
        monkeypatch,
        tmp_path,
        _QClient(OTHER, created),
        _QClient(CONSOLE_2, created),
        providers=H4I,
    )
    with pytest.raises(_Stop):
        deploy(sdl_path=sdl, bid_wait=2, bid_wait_retry=3)
    assert created == [CONSOLE_2]


def test_a_swap_between_non_flynn_wallets_is_unchanged(monkeypatch, tmp_path):
    created = []
    sdl = _swap(
        monkeypatch,
        tmp_path,
        _QClient(OTHER, created),
        _QClient("akash1anotherxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", created),
    )
    with pytest.raises(_Stop):
        deploy(sdl_path=sdl, bid_wait=2, bid_wait_retry=3)
    assert created == ["akash1anotherxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"]
