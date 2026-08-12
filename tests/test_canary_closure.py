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


def _provider_close(dseq=DSEQ, owner=OWNER, version="v1beta5"):
    return {
        "@type": f"/akash.market.{version}.MsgCloseBid",
        "id": {"owner": owner, "dseq": dseq, "gseq": 1, "oseq": 1, "provider": PROV_ADDR},
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


OTHER_OWNER = "akash1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"  # pragma: allowlist secret


def test_a_close_signed_by_someone_else_is_not_booked_as_ours():
    assert classify(_info(), _block(_owner_close(owner=OTHER_OWNER)), OWNER, DSEQ) == UNKNOWN


def test_another_accounts_provider_close_cannot_blame_OUR_provider():
    """dseq is a PER-OWNER sequence, so the same number exists under other accounts. A
    MsgCloseBid against someone else's deployment must never land on our provider's
    eviction counter — that would fabricate exactly the provider fault this module was
    written to stop fabricating. Raised by CodeRabbit and Copilot on #143."""
    assert classify(_info(), _block(_provider_close(owner=OTHER_OWNER)), OWNER, DSEQ) == UNKNOWN


def test_the_owner_check_does_not_break_a_genuine_provider_eviction():
    """The guard above must not be so tight that the real signal stops working."""
    assert classify(_info(), _block(_provider_close()), OWNER, DSEQ) == PROVIDER


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


# ── Lease lifetime, measured on chain ─────────────────────────────────────────────────
#
# WHY THESE EXIST. The obvious source for "how long did the lease live" is
# akash_canary_uptime_seconds, and it is WRONG: that gauge only reaches us on a successful
# scrape, so its last value before a lease dies is a LOWER BOUND at the collection cadence.
# On 2026-08-12 two such bounds (13.79h, 13.69h) were read as failure times, their closeness
# taken as proof of a fixed timer, and the hypothesis survived until the chain gave the real
# numbers for the same leases: 11.91h, 12.06h, 14.23h. No timer, a 2.3h spread.
#
# So the property under test is that a lifetime is published ONLY when both ends came off the
# chain, and that anything else publishes nothing rather than a plausible number.

from canary.closure import (  # noqa: E402
    VERDICTS,
    _block_time,
    attribute_detailed,
    lifetime_hours,
)

# 1786422914704 ms -> 2026-08-11T04:35:14.704Z. Real dseq, from the hetzner_hel lease whose
# chain-measured lifetime was 14.23h.
_DSEQ = "1786422914704"


def _blk(t: str) -> dict:
    return {"block": {"header": {"time": t}}}


def test_lifetime_is_measured_from_dseq_and_settling_block():
    # Real values: created 2026-08-11T04:35:14Z, escrow settled in a block at 18:49:02Z.
    got = lifetime_hours(_DSEQ, _blk("2026-08-11T18:49:02Z"))
    assert got is not None
    assert abs(got - 14.230) < 0.01, got


def test_nanosecond_precision_and_offsets_parse():
    """Cosmos emits RFC3339 with nanoseconds, which datetime.fromisoformat rejects."""
    assert _block_time(_blk("2026-08-11T18:49:02.123456789Z")) is not None
    assert _block_time(_blk("2026-08-11T18:49:02.123456789+00:00")) is not None
    # ...and a naive timestamp must still come back tz-aware, or the subtraction raises.
    bt = _block_time(_blk("2026-08-11T18:49:02"))
    assert bt is not None and bt.tzinfo is not None


def test_unreadable_ends_publish_NOTHING_rather_than_a_number():
    assert lifetime_hours(_DSEQ, None) is None
    assert lifetime_hours(_DSEQ, {}) is None
    assert lifetime_hours(_DSEQ, _blk("")) is None
    assert lifetime_hours(_DSEQ, _blk("not-a-time")) is None
    assert lifetime_hours("", _blk("2026-08-11T18:49:02Z")) is None
    assert lifetime_hours("not-a-dseq", _blk("2026-08-11T18:49:02Z")) is None


def test_impossible_spans_are_refused():
    """A dseq that is not epoch-ms (chain upgrade, foreign minter) must not publish."""
    # Block BEFORE creation -> negative.
    assert lifetime_hours(_DSEQ, _blk("2020-01-01T00:00:00Z")) is None
    # A dseq that is a block height, not a timestamp -> absurd span.
    assert lifetime_hours("28133888", _blk("2026-08-11T18:49:02Z")) is None


def test_attribute_detailed_returns_cause_and_lifetime_from_one_pair_of_reads():
    calls = {"info": 0, "block": 0}

    def fetch_info(owner, dseq):
        calls["info"] += 1
        return {
            "escrow_account": {
                "state": {"state": "closed", "settled_at": "28126510"},
            }
        }

    def fetch_block(height):
        calls["block"] += 1
        assert height == "28126510"
        return {
            "block": {
                "header": {"time": "2026-08-11T18:49:02Z"},
                "data": {},
            },
            "txs": [],
        }

    cause, lived = attribute_detailed(OWNER, _DSEQ, fetch_info=fetch_info, fetch_block=fetch_block)
    assert cause in VERDICTS
    assert lived is not None and abs(lived - 14.230) < 0.01
    # The lifetime must cost NO extra chain reads -- the block was already being fetched to
    # read who signed the close.
    assert calls == {"info": 1, "block": 1}, calls


def test_attribute_facade_still_returns_a_bare_cause():
    """Existing callers must be untouched by the tuple-returning variant."""
    got = attribute(OWNER, _DSEQ, fetch_info=lambda *_: {}, fetch_block=lambda *_: {})
    assert isinstance(got, str) and got in VERDICTS


def test_merge_accepts_both_the_tuple_and_the_bare_string():
    """The shim is load-bearing and therefore tested in BOTH directions.

    Every pre-existing fixture injects a bare cause string; the real attributor now returns
    (cause, lifetime). If merge() mishandled the tuple it would store it AS the cause, fail
    the VERDICTS check, and quietly book every closure as `unknown` -- turning a working
    signal into a permanent shrug.
    """
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
        attribute_closure=lambda *_: (SELF, 12.5),
    )
    assert st[ALPHA]["closures"] == {SELF: 1}
    assert st[ALPHA]["lease_lifetime_hours"] == 12.5

    st2 = merge({}, ALPHA, "100", True, _body("a"), 0.1, 1000.0)
    st2 = merge(
        st2,
        ALPHA,
        "200",
        True,
        _body("b"),
        0.1,
        1100.0,
        owner=OWNER,
        attribute_closure=lambda *_: SELF,
    )
    assert st2[ALPHA]["closures"] == {SELF: 1}
    assert "lease_lifetime_hours" not in st2[ALPHA]


def test_an_unmeasurable_lifetime_CLEARS_the_previous_one():
    """The state is DURABLE, so this is the case that actually matters.

    The first version of this test injected an unmeasurable replacement into a state that had
    never held a lifetime, so it passed whether or not merge() cleared the field — it could
    not detect the bug it was named after. Copilot caught that on #146.

    Sequence it properly: measure one lease, then fail to measure the next. If merge() does
    not drop the field, render() republishes the FIRST lease's lifetime as the second's, and
    it does so exactly when the chain could not be read.
    """
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
        attribute_closure=lambda *_: (SELF, 12.5),
    )
    assert st[ALPHA]["lease_lifetime_hours"] == 12.5, "precondition: a lifetime is recorded"

    # ...now a replacement the chain cannot speak to.
    st = merge(
        st,
        ALPHA,
        "300",
        True,
        _body("c"),
        0.1,
        1200.0,
        owner=OWNER,
        attribute_closure=lambda *_: (UNKNOWN, None),
    )
    assert "lease_lifetime_hours" not in st[ALPHA], (
        "the previous lease's lifetime was republished as this one's"
    )
    assert st[ALPHA]["closures"] == {SELF: 1, UNKNOWN: 1}


def test_a_failing_attributor_also_clears_the_previous_lifetime():
    """Same requirement on the exception path, which is a separate branch."""
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
        attribute_closure=lambda *_: (SELF, 9.0),
    )
    assert st[ALPHA]["lease_lifetime_hours"] == 9.0

    def boom(*_):
        raise RuntimeError("LCD down")

    st = merge(
        st,
        ALPHA,
        "300",
        True,
        _body("c"),
        0.1,
        1200.0,
        owner=OWNER,
        attribute_closure=boom,
    )
    assert "lease_lifetime_hours" not in st[ALPHA]


def test_a_malformed_block_envelope_does_not_raise():
    """lifetime_hours() runs BEFORE attribute_detailed()'s handler, so an AttributeError here
    would escape and crash the whole collection rather than costing one measurement.
    Raised by CodeRabbit on #146."""
    for bad in (
        {"block": "invalid"},
        {"block": {"header": "nope"}},
        {"block": []},
        {"block": {"header": []}},
    ):
        assert lifetime_hours(_DSEQ, bad) is None, bad

    def fetch_info(owner, dseq):
        return {"escrow_account": {"state": {"state": "closed", "settled_at": "1"}}}

    cause, lived = attribute_detailed(
        OWNER, _DSEQ, fetch_info=fetch_info, fetch_block=lambda _: {"block": "invalid"}
    )
    assert cause in VERDICTS
    assert lived is None
