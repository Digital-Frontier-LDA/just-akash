"""A lagging LCD must not under-report deploy credit, and a pin must mean what it says.

Both properties are load-bearing in front of every deploy: this call decides whether CI
believes the wallet is funded, and a wrong answer sends the investigation at providers
instead of at the balance.
"""

from __future__ import annotations

import pytest

from just_akash import chain


def test_an_explicitly_empty_pin_fails_fast(monkeypatch):
    """`if pinned:` treated an empty AKASH_REST_URL as "not pinned" and silently fanned
    out to the PUBLIC defaults — the opposite of what someone pinning an air-gapped or
    private LCD asked for. `is not None` defers to rest_url(), which raises."""
    monkeypatch.setenv("AKASH_REST_URL", "")
    # RuntimeError specifically, not a blind Exception: a blind assert would pass on a
    # typo in this very test, which is the same "check that cannot fail" shape the rest
    # of this module guards against.
    with pytest.raises(RuntimeError):
        chain.rest_urls()


def test_an_unset_pin_still_fans_out(monkeypatch):
    monkeypatch.delenv("AKASH_REST_URL", raising=False)
    assert len(chain.rest_urls()) > 1


def test_the_fallbacks_are_queried_CONCURRENTLY(monkeypatch):
    """Sequentially, three dead endpoints at the 15s timeout block ~45s — and this call
    sits in front of every deploy, so a slow reading looks like a hung CI job."""
    import threading
    import time as _t

    live = {"n": 0, "max": 0}
    lock = threading.Lock()

    def slow(path, timeout=15, base=None):
        with lock:
            live["n"] += 1
            live["max"] = max(live["max"], live["n"])
        _t.sleep(0.15)
        with lock:
            live["n"] -= 1
        return {"grants": []}

    monkeypatch.delenv("AKASH_REST_URL", raising=False)
    monkeypatch.setattr(chain, "_lcd_get", slow)
    chain.deploy_credit("akash1x")
    assert live["max"] > 1, "endpoints were queried one at a time"
