#!/usr/bin/env python3
"""Close explicitly named CI orphans with fresh identity and closure proof.

This is an operator-dispatched path, never an implicit account sweep. An explicit
prefix-to-repository register is required. Every group must carry agreeing versioned
CI identity whose owning run/attempt has completed. Production, staging, legacy and
unreadable identities are HELD. Orphan detection remains an independent condition,
rechecked before the final identity gate and DELETE.

DELETE acceptance never counts as success: two bound chain sources must verify complete
terminal (or empty) lease history plus closed deployment and escrow. No writer is enabled
here. Existing legacy workloads require separate migration and remain held.

Usage:
    python -m just_akash.close_orphans --dseq 1787240589224 \
        --ownership-register '{"just-akash-":"Digital-Frontier-LDA/just-akash"}'
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import _lease_verification, chain, cleanup_identity
from .api import AkashConsoleAPI, lease_status
from .cleanup_stale import _credit_line
from .orphan_detect import Classification, classify_deployment
from .provenance import PLACEMENT_PREFIX

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


def run(
    *,
    dseqs: list[str],
    execute: bool = False,
    ownership_register: dict | None = None,
    placement_prefix: str = PLACEMENT_PREFIX,
) -> int:
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
    held_identity: list[str] = []
    for dseq in dseqs:
        allowed, reason = cleanup_identity.eligible(
            address, dseq, placement_prefix, ownership_register
        )
        if not allowed:
            held_identity.append(dseq)
            print(f"  {dseq} HELD: {reason}")
            continue
        row = rows.get(dseq)
        if row is None:
            # The bound identity reader just proved this deployment active. A missing
            # Console row is conflicting instrumentation, not proof it was already closed.
            held_identity.append(dseq)
            print(f"  {dseq} HELD: active chain identity absent from Console population")
            continue
        # ADVISORY ONLY. Since #173 the classifier reads lease state from the chain and
        # uses this Console-derived count solely as a fallback when the chain cannot be
        # read — and then only in the safe direction (it can block a close, never permit
        # one). Still the ACTIVE count rather than the raw one: Console reports closed
        # leases as active, so the raw count would block closes at random.
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
        return 2 if held_identity else 0
    if not reapable:
        print("Nothing verified as an orphan; nothing to do.")
        return 2 if held_identity else 0

    closed, failed = 0, 0
    for dseq in reapable:
        # Recheck the orphan condition: an order may have opened since planning.
        fresh = classify_deployment(
            dseq, address, deployment_state="active", console_lease_count=0, escrow_uact=0
        )
        if not fresh.reapable:
            held_identity.append(dseq)
            print(f"  {dseq} HELD before DELETE: orphan condition changed")
            continue
        # Final identity authorization immediately precedes DELETE.
        allowed, reason = cleanup_identity.eligible(
            address, dseq, placement_prefix, ownership_register
        )
        if not allowed:
            held_identity.append(dseq)
            print(f"  {dseq} HELD before DELETE: {reason}")
            continue
        try:
            client.close_deployment(dseq)
        except Exception as exc:  # noqa: BLE001 — keep going; failures are tallied below
            failed += 1
            print(f"  FAILED to close {dseq}: {exc}")
            continue
        try:
            proof = _lease_verification.verdict(
                dseq,
                address,
                chain.rest_urls(),
                lambda url: chain._lcd_get("", base=url),
                retries=5,
                retry_sleep_s=2.0,
            )
            if proof.get("closed") is not True:
                failed += 1
                print(f"  {dseq}: close UNVERIFIED: {proof.get('reason')}")
                continue
        except Exception as exc:  # noqa: BLE001 — failed observation is never closure
            failed += 1
            print(f"  {dseq}: close UNVERIFIED ({exc})")
            continue
        closed += 1
        print(f"  closed {dseq} (chain deployment, lease and escrow proof)")

    print(f"\nclosed={closed} failed={failed}")
    time.sleep(SETTLE_PAUSE_SECONDS)
    print(f"credit AFTER:  {_credit_line(client, address)}")
    return 1 if failed else 2 if held_identity else 0


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
    ap.add_argument("--ownership-register", type=json.loads, default=None)
    ap.add_argument("--placement-prefix", default=PLACEMENT_PREFIX)
    args = ap.parse_args(argv)

    raw = list(args.dseq)
    if args.dseq_file:
        try:
            with open(args.dseq_file, encoding="utf-8") as fh:
                raw.extend(line.split("#", 1)[0] for line in fh)
        except OSError as exc:
            print(f"Error: cannot read --dseq-file {args.dseq_file}: {exc}", file=sys.stderr)
            return 2

    return run(
        dseqs=parse_dseqs(raw),
        execute=args.execute,
        ownership_register=args.ownership_register,
        placement_prefix=args.placement_prefix,
    )


if __name__ == "__main__":
    sys.exit(main())
