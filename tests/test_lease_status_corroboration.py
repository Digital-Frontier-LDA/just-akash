"""An empty Console listing has three meanings, and only one is an all-clear (#208).

⛔ `closeable_count: 0` prints identically whether the fleet is genuinely clean, the
listing is incomplete, or nobody could check. `lease-status` reported all three as
success, so the one signal an operator uses to decide "nothing to close" was the one
signal that could not distinguish "nothing" from "no answer".

★ THIS IS THE SAME SHAPE AS THE READ SIDE. `active_deployment_count` returns None and
never 0 on an unreadable chain, for exactly this reason: corroborating an empty listing
with an empty answer is not corroboration. The two halves have to agree, so both are
pinned here — the reader's None-not-zero property AND the decision that consumes it.
"""

from __future__ import annotations

import pytest

from just_akash import chain


class TestTheDecision:
    def test_a_non_empty_listing_needs_no_corroboration(self):
        assert chain.corroborate_listing(listing_is_empty=False, chain_active=None) == []
        assert chain.corroborate_listing(listing_is_empty=False, chain_active=24) == []

    def test_empty_listing_corroborated_by_a_chain_zero_is_clean(self):
        """The ONLY all-clear: both sources agree the fleet is empty."""
        assert chain.corroborate_listing(listing_is_empty=True, chain_active=0) == []

    def test_empty_listing_against_a_live_chain_is_a_mismatch(self):
        out = chain.corroborate_listing(listing_is_empty=True, chain_active=24, address="akash1x")
        assert len(out) == 1
        assert "24 ACTIVE" in out[0] and "akash1x" in out[0]

    def test_empty_listing_with_an_unreadable_chain_is_unconfirmed_not_clean(self):
        """⛔ THE CASE THE BUG COLLAPSED. None must not behave like 0."""
        out = chain.corroborate_listing(listing_is_empty=True, chain_active=None)
        assert len(out) == 1
        assert "UNCONFIRMED" in out[0]

    def test_zero_and_none_do_not_produce_the_same_verdict(self):
        """Stated as its own assertion because `if chain_active:` treats them alike —
        that falsy-collapse is precisely how this defect is reintroduced."""
        clean = chain.corroborate_listing(listing_is_empty=True, chain_active=0)
        unknown = chain.corroborate_listing(listing_is_empty=True, chain_active=None)
        assert clean != unknown, "an unreadable chain was reported as a clean fleet"


class TestTheReader:
    @pytest.mark.parametrize(
        "payload",
        [
            RuntimeError("transport died"),  # the chain could not be reached
            {"deployments": None},  # present but not a list
            {},  # key absent entirely
            {"deployments": "not-a-list"},  # wrong type
        ],
    )
    def test_an_unreadable_chain_is_none_never_zero(self, monkeypatch, payload):
        def _fake(path, timeout=15):
            if isinstance(payload, Exception):
                raise payload
            return payload

        monkeypatch.setattr(chain, "_lcd_get", _fake)
        got = chain.active_deployment_count("akash1x")
        assert got is None, f"unreadable chain returned {got!r} — 0 would read as 'clean'"

    def test_a_readable_chain_returns_the_count(self, monkeypatch):
        monkeypatch.setattr(chain, "_lcd_get", lambda p, timeout=15: {"deployments": [{}, {}, {}]})
        assert chain.active_deployment_count("akash1x") == 3

    def test_a_genuinely_empty_chain_returns_zero_not_none(self, monkeypatch):
        """The control. If this also returned None, the reader would be trivially
        'safe' and the distinction above would be untestable."""
        monkeypatch.setattr(chain, "_lcd_get", lambda p, timeout=15: {"deployments": []})
        assert chain.active_deployment_count("akash1x") == 0

    def test_the_market_module_version_is_not_reused_for_deployments(self):
        """v1beta4 for deployments, v1beta5 for market — swapping them 501s."""
        assert chain._MARKET_API.endswith("v1beta5")
        assert "v1beta4" in chain._DEPLOYMENT_API
