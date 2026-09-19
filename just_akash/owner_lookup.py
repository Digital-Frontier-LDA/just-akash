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
import math
import os
import threading
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

# ⛔ THE WHOLE OWNER-LOOKUP PHASE IS WALL-CLOCK BOUNDED (#370). The per-attempt budgets above
# bound RETRIES, not time: a full Console stall can cost CONSOLE_HTTP_TIMEOUT (180s, #369) per
# attempt, and N configured keys multiply it — 8 keys ≈ 79 minutes of a job that must then be
# killed mid-cleanup with no typed verdict. One deadline ends the phase instead: on expiry the
# caller raises/returns the typed OWNER_LOOKUP_UNREACHABLE, recording the attempts each key
# actually made. Sized so deadline + margin stays inside every invoking job's timeout-minutes;
# the structural test in tests/test_owner_lookup_deadline.py pins that relationship.
OWNER_LOOKUP_DEADLINE_SECONDS = 600.0
# Headroom for the close and the settlement audit that follow a successful lookup — the
# deadline must not consume the job budget the cleanup itself needs.
OWNER_LOOKUP_JOB_MARGIN_SECONDS = 240.0

# ⛔ THE BUDGET IS PER JOB STEP, NOT PER PROCESS (#378 review). A step that runs
# resolve-owner and then a retry loop of destroys starts a NEW process per command, and a
# per-process Deadline let each of them spend the full 600s again — 40+ minutes of lookup
# inside a 30-minute job. A step therefore exports OWNER_LOOKUP_DEADLINE_AT (epoch seconds,
# wall clock) once, before its first lookup; every process in the step honours it as an
# ABSOLUTE ceiling (min with its own budget), so retries share one budget instead of
# minting new ones.
OWNER_LOOKUP_DEADLINE_AT_ENV = "OWNER_LOOKUP_DEADLINE_AT"

# The exit code for "the shared lookup budget is exhausted, UNREACHABLE": EX_TEMPFAIL.
# Distinct from every other failure so a retry loop can tell "retrying is waste — the
# step's budget is gone" from "the destroy failed, try again". A LATER run (next job,
# next sweep) gets a fresh budget; this step must not burn more of it.
OWNER_LOOKUP_UNREACHABLE_EXIT_CODE = 75

# ⛔ ONE CEILING PER CLEANUP, BOUNDED BY THE STEP'S OWN END (#378 review). A step that loops
# many cleanups in ONE process (provider smoke: one probe per provider, 557–717s measured)
# cannot share a 600s ceiling exported at step start: every cleanup after the first 600s
# inherits an already-expired ceiling and exits 75 in under a second without a single
# lookup. So each cleanup mints its own ceiling when it starts, `min(now + budget, step
# end)`, shared by that cleanup's retries. The step end is exported ONCE as an END bound,
# `OWNER_LOOKUP_STEP_DEADLINE_AT = step start + timeout-minutes x 60 - margin`, so a late
# cleanup gets only what the step really has left and ends typed instead of being killed
# by the runner mid-close. tests/test_owner_lookup_deadline.py pins the literal against
# each step's timeout-minutes.
OWNER_LOOKUP_STEP_DEADLINE_AT_ENV = "OWNER_LOOKUP_STEP_DEADLINE_AT"


def cleanup_ceiling(now: float | None = None) -> float:
    """The epoch-seconds lookup ceiling for ONE cleanup starting now.

    ``min(now + OWNER_LOOKUP_DEADLINE_SECONDS, OWNER_LOOKUP_STEP_DEADLINE_AT)``. An inherited
    OWNER_LOOKUP_DEADLINE_AT is deliberately NOT consulted: it is the stale step-start
    ceiling this replaces. A malformed step deadline raises, like a malformed ceiling.
    """
    if now is None:
        now = time.time()
    ceiling = now + OWNER_LOOKUP_DEADLINE_SECONDS
    step_at = os.environ.get(OWNER_LOOKUP_STEP_DEADLINE_AT_ENV)
    if step_at:
        try:
            step_deadline = float(step_at)
        except ValueError:
            step_deadline = math.nan
        # float() also accepts "nan" and "inf": min(ceiling, nan) is ceiling, so a NaN step
        # deadline would be ignored silently. Only a finite epoch bounds the step.
        if not math.isfinite(step_deadline):
            raise ValueError(
                f"{OWNER_LOOKUP_STEP_DEADLINE_AT_ENV} must be epoch seconds, got {step_at!r}"
            )
        ceiling = min(ceiling, step_deadline)
    return ceiling


class Deadline:
    """A monotonic wall-clock budget shared by every credential and every attempt.

    ``time.monotonic``, never wall time: a clock jump backwards mid-lookup must not
    re-inflate an expired budget, and a jump forwards must not falsely expire one."""

    def __init__(self, budget: float | None = None, *, ceiling_at: float | None = None) -> None:
        # Read at CALL time, not definition time, so the deadline can be tuned (and
        # tested) by overriding the module constant.
        if budget is None:
            budget = OWNER_LOOKUP_DEADLINE_SECONDS
        self._expires_at = time.monotonic() + budget
        self._wall_expires_at: float | None = None
        if ceiling_at is not None:
            # An explicit per-cleanup ceiling (cleanup_ceiling) overrides the environment.
            self._wall_expires_at = ceiling_at
            return
        env_at = os.environ.get(OWNER_LOOKUP_DEADLINE_AT_ENV)
        if not env_at:
            self._wall_expires_at = None
        else:
            try:
                self._wall_expires_at = float(env_at)
            except ValueError as exc:
                # A malformed ceiling silently ignored IS the unbounded step again —
                # fail loudly at the first lookup instead.
                raise ValueError(
                    f"{OWNER_LOOKUP_DEADLINE_AT_ENV} must be epoch seconds, got {env_at!r}"
                ) from exc

    def remaining(self) -> float:
        # TWO CLOCKS, ON PURPOSE: the process's own budget on time.monotonic (a
        # backwards clock jump must not re-inflate it), the step-wide ceiling on epoch
        # time (the exporting shell speaks `date +%s`, and the ceiling must hold across
        # processes). The binding deadline is the MINIMUM of the two.
        remaining = self._expires_at - time.monotonic()
        if self._wall_expires_at is not None:
            remaining = min(remaining, self._wall_expires_at - time.time())
        return max(0.0, remaining)

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0


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


def bounded_call(call: Callable[[], Any], deadline: Deadline) -> tuple[bool, Any]:
    """Run one lookup call under the deadline's REMAINING wall clock, outside the socket.

    ⛔ WHY OUTSIDE THE SOCKET. A drip server — bytes trickling in slower than any
    reasonable read, but never stopping — resets the socket timeout with every chunk
    and defeats it entirely (measured on #369's review: 12.3s elapsed against a 0.5s
    socket timeout, no TimeoutError). So the bound that matters is a join on the
    wall clock: the call runs in a daemon worker and the caller waits at most
    ``deadline.remaining()``.

    On expiry the worker is ABANDONED, not joined. Its late result — if one ever
    arrives — is discarded by construction: it lands in a holder this function has
    already returned past, and nobody re-reads it (pinned by test in
    tests/test_owner_lookup_deadline.py). A lookup is READ-ONLY with respect to lease
    lifecycle — a JWT mint or a deployment GET cannot close, destroy or transfer
    anything — so a lingering worker cannot act on the world; it can only be ignored.
    The worker keeps #369's own socket timeout, so a no-byte stall still ends it; a
    drip may keep it alive past the deadline. That lingering daemon thread is the
    accepted residual of this design.
    """
    outcome: dict[str, Any] = {}

    def _run() -> None:
        try:
            outcome["value"] = call()
        except BaseException as exc:  # re-raised below ONLY if we outlived the join
            outcome["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(deadline.remaining())
    if worker.is_alive():
        return False, None
    if "error" in outcome:
        raise outcome["error"]
    return True, outcome.get("value")


def ask(
    call: Callable[[], Any], *, bound: bool = False, deadline: Deadline | None = None
) -> tuple[str, Any]:
    """("answered", value) | ("unknown", None) | ("unreadable", None) within the retry budget.

    With a ``deadline`` the phase budget also applies: each attempt runs under
    ``bounded_call`` (wall clock outside the socket), an expired attempt returns
    "unknown" immediately — the budget retries would spend is gone — and no backoff
    sleeps past an already-expired deadline.
    """
    attempts = BOUND_OWNER_LOOKUP_ATTEMPTS if bound else OWNER_LOOKUP_ATTEMPTS
    for attempt in range(1, attempts + 1):
        # ⛔ CHECK BEFORE EVERY ATTEMPT, not only after a failure (#378 review, Y3): a
        # Console call that STARTS after the deadline is budget the deadline did not
        # grant — measured at +0.51s and +1.0s past it under the old ordering.
        if deadline is not None and deadline.expired:
            return "unknown", None
        try:
            if deadline is None:
                return "answered", call()
            answered, value = bounded_call(call, deadline)
            if not answered:
                return "unknown", None
            return "answered", value
        except Exception as exc:  # noqa: BLE001 - classified below, never read as a mismatch
            if not is_transport_error(exc):
                return "unreadable", None
            if attempt < attempts:
                backoff = (
                    BOUND_OWNER_LOOKUP_BACKOFF_SECONDS * 2 ** (attempt - 1)
                    if bound
                    else OWNER_LOOKUP_BACKOFF_SECONDS * attempt
                )
                if deadline is not None:
                    if deadline.expired:
                        return "unknown", None
                    # Never sleep PAST the deadline: the sleep itself is budget too.
                    backoff = min(backoff, deadline.remaining())
                time.sleep(backoff)
    return "unknown", None


def lookup_owner(
    candidate: Any, *, bound: bool = False, deadline: Deadline | None = None
) -> tuple[str, str | None]:
    """("address"|"unknown"|"unreadable", address) for one credential's account lookup."""
    kind, value = ask(candidate.account_address, bound=bound, deadline=deadline)
    return ("address", value) if kind == "answered" else (kind, None)


def unresolved_verdict(kinds: set[str]) -> str:
    """The typed hold when nothing matched. `kinds` holds each credential's outcome; a proven
    mismatch is recorded as "address"."""
    if "unknown" in kinds:
        return OWNER_LOOKUP_UNREACHABLE
    if "address" in kinds:
        return NO_CREDENTIAL_MATCHES_OWNER
    return OWNER_LOOKUP_UNREADABLE
