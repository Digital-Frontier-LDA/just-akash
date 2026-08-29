#!/usr/bin/env python3
"""
Multi-step Akash deployment orchestrator.

Workflow:
1. Read SDL file
2. Create deployment via Console API
3. Collect bids for one bounded equal-opportunity window (default 60 seconds),
   then use the shared auction core to choose cheapest preferred or, when no
   preferred provider bid, cheapest eligible fallback.
4. Create lease with the selected provider.
5. Return deployment DSEQ and lease details.
"""

import json
import logging
import math
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from akash_lease_core import Auction, AuctionPolicy, AuctionStatus, BidObservation
from akash_lease_core.auction import PreferredSelection
from akash_lease_core.capacity import ProviderCapacity

from . import chain
from ._diagnostics import Code, emit, enabled
from .api import (
    AkashConsoleAPI,
    _extract_bid_price,
    _extract_gseq,
    _extract_provider,
)
from .provenance import PLACEMENT_PREFIX, SIBLING_REAPED_PREFIX, run_id_of, stamp_run
from .provider_capacity import capacity_by_provider
from .sdl_validate import SDLValidationError, validate_sdl

logger = logging.getLogger("akash.deploy")

# One per PROCESS, not per call: a single `deploy` run may create an order, fail, and
# re-create (the issue-#19 re-deploy round), and both are the same run's residue. Hex so
# it survives `_KEY_RE`'s charset and reads back through provenance.run_id_of.
_RUN_ID = uuid.uuid4().hex[:12]


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_window_deadline(deadline_epoch: float) -> str:
    """Format the absolute deadline of the equal-opportunity bid window.

    Surfaces a wall-clock UTC timestamp so the operator sees *when the window
    will close*, not just *how long the wait has been*. Without this, the
    poll output reads as "still waiting, still waiting" — silence that
    operators interpret as "nothing is happening" (C5 review item 3,
    just-akash tracking issue #178). The deadline is shown in the same UTC
    format as the rest of the deploy log so a CI run that scrapes the log
    can correlate the deadline with the orchestrator's clock.
    """
    return datetime.fromtimestamp(deadline_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_window_progress_bar(elapsed: int, total: int, width: int = 20) -> str:
    """A 20-char ASCII progress bar for the bid collection window.

    Cheap, terminal-safe (no Unicode box-drawing), and color-free — the
    output has to land in the same log as the rest of the deploy
    diagnostics, which CI pipelines grep without ANSI handling. The bar is
    informational only; it does not gate the actual selection deadline
    (that lives in `collection_deadline = start_time + bid_wait`). The width
    is fixed so successive log lines align.
    """
    total = max(total, 1)
    fraction = min(max(elapsed / total, 0.0), 1.0)
    filled = int(fraction * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _fmt_window_status_line(
    *,
    elapsed: int,
    total: int,
    deadline_iso: str,
    poll_count: int,
    bid_count: int,
    bar: str,
) -> str:
    """Format the per-poll status line for the bid collection window.

    Single source of truth for the format string so a future change lands in
    one place. Shows:

      - elapsed/total seconds and percent,
      - the absolute UTC deadline,
      - the ASCII progress bar,
      - the number of bids received so far,
      - the poll counter.

    Replaces the prior "Waiting for bids... {elapsed}s (poll #{poll_count})"
    line that read as silence during the window (C5 review item 3).
    """
    pct = min(int(elapsed * 100 / max(total, 1)), 100)
    return (
        f"  {bar} {elapsed:>3}/{total}s ({pct:>3}%)  "
        f"deadline={deadline_iso}  "
        f"bids={bid_count}  poll=#{poll_count}"
    )


def _log(level: int, msg: str):
    logger.log(level, f"[{_ts()}] {msg}")
    if level >= logging.INFO:
        print(f"[{_ts()}] {msg}", flush=True)


def _close_proven_orphan(client, dseq: str, key: str) -> bool:
    """Close a deployment proven to be this call's, reporting honestly either way.

    Only reached when the on-chain placement key carries THIS process's run id, so the
    deployment cannot belong to a sibling repo or to a concurrent run of our own. The
    create that produced it already raised, so nothing will ever claim it.

    Returns False on any failure, and the caller then reports the dseq the old way — a
    close we did not achieve must never be reported as one, which is the same rule
    runner-teardown.yml applies to its own destroy.
    """
    try:
        client.close_deployment(str(dseq))
    except Exception as exc:  # noqa: BLE001 — an error path must not raise a second error
        _log(logging.WARNING, f"could not close orphan {dseq} ({key}): {exc}")
        return False
    _log(
        logging.ERROR,
        f"ORPHAN CLOSED: deployment {dseq} carried this run's provenance ({key}) and no "
        f"lease. The create reported failure but the transaction had committed, so it was "
        f"holding escrow under a dseq nobody would have known to look for. Closed "
        f"automatically; no action needed.",
    )
    emit(
        Code.DEPLOY_CREATE_ORPHAN_SUSPECTED,
        "error",
        f"deployment {dseq} was created by a failed create and has been closed",
        dseq=str(dseq),
        provenance=[key],
        owned=True,
        closed=True,
    )
    return True


def _report_suspected_orphans(client, since_epoch_s: float, run_id: str = "") -> list[str]:
    """Name deployments a FAILED create may nonetheless have brought into existence.

    A create that raises is not proof that nothing was created. `POST /v1/deployments`
    writes on-chain state, so a gateway 500, a proxy timeout or a dropped connection can
    land *after* the transaction committed — measured shape: HTTP 500 returned 103
    SECONDS into the request. The deployment then exists, holds its deposit in escrow
    against the same grant every later run spends from, carries no tag, and nobody knows
    its dseq. That is the most expensive kind of leak precisely because nothing reports
    it: the next run's funding failure looks like a market outage.

    Attribution uses three signals now, and the third changes who gets named:
      * the dseq, which just-akash mints as a ms-epoch timestamp, is at or after the
        moment this create was issued — so it cannot be a pre-existing workload;
      * the deployment holds NO lease — one that won a lease is somebody's live
        workload, not the residue of a request that failed before returning a dseq;
      * its on-chain ``group_spec.name`` carries this repo's provenance prefix.

    THE THIRD SIGNAL REMOVES STRANGERS FROM THE REPORT. The shared Console wallet hosts
    other repos' deployments — a live read found six ``dfci-infra-runner`` among eleven
    active — and those are created concurrently, leaseless for a moment, inside exactly
    this window. Without provenance they were named as POSSIBLE ORPHAN, which sends an
    operator to destroy a sibling repo's live deployment. Naming the innocent is not a
    lesser failure than missing the guilty; it is the one that loses data.

    It suppresses only on POSITIVE foreign attribution — a known other-repo prefix — and
    never on "does not carry ours". This command deploys ARBITRARY caller SDLs: a user's
    own file may declare any placement key, and ``provenance.py``'s guard covers this
    repo's ``sdl/`` only. Reading "not our prefix" as "not our problem" would silently
    drop exactly the orphan whose SDL we never controlled.

    IT CLOSES WHAT IT CAN PROVE, AND ONLY THAT. The placement key is run-scoped, so a
    deployment carrying THIS process's run id was created by this very call — not by a
    sibling repo, and not by a concurrent run of our own. That create already raised, so
    the caller is abandoning it: a deployment we cannot return is by definition
    unclaimed, and leaving it open means escrow held against the grant every later run
    spends from, under a dseq nobody knows.

    Everything short of that proof is still REPORT ONLY. Repo-level provenance cannot
    single out one create's residue — a concurrent run of *this* repo stamps the same
    prefix, is also leaseless mid-create, and lands in the same window — so closing on it
    would destroy a healthy in-flight deploy. Naming the dseq makes cleanup a single
    command; guessing makes it a data-loss bug.

    Unreadable provenance keeps a deployment IN the report, marked unverified. Every LCD
    may have failed, and a leak we cannot attribute is still a leak — silence there would
    trade the loud failure for the expensive one.

    Never raises: this runs on an error path, and a failure to reconcile must not replace
    the original error with its own.
    """
    since_ms = int(since_epoch_s * 1000)
    try:
        active = client.list_deployments(active_only=True)
    except Exception as exc:  # noqa: BLE001 — diagnosis must never mask the real failure
        _log(logging.WARNING, f"Could not check for an orphaned deployment: {exc}")
        return []

    suspects: list[str] = []
    for dep in active or []:
        if dep.get("leases") or dep.get("lease"):
            continue
        dseq = dep.get("dseq") or (dep.get("deployment") or {}).get("dseq")
        try:
            created_ms = int(dseq)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            # A dseq we cannot date is not evidence either way. Staying silent about it
            # is right: this path exists to name what we can prove, not to raise alarms.
            continue
        # Compared in WHOLE MILLISECONDS, on purpose. `time.time()` carries sub-ms
        # precision while a dseq is floored to its millisecond, so dividing the dseq
        # back to seconds made a deployment minted in the SAME millisecond as the
        # request compare as OLDER than it — i.e. the tightest true orphan, the one
        # created by this very call, was the one most likely to be missed.
        if created_ms >= since_ms:
            suspects.append(str(dseq))

    if not suspects:
        return []

    # The owner is needed to read provenance. Without it every suspect is unverified,
    # which is the honest degradation — never a reason to drop the report.
    try:
        owner = client.account_address()
    except Exception:  # noqa: BLE001 — diagnosis must never mask the real failure
        owner = ""

    reported: list[str] = []
    for dseq in suspects:
        try:
            names = chain.deployment_group_names(owner, dseq) if owner else []
        except Exception:  # noqa: BLE001 — this function is documented as never raising
            # An unreadable provenance must weaken the CLAIM, never replace the create
            # failure with a lookup failure. The caller is already handling a real error.
            names = []
        ours = any(n.startswith(PLACEMENT_PREFIX) for n in names)
        foreign = (
            bool(names) and not ours and all(n.startswith(SIBLING_REAPED_PREFIX) for n in names)
        )
        if foreign:
            # Positively somebody else's. Do not name it: an operator handed this dseq
            # would run `destroy` on a live deployment belonging to another repo.
            # DEBUG, not INFO: _log prints to stdout at INFO and above, and the whole
            # point of suppressing this deployment is that its dseq must not appear in
            # human-facing output next to `destroy` instructions. The trace stays
            # available for diagnosing a wrong suppression.
            _log(
                logging.DEBUG,
                f"  deployment {dseq} was created in this window but belongs to another "
                f"repo (group_spec.name={names}) — suppressed",
            )
            continue

        # The NAME THAT MATCHED, not names[0]. A deployment may carry several groups,
        # and reporting the first one can print a foreign key beside a "this run" claim
        # — the one line an operator reads to decide whether the tool is right.
        mine_key = next((n for n in names if run_id and run_id_of(n) == run_id), "")
        our_key = next((n for n in names if n.startswith(PLACEMENT_PREFIX)), "")
        if mine_key:
            # PROVEN to be this call's residue. Close it rather than describing it.
            if _close_proven_orphan(client, dseq, mine_key):
                continue
            headline = f"ORPHAN (this run, group_spec.name={mine_key}) — COULD NOT CLOSE"
            proof = (
                "Its provenance carries THIS run's id, so this call created it, and the "
                "automatic close failed"
            )
        elif our_key:
            headline = f"ORPHAN (confirmed ours, group_spec.name={our_key})"
            proof = "Its on-chain provenance carries this repo's prefix"
        elif names:
            headline = f"POSSIBLE ORPHAN (unattributed, group_spec.name={names[0]})"
            proof = (
                "Its provenance names no repo we recognise — it may be from a caller "
                "SDL of ours, or another tenant's; check before destroying"
            )
        else:
            headline = "POSSIBLE ORPHAN (ownership unverified)"
            proof = (
                "Its provenance could not be read, so this may belong to another repo "
                "on the shared wallet — check before destroying"
            )
        _log(
            logging.ERROR,
            f"{headline}: deployment {dseq} was created during the failed request and "
            f"holds no lease. The create reported failure, but the transaction may still "
            f"have committed — it is holding escrow against the grant the next run spends "
            f"from. {proof}. Verify and close: just-akash status --dseq {dseq} && "
            f"just-akash destroy --dseq {dseq} -y",
        )
        emit(
            Code.DEPLOY_CREATE_ORPHAN_SUSPECTED,
            "error",
            f"deployment {dseq} may have been created by a create that reported failure",
            dseq=dseq,
            provenance=names or None,
            owned=ours,
        )
        reported.append(dseq)
    return reported


def _fmt_price(bid) -> str:
    amount, denom = _extract_bid_price(bid)
    return f"{amount} {denom}"


def _bid_state(b) -> str:
    """Extract a bid's state, tolerating both flat and nested API shapes."""
    if not isinstance(b, dict):
        return "?"
    nested = b.get("bid", {})
    nested_state = nested.get("state", "?") if isinstance(nested, dict) else "?"
    return b.get("state", nested_state)


def _backup_fallback_grace_s() -> int:
    """Max seconds after order creation to keep waiting for a preferred bid
    while open BACKUP bids are available (issue #14). Akash bids expire
    ~5 min after the order opens, so a grace longer than that guarantees the
    fallback pool is stale by the time phase 3 runs. Override with
    JUST_AKASH_BACKUP_FALLBACK_S.
    """
    try:
        return int(os.environ.get("JUST_AKASH_BACKUP_FALLBACK_S", "240"))
    except ValueError:
        return 240


def _redeploy_poll_window() -> tuple[float, float, float]:
    """Fast-poll window for the issue-#19 re-deploy round: (total_wait,
    backup_courtesy, poll_interval) in seconds.

    Intentionally short — the phased patience of the normal selection path is
    exactly what aged the first round's bid past its ~5-min expiry, so the
    re-created order is leased aggressively (preferred wins instantly; backup
    only after the courtesy window). Override via JUST_AKASH_REDEPLOY_WAIT_S,
    JUST_AKASH_REDEPLOY_BACKUP_COURTESY_S, and JUST_AKASH_REDEPLOY_POLL_INTERVAL_S.
    """

    def _f(name: str, default: str) -> float:
        try:
            return float(os.environ.get(name, default))
        except ValueError:
            return float(default)

    return (
        _f("JUST_AKASH_REDEPLOY_WAIT_S", "75"),
        _f("JUST_AKASH_REDEPLOY_BACKUP_COURTESY_S", "20"),
        _f("JUST_AKASH_REDEPLOY_POLL_INTERVAL_S", "5"),
    )


def _is_open_bid(b) -> bool:
    """Whether a bid is still leasable.

    The Console API keeps returning bids after they expire (state flips away
    from `open`), and leasing a non-open bid is a guaranteed HTTP 400
    ("The selected bid is no longer open") — issue #14. Bids with no state
    field at all ("?") are treated as open so older/partial API shapes keep
    working.
    """
    return _bid_state(b) in ("open", "?")


def _classify_bid(provider: str | None, preferred: list[str], backup: list[str]) -> str:
    """Tag a bid by tier. With no allowlist set, every bid is ACCEPTED.
    Accepts None (a malformed bid with no provider field) — classified as
    FOREIGN when an allowlist is configured, ACCEPTED otherwise.
    """
    if not preferred and not backup:
        return "ACCEPTED"
    if provider and provider in preferred:
        return "PREFERRED"
    if provider and provider in backup:
        return "BACKUP"
    return "FOREIGN"


class _SelectionKwarg(TypedDict, total=False):
    """The single optional keyword handed to AuctionPolicy.

    ⛔ A TypedDict, NOT `dict[str, PreferredSelection]`. Pyright reads `**dict[str, V]`
    as "may supply ANY keyword with a value of type V", so it then reports the value as
    incompatible with every other field on the policy — measured here against
    `excluded_providers` and `required_proofs`, both `frozenset[str]`. `total=False`
    says exactly what is true: this mapping carries `preferred_selection` or nothing.
    """

    preferred_selection: "PreferredSelection"


def _selection_kwarg(selection: "PreferredSelection | None") -> _SelectionKwarg:
    """`{'preferred_selection': ...}` when asked for, `{}` otherwise.

    Keeping this a separate function is what lets the type narrow: the caller holds an
    Optional, the policy field does not, and an empty mapping carries no key at all.
    """
    if selection is None:
        return {}
    return {"preferred_selection": selection}


def _resolve_selection(select: str) -> "PreferredSelection":
    """Map the CLI's `--select` to the auction's mode.

    ⛔ AN UNKNOWN VALUE RAISES. Falling back to cheapest on a typo would be the worst
    outcome: the operator asked for a different placement policy, got the default, and
    nothing said so. A typo must be loud.
    """
    table = {
        "cheapest": PreferredSelection.CHEAPEST,
        "emptiest": PreferredSelection.EMPTIEST,
    }
    key = (select or "cheapest").strip().lower()
    if key not in table:
        raise ValueError(f"unknown --select {select!r}; expected one of {sorted(table)}")
    return table[key]


def _select_auction_bid(
    bids: list,
    *,
    preferred: list[str],
    backup: list[str],
    collection_window_seconds: float,
    fallback_window_seconds: float = 0,
    evaluated_at: float | None = None,
    observed_at_by_provider: dict[str, float] | None = None,
    capacity_by_provider: dict[str, "ProviderCapacity"] | None = None,
    preferred_selection: "PreferredSelection | None" = None,
    already_selected: frozenset[str] | None = None,
):
    """Normalize Console bids and delegate the decision to the shared core.

    The caller owns polling and clocks.  This adapter owns only translation
    between Console's response shape and the transport-neutral auction schema.
    """
    has_allowlist = bool(preferred or backup)
    eligible = frozenset(preferred + backup) if has_allowlist else None
    auction = Auction(
        AuctionPolicy(
            collection_window_seconds=collection_window_seconds,
            fallback_window_seconds=fallback_window_seconds,
            preferred_providers=frozenset(preferred),
            eligible_providers=eligible,
            # ⚠ DEFAULT IS UNCHANGED. An absent selection leaves AuctionPolicy on its
            # OWN default rather than restating it here — passing CHEAPEST explicitly
            # would pin just-akash to today's library default and silently diverge if
            # akash-lease-core ever changes it. So the key is omitted, not defaulted.
            #
            # ⛔ Built as a narrowed dict rather than `**({...} if x else {})`: inside
            # the `is not None` branch the value is a PreferredSelection, which is what
            # the field declares. The inline-conditional form leaves the type as
            # `PreferredSelection | None` at the call site and Pyright rejects it.
            **_selection_kwarg(preferred_selection),
        ),
        started_at=0,
    )
    raw_by_key = {}
    for index, raw_bid in enumerate(bids):
        if not isinstance(raw_bid, dict):
            continue
        provider = _extract_provider(raw_bid)
        if not provider:
            continue
        amount, denom = _extract_bid_price(raw_bid)
        bid_key = f"{provider}:{index}"
        try:
            observation = BidObservation(
                bid_key=bid_key,
                provider=provider,
                price=amount,
                denom=denom or "uakt",
                observed_at=(observed_at_by_provider or {}).get(
                    provider, collection_window_seconds
                ),
                # Console's legacy/partial bid shape omits state.  This adapter
                # has always treated that shape as leasable; normalize the
                # transport quirk here rather than teaching the core about it.
                state="open" if _is_open_bid(raw_bid) else _bid_state(raw_bid),
                # ⛔ THE LINK THAT WAS MISSING. `PreferredSelection.EMPTIEST` has shipped
                # since v0.8.0 and ranked on a capacity that nothing ever supplied, so it
                # was selectable and inert — it silently degraded to cheapest and said so
                # in `selection_reason`.
                # ⚠ `None` here means UNMEASURED, and the core treats it as unrankable
                # rather than as full. A provider whose /status could not be read must not
                # sort last for being unreachable.
                # ⚠ The FETCH stays with the caller. This adapter translates Console's
                # response shape and nothing else; putting an HTTP call per bid inside a
                # bid loop is the hot-path cost that kept the funding primitive off the
                # deploy path for weeks.
                capacity=(capacity_by_provider or {}).get(provider),
                # The GROUP this bid is for. An order split into groups lets a provider
                # bid on the subset it can actually host, and the core needs the group to
                # tell two bids from one provider apart. None = the shape did not say.
                gseq=_extract_gseq(raw_bid),
            )
        except (TypeError, ValueError):
            continue
        auction.observe(observation)
        raw_by_key[bid_key] = raw_bid

    # ⭐ ANTI-AFFINITY. `already_selected` names the providers this DEPLOYMENT ROUND has
    # already placed on, so an N-region placement spreads across N distinct providers
    # instead of stacking on whichever one is cheapest N times over.
    #
    # ⛔ IT CHANGES THE ORDER, NEVER THE ELIGIBILITY — akash-lease-core's own words. If an
    # already-used provider is the ONLY bidder, it is still taken, because "taking it beats
    # failing to place". That is the correct trade at the measured ~93% provider fullness:
    # a soft spread wins placements, a hard exclusion loses them.
    #
    # ⚠ `None` and `frozenset()` are the same thing to the core (no spread requested), so a
    # caller that does not track placements is unaffected.
    #
    # ⛔⛔ AND IT ONLY ENGAGES UNDER `--select emptiest` WITH READABLE CAPACITY — measured,
    # against the assumption. The `taken` term lives INSIDE the core's `if emptiest and
    # readable:` branch; the else-branch is a plain `min(pool, key=price)` with no spread term.
    # So `already_selected` passed under CHEAPEST is silently INERT. The 2026-08-25 handoff
    # recorded the opposite ("needs NO capacity data ... the half that works today"); that is
    # false for v0.9.0. EMPTIEST is a PREREQUISITE for the spread, not a parallel lever.
    result = auction.evaluate(
        now=collection_window_seconds if evaluated_at is None else evaluated_at,
        already_selected=already_selected,
    )
    if result.status is not AuctionStatus.DECIDED or result.selected is None:
        return None, result
    return raw_by_key[result.selected.bid_key], result


def _cheapest_bid(pool: list, exclude: frozenset[str] = frozenset()):
    """Cheapest bid in ``pool`` whose provider is named and not in ``exclude``.

    Returns None when nothing qualifies, so a caller can widen the pool (retry
    with a smaller ``exclude``) rather than lease a bid it meant to skip. Bids
    with no provider are never returned — the caller could not lease them.
    """
    eligible = [b for b in pool if (p := _extract_provider(b)) and p not in exclude]
    if not eligible:
        return None
    return min(eligible, key=lambda b: _extract_bid_price(b)[0])


def _log_bid_table(
    bids: list,
    label: str,
    preferred: list[str] | None = None,
    backup: list[str] | None = None,
):
    preferred = preferred or []
    backup = backup or []
    has_allowlist = bool(preferred or backup)
    if not bids:
        _log(logging.INFO, f"  {label}: (none)")
        return
    _log(logging.INFO, f"  {label}: {len(bids)} bid(s)")
    for i, b in enumerate(bids):
        if not isinstance(b, dict):
            _log(logging.INFO, f"    [{i + 1}] (invalid bid entry)")
            continue
        provider = _extract_provider(b) or "unknown"
        state = _bid_state(b)
        suffix = ""
        if has_allowlist:
            suffix = f"  [{_classify_bid(provider, preferred, backup)}]"
        _log(
            logging.INFO,
            f"    [{i + 1}] provider={provider}  price={_fmt_price(b)}  state={state}{suffix}",
        )


def _inject_env_into_sdl(sdl_content: str, env_vars: list[str]) -> str:
    if not env_vars:
        return sdl_content
    override_keys = {v.split("=", 1)[0] for v in env_vars}
    env_match = re.search(r"^(\s+)env:\s*\n", sdl_content, re.MULTILINE)
    if env_match:
        indent = env_match.group(1)
        entry_indent = indent + "  "
        block_start = env_match.end()
        remaining = sdl_content[block_start:]
        lines = remaining.splitlines(keepends=True)
        kept = []
        consumed = 0
        for line in lines:
            stripped = line.rstrip("\n")
            if stripped and not stripped.startswith(entry_indent):
                break
            consumed += len(line)
            if any(re.match(r"\s*- " + re.escape(key) + r"=", line) for key in override_keys):
                continue
            kept.append(line)
        new_entries = "".join(f"{entry_indent}- {var}\n" for var in env_vars)
        return sdl_content[:block_start] + new_entries + "".join(kept) + remaining[consumed:]
    expose_match = re.search(r"^(\s+)expose:\s*\n", sdl_content, re.MULTILINE)
    if expose_match:
        indent = expose_match.group(1)
        new_block = f"{indent}env:\n"
        for var in env_vars:
            new_block += f"{indent}  - {var}\n"
        return (
            sdl_content[: expose_match.start()] + new_block + sdl_content[expose_match.start() :]
        )
    return sdl_content


def _resolve_tier(arg_value: list[str] | None, env_name: str) -> list[str]:
    """CLI args (when not None) override env var; trim & drop empties."""
    if arg_value is not None:
        return [p.strip() for p in arg_value if p and p.strip()]
    raw = os.environ.get(env_name, "")
    return [a.strip() for a in raw.split(",") if a.strip()]


def _resolve_sdl_path(sdl_path: str, gpu: bool) -> str:
    """When ``gpu`` is set, prefer a ``<stem>-gpu<suffix>`` sibling SDL.

    Returns the GPU variant path if it exists next to ``sdl_path``, otherwise
    the original path (with a warning). This makes the ``--gpu`` flag honest:
    "use the GPU variant SDL if available".
    """
    if not gpu:
        return sdl_path
    p = Path(sdl_path)
    variant = p.with_name(f"{p.stem}-gpu{p.suffix}")
    if variant.exists():
        _log(logging.INFO, f"GPU mode: using GPU SDL variant {variant}")
        return str(variant)
    _log(logging.WARNING, f"--gpu set but no GPU variant found at {variant}; using {sdl_path}")
    return sdl_path


def _prepare_sdl_content(
    sdl_path: str,
    image: str | None = None,
    env_vars: list[str] | None = None,
) -> str:
    """Read, validate, and apply image/SSH-key/env overrides to an SDL file.

    Shared by deploy() and update() so both paths transform the SDL identically.
    Returns the final SDL string ready to send to the Console API.
    """
    _log(logging.INFO, f"Reading SDL from {sdl_path}")
    sdl_path_obj = Path(sdl_path)
    if not sdl_path_obj.exists():
        raise RuntimeError(f"SDL file not found: {sdl_path}")

    with open(sdl_path_obj) as f:
        sdl_content = f.read()
    _log(logging.DEBUG, f"SDL content length: {len(sdl_content)} bytes")

    # RUN-SCOPE THE PROVENANCE KEY. The repo prefix proves which repo created a
    # deployment; this makes it prove which RUN. That is the difference between
    # reporting a suspected orphan and being able to close it: a concurrent run of THIS
    # repo also stamps `just-akash-*`, is also leaseless mid-create, and also lands in
    # the same window, so repo-level provenance can never single out one create's
    # residue. See _report_suspected_orphans.
    #
    # Stamped BEFORE validation so a bad rewrite fails here, against our own validator,
    # rather than as an opaque rejection from the Console API.
    sdl_content, _run_keys = stamp_run(sdl_content, _RUN_ID)
    if _run_keys:
        _log(logging.DEBUG, f"provenance keys for this run: {_run_keys}")

    try:
        validate_sdl(sdl_content)
    except SDLValidationError as e:
        _log(logging.ERROR, str(e))
        raise RuntimeError(str(e)) from e
    _log(logging.INFO, "SDL validation OK")

    if image:
        # Anchor to the YAML `image:` key at line start (after indentation) so a
        # comment that merely mentions "image:" can't be hijacked as the target.
        sdl_content, n_subs = re.subn(
            r"(?m)^(?P<indent>[ \t]*)image:[ \t]+\S[^\n]*",
            lambda m: f"{m.group('indent')}image: {image}",
            sdl_content,
            count=1,
        )
        if n_subs:
            _log(logging.INFO, f"Overrode image to: {image}")
        else:
            _log(logging.WARNING, f"--image {image} set but no 'image:' key found to override")

    if "PLACEHOLDER_SSH_PUBKEY_B64" in sdl_content:
        import base64

        ssh_pubkey = os.environ.get("SSH_PUBKEY", "")
        if not ssh_pubkey:
            raise RuntimeError(
                "SDL requires SSH_PUBKEY but it's not set. "
                "Add your public key to .env or export SSH_PUBKEY."
            )
        encoded = base64.b64encode(ssh_pubkey.encode()).decode()
        sdl_content = sdl_content.replace("PLACEHOLDER_SSH_PUBKEY_B64", encoded)
        _log(logging.INFO, "Injected SSH public key (base64) into SDL")

    if env_vars:
        # Reject malformed entries before they become a broken SDL env line
        # (mirrors the `inject` command's validation).
        for var in env_vars:
            key, sep, _ = var.partition("=")
            if not sep or not key:
                raise RuntimeError(f"Invalid --env {var!r}: expected KEY=VALUE")
        sdl_content = _inject_env_into_sdl(sdl_content, env_vars)
        _log(logging.INFO, f"Injected {len(env_vars)} env var(s) into SDL (provider-visible)")

    return sdl_content


def _check_wallet_credit(client: AkashConsoleAPI, deposit: float) -> None:
    """Pre-deploy wallet probe: emit a structured ``WALLET_*`` diagnostic so a caller
    can tell "out of deploy credit" from "provider capacity outage" (the two failures
    that otherwise look identical). Reads the Console DepositAuthorization credit
    straight from the chain via ``chain.deploy_credit``.

    Warn-only — NEVER raises. A failed probe (no creds, LCD down) emits
    ``WALLET_CREDIT_QUERY_FAILED`` and returns; the deploy proceeds regardless. The
    caller decides whether to act (e.g. pre-fail a CI job); just-akash does not abort.
    """
    if not enabled():
        # Skip the JWT-mint + LCD round-trip entirely when diagnostics are silent
        # (e.g. an interactive terminal) — the probe is only useful to a consumer.
        return
    from . import chain  # lazy: chain.py queries the public LCD only for this probe

    try:
        address = client.account_address()
    except RuntimeError as e:
        emit(
            Code.WALLET_CREDIT_QUERY_FAILED,
            "warning",
            f"could not resolve account address for credit check: {e}",
        )
        return
    try:
        credit = chain.deploy_credit(address)  # {denom: micro_units}
    except RuntimeError as e:
        emit(
            Code.WALLET_CREDIT_QUERY_FAILED,
            "warning",
            f"deploy-credit query failed (LCD unreachable?): {e}",
            account=address,
        )
        return

    # uact (Akash Credit Token, USD-pegged, 1e6 = $1) is the Console deploy currency.
    uact = credit.get("uact", 0)
    low_threshold_uact = int(max(deposit * 2, 1.0) * 1_000_000)  # cover >= 2 deposits
    if uact <= 0:
        emit(
            Code.WALLET_INSUFFICIENT_CREDIT,
            "error",
            "no deploy credit (DepositAuthorization spend_limits is empty/zero) — "
            "deploy will likely fail with HTTP 402",
            account=address,
            deploy_credit_uact=uact,
            deposit_usd=deposit,
        )
    elif uact < low_threshold_uact:
        emit(
            Code.WALLET_LOW_CREDIT,
            "warning",
            f"deploy credit is low ({uact / 1e6:.2f} ACT ≈ ${uact / 1e6:.2f}) — "
            "may not survive a long run",
            account=address,
            deploy_credit_uact=uact,
            deposit_usd=deposit,
            low_threshold_uact=low_threshold_uact,
        )


def deploy(
    sdl_path: str,
    gpu: bool = False,
    image: str | None = None,
    bid_wait: int = 60,
    bid_wait_retry: int = 120,
    env_vars: list[str] | None = None,
    preferred_providers: list[str] | None = None,
    backup_providers: list[str] | None = None,
    deposit: float = 5.0,
    select: str = "cheapest",
    already_selected: list[str] | None = None,
) -> dict:
    # deposit is user-controlled (--deposit); reject non-finite/non-positive
    # values before they reach json.dumps (which would emit invalid NaN/Infinity).
    if not math.isfinite(deposit) or deposit <= 0:
        raise RuntimeError(f"Invalid deposit {deposit!r}: must be a positive, finite USD amount.")

    if bid_wait_retry < bid_wait:
        raise ValueError(
            "bid_wait_retry is the total auction deadline and must be greater than "
            "or equal to bid_wait"
        )
    fallback_wait = bid_wait_retry - bid_wait
    # ⛔ VALIDATE BEFORE YOU SPEND. This raises on a bad --select, and it must raise HERE:
    #   resolving it next to its use (just before the auction) put it AFTER
    #   create_deployment, so a typo bought a deployment and a deposit before failing.
    #   Argument validation is free; do it before the first irreversible step.
    _selection = _resolve_selection(select)
    # ⭐ ANTI-AFFINITY INPUT, normalised beside the selection mode and for the same reason:
    # validate before the first irreversible step. Empty and None are the same thing to the
    # core — no spread requested — so a caller that does not track placements is unaffected.
    #
    # ⚠ Addresses are NOT validated against the allowlist. A caller naming a provider it
    # placed on last round is stating a FACT about its own history; rejecting an unknown
    # address would turn a spread hint into a failure, and the core already treats the set as
    # ordering-only. Blank entries are dropped so `--already-selected ""` cannot poison it.
    _already_selected = frozenset(a.strip() for a in (already_selected or []) if a and a.strip())
    AuctionPolicy(
        collection_window_seconds=bid_wait,
        fallback_window_seconds=fallback_wait,
    )

    from .wallet_pool import select_client_for_create

    required_uact = math.ceil(deposit * 1_000_000)
    wallet = select_client_for_create(required_uact, client_factory=AkashConsoleAPI)
    client = wallet.client
    if wallet.configured_keys > 1:
        _log(
            logging.INFO,
            f"WALLET policy={wallet.policy_version} selected_account={wallet.account} "
            f"available_uact={wallet.available_uact} distinct_accounts="
            f"{wallet.distinct_accounts}/{wallet.configured_keys}",
        )

    preferred = _resolve_tier(preferred_providers, "AKASH_PROVIDERS")
    backup = _resolve_tier(backup_providers, "AKASH_PROVIDERS_BACKUP")
    has_allowlist = bool(preferred or backup)

    _log(
        logging.INFO,
        f"CONFIG  sdl={sdl_path}  gpu={gpu}  image={image or '(default)'}  "
        f"preferred_window={bid_wait}s  fallback_deadline={bid_wait_retry}s total",
    )
    if preferred:
        _log(logging.INFO, f"PREFERRED_PROVIDERS ({len(preferred)}): {preferred}")
    if backup:
        _log(logging.INFO, f"BACKUP_PROVIDERS ({len(backup)}): {backup}")
    if not has_allowlist:
        _log(logging.INFO, "ALLOWED_PROVIDERS: (any — no allowlist set)")

    # Step 1: Read + validate + transform SDL (resolve GPU variant first)
    sdl_path = _resolve_sdl_path(sdl_path, gpu)
    _log(logging.INFO, "STEP 1: Preparing SDL")
    sdl_content = _prepare_sdl_content(sdl_path, image=image, env_vars=env_vars)
    _check_wallet_credit(client, deposit)

    # Step 2: Create deployment (with stale-deployment recovery)
    _log(
        logging.INFO,
        f"STEP 2: Creating deployment via Console API (escrow deposit: {deposit} USD)...",
    )
    # Stamped BEFORE the request so a deployment created by THIS call can be told from
    # one that already existed. See _report_suspected_orphans.
    _create_started = time.time()
    try:
        deployment_response = client.create_deployment(sdl_content, deposit=deposit)
    except RuntimeError as e:
        if "already exists" in str(e).lower():
            _log(
                logging.WARNING,
                "Deployment already exists — closing stale deployments and retrying...",
            )
            try:
                active = client.list_deployments(active_only=True)
                for dep in active:
                    # Only close deployments without a lease (stale from failed runs)
                    leases = dep.get("leases") or dep.get("lease", [])
                    if leases:
                        continue
                    stale_dseq = dep.get("dseq") or dep.get("deployment", {}).get("dseq")
                    if stale_dseq:
                        client.close_deployment(str(stale_dseq))
                        _log(logging.INFO, f"Closed stale deployment {stale_dseq}")
            except Exception as cleanup_err:
                _log(logging.ERROR, f"Stale deployment cleanup failed: {cleanup_err}")
            # Retry once after cleanup
            try:
                deployment_response = client.create_deployment(sdl_content, deposit=deposit)
            except RuntimeError as retry_err:
                _log(logging.ERROR, f"Create deployment FAILED after retry: {retry_err}")
                emit(
                    Code.DEPLOY_CREATE_FAILED,
                    "error",
                    f"create deployment failed after retry: {retry_err}",
                )
                _report_suspected_orphans(client, _create_started, _RUN_ID)
                raise RuntimeError(
                    f"Failed to create deployment after retry: {retry_err}"
                ) from retry_err
        else:
            _log(logging.ERROR, f"Create deployment FAILED: {e}")
            emit(Code.DEPLOY_CREATE_FAILED, "error", f"create deployment failed: {e}")
            _report_suspected_orphans(client, _create_started, _RUN_ID)
            raise RuntimeError(f"Failed to create deployment: {e}") from e

    dseq = deployment_response.get("dseq")
    _manifest_raw = deployment_response.get("manifest", "")
    manifest = _manifest_raw if isinstance(_manifest_raw, str) else ""
    if dseq is None:
        _log(
            logging.ERROR,
            f"No DSEQ in response: {json.dumps(deployment_response, default=str)}",
        )
        emit(
            Code.NO_DSEQ_RETURNED,
            "error",
            "create deployment returned no DSEQ",
            response_keys=list(deployment_response.keys())
            if isinstance(deployment_response, dict)
            else None,
        )
        raise RuntimeError(
            f"No DSEQ returned from API. Response: {json.dumps(deployment_response)}"
        )

    _log(logging.INFO, f"Deployment created  DSEQ={dseq}  manifest_len={len(manifest)}")
    _log(
        logging.DEBUG,
        f"Full deployment response: {json.dumps(deployment_response, default=str)[:500]}",
    )

    # Step 3: one equal-opportunity bid collection window, then one shared decision.
    start_time = time.time()
    collection_deadline = start_time + bid_wait
    deadline_iso = _fmt_window_deadline(collection_deadline)
    _log(
        logging.INFO,
        f"STEP 3: Collecting bids for {bid_wait}s window "
        f"(deadline={deadline_iso}, --bid-wait / AuctionPolicy.collection_window_seconds).",
    )
    bids: list = []
    poll_count = 0
    last_bid_count = -1
    first_seen_by_provider: dict[str, float] = {}

    # ⛔ A status line is written WITHOUT a trailing newline so the next poll can
    # overwrite it with \r. If anything else writes to stdout in between — a bid
    # arriving, an API error — its output lands on the SAME line, directly after
    # the progress text. `_status_open` tracks that state so the writer can close
    # the line first. Tracking it is not cosmetic: the corrupted line is the one a
    # reader consults when a poll fails, i.e. exactly when it must be legible.
    _status_open = False

    def _close_status_line() -> None:
        """Terminate a pending in-place status line before other stdout output."""
        nonlocal _status_open
        if _status_open:
            print(flush=True)
            _status_open = False

    def _log_below_status(level: int, msg: str) -> None:
        """`_log`, but never onto the tail of an unterminated status line."""
        _close_status_line()
        _log(level, msg)

    def _do_poll(total: int, window_deadline_iso: str) -> None:
        """Performs one poll, updates `bids`, prints progress + diff log line.

        ⛔ `total` and `window_deadline_iso` are PARAMETERS, not closure reads. The
        fallback round polls to `fallback_deadline`, and reading the collection
        window's `bid_wait` / `deadline_iso` here reported 100% against an ALREADY
        EXPIRED deadline while polling was still live — a progress bar that says
        finished while it is not is worse than none, because it is consulted to
        decide whether to keep waiting.
        """
        nonlocal poll_count, last_bid_count, bids, _status_open
        poll_count += 1
        elapsed = int(time.time() - start_time)
        bar = _fmt_window_progress_bar(elapsed, total)
        status_line = _fmt_window_status_line(
            elapsed=elapsed,
            total=total,
            deadline_iso=window_deadline_iso,
            poll_count=poll_count,
            bid_count=0,  # updated below when known
            bar=bar,
        )
        try:
            bids = client.get_bids(str(dseq))
        except RuntimeError as e:
            _log_below_status(
                logging.WARNING,
                f"  poll #{poll_count} @ {elapsed}s: API error: {e}",
            )
            print(f"\r{status_line}  api_error=1", end="", flush=True)
            _status_open = True
            return

        current_count = len(bids)
        if current_count != last_bid_count:
            last_bid_count = current_count
            if current_count == 0:
                _log_below_status(logging.DEBUG, f"  poll #{poll_count} @ {elapsed}s: 0 bids")
            else:
                _log_below_status(
                    logging.INFO,
                    f"  poll #{poll_count} @ {elapsed}s: {current_count} bid(s) received",
                )
                for i, b in enumerate(bids):
                    if not isinstance(b, dict):
                        continue
                    p = _extract_provider(b) or "unknown"
                    if p != "unknown":
                        first_seen_by_provider.setdefault(p, float(elapsed))
                    s = _bid_state(b)
                    tag = _classify_bid(p, preferred, backup)
                    _log_below_status(
                        logging.INFO,
                        f"    bid[{i}] provider={p}  price={_fmt_price(b)}  state={s}  [{tag}]",
                    )

        status_line = _fmt_window_status_line(
            elapsed=elapsed,
            total=total,
            deadline_iso=window_deadline_iso,
            poll_count=poll_count,
            bid_count=current_count,
            bar=bar,
        )
        if current_count > 0:
            # This branch prints WITH a newline, so no status line is left open.
            print(f"\r{status_line}  WAITING_FOR_LATE_BIDDERS_UNTIL_DEADLINE", flush=True)
            _status_open = False
        else:
            print(f"\r{status_line}  WAITING_FOR_FIRST_BID", end="", flush=True)
            _status_open = True

    def _poll_until(
        deadline: float,
        early_exit=None,
        *,
        total: int | None = None,
        window_deadline_iso: str | None = None,
    ) -> None:
        """Poll to `deadline`, reporting progress against THAT window.

        ⚠ `total` / `window_deadline_iso` default to the COLLECTION window so the
        first call reads unchanged. The fallback round MUST pass its own, or it
        reports the previous window's numbers.
        """
        while time.time() < deadline:
            _do_poll(
                bid_wait if total is None else total,
                deadline_iso if window_deadline_iso is None else window_deadline_iso,
            )
            if early_exit is not None and early_exit(bids):
                return
            time.sleep(5)

    _poll_until(collection_deadline)
    _close_status_line()

    def _filter_tier(current: list, tier: str) -> list:
        """Bids of a tier that are still leasable (state filter — issue #14)."""
        pool = []
        skipped_stale = 0
        for b in current:
            if not isinstance(b, dict):
                continue
            if _classify_bid(_extract_provider(b) or "", preferred, backup) != tier:
                continue
            if not _is_open_bid(b):
                skipped_stale += 1
                continue
            pool.append(b)
        if skipped_stale:
            _log(
                logging.WARNING,
                f"  Skipped {skipped_stale} {tier} bid(s) not in 'open' state",
            )
        return pool

    # ⛔ THE FETCH HAPPENS ONCE, AND ONLY WHEN ASKED. `select` defaults to "cheapest",
    #   so this block is skipped entirely on the default path — an HTTP round-trip per
    #   bidder inside a bid loop is the cost that kept the funding primitive off the
    #   deploy path for weeks. The result is reused for BOTH evaluations below; bidders
    #   do not change between them, and re-fetching would pay the cost twice to answer
    #   the same question.
    _capacity: dict[str, ProviderCapacity] | None = None
    if _selection is PreferredSelection.EMPTIEST:
        providers_bidding = [
            pr for pr in (_extract_provider(b) for b in bids if isinstance(b, dict)) if pr
        ]
        _capacity = capacity_by_provider(providers_bidding)
        _readable = sum(1 for c in _capacity.values() if c.available_fraction() is not None)
        # ⚠ SAY WHAT WAS MEASURED. If no provider's /status is readable the auction
        #   silently degrades to cheapest — correct behaviour, but invisible unless the
        #   coverage is reported. "emptiest requested" and "emptiest applied" are
        #   different facts.
        _log(
            logging.INFO,
            f"  EMPTIEST: capacity readable for {_readable}/{len(_capacity)} bidding "
            f"provider(s){' — falling back to cheapest' if not _readable else ''}",
        )
        # ⛔ THE LOG LINE ABOVE DOES NOT SURVIVE. The consuming workflow truncates
        #   deploy.log PER ROUND and accumulates only `akash-diag` lines into diag.jsonl
        #   (akash-runner.yml, "Accumulate just-akash's structured diagnostics across ALL
        #   rounds"). So coverage was discarded for every round but the last — on the one
        #   fact that says whether emptiest was APPLIED or merely REQUESTED.
        # ⚠ Emitted whenever coverage is INCOMPLETE, not only when it is zero. Partial
        #   coverage still ranks a subset while reading as "emptiest applied", and the
        #   caller cannot tell 3-of-3 from 1-of-3 without the numbers.
        if _readable < len(_capacity):
            emit(
                Code.SELECTION_EMPTIEST_DEGRADED,
                "warning",
                (
                    "emptiest requested; capacity unreadable for "
                    f"{len(_capacity) - _readable}/{len(_capacity)} bidding provider(s)"
                    + (" — auction fell back to cheapest" if not _readable else "")
                ),
                readable=_readable,
                bidding=len(_capacity),
                fully_degraded=not _readable,
                unreadable_providers=sorted(
                    p for p, c in _capacity.items() if c.available_fraction() is None
                ),
            )

    selected_bid, auction_result = _select_auction_bid(
        bids,
        preferred=preferred,
        backup=backup,
        collection_window_seconds=bid_wait,
        fallback_window_seconds=fallback_wait,
        evaluated_at=bid_wait,
        observed_at_by_provider=first_seen_by_provider,
        capacity_by_provider=_capacity,
        preferred_selection=_selection,
        already_selected=_already_selected,
    )
    if auction_result.status is AuctionStatus.COLLECTING:
        fallback_deadline = start_time + bid_wait_retry
        _log(
            logging.INFO,
            "  No preferred bid in the collection window; waiting for the first "
            f"eligible fallback until {bid_wait_retry}s total...",
        )

        def _eligible_open(current: list) -> bool:
            return any(
                isinstance(item, dict)
                and _is_open_bid(item)
                and _classify_bid(_extract_provider(item), preferred, backup)
                in ("PREFERRED", "BACKUP", "ACCEPTED")
                for item in current
            )

        _poll_until(
            fallback_deadline,
            early_exit=_eligible_open,
            total=bid_wait_retry,
            window_deadline_iso=_fmt_window_deadline(fallback_deadline),
        )
        _close_status_line()
        elapsed_for_decision = min(time.time() - start_time, float(bid_wait_retry))
        # ⛔ THE FALLBACK WINDOW CAN ADD BIDDERS THE FIRST FETCH NEVER SAW. The snapshot
        #   above covers only providers present before the first evaluation. If that
        #   returned COLLECTING, polling ran on and a NEW provider may now be bidding —
        #   and it would reach the auction with no capacity entry, i.e. unrankable, and
        #   EMPTIEST would silently ignore precisely the bid it was asked to consider.
        #   Fetch only the ADDITIONS, so the common case costs nothing.
        if _selection is PreferredSelection.EMPTIEST and _capacity is not None:
            late = [
                pr
                for pr in {_extract_provider(b) for b in bids if isinstance(b, dict)}
                if pr and pr not in _capacity
            ]
            if late:
                _capacity.update(capacity_by_provider(late))
                _log(
                    logging.INFO,
                    f"  EMPTIEST: fetched capacity for {len(late)} provider(s) that "
                    "arrived during the fallback window",
                )
                # ⛔ RE-EMIT, OR THE DEGRADED RECORD DESCRIBES A POPULATION THAT NO LONGER
                # EXISTS. The coverage event above was computed from the FIRST snapshot. A
                # late bidder whose capacity is unreadable degrades the selection AFTER that
                # event was emitted (or not emitted at all, if the first snapshot was fully
                # readable) — so the run could rank on partial capacity with no
                # SELECTION_EMPTIEST_DEGRADED record anywhere. Both halves were proven and
                # the value never travelled. Caught in review on #223.
                _readable = sum(
                    1 for c in _capacity.values() if c.available_fraction() is not None
                )
                if _readable < len(_capacity):
                    emit(
                        Code.SELECTION_EMPTIEST_DEGRADED,
                        "warning",
                        (
                            "emptiest requested; capacity unreadable for "
                            f"{len(_capacity) - _readable}/{len(_capacity)} bidding "
                            "provider(s) AFTER the fallback window admitted "
                            f"{len(late)} late bidder(s)"
                            + (" — auction fell back to cheapest" if not _readable else "")
                        ),
                        readable=_readable,
                        bidding=len(_capacity),
                        fully_degraded=not _readable,
                        after_fallback=True,
                        late_providers=sorted(late),
                        unreadable_providers=sorted(
                            p for p, c in _capacity.items() if c.available_fraction() is None
                        ),
                    )
        selected_bid, auction_result = _select_auction_bid(
            bids,
            preferred=preferred,
            backup=backup,
            collection_window_seconds=bid_wait,
            fallback_window_seconds=fallback_wait,
            evaluated_at=elapsed_for_decision,
            observed_at_by_provider=first_seen_by_provider,
            capacity_by_provider=_capacity,
            preferred_selection=_selection,
            already_selected=_already_selected,
        )
    selection_phase = (
        1 if auction_result.selection_reason == "cheapest_preferred" or not has_allowlist else 2
    )
    _log(
        logging.INFO,
        f"  Auction policy={auction_result.policy_version} "
        f"result={auction_result.selection_reason}",
    )

    elapsed_total = int(time.time() - start_time)

    # Post-polling diagnostics (run regardless of selection outcome): warn for
    # allowlisted providers that did not bid, mirroring legacy behavior so
    # operators see on-chain status even when selection ultimately fails.
    if has_allowlist and bids:
        bidding_providers = {_extract_provider(b) for b in bids if _extract_provider(b)}
        all_allowed = preferred + backup
        no_bid_from = [p for p in all_allowed if p not in bidding_providers]
        if no_bid_from:
            _log(
                logging.WARNING,
                f"NO BID FROM {len(no_bid_from)} allowlisted provider(s):",
            )
            for p in no_bid_from:
                tier = "preferred" if p in preferred else "backup"
                _log(logging.WARNING, f"  {p} ({tier})")
                try:
                    prov_info = client.get_provider(p)
                    if prov_info:
                        online = prov_info.get("isOnline")
                        valid = prov_info.get("isValidVersion")
                        uptime = prov_info.get("uptime1d")
                        stats = prov_info.get("stats") or {}
                        if not isinstance(stats, dict):
                            stats = {}
                        cpu = stats.get("cpu") or {}
                        if not isinstance(cpu, dict):
                            cpu = {}
                        mem = stats.get("memory") or {}
                        if not isinstance(mem, dict):
                            mem = {}
                        _log(
                            logging.WARNING,
                            f"    on-chain status: isOnline={online} "
                            f"isValidVersion={valid} uptime1d={uptime} "
                            f"cpu_avail={cpu.get('available')} "
                            f"cpu_active={cpu.get('active')} "
                            f"mem_avail={mem.get('available')} "
                            f"mem_active={mem.get('active')}",
                        )
                        # Classify WHY this provider didn't bid, from its on-chain
                        # status — a structured event a caller (CI/Sentry) can act on.
                        if online is False:
                            pcode, pmsg = Code.PROVIDER_OFFLINE, "provider reports offline"
                        elif valid is False:
                            pcode, pmsg = (
                                Code.PROVIDER_INVALID_VERSION,
                                "provider is running an invalid/disallowed version",
                            )
                        else:
                            pcode, pmsg = (
                                Code.PROVIDER_NO_BID,
                                "provider looks healthy on-chain but did not bid "
                                "(capacity full, SDL didn't match, or market timing)",
                            )
                        emit(
                            pcode,
                            "warning",
                            f"{pmsg}: {p}",
                            provider=p,
                            tier=tier,
                            isOnline=online,
                            isValidVersion=valid,
                            uptime1d=uptime,
                            cpu_available=cpu.get("available"),
                            cpu_active=cpu.get("active"),
                            mem_available=mem.get("available"),
                            mem_active=mem.get("active"),
                        )
                    else:
                        _log(
                            logging.WARNING,
                            "    on-chain status: NOT FOUND in provider registry",
                        )
                        emit(
                            Code.PROVIDER_UNKNOWN,
                            "warning",
                            f"allowlisted provider not in registry: {p}",
                            provider=p,
                            tier=tier,
                        )
                except RuntimeError as e:
                    _log(logging.WARNING, f"    on-chain status: query failed: {e}")
                    emit(
                        Code.PROVIDER_STATUS_QUERY_FAILED,
                        "warning",
                        f"on-chain status query failed for {p}: {e}",
                        provider=p,
                        tier=tier,
                        query_error=str(e)[:120],
                    )

    # Failure paths.
    if selected_bid is None:
        if not bids:
            _log(
                logging.ERROR,
                f"No bids after {poll_count} polls over {elapsed_total}s",
            )
            _log(
                logging.ERROR,
                "Possible causes: SDL unsatisfiable, providers offline, "
                "network partition, deposit too low, or no capacity on "
                "allowed providers",
            )
            _log(logging.INFO, f"Cleaning up deployment {dseq} (no bids)...")
            try:
                client.close_deployment(str(dseq))
                _log(logging.INFO, f"Deployment {dseq} closed after no bids received")
            except Exception as cleanup_err:
                _log(logging.ERROR, f"Cleanup of deployment {dseq} failed: {cleanup_err}")
            emit(
                Code.NO_BIDS_RECEIVED,
                "error",
                f"no bids received after {bid_wait}s",
                dseq=str(dseq),
                poll_count=poll_count,
                elapsed_s=elapsed_total,
                has_allowlist=has_allowlist,
                preferred=preferred,
                backup=backup,
            )
            raise RuntimeError(
                f"No bids received within {bid_wait}s. "
                "Your SDL may be unsatisfiable or all providers are busy."
            )
        # Bids exist but none from preferred or backup tiers.
        valid_bids = [b for b in bids if isinstance(b, dict)]
        if has_allowlist and not valid_bids:
            _log(logging.ERROR, f"All {len(bids)} bid(s) are invalid (non-dict entries)")
            _log(logging.INFO, f"Cleaning up deployment {dseq} (no valid bids)...")
            try:
                client.close_deployment(str(dseq))
            except Exception as cleanup_err:
                _log(logging.ERROR, f"Cleanup of deployment {dseq} failed: {cleanup_err}")
            emit(
                Code.BIDS_MALFORMED,
                "error",
                f"all {len(bids)} bid(s) were malformed (non-dict entries)",
                dseq=str(dseq),
            )
            raise RuntimeError("No valid bids received — all bid entries were malformed.")
        if valid_bids and not any(_extract_provider(b) for b in valid_bids):
            _log(logging.INFO, f"Cleaning up deployment {dseq} (bids have no provider)...")
            try:
                client.close_deployment(str(dseq))
            except Exception as cleanup_err:
                _log(logging.ERROR, f"Cleanup of deployment {dseq} failed: {cleanup_err}")
            raise RuntimeError("Selected bid has no provider address")
        # Bids from our own providers exist, but every one has aged out of the
        # 'open' state (issue #14). Without this branch the failure below would
        # misreport it as "non-allowed providers", which misleads operators —
        # the real cause is stale bids, not foreign ones.
        if has_allowlist:
            allowed_bids = [
                b
                for b in valid_bids
                if _classify_bid(_extract_provider(b), preferred, backup) != "FOREIGN"
            ]
        else:
            allowed_bids = valid_bids
        if allowed_bids and not any(_is_open_bid(b) for b in allowed_bids):
            states = sorted({_bid_state(b) for b in allowed_bids})
            providers = [_extract_provider(b) or "unknown" for b in allowed_bids]
            _log(
                logging.ERROR,
                f"All {len(allowed_bids)} bid(s) from your providers are no "
                f"longer open (states seen: {states})",
            )
            _log(logging.ERROR, f"  Providers: {providers}")
            _log(logging.INFO, f"Cleaning up deployment {dseq} (no open bids)...")
            try:
                client.close_deployment(str(dseq))
                _log(logging.INFO, f"Deployment {dseq} closed after no open bids")
            except Exception as cleanup_err:
                _log(logging.ERROR, f"Cleanup of deployment {dseq} failed: {cleanup_err}")
            emit(
                Code.BIDS_STALE,
                "error",
                f"{len(allowed_bids)} bid(s) from allowed providers but none still open",
                dseq=str(dseq),
                states=states,
                providers=providers,
            )
            raise RuntimeError(
                f"Received {len(allowed_bids)} bid(s) from your providers but none "
                f"are still open (states seen: {states}). Akash bids expire ~5 min "
                "after the order opens — retry the deployment to solicit fresh bids."
            )
        foreign = [_extract_provider(b) or "unknown" for b in bids]
        allowed_all = preferred + backup
        _log(logging.ERROR, f"All {len(bids)} bid(s) are from non-allowed providers")
        _log(logging.ERROR, f"  Preferred: {preferred}")
        _log(logging.ERROR, f"  Backup:    {backup}")
        _log(logging.ERROR, f"  Received from: {foreign}")
        _log(logging.INFO, f"Cleaning up deployment {dseq} (foreign bids only)...")
        try:
            client.close_deployment(str(dseq))
            _log(logging.INFO, f"Deployment {dseq} closed after foreign bids rejection")
        except Exception as cleanup_err:
            _log(logging.ERROR, f"Cleanup of deployment {dseq} failed: {cleanup_err}")
        emit(
            Code.BIDS_FOREIGN_ONLY,
            "error",
            f"{len(bids)} bid(s) but none from allowed providers",
            dseq=str(dseq),
            preferred=preferred,
            backup=backup,
            received_from=foreign,
        )
        # ⚠ DO NOT restore "check that your providers are online and have capacity" here.
        # A BID IS PROOF OF BOTH. A provider that bids has seen the order, is online, and
        # has declared it can serve that shape — so the one thing this failure can never
        # mean is that the providers are down or full. It means OUR allow-list rejected
        # what arrived.
        #
        # That advice sent at least four investigations to look at provider health.
        # Measured in Blazing-Back#1274 across 42 consecutive rejection rounds: a DFC-owned
        # `tier: preferred` provider had bid in 42 of 42 (one of two specific addresses in
        # 93% and 50% of rounds respectively). The providers were online, had capacity, and
        # bid — every single round. The advice was misleading in 100% of observed uses, and
        # the fix it eventually pointed to (Blazing-Back#1350) was to the ALLOW-LIST.
        # ⚠ The stale-bid branch above runs only when `allowed_bids` is NON-EMPTY, and it
        # is empty BY CONSTRUCTION whenever every bid is foreign — so an all-CLOSED foreign
        # set lands here, where "widen the allow-list" is true but NOT SUFFICIENT: those
        # specific bids can never be leased no matter who is allowed. Saying only "widen"
        # sends an operator to change config and re-run against an order whose bids have
        # already expired. Raised by CodeRabbit on #188 and confirmed against the branch.
        stale_note = (
            ""
            if any(_is_open_bid(b) for b in bids)
            else (
                " ALSO: every bid above has ALREADY EXPIRED (none is still open), so "
                "widening the allow-list is necessary but NOT sufficient — none of these "
                "bids can be leased. Widen it AND re-run to solicit fresh bids."
            )
        )
        raise RuntimeError(
            # ⛔ KEEP "NONE from our providers" — smoke_providers.py:943 CLASSIFIES on it
            # (`none from our providers` in its no-bid regex). Drop the phrase and an
            # allow-list rejection falls through to "deploy-failed", scoring OUR filter as
            # a PROVIDER FAIL — the exact mis-attribution this message is being fixed for.
            f"Received {len(bids)} bid(s) but NONE from our providers.\n"
            f"  Preferred: {preferred}\n"
            f"  Backup:    {backup}\n"
            f"  Received from: {foreign}\n"
            f"  Allowed total: {allowed_all}\n"
            "This is NOT a capacity or liveness problem — a bid is proof the provider was "
            "online and had capacity for this order shape. The mismatch is between the "
            "bidders above and the allow-list above. Widen the allow-list, or place the "
            "order somewhere a permitted provider will bid." + stale_note
        )

    # Selection success — log full bid table & per-tier breakdown.
    _log(
        logging.INFO,
        f"Bid polling complete: {len(bids)} total bid(s) in {elapsed_total}s",
    )
    _log_bid_table(bids, "ALL BIDS", preferred=preferred, backup=backup)

    # Step 4: per-tier bid tables.
    _log(logging.INFO, "STEP 4: Bid tier breakdown...")
    if has_allowlist:
        _log_bid_table(
            _filter_tier(bids, "PREFERRED"),
            "PREFERRED PROVIDERS",
            preferred=preferred,
            backup=backup,
        )
        if backup:
            _log_bid_table(
                _filter_tier(bids, "BACKUP"),
                "BACKUP PROVIDERS",
                preferred=preferred,
                backup=backup,
            )
        _log_bid_table(
            _filter_tier(bids, "FOREIGN"),
            "FOREIGN (rejected)",
            preferred=preferred,
            backup=backup,
        )
    else:
        _log_bid_table(
            [b for b in bids if isinstance(b, dict)],
            "ALL BIDS (no allowlist)",
            preferred=preferred,
            backup=backup,
        )

    # Step 5: announce selection (already chosen by state machine).
    phase_label = {
        1: "cheapest preferred after collection window",
        2: "first eligible fallback after preferred window",
    }
    selection_label = (
        phase_label[selection_phase]
        if has_allowlist
        else "first eligible bid after preferred window"
    )
    # ⚠ THE PHASE IS NOT THE POLICY. `phase_label` names WHEN the decision was taken
    #   ("cheapest preferred after collection window"); it hard-codes the tie-break as
    #   cheapest. Under EMPTIEST that sentence is simply false, and the deploy log is
    #   the only place an operator can see which policy actually ran.
    if _selection is PreferredSelection.EMPTIEST:
        selection_label = f"{selection_label} [selection: emptiest]"
    _log(
        logging.INFO,
        f"STEP 5: Selection made via {selection_label}",
    )
    # Show a compact ranking of the tier from which the winner came.
    if has_allowlist:
        winner_tier = _classify_bid(_extract_provider(selected_bid), preferred, backup)
        ranking_pool = _filter_tier(bids, winner_tier)
        ranking_label = winner_tier
    else:
        ranking_pool = [b for b in bids if isinstance(b, dict)]
        ranking_label = "ALL"
    for i, b in enumerate(sorted(ranking_pool, key=lambda b: _extract_bid_price(b)[0])):
        p = _extract_provider(b) or "unknown"
        marker = " <-- SELECTED" if b is selected_bid else ""
        _log(
            logging.INFO,
            f"  {ranking_label} rank[{i + 1}] provider={p}  price={_fmt_price(b)}{marker}",
        )

    provider = _extract_provider(selected_bid) or ""
    # ⛔ LEASE THE GROUP THAT WON, NOT GROUP 1. `create_lease` defaults `gseq=1`, and
    # every caller took that default — so a winning bid on group 2 created a lease
    # against group 1, which either fails or leases resources nobody bid on. Harmless
    # while orders are single-group; silently wrong the moment they are split, which is
    # the change that roughly DOUBLES the bid rate (74.9% of 191 vs 36.6% of 303).
    # ⚠ `or 1` is the deliberate floor: an unreadable shape keeps the historical
    # behaviour rather than crashing the deploy on a field Console may omit.
    lease_gseq = _extract_gseq(selected_bid) or 1
    price_amount, price_denom = _extract_bid_price(selected_bid)

    if not provider:
        _log(logging.INFO, f"Cleaning up deployment {dseq} (no provider in bid)...")
        try:
            client.close_deployment(str(dseq))
            _log(logging.INFO, f"Deployment {dseq} closed after no-provider bid")
        except Exception as cleanup_err:
            _log(logging.ERROR, f"Cleanup of deployment {dseq} failed: {cleanup_err}")
        raise RuntimeError("Selected bid has no provider address")

    _log(
        logging.INFO,
        f"SELECTED  provider={provider}  price={price_amount} {price_denom}  ({selection_label})",
    )

    # Step 6: Create lease (with stale-bid retry — issue #14).
    # A bid can expire between selection and the lease POST (the Console API
    # rejects it with 400 "no longer open"). On that specific failure,
    # re-fetch bids and fall to the next cheapest open bid, tier order
    # preserved (PREFERRED before BACKUP), before giving up.
    def _next_open_bid(fresh: list, exclude: set[str]):
        tiers = ["PREFERRED", "BACKUP"] if has_allowlist else ["ACCEPTED"]
        for tier in tiers:
            choice = _cheapest_bid(_filter_tier(fresh, tier), frozenset(exclude))
            if choice is not None:
                return choice
        return None

    def _poll_fresh_bid(
        order_dseq: str,
        wait_s: float,
        courtesy_s: float,
        interval_s: float,
        deprioritize: frozenset[str] = frozenset(),
    ):
        """Poll a freshly re-created order for the cheapest OPEN bid, tier-first.

        Preferred (or ACCEPTED when no allowlist) wins immediately; BACKUP is
        accepted only after ``courtesy_s``. Returns the bid dict, or None if
        nothing eligible appears within ``wait_s``. Reuses ``_filter_tier`` so
        only open bids are ever considered.

        ``deprioritize`` holds providers that already failed to lease THIS
        workload (issue #84). They are soft-skipped, never banned: without it
        the re-created order deterministically re-picks the cheapest bid, so
        when the provider that just failed is also the cheapest, the single
        bounded re-deploy round is guaranteed to reproduce the failure. A hard
        exclusion would over-correct — with n=2 we cannot prove the provider is
        at fault (versus Console-side order GC/propagation), and the allowlisted
        market is thin — so a de-prioritised provider is still leased if nothing
        else bids, after the same ``courtesy_s`` head start BACKUP already gets.

        Preference order: fresh preferred > fresh backup > failed preferred >
        failed backup. A provider that has NOT just failed always wins, tier
        order intact.
        """
        first_tier = "PREFERRED" if has_allowlist else "ACCEPTED"
        start = time.time()
        # Last NON-EMPTY pool seen per tier, kept so the soft skip stays soft
        # even if the courtesy window never opens (see the return below). Held
        # per tier so the fallback stays tier-first, and only overwritten when
        # non-empty so a transient get_bids() failure late in the loop cannot
        # erase the evidence and re-create the ban it exists to prevent.
        last_first: list = []
        last_backup: list = []
        while True:
            elapsed = time.time() - start
            if elapsed >= wait_s:
                break
            try:
                current = client.get_bids(str(order_dseq))
            except RuntimeError:
                current = []
            first_pool = _filter_tier(current, first_tier)
            backup_pool = _filter_tier(current, "BACKUP") if has_allowlist else []
            choice = _cheapest_bid(first_pool, deprioritize)
            if choice is None and elapsed >= courtesy_s:
                choice = (
                    _cheapest_bid(backup_pool, deprioritize)
                    # Only de-prioritised bids are on offer: the courtesy window
                    # gave a different provider its chance and none came, so take
                    # one rather than fail the deploy outright.
                    or _cheapest_bid(first_pool)
                    or _cheapest_bid(backup_pool)
                )
            if choice is not None:
                return choice
            if first_pool:
                last_first = first_pool
            if backup_pool:
                last_backup = backup_pool
            time.sleep(interval_s)
        # The wait expired without the courtesy window ever opening — reachable
        # only when courtesy_s was configured >= wait_s, which would otherwise
        # turn the soft skip into a silent hard ban (and make the "still
        # leasable if nothing else bids" log a lie). De-prioritisation is never
        # a ban, so honour a de-prioritised bid here rather than fail the
        # deploy over a misconfigured window — tier-first, as everywhere else.
        if not deprioritize:
            return None
        return (
            # Same preference order the courtesy branch uses: a provider that
            # has NOT just failed wins even across a tier drop, and only then
            # does tier order decide between the ones that did.
            _cheapest_bid(last_backup, deprioritize)
            or _cheapest_bid(last_first)
            or _cheapest_bid(last_backup)
        )

    def _redeploy_and_reselect(
        reason: str = "all bids stale",
        deprioritize: frozenset[str] = frozenset(),
    ) -> tuple[str, str, str, float, str, int]:
        """Close the stale/gone order and create a fresh one (issue #19), then select
        a fresh open bid on it.

        ``reason`` is the cause of the re-deploy (e.g. "all bids stale" for the
        issue-#14 path, or "order un-leaseable (404)" for the lease-CREATE 404) so
        the operator log names the actual failure mode, not a generic "stale".

        ``deprioritize`` names providers that already failed to lease this
        workload, so the fresh order prefers a different one (issue #84).

        Returns ``(dseq, manifest, provider, price_amount, price_denom)`` for the
        re-created order. Raises RuntimeError with an accurate cause if the round
        fails; any newly-created order is cleaned up before raising.
        """
        _log(
            logging.WARNING,
            f"Re-creating the order for fresh bids — {reason} (1 re-deploy round); "
            f"closing {dseq}...",
        )
        # Close the stale order BEFORE creating a new one — never leave two
        # funded orders on-chain. Transient close failures (often the same
        # Console flap that triggered the re-deploy) are retried; if the close
        # persistently fails we abort rather than double-fund escrow.
        closed = False
        for close_attempt in range(1, 4):
            try:
                client.close_deployment(str(dseq))
                _log(logging.INFO, f"  Stale order {dseq} closed")
                closed = True
                break
            except Exception as close_err:
                _log(
                    logging.WARNING,
                    f"  Close of stale order {dseq} failed "
                    f"(attempt {close_attempt}/3): {close_err}",
                )
                if close_attempt < 3:
                    time.sleep(2)
        if not closed:
            raise RuntimeError(
                f"could not close stale order {dseq} after 3 attempts — not "
                "re-deploying, to avoid double escrow. Close it manually: "
                f"just-akash destroy --dseq {dseq}"
            )
        # Same ambiguity as the initial create, and the same escrow at stake — this
        # path exists precisely because the first order went stale, so leaking a second
        # one here doubles the cost of the failure it is trying to recover from.
        _redeploy_started = time.time()
        try:
            redeploy_response = client.create_deployment(sdl_content, deposit=deposit)
        except RuntimeError as redeploy_err:
            _report_suspected_orphans(client, _redeploy_started, _RUN_ID)
            raise RuntimeError(f"re-deploy create failed: {redeploy_err}") from redeploy_err
        new_dseq = redeploy_response.get("dseq")
        if new_dseq is None:
            raise RuntimeError(
                f"re-deploy returned no DSEQ (response: "
                f"{json.dumps(redeploy_response, default=str)[:200]})"
            )
        _raw_manifest = redeploy_response.get("manifest", "")
        new_manifest = _raw_manifest if isinstance(_raw_manifest, str) else ""
        _log(
            logging.INFO,
            f"  Re-deployed: new order DSEQ={new_dseq} — fast-polling for fresh bids...",
        )
        wait_s, courtesy_s, interval_s = _redeploy_poll_window()
        if deprioritize:
            _log(
                logging.INFO,
                "  Preferring a provider other than "
                f"{', '.join(sorted(deprioritize))} on the fresh order "
                f"(still leasable if nothing else bids within {courtesy_s:g}s)",
            )
        fresh = _poll_fresh_bid(str(new_dseq), wait_s, courtesy_s, interval_s, deprioritize)
        fresh_provider = _extract_provider(fresh) if fresh is not None else None
        if fresh is None or not fresh_provider:
            try:
                client.close_deployment(str(new_dseq))
                _log(logging.INFO, f"  Re-created order {new_dseq} closed (no fresh bid)")
            except Exception as cleanup_err:
                _log(logging.ERROR, f"  Cleanup of {new_dseq} failed: {cleanup_err}")
            raise RuntimeError(f"no fresh open bid on re-created order {new_dseq}")
        amount, denom = _extract_bid_price(fresh)
        _log(
            logging.INFO,
            f"  Fresh bid selected: provider={fresh_provider}  price={amount} {denom} "
            "— leasing immediately",
        )
        # ⚠ The fresh bid's GROUP travels with it. A re-created order is a NEW order:
        # its winning bid may be for a different group than the one that won on the
        # order this replaced, so carrying the old `lease_gseq` forward would lease the
        # right provider against the wrong group — the exact defect this change fixes,
        # reintroduced one path over.
        return (
            str(new_dseq),
            new_manifest,
            fresh_provider,
            amount,
            denom,
            _extract_gseq(fresh) or 1,
        )

    _log(logging.INFO, "STEP 6: Creating lease...")
    max_lease_attempts = 3
    failed_providers: set[str] = set()
    lease_response = None
    # issue #19: one bounded re-deploy round. By the time a backup-only
    # market reaches lease creation, the selected bid is already
    # ~JUST_AKASH_BACKUP_FALLBACK_S old (bids expire ~5 min after the ORDER
    # opens), so a single Console flap (e.g. the ~35s 'JWT has invalid
    # claims' 400 — issue #18) can age the only bid past expiry. Re-fetching
    # bids on the SAME order then finds nothing open — every bid shares the
    # order's clock. Only a NEW order gets fresh bids, so when the stale-bid
    # retry runs out of open bids: close the order, re-create it, and lease
    # the first open allowlisted bid IMMEDIATELY (no phased patience — that
    # patience is what aged the first round past expiry).
    redeployed = False
    attempt = 0
    while True:
        attempt += 1
        try:
            lease_response = client.create_lease(
                dseq=str(dseq),
                provider=provider,
                manifest=manifest,
                gseq=lease_gseq,
            )
            break
        except RuntimeError as e:
            err_str = str(e).lower()
            stale = "no longer open" in err_str
            # 404 "no lease for deployment": the deployment's order became
            # un-leaseable during the bid-wait (Console GC/propagation, or a
            # shared-wallet sweep closing an un-leased deployment). Unlike a
            # stale bid, re-fetching bids on the SAME order can't recover it
            # (the order is gone), so this skips the same-order bid re-fetch
            # below and goes straight to the issue-#19 re-deploy round.
            no_order = "no lease for deployment" in err_str
            # Console API intermittently rejects lease creation with
            # 400 "JWT has invalid claims" while the bid itself is healthy
            # (transient auth flap on the Console side — see issue #18).
            # Retry the SAME provider after a short backoff instead of
            # advancing to the next bid. Message-match is intentional: the
            # structured fields are generic (code=bad_request,
            # type=client_error), so the message is the only signal.
            # 5s backoff (was 15s): the failing request itself burns ~35s,
            # and every second of backoff ages the bid toward its ~5-min
            # expiry (issue #19).
            transient_auth = "jwt has invalid claims" in err_str
            if transient_auth and attempt < max_lease_attempts:
                _log(
                    logging.WARNING,
                    f"Lease attempt {attempt}/{max_lease_attempts} hit a transient "
                    f"Console auth error (JWT claims) for provider={provider} — "
                    "retrying the same bid in 5s...",
                )
                time.sleep(5)
                continue
            if stale and attempt < max_lease_attempts:
                failed_providers.add(provider)
                _log(
                    logging.WARNING,
                    f"Lease attempt {attempt}/{max_lease_attempts} hit a stale bid "
                    f"(provider={provider}): re-fetching open bids...",
                )
                try:
                    fresh_bids = client.get_bids(str(dseq))
                except RuntimeError as poll_err:
                    _log(logging.WARNING, f"  Bid re-fetch failed: {poll_err}")
                    fresh_bids = []
                next_bid = _next_open_bid(fresh_bids, failed_providers)
                if next_bid is not None:
                    provider = _extract_provider(next_bid) or ""
                    # The group travels with the provider on EVERY reselection. This
                    # retry picks a different bid on the SAME order; the replacement may
                    # be for a different group, and keeping the previous `lease_gseq`
                    # would lease the new provider against the old group — the very
                    # defect this change exists to fix, surviving one path over.
                    lease_gseq = _extract_gseq(next_bid) or 1
                    price_amount, price_denom = _extract_bid_price(next_bid)
                    _log(
                        logging.INFO,
                        f"  Retrying lease with next open bid: provider={provider}  "
                        f"price={price_amount} {price_denom}",
                    )
                    continue
                _log(logging.WARNING, "  No other open bid available to retry with")
            if (stale or no_order) and not redeployed:
                # issue #19: every bid on this order has expired (bids share the
                # ORDER's ~5-min clock, so re-fetching the same order can't
                # recover), OR the order itself became un-leaseable (no_order
                # 404). Either way: close it, re-create once, lease a fresh bid.
                redeployed = True
                attempt = 0
                # issue #84: carry the provider that just failed into the fresh
                # order's bid selection, but ONLY on the 404 path. A `stale`
                # failure is the ORDER's ~5-min bid clock, which every bid
                # shares — it says nothing about the provider, and re-excluding
                # it on a NEW order would needlessly shrink an already-thin
                # allowlisted market. A 404 does carry provider-shaped signal,
                # so the fresh order prefers someone else (soft, not a ban —
                # see _poll_fresh_bid). Computed before the clear() below.
                deprioritize = (
                    frozenset(p for p in (failed_providers | {provider}) if p)
                    if no_order
                    else frozenset()
                )
                failed_providers.clear()
                try:
                    dseq, manifest, provider, price_amount, price_denom, lease_gseq = (
                        _redeploy_and_reselect(
                            reason="order un-leaseable (404 'no lease for deployment')"
                            if no_order
                            else "all bids stale",
                            deprioritize=deprioritize,
                        )
                    )
                except RuntimeError as redeploy_err:
                    emit(
                        Code.REDEPLOY_FAILED,
                        "error",
                        f"re-deploy round failed: {redeploy_err}",
                        dseq=str(dseq),
                    )
                    raise RuntimeError(
                        f"Failed to create lease after re-deploy: {redeploy_err}"
                    ) from redeploy_err
                continue
            _log(logging.ERROR, f"Lease creation FAILED: {e}")
            _log(logging.INFO, f"Cleaning up deployment {dseq}...")
            try:
                client.close_deployment(str(dseq))
                _log(logging.INFO, f"Deployment {dseq} closed after lease failure")
            except Exception as cleanup_err:
                _log(logging.ERROR, f"Cleanup of deployment {dseq} also failed: {cleanup_err}")
            emit(
                Code.LEASE_CREATE_FAILED,
                "error",
                f"lease creation failed: {e}",
                dseq=str(dseq),
                provider=provider,
            )
            raise RuntimeError(f"Failed to create lease: {e}") from e

    _log(logging.INFO, "Lease created successfully!")
    _log(
        logging.INFO,
        f"DEPLOYMENT SUMMARY  DSEQ={dseq}  "
        f"provider={provider}  price={price_amount} {price_denom}",
    )
    print("\nDeployment Summary:")
    print(f"  DSEQ: {dseq}")
    print(f"  Provider: {provider}")
    print(f"  Price: {price_amount} {price_denom}")
    wallet_account = wallet.account
    if wallet_account is None:
        try:
            resolved_account = client.account_address()
            wallet_account = resolved_account if isinstance(resolved_account, str) else None
        except RuntimeError:
            # Identity is lifecycle metadata, not a reason to fail a lease that already exists.
            wallet_account = None
    if wallet_account:
        print(f"  Wallet: {wallet_account}")
    if wallet.available_uact is not None:
        print(f"  Wallet available: {wallet.available_uact} uact")
    print(f"\nUse 'just-akash status --dseq {dseq}' to check deployment status")

    return {
        "dseq": dseq,
        "provider": provider,
        "price": price_amount,
        "price_denom": price_denom,
        "lease": lease_response,
        "wallet_account": wallet_account,
        "wallet_policy": wallet.policy_version,
    }


def update(
    dseq: str,
    sdl_path: str,
    image: str | None = None,
    env_vars: list[str] | None = None,
    api_key: str | None = None,
) -> dict:
    """Update an active deployment in place with a revised SDL.

    Reuses the same SDL preparation as deploy() (validation, image/SSH/env
    overrides) then PUTs to the Console API. The DSEQ and existing lease are
    preserved — no re-bid or new lease is created.
    """
    api_key = api_key or os.environ.get("AKASH_API_KEY")
    if not api_key:
        raise RuntimeError(
            "AKASH_API_KEY environment variable not set. Set AKASH_API_KEY before calling update."
        )

    client = AkashConsoleAPI(api_key)

    _log(
        logging.INFO,
        f"UPDATE  dseq={dseq}  sdl={sdl_path}  image={image or '(default)'}",
    )

    # Step 1: Read + validate + transform SDL (identical to deploy).
    _log(logging.INFO, "STEP 1: Preparing SDL")
    sdl_content = _prepare_sdl_content(sdl_path, image=image, env_vars=env_vars)

    # Step 2: Submit the in-place update.
    _log(logging.INFO, f"STEP 2: Submitting in-place update for deployment {dseq}...")
    try:
        result = client.update_deployment(str(dseq), sdl_content)
    except RuntimeError as e:
        _log(logging.ERROR, f"Update FAILED: {e}")
        raise RuntimeError(f"Failed to update deployment {dseq}: {e}") from e

    _log(
        logging.INFO,
        f"Deployment {dseq} updated in place (DSEQ and lease preserved).",
    )
    print(f"\nDeployment {dseq} updated.")
    print(f"Use 'just-akash status --dseq {dseq}' to verify the new revision is live.")

    return {"dseq": str(dseq), "result": result}


def deploy_main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Deploy to Akash Network",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sdl",
        default="sdl/cpu-backtest.yaml",
        help="Path to SDL file (default: sdl/cpu-backtest.yaml)",
    )
    parser.add_argument(
        "--gpu",
        action="store_true",
        help="Use GPU variant SDL if available",
    )
    parser.add_argument(
        "--image",
        help="Override container image",
    )
    parser.add_argument(
        "--bid-wait",
        type=int,
        default=60,
        help="Equal-opportunity auction window, 0-60 seconds (default: 60)",
    )
    parser.add_argument(
        "--bid-wait-retry",
        type=int,
        default=120,
        help="Deprecated compatibility option; ignored",
    )
    parser.add_argument(
        "--env",
        action="append",
        dest="env_vars",
        default=[],
        help="KEY=VALUE env var to inject into SDL (repeatable, provider-visible)",
    )
    parser.add_argument(
        "--provider",
        action="append",
        dest="preferred_providers",
        default=None,
        help="Preferred provider address (repeatable; overrides AKASH_PROVIDERS)",
    )
    parser.add_argument(
        "--backup-provider",
        action="append",
        dest="backup_providers",
        default=None,
        help="Backup provider address (repeatable; overrides AKASH_PROVIDERS_BACKUP)",
    )
    # Mirrors the flag on `just-akash deploy` (cli.py). Both entry points reach the same
    # deploy(), so a flag on only one of them is a trap for whoever uses the other.
    parser.add_argument(
        "--no-backup-fallback",
        action="store_true",
        dest="no_backup_fallback",
        help="Fail if no PREFERRED provider bids, instead of falling back to the backup "
        "tier. Ignores AKASH_PROVIDERS_BACKUP entirely.",
    )

    args = parser.parse_args()

    if args.no_backup_fallback and args.backup_providers:
        print(
            "Error: --no-backup-fallback and --backup-provider contradict each other. "
            f"--no-backup-fallback means 'the preferred provider or nothing', but "
            f"{len(args.backup_providers)} backup provider(s) were also given. Drop one.",
            file=sys.stderr,
        )
        sys.exit(2)
    # Without a preferred tier this flag inverts its own promise: has_allowlist becomes False
    # and deploy() accepts a bid from ANY provider. Same guard as cli.py -- both entry points
    # reach the same deploy(). Raised by Copilot on #145.
    if args.no_backup_fallback and not _resolve_tier(args.preferred_providers, "AKASH_PROVIDERS"):
        print(
            "Error: --no-backup-fallback requires a preferred provider, and none is "
            "configured. Pass --provider or set AKASH_PROVIDERS. Without one there is no "
            "allowlist at all, so the deploy would accept a bid from ANY provider -- the "
            "opposite of what this flag promises.",
            file=sys.stderr,
        )
        sys.exit(2)

    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("AKASH_DEBUG") else logging.INFO,
        format="",
    )

    try:
        deploy(
            sdl_path=args.sdl,
            gpu=args.gpu,
            image=args.image,
            bid_wait=args.bid_wait,
            bid_wait_retry=args.bid_wait_retry,
            env_vars=args.env_vars,
            preferred_providers=args.preferred_providers,
            # [] means "no backups, ignore the environment"; None means "read
            # AKASH_PROVIDERS_BACKUP". See _resolve_tier.
            backup_providers=[] if args.no_backup_fallback else args.backup_providers,
        )
        sys.exit(0)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
