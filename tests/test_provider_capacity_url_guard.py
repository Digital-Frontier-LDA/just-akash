"""`host_uri` crosses a trust boundary, and a `# noqa` cannot defend it.

⛔⛔ `provider.host_uri` is whatever a provider registered ON CHAIN, and anyone can
register a provider. `urllib` honours `file://`, so dereferencing it unchecked is an
arbitrary-file read: a provider registering `file:///etc/passwd` would have us fetch and
parse it.

⚠ WHY THIS FILE EXISTS RATHER THAN TRUSTING THE LINTER. `_get_json` carries
`# noqa: S310`, and that suppression is UNCONDITIONAL — measured: deleting the
`_require_safe_url` call leaves ruff reporting ZERO findings. So the linter cannot notice
the guard being removed. These tests can.
"""

from __future__ import annotations

import pytest

from just_akash import provider_capacity as pc


@pytest.mark.parametrize(
    "hostile",
    [
        "file:///etc/passwd",
        "file://localhost/etc/hosts",
        "ftp://example.com/x",
        "data:text/plain,pwned",
        "gopher://example.com/",
        "https:///no-host-at-all",
    ],
)
def test_get_json_refuses_to_dereference_a_hostile_url(hostile: str) -> None:
    """⛔ THE LOAD-BEARING TEST. If `_require_safe_url` is ever removed from `_get_json`,
    this fails — where the linter stays silent."""
    with pytest.raises(pc.UnsafeProviderURL):
        pc._get_json(hostile, timeout=1)


def test_an_ordinary_provider_url_is_allowed() -> None:
    """⭐ The control. A guard that refuses everything would pass the tests above and
    make every provider unreadable."""
    assert pc._require_safe_url("https://provider.example.com:8443") is not None
    assert pc._require_safe_url("http://provider.example.com:8443") is not None


def test_a_hostile_host_uri_yields_UNREADABLE_not_full(monkeypatch) -> None:
    """⚠ The refusal must not be reported as 0% free. A provider advertising a hostile
    URL loses its RANKING, not the auction — and must not sort last as though measured
    and full."""
    monkeypatch.setattr(pc, "provider_host_uri", lambda a, timeout=12: "file:///etc/passwd")
    cap = pc.capacity_for("akash1hostile")
    assert cap.available_fraction() is None


def test_a_provider_with_no_host_uri_is_unreadable_not_full(monkeypatch) -> None:
    monkeypatch.setattr(pc, "provider_host_uri", lambda a, timeout=12: None)
    assert pc.capacity_for("akash1silent").available_fraction() is None


def test_every_requested_address_gets_an_entry(monkeypatch) -> None:
    """⚠ "we asked and could not read" must be distinguishable from "we never asked"."""
    monkeypatch.setattr(pc, "provider_host_uri", lambda a, timeout=12: None)
    out = pc.capacity_by_provider(["akash1a", "akash1b", "akash1a"])
    assert set(out) == {"akash1a", "akash1b"}
    assert all(c.available_fraction() is None for c in out.values())
