"""A create that REPORTED failure may still have created the deployment.

`POST /v1/deployments` writes on-chain state, so a gateway 500, a proxy timeout or a
dropped connection can land after the transaction committed. Measured in CI: HTTP 500
returned 103 SECONDS into the request. The deployment then exists, holds its deposit in
escrow against the grant every later run spends from, carries no tag, and nobody knows
its dseq — so the next run's funding failure reads as a market outage.

Before this, deploy.py raised on any create error that was not "already exists" and never
looked. These lock the reconciliation, including the two ways it must stay quiet: it may
not guess, and it may not mask the original failure.
"""

from __future__ import annotations

import time

import pytest

from just_akash import deploy as dep_mod
from just_akash.deploy import _report_suspected_orphans

NOW = time.time()


def _dseq(offset_s: float) -> str:
    """A ms-epoch dseq, `offset_s` after NOW (negative = before)."""
    return str(int((NOW + offset_s) * 1000))


class _Client:
    def __init__(self, deployments, exc=None):
        self._deployments = deployments
        self._exc = exc
        self.calls = 0

    def list_deployments(self, active_only: bool = True):
        self.calls += 1
        if self._exc:
            raise self._exc
        return self._deployments


def test_a_leaseless_deployment_created_during_the_failed_call_is_named():
    """The whole point: the dseq has to reach the operator, because without it the
    escrow is held by something nobody can find."""
    client = _Client([{"dseq": _dseq(+2), "leases": []}])
    assert _report_suspected_orphans(client, NOW) == [_dseq(+2)]


def test_a_deployment_minted_in_the_same_millisecond_is_still_ours():
    """The tightest true orphan is the one this very call created, and it was the one
    most likely to be missed.

    `time.time()` carries sub-ms precision while a dseq is floored to its millisecond, so
    dividing the dseq back to seconds made `created < since_epoch_s` purely by
    truncation. Compared in whole milliseconds it is caught."""
    started = 1786543210.123456
    same_ms = str(int(started * 1000))  # 1786543210123 — floor of the same instant
    assert _report_suspected_orphans(_Client([{"dseq": same_ms, "leases": []}]), started) == [
        same_ms
    ]


def test_a_deployment_that_predates_the_call_is_not_ours():
    """A create cannot have produced a deployment that already existed. Reporting one
    would send the operator to destroy a live workload."""
    client = _Client([{"dseq": _dseq(-3600), "leases": []}])
    assert _report_suspected_orphans(client, NOW) == []


def test_a_deployment_holding_a_lease_is_somebody_s_workload():
    """A lease means it was bid on and won — that is a live workload, not the residue of
    a request that failed before it could return a dseq."""
    client = _Client([{"dseq": _dseq(+2), "leases": [{"id": {"provider": "akash1x"}}]}])
    assert _report_suspected_orphans(client, NOW) == []


def test_both_signals_are_required_not_either():
    """Age alone would sweep in a concurrent run's fresh deployment; leaselessness alone
    would sweep in every idle deployment on the account."""
    client = _Client(
        [
            {"dseq": _dseq(-60), "leases": []},  # old, leaseless   -> not ours
            {"dseq": _dseq(+1), "leases": [{"id": {}}]},  # new, leased -> not ours
            {"dseq": _dseq(+5), "leases": []},  # new, leaseless  -> ours
        ]
    )
    assert _report_suspected_orphans(client, NOW) == [_dseq(+5)]


def test_an_undateable_dseq_is_not_reported():
    """A dseq that is not a ms timestamp (a legacy block height, a malformed record) is
    not evidence either way. This path exists to name what it can prove — a false
    'possible orphan' sends someone to destroy an unrelated deployment."""
    for bad in ({"dseq": "not-a-number", "leases": []}, {"dseq": None, "leases": []}, {}):
        assert _report_suspected_orphans(_Client([bad]), NOW) == []


def test_the_nested_deployment_shape_is_read_too():
    """The Console API returns the dseq nested under `deployment` in some responses;
    missing it there would silently report nothing on exactly the shape that matters."""
    client = _Client([{"deployment": {"dseq": _dseq(+2)}, "leases": []}])
    assert _report_suspected_orphans(client, NOW) == [_dseq(+2)]


def test_a_failure_to_reconcile_never_replaces_the_original_error():
    """This runs on an error path. If listing also fails, the caller must still surface
    the create failure — swapping in a second error loses the one that explains what
    happened."""
    client = _Client([], exc=RuntimeError("API Error (500): Internal server error"))
    assert _report_suspected_orphans(client, NOW) == []
    assert client.calls == 1


@pytest.mark.parametrize("empty", [None, []])
def test_an_empty_account_reports_nothing(empty):
    assert _report_suspected_orphans(_Client(empty), NOW) == []


def test_it_does_not_close_anything():
    """REPORT ONLY, deliberately. At spike, concurrent runs create deployments at the
    same time, so a leaseless deployment inside this window may be another run's
    in-flight create. Closing on that inference is how a sweep once destroyed 14
    third-party deployments."""
    closed = []

    class _Closing(_Client):
        def close_deployment(self, dseq):  # pragma: no cover - must never be called
            closed.append(dseq)

    _report_suspected_orphans(_Closing([{"dseq": _dseq(+2), "leases": []}]), NOW)
    assert closed == [], "detection must not destroy what it cannot positively attribute"


# --------------------------------------------------------------------------
# Provenance: naming the innocent loses data, so strangers leave the report
# --------------------------------------------------------------------------

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"


class _OwnedClient(_Client):
    def account_address(self) -> str:
        return OWNER


def _with_provenance(monkeypatch, mapping: dict[str, list[str]]):
    monkeypatch.setattr(
        dep_mod.chain, "deployment_group_names", lambda owner, dseq: mapping.get(dseq, [])
    )


def test_another_repos_deployment_in_the_window_is_not_reported(monkeypatch):
    """The failure this prevents is worse than a miss. The shared Console wallet hosts
    other repos' deployments — a live read found six `dfci-infra-runner` among eleven
    active — created concurrently and leaseless for a moment, i.e. matching every
    pre-provenance signal. Handing that dseq to an operator sends them to `destroy` a
    sibling repo's LIVE deployment."""
    d = _dseq(+2)
    _with_provenance(monkeypatch, {d: ["dfci-infra-runner"]})
    assert _report_suspected_orphans(_OwnedClient([{"dseq": d, "leases": []}]), NOW) == []


def test_our_own_orphan_is_reported_and_named_as_confirmed(monkeypatch):
    d = _dseq(+2)
    _with_provenance(monkeypatch, {d: ["just-akash-runner"]})
    assert _report_suspected_orphans(_OwnedClient([{"dseq": d, "leases": []}]), NOW) == [d]


def test_unreadable_provenance_keeps_it_in_the_report(monkeypatch):
    """Every LCD may have failed, and a leak we cannot attribute is still a leak.
    Dropping it would trade the loud failure for the expensive one."""
    d = _dseq(+2)
    _with_provenance(monkeypatch, {})  # empty == could not read
    assert _report_suspected_orphans(_OwnedClient([{"dseq": d, "leases": []}]), NOW) == [d]


def test_an_unreadable_owner_degrades_to_unverified_not_to_silence(monkeypatch):
    """If the account address cannot be fetched, provenance cannot be read for anything.
    That must weaken the CLAIM, never suppress the report."""

    class _NoAddress(_Client):
        def account_address(self) -> str:
            raise RuntimeError("console unreachable")

    d = _dseq(+2)
    _with_provenance(monkeypatch, {d: ["dfci-infra-runner"]})  # never consulted
    assert _report_suspected_orphans(_NoAddress([{"dseq": d, "leases": []}]), NOW) == [d]


def test_a_mixed_window_reports_only_ours(monkeypatch):
    """The realistic spike shape: our failed create alongside a sibling's healthy one and
    one we cannot read."""
    ours, theirs, unknown = _dseq(+1), _dseq(+2), _dseq(+3)
    _with_provenance(monkeypatch, {ours: ["just-akash-backtest"], theirs: ["dfci-infra-consul"]})
    client = _OwnedClient(
        [
            {"dseq": ours, "leases": []},
            {"dseq": theirs, "leases": []},
            {"dseq": unknown, "leases": []},
        ]
    )
    assert _report_suspected_orphans(client, NOW) == [ours, unknown]


def test_provenance_is_read_only_for_suspects(monkeypatch):
    """A chain round-trip per deployment would make an error path slow on an account of
    hundreds — and every one of those deployments already failed the age or lease test."""
    asked: list[str] = []
    monkeypatch.setattr(
        dep_mod.chain,
        "deployment_group_names",
        lambda owner, dseq: asked.append(dseq) or ["just-akash-x"],
    )
    suspect, old, leased = _dseq(+2), _dseq(-3600), _dseq(+3)
    client = _OwnedClient(
        [
            {"dseq": suspect, "leases": []},
            {"dseq": old, "leases": []},
            {"dseq": leased, "leases": [{"id": {}}]},
        ]
    )
    _report_suspected_orphans(client, NOW)
    assert asked == [suspect], asked


def test_confirmed_ownership_still_does_not_close_anything(monkeypatch):
    """Provenance proves the REPO, not the RUN. A concurrent run of *this* repo also
    stamps `just-akash-*`, is also leaseless mid-create, and also lands in this window —
    so closing on a confirmed match would destroy a healthy in-flight deploy."""
    closed = []

    class _Closing(_OwnedClient):
        def close_deployment(self, dseq):  # pragma: no cover - must never be called
            closed.append(dseq)

    d = _dseq(+2)
    _with_provenance(monkeypatch, {d: ["just-akash-runner"]})
    _report_suspected_orphans(_Closing([{"dseq": d, "leases": []}]), NOW)
    assert closed == [], "proving the repo is not proving the run"
