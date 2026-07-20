"""Tests for NO-BID evidence capture in the smoke harness.

Regression context: a healthy, actively-bidding provider (z9nr) read as NO-BID for
32 consecutive smoke runs. A bare "NO-BID" carries no evidence, so nothing
distinguished "the provider declined" from "we never got a usable answer" — the
run threw away the bid table that was already on screen. These tests pin the
evidence capture that makes a NO-BID diagnosable.
"""

import json
from unittest.mock import patch

from just_akash.smoke_providers import _bidders_from_output, _record_no_bid_evidence

TARGET = "akash1z9nr23cgweu45g2jktfx95v7g2xp8qlsa3ys2x"
OTHER1 = "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk"
OTHER2 = "akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z"

DEPLOY_OUT_OTHERS_BID = f"""
[2026-07-20T15:33:07Z]   poll #2 @ 5s: 12 bid(s) received
[2026-07-20T15:33:13Z]     bid[1] provider={OTHER1}  price=3.0 uact  state=open  [FOREIGN]
[2026-07-20T15:33:13Z]     bid[2] provider={OTHER2}  price=4.0 uact  state=open  [FOREIGN]
[2026-07-20T15:35:07Z] NO BID FROM 1 allowlisted provider(s):
"""

DEPLOY_OUT_NOBODY_BID = """
[2026-07-20T15:33:07Z]   Waiting for bids... 0s (poll #1)
[2026-07-20T15:35:07Z] No bids received within 180s.
"""


class TestBiddersFromOutput:
    def test_extracts_and_dedupes_providers(self):
        out = f"provider={OTHER1} price=3\nprovider={OTHER2} price=4\nprovider={OTHER1} again"
        assert _bidders_from_output(out) == [OTHER1, OTHER2]

    def test_empty_when_no_bids(self):
        assert _bidders_from_output(DEPLOY_OUT_NOBODY_BID) == []

    def test_order_preserving(self):
        out = f"provider={OTHER2}\nprovider={OTHER1}"
        assert _bidders_from_output(out) == [OTHER2, OTHER1]


class TestRecordNoBidEvidence:
    """The core regression: a healthy provider that declines while others bid must
    emit PROVIDER_NO_BID with the market context attached."""

    def _emit_events(self, capsys):
        err = capsys.readouterr().err
        return [
            json.loads(line)
            for line in err.splitlines()
            if line.strip().startswith("{") and "akash-diag" in line
        ]

    def test_healthy_provider_declining_emits_provider_no_bid(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        healthy = {
            "isOnline": True,
            "isValidVersion": True,
            "stats": {"cpu": {"available": 97215}, "memory": {"available": 440817090560}},
        }
        with patch("just_akash.smoke_providers._api") as mock_api:
            mock_api.return_value.get_provider.return_value = healthy
            _record_no_bid_evidence(TARGET, DEPLOY_OUT_OTHERS_BID)

        events = self._emit_events(capsys)
        assert len(events) == 1
        ev = events[0]
        assert ev["code"] == "PROVIDER_NO_BID"
        assert ev["level"] == "warning"
        ctx = ev["context"]
        assert ctx["provider"] == TARGET
        assert ctx["isOnline"] is True
        assert ctx["other_bidders"] == 2  # the market context that was being discarded
        assert ctx["market_had_bids"] is True
        assert ctx["cpu_available"] == 97215

    def test_offline_provider_emits_provider_offline(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        with patch("just_akash.smoke_providers._api") as mock_api:
            mock_api.return_value.get_provider.return_value = {"isOnline": False}
            _record_no_bid_evidence(TARGET, DEPLOY_OUT_OTHERS_BID)
        assert self._emit_events(capsys)[0]["code"] == "PROVIDER_OFFLINE"

    def test_invalid_version_emits_its_own_code(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        with patch("just_akash.smoke_providers._api") as mock_api:
            mock_api.return_value.get_provider.return_value = {
                "isOnline": True,
                "isValidVersion": False,
            }
            _record_no_bid_evidence(TARGET, DEPLOY_OUT_OTHERS_BID)
        assert self._emit_events(capsys)[0]["code"] == "PROVIDER_INVALID_VERSION"

    def test_unknown_provider_emits_provider_unknown(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        with patch("just_akash.smoke_providers._api") as mock_api:
            mock_api.return_value.get_provider.return_value = None
            _record_no_bid_evidence(TARGET, DEPLOY_OUT_OTHERS_BID)
        assert self._emit_events(capsys)[0]["code"] == "PROVIDER_UNKNOWN"

    def test_market_wide_no_bid_is_distinguished(self, monkeypatch, capsys):
        """Nobody bid → market-wide, NOT provider-specific. The human line must say so."""
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        with patch("just_akash.smoke_providers._api") as mock_api:
            mock_api.return_value.get_provider.return_value = {
                "isOnline": True,
                "isValidVersion": True,
            }
            _record_no_bid_evidence(TARGET, DEPLOY_OUT_NOBODY_BID)
        out = capsys.readouterr().out
        assert "NOBODY bid" in out
        assert "market-wide" in out

    def test_never_raises_when_provider_query_fails(self, monkeypatch, capsys):
        """Diagnostics must never break the smoke run."""
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        with patch("just_akash.smoke_providers._api", side_effect=RuntimeError("registry down")):
            _record_no_bid_evidence(TARGET, DEPLOY_OUT_OTHERS_BID)  # must not raise

    def test_human_line_reports_other_bidders(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "off")  # human line is independent of JSON
        with patch("just_akash.smoke_providers._api") as mock_api:
            mock_api.return_value.get_provider.return_value = {
                "isOnline": True,
                "isValidVersion": True,
            }
            _record_no_bid_evidence(TARGET, DEPLOY_OUT_OTHERS_BID)
        out = capsys.readouterr().out
        assert "2 other provider(s) bid" in out
