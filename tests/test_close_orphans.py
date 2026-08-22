"""Every test here is a wrong-close vector, not a happy path.

This module closes deployments on a SHARED wallet, so the bar is not "does it close
orphans" — it is "can it ever close something that is not one". Each test below is one way
it could, and `cleanup_stale`'s comment about the sweep that destroyed 14 third-party
deployments is why the bar is set there.
"""

from __future__ import annotations

import pytest

from just_akash import close_orphans
from just_akash.orphan_detect import Classification, DeploymentVerdict

ADDRESS = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"


class _FakeClient:
    """Stands in for AkashConsoleAPI. Records every close so a test can assert on none."""

    def __init__(self, rows, states=None):
        self._rows = rows
        self._states = states or {}
        self.closed: list[str] = []

    def account_address(self) -> str:
        return ADDRESS

    def list_deployments(self, active_only: bool = True):
        return list(self._rows)

    def close_deployment(self, dseq: str):
        self.closed.append(dseq)
        return {}

    def get_deployment(self, dseq: str):
        # Read-back shape; _reconcile_lease_row reads deployment.state off this.
        return {
            "deployment": {
                "id": {"owner": ADDRESS, "dseq": dseq},
                "state": self._states.get(dseq, "closed"),
            },
            "leases": [],
            "escrow_account": {"state": {"state": "closed"}},
        }


def _row(dseq, *, dep_state="active", active_leases=0, escrow=5_000_000):
    return {
        "deployment": {"id": {"owner": ADDRESS, "dseq": dseq}, "state": dep_state},
        "leases": (
            [{"id": {"owner": ADDRESS, "dseq": dseq, "provider": "akash1p"}, "state": "active"}]
            * active_leases
        ),
        "escrow_account": {
            "state": {"state": "open", "funds": [{"denom": "uact", "amount": str(escrow)}]}
        },
    }


@pytest.fixture
def _wired(monkeypatch):
    """Install a fake client + a credit line that needs no chain, and return a setter."""
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.setattr(close_orphans, "_credit_line", lambda client, address: "granted=0.00")
    monkeypatch.setattr(close_orphans, "SETTLE_PAUSE_SECONDS", 0)

    def install(rows, verdicts, states=None):
        client = _FakeClient(rows, states)
        monkeypatch.setattr(close_orphans, "AkashConsoleAPI", lambda key: client)
        monkeypatch.setattr(
            close_orphans,
            "classify_deployment",
            lambda dseq, owner, **kw: verdicts[dseq],
        )
        return client

    return install


def _verdict(dseq, classification, confirmations=2, escrow=5_000_000, detail=""):
    return DeploymentVerdict(
        dseq=dseq,
        classification=classification,
        escrow_uact=escrow,
        confirmations=confirmations,
        detail=detail,
    )


# --------------------------------------------------------------------------
# The input itself cannot widen
# --------------------------------------------------------------------------
class TestParseDseqs:
    def test_comma_and_space_and_dedup_preserving_order(self):
        assert close_orphans.parse_dseqs(["3,1 2", "1"]) == ["3", "1", "2"]

    def test_empty_input_is_empty(self):
        assert close_orphans.parse_dseqs(["", "  ,  "]) == []


def test_empty_dseq_list_is_a_refusal_not_a_sweep(_wired):
    """No dseqs must never mean "all of them". This is the whole safety premise."""
    client = _wired([], {})
    assert close_orphans.run(dseqs=[], execute=True) == 2
    assert client.closed == []


# --------------------------------------------------------------------------
# Wrong-close vectors
# --------------------------------------------------------------------------
def test_dry_run_closes_nothing_even_when_verified(_wired):
    rows = [_row("1")]
    client = _wired(rows, {"1": _verdict("1", Classification.ORPHANED)})
    assert close_orphans.run(dseqs=["1"], execute=False) == 0
    assert client.closed == []


def test_leased_deployment_is_refused(_wired):
    rows = [_row("1", active_leases=1)]
    client = _wired(rows, {"1": _verdict("1", Classification.LEASED)})
    assert close_orphans.run(dseqs=["1"], execute=True) == 0
    assert client.closed == []


def test_waiting_on_a_bid_is_refused(_wired):
    """A deployment with a live order is legitimately lease-less, not an orphan."""
    rows = [_row("1")]
    client = _wired(rows, {"1": _verdict("1", Classification.WAITING)})
    assert close_orphans.run(dseqs=["1"], execute=True) == 0
    assert client.closed == []


def test_unknown_is_refused_because_unread_is_not_clean(_wired):
    rows = [_row("1")]
    client = _wired(rows, {"1": _verdict("1", Classification.UNKNOWN, detail="no endpoint")})
    assert close_orphans.run(dseqs=["1"], execute=True) == 0
    assert client.closed == []


def test_orphan_with_too_few_confirmations_is_refused(_wired):
    """ORPHANED on one endpoint is a reading, not a confirmation — `.reapable` gates it."""
    rows = [_row("1")]
    client = _wired(rows, {"1": _verdict("1", Classification.ORPHANED, confirmations=1)})
    assert close_orphans.run(dseqs=["1"], execute=True) == 0
    assert client.closed == []


def test_dseq_not_in_active_set_is_skipped_not_closed(_wired):
    """Someone else's dseq, or one already closed. Both are refusals."""
    rows = [_row("1")]
    client = _wired(rows, {"1": _verdict("1", Classification.ORPHANED)})
    assert close_orphans.run(dseqs=["999"], execute=True) == 0
    assert client.closed == []


def test_only_the_verified_orphan_in_a_mixed_list_is_closed(_wired):
    rows = [_row("1"), _row("2", active_leases=1), _row("3")]
    client = _wired(
        rows,
        {
            "1": _verdict("1", Classification.ORPHANED),
            "2": _verdict("2", Classification.LEASED),
            "3": _verdict("3", Classification.WAITING),
        },
    )
    assert close_orphans.run(dseqs=["1", "2", "3"], execute=True) == 0
    assert client.closed == ["1"]


# --------------------------------------------------------------------------
# A close is only a close once the state says so
# --------------------------------------------------------------------------
def test_close_that_does_not_read_back_terminal_is_counted_failed(_wired):
    """A DELETE against an account that does not own the lease succeeds trivially."""
    rows = [_row("1")]
    client = _wired(rows, {"1": _verdict("1", Classification.ORPHANED)}, states={"1": "active"})
    assert close_orphans.run(dseqs=["1"], execute=True) == 1
    assert client.closed == ["1"]


def test_close_read_back_terminal_succeeds(_wired):
    rows = [_row("1")]
    client = _wired(rows, {"1": _verdict("1", Classification.ORPHANED)}, states={"1": "closed"})
    assert close_orphans.run(dseqs=["1"], execute=True) == 0
    assert client.closed == ["1"]


def test_missing_api_key_refuses_before_touching_anything(monkeypatch):
    monkeypatch.delenv("AKASH_API_KEY", raising=False)
    assert close_orphans.run(dseqs=["1"], execute=True) == 2
