"""Every test here is a false-clean vector the quorum named. None is hypothetical.

The design this replaces was blocked 2/2 because it could report a healthy fleet while
escrow bled. So the bar for this module is not "does it find orphans" — it is "can it
ever say clean when it should not". Each test below is one way it could.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from just_akash.orphan_detect import (
    MIN_CONFIRMATIONS,
    Classification,
    DeploymentVerdict,
    FleetReport,
    active_leases_for,
    classify_deployment,
    enumeration_is_complete,
    live_orders_for,
)

OWNER = "akash14n4rkmz64rn0tey0r5g07l8q5x0fh2h4hu44kt"


def _classify(monkeypatch, readings, lease_readings=None, **kw):
    """Drive classify_deployment with a fixed order reading per endpoint.

    `lease_readings` is the per-endpoint ACTIVE-LEASE count and defaults to 0 everywhere,
    i.e. "the chain says nothing is leased" — the precondition for every order-based case
    below. Pass it explicitly to exercise the lease gate itself. It must be stubbed rather
    than left live: the bases here are "0", "1", ... not URLs, so a real call would fail to
    a None reading and every test would collapse to UNKNOWN.
    """
    seq = list(readings)
    lseq = list(lease_readings) if lease_readings is not None else [0] * len(seq)
    monkeypatch.setattr(
        "just_akash.orphan_detect.live_orders_for",
        lambda dseq, owner, base: seq[int(base)],
    )
    monkeypatch.setattr(
        "just_akash.orphan_detect.active_leases_for",
        lambda dseq, owner, base: lseq[int(base)],
    )
    # Explicit parameters, not a **kwargs splat: a dict[str, object] defeats the type
    # checker, and suppressing that would hide a real signature mismatch later.
    return classify_deployment(
        "1",
        OWNER,
        deployment_state=str(kw.get("deployment_state", "active")),
        console_lease_count=int(kw.get("console_lease_count", 0)),
        escrow_uact=int(kw.get("escrow_uact", 5_000_000)),
        bases=[str(i) for i in range(len(seq))],
    )


# --------------------------------------------------------------------------
# The core distinction the time series was a workaround for
# --------------------------------------------------------------------------


def test_no_live_order_is_an_orphan(monkeypatch):
    """An OPEN ORDER is the only path to a future lease. Without one, a deployment
    holding escrow will never get a lease — that is knowable NOW, from the chain, with
    no sampling at all."""
    v = _classify(monkeypatch, [0, 0])
    assert v.classification is Classification.ORPHANED
    assert v.confirmations == 2


def test_a_live_order_means_WAITING_not_orphaned(monkeypatch):
    """This is the case that made the snapshot unstable: a deployment awaiting its first
    bid is legitimately zero-lease. The chain distinguishes it; lease_count alone cannot."""
    v = _classify(monkeypatch, [1, 1])
    assert v.classification is Classification.WAITING


def test_a_single_endpoint_seeing_an_order_beats_one_seeing_none(monkeypatch):
    """Trust the POSITIVE. A node that sees an order has information a node that sees
    none does not, and a lagging node reporting zero is indistinguishable from a real
    orphan. Being wrong this way costs a delay; the other way destroys a live deployment."""
    v = _classify(monkeypatch, [0, 1])
    assert v.classification is Classification.WAITING


# --------------------------------------------------------------------------
# Unread must never read as clean
# --------------------------------------------------------------------------


def test_no_endpoint_answering_is_UNKNOWN_not_orphaned(monkeypatch):
    """The whole point. An unreachable chain must not manufacture orphans, and must not
    manufacture a clean fleet either."""
    v = _classify(monkeypatch, [None, None])
    assert v.classification is Classification.UNKNOWN
    assert "unread" in v.detail


def test_an_unparseable_response_is_None_not_zero(monkeypatch):
    """Returning 0 for a malformed payload turns a broken endpoint into a destroyed
    deployment."""
    monkeypatch.setattr(
        "just_akash.orphan_detect._lcd_get", lambda *a, **k: {"orders": "not-a-list"}
    )
    assert live_orders_for("1", OWNER, "http://x") is None


def test_a_read_error_is_None_not_zero(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("just_akash.orphan_detect._lcd_get", boom)
    assert live_orders_for("1", OWNER, "http://x") is None


def test_a_non_active_deployment_is_UNKNOWN_not_orphaned(monkeypatch):
    """Only an ACTIVE deployment holding escrow is the thing we are hunting. Anything
    else is someone else's classification to make."""
    v = _classify(monkeypatch, [0, 0], deployment_state="closed")
    assert v.classification is Classification.UNKNOWN


def test_no_endpoints_configured_is_UNKNOWN():
    v = classify_deployment("1", OWNER, deployment_state="active", console_lease_count=0, bases=[])
    assert v.classification is Classification.UNKNOWN


# --------------------------------------------------------------------------
# Destructive action needs CONFIRMATION, not a reading
# --------------------------------------------------------------------------


def test_one_endpoint_alone_is_not_reapable(monkeypatch):
    """A verdict from a single node is a reading, not a confirmation. A lagging node can
    make a live deployment look orphaned."""
    v = _classify(monkeypatch, [0])
    assert v.classification is Classification.ORPHANED
    assert not v.reapable, "one endpoint must never authorise a destroy"


def test_two_agreeing_endpoints_are_reapable(monkeypatch):
    v = _classify(monkeypatch, [0, 0])
    assert v.reapable and v.confirmations >= MIN_CONFIRMATIONS


def test_nothing_but_ORPHANED_is_ever_reapable():
    for c in (Classification.WAITING, Classification.LEASED, Classification.UNKNOWN):
        assert not DeploymentVerdict(dseq="1", classification=c, confirmations=9).reapable


# --------------------------------------------------------------------------
# Truncation — the vector that defeats any per-deployment check
# --------------------------------------------------------------------------


def test_a_truncated_console_list_is_detected():
    """`list_deployments` cannot detect truncation (api.py records it as verified live:
    `total` is the returned page size, `hasMore` always false). A deployment the Console
    omits is invisible to every check downstream, and the report looks CLEANER the worse
    the truncation is. Only a cross-check against the chain notices."""
    ok, why = enumeration_is_complete([{"dseq": "1"}, {"dseq": "2"}], {"1", "2", "3"})
    assert not ok
    assert "truncated or stale" in why and "3" in why


def test_a_complete_list_passes():
    ok, why = enumeration_is_complete([{"dseq": "1"}, {"dseq": "2"}], {"1", "2"})
    assert ok and why == ""


def test_a_degraded_report_says_the_counts_are_a_FLOOR():
    """A short orphan list from a truncated fleet is the exact false-clean this module
    exists to prevent. The summary must not let it read as good news."""
    r = FleetReport(
        verdicts=[
            DeploymentVerdict(
                dseq="1", classification=Classification.ORPHANED, escrow_uact=2_000_000
            )
        ],
        degraded=["Console returned 2 deployments but the chain lists 5"],
    )
    assert r.is_degraded
    out = r.summary()
    assert "DEGRADED" in out and "FLOOR" in out
    assert "does not mean" in out, "it must actively block the optimistic reading"


def test_a_clean_report_is_quiet():
    r = FleetReport(verdicts=[DeploymentVerdict(dseq="1", classification=Classification.LEASED)])
    assert not r.is_degraded and "DEGRADED" not in r.summary()


def test_orphaned_escrow_is_quantified():
    r = FleetReport(
        verdicts=[
            DeploymentVerdict(
                dseq="1", classification=Classification.ORPHANED, escrow_uact=5_000_000
            ),
            DeploymentVerdict(
                dseq="2", classification=Classification.ORPHANED, escrow_uact=2_500_000
            ),
            DeploymentVerdict(
                dseq="3", classification=Classification.WAITING, escrow_uact=9_000_000
            ),
        ]
    )
    assert r.orphaned_escrow_uact == 7_500_000, "WAITING escrow must not be counted as bleed"


# --------------------------------------------------------------------------
# Anti-vacuity: prove these guards can fail
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "readings,expected",
    [
        ([0, 0], Classification.ORPHANED),
        ([1, 0], Classification.WAITING),
        ([None, None], Classification.UNKNOWN),
        ([None, 0], Classification.ORPHANED),  # one unread, one measured-zero
    ],
)
def test_the_classifier_actually_discriminates(monkeypatch, readings, expected):
    """Four inputs, three different answers. A classifier that returned one value for
    everything would pass a single-case test — that is how an unvalidated detector got
    shipped here before."""
    assert _classify(monkeypatch, readings).classification is expected


# --------------------------------------------------------------------------
# It must be REACHABLE — a module with zero callers protects nothing
# --------------------------------------------------------------------------


def test_orphan_scan_is_wired_into_the_cli():
    """This module was initially referenced only from its own tests. A detector nobody
    can invoke is the 'ratified but never invoked' failure: it looks like the problem is
    handled while no runtime path uses it."""
    from just_akash import cli

    src = Path(cli.__file__).read_text()
    assert '"orphan-scan"' in src, "no subcommand registers it"
    assert 'args.command == "orphan-scan"' in src, "no dispatch branch invokes it"
    assert "classify_deployment(" in src, "the classifier is never actually called"


def test_a_degraded_scan_does_not_exit_zero():
    """A caller checking only the exit status must not read an incomplete scan as a
    clean fleet — the same false-clean the classifier refuses one level down."""
    from just_akash import cli

    src = Path(cli.__file__).read_text()
    block = src[
        src.index('args.command == "orphan-scan"') : src.index('args.command == "lease-status"')
    ]
    assert "if report.is_degraded:" in block and "sys.exit(1)" in block


# --------------------------------------------------------------------------
# The lease half must come from the CHAIN, not from Console
#
# Measured 2026-08-22: the Console API reported leases as `active` that the chain said
# were `closed`, and gave different answers four minutes apart for the same 9 dseqs
# (9 ORPHANED, then 2 ORPHANED / 7 LEASED) while the chain said no active lease for any
# of them on both reads. `console_lease_count` used to gate this function before it made
# a single chain call, so a real orphan read as healthy at random.
# --------------------------------------------------------------------------
class TestLeaseStateComesFromTheChain:
    def test_console_says_leased_but_chain_says_no_lease_is_still_an_orphan(self, monkeypatch):
        """The exact production false-negative: Console lies, the chain does not."""
        v = _classify(monkeypatch, [0, 0], lease_readings=[0, 0], console_lease_count=1)
        assert v.classification is Classification.ORPHANED
        assert v.reapable is True

    def test_chain_says_leased_wins_even_when_console_says_nothing(self, monkeypatch):
        """Trust the positive. Never close something an endpoint reports as running."""
        v = _classify(monkeypatch, [0, 0], lease_readings=[1, 0], console_lease_count=0)
        assert v.classification is Classification.LEASED
        assert v.reapable is False

    def test_one_endpoint_seeing_a_lease_is_enough_to_refuse(self, monkeypatch):
        v = _classify(monkeypatch, [0, 0, 0], lease_readings=[0, 0, 2], console_lease_count=0)
        assert v.classification is Classification.LEASED

    def test_unreadable_lease_query_falls_back_to_console_in_the_safe_direction(self, monkeypatch):
        """Chain unreadable + Console says leased -> LEASED, never orphan."""
        v = _classify(monkeypatch, [0, 0], lease_readings=[None, None], console_lease_count=1)
        assert v.classification is Classification.LEASED
        assert v.reapable is False

    def test_unreadable_lease_query_with_no_console_lease_is_UNKNOWN_not_orphan(self, monkeypatch):
        """Absence of evidence is not evidence of absence — the module's whole premise."""
        v = _classify(monkeypatch, [0, 0], lease_readings=[None, None], console_lease_count=0)
        assert v.classification is Classification.UNKNOWN
        assert v.reapable is False

    def test_a_live_order_still_beats_orphan_once_the_lease_gate_passes(self, monkeypatch):
        """The order check must still run after the lease check clears."""
        v = _classify(monkeypatch, [1, 0], lease_readings=[0, 0], console_lease_count=0)
        assert v.classification is Classification.WAITING

    def test_no_endpoints_and_console_says_leased_refuses_rather_than_UNKNOWN(self):
        v = classify_deployment(
            "1", OWNER, deployment_state="active", console_lease_count=1, bases=[]
        )
        assert v.classification is Classification.LEASED
        assert v.reapable is False


class TestActiveLeasesFor:
    def test_counts_only_active_leases(self, monkeypatch):
        payload = {
            "leases": [
                {"lease": {"state": "active"}},
                {"lease": {"state": "closed"}},
                {"state": "active"},  # un-nested shape, some node versions
            ]
        }
        monkeypatch.setattr("just_akash.orphan_detect._lcd_get", lambda p, base: payload)
        assert active_leases_for("1", OWNER, "b") == 2

    def test_read_failure_is_None_not_zero(self, monkeypatch):
        def boom(p, base):
            raise RuntimeError("endpoint down")

        monkeypatch.setattr("just_akash.orphan_detect._lcd_get", boom)
        assert active_leases_for("1", OWNER, "b") is None

    def test_unparseable_payload_is_None_not_zero(self, monkeypatch):
        monkeypatch.setattr(
            "just_akash.orphan_detect._lcd_get", lambda p, base: {"leases": "nope"}
        )
        assert active_leases_for("1", OWNER, "b") is None

    def test_query_filters_state_server_side(self, monkeypatch):
        seen = {}

        def capture(path, base):
            seen["path"] = path
            return {"leases": []}

        monkeypatch.setattr("just_akash.orphan_detect._lcd_get", capture)
        active_leases_for("77", OWNER, "b")
        assert "filters.state=active" in seen["path"]
        assert "filters.dseq=77" in seen["path"]
