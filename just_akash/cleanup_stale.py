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


# ⛔ DEPLOYMENTS THAT MUST NEVER BE CLOSED, WHATEVER THE CLASSIFIER SAYS.
#
# This is not defensive padding. The classifier below is strong on two of its three closable
# classes — a runner needs on-chain provenance, and anything with an unrecognised service set
# is LEAVE-real-or-unknown — but STALE-e2e closes on SERVICE NAME AND AGE ALONE. Measured
# against the shipped classifier: services=["backtest"] at 30 days -> STALE-e2e -> CLOSES.
#
# A long-running research or backtest workload sharing a Console wallet with CI is therefore
# INDISTINGUISHABLE from an interrupted e2e run. The sibling sweeper in Blazing-Back learned
# this the expensive way: the df-sci-runtime deployment (64 vCPU / 64 GiB / 200 GiB
# persistent) was destroyed FOUR times — dseqs 1784375167504, 1784396842984, 1784470750834,
# and the current incarnation — each close taking the persistent volume with it.
#
# ⚠ THE DURABLE FIX IS A NARROWER PREDICATE, NOT A LONGER LIST, and this does not pretend
# otherwise. An allowlist protects the instances someone remembered to add; it cannot protect
# the next research deployment nobody told it about. It is kept because it is cheap, exact,
# and orthogonal to every heuristic above it — the one protection that holds when the
# classifier is wrong.
#
# ⚠ AND IT IS PRINTED, NEVER SILENT. A deployment skipped without a word is indistinguishable
# from one that was not there, which is how an over-broad allowlist would hide a real leak
# forever.
PROTECTED_DSEQS = frozenset(
    d.strip() for d in os.environ.get("PROTECTED_DSEQS", "1784532174413").split(",") if d.strip()
)


def classify(
    detail: dict,
    dseq: str,
    now: float | None = None,
    reap_runners: bool = False,
    group_names: list[str] | None = None,
    placement_prefix: str = PLACEMENT_PREFIX,
) -> tuple[str, list[str], float | None]:
    """(verdict, services, age_seconds) for one deployment detail.

    ``placement_prefix`` is the on-chain provenance marker a runner must carry to be
    considered OURS. It is a parameter of the REAP, never of the STAMP: `deploy.py` still
    writes `provenance.PLACEMENT_PREFIX` unconditionally, so nothing already deployed is
    orphaned. What this makes possible is a sibling repo sweeping ITS OWN prefix with this
    implementation instead of a second one.
    """
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
        if not any(n.startswith(placement_prefix) for n in group_names):
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


def run(
    *,
    execute: bool = False,
    now: float | None = None,
    reap_runners: bool = False,
    only_service: str | None = None,
    placement_prefix: str = PLACEMENT_PREFIX,
) -> int:
    """Audit (and optionally close) stale test deployments.

    ``only_service`` narrows the closable set to deployments whose service set
    is exactly that one service — e.g. ``probe``. An unattended, scheduled
    reaper must be able to reap the short-lived class it understands WITHOUT
    also being licensed to close the 48h ``backtest`` class, which can legally
    be a live e2e run, or the ``runner`` class whose ownership has to be proven
    on chain first. Without this the only options were "close everything stale"
    or "close nothing", so the scheduled sweep could not be enabled at all.
    Deployments outside the filter are still reported, just never closed.

    It composes with ``reap_runners`` rather than replacing it: that flag opens
    a class up for reaping, this one narrows which classes a given invocation is
    allowed to act on. Passing ``--only-service probe`` makes runner provenance
    moot for that run, which is the point — the bid-probe's own sweep has no
    business deciding anything about a runner pool.
    """
    # ⛔ A BLANK PREFIX MATCHES EVERY DEPLOYMENT ON THE ACCOUNT. `"".startswith` is True for
    # any string, so an empty prefix turns the ownership conjunct — the ONLY thing standing
    # between this reaper and a third party's workload — into a tautology. That is how a
    # sweep once destroyed 14 third-party deployments. An absent value is a configuration
    # error, never a permissive default.
    placement_prefix = (placement_prefix or "").strip()
    if not placement_prefix:
        print(
            "Error: placement prefix is empty. It is the ownership predicate; blank matches "
            "EVERY deployment on the account, including other repos'. Refusing to run.",
            file=sys.stderr,
        )
        return 2

    api_key = os.environ.get("AKASH_API_KEY")
    if not api_key:
        print("Error: AKASH_API_KEY not set.", file=sys.stderr)
        return 2
    client = AkashConsoleAPI(api_key)
    address = client.account_address()
    now = time.time() if now is None else now

    print(f"account: {address}")
    print(f"credit BEFORE: {_credit_line(client, address)}")

    # ⛔ ENUMERATE FROM THE CHAIN, NOT FROM THE CONSOLE LISTING. `client.list_deployments()`
    # sends `GET /v1/deployments` and relies on the API key to scope the response
    # server-side. IT DOES NOT. Measured 2026-08-30, three DISTINCT keys for three DISTINCT
    # accounts in the same minute: byte-identical bodies (sha256[:10]=56432a8d66, n=2)
    # against a chain showing 23 / 33 / 0 active. Minutes later all three returned HTTP 403.
    # The same endpoint is separately non-deterministic over time — 44 / 27 / 0 for ONE key
    # minutes apart, every time HTTP 200.
    #
    # ⛔ WHY THAT IS FATAL *HERE* SPECIFICALLY. This function's next act is to CLOSE things.
    # An enumeration that can return another account's page means closing another account's
    # deployments; one that can return a short page means a wallet is skipped with no error
    # for an unknown number of cycles. `filters.owner` on the chain is keyless, per-owner and
    # authoritative, and `_extract_dseq` already accepts the chain's nested record shape.
    #
    # Per-DSEQ Console reads below are unaffected — it is the LISTING that cannot scope.
    deployments = chain.list_active_deployments(address)
    if deployments is None:
        # ⛔ None IS NOT []. "Could not ask the chain" must never be swept as "holds nothing":
        # that collapse is exactly how a broken enumeration reads as a clean account.
        print(
            "::error::chain enumeration FAILED for "
            f"{address} — refusing to sweep. This is NOT an empty account; nothing was "
            "closed and nothing was ruled out. Retry, or set AKASH_REST_URL to a healthy "
            "endpoint.",
            file=sys.stderr,
        )
        return 2
    print(f"active deployments: {len(deployments)} (source: chain, owner-scoped)")
    # ⚠ PRINTED, because "0 closable" and "looking for the wrong prefix" are the same
    # output otherwise — and the second reads as a clean account forever.
    print(f"ownership prefix: {placement_prefix!r}\n")

    stale: list[str] = []
    protected: list[str] = []
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
        verdict, services, age = classify(detail, dseq, now, reap_runners, names, placement_prefix)
        age_str = f"{age / 86400:5.1f}d" if age is not None else "   ?  "
        filtered = only_service is not None and set(services or []) != {only_service}
        suffix = f" (skipped: not services=={{{only_service}}})" if filtered else ""
        print(f"  {dseq}  age={age_str}  services={services or '-'}  -> {verdict}{suffix}")
        if verdict in STALE_VERDICTS and dseq in PROTECTED_DSEQS:
            print(f"    ^ PROTECTED-DSEQ: on the never-close list, {verdict} overridden")
            protected.append(dseq)
            continue
        if verdict in STALE_VERDICTS and not filtered:
            stale.append(dseq)

    if protected:
        print(f"\nPROTECTED (never-close list): {len(protected)} -> {', '.join(protected)}")
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
    ap.add_argument(
        "--only-service",
        default=None,
        metavar="NAME",
        help=(
            f"Only close deployments whose service set is exactly {{NAME}} "
            f"(e.g. {PROBE_SERVICE}). Everything else is reported but left alone. "
            "Use this for unattended/scheduled sweeps so the reaper can never "
            "close a long-lived class it does not understand."
        ),
    )
    ap.add_argument(
        "--placement-prefix",
        default=os.environ.get("AKASH_PLACEMENT_PREFIX", PLACEMENT_PREFIX),
        metavar="PREFIX",
        help=(
            "The on-chain provenance marker a runner must carry to count as ours "
            f"(default: {PLACEMENT_PREFIX!r}). Set this ONLY to sweep a sibling repo's own "
            "prefix with this implementation; it changes what is REAPED, never what is "
            "STAMPED. A blank value is refused — it would match everything."
        ),
    )
    args = ap.parse_args(argv)
    return run(
        execute=args.execute,
        reap_runners=args.reap_runners,
        placement_prefix=args.placement_prefix,
        only_service=args.only_service,
    )


if __name__ == "__main__":
    sys.exit(main())
