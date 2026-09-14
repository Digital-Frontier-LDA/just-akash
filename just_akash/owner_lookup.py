"""Owner-lookup outcomes shared by e2e cleanup and production teardown (#363, #367).

⛔ AN API OUTAGE IS NOT OWNERSHIP EVIDENCE. Selecting a signer means asking each configured
Console credential a network question: which account it belongs to (`account_address()`), or
whether it can read a deployment (`get_deployment()`). Reading every failure as "not this one"
turns a connection-reset window into "no credential owns this lease" and HOLDS a lease the fleet
created (just-akash#362 run 34818093597 in e2e cleanup; the same shape in `wallet_pool` for the
teardown `runner-teardown.yml` runs, #367).

Three outcomes per credential, never two:
    MATCH      the question was answered and the answer is the one required: the ONLY thing that
               selects a signer
    MISMATCH   answered with a different address: the only thing that excludes a credential
    UNKNOWN    every attempt in a bounded budget failed in TRANSPORT: evidence of nothing
A credential that fails for a non-transport reason (401, 403, a malformed JWT) is UNREADABLE: not
retried, and never counted as an outage.

Verdict precedence when nothing matched: any UNKNOWN → OWNER_LOOKUP_UNREACHABLE; else any proven
MISMATCH → NO_CREDENTIAL_MATCHES_OWNER; else OWNER_LOOKUP_UNREADABLE (no address was ever read).
"""

from __future__ import annotations

import http.client
import time
from typing import TYPE_CHECKING, Any

from .api import AkashAPIError

if TYPE_CHECKING:
    from collections.abc import Callable

OWNER_LOOKUP_ATTEMPTS = 3
OWNER_LOOKUP_BACKOFF_SECONDS = 1.0
# A create-time-bound credential waits longer: 5 attempts, exponential 2+4+8+16 = 30s of backoff.
BOUND_OWNER_LOOKUP_ATTEMPTS = 5
BOUND_OWNER_LOOKUP_BACKOFF_SECONDS = 2.0
OWNER_LOOKUP_UNREACHABLE = "OWNER_LOOKUP_UNREACHABLE"
NO_CREDENTIAL_MATCHES_OWNER = "NO_CREDENTIAL_MATCHES_OWNER"
# Every credential failed for a non-transport reason: no address was read at all, so "no
# credential matches" would claim a comparison that never happened.
# ⚠ A MIX is still NO_CREDENTIAL_MATCHES_OWNER: a 403 credential beside one that returned a
# different address yields no-match although the 403 credential's address was never read. That is
# safe: a credential that cannot mint a JWT cannot close the lease either.
OWNER_LOOKUP_UNREADABLE = "OWNER_LOOKUP_UNREADABLE"


class OwnerLookupUnresolved(RuntimeError):
    """No configured credential was proven to be the owner; `verdict` says why.

    A RuntimeError, so every existing `except RuntimeError` handler keeps working."""

    def __init__(self, verdict: str, message: str) -> None:
        super().__init__(f"{verdict}: {message}")
        self.verdict = verdict


def is_transport_error(exc: BaseException) -> bool:
    """A failure that says nothing about which account the credential belongs to.

    Measured through the real AkashConsoleAPI with only urlopen patched (DEV7 on #366):
    connect-phase resets, timeouts and DNS failures arrive as "Connection error: …"; a reset or
    timeout while reading the body arrives raw; a truncated body arrives as
    http.client.IncompleteRead, which is NOT an OSError; 5xx, 524, 429 and 408 arrive as
    AkashAPIError."""
    if isinstance(exc, AkashAPIError):
        return exc.status is not None and (exc.status >= 500 or exc.status in (408, 429))
    if isinstance(exc, (ConnectionError, TimeoutError, OSError, http.client.IncompleteRead)):
        return True
    return isinstance(exc, RuntimeError) and str(exc).startswith("Connection error:")


def ask(call: Callable[[], Any], *, bound: bool = False) -> tuple[str, Any]:
    """("answered", value) | ("unknown", None) | ("unreadable", None) within the retry budget."""
    attempts = BOUND_OWNER_LOOKUP_ATTEMPTS if bound else OWNER_LOOKUP_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            return "answered", call()
        except Exception as exc:  # noqa: BLE001 - classified below, never read as a mismatch
            if not is_transport_error(exc):
                return "unreadable", None
            if attempt < attempts:
                time.sleep(
                    BOUND_OWNER_LOOKUP_BACKOFF_SECONDS * 2 ** (attempt - 1)
                    if bound
                    else OWNER_LOOKUP_BACKOFF_SECONDS * attempt
                )
    return "unknown", None


def lookup_owner(candidate: Any, *, bound: bool = False) -> tuple[str, str | None]:
    """("address"|"unknown"|"unreadable", address) for one credential's account lookup."""
    kind, value = ask(candidate.account_address, bound=bound)
    return ("address", value) if kind == "answered" else (kind, None)


def unresolved_verdict(kinds: set[str]) -> str:
    """The typed hold when nothing matched. `kinds` holds each credential's outcome; a proven
    mismatch is recorded as "address"."""
    if "unknown" in kinds:
        return OWNER_LOOKUP_UNREACHABLE
    if "address" in kinds:
        return NO_CREDENTIAL_MATCHES_OWNER
    return OWNER_LOOKUP_UNREADABLE
