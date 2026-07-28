"""Unit tests for api.lease_status — the Console-sourced lease/deployment/escrow
reconciliation behind `just-akash lease-status`.

The shapes here mirror a real Console API `/v1/deployments` record (verified live):
``{deployment:{id,state}, leases:[{id:{provider},state}], escrow_account:{state:{state,funds}}}``.
The escrow balance remaining is ``escrow_account.state.funds`` (not ``transferred``).
"""

from just_akash import api


class _FakeClient:
    def __init__(self, deployments):
        self._deps = deployments
        self.last_active_only = None

    def list_deployments(self, active_only=True):
        self.last_active_only = active_only
        return self._deps


def _dep(
    dseq,
    dep_state="active",
    lease_state="active",
    provider="akash1prov",
    escrow_state="open",
    funds=(("uact", "2986627.000000000000000000"),),
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

    def test_only_uact_funds_counted(self):
        row = api._reconcile_lease_row(_dep("7", funds=(("uakt", "999"), ("uact", "10"))))
        assert row["escrow_remaining_uact"] == 10


class TestLeaseStatus:
    def test_maps_all_deployments_and_passes_active_only_flag(self):
        c = _FakeClient([_dep("1"), _dep("2", dep_state="closed", funds=(("uact", "0"),))])
        rows = api.lease_status(c, active_only=False)
        assert c.last_active_only is False
        assert [r["dseq"] for r in rows] == ["1", "2"]
        assert rows[0]["closeable"] is False
        assert rows[1]["closeable"] is True

    def test_defaults_to_active_only(self):
        c = _FakeClient([_dep("1")])
        api.lease_status(c)
        assert c.last_active_only is True

    def test_skips_non_dict_records(self):
        c = _FakeClient([_dep("1"), "garbage", None])
        rows = api.lease_status(c)
        assert [r["dseq"] for r in rows] == ["1"]
