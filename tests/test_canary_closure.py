"""Tests for lease-closure attribution (canary/closure.py, and its use in collect.py).

WHAT THESE ARE GUARDING. On 2026-08-11 df-grafana paged three providers critical with
"this provider does not keep customer deployments alive". The chain said every one of
those closures was `MsgCloseDeployment` signed by our own wallet — we were closing our
own canaries and reading the redeploys back as provider evictions. The rule was blaming a
cause it could not observe.

So the property under test is not "attribution works" but "attribution never invents a
culprit". A shape it does not recognise, an LCD that will not answer, a chain upgrade that
renames a type URL — every one of those must come out as `unknown`, which is published and
alerted on, rather than as somebody's fault.
"""

from __future__ import annotations

import pytest

from canary.closure import (
    LAPSED,
    OPEN,
    PROVIDER,
    SELF,
    UNKNOWN,
    attribute,
    classify,
)
from canary.collect import merge, parse_exposition, render

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"  # pragma: allowlist secret
PROV_ADDR = "akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z"  # pragma: allowlist secret
DSEQ = "1786409659160"
ALPHA = "alphavps"


def _info(*, state: str = "closed", escrow: str = "closed", settled: str | None = "28122864"):
    """A deployments/info document in publicnode's nested-escrow shape."""
    return {
        "deployment": {"id": {"owner": OWNER, "dseq": DSEQ}, "state": state},
        "escrow_account": {"state": {"state": escrow, "settled_at": settled}},
    }


def _block(*msgs):
    return {"txs": [{"body": {"messages": [m]}} for m in msgs]}


def _owner_close(dseq=DSEQ, owner=OWNER, version="v1beta4"):
    return {
        "@type": f"/akash.deployment.{version}.MsgCloseDeployment",
        "id": {"owner": owner, "dseq": dseq},
    }


def _provider_close(dseq=DSEQ, version="v1beta5"):
    return {
        "@type": f"/akash.market.{version}.MsgCloseBid",
        "id": {"owner": OWNER, "dseq": dseq, "gseq": 1, "oseq": 1, "provider": PROV_ADDR},
    }


def _body(boot: str) -> str:
    return f'akash_canary_build_info{{version="1.0.0",boot_id="{boot}"}} 1\n'


# ── the decision table ──────────────────────────────────────────────────────────────────


def test_owner_signed_close_is_ours_not_the_providers():
    """The exact 2026-08-11 case: MsgCloseDeployment from our wallet."""
    assert classify(_info(), _block(_owner_close()), OWNER, DSEQ) == SELF


def test_provider_closing_its_bid_is_the_providers():
    assert classify(_info(), _block(_provider_close()), OWNER, DSEQ) == PROVIDER


def test_overdrawn_escrow_with_no_close_message_is_a_lapse_not_a_fault():
    """We underfunded it. Nobody closed anything, and no provider should be paged."""
    assert classify(_info(escrow="overdrawn"), _block(), OWNER, DSEQ) == LAPSED


def test_an_explicit_close_outranks_an_overdrawn_escrow():
    """A deployment we closed also stops being funded, so escrow state alone would
    misread our own close as a lapse."""
    assert classify(_info(escrow="overdrawn"), _block(_owner_close()), OWNER, DSEQ) == SELF


def test_a_deployment_still_open_on_chain_is_reported_as_orphaned():
    """We stopped watching a live lease — the duplicate-canary case ensure.py warns
    about. It costs escrow and it is ours, so it must not read as a closure."""
    assert classify(_info(state="active"), None, OWNER, DSEQ) == OPEN


def test_a_close_for_a_DIFFERENT_dseq_in_the_same_block_is_not_ours():
    """Blocks carry unrelated traffic, and our canary closes have been observed sharing
    a block with other deployments of ours. Matching on message type alone would
    attribute whichever close landed first."""
    assert classify(_info(), _block(_owner_close(dseq="999")), OWNER, DSEQ) == UNKNOWN


def test_a_close_signed_by_someone_else_is_not_booked_as_ours():
    other = "akash1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"  # pragma: allowlist secret
    assert classify(_info(), _block(_owner_close(owner=other)), OWNER, DSEQ) == UNKNOWN


def test_no_close_message_and_a_normally_closed_escrow_is_unknown():
    """Honest ignorance. This is the value df-grafana alerts on, precisely so that a
    silent loss of attribution cannot look like a healthy fleet."""
    assert classify(_info(), _block(), OWNER, DSEQ) == UNKNOWN


@pytest.mark.parametrize("version", ["v1beta3", "v1beta4", "v1beta5", "v2"])
def test_attribution_survives_a_module_version_bump(version):
    """Matching pins the message NAME, never the versioned type URL. Pinning the version
    would make the next chain upgrade misattribute every closure as `unknown` while the
    rule consuming it went quietly blind — the exact failure mode this replaces."""
    assert classify(_info(), _block(_owner_close(version=version)), OWNER, DSEQ) == SELF


def test_flat_escrow_shape_is_read_as_well_as_the_nested_one():
    """publicnode nests escrow_account.state; other LCDs flatten it. Reading only one
    shape would send every closure to `unknown` after an endpoint swap."""
    flat = {
        "deployment": {"id": {"owner": OWNER, "dseq": DSEQ}, "state": "closed"},
        "escrow_account": {"state": "overdrawn", "settled_at": "28122864"},
    }
    assert classify(flat, _block(), OWNER, DSEQ) == LAPSED


# ── never raises ────────────────────────────────────────────────────────────────────────


def test_an_unreadable_chain_is_unknown_rather_than_an_exception():
    def boom(*_args, **_kwargs):
        raise RuntimeError("chain query failed")

    assert attribute(OWNER, DSEQ, fetch_info=boom) == UNKNOWN


def test_a_missing_owner_or_dseq_cannot_blame_anyone():
    assert attribute("", DSEQ) == UNKNOWN
    assert attribute(OWNER, "") == UNKNOWN


def test_a_block_fetch_failure_still_yields_the_escrow_only_verdict():
    def boom(_height):
        raise RuntimeError("no such block")

    verdict = attribute(
        OWNER,
        DSEQ,
        fetch_info=lambda *_: _info(escrow="overdrawn"),
        fetch_block=boom,
    )
    assert verdict == LAPSED


def test_garbage_shapes_do_not_raise():
    for junk in ({}, {"deployment": "nonsense"}, {"escrow_account": []}):
        assert attribute(OWNER, DSEQ, fetch_info=lambda *_, j=junk: j) in (UNKNOWN, OPEN)


# ── the collector wiring ────────────────────────────────────────────────────────────────


def test_a_replacement_attributes_the_lease_we_STOPPED_watching():
    """The old dseq is the only handle the chain will answer about, and merge() overwrites
    it moments later. Asking about the new one would attribute a live deployment."""
    asked: list[str] = []

    def attributor(owner, dseq):
        asked.append(dseq)
        return SELF

    st = merge({}, ALPHA, "100", True, _body("aaa"), 0.1, 1000.0)
    st = merge(
        st,
        ALPHA,
        "200",
        True,
        _body("zzz"),
        0.1,
        1100.0,
        owner=OWNER,
        attribute_closure=attributor,
    )
    assert asked == ["100"]
    assert st[ALPHA]["closures"] == {SELF: 1}
    assert st[ALPHA]["lease_replacements_total"] == 1


def test_causes_accumulate_separately_across_runs():
    causes = iter([SELF, PROVIDER, SELF])
    st = merge({}, ALPHA, "100", True, _body("a"), 0.1, 1000.0)
    for i, dseq in enumerate(("200", "300", "400"), start=1):
        st = merge(
            st,
            ALPHA,
            dseq,
            True,
            _body(f"b{i}"),
            0.1,
            1000.0 + i,
            owner=OWNER,
            attribute_closure=lambda *_: next(causes),
        )
    assert st[ALPHA]["closures"] == {SELF: 2, PROVIDER: 1}
    assert st[ALPHA]["lease_replacements_total"] == 3


def test_no_owner_books_the_replacement_as_unknown_not_as_a_provider_fault():
    st = merge({}, ALPHA, "100", True, _body("a"), 0.1, 1000.0)
    st = merge(st, ALPHA, "200", True, _body("b"), 0.1, 1100.0)
    assert st[ALPHA]["closures"] == {UNKNOWN: 1}


def test_an_attributor_that_raises_loses_the_cause_not_the_run():
    def boom(*_args):
        raise RuntimeError("LCD down")

    st = merge({}, ALPHA, "100", True, _body("a"), 0.1, 1000.0)
    st = merge(
        st,
        ALPHA,
        "200",
        True,
        _body("b"),
        0.1,
        1100.0,
        owner=OWNER,
        attribute_closure=boom,
    )
    assert st[ALPHA]["closures"] == {UNKNOWN: 1}
    assert st[ALPHA]["reachable"] == 1


def test_an_attributor_returning_junk_is_normalised_to_unknown():
    st = merge({}, ALPHA, "100", True, _body("a"), 0.1, 1000.0)
    st = merge(
        st,
        ALPHA,
        "200",
        True,
        _body("b"),
        0.1,
        1100.0,
        owner=OWNER,
        attribute_closure=lambda *_: "the-provider-did-it",
    )
    assert st[ALPHA]["closures"] == {UNKNOWN: 1}


def test_a_restart_within_one_lease_attributes_nothing():
    """Attribution costs two chain reads. It must fire on replacements only."""
    called = []
    st = merge({}, ALPHA, "100", True, _body("a"), 0.1, 1000.0)
    st = merge(
        st,
        ALPHA,
        "100",
        True,
        _body("b"),
        0.1,
        1100.0,
        owner=OWNER,
        attribute_closure=lambda *a: called.append(a) or SELF,
    )
    assert called == []
    assert st[ALPHA]["restarts_total"] == 1
    assert st[ALPHA].get("closures") == {}


def test_render_emits_every_cause_including_zeros():
    """An absent series makes increase() match nothing, so a rule gated on
    cause="provider" could not fire in the state that most needs it."""
    st = merge({}, ALPHA, "100", True, _body("a"), 0.1, 1000.0)
    st = merge(
        st,
        ALPHA,
        "200",
        True,
        _body("b"),
        0.1,
        1100.0,
        owner=OWNER,
        attribute_closure=lambda *_: PROVIDER,
    )
    samples = parse_exposition(render(st, 1200.0))

    def cause(c):
        return samples[("akash_canary_lease_closures_total", (("cause", c), ("provider", ALPHA)))]

    assert cause(PROVIDER) == 1
    for c in (SELF, LAPSED, OPEN, UNKNOWN):
        assert cause(c) == 0
