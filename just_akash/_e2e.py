"""Shared helpers for e2e test scripts.

Centralizes:
  - tier resolution from env (preferred ∪ backup) for provider verification
  - leak-proof cleanup: SIGINT/SIGTERM handler + retry-on-fail destroy + post-destroy audit

These helpers are imported by just_akash/test_lifecycle.py, test_secrets_e2e.py,
and test_shell_e2e.py. Keeping them here ensures all three e2e tests share the
same "no deployment leak" behavior — if any one diverges, that's a bug to fix
here, not by patching three call sites.
"""

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

# Refs registered by install_signal_cleanup. The signal handler iterates this
# list so multiple deployments — created sequentially in the same process —
# are ALL cleaned up on interrupt. Without this, the second install() would
# replace the first handler and orphan the first deployment.
_REGISTERED_DSEQ_REFS: list[dict] = []
_SIGNAL_HANDLERS_INSTALLED = False
# Reentrancy guard. An impatient user double-Ctrl-C-ing during cleanup would
# otherwise re-enter _signal_handler recursively and re-destroy every
# registered ref once per re-entry level. The guard makes re-entry a no-op:
# the first signal "wins" and is allowed to finish (or be hard-killed).
_HANDLER_RUNNING = False


def _info(msg: str) -> None:
    print(f"  {YELLOW}INFO{RESET} {msg}")


def _pass(msg: str) -> None:
    print(f"  {GREEN}PASS{RESET} {msg}")


def _fail(msg: str) -> None:
    print(f"  {RED}FAIL{RESET} {msg}")


def resolve_tiers() -> tuple[list[str], list[str], list[str]]:
    """Return (preferred, backup, union) parsed from env vars."""
    pref = [p.strip() for p in os.environ.get("AKASH_PROVIDERS", "").split(",") if p.strip()]
    backup = [
        p.strip() for p in os.environ.get("AKASH_PROVIDERS_BACKUP", "").split(",") if p.strip()
    ]
    return pref, backup, pref + backup


def classify_provider(provider: str, preferred: list[str], backup: list[str]) -> str:
    """Tag a provider as 'preferred' / 'backup' / 'foreign' / 'unknown'."""
    if not provider:
        return "unknown"
    if provider in preferred:
        return "preferred"
    if provider in backup:
        return "backup"
    return "foreign"


def assert_provider_in_tiers(
    provider: str | None, preferred: list[str], backup: list[str]
) -> bool:
    """Log + return whether `provider` is in the configured tiered allowlist.

    Returns True on hit (preferred OR backup), False on miss.  Also returns True
    when no allowlist is configured (preferred and backup both empty), since the
    deploy.py state machine accepts any provider in that case.
    """
    if not preferred and not backup:
        _info("No allowlist configured — any provider accepted (skip tier check)")
        return True
    tier = classify_provider(provider or "", preferred, backup)
    if tier == "preferred":
        _pass(f"selected provider {provider} is PREFERRED ({len(preferred)} configured)")
        return True
    if tier == "backup":
        _info(
            f"selected provider {provider} is BACKUP ({len(backup)} configured) "
            "— preferred tier was unresponsive"
        )
        return True
    _fail(
        f"selected provider {provider!r} is NOT in any tier — "
        f"preferred={preferred} backup={backup}"
    )
    return False


def _run(
    cmd: str, *, timeout: int = 60, input_text: str | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_text,
    )


# Words that mean "the deployment is gone" in `just destroy` output.
#
# "destroyed" is what the CLI actually prints on success -- `print(f"Deployment
# {label} destroyed.")` in cli.py's destroy branch. This list used to hold only
# "closed", a word the CLI never emits, so EVERY successful destroy was misread as
# a failure: attempt 1 really did close the deployment, the check failed to notice,
# and two more destroys then fired against an already-closed deployment (exiting 1,
# as they should). The audit passed, so the run stayed green -- it just printed
# three red FAILs and burned two pointless API calls on every E2E run.
#
# The unit tests missed it because their fixtures asserted against a made-up
# "Deployment 12345 closed" that no version of the CLI has ever printed. If you
# reword the CLI's success message, add it here; a test pins the two together.
_DESTROY_SUCCESS_WORDS = ("destroyed", "closed")


def _destroy_succeeded(result: subprocess.CompletedProcess) -> bool:
    """Did this `just destroy` actually close the deployment?

    Silence is deliberately NOT trusted: a clean exit with no output could equally
    mean "already gone" or "did nothing", so we require the CLI to say so. The audit
    in robust_destroy is the backstop that keeps a false negative from failing a run.
    """
    if result.returncode != 0:
        return False
    output = ((result.stdout or "") + (result.stderr or "")).lower()
    return any(word in output for word in _DESTROY_SUCCESS_WORDS)


def _dseq_in_list_output(dseq: str, output: str) -> bool:
    """Word-boundary check for DSEQ in `just list` output.

    Plain substring matching is unsafe: dseq="123" would falsely match a
    different deployment "12345". DSEQs are numeric tokens; require a word
    boundary on both sides so "123" doesn't match "12345" but does match
    "dseq=123 active" or "12345 closed\n123 active".
    """
    if not dseq:
        return False
    return re.search(rf"(?<!\d){re.escape(dseq)}(?!\d)", output) is not None


# Terminal on-chain states: the deployment is settled and holds no escrow —
# measured: a `closed` deployment reads escrow.state=closed with funds=0.
# insufficient_funds is settled by definition (the escrow is what ran out) and is
# already treated as terminal by smoke_providers._DEAD_STATES; kept in sync with it.
_SETTLED_STATES = ("closed", "failed", "insufficient_funds", "insufficientfunds")
# States that positively mean the deployment is still up (and so may hold escrow).
# Deliberately an ALLOWLIST, not "everything that isn't settled": a state we do not
# recognise is UNKNOWN, not proof of life, and saying "STILL ACTIVE" about it would
# be a claim we cannot support. Unknown falls through to "could not confirm", which
# fails closed just the same but tells the operator the truth.
_OPEN_STATES = ("active", "open")


def _confirm_settled(dseq: str, *, attempts: int = 8, interval_s: int = 3) -> bool | None:
    """Authoritative per-deployment read: is ``dseq`` settled (holding no escrow)?

    Returns True (confirmed settled), False (positively still open — a recognised
    open state persisted through the whole window), or None (indeterminate: either
    no probe was readable, or the readable state was one we don't recognise as open,
    which is UNKNOWN, not proof of life). Both False and None fail the audit closed;
    they differ only in the message — "STILL ACTIVE" vs "could not confirm".

    Deliberately NOT `just list`: the collection endpoint serves STALE state — it
    reported a deployment as active minutes after that deployment's own record read
    state=closed / escrow=closed / funds=0. Staleness in that direction only cries
    wolf on a clean destroy, but the same staleness can report a deployment GONE
    while its escrow is still open, which is a silent leak the audit exists to catch.
    Only the per-deployment record decides whether funds are held, so ask it.

    POLLS, because the per-deployment record is authoritative but NOT instantaneous:
    a close takes ~6-12s to reflect, so a just-destroyed deployment keeps reading
    `active` for a while. Reading once and calling that "STILL ACTIVE" fails a
    perfectly clean destroy — measured: it broke the lease-shell E2E, whose destroy
    reported "closed (attempt 1)" and was then declared a leak 2s later. So `active`
    inside the window means "not settled YET", not "still open"; only `active` that
    PERSISTS through the whole window is a real leak.

    Polling also covers transient blips, which matters because the caller fails
    CLOSED: without it a single API hiccup would report a leak that isn't one.
    """
    last_state = ""
    for attempt in range(1, attempts + 1):
        try:
            cmd = f"uv run just-akash status --dseq {shlex.quote(str(dseq))} --json"
            r = _run(cmd, timeout=30)
            if r.returncode == 0 and r.stdout:
                state = str(json.loads(r.stdout).get("state", "")).strip().lower()
                if state in _SETTLED_STATES:
                    return True
                # Anything else — `active` (not settled yet) or an unrecognised value
                # (which is UNKNOWN, never proof of life) — keeps the poll going.
                last_state = state
        except Exception:  # noqa: BLE001 — a probe failure must never raise from cleanup
            pass
        if attempt < attempts:
            time.sleep(interval_s)
    # Window exhausted. Only a state we positively recognise as open lets us claim
    # "STILL ACTIVE"; anything else is an honest "could not confirm".
    if last_state in _OPEN_STATES:
        return False
    return None


def robust_destroy(dseq: str, *, retries: int = 2, audit: bool = True) -> bool:
    """Destroy a deployment with retry-on-fail and post-destroy audit.

    Returns True if the deployment is confirmed gone, False otherwise. Safe to call
    from a signal handler or a finally block: it swallows every ``Exception`` (a
    failed destroy or audit becomes a logged False, never a raise). The one thing it
    lets through is ``KeyboardInterrupt`` — a ``BaseException``, not an ``Exception``
    — so a user Ctrl-C'ing out of cleanup is never trapped. "Never raises" means
    never on a *program* error, not never on a deliberate interrupt.
    """
    if not dseq:
        return True
    # Clamp negative retries so a caller mistake (or signal-handler default
    # of retries=1 minus a typo) never silently skips the destroy loop. Empty
    # range with retries<0 used to issue ZERO destroy commands but still
    # return True from the audit — a silent leak. Clamp to 0 (one attempt).
    retries = max(retries, 0)
    last_err = ""
    for attempt in range(1, retries + 2):
        try:
            r = _run(f"just destroy {dseq}", input_text="y\n", timeout=60)
            if _destroy_succeeded(r):
                _pass(f"Deployment {dseq} closed (attempt {attempt})")
                break
            last_err = (r.stderr or r.stdout).strip()
            _fail(f"destroy attempt {attempt} failed: {last_err[:200]}")
        except Exception as e:  # noqa: BLE001 — must not raise from cleanup
            last_err = str(e)
            _fail(f"destroy attempt {attempt} raised: {e}")
        if attempt <= retries:
            time.sleep(3)
    if not audit:
        return True
    # Audit against the deployment's OWN record, never `just list` (see
    # _confirm_settled). Fails closed: only a positive "settled" reading clears the
    # audit, because the whole point is to catch escrow we failed to release.
    #
    # Wrapped because robust_destroy swallows every Exception: it runs from
    # a finally block and from the signal handler, so an exception escaping here
    # would abort cleanup — the exact failure the audit exists to prevent. Scope is
    # Exception, matching the destroy loop above: KeyboardInterrupt deliberately
    # still propagates, so a user hammering Ctrl-C can always escape. An unreadable
    # audit fails closed rather than claiming success.
    try:
        time.sleep(2)
        settled = _confirm_settled(dseq)
    except Exception as e:  # noqa: BLE001 — cleanup must never raise
        _fail(f"Audit: probe raised ({type(e).__name__}) — treating as a possible leak")
        return False
    if settled is True:
        _pass(f"Audit: deployment {dseq} confirmed settled (no escrow held)")
        return True
    if settled is False:
        _fail(f"Audit: deployment {dseq} STILL ACTIVE after destroy — manual cleanup required")
        return False
    _fail(
        f"Audit: could not confirm {dseq} is settled — treating as a possible leak. "
        f"Verify with: uv run just-akash status --dseq {shlex.quote(str(dseq))} --json"
    )
    return False


def _signal_handler(signum, _frame):
    """Single shared handler — destroys EVERY registered dseq_ref.

    Multiple deployments in one process (sequential or parallel test scripts)
    each call install_signal_cleanup; we accumulate their refs so an interrupt
    cleans them all. Without this, the second install replaces the handler
    and the first deployment leaks.

    Reentrancy: a second signal that arrives while the first is still cleaning
    up is a no-op. The first signal "wins". Without this guard a double-Ctrl-C
    would recursively re-iterate the registry, multiplying destroy calls.
    """
    global _HANDLER_RUNNING
    if _HANDLER_RUNNING:
        # Already cleaning up. Don't re-iterate; let the first signal finish.
        return
    _HANDLER_RUNNING = True
    try:
        sig_name = signal.Signals(signum).name
        print(f"\n  {RED}INTERRUPTED{RESET} ({sig_name}) — running cleanup...")
        cleaned_any = False
        for ref in list(_REGISTERED_DSEQ_REFS):
            dseq = (ref or {}).get("dseq") or ""
            if dseq:
                robust_destroy(dseq, retries=1, audit=True)
                cleaned_any = True
        if not cleaned_any:
            _info("No DSEQ recorded yet — nothing to clean up")
    finally:
        _HANDLER_RUNNING = False
    sys.exit(130)


def install_signal_cleanup(dseq_ref: dict) -> None:
    """Register a dseq_ref for SIGINT/SIGTERM-driven cleanup.

    `dseq_ref` is a mutable dict: tests update `dseq_ref['dseq']` once the
    deployment is created so the handler knows what to clean up.  Call this
    BEFORE creating the deployment so signals during `just up` are also caught.

    Idempotent: re-installing with a NEW dseq_ref appends it to the registry
    rather than replacing the previous handler. All registered refs are
    cleaned up on a single signal — no leaked deployment from an earlier
    install_signal_cleanup call.
    """
    global _SIGNAL_HANDLERS_INSTALLED
    if dseq_ref not in _REGISTERED_DSEQ_REFS:
        _REGISTERED_DSEQ_REFS.append(dseq_ref)
    if not _SIGNAL_HANDLERS_INSTALLED:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
        _SIGNAL_HANDLERS_INSTALLED = True


def _reset_signal_cleanup_for_tests() -> None:
    """Test-only helper: clear registry + handler-installed flag between tests."""
    _REGISTERED_DSEQ_REFS.clear()
    global _SIGNAL_HANDLERS_INSTALLED, _HANDLER_RUNNING
    _SIGNAL_HANDLERS_INSTALLED = False
    _HANDLER_RUNNING = False
