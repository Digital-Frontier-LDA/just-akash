"""Find deployments holding escrow whose order never acquired a lease.

⛔ THE POLICY ALREADY EXISTED AND NOTHING CALLED IT. `akash_lease_core.orders` has
shipped `evaluate_order` — seven outcomes, a derived age floor, prefix and owner
exclusions, protected dseqs — with a full test suite, and a grep across
DigitalFrontier-infra, just-akash and akash-github-runner found ZERO consumers. A tested
rule that no code path reaches is the shape this fleet keeps rediscovering: merged is
not invoked. This module is the adapter, not a second policy — every verdict below is
`evaluate_order`'s.

⛔ AGE IS THE WHOLE DIFFERENCE BETWEEN A LEAK AND AN AUCTION. Measured 2026-08-25
against the live chain: three deployments matched "active + open order + no lease" and
ALL THREE were 1.1–2.9 minutes old — bids were still arriving. An earlier version of
this audit reported exactly that shape as five leaks and was deleted for it. The floor
that separates them is `DEFAULT_MIN_AGE_SECONDS` (900s), derived in akash-lease-core
from the 450s bid window rather than chosen; past it, no new bid can arrive and any
prior bid has expired, so the order is dead rather than pending.

⚠ REPORTING ONLY. Nothing here closes anything. `evaluate_order` returns CLOSEABLE as a
CANDIDATE, and this module surfaces candidates; a close is a separate, deliberate act
with its own confirmation step (`confirm_close`).
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable, Iterable
from typing import Any

from akash_lease_core.orders import (
    LeaseEvidence,
    OrderDecision,
    OrderObservation,
    OrderPolicy,
    OrderStatus,
    evaluate_order,
)

from . import chain

__all__ = ["audit_owner", "build_observations", "fetch_owner_state"]

_SECONDS_PER_BLOCK = 6.0


def _paged(fetch: Callable[[str], dict], path: str, key: str) -> list[dict]:
    """Walk a Cosmos list endpoint to exhaustion.

    ⛔ `pagination.total` ECHOES THE LIMIT on this API — it is not a count. Measured:
    asking for 1000 rows reports `total: 1000` whether 1000 or 17391 exist. Reading it
    as a total silently truncates the population, and a truncated population is exactly
    how an audit reports a clean fleet it never looked at.
    """
    out: list[dict] = []
    next_key: str | None = None
    while True:
        sep = "&" if "?" in path else "?"
        page = f"{path}{sep}pagination.limit=1000"
        if next_key:
            # ⛔ QUOTE IT. The key is base64 and routinely contains '+' and '=';
            #   interpolated raw, the server decodes '+' as a space and answers
            #   HTTP 400. Measured on the first live run of this function.
            page = f"{page}&pagination.key={urllib.parse.quote(next_key, safe='')}"
        data = fetch(page)
        rows = data.get(key)
        if not isinstance(rows, list) or not rows:
            return out
        out.extend(r for r in rows if isinstance(r, dict))
        next_key = (data.get("pagination") or {}).get("next_key")
        if not next_key:
            return out


def fetch_owner_state(owner: str, fetch: Callable[[str], dict] | None = None) -> dict[str, Any]:
    """Everything the decision needs, in three reads. Transport is injectable so the
    decision can be tested without a chain."""
    get = fetch or (lambda p: chain._lcd_get(p, timeout=45))
    deployments = _paged(
        get,
        f"{chain._DEPLOYMENT_API}/deployments/list?filters.owner={owner}&filters.state=active",
        "deployments",
    )
    orders = _paged(get, f"{chain._MARKET_API}/orders/list?filters.owner={owner}", "orders")
    leases = _paged(get, f"{chain._MARKET_API}/leases/list?filters.owner={owner}", "leases")
    return {"deployments": deployments, "orders": orders, "leases": leases}


def build_observations(
    owner: str, state: dict[str, Any], height: int | None
) -> list[OrderObservation]:
    """One observation per ACTIVE deployment. Escrow is what we care about, and only an
    active deployment holds it — an open order under a closed deployment costs nothing.
    """
    # Count leases PER DSEQ. `lease_count` is what separates HAS_LEASE from CLOSEABLE,
    # so a set-membership test would lose the multiplicity the policy asks for.
    leases_by_dseq: dict[str, int] = {}
    for entry in state["leases"]:
        body = entry.get("lease")
        if not isinstance(body, dict):
            continue
        dseq = str((body.get("id") or {}).get("dseq"))
        leases_by_dseq[dseq] = leases_by_dseq.get(dseq, 0) + 1

    observations: list[OrderObservation] = []
    for dep in state["deployments"]:
        body = dep.get("deployment") or {}
        dseq = str((body.get("id") or {}).get("dseq"))
        created = body.get("created_at")
        # ⚠ An unreadable creation height yields age_seconds=None, NOT 0. The policy
        #   treats an unknown age as not-yet-judgeable; a 0 would read as "ancient".
        # ⛔ AN UNREADABLE CLOCK MAKES AGES UNKNOWN, NOT ANCIENT — and not young
        #   either. A sentinel height (say -1) would yield a negative age and the
        #   policy would answer TOO_YOUNG: fail-safe, but a lie about what we know.
        #   None yields UNDETERMINED, which is what is actually true.
        if height is None or created is None:
            age = None
        else:
            try:
                age = (height - int(created)) * _SECONDS_PER_BLOCK
            except (TypeError, ValueError):
                age = None
        groups = [g for g in (dep.get("groups") or []) if isinstance(g, dict)]
        names = [str((g.get("group_spec") or {}).get("name") or "") for g in groups]
        observations.append(
            OrderObservation(
                dseq=dseq,
                owner=owner,
                deployment_state=str(body.get("state") or "") or None,
                lease_count=leases_by_dseq.get(dseq, 0),
                # Read straight from the chain's own lease list — the strongest of the
                # three evidences the policy accepts, and the only one that may decide
                # a close on its own.
                lease_evidence=LeaseEvidence.CHAIN,
                age_seconds=age,
                group_states=tuple(str(g.get("state") or "") for g in groups) or None,
                name=names[0] if names else "",
            )
        )
    return observations


def audit_owner(
    owner: str,
    *,
    policy: OrderPolicy | None = None,
    height: int | None = None,
    fetch: Callable[[str], dict] | None = None,
) -> list[OrderDecision]:
    """Every active deployment for ``owner``, classified by akash-lease-core."""
    state = fetch_owner_state(owner, fetch=fetch)
    if height is None:
        height = chain.latest_height()
    observations = build_observations(owner, state, height)
    return [evaluate_order(o, policy) for o in observations]


def summarise(decisions: Iterable[OrderDecision]) -> dict[str, int]:
    """Counts by status, SEEDED FROM THE ENUM so a zero is reported as a zero.

    ⛔ THIS DOCSTRING USED TO BE FALSE. It promised "every status present, so a zero is
    visible as a zero" over a plain Counter, which only ever contains the statuses that
    OCCURRED. A status with no members was ABSENT from the JSON, not zero.

    MEASURED 2026-08-29, unleased-order-audit run 33239504420 — three owners, and the
    shape of each answer differs for reasons a reader cannot see:

        akash1cklqag…  {"has_lease": 6, "excluded": 14}
        akash1n4uut3…  {"excluded": 36}          <- has_lease is 0, so the key vanished
        akash14n4rkm…  {}                        <- no decisions at all, or no read at all

    The second is indistinguishable from "this owner has no leases tracked" and the third
    from a failed enumeration, while the run reported "0 unreadable".

    ⚠ AND THE TEXT PATH WAS ALREADY RIGHT, which is what makes this a drift rather than an
    oversight. `cli.py` prints a hardcoded list of all eight statuses under the comment
    "Print EVERY status, including the zeros." So the human-readable output carried the
    invariant and the machine-readable output — the one the workflow consumes with --json —
    did not. Two spellings of one promise, and the automation got the lossy one.

    Seeding from `OrderStatus` means the enum is the single authority. A new status appears
    in every consumer for free; none of them can silently omit one.
    """
    counts: dict[str, int] = {s.value: 0 for s in OrderStatus}
    for d in decisions:
        counts[d.status.value] = counts.get(d.status.value, 0) + 1
    return counts
