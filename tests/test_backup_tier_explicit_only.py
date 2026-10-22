"""The backup tier is EXPLICIT ONLY: AKASH_PROVIDERS_BACKUP in the environment is never consulted.

Flynn's fleet leases landed on third-party providers (TEAMLEAD-flynn, 2026-10-22: 9 leases,
2026-10-05..08) and .env.example shipped 10 third-party providers as the backup tier, which any
deploy that did not pass --backup-provider inherited (cli.py's own comment records the canary
that "inherited 10 backups from the environment, and landed on a provider it had not asked
for"). A backup provider is now only one the caller names in the command.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from just_akash.deploy import deploy
from tests.test_deploy import SDL_YAML, _make_bid, _time_mock


@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_an_inherited_backup_tier_is_ignored_and_its_bids_are_foreign(
    MockAPI, mock_time, tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.setenv("AKASH_PROVIDERS", "akash1pref")
    monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", "akash1thirdparty")
    sdl = tmp_path / "sdl.yaml"
    sdl.write_text(SDL_YAML)
    client = MockAPI.return_value
    client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
    # ONLY the inherited "backup" bids: before, it was taken as BACKUP; now it is FOREIGN.
    client.get_bids.return_value = [_make_bid("akash1thirdparty", 10)]
    client.create_lease.return_value = {"data": {"lease": "created"}}
    mock_time.time.side_effect = _time_mock()
    mock_time.sleep.return_value = None
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError) as err:
        deploy(sdl_path=str(sdl), bid_wait=10, bid_wait_retry=10)
    assert "akash1thirdparty" in str(err.value), "the refused bidder is named"
    client.create_lease.assert_not_called()
    assert any(
        "AKASH_PROVIDERS_BACKUP" in r.getMessage() and "ignored" in r.getMessage()
        for r in caplog.records
    ), "a set-but-ignored backup env is said out loud"


@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_an_explicit_backup_tier_still_works(MockAPI, mock_time, tmp_path, monkeypatch):
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.setenv("AKASH_PROVIDERS", "akash1pref")
    monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
    sdl = tmp_path / "sdl.yaml"
    sdl.write_text(SDL_YAML)
    client = MockAPI.return_value
    client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
    client.get_bids.return_value = [_make_bid("akash1back", 80)]  # no preferred bid
    client.create_lease.return_value = {"data": {"lease": "created"}}
    mock_time.time.side_effect = _time_mock()
    mock_time.sleep.return_value = None
    result = deploy(
        sdl_path=str(sdl), bid_wait=10, bid_wait_retry=10, backup_providers=["akash1back"]
    )
    assert result["provider"] == "akash1back"


def test_env_example_ships_no_backup_providers():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    line = next(ln for ln in text.splitlines() if ln.startswith("AKASH_PROVIDERS_BACKUP="))
    assert line == "AKASH_PROVIDERS_BACKUP=", line
