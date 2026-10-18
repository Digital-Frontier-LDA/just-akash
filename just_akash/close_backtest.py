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
stop a WRONG dseq from being closed under that approval: a typo, a fleet or canary lease,
someone else's deployment, or a recent one (the 48 h floor; a long-running live backtest
older than that passes it). They do not stop a determined actor who holds this wallet's key:
anything such an actor can name, this would close. The signed creation proof the other paths
want would not separate them either, since repos that share the Console wallet share its
signer.

WHAT IT REFUSES
---------------
  * the dseq or group not in their exact shapes (group ``just-akash-backtest.<12 hex>``), or
    an owner that is not a plain akash address;
  * on the CHAIN (public LCDs, no key; this is all the ungated ``plan`` job can see):
      - fewer than 2 sources from the registry ``chain.OWNER_CORROBORATION_SOURCES_V2``, or two
        with the same registered ``operator`` or host, or a registered chain id other than
        ``akashnet-2``. Independence here is the REGISTRY's claim (distinct operators), not
        something this module measures: it does not resolve DNS or prove two hosts are not
        fronting one upstream. Reads never follow a redirect, so an answer comes from the
        registered host.
      - a tip whose header chain id is not ``akashnet-2``, whose height is not a positive
        integer, or whose block time is older than ``max_age_seconds`` when THAT response
        arrived; tips more than ``max_height_skew`` blocks apart.
      - then every source is read at ONE pinned height, the lowest tip minus 2 (the registry's
        ``committed-tip-minus-2``), with ``x-cosmos-block-height`` sent and the echoed height
        required to equal it (``chain._lcd_get``): a cached or historical answer is refused.
        At that height each must report state ``active``, exactly ``[expected_group]`` as the
        group population, and the same well-formed, positive ``created_at`` height at or
        below the pin.
      - an age, ``(pin - created_at) x wallet_pool.SECONDS_PER_BLOCK`` (never from the dseq,
        which the deployer chooses), under ``cleanup_stale.STALE_E2E_AGE_SECONDS`` (48 h);
  * with the KEY (only the gated ``close`` job): a key whose wallet is not ``owner``; a
    Console record whose owner, dseq or state disagrees; any malformed lease row (no state, no
    id); anything but EXACTLY ONE active lease, with this owner and dseq in its id, whose
    status was read and whose complete service set is ``{backtest}``.

The Console read of the active lease is the LAST thing before the DELETE, after the chain
view. ⚠ A race remains and is not closable here: Console's DELETE takes the dseq only, with
no precondition, so a change between that last read and the DELETE is not seen. The window is
the DELETE call itself, bounded by the Console timeout (``api.CONSOLE_HTTP_TIMEOUT``, 180 s).

A close counts only when the deployment reads back with this owner, this dseq and state
exactly ``closed``. If the DELETE itself errors, the read-back still runs: the close may have
been committed and only its reply lost. Reporting after a proven close (credit, intent) can
only warn; it never changes the outcome.

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
HEIGHT_RE = re.compile(r"[1-9][0-9]{0,19}")
CHAIN_ID = "akashnet-2"
# The registry's finality rule, `committed-tip-minus-2`: read state two blocks under the tip.
FINALITY_DEPTH = 2
# Our clock against a block's timestamp: a block may look a little "from the future".
CLOCK_SKEW_SECONDS = 30

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
    pinned_height: int
    age_seconds: float


def _read_no_redirect(path: str, base: str, height: int | None) -> dict:
    # No redirect: an answer comes from the registered host. With `height`, _lcd_get sends
    # x-cosmos-block-height and refuses an answer that does not echo exactly that height.
    return chain._lcd_get(path, base=base, timeout=15, height=height, follow_redirects=False)


def _sources_refusal(sources) -> str | None:
    hosts: set[str] = set()
    operators: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("url"), str):
            return "a registered chain source is malformed"
        host = (urllib.parse.urlsplit(source["url"]).hostname or "").lower()
        operator = source.get("operator")
        if not host or not isinstance(operator, str) or not operator:
            return "a registered chain source has no host or no operator"
        if source.get("chain_id") != CHAIN_ID:
            return f"a registered chain source is not on {CHAIN_ID}"
        if host in hosts or operator in operators:
            return "two registered chain sources share a host or an operator"
        hosts.add(host)
        operators.add(operator)
    if len(operators) < 2:
        return f"only {len(operators)} registered chain operator(s)"
    return None


def _chain_view(
    owner: str,
    dseq: str,
    group: str,
    *,
    sources,
    reader: Callable[[str, str, int | None], dict],
    clock: Callable[[], datetime],
) -> tuple[ChainView | None, str | None]:
    """Every registered source's view of this deployment at one pinned height, or why not."""
    why = _sources_refusal(sources)
    if why is not None:
        return None, why

    tips: list[int] = []
    for source in sources:
        host = urllib.parse.urlsplit(source["url"]).hostname
        try:
            tip = reader("/cosmos/base/tendermint/v1beta1/blocks/latest", source["url"], None)
        except Exception as exc:  # noqa: BLE001 — an unread source is no vote
            return None, f"chain source {host} unreadable ({type(exc).__name__})"
        arrived = clock()
        header = (tip.get("block") or {}).get("header") if isinstance(tip, dict) else None
        if not isinstance(header, dict):
            return None, f"chain source {host} answered an unexpected tip shape"
        if header.get("chain_id") != CHAIN_ID:
            return None, f"chain source {host} is on chain {header.get('chain_id')!r}"
        height_text = header.get("height")
        tip_time = chain._rfc3339(header.get("time"))
        if not isinstance(height_text, str) or not HEIGHT_RE.fullmatch(height_text):
            return None, f"chain source {host} gave a malformed tip height"
        if tip_time is None:
            return None, f"chain source {host} gave no block time"
        lag = (arrived - tip_time).total_seconds()
        if not -CLOCK_SKEW_SECONDS <= lag <= int(source.get("max_age_seconds", 180)):
            return None, f"chain source {host} tip is not fresh ({lag:.0f} s old on arrival)"
        tips.append(int(height_text))

    skew = min(int(s.get("max_height_skew", 5)) for s in sources)
    if max(tips) - min(tips) > skew:
        return None, f"chain source tips are {max(tips) - min(tips)} blocks apart (> {skew})"
    pinned = min(tips) - FINALITY_DEPTH
    if pinned < 1:
        return None, "chain tip is too low to pin"

    info_path = (
        f"{chain._DEPLOYMENT_API}/deployments/info"
        f"?id.owner={urllib.parse.quote(owner)}&id.dseq={urllib.parse.quote(dseq)}"
    )
    created_heights: set[int] = set()
    for source in sources:
        host = urllib.parse.urlsplit(source["url"]).hostname
        try:
            info = reader(info_path, source["url"], pinned)
        except Exception as exc:  # noqa: BLE001 — includes a wrong or missing echoed height
            return (
                None,
                f"chain source {host} unreadable at height {pinned} ({type(exc).__name__})",
            )
        dep = info.get("deployment") if isinstance(info, dict) else None
        if not isinstance(dep, dict):
            return None, f"chain source {host} answered an unexpected deployment shape"
        if dep.get("state") != "active":
            return None, f"chain source {host} reports state {dep.get('state')!r}, not active"
        if chain._deployment_group_snapshot(info, owner, dseq) != (("1", group),):
            return None, f"chain source {host} does not show {group!r} as the only group"
        created_text = dep.get("created_at")
        if not isinstance(created_text, str) or not HEIGHT_RE.fullmatch(created_text):
            return None, f"chain source {host} gave a malformed created_at {created_text!r}"
        created = int(created_text)
        if created > pinned:
            return None, f"chain source {host} says it was created above the pinned height"
        created_heights.add(created)
    if len(created_heights) != 1:
        return None, f"chain sources disagree on created_at ({sorted(created_heights)})"
    created_height = created_heights.pop()
    age = (pinned - created_height) * SECONDS_PER_BLOCK
    if age < STALE_E2E_AGE_SECONDS:
        hours = STALE_E2E_AGE_SECONDS // 3600
        return None, f"only {age / 3600:.2f} h old on chain; a backtest is stale after {hours} h"
    return ChainView(created_height, pinned, age), None


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
        clock=lambda: datetime.now(timezone.utc),
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
    if not isinstance(leases, list):
        return "the Console record has no lease list"
    for lease in leases:
        # A row we cannot classify could be the live one: refuse rather than skip it.
        if (
            not isinstance(lease, dict)
            or not isinstance(lease.get("state"), str)
            or not isinstance(lease.get("id"), dict)
        ):
            return "a lease row has no state or no id"
    active = [x for x in leases if x["state"] == "active"]
    if len(active) != 1:
        return f"{len(active)} active lease(s); exactly one is required"
    lease_id = active[0]["id"]
    if lease_id.get("owner") != owner or str(lease_id.get("dseq")) != dseq:
        return "the active lease's id names another owner or dseq"
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
            f"  {dseq}  {group}  created_at={view.created_height}  pinned={view.pinned_height}  "
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
    _report("credit BEFORE", lambda: _credit_line(client, owner))

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
    # Everything below only reports. The outcome is settled: a failure here warns, and the
    # exit stays 0, because 1 means "refused, nothing sent" and that is no longer true.
    # Flynn openmix-h2l7: end any intent bound to it.
    _report(
        "intent",
        lambda: lease_intent.close_quietly(
            dseq, "closed as a leaked backtest (read back: closed)"
        ),
    )
    time.sleep(SETTLE_PAUSE_SECONDS)
    _report("credit AFTER", lambda: _credit_line(client, owner))
    return EXIT_OK


def _report(label: str, fn: Callable[[], object]) -> None:
    """Print a reporting line; a failure warns and never changes the run's outcome."""
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001
        print(f"::warning::{label} not reported: {type(exc).__name__}: {str(exc)[:120]}")
        return
    if value is not None:
        print(f"{label}: {value}")


def main(argv: list[str] | None = None) -> int:
    # allow_abbrev=False: `--exec` (or any prefix) must not silently mean --execute.
    ap = argparse.ArgumentParser(
        description="Close one named, leaked just-akash backtest.", allow_abbrev=False
    )
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
