"""A Console count that settles is waited for, then still refused if it never does (#281).

Bid probe, 2026-11-19, "Reap leaked probe orders": `list_deployments` raised on a single
page reporting ONE more deployment than it returned, identical on all 3 passes ~2 s apart:

    run 44521298699 12:16Z  13 rows, total=14
    run 44584558649 18:14Z   8 rows, total=9
    run 44615373828 21:13Z  19 rows, total=20

Each right after the probe's own create/close cycle. The guard must keep failing closed on a
gap that persists, but wait long enough (2 s, then ~12 s) for one that settles. Every pass
logs its row count and total, so a later run shows which of the two it was.
"""

from __future__ import annotations

import pytest

import just_akash.api as api_mod
from just_akash.api import AkashConsoleAPI


def _page(n_rows: int, total: int) -> dict:
    return {
        "data": {
            "deployments": [
                {"dseq": str(i), "deployment": {"state": "active"}} for i in range(n_rows)
            ],
            "pagination": {"total": total, "skip": 0, "limit": 100, "hasMore": False},
        }
    }


class _Console:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = 0

    def __call__(self, method, path, *a, **kw):
        assert method == "GET"
        page = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        return page


@pytest.fixture
def sleeps(monkeypatch):
    seen: list[float] = []
    monkeypatch.setattr(api_mod.time, "sleep", seen.append)
    return seen


def _client(fake, monkeypatch):
    c = AkashConsoleAPI("key")
    monkeypatch.setattr(c, "_request", fake)
    return c


def test_the_pause_between_inconsistent_passes_escalates(monkeypatch, sleeps):
    fake = _Console([_page(19, 20)])
    with pytest.raises(RuntimeError, match="inconsistent across 3 passes"):
        _client(fake, monkeypatch).list_deployments()
    assert fake.calls == AkashConsoleAPI.LIST_ATTEMPTS == 3
    assert sleeps == [2.0, 12.0]


def test_a_count_that_settles_within_the_backoff_is_accepted(monkeypatch, sleeps, capsys):
    fake = _Console([_page(19, 20), _page(19, 20), _page(19, 19)])
    out = _client(fake, monkeypatch).list_deployments()
    assert len(out) == 19
    assert sleeps == [2.0, 12.0]
    err = capsys.readouterr().err
    assert "consistent on pass 3/3 after 2 inconsistent: rows=19 total=19" in err


def test_every_pass_logs_its_rows_and_total(monkeypatch, sleeps, capsys):
    fake = _Console([_page(8, 9)])
    with pytest.raises(RuntimeError):
        _client(fake, monkeypatch).list_deployments()
    err = capsys.readouterr().err
    assert "pass 1/3: rows=8 total=9" in err
    assert "pass 2/3: rows=8 total=9" in err
    # The raising pass is reported in the exception itself.
    assert "pass 3/3" not in err


def test_a_consistent_first_pass_never_waits_or_logs(monkeypatch, sleeps, capsys):
    """THE CONTROL: the normal listing is one call, no pause, no noise."""
    fake = _Console([_page(5, 5)])
    assert len(_client(fake, monkeypatch).list_deployments()) == 5
    assert fake.calls == 1
    assert sleeps == []
    assert capsys.readouterr().err == ""
