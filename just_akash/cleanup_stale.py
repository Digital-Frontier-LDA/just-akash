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
  * services == {}           -> LEAVE-unclassifiable (provider reported nothing) — UNLESS
    --reap-owned proves OURS on chain, then STALE-provider-closed at the 1h probe floor
    (#1763: provider-stopped, escrow recoverable only by close; unowned/unreadable/young
    stay LEAVE — the safe default is not widened, it is preceded by a proof)
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
from .wallet_pool import configured_api_keys

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
# ⛔ THE AGE RULE WAS UNREACHABLE FOR ANYTHING NOT ON THE SERVICE ALLOWLIST.
# `stamp_run`'s docstring promises that an UNSTAMPED deployment "falls back to the age
# rule". It could not: `classify` only reaches an age test after matching one of three
# EXACT service names — `probe`, `backtest`, `runner`. Anything else lands in
# LEAVE-real-or-unknown, where no age can ever be consulted.
#
# MEASURED 2026-09-01: six Blazing-Back E2E deployments (service `app`, images
# postgres:16-alpine and uzyexe/tetris) sat 62-139h holding 28.28 ACT. The scheduled
# reaper reported `stale (closable): 0` on every run. `E2E_SERVICE = "backtest"` is
# just-akash's own vocabulary; the sibling repo sharing this sweeper names its service
# `app`, and a service-name allowlist is a CONVENTION, not a fact about ownership.
#
# ⭐ Ownership is already provable without it. `group_spec.name` is author-controlled,
# written atomically inside MsgCreateDeployment and immutable after — the runner branch
# below already rests on exactly that. This threshold lets the same proof gate the age
# rule for ANY service, so the promise in stamp_run's docstring becomes true.
STALE_OWNED_AGE_SECONDS = 48 * 3600

RUNNER_SERVICE = "runner"
STALE_RUNNER_AGE_SECONDS = 6 * 3600

# ── EXECUTE-PATH RAILS (#250) ────────────────────────────────────────────
#
# These bind ONLY when --execute is passed. A dry run reports whatever it finds;
# the rails exist because closing is irreversible and spends escrow on a shared
# wallet. Every one of them REFUSES LOUDLY and exits non-zero — a rail that
# declines quietly reproduces the exact defect #250 is about, where a green run
# and a run that did nothing are indistinguishable.

# Bounded blast radius. If more than this is closable, close the OLDEST
# MAX_CLOSE_PER_RUN and say plainly that the run stopped short, so the operator
# sees progress AND sees that it is incomplete. Sized above a normal day's
# residue (the observed range over 56 scheduled runs is 0-55) and far below the
# whole active set, so a classification fault cannot drain the account in one
# pass while a genuine backlog still drains over a few runs.
MAX_CLOSE_PER_RUN = 25

# Tripwire on the SHAPE of the verdict table rather than on any single row. On
# 2026-09-04 the split was 55 stale of 154 audited = 36%. A classification bug
# that made everything look closable would spike this fraction, and the right
# response to "suddenly almost everything is garbage" is to stop and be looked
# at, not to act on it faster.
MAX_STALE_FRACTION = 0.75

# ...but ONLY once there is enough population for a fraction to mean anything.
# Caught by the existing suite: a fixture with two deployments, both genuinely
# stale, is 100% — and refusing there is simply wrong. A small account whose
# every deployment IS test residue is the normal case, not the alarming one, so
# an unguarded fraction rail would deadlock exactly the accounts it should be
# draining. The tripwire is a signal about a POPULATION; below this it has no
# population to be a signal about.
MIN_AUDITED_FOR_FRACTION_RAIL = 20

# Every verdict classify() can return. The execute path REFUSES on anything not
# in this set: an unrecognised verdict means the classifier has learned a
# category this reaper has never been taught to reason about, and closing on a
# label you do not understand is how a reaper starts closing the wrong thing.
# Pinned against the source by tests/test_cleanup_stale_rails.py so adding a
# verdict without updating this fails the suite rather than the fleet.
KNOWN_VERDICTS = frozenset(
    {
        "STALE-probe",
        "STALE-e2e",
        "STALE-runner",
        "STALE-owned",
        "STALE-provider-closed",
        "LEAVE-not-ours",
        "LEAVE-not-ours-provider-closed",
        "LEAVE-real-or-unknown",
        "LEAVE-recent-backtest",
        "LEAVE-recent-owned",
        "LEAVE-recent-runner",
        "LEAVE-unclassifiable",
        "LEAVE-unverified-owned",
        "LEAVE-unverified-provider-closed",
        "LEAVE-unverified-runner",
        "LEAVE-young-or-unaged-probe",
        "LEAVE-young-or-unaged-provider-closed",
    }
)


STALE_VERDICTS = (
    "STALE-probe",
    "STALE-e2e",
    "STALE-runner",
    "STALE-owned",
    "STALE-provider-closed",
)


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


def _wants_owned_provenance(detail: dict, dseq: str, now: float | None = None) -> bool:
    """True when `classify`'s reap_owned branch would actually consult ``group_names``.

    ⛔ MIRRORS classify's EARLY RETURNS and must be kept in step with them. {probe},
    {backtest} and {runner} return before the reap_owned branch, so reading their
    provenance spends a chain round-trip on a value nothing looks at.
    `test_provenance_is_skipped_where_classify_ignores_it` pins the pairing.

    The EMPTY set is judged by the reap_owned branch too (provider-closed), but at the
    PROBE floor rather than the owned floor — see classify for why the two floors differ.

    ⚠ The age test lives here too: a deployment younger than the floor returns
    LEAVE-recent-* regardless of ownership, so its provenance is equally unread. On a busy
    account that is the majority case and where most of the saving is.
    """
    services = _deployment_service_names(detail)
    if services in ({PROBE_SERVICE}, {E2E_SERVICE}, {RUNNER_SERVICE}):
        return False
    age = _probe_age_seconds(dseq, now)
    # An UNAGED deployment (undecodable dseq) still needs the read: classify cannot rule on
    # age, and skipping it would silently downgrade the verdict to LEAVE-unverified-* —
    # unreadable dressed as unowned, which is the confusion this module exists to refuse.
    floor = STALE_OWNED_AGE_SECONDS if services else MIN_ORPHAN_AGE_SECONDS
    return age is None or age >= floor


def classify(
    detail: dict,
    dseq: str,
    now: float | None = None,
    reap_runners: bool = False,
    group_names: list[str] | None = None,
    placement_prefix: str = PLACEMENT_PREFIX,
    reap_owned: bool = False,
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
        # PROVIDER-CLOSED (#1763, Blazing-Back): open on chain, the provider stopped it,
        # no live service manifest, escrow still held — and with auto top-up on, a
        # RECURRING charge into something running nothing. Console's own text: "close it
        # to recover any unused funds"; nothing else recovers them. This used to exit
        # here as LEAVE-unclassifiable BEFORE the ownership-proven path below could run,
        # so the one class that most needs closing was unreachable by the one proof that
        # would license it — the scheduled sweep reported `stale (closable): 0` while
        # exactly this shape sat holding escrow (dseq 1788245492506, group
        # dfci-infra-app, observed 2026-09-01).
        #
        # The guards are the owned branch's, in the same order, at the PROBE floor. The
        # floor is NOT a workload lifetime — a provider-closed deployment cannot become
        # live again, only a NEW deployment can replace it — it guards the READ RACE: a
        # healthy deployment minutes old may show no services yet (age=0.0d, services=-
        # was observed in run 33431994913), and 1h is this module's smallest existing
        # margin over every deploy→lease→manifest window in this fleet's pipelines.
        if reap_owned:
            if not group_names:
                # UNREADABLE is not UNOWNED, and here it is not CLASSIFIABLE either: a
                # failed chain read leaves this exactly where the safe default found it.
                return "LEAVE-unverified-provider-closed", services, age
            if not any(n.startswith(placement_prefix) for n in group_names):
                # Another project's naming scheme, or a bare unattributable group
                # (dcloud / akash1...): not ours to close, however stranded it looks.
                return "LEAVE-not-ours-provider-closed", services, age
            if age is not None and age >= MIN_ORPHAN_AGE_SECONDS:
                return "STALE-provider-closed", services, age
            return "LEAVE-young-or-unaged-provider-closed", services, age
        return "LEAVE-unclassifiable", services, age
    # ── ANY service, when ownership is PROVEN on chain and the thing is old ────────────
    # Opt-in (`reap_owned`), so no existing caller changes behaviour by upgrading. The
    # guards are the runner branch's, in the same order and for the same reasons:
    #   no group_names      -> UNREADABLE is not UNOWNED; a failed read must not destroy
    #   prefix does not match -> not ours; a shared wallet carries other repos' work
    #   young               -> leave; the age floor is the whole safety margin
    if reap_owned:
        if not group_names:
            return "LEAVE-unverified-owned", services, age
        if not any(n.startswith(placement_prefix) for n in group_names):
            return "LEAVE-not-ours", services, age
        if age is not None and age >= STALE_OWNED_AGE_SECONDS:
            return "STALE-owned", services, age
        return "LEAVE-recent-owned", services, age
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
    reap_owned: bool = False,
    api_key: str | None = None,
    max_close: int = MAX_CLOSE_PER_RUN,
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

    # Passed in by run_all_wallets(); the env read is the single-wallet path
    # kept so this function still works standalone and in tests.
    api_key = api_key or os.environ.get("AKASH_API_KEY")
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
    # Rail inputs. `seen_verdicts` feeds the unrecognised-verdict refusal;
    # `stale_ages` lets a capped run close the OLDEST first, so a partial pass
    # frees the escrow that has been locked longest rather than an arbitrary
    # slice. Both are gathered on every run, including dry ones, so the report
    # shows what the execute path WOULD have refused on.
    seen_verdicts: set[str] = set()
    stale_ages: dict[str, float] = {}
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
        elif reap_owned and _wants_owned_provenance(detail, dseq, now):
            # ⛔ reap_owned DECIDES ON PROVENANCE, so it must READ provenance — for the
            # services `classify` will actually consult it for. Without the read the flag is
            # inert in the worst way: `classify` returns LEAVE-unverified-owned for everything
            # and the sweep reports a clean account it never judged.
            #
            # ⚠ NARROWED, because the naive form paid a chain round-trip for EVERY deployment
            # — including {probe}, {backtest} and {runner}, whose branches return before
            # `group_names` is ever read. On an account of hundreds that is hundreds of wasted
            # reads for a value nothing consults.
            names = chain.deployment_group_names(address, dseq)
        verdict, services, age = classify(
            detail, dseq, now, reap_runners, names, placement_prefix, reap_owned=reap_owned
        )
        age_str = f"{age / 86400:5.1f}d" if age is not None else "   ?  "
        filtered = only_service is not None and set(services or []) != {only_service}
        suffix = f" (skipped: not services=={{{only_service}}})" if filtered else ""
        # When a verdict rests on provenance, the line SHOWS the name it rested on. A
        # table that asserts ownership without printing it makes every STALE-*-owned row
        # an uncheckable claim (#1763 wants per-deployment proof in the report itself).
        prov = f"  group={','.join(names) or '?'}" if names is not None else ""
        print(f"  {dseq}  age={age_str}  services={services or '-'}{prov}  -> {verdict}{suffix}")
        seen_verdicts.add(verdict)
        if verdict in STALE_VERDICTS and dseq in PROTECTED_DSEQS:
            print(f"    ^ PROTECTED-DSEQ: on the never-close list, {verdict} overridden")
            protected.append(dseq)
            continue
        if verdict in STALE_VERDICTS and not filtered:
            stale.append(dseq)
            # -1.0 sorts an unaged deployment LAST, never first: an unknown age
            # must not win a race to be closed under a cap.
            stale_ages[dseq] = age if age is not None else -1.0

    if protected:
        print(f"\nPROTECTED (never-close list): {len(protected)} -> {', '.join(protected)}")
    print(f"\nstale (closable): {len(stale)}")
    if not execute:
        print("DRY RUN — nothing closed. Re-run with --execute to close the stale set.")
        return 0

    # ── EXECUTE-PATH RAILS (#250) ────────────────────────────────────────
    # Past this point the run destroys things. Each rail refuses LOUDLY and
    # returns non-zero; none of them may decline quietly, because a silent
    # decline is indistinguishable from a clean run and that is the defect
    # this whole issue is about.

    # RAIL 1 — unrecognised verdict. classify() returning a label this reaper
    # has never been taught means the classifier grew a category and nobody
    # told the thing that acts on it. Closing on a label you cannot reason
    # about is how a reaper starts closing the wrong deployments. Refuse the
    # WHOLE run, not just the unknown rows: the unknown one may be evidence
    # that the known ones are also being judged by changed rules.
    unknown = sorted(seen_verdicts - KNOWN_VERDICTS)
    if unknown:
        print(
            # noqa: S608 — the bandit heuristic fires on "EXECUTE" in an f-string and
            # reads this operator message as SQL. There is no database here; the only
            # thing this function talks to is the Console API and the chain.
            f"\nREFUSING TO EXECUTE: classify() returned {len(unknown)} verdict(s) this "  # noqa: S608
            f"reaper does not know: {', '.join(unknown)}.\n"
            "  The classifier has a category the close path was never taught to reason "
            "about. Update KNOWN_VERDICTS deliberately after deciding whether each new "
            "verdict is closable — do not widen the set to make this pass.",
            file=sys.stderr,
        )
        return 2

    # RAIL 2 — shape tripwire. Not about any single row: if suddenly almost
    # everything looks closable, the likeliest cause is a classification fault,
    # and the right response to that is to stop and be looked at.
    audited = len(deployments)
    if audited >= MIN_AUDITED_FOR_FRACTION_RAIL:
        fraction = len(stale) / audited
        if fraction > MAX_STALE_FRACTION:
            print(
                f"\nREFUSING TO EXECUTE: {len(stale)}/{audited} = {fraction:.0%} of audited "
                f"deployments classified closable, above the {MAX_STALE_FRACTION:.0%} "
                "tripwire.\n"
                "  A share this high is more likely a classification fault than a real "
                "backlog. Investigate the verdict table above before closing anything.",
                file=sys.stderr,
            )
            return 2

    # RAIL 3 — cap. Close the OLDEST first so a partial pass frees the escrow
    # locked longest, and say plainly that it stopped short. Progress that
    # announces its own incompleteness beats either refusing entirely (the
    # backlog never drains) or closing everything (unbounded blast radius).
    stale = sorted(stale, key=lambda d: stale_ages.get(d, -1.0), reverse=True)
    capped = False
    if len(stale) > max_close:
        capped = True
        print(
            f"\nCAP: {len(stale)} closable, closing the {max_close} oldest this run. "
            f"{len(stale) - max_close} will remain — re-run to continue draining."
        )
        stale = stale[:max_close]

    closed, failed = 0, 0
    for dseq in stale:
        try:
            client.close_deployment(dseq)
            closed += 1
            print(f"  closed {dseq}")
        except Exception as exc:  # noqa: BLE001 — keep reaping; report failures at the end
            failed += 1
            print(f"  FAILED to close {dseq}: {exc}")

    remaining = " (CAPPED — more remain, re-run to continue)" if capped else ""
    print(f"\nclosed={closed} failed={failed}{remaining}")
    # Escrow settlement can lag a block or two; read after a short pause so the
    # AFTER line reflects the releases.
    time.sleep(10)
    print(f"credit AFTER:  {_credit_line(client, address)}")
    return 0 if failed == 0 else 1


def run_all_wallets(**kwargs) -> int:
    """Audit (and optionally close) across EVERY configured Console wallet.

    #250: the deploy path is pool-aware (`wallet_pool`, used by deploy.py and
    cli.py) and the reap path was not — it read the singular AKASH_API_KEY and
    audited one account. A reaper structurally unable to see wallets B and C
    reports green about them forever, and cannot distinguish "no stale
    deployments on wallet B" from "wallet B was never in scope". That is the
    same not-measured-vs-measured-clean defect the issue is about, one level up.

    Backward compatible BY CONSTRUCTION, not by a flag: `configured_api_keys()`
    appends the singular AKASH_API_KEY as a fallback, so with today's CI config
    this resolves to exactly one key and behaves identically to before.

    Each wallet is enumerated from the CHAIN under its own address — the Console
    listing does not scope by API key (see the comment in run(); three distinct
    keys returned byte-identical bodies), so per-wallet isolation has to come
    from the chain query, not from the credential.

    Returns the WORST exit code across wallets: one wallet refusing on a rail,
    or failing to close, must not be masked by another's success.
    """
    keys = configured_api_keys()
    if not keys:
        print("Error: neither AKASH_API_KEY nor AKASH_API_KEYS is set.", file=sys.stderr)
        return 2

    # Say how many wallets are in scope BEFORE auditing any. "1 wallet, clean"
    # and "3 wallets, only 1 audited" must never render the same way.
    print(f"Console wallets configured: {len(keys)}\n")
    worst = 0
    for index, key in enumerate(keys, start=1):
        print(f"===== wallet {index}/{len(keys)} =====")
        rc = run(api_key=key, **kwargs)
        worst = max(worst, rc)
        print()
    if len(keys) > 1:
        print(f"audited {len(keys)} wallet(s); worst exit code {worst}")
    return worst


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Close stale test deployments to free escrow.")
    ap.add_argument(
        "--execute",
        action="store_true",
        help="Actually close the stale deployments (default: dry-run report only).",
    )
    ap.add_argument(
        "--max-close",
        type=int,
        default=MAX_CLOSE_PER_RUN,
        metavar="N",
        help=(
            f"Close at most N deployments per wallet per run (default {MAX_CLOSE_PER_RUN}); "
            "the OLDEST first, so a capped pass frees the escrow locked longest. Bounds "
            "the blast radius of a classification fault without stalling a real backlog."
        ),
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
        "--reap-owned",
        action="store_true",
        help=(
            "Also close deployments of ANY service whose on-chain group name carries our "
            "placement prefix and which are older than STALE_OWNED_AGE_SECONDS. Ownership is "
            "PROVEN from group_spec.name, never assumed from the service name — a service-name "
            "allowlist is a convention between repos, and it silently stopped covering when a "
            "second repo began deploying. Costs one chain read per CANDIDATE — deployments "
            "whose service set classify decides without provenance ({probe}, {backtest}, "
            "{runner}, none) and those younger than the age floor are skipped."
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
    return run_all_wallets(
        execute=args.execute,
        max_close=args.max_close,
        reap_runners=args.reap_runners,
        reap_owned=args.reap_owned,
        placement_prefix=args.placement_prefix,
        only_service=args.only_service,
    )


if __name__ == "__main__":
    sys.exit(main())
