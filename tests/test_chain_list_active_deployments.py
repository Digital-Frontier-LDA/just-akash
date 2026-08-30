"""`list_active_deployments` must paginate, fall back, and never confuse "empty" with "unknown".

⛔ THE DEFECT IT REPLACES. `AkashConsoleAPI.list_deployments` sends `GET /v1/deployments` and
relies on the API key to scope the response server-side. MEASURED 2026-08-30, three DISTINCT
keys for three DISTINCT accounts, same minute, byte-identical bodies (sha256[:10]=56432a8d66,
n=2) against a chain showing 23 / 33 / 0 active. Minutes later the same three keys all
returned HTTP 403. The endpoint is not a reliable per-account enumeration in either state.

The three properties below all fail SILENTLY, and a caller uses this to decide what to
DESTROY:

  1. an UNREADABLE chain must return None, never []   — "could not ask" vs "holds nothing"
  2. a truncated page must return None, never a short list — invisible deployments are not
     a smaller report, they are an unswept set
  3. a single dead endpoint must not answer for the whole chain
"""

from __future__ import annotations

import json

import pytest

from just_akash import chain

OWNER = "akash1testowner000000000000000000000000000"


def _page(n, next_key=None, start=0):
    return {
        "deployments": [{"deployment": {"id": {"owner": OWNER, "dseq": str(start + i)}}} for i in range(n)],
        "pagination": {"next_key": next_key},
    }


@pytest.fixture
def lcd(monkeypatch):
    """Stub `_lcd_get`, recording every (base, path) it is asked for."""
    calls: list[tuple[str, str]] = []

    def install(responder):
        def fake(path, timeout=15, base=None, height=None):  # noqa: ARG001
            calls.append((base or "default", path))
            r = responder(base or "default", path)
            if isinstance(r, Exception):
                raise r
            return r

        monkeypatch.setattr(chain, "_lcd_get", fake)
        return calls

    return install


def test_a_single_page_is_returned_whole(lcd):
    lcd(lambda base, path: _page(3))
    got = chain.list_active_deployments(OWNER)
    assert got is not None and len(got) == 3


def test_pages_are_followed_and_concatenated(lcd):
    """⚠ `active_deployment_count` asks for limit=1000 ONCE and would silently truncate a
    larger account. Truncation here is an unswept set, not a smaller report."""
    seq = [_page(200, next_key="k1"), _page(200, next_key="k2", start=200), _page(7, start=400)]
    box = {"i": 0}

    def responder(base, path):
        r = seq[box["i"]]
        box["i"] += 1
        return r

    calls = lcd(responder)
    got = chain.list_active_deployments(OWNER)
    assert got is not None and len(got) == 407, f"pagination lost records: {len(got or [])}"
    assert any("pagination.key=k1" in p for _, p in calls), "the cursor was not sent on page 2"


def test_an_unreadable_chain_is_None_not_empty(lcd):
    """⛔ THE LOAD-BEARING ONE. [] would tell a destroying caller the wallet is clean."""
    lcd(lambda base, path: RuntimeError("chain query failed"))
    assert chain.list_active_deployments(OWNER) is None


def test_a_genuinely_empty_owner_is_empty_not_None(lcd):
    """Anti-vacuity partner: if None were returned for both, the test above would pass while
    the function could never report a clean wallet at all."""
    lcd(lambda base, path: _page(0))
    got = chain.list_active_deployments(OWNER)
    assert got == [], f"a readable, empty owner must be [], got {got!r}"


def test_a_non_list_deployments_field_is_None(lcd):
    lcd(lambda base, path: {"deployments": "not-a-list", "pagination": None})
    assert chain.list_active_deployments(OWNER) is None


def test_a_runaway_cursor_refuses_the_partial(lcd):
    """A cursor that never terminates must not yield a partial set that looks complete."""
    lcd(lambda base, path: _page(1, next_key="always-more"))
    assert chain.list_active_deployments(OWNER) is None


def test_one_dead_endpoint_does_not_answer_for_the_chain(lcd, monkeypatch):
    monkeypatch.setattr(chain, "rest_urls", lambda: ["https://dead.example", "https://live.example"])
    calls = lcd(lambda base, path: RuntimeError("dead") if "dead" in base else _page(5))
    got = chain.list_active_deployments(OWNER)
    assert got is not None and len(got) == 5
    assert any("dead" in b for b, _ in calls), "the first endpoint was never tried"


def test_all_endpoints_dead_is_None(lcd, monkeypatch):
    monkeypatch.setattr(chain, "rest_urls", lambda: ["https://a.example", "https://b.example"])
    lcd(lambda base, path: RuntimeError("dead"))
    assert chain.list_active_deployments(OWNER) is None


def test_the_query_is_scoped_to_the_owner_and_to_active(lcd):
    """The whole point: Console could not scope by account. This must, and must say so in
    the URL rather than filtering client-side after asking for everything."""
    calls = lcd(lambda base, path: _page(1))
    chain.list_active_deployments(OWNER)
    path = calls[0][1]
    assert f"filters.owner={OWNER}" in path
    assert "filters.state=active" in path


def test_a_blank_owner_is_refused(lcd):
    """An empty owner would list the whole chain — every account's deployments, handed to a
    caller whose next action is to close them."""
    lcd(lambda base, path: _page(999))
    assert chain.list_active_deployments("") is None
