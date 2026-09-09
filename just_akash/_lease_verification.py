"""Owner-scoped chain lease verification across two independent endpoints.

Closure proof requires two distinct chain endpoints to agree on the complete
owner/dseq/gseq/oseq/bseq/provider lease map, with all leases terminal. Both sources
must also positively identify the deployment and its escrow as closed. Closed
leases alone can coexist with an active deployment and retained escrow.
A single Console API read is not proof — the Console API and
the chain RPC are different channels that can disagree (the failure that
closed #952 reproduced 2026-09-09: Console GET 404 while the chain still
showed state=active). Destroy-side text matching ("Deployment closed",
"already closed") is also not proof — those strings are emitted on
sub-paths that may not have actually torn down the lease.

This module is the contract shared with `scripts/akash_lease_verification.py`
on the blazing side; both speak the same shape so the upstream teardown and
the local closer can agree on what counts as a positive observation.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable

DEFAULT_ENDPOINTS: tuple[str, ...] = (
    "https://akash-api.polkachu.com",
    "https://rest.cosmos.directory/akash",
)
TERMINAL_STATES = frozenset({"closed", "insufficient_funds"})


def lease_snapshot(
    base: str,
    dseq: str,
    owner: str,
    get: Callable[[str], dict | None],
) -> dict[tuple[str, ...], str] | None:
    """Read every lease page from one endpoint; never return a partial population.

    Returns the COMPLETE identity-state mapping for the (owner, dseq)
    intersection, or None if any page was unreadable / structurally invalid.
    A partial read is treated as a failed read — it is the same defect as
    an empty read, just hidden inside a population that LOOKED populated.
    """
    leases: dict[tuple[str, ...], str] = {}
    cursor = ""
    visited: set[str] = set()
    for _ in range(50):
        query = {"filters.owner": owner, "filters.dseq": dseq, "pagination.limit": "200"}
        if cursor:
            query["pagination.key"] = cursor
        doc = get(f"{base}/akash/market/v1beta5/leases/list?{urllib.parse.urlencode(query)}")
        if not isinstance(doc, dict) or not isinstance(doc.get("leases"), list):
            return None
        for row in doc["leases"]:
            lease = row.get("lease") if isinstance(row, dict) else None
            if not isinstance(lease, dict) or lease.get("state") not in {
                "active",
                "closed",
                "insufficient_funds",
            }:
                return None
            identity = lease.get("id")
            if not isinstance(identity, dict):
                return None
            owner_field = identity.get("owner")
            provider_field = identity.get("provider")
            if (
                not isinstance(owner_field, str)
                or not owner_field
                or not isinstance(provider_field, str)
                or not provider_field
            ):
                return None
            numbers = []
            for field in ("dseq", "gseq", "oseq", "bseq"):
                value = identity.get(field)
                if isinstance(value, bool) or not isinstance(value, (str, int)):
                    return None
                text = str(value)
                if not text.isascii() or not text.isdigit():
                    return None
                numbers.append(str(int(text)))
            if numbers[0] != str(int(dseq)) or owner_field != owner:
                return None
            key = (owner_field, *numbers, provider_field)
            if key in leases:
                return None
            leases[key] = lease["state"]
        pagination = doc.get("pagination")
        if pagination is not None and not isinstance(pagination, dict):
            return None
        cursor = pagination.get("next_key") if pagination is not None else None
        if cursor in (None, ""):
            return leases or None
        if not isinstance(cursor, str) or cursor in visited:
            return None
        visited.add(cursor)
    return None


def consensus(
    dseq: str,
    owner: str,
    endpoints,
    get: Callable[[str], dict | None],
) -> tuple[dict[tuple[str, ...], str] | None, tuple[str, ...]]:
    """Return (complete agreeing lease map, sources) or (None, ()) if unverified.

    Two endpoints must return IDENTICAL complete lease maps (same keys,
    same states). Partial reads on either side are treated as unverified.
    Endpoints are deduped by HOSTNAME (case-insensitive) so the same host
    on different ports is one source. Non-HTTPS or credentials-in-URL
    endpoints are skipped.
    """
    if not isinstance(dseq, str) or not re.fullmatch(r"[0-9]{1,32}", dseq):
        raise ValueError("dseq must be 1-32 ASCII digits")
    if not isinstance(owner, str) or not re.fullmatch(r"akash1[a-z0-9]{38,58}", owner):
        raise ValueError("owner must be an Akash account address")
    seen: list[dict[tuple[str, ...], str]] = []
    sources: list[str] = []
    hostnames: set[str] = set()
    for base in endpoints:
        parsed = urllib.parse.urlsplit(base)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username
            or parsed.password
            or hostname in hostnames
        ):
            continue
        hostnames.add(hostname)
        try:
            snapshot = lease_snapshot(base.rstrip("/"), dseq, owner, get)
        except Exception:
            snapshot = None
        if snapshot is None:
            continue
        seen.append(snapshot)
        sources.append(base)
        if len(seen) == 2:
            break
    if len(seen) < 2 or seen[0] != seen[1]:
        return None, ()
    return seen[0], tuple(sources)


def deployment_closed(base: str, dseq: str, owner: str, get: Callable[[str], dict | None]) -> bool:
    """Prove deployment and escrow closure on the same source as the lease read."""
    query = urllib.parse.urlencode({"id.owner": owner, "id.dseq": dseq})
    try:
        doc = get(f"{base.rstrip('/')}/akash/deployment/v1beta4/deployments/info?{query}")
    except Exception:
        return False
    if not isinstance(doc, dict):
        return False
    deployment = doc.get("deployment")
    escrow = doc.get("escrow_account")
    if not isinstance(deployment, dict) or not isinstance(escrow, dict):
        return False
    identity = deployment.get("id")
    escrow_id = escrow.get("id")
    state = escrow.get("state")
    return (
        isinstance(identity, dict)
        and identity.get("owner") == owner
        and str(identity.get("dseq")) == dseq
        and deployment.get("state") == "closed"
        and isinstance(escrow_id, dict)
        and escrow_id.get("scope") == "deployment"
        and escrow_id.get("xid") == f"{owner}/{dseq}"
        and isinstance(state, dict)
        and state.get("owner") == owner
        and state.get("state") == "closed"
    )


def verdict(
    dseq: str,
    owner: str,
    endpoints,
    get: Callable[[str], dict | None],
    *,
    retries: int = 5,
    retry_sleep_s: float = 0.0,
) -> dict[str, object]:
    """Machine-readable verdict for the close step.

    Bounded retries cover TWO distinct chain-lag shapes:

    - consensus returns None (one or both endpoints unreadable): retry
      until retries are exhausted or both become readable.
    - consensus returns agreeing ACTIVE populations: retry with delay.
      Right after a destroy, the lease IS being closed on chain but the
      RPCs may still reflect `active` until the close-tx propagates. A
      verdict that returns immediately on active would treat the lag
      as a permanent failure and report closed=false on a lease that
      is in fact mid-close. The destroy retry loop had the same shape
      and the same fix; this is its observation-side counterpart.

    closed=true ONLY when both endpoints agree on terminal lease states
    across the complete owner-scoped identity map AND read deployment and escrow closed.
    closed=false when
    the populations still show active after all retries, when the
    populations disagreed, or when all retries were exhausted
    unreadable.
    """
    import time as _time

    last_sources: tuple[str, ...] = ()
    last_reason = "unverified"
    attempts = max(1, int(retries))
    for attempt in range(attempts):
        leases, sources = consensus(dseq, owner, endpoints, get)
        if leases is not None:
            states = set(leases.values())
            if (
                states
                and states.issubset(TERMINAL_STATES)
                and all(deployment_closed(base, dseq, owner, get) for base in sources)
            ):
                return {
                    "closed": True,
                    "sources": list(sources),
                    "reason": "agreeing terminal states",
                }
            # ⇒ Active observation. NOT terminal yet — but the
            # destroy-then-propagate lag can mean this is stale.
            # Consume the retry budget before declaring closed=false.
            last_sources = sources
            last_reason = (
                "active lease on chain"
                if not states.issubset(TERMINAL_STATES)
                else "deployment or escrow closure not verified"
            )
            if attempt + 1 < attempts and retry_sleep_s > 0:
                _time.sleep(retry_sleep_s)
            elif attempt + 1 == attempts:
                # ⇒ Out of patience — surface the active observation
                # as closed=false rather than swallowing it.
                return {
                    "closed": False,
                    "sources": list(sources),
                    "reason": last_reason,
                }
            continue
        # ⇒ consensus returned None — at least one endpoint unreadable.
        # Same retry-with-delay treatment so a transient RPC blip does
        # not become a permanent closed=false.
        last_sources = sources
        last_reason = "unverified"
        if attempt + 1 < attempts and retry_sleep_s > 0:
            _time.sleep(retry_sleep_s)
    return {
        "closed": False,
        "sources": list(last_sources),
        "reason": last_reason,
    }
