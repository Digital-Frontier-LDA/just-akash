"""AC2 (just-akash-3sq): the charter is POSTed to Flynn BEFORE the spend and bound AFTER it.

The order is the contract. A charter written after the create leaves a window where an
uncharted lease of ours is on chain; a bind written before the lease names a dseq that may
not survive the auction (the issue-#19 re-deploy replaces it). So one event log, shared by
the fake Console, the fake Flynn and the credit probe, must read:

    flynn.post < credit_probe < create_deployment < create_lease < deposit < bind

and a POST that fails in any way refuses the deploy with ZERO create_deployment calls.

AC5: everything here runs against 127.0.0.1 fakes -- no lease, no Flynn row, no spend.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from just_akash import charter as charter_mod
from just_akash import deploy as deploy_mod
from tests import _charter_harness as harness


@pytest.fixture
def h(monkeypatch, tmp_path):
    hh = harness.start(monkeypatch, tmp_path)
    monkeypatch.setattr(charter_mod, "BIND_RETRY_DELAY_S", 0)
    yield hh
    harness.stop(hh)


def _deploy(h, key="fleet-serve-qwen", **charter_overrides):
    charter = charter_mod.load_charter(h.write_charter(**charter_overrides))
    return deploy_mod.deploy(
        sdl_path=h.write_sdl(key), bid_wait=1, bid_wait_retry=1, charter=charter
    )


def _first(names, name):
    assert name in names, (name, names)
    return names.index(name)


def test_the_charter_is_posted_first_and_bound_last(h):
    h.set_bid_price(1000)
    result = _deploy(h)
    names = h.names()
    order = [
        "flynn.post",
        "credit_probe",
        "create_deployment",
        "create_lease",
        "deposit_deployment",
        "flynn.bind",
    ]
    positions = [_first(names, n) for n in order]
    assert positions == sorted(positions), names

    (posted,) = h.flynn.posts
    assert posted["bead"] == "openmix-pvr6.1" and posted["kind"] == "job"
    assert "wallet_label" not in posted, "local-only fields must not reach Flynn's validator"
    assert h.flynn.auth[0] == "Bearer fake-internal-key"

    ((cid, body),) = h.flynn.binds
    assert cid == "ch-0123456789"
    assert body == {
        "wallet": "console-2",
        "dseq": result["dseq"],
        "endpoint_url": "https://serve.example/v1",
        "telemetry_url": "https://serve.example/metrics",
    }
    assert result["charter_id"] == "ch-0123456789"


def test_the_placement_name_carries_the_charter_id(h):
    h.set_bid_price(1000)
    result = _deploy(h)
    sdl = h.app.deployments[result["dseq"]]["sdl"]
    assert "fleet-serve-qwen.ch-0123456789:" in sdl
    assert "fleet-serve-qwen:" not in sdl


@pytest.mark.parametrize(
    "status,charter_id",
    [(500, "ch-0123456789"), (422, "ch-0123456789"), (201, "not-a-charter-id")],
    ids=["flynn-500", "flynn-422", "malformed-id"],
)
def test_a_failed_post_refuses_the_deploy(h, capsys, status, charter_id):
    h.flynn.post_status = status
    h.flynn.charter_id = charter_id
    with pytest.raises(charter_mod.CharterError):
        _deploy(h)
    assert h.app.created_deposits == [], "a refused charter must create nothing"
    assert "credit_probe" not in h.names()
    assert "CHARTER_POST_FAILED" in capsys.readouterr().err


def test_no_flynn_credentials_refuses_before_any_spend(monkeypatch, h):
    monkeypatch.delenv("FLYNN_INTERNAL_API_KEY")
    with pytest.raises(charter_mod.CharterError, match="FLYNN_INTERNAL_API_KEY"):
        _deploy(h)
    assert h.app.created_deposits == [] and h.flynn.posts == []


def test_a_plaintext_flynn_url_off_localhost_is_refused(monkeypatch):
    """The internal key travels as a bearer header; it must never cross the network in clear."""
    monkeypatch.setenv("FLYNN_API_URL", "http://flynn.example")
    monkeypatch.setenv("FLYNN_INTERNAL_API_KEY", "k")
    with pytest.raises(charter_mod.CharterError, match="https"):
        charter_mod.FlynnCharterClient()


def test_a_redeploy_binds_the_new_dseq(h):
    """The issue-#19 round closes the first order and creates another. The bind must name
    the dseq that holds the lease, and the second order must be stamped and min-funded too."""
    h.set_bid_price(1000)
    h.app.lease_errors = [(404, "no lease for deployment")]
    result = _deploy(h)
    assert h.app.closed[:1] == ["1000"]
    assert result["dseq"] == "1001"
    assert h.app.created_deposits == [0.5, 0.5]
    assert "fleet-serve-qwen.ch-0123456789:" in h.app.deployments["1001"]["sdl"]
    ((_, body),) = h.flynn.binds
    assert body["dseq"] == "1001"
    assert h.app.deposits == [("1001", pytest.approx(6.4))]


def test_a_failing_bind_is_retried_then_reported_and_the_lease_kept(h, capsys):
    h.set_bid_price(1000)
    h.flynn.bind_statuses = [500, 500, 500]
    result = _deploy(h)
    assert len(h.flynn.binds) == 3
    assert result["dseq"] not in h.app.closed
    assert "CHARTER_BIND_FAILED" in capsys.readouterr().err
    assert result["charter_bound"] is False


def test_a_transient_bind_failure_recovers(h):
    h.set_bid_price(1000)
    h.flynn.bind_statuses = [503]
    result = _deploy(h)
    assert len(h.flynn.binds) == 2 and result["charter_bound"] is True


def test_every_endpoint_in_these_tests_is_a_localhost_fake(h):
    """AC5 guard: no real Console, no real Flynn. A harness change that pointed either at a
    real host would make this suite spend money, so it fails here first."""
    import os

    from just_akash import deploy as deploy_mod

    for url in (
        deploy_mod.AkashConsoleAPI("test-key").base_url,
        charter_mod.FlynnCharterClient().base_url,
        os.environ["AKASH_CONSOLE_URL"],
        os.environ["FLYNN_API_URL"],
    ):
        assert urlparse(url).hostname == "127.0.0.1", url
