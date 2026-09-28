"""list_deployments pages with `skip` and refuses an inconsistent listing (#408).

The fake below answers `?limit=N&skip=K` the way the live Console did on 2026-09-28:
slices of one account, with `data.pagination = {total, skip, limit, hasMore}`.
"""

import re

import pytest

from just_akash.api import AkashConsoleAPI


def _row(i, state="active"):
    return {"dseq": str(i), "deployment": {"state": state}}


class FakeConsole:
    """Serves `rows` in pages. `script` overrides the response for a given call number."""

    def __init__(self, rows, *, has_more=True, total="skip+rows", script=None):
        self.rows = rows
        self.has_more = has_more
        self.total = total
        self.script = script or {}
        self.calls = []

    def __call__(self, method, path, *a, **kw):
        assert method == "GET"
        self.calls.append(path)
        if len(self.calls) in self.script:
            return self.script[len(self.calls)]
        limit = int(_q(path, "limit"))
        m = re.search(r"[?&]skip=(\d+)", path)
        skip = int(m.group(1)) if m else 0
        page = self.rows[skip : skip + limit]
        more = skip + len(page) < len(self.rows)
        total = len(self.rows) if self.total == "skip+rows" else len(page)
        return {
            "data": {
                "deployments": page,
                "pagination": {
                    "total": total,
                    "skip": skip,
                    "limit": limit,
                    "hasMore": more if self.has_more else False,
                },
            }
        }


def _client(fake, monkeypatch):
    c = AkashConsoleAPI("key")
    monkeypatch.setattr(c, "_request", fake)
    monkeypatch.setattr(c, "LIST_RETRY_SLEEP_S", 0)
    return c


def _q(path, name):
    m = re.search(rf"[?&]{name}=(\d+)", path)
    assert m is not None, f"{name} missing from {path!r}"
    return m.group(1)


def _skips(fake):
    return [int(_q(p, "skip")) for p in fake.calls]


def test_an_account_larger_than_one_page_is_listed_completely(monkeypatch):
    """THE BUG. 250 deployments used to come back as the first 100, plus a warning."""
    fake = FakeConsole([_row(i) for i in range(250)])
    out = _client(fake, monkeypatch).list_deployments(active_only=False)
    assert [d["dseq"] for d in out] == [str(i) for i in range(250)]
    # A multi-page listing is read twice and trusted only when both passes agree.
    assert _skips(fake) == [0, 100, 200, 0, 100, 200]


def test_paging_does_not_trust_hasmore(monkeypatch):
    """An older live reading had `hasMore` always false. A FULL page must still fetch
    the next one, or such a server truncates silently."""
    fake = FakeConsole([_row(i) for i in range(150)], has_more=False)
    out = _client(fake, monkeypatch).list_deployments(active_only=False)
    assert len(out) == 150
    assert _skips(fake) == [0, 100, 0, 100]


def test_exactly_one_full_page_ends_on_an_empty_page(monkeypatch):
    fake = FakeConsole([_row(i) for i in range(100)], has_more=False)
    out = _client(fake, monkeypatch).list_deployments(active_only=False)
    assert len(out) == 100
    assert _skips(fake) == [0, 100, 0, 100]


def test_a_server_whose_total_is_the_page_size_cannot_confirm_page_two(monkeypatch):
    """The older reading also had `total` = the returned page size. Past page 1 that
    cannot tell a complete list from a truncated one, so it is refused, not trusted."""
    fake = FakeConsole([_row(i) for i in range(150)], has_more=False, total="rows")
    with pytest.raises(RuntimeError, match="reports total=50"):
        _client(fake, monkeypatch).list_deployments(active_only=False)


def test_a_short_last_page_reporting_only_its_own_size_is_refused(monkeypatch):
    """Review probe: 100 rows (total=142, hasMore) then 1 row reporting total=1. The
    old `total == rows` allowance returned 101 of 142 with no error."""
    rows = [_row(i) for i in range(142)]
    short = {
        "data": {
            "deployments": rows[100:101],
            "pagination": {"total": 1, "skip": 100, "limit": 100, "hasMore": False},
        }
    }
    fake = FakeConsole(rows, script={n: short for n in (2, 4, 6)})
    with pytest.raises(RuntimeError, match="reports total=1"):
        _client(fake, monkeypatch).list_deployments(active_only=False)


def test_a_deployment_closed_between_page_reads_is_not_lost(monkeypatch):
    """Review probe: 142 rows; dseq 5 closes after page 1 is read. Every later row shifts
    up one, so live dseq 100 lands on page 1 (already read) and `total` drops to 141:
    one pass is self-consistent and silently misses dseq 100. The second, identical
    pass is what notices."""
    fake = FakeConsole([_row(i) for i in range(142)])

    def closing(method, path, *a, **kw):
        out = fake(method, path, *a, **kw)
        if len(fake.calls) == 1:
            del fake.rows[5]
        return out

    out = _client(closing, monkeypatch).list_deployments(active_only=False)
    got = [d["dseq"] for d in out]
    assert "100" in got and "5" not in got
    assert got == [str(i) for i in range(142) if i != 5]


def test_a_page_without_pagination_warns(monkeypatch, capsys):
    """Review probe: with no pagination every check is blind, so say so."""
    fake = FakeConsole([], script={1: {"data": {"deployments": []}}})
    assert _client(fake, monkeypatch).list_deployments() == []
    assert "carries no pagination" in capsys.readouterr().err


def test_a_short_first_page_is_one_request(monkeypatch):
    """THE CONTROL for request count: the common 15-42 deployment account."""
    fake = FakeConsole([_row(i) for i in range(42)])
    out = _client(fake, monkeypatch).list_deployments(active_only=False)
    assert len(out) == 42 and len(fake.calls) == 1


def test_active_only_filters_after_the_complete_listing(monkeypatch):
    rows = [_row(i, "closed" if i < 120 else "active") for i in range(130)]
    fake = FakeConsole(rows)
    out = _client(fake, monkeypatch).list_deployments(active_only=True)
    assert [d["dseq"] for d in out] == [str(i) for i in range(120, 130)]


SILENT_EMPTY = {
    "data": {
        "deployments": [],
        "pagination": {"total": 1, "skip": 0, "limit": 100, "hasMore": False},
    }
}


def test_the_measured_silent_empty_page_is_retried(monkeypatch, capsys):
    """Live 2026-09-28, twice: `?limit=100` -> 0 rows, `{"total": 1, "hasMore": false}`,
    then 42 rows on a later call. Reading it as an empty account hands two deleting
    sweepers "nothing to do"."""
    fake = FakeConsole([_row(i) for i in range(42)], script={1: SILENT_EMPTY})
    out = _client(fake, monkeypatch).list_deployments(active_only=False)
    assert len(out) == 42
    assert len(fake.calls) == 2
    assert "inconsistent" in capsys.readouterr().err


def test_a_persistent_silent_empty_page_raises(monkeypatch):
    fake = FakeConsole([], script={n: SILENT_EMPTY for n in range(1, 10)})
    c = _client(fake, monkeypatch)
    with pytest.raises(RuntimeError, match="inconsistent across 3 passes"):
        c.list_deployments()
    assert len(fake.calls) == AkashConsoleAPI.LIST_ATTEMPTS


def test_a_genuinely_empty_account_is_not_retried(monkeypatch):
    """THE CONTROL. `total=0` with 0 rows is consistent; it must stay one green call."""
    fake = FakeConsole([])
    assert _client(fake, monkeypatch).list_deployments() == []
    assert len(fake.calls) == 1


def test_an_empty_page_that_claims_more_restarts_the_listing(monkeypatch):
    rows = [_row(i) for i in range(150)]
    empty_more = {
        "data": {
            "deployments": [],
            "pagination": {"total": 150, "skip": 100, "limit": 100, "hasMore": True},
        }
    }
    fake = FakeConsole(rows, script={2: empty_more})
    out = _client(fake, monkeypatch).list_deployments(active_only=False)
    assert len(out) == 150
    assert _skips(fake) == [0, 100, 0, 100, 0, 100]


def test_a_last_page_short_of_total_restarts_the_listing(monkeypatch):
    """A last page whose `total` matches neither `skip + rows` nor `rows`: rows are
    missing somewhere, so the whole pass is repeated rather than trusted."""
    rows = [_row(i) for i in range(130)]
    short = {
        "data": {
            "deployments": rows[100:120],
            "pagination": {"total": 130, "skip": 100, "limit": 100, "hasMore": False},
        }
    }
    fake = FakeConsole(rows, script={2: short})
    out = _client(fake, monkeypatch).list_deployments(active_only=False)
    assert len(out) == 130


def test_a_server_ignoring_skip_raises_instead_of_duplicating(monkeypatch):
    """Every page repeats the first: a dseq seen twice. Never returns 200 rows of 100."""
    rows = [_row(i) for i in range(100)]
    same = {"data": {"deployments": rows, "pagination": {"total": 200, "hasMore": True}}}
    fake = FakeConsole(rows, script={n: same for n in range(1, 20)})
    with pytest.raises(RuntimeError, match="repeated"):
        _client(fake, monkeypatch).list_deployments(active_only=False)


def test_a_set_that_shifts_between_pages_is_listed_once(monkeypatch):
    """A deployment created mid-listing pushes a row from page 1 onto page 2. The
    duplicate forces a fresh pass, which sees the settled set exactly once."""
    base = [_row(i) for i in range(150)]
    shifted_page2 = {
        "data": {
            "deployments": [_row(99)] + base[100:150],
            "pagination": {"total": 151, "skip": 100, "limit": 100, "hasMore": False},
        }
    }
    fake = FakeConsole(base, script={2: shifted_page2})
    out = _client(fake, monkeypatch).list_deployments(active_only=False)
    assert [d["dseq"] for d in out] == [str(i) for i in range(150)]


def test_paging_is_capped(monkeypatch):
    """A server that always says hasMore with fresh rows must not loop forever."""

    counter = {"n": 0}

    def endless(method, path, *a, **kw):
        counter["n"] += 1
        base = counter["n"] * 1000
        return {
            "data": {
                "deployments": [_row(base + i) for i in range(100)],
                "pagination": {"total": 10**6, "hasMore": True},
            }
        }

    c = _client(endless, monkeypatch)
    with pytest.raises(RuntimeError, match="still paging"):
        c.list_deployments()
    assert counter["n"] == AkashConsoleAPI.LIST_MAX_PAGES


def test_a_full_page_without_pagination_does_not_extend_the_listing(monkeypatch):
    """CodeRabbit: continuing past a full page with no `total`/`hasMore` builds a
    multi-page listing nothing can check. Refused, then raised."""
    rows = [_row(i) for i in range(100)]
    bare = {"data": {"deployments": rows}}
    fake = FakeConsole(rows, script={n: bare for n in range(1, 10)})
    with pytest.raises(RuntimeError, match="without an integer total"):
        _client(fake, monkeypatch).list_deployments(active_only=False)


def test_a_single_page_after_a_multi_page_pass_must_match_it(monkeypatch):
    """CodeRabbit: 150 rows over two pages, then 42 on one page. The one-page pass is
    self-consistent but contradicts the pass before it, so it needs confirming too."""
    fake = FakeConsole([_row(i) for i in range(150)])

    def shrinking(method, path, *a, **kw):
        out = fake(method, path, *a, **kw)
        if len(fake.calls) == 2:
            del fake.rows[42:]
        return out

    out = _client(shrinking, monkeypatch).list_deployments(active_only=False)
    assert len(out) == 42
    assert _skips(fake) == [0, 100, 0, 0]
