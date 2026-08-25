"""Fetch per-provider free capacity, so EMPTIEST has something to rank on.

`akash_lease_core.PreferredSelection.EMPTIEST` ranks bids by `BidObservation.capacity`.
The library provides the PARSER (`from_provider_status`, sans-I/O); this module is the
FETCH the caller owns.

⚠ IT IS NOT ON THE HOT PATH BY DEFAULT. `deploy` only calls this when EMPTIEST is
explicitly requested. An HTTP round-trip per bidder inside a bid loop is exactly the
cost that kept the funding primitive off the deploy path for weeks, and paying it
unconditionally to support an opt-in mode would be the same mistake.

TWO HOPS, both public and unauthenticated:

    chain     /akash/provider/v1beta4/providers/{addr}   ->  provider.host_uri
    provider  {host_uri}/status                          ->  cluster.inventory.available

⛔⛔ `host_uri` IS ATTACKER-CONTROLLED. It is whatever a provider registered on chain, and
anyone can register a provider. `urllib` honours `file://`, so dereferencing it unchecked
is an arbitrary-file read — a provider registering `file:///etc/passwd` would have us
fetch and parse it. Flagged by a SAST bot on a sibling repo for the identical pattern,
and found in a sibling script the same day.

⚠ ALLOWLIST, NOT DENYLIST. Blocking `file://` alone leaves `ftp://`, `data:`, and
whatever a future opener adds. Only two schemes are ever correct for a provider endpoint.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from akash_lease_core import from_provider_status
from akash_lease_core.capacity import ProviderCapacity

from . import chain

_ALLOWED_SCHEMES = ("https", "http")

# ⚠ Provider endpoints commonly serve self-signed certificates; `curl -k` is what the
#   ecosystem uses. This reads PUBLIC capacity telemetry and sends NO credential, so the
#   worst a MITM achieves is a wrong ranking — which degrades placement and leaks
#   nothing. Do not copy this context into any path that carries a secret.
_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE

_HEADERS = {"User-Agent": "just-akash-capacity/1.0", "Accept": "application/json"}


class UnsafeProviderURL(ValueError):
    """A provider advertised a URL we will not dereference."""


def _is_public_host(host: str) -> bool:
    """Whether ``host`` resolves ONLY to public addresses.

    ⛔ A HOSTNAME IS NOT PROOF OF A PUBLIC ENDPOINT. The scheme allowlist stops
    `file://`, and stops nothing else: `http://127.0.0.1:9090/status`,
    `http://169.254.169.254/` (cloud metadata) and `http://10.0.0.5/` all carry a
    hostname and an allowed scheme. The URL comes from a PROVIDER'S OWN on-chain
    record, so any bidder can choose it — this is a server-side request forgery with
    an attacker-controlled target, and the target is inside our CI network.

    ⚠ Resolution happens HERE and the check covers EVERY returned address, because a
    name can map to several and a permissive check on the first would be bypassed by
    ordering. This still leaves a DNS-rebinding window between the check and the
    connect; closing that needs connect-time pinning, which urllib does not expose.
    Narrowing an attacker from "any address" to "wins a rebind race" is the reduction
    available at this layer, and saying so is better than implying it is airtight.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return False
    return True


def _require_safe_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeProviderURL(f"refusing scheme {parsed.scheme!r} (allowed: {_ALLOWED_SCHEMES})")
    if not parsed.hostname:
        raise UnsafeProviderURL("refusing a URL with no host")
    if not _is_public_host(parsed.hostname):
        raise UnsafeProviderURL(
            f"refusing non-public host {parsed.hostname!r} — a provider-advertised URL "
            "must not point at loopback, private, link-local or reserved space"
        )
    return url


def _get_json(url: str, timeout: int) -> dict:
    # ⚠ S310 is suppressed on the CONSTRUCTION, not the open — that is the line ruff
    #   flags. `_require_safe_url` allowlists {https, http} and requires a hostname
    #   BEFORE the Request exists, which is the audit S310 asks for.
    # ⛔ BUT THE noqa DOES NOT ENFORCE THAT. Measured: deleting the `_require_safe_url`
    #   call leaves ruff reporting ZERO findings, because the suppression is
    #   unconditional. A comment claiming a safety property is not a test of it — the
    #   guard is pinned by `tests/test_provider_capacity_url_guard.py`, which fails if
    #   this function ever dereferences a non-http(s) URL.
    req = urllib.request.Request(_require_safe_url(url), headers=_HEADERS)  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout, context=_TLS) as resp:  # noqa: S310
        return json.load(resp)


def provider_host_uri(address: str, timeout: int = 15) -> str | None:
    """The provider's advertised endpoint, or None if it publishes none."""
    try:
        data = chain._lcd_get(f"/akash/provider/v1beta4/providers/{address}", timeout=timeout)
    except RuntimeError:
        return None
    return ((data.get("provider")) or {}).get("host_uri")


def capacity_for(address: str, timeout: int = 12) -> ProviderCapacity:
    """One provider's free capacity. UNREADABLE on any failure — never full.

    ⛔ Every failure path returns an unreadable `ProviderCapacity`, whose
    `available_fraction()` is None. It never returns a zero fraction. `None` means "do
    not rank this provider"; `0.0` means "measured, and completely full" and would sort
    it LAST — so a provider behind a flaky endpoint would be deprioritised for being
    unreachable rather than for being busy.
    """
    uri = provider_host_uri(address, timeout=timeout)
    if not uri:
        return ProviderCapacity()
    try:
        return from_provider_status(_get_json(uri.rstrip("/") + "/status", timeout))
    except (UnsafeProviderURL, urllib.error.URLError, TimeoutError, ValueError, OSError):
        return ProviderCapacity()


def capacity_by_provider(
    addresses: list[str], *, timeout: int = 12, workers: int = 8
) -> dict[str, ProviderCapacity]:
    """Fetch capacity for several providers concurrently.

    ⚠ Every address gets an entry, including unreadable ones. Omitting a provider and
    mapping it to an unreadable capacity are equivalent to the auction core (both mean
    "unranked"), but returning the key makes the FETCH's coverage visible to the caller
    — "we asked and could not read" is different from "we never asked".
    """
    uniq = list(dict.fromkeys(a for a in addresses if a))
    if not uniq:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(uniq)))) as pool:
        results = list(pool.map(lambda a: capacity_for(a, timeout=timeout), uniq))
    return dict(zip(uniq, results, strict=True))
