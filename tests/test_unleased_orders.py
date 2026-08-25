"""An order that never got a lease, on a deployment still holding escrow.

⛔ THE POLICY EXISTED AND NOTHING CALLED IT. `akash_lease_core.orders.evaluate_order`
ships seven outcomes, a derived age floor and prefix/owner exclusions, with its own test
suite — and a grep across DigitalFrontier-infra, just-akash and akash-github-runner
found ZERO consumers. These tests cover the ADAPTER; the verdicts are the library's.

⛔ AGE IS THE WHOLE DIFFERENCE BETWEEN A LEAK AND A LIVE AUCTION. Measured 2026-08-25
against the live chain: three deployments matched "active + open order + no lease" and
all three were 1.1–2.9 minutes old — bids were still arriving. A previous version of
this audit reported that exact shape as five leaks and was deleted for it. That case is
`test_a_young_unleased_deployment_is_an_auction_not_a_leak`, with the measured ages.

★ A KNOWN-POSITIVE RUNS THROUGH THE SAME HARNESS. A monitor that reports zero is
indistinguishable from a monitor that cannot report anything, so one fixture MUST come
back CLOSEABLE. Without it, every assertion here would still pass if the adapter
returned an empty list.
"""

from __future__ import annotations

import pytest
from akash_lease_core.orders import OrderPolicy, OrderStatus

from just_akash import unleased_orders as uo

OWNER = "akash1owner"
HEIGHT = 1_000_000
BLOCK = 6.0


def _height_for_age(seconds: float) -> int:
    """created_at such that the deployment is `seconds` old at HEIGHT."""
    return HEIGHT - int(seconds / BLOCK)


def _state(
    *,
    dseq="1",
    age_s=10_000.0,
    name="dfci-infra-app",
    leases=0,
    dep_state="active",
    group_state="open",
):
    return {
        "deployments": [
            {
                "deployment": {
                    "id": {"dseq": dseq},
                    "state": dep_state,
                    "created_at": str(_height_for_age(age_s)),
                },
                "groups": [{"state": group_state, "group_spec": {"name": name}}],
            }
        ],
        "orders": [{"id": {"dseq": dseq}, "state": "open"}],
        "leases": [{"lease": {"id": {"dseq": dseq}}} for _ in range(leases)],
    }


def _decide(state, policy=None, height=HEIGHT):
    obs = uo.build_observations(OWNER, state, height)
    from akash_lease_core.orders import evaluate_order

    return [evaluate_order(o, policy) for o in obs]


class TestTheDiscriminator:
    def test_an_old_unleased_active_deployment_is_closeable(self):
        """★ THE KNOWN-POSITIVE. If this stops firing, every zero below is meaningless."""
        d = _decide(_state(age_s=10_000.0))[0]
        assert d.status is OrderStatus.CLOSEABLE, d

    @pytest.mark.parametrize("age_s", [66.0, 84.0, 174.0])  # the measured 1.1 / 1.4 / 2.9 min
    def test_a_young_unleased_deployment_is_an_auction_not_a_leak(self, age_s):
        """⛔ THE FALSE POSITIVE THAT DELETED THE LAST VERSION OF THIS AUDIT."""
        d = _decide(_state(age_s=age_s))[0]
        assert d.status is OrderStatus.TOO_YOUNG, f"age={age_s}s reported as {d.status}"

    def test_a_leased_deployment_is_not_a_leak(self):
        d = _decide(_state(leases=1))[0]
        assert d.status is OrderStatus.HAS_LEASE


class TestTheProtections:
    def test_just_akash_is_excluded_by_prefix(self):
        """⛔ KNOWN-NEGATIVE. just-akash canaries must never be reported closeable —
        one was closed four times, destroying a 200 GiB volume each time."""
        d = _decide(_state(name="just-akash-canary.abc", age_s=10_000.0))[0]
        assert d.status is OrderStatus.EXCLUDED

    def test_a_protected_dseq_is_never_closeable(self):
        pol = OrderPolicy(protected_dseqs=frozenset({"1"}))
        d = _decide(_state(age_s=10_000.0), policy=pol)[0]
        assert d.status is OrderStatus.PROTECTED


class TestTheClock:
    def test_an_unreadable_height_makes_age_unknown_not_ancient(self):
        """⛔ A sentinel height would yield a negative age and read as TOO_YOUNG —
        fail-safe, but a lie. None must reach the policy as an unknown age."""
        obs = uo.build_observations(OWNER, _state(age_s=10_000.0), None)
        assert obs[0].age_seconds is None

    def test_an_unreadable_created_at_is_also_unknown(self):
        s = _state()
        s["deployments"][0]["deployment"]["created_at"] = "not-a-height"
        assert uo.build_observations(OWNER, s, HEIGHT)[0].age_seconds is None


class TestPagination:
    def test_every_page_is_walked_not_just_the_first(self):
        """⛔ `pagination.total` ECHOES THE LIMIT on this API. Reading it as a count
        truncates the population, and a truncated population is how an audit reports a
        clean fleet it never looked at."""
        pages = [
            {"orders": [{"id": {"dseq": "1"}}], "pagination": {"next_key": "a+b/c="}},
            {"orders": [{"id": {"dseq": "2"}}], "pagination": {"next_key": None}},
        ]
        seen: list[str] = []

        def fetch(path):
            seen.append(path)
            return pages[len(seen) - 1]

        rows = uo._paged(fetch, "/x/orders/list?filters.owner=o", "orders")
        assert len(rows) == 2, "stopped after the first page"
        assert "pagination.key=a%2Bb%2Fc%3D" in seen[1], (
            f"page key not URL-encoded — '+' decodes to a space and the API 400s: {seen[1]}"
        )

    def test_an_empty_first_page_is_not_an_error(self):
        assert uo._paged(lambda p: {"orders": []}, "/x", "orders") == []
