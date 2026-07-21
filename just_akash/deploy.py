#!/usr/bin/env python3
"""
Multi-step Akash deployment orchestrator.

Workflow:
1. Read SDL file
2. Create deployment via Console API
3. Poll for bids using a 3-phase tiered selection state machine:
   - Phase 1 (preferred-only patience, [0, T1]): collect bids; pick cheapest
     preferred at end of window if any.
   - Phase 2 (preferred-grace, [T1, T1+T2]): continue collecting; the moment a
     preferred bid appears, accept it immediately (first-wins).
   - Phase 3 (backup fallback): pick cheapest backup from bids collected across
     phases 1+2.
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
from datetime import datetime, timezone
from pathlib import Path

from ._diagnostics import Code, emit, enabled
from .api import (
    AkashConsoleAPI,
    _extract_bid_price,
    _extract_provider,
)
from .sdl_validate import SDLValidationError, validate_sdl

logger = logging.getLogger("akash.deploy")


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(level: int, msg: str):
    logger.log(level, f"[{_ts()}] {msg}")
    if level >= logging.INFO:
        print(f"[{_ts()}] {msg}", flush=True)


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
) -> dict:
    api_key = os.environ.get("AKASH_API_KEY")
    if not api_key:
        raise RuntimeError(
            "AKASH_API_KEY environment variable not set. "
            "Please set your API key: export AKASH_API_KEY='your-key'"
        )

    # deposit is user-controlled (--deposit); reject non-finite/non-positive
    # values before they reach json.dumps (which would emit invalid NaN/Infinity).
    if not math.isfinite(deposit) or deposit <= 0:
        raise RuntimeError(f"Invalid deposit {deposit!r}: must be a positive, finite USD amount.")

    client = AkashConsoleAPI(api_key)

    preferred = _resolve_tier(preferred_providers, "AKASH_PROVIDERS")
    backup = _resolve_tier(backup_providers, "AKASH_PROVIDERS_BACKUP")
    has_allowlist = bool(preferred or backup)

    _log(
        logging.INFO,
        f"CONFIG  sdl={sdl_path}  gpu={gpu}  image={image or '(default)'}  "
        f"bid_wait={bid_wait}s  bid_wait_retry={bid_wait_retry}s",
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
                raise RuntimeError(
                    f"Failed to create deployment after retry: {retry_err}"
                ) from retry_err
        else:
            _log(logging.ERROR, f"Create deployment FAILED: {e}")
            emit(Code.DEPLOY_CREATE_FAILED, "error", f"create deployment failed: {e}")
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

    # Step 3: 3-phase bid polling and selection.
    _log(
        logging.INFO,
        f"STEP 3: Polling for bids (3-phase: preferred-only [{bid_wait}s] → "
        f"preferred-grace [{bid_wait_retry}s] → backup fallback)...",
    )
    start_time = time.time()
    bids: list = []
    poll_count = 0
    last_bid_count = -1

    def _has_open_tier_bid(current: list, tier: str) -> bool:
        return any(
            isinstance(b, dict)
            and _is_open_bid(b)
            and _classify_bid(_extract_provider(b) or "", preferred, backup) == tier
            for b in current
        )

    def _has_preferred_bid(current: list) -> bool:
        return _has_open_tier_bid(current, "PREFERRED")

    def _has_any_valid_bid(current: list) -> bool:
        return any(isinstance(b, dict) and _is_open_bid(b) for b in current)

    def _do_poll() -> None:
        """Performs one poll, updates `bids`, prints progress + diff log line."""
        nonlocal poll_count, last_bid_count, bids
        poll_count += 1
        elapsed = int(time.time() - start_time)
        try:
            bids = client.get_bids(str(dseq))
        except RuntimeError as e:
            _log(
                logging.WARNING,
                f"  poll #{poll_count} @ {elapsed}s: API error: {e}",
            )
            print(
                f"\r  Waiting for bids... {elapsed}s (poll #{poll_count})",
                end="",
                flush=True,
            )
            return

        current_count = len(bids)
        if current_count != last_bid_count:
            last_bid_count = current_count
            if current_count == 0:
                _log(logging.DEBUG, f"  poll #{poll_count} @ {elapsed}s: 0 bids")
            else:
                _log(
                    logging.INFO,
                    f"  poll #{poll_count} @ {elapsed}s: {current_count} bid(s) received",
                )
                for i, b in enumerate(bids):
                    if not isinstance(b, dict):
                        continue
                    p = _extract_provider(b) or "unknown"
                    s = _bid_state(b)
                    tag = _classify_bid(p, preferred, backup)
                    _log(
                        logging.INFO,
                        f"    bid[{i}] provider={p}  price={_fmt_price(b)}  state={s}  [{tag}]",
                    )

        if current_count > 0:
            print(f"\r  {current_count} bid(s) received after {elapsed}s", flush=True)
        else:
            print(
                f"\r  Waiting for bids... {elapsed}s (poll #{poll_count})",
                end="",
                flush=True,
            )

    def _poll_until(deadline: float, early_exit=None) -> None:
        while time.time() < deadline:
            _do_poll()
            if early_exit is not None and early_exit(bids):
                return
            time.sleep(5)

    phase1_deadline = start_time + bid_wait
    phase2_deadline = phase1_deadline + bid_wait_retry

    # Phase 1: preferred-only patience — collect bids for full T1 window.
    _log(logging.INFO, f"  Phase 1 (preferred-only patience): waiting up to {bid_wait}s...")
    _poll_until(phase1_deadline)
    print()

    selected_bid = None
    selection_phase = 0

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

    if has_allowlist:
        preferred_phase1 = _filter_tier(bids, "PREFERRED")
        if preferred_phase1:
            selected_bid = min(preferred_phase1, key=lambda b: _extract_bid_price(b)[0])
            selection_phase = 1
    else:
        accepted_phase1 = _filter_tier(bids, "ACCEPTED")
        if accepted_phase1:
            selected_bid = min(accepted_phase1, key=lambda b: _extract_bid_price(b)[0])
            selection_phase = 1

    # Phase 2: preferred-grace — only enter if no selection yet AND
    # (backup tier configured OR no bids at all in phase 1). The "no bids"
    # condition preserves today's retry behavior when backup is unset.
    if selected_bid is None:
        enter_phase2 = bool(backup) or len(bids) == 0
        if enter_phase2:
            label = "preferred-grace" if has_allowlist else "retry"
            _log(
                logging.WARNING,
                f"  Phase 2 ({label}): no preferred bid yet — "
                f"waiting up to {bid_wait_retry}s for first preferred...",
            )
            if has_allowlist and backup:
                # Akash bids expire ~5 min after the order opens. If the full
                # grace outlasts that, phase 3 can only ever see stale backup
                # bids (issue #14) — so once open backup bids exist, stop
                # waiting for a preferred bid at the fallback safety mark.
                fallback_after = start_time + _backup_fallback_grace_s()
                fallback_cut = False

                def _phase2_exit(current: list) -> bool:
                    nonlocal fallback_cut
                    if _has_preferred_bid(current):
                        return True
                    if time.time() >= fallback_after and _has_open_tier_bid(current, "BACKUP"):
                        if not fallback_cut:
                            fallback_cut = True
                            _log(
                                logging.WARNING,
                                f"  Cutting preferred-grace short at "
                                f"{int(time.time() - start_time)}s: open BACKUP bid(s) "
                                f"available and bids expire ~5min after order creation",
                            )
                        return True
                    return False

                early_exit = _phase2_exit
            elif has_allowlist:
                early_exit = _has_preferred_bid
            else:
                early_exit = _has_any_valid_bid
            _poll_until(phase2_deadline, early_exit=early_exit)
            print()

            if has_allowlist:
                preferred_now = _filter_tier(bids, "PREFERRED")
                if preferred_now:
                    selected_bid = min(preferred_now, key=lambda b: _extract_bid_price(b)[0])
                    selection_phase = 2
            else:
                accepted_now = _filter_tier(bids, "ACCEPTED")
                if accepted_now:
                    selected_bid = min(accepted_now, key=lambda b: _extract_bid_price(b)[0])
                    selection_phase = 2

    # Phase 3: cheapest backup fallback (across bids collected in phases 1+2).
    if selected_bid is None and backup:
        backup_bids_all = _filter_tier(bids, "BACKUP")
        if backup_bids_all:
            selected_bid = min(backup_bids_all, key=lambda b: _extract_bid_price(b)[0])
            selection_phase = 3

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
                f"no bids received after {bid_wait + bid_wait_retry}s",
                dseq=str(dseq),
                poll_count=poll_count,
                elapsed_s=elapsed_total,
                has_allowlist=has_allowlist,
                preferred=preferred,
                backup=backup,
            )
            raise RuntimeError(
                f"No bids received within {bid_wait + bid_wait_retry}s. "
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
        raise RuntimeError(
            f"Received {len(bids)} bid(s) but NONE from our providers.\n"
            f"  Preferred: {preferred}\n"
            f"  Backup:    {backup}\n"
            f"  Received from: {foreign}\n"
            "Check that your providers are online and have capacity. "
            f"Allowed total: {allowed_all}"
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
        1: "phase 1: cheapest preferred",
        2: "phase 2: first preferred (grace)",
        3: "phase 3: cheapest backup (fallback)",
    }
    _log(
        logging.INFO,
        f"STEP 5: Selection made via {phase_label[selection_phase]}",
    )
    # Show a compact ranking of the tier from which the winner came.
    if selection_phase == 3:
        ranking_pool = _filter_tier(bids, "BACKUP")
        ranking_label = "BACKUP"
    elif has_allowlist:
        ranking_pool = _filter_tier(bids, "PREFERRED")
        ranking_label = "PREFERRED"
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
        f"SELECTED  provider={provider}  price={price_amount} {price_denom}  "
        f"({phase_label[selection_phase]})",
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
        # De-prioritised bids seen on the last poll, kept so the soft-skip stays
        # soft even if the courtesy window never opens (see the return below).
        skipped: list = []
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
            skipped = first_pool + backup_pool
            time.sleep(interval_s)
        # The wait expired without the courtesy window ever opening — reachable
        # only when courtesy_s was configured >= wait_s, which would otherwise
        # turn the soft skip into a silent hard ban (and make the "still
        # leasable if nothing else bids" log a lie). De-prioritisation is never
        # a ban, so honour a de-prioritised bid here rather than fail the
        # deploy over a misconfigured window.
        return _cheapest_bid(skipped) if deprioritize else None

    def _redeploy_and_reselect(
        reason: str = "all bids stale",
        deprioritize: frozenset[str] = frozenset(),
    ) -> tuple[str, str, str, float, str]:
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
        try:
            redeploy_response = client.create_deployment(sdl_content, deposit=deposit)
        except RuntimeError as redeploy_err:
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
        return str(new_dseq), new_manifest, fresh_provider, amount, denom

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
                    dseq, manifest, provider, price_amount, price_denom = _redeploy_and_reselect(
                        reason="order un-leaseable (404 'no lease for deployment')"
                        if no_order
                        else "all bids stale",
                        deprioritize=deprioritize,
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
    print(f"\nUse 'just-akash status --dseq {dseq}' to check deployment status")

    return {
        "dseq": dseq,
        "provider": provider,
        "price": price_amount,
        "price_denom": price_denom,
        "lease": lease_response,
    }


def update(
    dseq: str,
    sdl_path: str,
    image: str | None = None,
    env_vars: list[str] | None = None,
) -> dict:
    """Update an active deployment in place with a revised SDL.

    Reuses the same SDL preparation as deploy() (validation, image/SSH/env
    overrides) then PUTs to the Console API. The DSEQ and existing lease are
    preserved — no re-bid or new lease is created.
    """
    api_key = os.environ.get("AKASH_API_KEY")
    if not api_key:
        raise RuntimeError(
            "AKASH_API_KEY environment variable not set. "
            "Please set your API key: export AKASH_API_KEY='your-key'"
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
        help="Phase 1 (preferred-only) window seconds (default: 60)",
    )
    parser.add_argument(
        "--bid-wait-retry",
        type=int,
        default=120,
        help="Phase 2 (preferred-grace) window seconds (default: 120)",
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

    args = parser.parse_args()

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
            backup_providers=args.backup_providers,
        )
        sys.exit(0)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
