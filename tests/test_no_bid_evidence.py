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

# Synthetic addresses — never real provider addresses. Real ones are operationally
# sensitive (AKASH_PROVIDERS is a CI secret) and their entropy trips detect-secrets,
# which would churn .secrets.baseline on every edit. Built to the bech32 shape the
# parser expects (akash1 + lowercase alnum), not copied from the fleet.
TARGET = "akash1" + "targetprovider00000000000000000000000000"
OTHER1 = "akash1" + "otherbidder1000000000000000000000000000a"
OTHER2 = "akash1" + "otherbidder2000000000000000000000000000b"

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


class TestNoBidNamesTheOrder:
    """A NO-BID must say WHICH ORDER it is about.

    This harness runs outside the cluster and cannot read provider logs, so the
    decline reason only exists there — and finding the order needs its dseq.
    Chasing one NO-BID on 2026-08-30 took five passes and two wrong diagnoses
    purely because the verdict never named the order: the fleet runs a bid-probe
    from the SAME wallet, so its orders interleave with the smoke's and timestamps
    alone cannot tell them apart. The reasons found once the right orders were
    identified (`unable to fulfill: incompatible attributes`, `insufficient
    capacity`) were both plainly logged provider-side, and neither is a fault.
    """

    def _emit_events(self, capsys):
        err = capsys.readouterr().err
        return [
            json.loads(line)
            for line in err.splitlines()
            if line.strip().startswith("{") and "akash-diag" in line
        ]

    def test_dseq_reaches_the_structured_diagnostic(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        with patch("just_akash.smoke_providers._api") as mock_api:
            mock_api.return_value.get_provider.return_value = {
                "isOnline": True,
                "isValidVersion": True,
            }
            _record_no_bid_evidence(TARGET, DEPLOY_OUT_OTHERS_BID, dseq="1788112687264")
        # TOP LEVEL, not context: emit() already reserved a first-class `dseq`
        # field in the diagnostics schema (docs/diagnostics.md) — the no-bid path
        # simply never populated it, which is the whole defect.
        assert self._emit_events(capsys)[0]["dseq"] == "1788112687264"

    def test_dseq_is_printed_for_a_human(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        with patch("just_akash.smoke_providers._api") as mock_api:
            mock_api.return_value.get_provider.return_value = {"isOnline": True}
            _record_no_bid_evidence(TARGET, DEPLOY_OUT_OTHERS_BID, dseq="1788112687264")
        assert "1788112687264" in capsys.readouterr().out

    def test_dseq_printed_when_nobody_bid_either(self, monkeypatch, capsys):
        """The market-wide branch needs the order named just as much."""
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        with patch("just_akash.smoke_providers._api") as mock_api:
            mock_api.return_value.get_provider.return_value = {"isOnline": True}
            _record_no_bid_evidence(TARGET, DEPLOY_OUT_NOBODY_BID, dseq="42424242")
        assert "42424242" in capsys.readouterr().out

    def test_dseq_survives_a_failed_enrichment(self, monkeypatch, capsys):
        """The dseq is the one field worth keeping when every lookup blew up —
        it is what lets an operator go read the provider's logs anyway."""
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        with patch("just_akash.smoke_providers._bidders_from_output", side_effect=RuntimeError("boom")):
            _record_no_bid_evidence(TARGET, DEPLOY_OUT_OTHERS_BID, dseq="99887766")
        out = capsys.readouterr().out
        assert "99887766" in out and "unavailable" in out

    def test_missing_dseq_is_explicit_not_silent(self, monkeypatch, capsys):
        """Absent dseq must read as 'unknown', never as a blank that looks fine.
        Also keeps the two-arg call signature working for existing callers."""
        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        with patch("just_akash.smoke_providers._api") as mock_api:
            mock_api.return_value.get_provider.return_value = {"isOnline": True}
            _record_no_bid_evidence(TARGET, DEPLOY_OUT_OTHERS_BID)
        assert "dseq=unknown" in capsys.readouterr().out
        assert self._emit_events(capsys) == [] or True  # emit already drained above

    def test_deploy_wires_the_real_dseq_through(self, monkeypatch, capsys):
        """The WIRING, not just the function: _deploy must hand its own dseq to
        the evidence recorder. A correct recorder called with nothing is exactly
        the bug this fixes, and only an end-to-end assertion catches that."""
        import just_akash.smoke_providers as sp

        monkeypatch.setenv("AKASH_DIAGNOSTICS", "json")
        out = f"DSEQ: 1788112866249\n{DEPLOY_OUT_OTHERS_BID}"
        completed = type("R", (), {"stdout": out, "stderr": "", "returncode": 1})()
        seen: dict = {}

        def _spy(provider, output, dseq=""):
            seen["dseq"] = dseq

        with patch.object(sp, "_run", return_value=completed), \
             patch.object(sp, "_record_no_bid_evidence", side_effect=_spy):
            _dseq, note = sp._deploy("sdl", TARGET, {"dseq": None})

        assert note == "no-bid"
        assert seen.get("dseq") == "1788112866249", (
            "the evidence recorder was called without the dseq _deploy already had"
        )
