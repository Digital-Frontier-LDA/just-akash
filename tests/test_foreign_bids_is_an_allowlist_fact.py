"""`BIDS_FOREIGN_ONLY` must blame the allow-list, never the providers' health.

⚠ WHY. The old message ended "Check that your providers are online and have capacity."
**A bid is proof of both.** A provider that bids has seen the order, is online, and has
declared it can serve that shape — so the one thing this failure can never mean is that
the providers are down or full. It means our allow-list rejected what arrived.

Measured in Blazing-Back#1274 across 42 consecutive rejection rounds: a DFC-owned
`tier: preferred` provider had bid in **42 of 42**. The providers were online, had
capacity, and bid every round; the advice was misleading in 100% of observed uses and
sent investigations to look at provider health. The eventual fix (Blazing-Back#1350)
was to the ALLOW-LIST.

These are behavioural — they drive `deploy()` to the real raise site, not a source scan.
"""

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from just_akash.deploy import deploy

SDL_YAML = """
version: "2.0"
services:
  web:
    image: python:3.13-slim
    expose:
      - port: 22
        as: 22
        to:
          - global: true
"""

# The exact sentence that must never come back. Kept as a constant so a reader who
# deletes it here also has to look at why.
BANNED = "online and have capacity"


def _make_bid(provider, amount=10):
    return {
        "id": {"provider": provider},
        "price": {"amount": amount, "denom": "uakt"},
        "state": "open",
    }


def _time_mock():
    counter = [0.0]

    def advance():
        counter[0] += 1
        return counter[0]

    return advance


def _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers):
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.setenv("AKASH_PROVIDERS", providers)
    monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
    sdl_file = tmp_path / "sdl.yaml"
    sdl_file.write_text(SDL_YAML)
    client = MockAPI.return_value
    client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
    client.create_lease.return_value = {"lease": "ok"}
    mock_time.time.side_effect = _time_mock()
    mock_time.sleep.return_value = None
    return client, str(sdl_file)


@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_the_message_blames_the_allowlist_and_names_who_bid(
    MockAPI, mock_time, tmp_path, monkeypatch
):
    client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1pref")
    client.get_bids.return_value = [_make_bid("akash1foreign"), _make_bid("akash1other")]

    with pytest.raises(RuntimeError) as err:
        deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
    msg = str(err.value)

    assert "mismatch is between the" in msg, f"the cause must be named, not implied:\n{msg}"
    # The reader needs BOTH sides of the mismatch to act on it.
    assert "akash1foreign" in msg, f"who bid must be shown:\n{msg}"
    assert "akash1pref" in msg, f"what was allowed must be shown:\n{msg}"


@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_it_never_tells_the_reader_to_check_provider_health(
    MockAPI, mock_time, tmp_path, monkeypatch
):
    """⛔ The load-bearing regression guard — this is the sentence that cost the time."""
    client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1pref")
    client.get_bids.return_value = [_make_bid("akash1foreign")]

    with pytest.raises(RuntimeError) as err:
        deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
    msg = str(err.value)

    assert BANNED not in msg, (
        "a bid PROVES the provider is online and has capacity; advising a health check "
        f"here was misleading in 42 of 42 measured rounds (Blazing-Back#1274):\n{msg}"
    )
    assert "NOT a capacity or liveness problem" in msg, (
        "state it positively too — deleting the bad advice without replacing it leaves "
        "the next reader to guess, and provider health is the guess they already make"
    )


@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_control_an_allowed_bidder_still_succeeds(MockAPI, mock_time, tmp_path, monkeypatch):
    """⛔ Known-positive. Without this the two tests above would pass over a `deploy()`
    that raises unconditionally — i.e. over a completely broken selector."""
    client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1pref")
    client.get_bids.return_value = [_make_bid("akash1foreign"), _make_bid("akash1pref", 50)]

    result = deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
    assert result["provider"] == "akash1pref"


# ---------------------------------------------------------------------------
# ⛔ THE CONTRACT THAT NEARLY BROKE. This message is not only prose — it is a
# MACHINE INTERFACE. `smoke_providers._classify` matches on it to decide whether an
# outcome is `no-bid` (a market/allow-list condition, provider not at fault) or
# `deploy-failed` (a PROVIDER FAIL).
#
# The first version of this fix rewrote the headline to "N bid(s) arrived and OUR
# ALLOW-LIST rejected every one" — which deleted the phrase the classifier matches.
# Every allow-list rejection would then have scored as a provider failure: the exact
# mis-attribution this whole change exists to stop, re-created one layer down, by the
# change meant to fix it. Nothing in the deploy-side tests could see it.
# ---------------------------------------------------------------------------

NO_BID_RE = re.compile(r"no bids?\b|none from our providers|foreign bids", re.IGNORECASE)
POSITIVE_EVIDENCE_RE = re.compile(
    r"none from our providers|foreign bids|no bid from", re.IGNORECASE
)


@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_the_message_is_still_classified_as_NO_BID_not_provider_fail(
    MockAPI, mock_time, tmp_path, monkeypatch
):
    """The literal `smoke_providers` regexes, applied to the real raised message."""
    client, sdl = _setup(MockAPI, mock_time, tmp_path, monkeypatch, providers="akash1pref")
    client.get_bids.return_value = [_make_bid("akash1foreign")]
    with pytest.raises(RuntimeError) as err:
        deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
    msg = str(err.value)

    assert NO_BID_RE.search(msg), (
        "smoke_providers.py:943 would fall through to 'deploy-failed' and score OUR "
        f"allow-list rejection as a PROVIDER FAIL:\n{msg}"
    )
    assert POSITIVE_EVIDENCE_RE.search(msg), (
        "smoke_providers.py:950 uses this second match to skip the on-chain cross-check, "
        "because these variants carry POSITIVE evidence that order flow worked. Losing it "
        f"turns a definite no-bid into 'no-bid-unverified':\n{msg}"
    )


def test_the_classifier_regexes_here_still_match_smoke_providers():
    """⛔ A copied regex rots. Pin these against the source they were copied from, so this
    file cannot keep asserting a contract the other side has already changed."""
    src = (Path(__file__).resolve().parents[1] / "just_akash" / "smoke_providers.py").read_text()
    assert NO_BID_RE.pattern in src, "no-bid regex drifted from smoke_providers.py"
    assert POSITIVE_EVIDENCE_RE.pattern in src, "positive-evidence regex drifted"
