"""A Flynn wallet never deploys without a provider allowlist (TEAMLEAD-flynn, openmix-zgt4r PR-B).

just-akash refuses a FOREIGN bid only when an allowlist is set; with none it takes ANY bid. Flynn's
fleet leases reached third-party providers that way (9 leases, 2026-10-05..08). PR-A set the
allowlist on every Flynn caller (OpenMix #1666, inference-benchmark #123, just-akash #475); this
makes it mandatory: a deploy from a Flynn wallet with no allowlist is refused BEFORE anything is
created, naming AKASH_PROVIDERS and the allowed values. An account that cannot be resolved is
treated as Flynn's (fail closed). Other wallets are unchanged.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from just_akash import deploy as deploy_mod
from just_akash.deploy import deploy
from tests.test_deploy import SDL_YAML, _make_bid, _time_mock

CONSOLE_3 = "akash14n4rkmz64rn0tey0r5g07l8q5x0fh2h4hu44kt"
H4I = "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk"


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
