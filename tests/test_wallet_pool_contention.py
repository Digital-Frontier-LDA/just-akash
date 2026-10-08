"""openmix-wxs8: OPT-IN (quiet=True / AKASH_QUIET_WALLET=1), a multi-key create picks a FUNDED
and QUIET Console wallet, or refuses. The DEFAULT stays funding-only for every caller: CI runner
pools create on these wallets every few minutes and would refuse because of their own creates.

Quiet = no deployment created within FLEET_QUIET_MINUTES (dseq is the creation epoch-ms) and no
deployment still bidding (active, no lease). Measured 2026-10-07: one funded wallet carried a
CI runner controller creating every 1-4 min, so funding-only ranking would have raced it.
"""

from unittest.mock import MagicMock

import pytest

from just_akash import wallet_pool
from just_akash.wallet_pool import (
    confirm_quiet_or_reselect,
    select_client_for_create,
    wallet_contention,
)

NOW_MS = 1_791_500_000_000
MIN = 60_000


def _row(dseq_ms: int, *, state: str = "active", provider: str | None = "akash1prov") -> dict:
    leases = [{"id": {"provider": provider}}] if provider else []
    return {"deployment": {"id": {"dseq": str(dseq_ms)}, "state": state}, "leases": leases}


def _pool(monkeypatch, wallets: dict[str, dict]):
    """wallets: key -> {"account", "credit", "rows"}; returns (factory, clients)."""
    monkeypatch.setenv("AKASH_API_KEYS", ",".join(wallets))
    monkeypatch.delenv("AKASH_API_KEY", raising=False)
    monkeypatch.delenv("AKASH_QUIET_WALLET", raising=False)
    monkeypatch.delenv("FLEET_QUIET_MINUTES", raising=False)
    clients = {}
    for key, spec in wallets.items():
        c = MagicMock(api_key=key)
        c.account_address.return_value = spec["account"]
        c.list_deployments.return_value = spec["rows"]
        clients[key] = c
    credit = {spec["account"]: spec["credit"] for spec in wallets.values()}
    return (lambda key: clients[key]), clients, (lambda account: credit[account])


def _select(factory, credit, **kw):
    """Quiet-aware unless a test says otherwise; the default path has its own tests below."""
    kw.setdefault("quiet", True)
    return select_client_for_create(
        5_000_000, client_factory=factory, credit_reader=credit, clock=lambda: NOW_MS / 1000, **kw
    )


# ── wallet_contention ──────────────────────────────────────────────────────────────────


def test_a_quiet_wallet_has_no_reasons():
    c = MagicMock()
    c.list_deployments.return_value = [_row(NOW_MS - 30 * MIN)]
    assert wallet_contention(c, now_ms=NOW_MS, quiet_minutes=10) == []
    c.list_deployments.assert_called_once_with(
        active_only=False, max_pages=wallet_pool.QUIET_LIST_MAX_PAGES
    )


def test_a_truncated_listing_is_busy_not_quiet():
    """The Console lists OLDEST-first, so a capped listing has not read the newest rows."""
    from just_akash.api import ListingTruncated

    c = MagicMock()
    c.list_deployments.side_effect = ListingTruncated("capped")
    (reason,) = wallet_contention(c, now_ms=NOW_MS, quiet_minutes=10)
    assert reason.startswith("listing truncated")


def test_a_recent_create_of_any_state_is_contention():
    c = MagicMock()
    c.list_deployments.return_value = [_row(NOW_MS - 3 * MIN, state="closed", provider=None)]
    (reason,) = wallet_contention(c, now_ms=NOW_MS, quiet_minutes=10)
    assert "recent create" in reason and "3.0m ago" in reason


def test_an_old_active_deployment_with_no_lease_is_bidding():
    c = MagicMock()
    c.list_deployments.return_value = [_row(NOW_MS - 30 * MIN, provider=None)]
    (reason,) = wallet_contention(c, now_ms=NOW_MS, quiet_minutes=10)
    assert reason.startswith("bidding") and str(NOW_MS - 30 * MIN) in reason


def test_an_unreadable_listing_is_contention_not_quiet():
    c = MagicMock(api_key="secret-key")
    c.list_deployments.side_effect = RuntimeError("401 for key secret-key")
    (reason,) = wallet_contention(c, now_ms=NOW_MS, quiet_minutes=10, keys=["secret-key"])
    assert reason.startswith("listing unavailable") and "secret-key" not in reason


# ── select_client_for_create ───────────────────────────────────────────────────────────


def test_quiet_funded_wallet_beats_a_richer_busy_one(monkeypatch):
    factory, clients, credit = _pool(
        monkeypatch,
        {
            "rich": {"account": "acc-rich", "credit": 900_000_000, "rows": [_row(NOW_MS - MIN)]},
            "calm": {"account": "acc-calm", "credit": 50_000_000, "rows": []},
        },
    )
    sel = _select(factory, credit)
    assert sel.client is clients["calm"] and sel.contention_aware is True


def test_an_unfunded_quiet_wallet_is_skipped(monkeypatch):
    factory, clients, credit = _pool(
        monkeypatch,
        {
            "empty": {"account": "acc-empty", "credit": 0, "rows": []},
            "calm": {"account": "acc-calm", "credit": 50_000_000, "rows": []},
        },
    )
    assert _select(factory, credit).client is clients["calm"]


def test_a_bidding_wallet_is_skipped(monkeypatch):
    factory, clients, credit = _pool(
        monkeypatch,
        {
            "bid": {
                "account": "acc-bid",
                "credit": 900_000_000,
                "rows": [_row(NOW_MS - 60 * MIN, provider=None)],
            },
            "calm": {"account": "acc-calm", "credit": 50_000_000, "rows": []},
        },
    )
    assert _select(factory, credit).client is clients["calm"]


def test_all_busy_refuses_with_a_reason_per_wallet(monkeypatch):
    factory, clients, credit = _pool(
        monkeypatch,
        {
            "empty": {"account": "acc-empty", "credit": 0, "rows": []},
            "busy": {"account": "acc-busy", "credit": 900_000_000, "rows": [_row(NOW_MS - MIN)]},
            "bid": {
                "account": "acc-bid",
                "credit": 900_000_000,
                "rows": [_row(NOW_MS - 60 * MIN, provider=None)],
            },
        },
    )
    with pytest.raises(RuntimeError) as err:
        _select(factory, credit)
    msg = str(err.value)
    assert "no funded AND quiet Console wallet" in msg
    assert "unfunded" in msg and "recent create" in msg and "bidding" in msg
    for key in ("empty", "busy", "bid"):
        assert f"for key {key}" not in msg
    for c in clients.values():
        c.create_deployment.assert_not_called()


def test_the_quiet_window_is_configurable(monkeypatch):
    factory, clients, credit = _pool(
        monkeypatch,
        {
            "only-funded": {
                "account": "a",
                "credit": 900_000_000,
                "rows": [_row(NOW_MS - 6 * MIN)],
            },
            "empty": {"account": "b", "credit": 0, "rows": []},
        },
    )
    monkeypatch.setenv("FLEET_QUIET_MINUTES", "5")
    assert _select(factory, credit).client is clients["only-funded"]


def test_default_with_several_keys_is_unchanged_funding_only(monkeypatch):
    """⛔ The default every existing multi-key caller (CI runner pools) gets: richest funded
    wallet, even a busy one, and no listing read at all."""
    factory, clients, credit = _pool(
        monkeypatch,
        {
            "rich": {"account": "acc-rich", "credit": 900_000_000, "rows": [_row(NOW_MS - MIN)]},
            "calm": {"account": "acc-calm", "credit": 50_000_000, "rows": []},
        },
    )
    sel = select_client_for_create(
        5_000_000, client_factory=factory, credit_reader=credit, clock=lambda: NOW_MS / 1000
    )
    assert sel.client is clients["rich"] and sel.contention_aware is False
    assert "+quiet" not in sel.policy_version
    for c in clients.values():
        c.list_deployments.assert_not_called()


def test_env_opt_in_gives_quiet_selection(monkeypatch):
    factory, clients, credit = _pool(
        monkeypatch,
        {
            "rich": {"account": "acc-rich", "credit": 900_000_000, "rows": [_row(NOW_MS - MIN)]},
            "calm": {"account": "acc-calm", "credit": 50_000_000, "rows": []},
        },
    )
    monkeypatch.setenv("AKASH_QUIET_WALLET", "1")
    sel = select_client_for_create(
        5_000_000, client_factory=factory, credit_reader=credit, clock=lambda: NOW_MS / 1000
    )
    assert sel.client is clients["calm"] and sel.contention_aware is True


def test_explicit_quiet_false_overrides_the_env(monkeypatch):
    factory, clients, credit = _pool(
        monkeypatch,
        {
            "rich": {"account": "acc-rich", "credit": 900_000_000, "rows": [_row(NOW_MS - MIN)]},
            "calm": {"account": "acc-calm", "credit": 50_000_000, "rows": []},
        },
    )
    monkeypatch.setenv("AKASH_QUIET_WALLET", "1")
    assert _select(factory, credit, quiet=False).client is clients["rich"]


def test_single_key_is_unchanged_and_never_reads_contention(monkeypatch):
    monkeypatch.delenv("AKASH_API_KEYS", raising=False)
    monkeypatch.setenv("AKASH_API_KEY", "only")
    factory = MagicMock()
    sel = select_client_for_create(5_000_000, client_factory=factory, credit_reader=MagicMock())
    factory.return_value.list_deployments.assert_not_called()
    assert sel.contention_aware is False


# ── confirm_quiet_or_reselect (the pre-create re-check) ────────────────────────────────


def test_recheck_still_quiet_keeps_the_selection(monkeypatch):
    factory, clients, credit = _pool(
        monkeypatch,
        {
            "a": {"account": "acc-a", "credit": 90_000_000, "rows": []},
            "b": {"account": "acc-b", "credit": 10_000_000, "rows": []},
        },
    )
    sel = _select(factory, credit)
    again = confirm_quiet_or_reselect(
        sel, 5_000_000, client_factory=factory, credit_reader=credit, clock=lambda: NOW_MS / 1000
    )
    assert again is sel


def test_recheck_that_flips_busy_reranks_once(monkeypatch):
    factory, clients, credit = _pool(
        monkeypatch,
        {
            "a": {"account": "acc-a", "credit": 90_000_000, "rows": []},
            "b": {"account": "acc-b", "credit": 10_000_000, "rows": []},
        },
    )
    sel = _select(factory, credit)
    assert sel.client is clients["a"]
    clients["a"].list_deployments.return_value = [_row(NOW_MS - 10_000)]  # a CI create landed
    again = confirm_quiet_or_reselect(
        sel, 5_000_000, client_factory=factory, credit_reader=credit, clock=lambda: NOW_MS / 1000
    )
    assert again.client is clients["b"]


def test_recheck_with_nothing_left_refuses(monkeypatch):
    factory, clients, credit = _pool(
        monkeypatch,
        {
            "a": {"account": "acc-a", "credit": 90_000_000, "rows": []},
            "b": {"account": "acc-b", "credit": 0, "rows": []},
        },
    )
    sel = _select(factory, credit)
    clients["a"].list_deployments.return_value = [_row(NOW_MS - 10_000)]
    with pytest.raises(RuntimeError, match="no funded AND quiet Console wallet"):
        confirm_quiet_or_reselect(
            sel,
            5_000_000,
            client_factory=factory,
            credit_reader=credit,
            clock=lambda: NOW_MS / 1000,
        )


def test_recheck_is_a_no_op_when_selection_was_not_contention_aware(monkeypatch):
    sel = MagicMock(contention_aware=False)
    assert confirm_quiet_or_reselect(sel, 1) is sel
    assert wallet_pool  # module import used by monkeypatching tests elsewhere


@pytest.mark.parametrize("raw", ["abc", "-1", "nan", "inf"])
def test_a_bad_quiet_window_is_a_runtime_error(monkeypatch, raw):
    """The CLI turns RuntimeError into a clean `Error:` line; a ValueError would be a traceback."""
    factory, clients, credit = _pool(
        monkeypatch,
        {
            "a": {"account": "acc-a", "credit": 90_000_000, "rows": []},
            "b": {"account": "acc-b", "credit": 90_000_000, "rows": []},
        },
    )
    monkeypatch.setenv("FLEET_QUIET_MINUTES", raw)
    with pytest.raises(RuntimeError, match="FLEET_QUIET_MINUTES"):
        _select(factory, credit)


# ── review of f8666fcd (openmix-wxs8) ──────────────────────────────────────────────────────


def _bare(dseq: str, *, state: str = "active", provider: str | None = "akash1prov") -> dict:
    leases = [{"id": {"provider": provider}}] if provider else []
    return {"deployment": {"id": {"dseq": dseq}, "state": state}, "leases": leases}


def test_the_window_boundary_is_exclusive():
    """Exactly FLEET_QUIET_MINUTES old is outside the window; one ms younger is inside."""
    c = MagicMock()
    c.list_deployments.return_value = [_row(NOW_MS - 10 * MIN)]
    assert wallet_contention(c, now_ms=NOW_MS, quiet_minutes=10) == []
    c.list_deployments.return_value = [_row(NOW_MS - 10 * MIN + 1)]
    (reason,) = wallet_contention(c, now_ms=NOW_MS, quiet_minutes=10)
    assert reason.startswith("recent create")


@pytest.mark.parametrize("dseq", ["abc", "²", "12a", "", "-5", "0"])
def test_an_unparseable_dseq_is_busy(dseq):
    """Non-numeric, Unicode-digit (str.isdigit accepts '²'), signed or zero: never quiet, never
    an uncaught ValueError."""
    c = MagicMock()
    c.list_deployments.return_value = [_bare(dseq)]
    reasons = wallet_contention(c, now_ms=NOW_MS, quiet_minutes=10)
    assert any("unparseable dseq" in r for r in reasons)


def test_an_unparseable_dseq_still_gets_the_bidding_check():
    c = MagicMock()
    c.list_deployments.return_value = [_bare("abc", provider=None)]
    reasons = wallet_contention(c, now_ms=NOW_MS, quiet_minutes=10)
    assert any("unparseable dseq" in r for r in reasons)
    assert any(r.startswith("bidding") for r in reasons)


def test_a_block_height_dseq_is_aged_against_the_chain_height():
    """The Akash CLI default dseq is a block height (~2.5e7), not epoch-ms: read as ms it is
    always ancient, which fails OPEN. ~6 s/block."""
    c = MagicMock()
    head = 25_000_000
    c.list_deployments.return_value = [_bare(str(head - 50))]  # 50 blocks ~ 5 min
    (reason,) = wallet_contention(c, now_ms=NOW_MS, quiet_minutes=10, height_reader=lambda: head)
    assert reason.startswith("recent create") and "5.0m ago" in reason
    c.list_deployments.return_value = [_bare(str(head - 200))]  # ~20 min
    assert wallet_contention(c, now_ms=NOW_MS, quiet_minutes=10, height_reader=lambda: head) == []


def test_a_block_height_dseq_with_no_readable_height_is_busy():
    c = MagicMock()
    c.list_deployments.return_value = [_bare("25000000")]
    (reason,) = wallet_contention(c, now_ms=NOW_MS, quiet_minutes=10, height_reader=lambda: None)
    assert "age unknown" in reason


def test_the_height_is_read_once_and_only_when_needed():
    reads = []
    c = MagicMock()
    c.list_deployments.return_value = [_row(NOW_MS - 30 * MIN)]
    wallet_contention(c, now_ms=NOW_MS, quiet_minutes=10, height_reader=lambda: reads.append(1))
    assert reads == []
    c.list_deployments.return_value = [_bare("24999000"), _bare("24999001")]
    wallet_contention(
        c, now_ms=NOW_MS, quiet_minutes=10, height_reader=lambda: reads.append(1) or 25_000_000
    )
    assert reads == [1]


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("1", True),
        ("TRUE", True),
        ("yes", True),
        ("On", True),
        ("0", False),
        ("false", False),
        ("NO", False),
        ("off", False),
        ("", False),
    ],
)
def test_the_quiet_env_accepts_the_usual_spellings(monkeypatch, raw, expect):
    monkeypatch.setenv("AKASH_QUIET_WALLET", raw)
    assert wallet_pool._quiet_from_env() is expect


def test_an_unrecognised_quiet_env_value_raises(monkeypatch):
    monkeypatch.setenv("AKASH_QUIET_WALLET", "maybe")
    with pytest.raises(RuntimeError, match="AKASH_QUIET_WALLET"):
        wallet_pool._quiet_from_env()


def test_contention_through_the_real_console_paging(monkeypatch):
    """wallet_contention against the real AkashConsoleAPI listing code (pagination, envelope,
    oldest-first order), with HTTP stubbed at AkashConsoleAPI._request — not a MagicMock
    client. The newest row is on the second page."""
    from just_akash.api import AkashConsoleAPI

    rows = [
        {
            "deployment": {"id": {"dseq": str(NOW_MS - (200 - i) * MIN)}, "state": "active"},
            "leases": [{"id": {"provider": "akash1prov"}}],
        }
        for i in range(150)
    ]
    rows[-1]["deployment"]["id"]["dseq"] = str(NOW_MS - 2 * MIN)

    def http(method, path, *a, **kw):
        import re

        skip_m = re.search(r"skip=(\d+)", path)
        limit_m = re.search(r"limit=(\d+)", path)
        assert skip_m and limit_m, path
        skip, limit = int(skip_m.group(1)), int(limit_m.group(1))
        page = rows[skip : skip + limit]
        return {
            "data": {
                "deployments": page,
                "pagination": {
                    "total": len(rows),
                    "skip": skip,
                    "limit": limit,
                    "hasMore": skip + len(page) < len(rows),
                },
            }
        }

    c = AkashConsoleAPI("key")
    monkeypatch.setattr(c, "_request", http)
    monkeypatch.setattr(c, "LIST_RETRY_SLEEP_S", 0)
    (reason,) = wallet_contention(c, now_ms=NOW_MS, quiet_minutes=10)
    assert reason.startswith("recent create") and "2.0m ago" in reason
