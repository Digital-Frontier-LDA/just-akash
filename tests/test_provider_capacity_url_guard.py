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


def test_an_ordinary_provider_url_is_allowed(monkeypatch) -> None:
    """⭐ The control. A guard that refuses everything would pass the tests above and
    make every provider unreadable.

    ⚠ Resolution is STUBBED to a public address. The guard now resolves the hostname to
    prove it is not loopback/private/link-local, so a placeholder name that does not
    resolve is (correctly) refused — and a test that reached real DNS would be measuring
    the network rather than the guard.
    """
    monkeypatch.setattr(
        pc.socket, "getaddrinfo", lambda h, p, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    assert pc._require_safe_url("https://provider.example.com:8443") is not None
    assert pc._require_safe_url("http://provider.example.com:8443") is not None


def test_an_unresolvable_host_is_refused_rather_than_assumed_public(monkeypatch) -> None:
    """We cannot prove an unresolvable name is public, so we do not dereference it.

    The caller turns this into UNREADABLE — never 0.0 — so a DNS outage degrades the
    fleet to "cannot say", which is the honest direction."""
    import socket as _s

    def _boom(*a, **k):
        raise _s.gaierror("no such host")

    monkeypatch.setattr(pc.socket, "getaddrinfo", _boom)
    with pytest.raises(pc.UnsafeProviderURL):
        pc._require_safe_url("https://nowhere.invalid:8443")


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


class TestNonPublicTargetsAreRefused:
    """⛔ A HOSTNAME IS NOT PROOF OF A PUBLIC ENDPOINT (CodeRabbit, PR #211).

    The scheme allowlist stops `file://` and stops nothing else. The URL is read from
    the PROVIDER'S OWN on-chain record, so any bidder chooses it — and this code runs
    inside CI, next to the metadata service and the cluster network. That is a
    server-side request forgery with an attacker-controlled target.
    """

    import pytest as _pytest

    @_pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:9090/status",  # loopback
            "http://localhost/status",  # loopback by name
            "http://10.0.0.5/status",  # RFC1918
            "http://192.168.1.1/status",  # RFC1918
            "http://169.254.169.254/latest/",  # cloud metadata — the prize
            "http://[::1]:8443/status",  # loopback v6
            "http://0.0.0.0/status",  # unspecified
        ],
    )
    def test_a_provider_cannot_point_us_at_our_own_network(self, url):
        from just_akash.provider_capacity import UnsafeProviderURL, _require_safe_url

        with self._pytest.raises(UnsafeProviderURL):
            _require_safe_url(url)

    def test_a_genuinely_public_url_is_still_allowed(self, monkeypatch):
        """The control. A guard that refuses everything would pass every test above
        while making the feature useless. Resolution stubbed — see the note on
        `test_an_ordinary_provider_url_is_allowed`."""
        from just_akash import provider_capacity as _pc

        monkeypatch.setattr(
            _pc.socket, "getaddrinfo", lambda h, p, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
        )
        assert _pc._require_safe_url("https://provider.akash.pro:8443/status")

    def test_every_resolved_address_is_checked_not_just_the_first(self):
        """A name can map to several addresses; checking only one is bypassed by
        ordering. Asserted by making resolution return public THEN loopback."""
        import socket

        from just_akash import provider_capacity as pc

        real = socket.getaddrinfo
        try:
            socket.getaddrinfo = lambda h, p, *a, **k: [
                (2, 1, 6, "", ("93.184.216.34", 0)),
                (2, 1, 6, "", ("127.0.0.1", 0)),
            ]
            assert pc._is_public_host("mixed.example") is False
        finally:
            socket.getaddrinfo = real
