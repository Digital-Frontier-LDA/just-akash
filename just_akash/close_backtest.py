#!/usr/bin/env python3
"""Close ONE named, leased, leaked just-akash backtest deployment.

WHY THIS EXISTS
---------------
An e2e run deploys ``sdl/cpu-backtest-ssh.yaml`` (sole service ``backtest``) and destroys it
in-run. When the run dies first, the lease keeps billing and nothing in this repo can close
it by name:

  * ``close_orphans`` closes only ORPHANED deployments (no active lease). A leaked backtest
    still HAS its lease, so every check there refuses it, correctly.
  * ``cleanup_stale`` classifies it ``STALE-e2e`` after 48 h but HOLDs the close, because a
    live close needs ``chain.owner_close_population_evidence``: the signed creation tx read
    back from >= 2 LCDs at the creation height. Public LCDs prune history, so a lease older
    than their retention can never produce that proof (Flynn openmix-hxlg; on 2026-10-18
    the sweep had failed every 6-hourly run for days on 1789163231320 and 1789370984331,
    both idle for over a month).
  * ``robust_destroy(owner=, group=)`` ends in ``wallet_pool.authorize_client_for_bound_owner``,
    which needs ``chain.owner_close_evidence``: the same creation proof. So it HOLDs too.

⛔ WHERE THE AUTHORITY COMES FROM
--------------------------------
From a PERSON: the operator names the exact dseq and its exact group, and a reviewer approves
the ``production`` environment for that run. The checks below do not prove "leaked"; they
stop a WRONG dseq (a typo, a fleet or canary lease, someone else's deployment, a young or
live one) from being closed under that approval. They do not stop a determined actor who
holds this wallet's key: anything such an actor can name, this would close. The signed
creation proof the other paths want would not separate them either, since repos that share
the Console wallet share its signer.

WHAT IT REFUSES
---------------
  * the dseq or group not in their exact shapes (group ``just-akash-backtest.<12 hex>``), or
    an owner that is not a plain akash address;
  * on the CHAIN (public LCDs, no key; this is all the ungated ``plan`` job can see): fewer
    than 2 registered sources that answer WITHOUT redirect and resolve to disjoint addresses;
    any of them reporting a state other than ``active``, a group population other than
    exactly ``[expected_group]``, a ``created_at`` height they disagree on, tips more than
    ``max_height_skew`` blocks apart or older than ``max_age_seconds``; an age, from
    ``created_at`` HEIGHT x ``wallet_pool.SECONDS_PER_BLOCK`` (never from the dseq, which the
    deployer chooses), under ``cleanup_stale.STALE_E2E_AGE_SECONDS`` (48 h);
  * with the KEY (only the gated ``close`` job): a key whose wallet is not ``owner``; a
    Console record whose owner, dseq or state disagrees; anything but EXACTLY ONE active
    lease whose status was read and whose complete service set is ``{backtest}``.

The Console read of the active lease is the LAST thing before the DELETE, after the chain
view. ⚠ A race remains and is not closable here: Console's DELETE takes the dseq only, with
no precondition, so a change between that last read and the DELETE (seconds) is not seen.

A close counts only when the deployment reads back with this owner, this dseq and state
exactly ``closed``. If the DELETE itself errors, the read-back still runs: the close may have
been committed and only its reply lost.

Exit codes: 0 verified (plan) or closed (execute); 1 refused, nothing sent; 2 usage;
3 outcome UNKNOWN (a DELETE may have been sent; verify by hand).

Usage:
    uv run python -m just_akash.close_backtest --owner akash1... --dseq 1789163231320 \\
        --expected-group just-akash-backtest.482950fc2cf3            # chain checks only
    AKASH_API_KEY=... uv run python -m just_akash.close_backtest ... --execute
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from . import chain, lease_intent
from .api import AkashConsoleAPI
from .cleanup_stale import E2E_SERVICE, STALE_E2E_AGE_SECONDS, _credit_line
from .wallet_pool import SECONDS_PER_BLOCK

DSEQ_RE = re.compile(r"[1-9][0-9]{0,19}")
GROUP_RE = re.compile(r"just-akash-backtest\.[0-9a-f]{12}")
OWNER_RE = re.compile(r"akash1[a-z0-9]{38,58}")

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_UNKNOWN = 0, 1, 2, 3

# Escrow settlement can lag a block or two (same pause as close_orphans and cleanup_stale).
SETTLE_PAUSE_SECONDS = 10


def shape_refusal(owner: str, dseq: str, group: str) -> str | None:
    if not OWNER_RE.fullmatch(owner):
        return "owner is not a plain akash address"
    if not DSEQ_RE.fullmatch(dseq) or int(dseq) > 2**64 - 1:
        return "dseq is not a plain positive integer"
    if not GROUP_RE.fullmatch(group):
        return f"expected group {group!r} is not just-akash-backtest.<12 hex>"
    return None


# ── the chain view: public reads, no key ────────────────────────────────────────────────


@dataclass(frozen=True)
class ChainView:
    created_height: int
    tip_height: int
    age_seconds: float


def _read_no_redirect(path: str, base: str) -> dict:
    # A redirect would let one registered host answer for another; refuse to follow it.
    return chain._lcd_get(path, base=base, timeout=15, follow_redirects=False)


def _resolve(host: str) -> frozenset[str]:
    return frozenset(
        str(info[4][0]) for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    )


def _chain_view(
    owner: str,
    dseq: str,
    group: str,
    *,
    sources,
    reader: Callable[[str, str], dict],
    resolve: Callable[[str], frozenset[str]],
    now: datetime,
) -> tuple[ChainView | None, str | None]:
    """Every registered source's view of this deployment, or why it is not enough."""
    info_path = (
        f"{chain._DEPLOYMENT_API}/deployments/info"
        f"?id.owner={urllib.parse.quote(owner)}&id.dseq={urllib.parse.quote(dseq)}"
    )
    tip_path = "/cosmos/base/tendermint/v1beta1/blocks/latest"
    votes: list[tuple[frozenset[str], int, datetime, int]] = []
    for source in sources:
        base = source.get("url") if isinstance(source, dict) else None
        if not isinstance(base, str):
            return None, "a registered chain source has no url"
        host = urllib.parse.urlsplit(base).hostname
        if not host:
            return None, "a registered chain source has no host"
        try:
            addresses = resolve(host)
            tip = reader(tip_path, base)
            info = reader(info_path, base)
        except Exception as exc:  # noqa: BLE001 — an unread source is no vote
            return None, f"chain source {host} unreadable ({type(exc).__name__})"
        header = (tip.get("block") or {}).get("header") if isinstance(tip, dict) else None
        dep = info.get("deployment") if isinstance(info, dict) else None
        try:
            tip_height = int(header["height"])  # type: ignore[index]
            tip_time = chain._rfc3339(header["time"])  # type: ignore[index]
            created = int(dep["created_at"])  # type: ignore[index]
            state = dep["state"]  # type: ignore[index]
        except (KeyError, TypeError, ValueError):
            return None, f"chain source {host} answered an unexpected shape"
        if not addresses or tip_time is None:
            return None, f"chain source {host} gave no address or no block time"
        if state != "active":
            return None, f"chain source {host} reports state {state!r}, not active"
        if chain._deployment_group_snapshot(info, owner, dseq) != (("1", group),):
            return None, f"chain source {host} does not show {group!r} as the only group"
        max_age = source.get("max_age_seconds", 180)
        if not 0 <= (now - tip_time).total_seconds() <= max_age:
            return None, f"chain source {host} tip is not fresh (block time {tip_time})"
        votes.append((addresses, tip_height, tip_time, created))

    # Independence: two names on one upstream are one vote.
    distinct: list[tuple[frozenset[str], int, datetime, int]] = []
    for vote in votes:
        if all(vote[0].isdisjoint(other[0]) for other in distinct):
            distinct.append(vote)
    if len(distinct) < 2:
        return None, f"only {len(distinct)} independent chain source(s) answered"
    created_heights = {v[3] for v in distinct}
    if len(created_heights) != 1:
        return None, f"chain sources disagree on created_at ({sorted(created_heights)})"
    tips = [v[1] for v in distinct]
    skew = min(int(s.get("max_height_skew", 5)) for s in sources if isinstance(s, dict))
    if max(tips) - min(tips) > skew:
        return None, f"chain source tips are {max(tips) - min(tips)} blocks apart (> {skew})"
    created_height = created_heights.pop()
    age = (min(tips) - created_height) * SECONDS_PER_BLOCK
    if age < STALE_E2E_AGE_SECONDS:
        hours = STALE_E2E_AGE_SECONDS // 3600
        return None, f"only {age / 3600:.1f} h old on chain; a backtest is stale after {hours} h"
    return ChainView(created_height, min(tips), age), None


def chain_view(owner: str, dseq: str, group: str) -> tuple[ChainView | None, str | None]:
    """The registered sources (chain.OWNER_CORROBORATION_SOURCES_V2), read without redirects."""
    if os.environ.get("AKASH_REST_URL") is not None:
        return None, "AKASH_REST_URL is set: a destructive read must use the registered sources"
    sources = chain.OWNER_CORROBORATION_SOURCES_V2
    if chain._source_registry_digest(sources) != chain.OWNER_CORROBORATION_REGISTRY_SHA256:
        return None, "the registered chain-source list does not match its digest"
    return _chain_view(
        owner,
        dseq,
        group,
        sources=sources,
        reader=_read_no_redirect,
        resolve=_resolve,
        now=datetime.now(timezone.utc),
    )


# ── the Console view: needs the key, so only the gated close job reaches it ─────────────


def console_refusal(client, owner: str, dseq: str) -> str | None:
    """Exactly one active lease, its status read, its complete service set {backtest}."""
    try:
        detail = client.get_deployment(dseq)
    except Exception as exc:  # noqa: BLE001 — unreadable is a refusal, never a close
        return f"deployment unreadable ({str(exc)[:120]})"
    dep = detail.get("deployment") if isinstance(detail, dict) else None
    dep_id = dep.get("id") if isinstance(dep, dict) else None
    if not isinstance(dep_id, dict) or dep_id.get("owner") != owner:
        return f"Console owner is not {owner}"
    if str(dep_id.get("dseq")) != dseq:
        return "Console returned a different dseq"
    if dep.get("state") != "active":  # type: ignore[union-attr]
        return f"Console state is {dep.get('state')!r}, not active"  # type: ignore[union-attr]
    leases = detail.get("leases")
    active = (
        [x for x in leases if isinstance(x, dict) and x.get("state") == "active"]
        if isinstance(leases, list)
        else []
    )
    if len(active) != 1:
        return f"{len(active)} active lease(s); exactly one is required"
    status = active[0].get("status")
    services = status.get("services") if isinstance(status, dict) else None
    if not isinstance(services, dict) or not services:
        return "the active lease's status (its services) could not be read"
    names = sorted(str(k) for k in services)
    if names != [E2E_SERVICE]:
        return f"the active lease runs {names}, not exactly [{E2E_SERVICE!r}]"
    return None


def _read_back(client, owner: str, dseq: str) -> str:
    """'closed', or why the read-back does not prove it."""
    try:
        detail = client.get_deployment(dseq)
    except Exception as exc:  # noqa: BLE001
        return f"unreadable ({str(exc)[:120]})"
    dep = detail.get("deployment") if isinstance(detail, dict) else None
    dep_id = dep.get("id") if isinstance(dep, dict) else None
    if (
        not isinstance(dep_id, dict)
        or dep_id.get("owner") != owner
        or str(dep_id.get("dseq")) != dseq
    ):
        return "read back a different owner or dseq"
    state = dep.get("state")  # type: ignore[union-attr]
    return "closed" if state == "closed" else f"state {state!r}"


def run(*, owner: str, dseq: str, group: str, execute: bool = False) -> int:
    why = shape_refusal(owner, dseq, group)
    if why is not None:
        print(f"::error::{dseq} refused: {why}")
        return EXIT_USAGE

    if not execute:
        view, why = chain_view(owner, dseq, group)
        if view is None:
            print(f"::error::{dseq} refused: {why}")
            return EXIT_REFUSED
        print(
            f"  {dseq}  {group}  created_at={view.created_height}  tip={view.tip_height}  "
            f"age={view.age_seconds / 86400:.1f} d  -> chain VERIFIED (services are read by the "
            "gated close job, with the key)"
        )
        print("PLAN ONLY — nothing closed.")
        return EXIT_OK

    api_key = os.environ.get("AKASH_API_KEY")
    if not api_key:
        print("::error::AKASH_API_KEY not set")
        return EXIT_USAGE
    client = AkashConsoleAPI(api_key)
    if client.account_address() != owner:
        print(f"::error::{dseq} refused: the key's wallet is not {owner}")
        return EXIT_REFUSED
    print(f"credit BEFORE: {_credit_line(client, owner)}")

    view, why = chain_view(owner, dseq, group)
    if view is None:
        print(f"::error::{dseq} refused: {why}")
        return EXIT_REFUSED
    # LAST read before the DELETE, after the chain view (see the race note above).
    why = console_refusal(client, owner, dseq)
    if why is not None:
        print(f"::error::{dseq} refused: {why}")
        return EXIT_REFUSED

    sent_error = None
    try:
        client.close_deployment(dseq)
    except Exception as exc:  # noqa: BLE001 — the close may still have committed
        sent_error = exc
    outcome = _read_back(client, owner, dseq)
    if outcome != "closed":
        detail = f" (DELETE raised: {sent_error})" if sent_error else ""
        print(
            f"::error::{dseq} outcome UNKNOWN: the close may have been sent, verify manually; "
            f"read-back: {outcome}{detail}"
        )
        return EXIT_UNKNOWN
    if sent_error is not None:
        print(f"  DELETE raised ({sent_error}) but the deployment reads back closed")
    print(f"  closed {dseq} (read back: closed)")
    # Flynn openmix-h2l7: end any intent bound to it. Never stands in the way of a close.
    lease_intent.close_quietly(dseq, "closed as a leaked backtest (read back: closed)")
    time.sleep(SETTLE_PAUSE_SECONDS)
    print(f"credit AFTER:  {_credit_line(client, owner)}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Close one named, leaked just-akash backtest.")
    ap.add_argument("--owner", required=True, help="The wallet that owns it (akash1...).")
    ap.add_argument("--dseq", required=True, help="The deployment to close.")
    ap.add_argument(
        "--expected-group",
        required=True,
        help="The group name it must carry on chain: just-akash-backtest.<12 hex>.",
    )
    ap.add_argument(
        "--execute", action="store_true", help="Close it (needs AKASH_API_KEY); default: plan."
    )
    args = ap.parse_args(argv)
    return run(
        owner=args.owner.strip(),
        dseq=args.dseq.strip(),
        group=args.expected_group.strip(),
        execute=args.execute,
    )


if __name__ == "__main__":
    sys.exit(main())
