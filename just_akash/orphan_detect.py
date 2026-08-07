"""Find deployments that hold escrow and will never get a lease.

WHY NOT A TIME SERIES (the design this replaces)

The first design sampled `lease-status` on a schedule and called a deployment orphaned
once it read `lease_count == 0` across N consecutive samples. A quorum blocked it 2/2 and
every objection was verified in this repo's own source:

1. THE SOURCE SILENTLY OMITS ROWS. `list_deployments` cannot detect Console truncation —
   `api.py` records it as verified live: `?limit=1` returns `total=1, hasMore=false` while
   15+ deployments exist, so `total` is the RETURNED PAGE SIZE and `hasMore` is always
   false. A deployment omitted upstream never enters the series, and the monitor reports
   a clean fleet.

2. N-CONSECUTIVE FAILS OPEN. A dropped cron, a storage gap or a rate-limited call resets
   the counter, so a real orphan never ages into the flag. The mechanism added to avoid
   destroying live deployments was itself the false-clean vector: it reads clean exactly
   when monitoring is degraded.

3. THE CHAIN ANSWERS IT DIRECTLY. An OPEN ORDER is the only path to a future lease. So:

       active + lease_count == 0 + NO open order   -> orphan NOW
       active + lease_count == 0 + an open order   -> legitimately waiting for a bid

   That is exactly the distinction the sampling was a workaround for. No persistence, no
   counter, no storage to silently stop working.

WHAT THIS MODULE WILL NOT DO

It will not report "clean" for anything it could not read. Every failure to reach the
chain, every disagreement between endpoints, and every sign that the Console list was
truncated resolves to UNKNOWN or DEGRADED — never to "no orphans". Absence of evidence is
not evidence of absence, and this fleet has lost more time to that one confusion than to
any real outage.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .chain import _lcd_get, rest_urls

# Akash market module version. Matches the bids query already in use in
# smoke_providers.py — do not drift the two apart.
MARKET_API = "/akash/market/v1beta5"

# An order in one of these states can still become a lease. Anything else cannot.
# `open` is the live matching state; `active` appears on some node versions for an order
# that has been matched but whose lease is not yet visible — treated as live because the
# cost of waiting is a delay and the cost of being wrong is a destroyed deployment.
LIVE_ORDER_STATES = frozenset({"open", "active"})

# Independent endpoints that must AGREE before any destructive action. A single lagging
# node can report zero orders for a deployment that is matching fine.
MIN_CONFIRMATIONS = 2


class Classification(str, Enum):
    """What we know about one deployment. UNKNOWN is a first-class answer."""

    ORPHANED = "ORPHANED"  # active, escrow open, no lease, NO live order — bleeding now
    WAITING = "WAITING"  # active, no lease, but a live order exists — legitimately pending
    LEASED = "LEASED"  # has an active lease; not our problem
    # We could not read the chain, or endpoints disagreed. NEVER means "fine".
    UNKNOWN = "UNKNOWN"


@dataclass
class DeploymentVerdict:
    dseq: str
    classification: Classification
    escrow_uact: int = 0
    live_orders: int | None = None  # None == unread
    confirmations: int = 0  # how many independent endpoints agreed
    detail: str = ""

    @property
    def reapable(self) -> bool:
        """Destructive action is permitted ONLY here.

        Requires the classification AND enough independent endpoints to have agreed.
        A verdict from one endpoint is a reading, not a confirmation.
        """
        return (
            self.classification is Classification.ORPHANED
            and self.confirmations >= MIN_CONFIRMATIONS
        )


@dataclass
class FleetReport:
    verdicts: list[DeploymentVerdict] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)

    @property
    def is_degraded(self) -> bool:
        """True when the report cannot be trusted to be complete.

        Kept separate from the verdict list on purpose: a caller that only reads
        `orphaned` would otherwise treat a truncated, half-read fleet as a healthy one.
        """
        return bool(self.degraded)

    @property
    def orphaned(self) -> list[DeploymentVerdict]:
        return [v for v in self.verdicts if v.classification is Classification.ORPHANED]

    @property
    def unknown(self) -> list[DeploymentVerdict]:
        return [v for v in self.verdicts if v.classification is Classification.UNKNOWN]

    @property
    def orphaned_escrow_uact(self) -> int:
        return sum(v.escrow_uact for v in self.orphaned)

    def summary(self) -> str:
        parts = [
            f"{len(self.orphaned)} orphaned ({self.orphaned_escrow_uact / 1e6:.2f} ACT held)",
            f"{len(self.unknown)} unknown",
            f"{len(self.verdicts)} examined",
        ]
        line = "  " + ", ".join(parts)
        if self.is_degraded:
            # Loud, and phrased so nobody reads a short orphan list as good news.
            line += (
                "\n  ::warning title=Fleet report is DEGRADED — treat the counts as a FLOOR::"
                + "; ".join(self.degraded)
                + ". Deployments may be missing from this report entirely, so 'few orphans' "
                "here does not mean 'little bleed'."
            )
        return line


def live_orders_for(dseq: str, owner: str, base: str) -> int | None:
    """Count orders for `dseq` that can still become a lease, from ONE endpoint.

    Returns None when the endpoint could not be read or returned something unparseable.
    None is not zero: reporting zero here is what turns an unreachable node into a
    destroyed deployment.
    """
    path = (
        f"{MARKET_API}/orders/list?filters.owner={owner}&filters.dseq={dseq}&pagination.limit=50"
    )
    try:
        payload = _lcd_get(path, base=base)
    except Exception:  # noqa: BLE001 - any read failure is UNKNOWN, never "no orders"
        return None
    orders = payload.get("orders")
    if not isinstance(orders, list):
        return None
    live = 0
    for entry in orders:
        if not isinstance(entry, dict):
            continue
        # Node versions nest this differently; check both shapes rather than assuming.
        # Read ONCE into a local: calling .get twice narrows nothing for a type checker,
        # and the two calls are not guaranteed to return the same object.
        nested = entry.get("order")
        order = nested if isinstance(nested, dict) else entry
        state = str(order.get("state", "")).lower()
        if state in LIVE_ORDER_STATES:
            live += 1
    return live


def classify_deployment(
    dseq: str,
    owner: str,
    *,
    deployment_state: str,
    lease_count: int,
    escrow_uact: int = 0,
    bases: list[str] | None = None,
) -> DeploymentVerdict:
    """Classify ONE deployment from authoritative on-chain order state.

    Queries every endpoint CONCURRENTLY and counts how many independently agree, because
    a single lagging node reporting zero orders is indistinguishable from a real orphan
    until a second node confirms it.
    """
    if lease_count > 0:
        return DeploymentVerdict(
            dseq=dseq, classification=Classification.LEASED, escrow_uact=escrow_uact
        )

    endpoints = bases if bases is not None else rest_urls()
    if not endpoints:
        return DeploymentVerdict(
            dseq=dseq,
            classification=Classification.UNKNOWN,
            escrow_uact=escrow_uact,
            detail="no LCD endpoint available",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(endpoints))) as pool:
        readings = list(pool.map(lambda b: live_orders_for(dseq, owner, b), endpoints))

    answered = [r for r in readings if r is not None]
    if not answered:
        return DeploymentVerdict(
            dseq=dseq,
            classification=Classification.UNKNOWN,
            escrow_uact=escrow_uact,
            detail="no endpoint answered — unread, not orphaned",
        )

    # Any endpoint seeing a live order means the deployment can still be matched. Trust
    # the POSITIVE over the negative: a node that sees an order has information a node
    # that sees none does not, and being wrong in this direction only costs a delay.
    if any(r > 0 for r in answered):
        return DeploymentVerdict(
            dseq=dseq,
            classification=Classification.WAITING,
            escrow_uact=escrow_uact,
            live_orders=max(answered),
            confirmations=sum(1 for r in answered if r > 0),
            detail="a live order exists — legitimately waiting for a bid",
        )

    if str(deployment_state).lower() != "active":
        return DeploymentVerdict(
            dseq=dseq,
            classification=Classification.UNKNOWN,
            escrow_uact=escrow_uact,
            live_orders=0,
            confirmations=len(answered),
            detail=f"deployment_state={deployment_state!r} is not active",
        )

    return DeploymentVerdict(
        dseq=dseq,
        classification=Classification.ORPHANED,
        escrow_uact=escrow_uact,
        live_orders=0,
        confirmations=len(answered),
        detail=(
            f"no live order across {len(answered)} endpoint(s); "
            "holds escrow with no path to a lease"
        ),
    )


def enumeration_is_complete(
    console_rows: list[dict[str, Any]], onchain_dseqs: set[str]
) -> tuple[bool, str]:
    """Is the Console list we classified actually the whole fleet?

    `list_deployments` cannot detect truncation — `api.py` records it as verified live
    that `total` is the returned page size and `hasMore` is always false. So a deployment
    the Console omitted is invisible to every check downstream, and the report would look
    CLEANER the more badly it was truncated. Cross-checking against the chain's own
    deployments-by-owner list is the only way to notice.
    """
    console_dseqs = {str(r.get("dseq")) for r in console_rows if r.get("dseq") is not None}
    missing = onchain_dseqs - console_dseqs
    if missing:
        sample = ", ".join(sorted(missing)[:5])
        return False, (
            f"Console returned {len(console_dseqs)} deployments but the chain lists "
            f"{len(onchain_dseqs)}; {len(missing)} missing (e.g. {sample}). The Console "
            f"list is truncated or stale, so this report is INCOMPLETE."
        )
    return True, ""
