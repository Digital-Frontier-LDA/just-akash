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

This is the operator's path for exactly that case: one dseq and the group name it must carry,
both named by a person, verified here, closed only from a job behind a required-reviewer
environment.

WHAT IT REFUSES
---------------
Everything that is not provably a leaked backtest of this wallet:

  * the dseq or group not in their exact shapes (group ``just-akash-backtest.<12 hex>``);
  * a deployment this wallet's key does not own, or that is not active;
  * a live service set other than exactly ``{backtest}``; an EMPTY set (provider reported
    nothing) is unclassifiable, not a backtest;
  * younger than ``cleanup_stale.STALE_E2E_AGE_SECONDS`` (48 h), or an unknown age;
  * a chain group population that is not exactly ``[expected_group]`` as agreed by >= 2
    registered LCD sources (``chain.corroborated_deployment_group_names``: containment, not
    the creation proof). An unreachable or disagreeing chain is a refusal.

What it does NOT have, on purpose: the creation proof above. The containment read is what
``close_orphans`` also rests on (multi-endpoint agreement on the current chain), plus the two
things only a person can add: the exact dseq and the exact group, typed into a gated run.

Every check is re-run immediately before the close, and a close counts only once the
deployment reads back in a terminal state (a DELETE against a deployment we do not own
"succeeds" trivially; see close_orphans).

Usage:
    uv run python -m just_akash.close_backtest --dseq 1789163231320 \\
        --expected-group just-akash-backtest.482950fc2cf3            # verify only
    uv run python -m just_akash.close_backtest --dseq ... --expected-group ... --execute
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

from . import chain, lease_intent
from ._states import TERMINAL_DEPLOYMENT_STATES
from .api import AkashConsoleAPI, _reconcile_lease_row
from .cleanup_stale import E2E_SERVICE, STALE_E2E_AGE_SECONDS, _credit_line
from .smoke_providers import _deployment_service_names, _probe_age_seconds

DSEQ_RE = re.compile(r"[1-9][0-9]{0,19}")
GROUP_RE = re.compile(r"just-akash-backtest\.[0-9a-f]{12}")

# Escrow settlement can lag a block or two (same pause as close_orphans and cleanup_stale).
SETTLE_PAUSE_SECONDS = 10


def refusal(client, owner: str, dseq: str, group: str, now: float | None = None) -> str | None:
    """Why this deployment must NOT be closed, or None when every check holds."""
    if not DSEQ_RE.fullmatch(dseq) or int(dseq) > 2**64 - 1:
        return "dseq is not a plain positive integer"
    if not GROUP_RE.fullmatch(group):
        return f"expected group {group!r} is not just-akash-backtest.<12 hex>"
    try:
        detail = client.get_deployment(dseq)
    except Exception as exc:  # noqa: BLE001 — unreadable is a refusal, never a close
        return f"deployment unreadable ({str(exc)[:120]})"
    if not isinstance(detail, dict):
        return "deployment record is not an object"
    dep = detail.get("deployment")
    dep_owner = (dep.get("id") or {}).get("owner") if isinstance(dep, dict) else None
    if dep_owner != owner:
        return f"owner {dep_owner!r} is not this key's wallet {owner}"
    state = str(_reconcile_lease_row(detail).get("deployment_state") or "")
    if state != "active":
        return f"deployment state is {state!r}, not active"
    services = sorted(_deployment_service_names(detail))
    if not services:
        return "provider reports no services: unclassifiable, not a backtest"
    if services != [E2E_SERVICE]:
        return f"services {services} are not exactly [{E2E_SERVICE!r}]"
    age = _probe_age_seconds(dseq, now)
    if age is None:
        return "age unknown (dseq is not a ms timestamp)"
    if age < STALE_E2E_AGE_SECONDS:
        floor_h = STALE_E2E_AGE_SECONDS // 3600
        return f"only {age / 3600:.1f} h old; a backtest is stale after {floor_h} h"
    try:
        names = chain.corroborated_deployment_group_names(owner, dseq, group)
    except Exception as exc:  # noqa: BLE001 — ChainCorroborationUnreachable and anything else
        return f"chain group population unreadable ({type(exc).__name__})"
    if names != [group]:
        return f"chain sources do not agree the only group is {group!r} (got {names})"
    return None


def run(*, dseq: str, group: str, execute: bool = False) -> int:
    api_key = os.environ.get("AKASH_API_KEY")
    if not api_key:
        print("::error::AKASH_API_KEY not set", file=sys.stderr)
        return 2

    client = AkashConsoleAPI(api_key)
    owner = client.account_address()
    print(f"account: {owner}")
    print(f"credit BEFORE: {_credit_line(client, owner)}")

    why = refusal(client, owner, dseq, group)
    if why is not None:
        print(f"  {dseq}  {group} -> REFUSE: {why}")
        print(f"::error::{dseq} not closed: {why}")
        return 1
    print(f"  {dseq}  {group}  services=[{E2E_SERVICE}] -> VERIFIED leaked backtest")
    if not execute:
        print("DRY RUN — nothing closed. Re-run with execute to close it.")
        return 0

    # Re-verify at the close: the plan job's verdict is minutes old by the time an approver
    # lets this job start.
    why = refusal(client, owner, dseq, group)
    if why is not None:
        print(f"::error::{dseq} not closed: re-check at close time refused: {why}")
        return 1
    try:
        client.close_deployment(dseq)
    except Exception as exc:  # noqa: BLE001
        print(f"::error::close of {dseq} failed: {exc}")
        return 1
    try:
        state = str(_reconcile_lease_row(client.get_deployment(dseq)).get("deployment_state"))
    except Exception as exc:  # noqa: BLE001
        print(f"::error::{dseq}: close sent but state UNREADABLE ({exc}) — verify by hand")
        return 1
    if state not in TERMINAL_DEPLOYMENT_STATES:
        print(f"::error::{dseq}: close sent but state is still {state!r} — NOT counted as closed")
        return 1
    print(f"  closed {dseq} (read back: {state})")
    # Flynn openmix-h2l7: end any intent bound to it. Never stands in the way of a close.
    lease_intent.close_quietly(dseq, f"closed as a leaked backtest (read back: {state})")
    time.sleep(SETTLE_PAUSE_SECONDS)
    print(f"credit AFTER:  {_credit_line(client, owner)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Close one named, leaked just-akash backtest.")
    ap.add_argument("--dseq", required=True, help="The deployment to close.")
    ap.add_argument(
        "--expected-group",
        required=True,
        help="The group name it must carry on chain: just-akash-backtest.<12 hex>.",
    )
    ap.add_argument("--execute", action="store_true", help="Close it (default: verify only).")
    args = ap.parse_args(argv)
    return run(dseq=args.dseq.strip(), group=args.expected_group.strip(), execute=args.execute)


if __name__ == "__main__":
    sys.exit(main())
