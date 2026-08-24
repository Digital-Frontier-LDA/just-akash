"""Unit tests for api.lease_status — the Console-sourced lease/deployment/escrow
reconciliation behind `just-akash lease-status`.

The shapes here mirror a real Console API `/v1/deployments` record (verified live):
``{deployment:{id,state}, leases:[{id:{provider},state}], escrow_account:{state:{state,funds}}}``.
The escrow balance remaining is ``escrow_account.state.funds`` (not ``transferred``).
"""

from __future__ import annotations

from typing import Any

from just_akash import api


class _FakeClient:
    def __init__(self, deployments: list[Any]):
        self._deps = deployments
        self.last_active_only: bool | None = None

    def list_deployments(self, active_only: bool = True) -> list[Any]:
        self.last_active_only = active_only
        return self._deps


def _dep(
    dseq: str,
    dep_state: str = "active",
    lease_state: str | None = "active",
    provider: str = "akash1prov",
    escrow_state: str = "open",
    funds: tuple[tuple[str, str], ...] | None = (("uact", "2986627.000000000000000000"),),
):
    d = {
        "deployment": {"id": {"owner": "akash1me", "dseq": dseq}, "state": dep_state},
        "leases": (
            [
                {
                    "id": {"owner": "akash1me", "dseq": dseq, "provider": provider},
                    "state": lease_state,
                }
            ]
            if lease_state is not None
            else []
        ),
        "escrow_account": {"id": {"scope": "deployment"}, "state": {"state": escrow_state}},
    }
    if funds is not None:
        d["escrow_account"]["state"]["funds"] = [{"denom": de, "amount": am} for de, am in funds]
    return d


class TestReconcileLeaseRow:
    def test_active_healthy_not_closeable(self):
        row = api._reconcile_lease_row(_dep("1"))
        assert row["dseq"] == "1"
        assert row["deployment_state"] == "active"
        assert row["lease_state"] == "active"
        assert row["provider"] == "akash1prov"
        # amount carries a ".000…" suffix; only the integer part is kept.
        assert row["escrow_remaining_uact"] == 2986627
        assert row["closeable"] is False

    def test_drained_escrow_is_closeable(self):
        row = api._reconcile_lease_row(_dep("2", funds=(("uact", "0"),)))
        assert row["escrow_remaining_uact"] == 0
        assert row["closeable"] is True

    def test_terminal_deployment_state_is_closeable(self):
        # terminal deployment wins even with funds left in escrow.
        row = api._reconcile_lease_row(_dep("3", dep_state="closed", funds=(("uact", "5"),)))
        assert row["closeable"] is True

    def test_closed_escrow_is_closeable(self):
        row = api._reconcile_lease_row(_dep("4", escrow_state="closed", funds=None))
        assert row["escrow_remaining_uact"] is None
        assert row["closeable"] is True

    def test_missing_funds_is_unknown_not_zero(self):
        # funds absent + open escrow + active lease => unknown balance, NOT flagged
        # closeable (a missing field must not masquerade as a drained lease).
        row = api._reconcile_lease_row(_dep("5", funds=None))
        assert row["escrow_remaining_uact"] is None
        assert row["closeable"] is False

    def test_no_lease_row(self):
        row = api._reconcile_lease_row(_dep("6", lease_state=None))
        assert row["lease_state"] is None
        assert row["lease_count"] == 0
        assert row["provider"] is None

    def test_closed_lease_does_not_count_as_active(self):
        """The orphan case: the lease closed, the deployment did not.

        `lease_count` counts every lease on the record, so it stays 1 here — and passing
        THAT to `classify_deployment` returns LEASED and reports the fleet clean. This is
        the field an orphan scan must read. Measured 2026-08-22: 26 such deployments held
        $104.33 for ~45h while `akash_canary_orphans_total` read 0.
        """
        row = api._reconcile_lease_row(_dep("8", lease_state="closed"))
        assert row["lease_count"] == 1
        assert row["active_lease_count"] == 0

    def test_active_lease_counts_as_active(self):
        row = api._reconcile_lease_row(_dep("9", lease_state="active"))
        assert row["lease_count"] == 1
        assert row["active_lease_count"] == 1

    def test_no_lease_counts_zero_both_ways(self):
        row = api._reconcile_lease_row(_dep("10", lease_state=None))
        assert row["lease_count"] == 0
        assert row["active_lease_count"] == 0

    def test_only_uact_funds_counted(self):
        row = api._reconcile_lease_row(_dep("7", funds=(("uakt", "999"), ("uact", "10"))))
        assert row["escrow_remaining_uact"] == 10

    def test_active_zero_leases_is_closeable(self):
        """⭐ #123 LOAD-BEARING DEFECT-DETECTING CONTROL.

        The defect shape measured in production: a deployment is ``active``
        (bid window closed / auction finished), ``lease_count == 0`` (no
        provider ever bid), ``lease_state is None``, ``provider is None``,
        ``escrow_state == "open"`` and a non-zero ``escrow_remaining_uact``
        (13.34 ACT in the reported case). Nothing is running — no provider
        ever took it — yet the OLD ``closeable`` rule returns False because
        none of the four terminals it recognises apply: deployment_state is
        not terminal, lease_state is None (not a terminal string), escrow is
        open, and escrow funds are positive.

        The discriminator is ``lease_count == 0 AND deployment is active``:
        no lease ever existed, so closing the deployment releases the
        escrow back to the grant. This control fails under the OLD code
        (returns False); it MUST pass under the fix.

        Reported by DEVOPS today across AKASH_CONSOLE: 50 active deployments
        with rent transferred 0.0018 ACT — essentially nothing consumed
        against 250 ACT locked. Across all accounts: 569.03 ACT locked over
        116 deployments, ~4.91 ACT each. If a meaningful slice of those 116
        are active-with-zero-leases, fixing ``closeable`` turns dead escrow
        into recoverable escrow automatically.
        """
        row = api._reconcile_lease_row(
            _dep(
                "1234567890",
                dep_state="active",
                lease_state=None,  # no lease — bid window closed without a bid
                provider="akash1prov",  # provider arg is ignored when lease_state is None
                escrow_state="open",
                funds=(("uact", "13344539000000"),),  # 13.34 ACT still locked
            )
        )
        assert row["lease_count"] == 0, "the fixture must start with zero leases"
        assert row["deployment_state"] == "active", "the deployment must still be active"
        assert row["escrow_remaining_uact"] == 13344539000000, (
            "the funds must remain visible — escrow is open and untouched"
        )
        assert row["closeable"] is True, (
            "active + lease_count==0 is the closeable shape: nothing is running, "
            "the escrow is held against nothing, and closing the deployment "
            "releases the locked deposit back to the grant. That is #123."
        )

    def test_active_with_lease_stays_not_closeable(self):
        """⭐ #123 KN — the load-bearing negative control.

        If the fix is implemented as "close anything active" (e.g. a blanket
        ``dep_state == 'active'`` flag), this control catches the catastrophic
        version that would close healthy running deployments. The discriminator
        MUST be active AND zero leases — NOT active alone. Active + at least
        one lease means a provider is running something, and closing releases
        a provider's escrow mid-flight, not dead escrow.

        This control fails under a "close all active" blanket (returns True);
        it MUST pass under the precise "active AND zero leases" discriminator.
        """
        row = api._reconcile_lease_row(
            _dep(
                "9876543210",
                dep_state="active",
                lease_state="active",  # a provider is running on this lease
                provider="akash1prov",
                escrow_state="open",
                funds=(("uact", "5000000"),),
            )
        )
        assert row["lease_count"] == 1
        assert row["active_lease_count"] == 1
        assert row["closeable"] is False, (
            "active + at least one active lease is the NOT-closeable shape: "
            "a provider is running. Closing here releases a live provider's "
            "escrow mid-flight, not dead escrow."
        )


class TestLeaseStatus:
    def test_maps_all_deployments_and_passes_active_only_flag(self):
        c = _FakeClient([_dep("1"), _dep("2", dep_state="closed", funds=(("uact", "0"),))])
        rows = api.lease_status(c, active_only=False)  # type: ignore[arg-type]
        assert c.last_active_only is False
        assert [r["dseq"] for r in rows] == ["1", "2"]
        assert rows[0]["closeable"] is False
        assert rows[1]["closeable"] is True

    def test_defaults_to_active_only(self):
        c = _FakeClient([_dep("1")])
        api.lease_status(c)  # type: ignore[arg-type]
        assert c.last_active_only is True

    def test_skips_non_dict_records(self):
        c = _FakeClient([_dep("1"), "garbage", None])
        rows = api.lease_status(c)  # type: ignore[arg-type]
        assert [r["dseq"] for r in rows] == ["1"]
