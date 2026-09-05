"""The stale-deployment recovery closes; it must prove what it closes (#267).

`deploy.py`'s recovery called `client.list_deployments(active_only=True)` and
closed EVERY lease-less row. Three defects at once, each already documented in
this repo for a NEIGHBOURING function:

  * `list_deployments` does not scope by API key (cleanup_stale.py:391 — three
    distinct keys for three distinct accounts returned byte-identical bodies).
    `_report_suspected_orphans` tolerates that because it only NAMES.
  * No provenance. The shared wallet hosts other repos' deployments.
  * No age floor, though `_report_suspected_orphans` states that a concurrent
    run of this repo "is also leaseless mid-create, and lands in the same
    window" — the collision ci.yml:150-155 attributes to this function.

⚠️ EACH GUARD IS TESTED FOR ITS OWN CONTRIBUTION, not through the combined
result. Five overlapping guards can hide a dead one: if a test only asserts
"nothing wrong was closed", a guard that never fires is indistinguishable from
one that does its job, and reordering or narrowing another guard reopens the
hole with the suite still green.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest

from just_akash import deploy as dp

NOW = time.time()
OLD = dp.STALE_RETRY_MIN_AGE_SECONDS + 3600
OURS = "just-akash-runner-abc123"

_MINIMAL_SDL = """\
version: "2.0"
services:
  web:
    image: nginx
    expose:
      - port: 80
        as: 80
        to:
          - global: true
profiles:
  compute:
    web:
      resources:
        cpu: {units: 0.1}
        memory: {size: 128Mi}
        storage: {size: 128Mi}
  placement:
    akash:
      pricing:
        web: {denom: uakt, amount: 1000}
deployment:
  web:
    akash:
      profile: web
      count: 1
"""


def _dseq(age_s: float) -> str:
    return str(int((NOW - age_s) * 1000))


def _client(details: dict[str, dict] | None = None, address: str = "akash1me"):
    c = MagicMock()
    c.account_address.return_value = address
    c.get_deployment.side_effect = lambda d: (details or {}).get(str(d), {"leases": []})
    return c


def _run(client, chain_rows, *, names: str | Callable[..., list[str]] = OURS, now=NOW):
    """Names default to OURS so a test that varies something ELSE is not
    silently passing because provenance happened to reject everything."""
    with (
        patch.object(dp.chain, "list_active_deployments", return_value=chain_rows),
        patch.object(
            dp.chain,
            "deployment_group_names",
            side_effect=(names if callable(names) else (lambda o, d: [names] if names else [])),
        ),
    ):
        return dp._close_stale_for_retry(client, now=now)


def _rows(*dseqs):
    return [{"deployment": {"id": {"owner": "akash1me", "dseq": d}}} for d in dseqs]


class TestEachGuardOnItsOwn:
    def test_guard1_a_chain_failure_closes_NOTHING(self):
        """⛔ None IS NOT []. 'Could not ask the chain' swept as 'holds nothing'
        is how a broken enumeration reads as a clean account."""
        client = _client()
        assert _run(client, None) == []
        client.close_deployment.assert_not_called()

    def test_guard1_enumeration_is_owner_scoped_not_the_console_listing(self):
        """The listing cannot scope by key, and this path CLOSES. Asserted by
        absence: `list_deployments` must never be consulted here."""
        client = _client()
        _run(client, _rows(_dseq(OLD)))
        client.list_deployments.assert_not_called()

    def test_guard2_a_fresh_deployment_is_left_alone(self):
        """The concurrency defect itself: a sibling run's deployment is
        leaseless mid-create and lands in exactly this window."""
        client = _client()
        assert _run(client, _rows(_dseq(60))) == []
        client.close_deployment.assert_not_called()

    def test_guard2_an_undatable_dseq_is_not_treated_as_old(self):
        """Unknown must never read as safe to destroy."""
        client = _client()
        assert _run(client, [{"deployment": {"id": {"owner": "akash1me", "dseq": "12345"}}}]) == []

    def test_guard3_a_leased_deployment_is_left_alone(self):
        """A lease means somebody's live workload."""
        d = _dseq(OLD)
        client = _client({d: {"leases": [{"id": {"provider": "akash1p"}}]}})
        assert _run(client, _rows(d)) == []
        client.close_deployment.assert_not_called()

    def test_guard4_another_repos_deployment_is_left_alone(self):
        """The shared wallet hosts other repos — a live read found six
        `dfci-infra-runner` among eleven active."""
        client = _client()
        assert _run(client, _rows(_dseq(OLD)), names="dfci-infra-runner-xyz") == []
        client.close_deployment.assert_not_called()

    def test_guard4_unreadable_provenance_is_unproven_ownership(self):
        def _boom(owner, dseq):
            raise RuntimeError("every LCD failed")

        client = _client()
        assert _run(client, _rows(_dseq(OLD)), names=_boom) == []

    def test_the_happy_path_still_closes_what_it_can_prove(self):
        """Every guard rejecting everything would pass all of the above while
        making the function useless. This is the complement."""
        d = _dseq(OLD)
        client = _client()
        assert _run(client, _rows(d)) == [d]
        client.close_deployment.assert_called_once_with(d)


class TestTheCapConstrains:
    """Same standard as #268: at the boundary AND one past, both directions."""

    def _many(self, n):
        return [_dseq(OLD + i) for i in range(n)]

    def test_exactly_at_the_cap_closes_all(self):
        ds = self._many(dp.STALE_RETRY_MAX_CLOSE)
        assert len(_run(_client(), _rows(*ds))) == dp.STALE_RETRY_MAX_CLOSE

    def test_one_past_the_cap_closes_exactly_the_cap(self):
        ds = self._many(dp.STALE_RETRY_MAX_CLOSE + 1)
        assert len(_run(_client(), _rows(*ds))) == dp.STALE_RETRY_MAX_CLOSE

    def test_one_under_the_cap_closes_everything(self):
        ds = self._many(dp.STALE_RETRY_MAX_CLOSE - 1)
        assert len(_run(_client(), _rows(*ds))) == dp.STALE_RETRY_MAX_CLOSE - 1

    def test_the_cap_takes_the_OLDEST(self):
        ds = self._many(dp.STALE_RETRY_MAX_CLOSE + 3)
        closed = _run(_client(), _rows(*ds))
        assert closed == sorted(ds)[: dp.STALE_RETRY_MAX_CLOSE], (
            "a partial pass must free the escrow locked longest"
        )


class TestAgeFloorBoundary:
    @pytest.mark.parametrize(
        ("offset", "should_close"),
        [(+60, True), (+1, True), (0, True), (-1, False), (-60, False)],
    )
    def test_the_floor_constrains_at_the_boundary(self, offset, should_close):
        """THE FLOOR IS INCLUSIVE: `age >= floor` is eligible, which is the
        natural reading of "at least 15 minutes old". Pinned so the choice stays
        deliberate rather than incidental to a `<` someone later tidies.

        ⚠ Unlike the cap, this boundary is NOT where the safety lives. The
        hazard is a concurrent run's in-flight deployment, which is seconds to
        low minutes old — orders of magnitude below the floor, so either side of
        the knife-edge is safe. The cap's boundary IS its blast radius; this
        one is a threshold with the real risk far from it. Stating the
        difference so a future reader does not tighten the wrong one.

        (`_dseq` floors to whole milliseconds, so offset 0 lands at or a hair
        above the floor — the inclusive side either way.)
        """
        d = _dseq(dp.STALE_RETRY_MIN_AGE_SECONDS + offset)
        assert bool(_run(_client(), _rows(d))) is should_close


def test_the_recovery_never_raises_into_the_callers_error_path():
    """It runs while a create has already failed; a reconcile failure must not
    replace the caller's real error with its own."""
    client = _client()
    client.account_address.side_effect = RuntimeError("console down")
    assert dp._close_stale_for_retry(client, now=NOW) == []


class TestTheRecoveryNeverEscapesIntoTheCallersError:
    """⛔ TWO FAILURE MODES, ONE COVERED. Guard 1 handled the enumeration
    RETURNING None — "could not ask the chain is not an empty account" — and
    left the case where it RAISES bare. `list_active_deployments` calls
    `rest_urls()` outside any try, and `rest_url()` raises RuntimeError on a bad
    AKASH_REST_URL, so the docstring's "never raises" was untrue for the one
    external call of five that was not wrapped.

    Distinguishing None from [] and forgetting the third state is the same shape
    as an assertion that cannot tell "not yet" from "never".

    ⚠ Severity is diagnostic, not destructive: a raise closes NOTHING, so the
    safety property held. What broke is that the exception replaces the caller's
    real failure — which is why the second test here asserts the caller's error
    SURVIVES, not merely that this function returned [].
    """

    def test_an_enumeration_that_raises_closes_nothing_and_returns_empty(self):
        client = _client()
        with patch.object(
            dp.chain, "list_active_deployments", side_effect=RuntimeError("bad AKASH_REST_URL")
        ):
            assert dp._close_stale_for_retry(client, now=NOW) == []
        client.close_deployment.assert_not_called()

    def test_the_contract_holds_WITHOUT_the_callers_wrapper(self):
        """⛔ THE HONEST VERSION OF THIS TEST.

        My first attempt asserted that `deploy()` still surfaces the create's
        error when the enumeration raises — and it PASSED with the bug present,
        because the call site wraps `_close_stale_for_retry` in
        `except Exception` and logs "Stale deployment cleanup failed". So the
        caller's error was never actually replaced, and a test asserting it
        could not fail. I had claimed a severity the code did not support.

        What was really wrong is narrower and more interesting: this function
        was safe only because something DOWNSTREAM covered it, while its own
        docstring promised the guarantee. So the assertion that means something
        is made HERE, against the function alone, with no caller in the picture —
        a contract that holds by luck is one refactor from not holding.
        """

        client = _client()
        for boom in (RuntimeError("bad AKASH_REST_URL"), ValueError("x"), OSError("y")):
            with patch.object(dp.chain, "list_active_deployments", side_effect=boom):
                assert dp._close_stale_for_retry(client, now=NOW) == [], (
                    f"{type(boom).__name__} escaped a function documented as never raising"
                )
        client.close_deployment.assert_not_called()
