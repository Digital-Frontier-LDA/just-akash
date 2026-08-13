#!/usr/bin/env python3
"""Close STALE test deployments on the Console account to free locked escrow.

Why this exists: every active deployment holds its deposit in escrow against
the account's deploy-credit grant, so leaked test deployments starve the
account until deploys 402 (measured 2026-07-21: ~$191 of a $246 grant locked,
free credit under the $5 deposit floor — CI e2e red for hours). The daily
smoke's sweep only reaps service-set ``{probe}`` deployments; e2e leftovers
(service ``backtest``) and older leaks accumulate with no reaper. This is that
reaper, as an on-demand maintenance command.

Classification is deliberately conservative — close ONLY what is unambiguously
disposable test residue; when in doubt, leave it and say so:

  * services == {probe}     and older than 1h   -> STALE (leaked smoke probe)
  * services == {backtest}  and older than 48h  -> STALE (leaked e2e workload;
    every e2e destroys its deployment in-run, so a 2-day-old one is a leak)
  * services == {runner}    and older than 6h   -> STALE **only with
    --reap-runners** AND only when the deployment's on-chain
    ``group_spec.name`` carries this repo's provenance prefix. Ownership is
    read from chain, not assumed: the shared wallet demonstrably hosts a
    sibling repo's runners too. An unreadable provenance leaves it alone —
    unreadable is not unowned. 6h because a pool is long-lived by design
    (``ephemeral: false`` outlives one job, a slow matrix runs for hours),
    while the e2e's 48h would let one cancelled run starve every other pool
    spending from the same grant
  * services == {}           -> LEAVE (provider reported nothing: cannot classify)
  * anything else (node, runner, train, ...) -> LEAVE (real or unknown workload)
  * unknown age -> LEAVE (never mis-age and reap wrongly)

DRY RUN IS THE DEFAULT. Pass ``--execute`` to actually close. Both modes print
the same per-deployment verdict table plus the free/locked credit before (and,
with --execute, after) so the freed escrow is visible in the run log.

Usage:
    uv run python -m just_akash.cleanup_stale             # report only
    uv run python -m just_akash.cleanup_stale --execute   # close stale ones
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import chain
from .api import AkashConsoleAPI, _extract_dseq, escrow_locked
from .provenance import PLACEMENT_PREFIX
from .smoke_providers import (
    MIN_ORPHAN_AGE_SECONDS,
    PROBE_SERVICE,
    _deployment_service_names,
    _probe_age_seconds,
)

# e2e (test_shell_e2e / test_secrets_e2e / smoke SSH checks) deploys the
# cpu-backtest-ssh SDL, whose sole service is `backtest`, and destroys it
# in-run — minutes, not days. 48h is far past any legitimate holder (a
# concurrent run, a paused debug session) while still catching week-old leaks.
E2E_SERVICE = "backtest"
STALE_E2E_AGE_SECONDS = 48 * 3600

# runner-pool.yml renders an SDL whose sole service is `runner`, and nothing reaped it:
# `runner` fell into LEAVE-real-or-unknown, so a pool cancelled between deploy and
# teardown leaked its lease forever. docs/github-runners.md sells `tag-prefix` as the
# thing that lets "a sweeper reap this run's lease", and runner-teardown.yml defers to
# an "akash-stale-sweeper" that does not exist here — this is that sweeper.
#
# 6h, not the probe's 1h: a pool is a LONG-LIVED workload by design. `ephemeral: false`
# keeps one alive across a queue of jobs, and a slow matrix on a small pool can legitimately
# run for hours, so an hour would reap live CI. It is not the e2e's 48h either, because at
# spike every leaked lease holds escrow against the same grant every other pool spends
# from — two days of that is what turns one cancelled run into a fleet-wide 402.
RUNNER_SERVICE = "runner"
STALE_RUNNER_AGE_SECONDS = 6 * 3600

STALE_VERDICTS = ("STALE-probe", "STALE-e2e", "STALE-runner")


def classify(
    detail: dict,
    dseq: str,
    now: float | None = None,
    reap_runners: bool = False,
    group_names: list[str] | None = None,
) -> tuple[str, list[str], float | None]:
    """(verdict, services, age_seconds) for one deployment detail."""
    services = sorted(_deployment_service_names(detail))
    age = _probe_age_seconds(dseq, now)
    if services == [PROBE_SERVICE]:
        if age is not None and age >= MIN_ORPHAN_AGE_SECONDS:
            return "STALE-probe", services, age
        return "LEAVE-young-or-unaged-probe", services, age
    if services == [E2E_SERVICE]:
        if age is not None and age >= STALE_E2E_AGE_SECONDS:
            return "STALE-e2e", services, age
        return "LEAVE-recent-backtest", services, age
    if services == [RUNNER_SERVICE]:
        # OWNERSHIP IS NOW PROVEN, NOT ASSERTED.
        #
        # This used to rest on the operator declaring that the Console account hosted
        # nothing but their own pools. That declaration was measurably FALSE on the very
        # wallet this ships against: a live read on 2026-08-12 found 11 active
        # deployments, SIX of them `dfci-infra-runner` — a sibling repo's runners on the
        # shared wallet. Reaping on shape plus an assertion would have destroyed them,
        # which is the 14-third-party-deployments failure all over again.
        #
        # `group_spec.name` settles it: the placement key is author-controlled, written
        # atomically inside MsgCreateDeployment and immutable after, so a deployment
        # carrying our prefix was created by this repo and nothing else can claim it.
        if not reap_runners:
            return "LEAVE-real-or-unknown", services, age
        if not group_names:
            # UNREADABLE is not UNOWNED. Every endpoint may have failed, or the
            # deployment may have closed under us. Destroying on a failed read is the
            # same class of error as destroying on a guess.
            return "LEAVE-unverified-runner", services, age
        if not any(n.startswith(PLACEMENT_PREFIX) for n in group_names):
            return "LEAVE-not-ours", services, age
        if age is not None and age >= STALE_RUNNER_AGE_SECONDS:
            return "STALE-runner", services, age
        return "LEAVE-recent-runner", services, age
    if not services:
        return "LEAVE-unclassifiable", services, age
    return "LEAVE-real-or-unknown", services, age


def _credit_line(client: AkashConsoleAPI, address: str) -> str:
    granted = chain.deploy_credit(address).get("uact", 0)
    locked = escrow_locked(client)
    free = max(granted - locked["locked_uact"], 0)
    # A tally that omitted a deployment makes FREE an upper bound, and this line is read
    # before deciding what to tear down. Silence about it reads as a measurement.
    omitted = locked.get("unreadable", 0) + locked.get("skipped_no_dseq", 0)
    suffix = f" [UPPER BOUND: {omitted} omitted]" if omitted else ""
    return (
        f"granted={granted / 1e6:.2f} locked_in_escrow={locked['locked_uact'] / 1e6:.2f} "
        f"FREE={free / 1e6:.2f} USD across {locked['deployments']} active deployments{suffix}"
    )


def run(*, execute: bool = False, now: float | None = None, reap_runners: bool = False) -> int:
    api_key = os.environ.get("AKASH_API_KEY")
    if not api_key:
        print("Error: AKASH_API_KEY not set.", file=sys.stderr)
        return 2
    client = AkashConsoleAPI(api_key)
    address = client.account_address()
    now = time.time() if now is None else now

    print(f"account: {address}")
    print(f"credit BEFORE: {_credit_line(client, address)}")

    deployments = client.list_deployments()
    print(f"active deployments: {len(deployments)}\n")

    stale: list[str] = []
    for d in deployments:
        dseq = _extract_dseq(d)
        if not dseq:
            continue
        try:
            detail = client.get_deployment(dseq)
        except Exception as exc:  # noqa: BLE001 — one unreadable deployment must not stop the audit
            print(f"  {dseq}  ERROR reading detail: {exc} -> LEAVE")
            continue
        # Read provenance ONLY for the candidates it can decide, so a sweep does not
        # spend a chain round-trip per deployment on an account of hundreds.
        names: list[str] | None = None
        if reap_runners and _deployment_service_names(detail) == {RUNNER_SERVICE}:
            names = chain.deployment_group_names(address, dseq)
        verdict, services, age = classify(detail, dseq, now, reap_runners, names)
        age_str = f"{age / 86400:5.1f}d" if age is not None else "   ?  "
        print(f"  {dseq}  age={age_str}  services={services or '-'}  -> {verdict}")
        if verdict in STALE_VERDICTS:
            stale.append(dseq)

    print(f"\nstale (closable): {len(stale)}")
    if not execute:
        print("DRY RUN — nothing closed. Re-run with --execute to close the stale set.")
        return 0

    closed, failed = 0, 0
    for dseq in stale:
        try:
            client.close_deployment(dseq)
            closed += 1
            print(f"  closed {dseq}")
        except Exception as exc:  # noqa: BLE001 — keep reaping; report failures at the end
            failed += 1
            print(f"  FAILED to close {dseq}: {exc}")

    print(f"\nclosed={closed} failed={failed}")
    # Escrow settlement can lag a block or two; read after a short pause so the
    # AFTER line reflects the releases.
    time.sleep(10)
    print(f"credit AFTER:  {_credit_line(client, address)}")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Close stale test deployments to free escrow.")
    ap.add_argument(
        "--execute",
        action="store_true",
        help="Actually close the stale deployments (default: dry-run report only).",
    )
    ap.add_argument(
        "--reap-runners",
        action="store_true",
        help=(
            "Also treat a lone `runner` service older than 6h as stale. OFF by default: "
            "nothing on chain proves a `runner` service is a just-akash CI pool, so this "
            "is YOUR assertion that this Console account hosts nothing else."
        ),
    )
    args = ap.parse_args(argv)
    return run(execute=args.execute, reap_runners=args.reap_runners)


if __name__ == "__main__":
    sys.exit(main())
