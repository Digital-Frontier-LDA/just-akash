"""Structured diagnostic events — machine-readable "why it didn't happen".

Every diagnosable reason a deploy/bid/lease didn't happen is emitted as one JSON
line to stderr with a stable reason code + evidence, so a caller (CI, a Sentry
shipper, a log processor) can ingest and classify it. just-akash stays dependency-
free (stdlib ``json``) and the events are **additive**: they never change program
behavior — the operation proceeds exactly as before; the caller decides whether to
act on an event (e.g. pre-fail a CI job on ``WALLET_INSUFFICIENT_CREDIT``).

Envelope (one JSON object per line, stderr only — stdout owns ``--json`` data)::

    {"type":"akash-diag","level":"error","code":"PROVIDER_NO_BID",
     "message":"allowlisted provider did not bid","dseq":"12345",
     "context":{"provider":"akash1...","tier":"preferred","isOnline":true,...}}

Gating (mirrors the ``use_json = args.json or not sys.stdout.isatty()`` convention,
applied to stderr): emit when stderr is not a tty (CI/pipes) **or** when
``AKASH_DIAGNOSTICS`` is ``1``/``json``/``true``; silent in an interactive terminal
so humans keep the existing ``_log``/``Error:`` prose. ``AKASH_DIAGNOSTICS=off``
forces it off. ``emit`` never raises — a diagnostic failure must never break the
operation it reports on.

See ``docs/diagnostics.md`` for the full code table and the caller bridge (how a
runner turns ``WALLET_INSUFFICIENT_CREDIT`` into a GitHub ``::error`` / Sentry event).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

_TYPE = "akash-diag"
_LEVELS = ("warning", "error")


class Code:
    """Stable reason codes (``UPPER_SNAKE``), grouped by the condition they signal.

    Aligned with the smoke outcome/``diag`` taxonomy (``smoke_providers.py``) so a
    caller consumes one vocabulary across deploy-time and smoke-time events.
    """

    # Wallet / credit — pre-deploy, via chain.deploy_credit + account_address.
    # The highest-leverage codes: they disambiguate "our wallet is empty" from
    # "the provider market had nothing to offer" (otherwise indistinguishable).
    WALLET_INSUFFICIENT_CREDIT = "WALLET_INSUFFICIENT_CREDIT"  # 0 credit / no grant → would 402
    WALLET_LOW_CREDIT = "WALLET_LOW_CREDIT"  # below threshold; may still succeed
    WALLET_CREDIT_QUERY_FAILED = "WALLET_CREDIT_QUERY_FAILED"  # LCD unreachable / query errored

    # Provider health — from deploy's no-bid on-chain-status block.
    PROVIDER_OFFLINE = "PROVIDER_OFFLINE"  # isOnline false
    PROVIDER_INVALID_VERSION = "PROVIDER_INVALID_VERSION"  # isValidVersion false
    PROVIDER_NO_CAPACITY = "PROVIDER_NO_CAPACITY"  # cpu/mem available too low for the SDL
    PROVIDER_NO_BID = "PROVIDER_NO_BID"  # on-chain looks OK but no bid (catch-all)
    PROVIDER_STATUS_QUERY_FAILED = "PROVIDER_STATUS_QUERY_FAILED"  # couldn't read status
    PROVIDER_UNKNOWN = "PROVIDER_UNKNOWN"  # not in the provider registry

    # Deploy lifecycle — one per deploy.py failure path.
    NO_BIDS_RECEIVED = "NO_BIDS_RECEIVED"
    BIDS_FOREIGN_ONLY = "BIDS_FOREIGN_ONLY"  # only non-allowed providers bid
    BIDS_STALE = "BIDS_STALE"  # bids aged out of 'open'
    BIDS_MALFORMED = "BIDS_MALFORMED"  # all bid entries failed schema
    DEPLOY_CREATE_FAILED = "DEPLOY_CREATE_FAILED"
    NO_DSEQ_RETURNED = "NO_DSEQ_RETURNED"
    LEASE_CREATE_FAILED = "LEASE_CREATE_FAILED"  # incl. the 404 "no lease for deployment" flake
    REDEPLOY_FAILED = "REDEPLOY_FAILED"
    SDL_ERROR = "SDL_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"  # missing API key / bad deposit / bad --env

    # Reliability — post-deploy / smoke; the "external sweep reaps the lease" symptom.
    LEASE_DOWN = "LEASE_DOWN"


def _enabled() -> bool:
    """Emit when stderr is piped (CI/logs) or ``AKASH_DIAGNOSTICS`` opts in; off in a
    human terminal unless explicitly enabled."""
    flag = os.environ.get("AKASH_DIAGNOSTICS", "").strip().lower()
    if flag in ("off", "0", "false"):
        return False
    if flag in ("1", "json", "true", "on"):
        return True
    return not sys.stderr.isatty()


def enabled() -> bool:
    """Public alias of :func:`_enabled` — whether diagnostic events are currently
    emitted. Callers use this to gate *expensive* probes (e.g. the pre-deploy wallet
    check, which does network calls) so an interactive terminal pays no latency when
    diagnostics are silent."""
    return _enabled()


def emit(
    code: str,
    level: str,
    message: str,
    *,
    dseq: str | None = None,
    **context: Any,
) -> None:
    """Emit one structured diagnostic event as a JSON line to stderr.

    ``level`` is ``"warning"`` (degradation/risk; the operation continues) or
    ``"error"`` (the operation failed). ``context`` carries the evidence (provider,
    on-chain fields, credit amounts, bid states…). No-op when disabled; never raises.
    """
    if not _enabled():
        return
    event: dict[str, Any] = {
        "type": _TYPE,
        "level": level if level in _LEVELS else "warning",
        "code": code,
        "message": message,
    }
    if dseq is not None:
        event["dseq"] = str(dseq)
    if context:
        # Drop None values so the event stays compact; keep 0/False (real evidence).
        event["context"] = {k: v for k, v in context.items() if v is not None}
    try:
        sys.stderr.write(json.dumps(event) + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001, S110 — diagnostics must never break the op
        pass
