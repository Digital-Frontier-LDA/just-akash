#!/usr/bin/env python3
"""Close a NAMED list of orphaned deployments to release their escrow.

WHY THIS EXISTS
---------------
``cleanup_stale`` reaps deployments it can positively identify as test residue, by service
set. That classifier is deliberately conservative and it cannot help with an orphan,
because of a property the orphan itself has: with no active lease there is no provider, so
the provider reports no services, so ``classify()`` returns ``LEAVE-unclassifiable`` and
moves on. Measured 2026-08-22 against 84 active deployments: 26 held $104.33 with no active
lease and no open order, and the reaper's verdict on every one of them was
``services=- -> LEAVE-unclassifiable``, ``stale (closable): 0``.

So the fleet had a reaper that could not see orphans, and an orphan scan that reported zero
(see the ``active_lease_count`` fix in ``api._reconcile_lease_row`` for why). This is the
third piece: an operator names the dseqs, and this verifies each one INDEPENDENTLY before
closing it.

WHAT IT REFUSES
---------------
Everything it was not explicitly given, and then most of what it was:

  * there is no ``--all``, no glob and no age sweep. The dseq list is the whole input, so a
    bug here cannot widen its own blast radius.
  * each dseq is re-classified through ``orphan_detect.classify_deployment`` at close time,
    against ``active_lease_count`` — not the caller's belief, and not the list's age. Only
    ``DeploymentVerdict.reapable`` is closed, which requires ORPHANED **and** agreement from
    ``MIN_CONFIRMATIONS`` independent LCD endpoints.
  * a dseq that is not in the active set is reported and skipped, never closed. Already
    closed, or never ours: both are refusals, not errors.

That last property is the one that matters on this account. The wallet is SHARED — a live
read on 2026-08-22 found ``just-akash-*``, ``dfci-infra-*`` and ``borduas`` placements on it
— and ``cleanup_stale`` carries the scar of a sweep that destroyed 14 third-party
deployments. Naming dseqs explicitly and re-verifying each is what makes a wrong entry in
the list a refusal instead of somebody else's outage.

VERIFICATION IS ON READ-BACK, NOT ON THE EXIT CODE
--------------------------------------------------
``runner-teardown.yml`` learned this the hard way and says so: a destroy against an account
that does not own the lease succeeds trivially. So a close is only reported as a close once
the deployment reads back in a terminal state.

Usage:
    uv run python -m just_akash.close_orphans --dseq 1787240589224,1787240613971
    uv run python -m just_akash.close_orphans --dseq-file orphans.txt --execute
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from ._states import TERMINAL_DEPLOYMENT_STATES
from .api import AkashConsoleAPI, _reconcile_lease_row, lease_status
from .cleanup_stale import _credit_line
from .orphan_detect import Classification, classify_deployment

# Escrow settlement can lag a block or two; the AFTER line is read after this pause so it
# reflects the releases rather than the moment before them. Same value cleanup_stale uses.
SETTLE_PAUSE_SECONDS = 10


def parse_dseqs(raw: list[str]) -> list[str]:
    """Split comma/whitespace-separated dseq arguments into a de-duplicated list.

    Order is preserved so the log reads in the order the operator supplied, which is how
    they will diff it against whatever produced the list.
    """
    out: list[str] = []
    for chunk in raw:
        for token in chunk.replace(",", " ").split():
            token = token.strip()
            if token and token not in out:
                out.append(token)
    return out


def run(*, dseqs: list[str], execute: bool = False) -> int:
    if not dseqs:
        print("Error: no dseqs given. This command has no implicit target.", file=sys.stderr)
        return 2
    api_key = os.environ.get("AKASH_API_KEY")
    if not api_key:
        print("Error: AKASH_API_KEY not set.", file=sys.stderr)
        return 2

    client = AkashConsoleAPI(api_key)
    address = client.account_address()

    print(f"account: {address}")
    print(f"credit BEFORE: {_credit_line(client, address)}")
    print(f"requested: {len(dseqs)} dseq(s)\n")

    # ONE round-trip for the active set, then index it. Asking per-dseq would be N calls and
    # would still not tell us whether a dseq is absent because it closed or because the
    # listing failed — a listing we hold entire answers both.
    #
    # A row with no dseq is DROPPED rather than keyed as "None": one malformed record would
    # otherwise collide with the next and quietly make the index answer for the wrong
    # deployment, and this index is what decides whether a close is permitted.
    rows: dict[str, dict] = {}
    for r in lease_status(client, active_only=True):
        key = r.get("dseq")
        if key is None:
            continue
        rows[str(key)] = r

    reapable: list[str] = []
    for dseq in dseqs:
        row = rows.get(dseq)
        if row is None:
            # `active_only=True` filters on deployment.state == "active", so absence means
            # closed OR any other non-active state OR never ours. Naming only the first
            # would send an operator reconciling a list looking for the wrong thing.
            print(f"  {dseq}  not in the active set -> SKIP (closed, not active, or not ours)")
            continue
        # The classifier's parameter is called `lease_count`, but the value it must receive
        # is the ACTIVE count — passing the raw one is precisely the bug this module exists
        # downstream of. Named here so the mismatch is visible rather than inferred.
        active_leases = int(row.get("active_lease_count", 0) or 0)
        # None means the record omitted `funds` — UNKNOWN, not zero. Coercing it to 0 for
        # the classifier is fine (it does not decide on escrow), but printing "$0.00" would
        # tell the operator this deployment holds nothing while it may hold plenty.
        raw_escrow = row.get("escrow_remaining_uact")
        held = "unknown" if raw_escrow is None else f"${int(raw_escrow) / 1e6:.2f}"
        verdict = classify_deployment(
            dseq,
            address,
            deployment_state=str(row.get("deployment_state", "")),
            console_lease_count=active_leases,
            escrow_uact=int(raw_escrow or 0),
        )
        if verdict.reapable:
            reapable.append(dseq)
            print(
                f"  {dseq}  {verdict.classification.value}  {held}  "
                f"confirmations={verdict.confirmations} -> CLOSE"
            )
        else:
            why = verdict.detail or verdict.classification.value
            extra = ""
            if verdict.classification is Classification.ORPHANED:
                # ORPHANED but not reapable means too few endpoints agreed. Say so: that is
                # a read problem rather than a verdict, and it clears on its own.
                extra = f" (only {verdict.confirmations} endpoint(s) agreed)"
            print(f"  {dseq}  {verdict.classification.value}  {held} -> REFUSE: {why}{extra}")

    print(f"\nverified orphans (closable): {len(reapable)} of {len(dseqs)} requested")
    if not execute:
        print("DRY RUN — nothing closed. Re-run with --execute to close the verified set.")
        return 0
    if not reapable:
        print("Nothing verified as an orphan; nothing to do.")
        return 0

    closed, failed = 0, 0
    for dseq in reapable:
        try:
            client.close_deployment(dseq)
        except Exception as exc:  # noqa: BLE001 — keep going; failures are tallied below
            failed += 1
            print(f"  FAILED to close {dseq}: {exc}")
            continue
        # Read it back. A DELETE that returns 200 against a deployment we do not own is
        # indistinguishable from a real close until the state says so.
        try:
            state = str(_reconcile_lease_row(client.get_deployment(dseq)).get("deployment_state"))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  {dseq}: close sent but state UNREADABLE ({exc}) — verify by hand")
            continue
        if state in TERMINAL_DEPLOYMENT_STATES:
            closed += 1
            print(f"  closed {dseq} (read back: {state})")
        else:
            failed += 1
            print(f"  {dseq}: close sent but state is still {state!r} — NOT counted as closed")

    print(f"\nclosed={closed} failed={failed}")
    time.sleep(SETTLE_PAUSE_SECONDS)
    print(f"credit AFTER:  {_credit_line(client, address)}")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Close explicitly named orphaned deployments to free locked escrow.",
    )
    ap.add_argument(
        "--dseq",
        action="append",
        default=[],
        metavar="DSEQ[,DSEQ...]",
        help="Deployment(s) to close. Repeatable, and accepts comma/space-separated lists.",
    )
    ap.add_argument(
        "--dseq-file",
        default=None,
        metavar="PATH",
        help="File of dseqs, one per line (blank lines and #-comments ignored).",
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="Actually close the verified orphans (default: dry-run report only).",
    )
    args = ap.parse_args(argv)

    raw = list(args.dseq)
    if args.dseq_file:
        try:
            with open(args.dseq_file, encoding="utf-8") as fh:
                raw.extend(line.split("#", 1)[0] for line in fh)
        except OSError as exc:
            print(f"Error: cannot read --dseq-file {args.dseq_file}: {exc}", file=sys.stderr)
            return 2

    return run(dseqs=parse_dseqs(raw), execute=args.execute)


if __name__ == "__main__":
    sys.exit(main())
