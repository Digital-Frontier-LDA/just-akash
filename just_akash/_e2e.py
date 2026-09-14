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
import signal
import subprocess
import sys
import time
from contextlib import suppress
from urllib import request as urllib_request

from ._lease_verification import DEFAULT_ENDPOINTS
from ._lease_verification import verdict as closure_verdict
from ._states import TERMINAL_DEPLOYMENT_STATES
from .address import is_canonical_akash_address

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
    cmd: list[str],
    *,
    timeout: int = 60,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    if env is None:
        return subprocess.run(  # noqa: S603 - argv is constructed by this package
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
    process = subprocess.Popen(  # noqa: S603 - argv is constructed by this package
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if input_text is not None else None,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, output=stdout, stderr=stderr) from None
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


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
# insufficient_funds is settled by definition (the escrow is what ran out). Defined
# once in _states.py and shared with smoke_providers._DEAD_STATES — the old "kept in
# sync by comment" pair is gone.
_SETTLED_STATES = TERMINAL_DEPLOYMENT_STATES
# States that positively mean the deployment is still up (and so may hold escrow).
# Deliberately an ALLOWLIST, not "everything that isn't settled": a state we do not
# recognise is UNKNOWN, not proof of life, and saying "STILL ACTIVE" about it would
# be a claim we cannot support. Unknown falls through to "could not confirm", which
# fails closed just the same but tells the operator the truth.
_OPEN_STATES = ("active", "open")


def resolve_deployment_owner(dseq: str) -> str:
    """Capture the exact owner while the deployment is still readable.

    ``resolve-owner`` walks the configured wallet pool and positively binds the
    DSEQ to the owning account.  Cleanup calls this before destroy because the
    Console deployment record may disappear immediately after close, while the
    settlement verifier still needs the owner-scoped chain identity.
    """
    cmd = ["uv", "run", "just-akash", "resolve-owner", "--dseq", str(dseq), "--json"]
    result = _run(cmd, timeout=60)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "owner resolution failed").strip())
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("owner resolution returned invalid JSON") from exc
    if not isinstance(payload, dict) or str(payload.get("dseq")) != str(dseq):
        raise RuntimeError("owner resolution did not bind the requested DSEQ")
    if payload.get("source") not in {"wallet_pool", "owner_bound_containment"}:
        raise RuntimeError("owner resolution returned an unknown evidence source")
    owner = payload.get("owner")
    if not isinstance(owner, str) or not is_canonical_akash_address(owner):
        raise RuntimeError("owner resolution returned an invalid Akash address")
    return owner


def _chain_get_json(url: str) -> dict | None:
    req = urllib_request.Request(  # noqa: S310 — verifier admits HTTPS endpoints only
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "just-akash-e2e-settlement-audit/1.0",
        },
    )
    with urllib_request.urlopen(req, timeout=15) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else None


def _confirm_settled(
    dseq: str,
    owner: str,
    *,
    attempts: int = 8,
    interval_s: int = 3,
) -> bool:
    """Require two owner-scoped chain readers to prove complete settlement.

    The former E2E audit trusted one REST-backed ``status`` reader. In #275 and
    #304 that reader returned ``active`` throughout its polling window after the
    close had settled. Extending that window cannot turn one lagging reader into
    independent evidence, so this path delegates to the repository's typed
    complete-population verifier. On timeout False means only "settlement not
    observed"; it never supports the stronger ``STILL ACTIVE`` claim.
    """
    result = closure_verdict(
        str(dseq),
        owner,
        DEFAULT_ENDPOINTS,
        _chain_get_json,
        retries=attempts,
        retry_sleep_s=interval_s,
    )
    return result.get("closed") is True


def _confirm_settled_single_reader(
    dseq: str, *, attempts: int = 8, interval_s: int = 3
) -> bool | None:
    """Legacy per-deployment observation: did one reader report settlement?

    Legacy single-reader observation retained for callers that have not supplied
    owner identity. Returns True only for a terminal reading; False and None both
    mean settlement was not observed. Neither result supports a claim that the
    deployment remains active because this REST reader can lag chain truth.

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
    saw_open = False  # at least one probe positively read an open state
    all_open = True  # EVERY attempt positively read an open state (no blip, no unknown)
    for attempt in range(1, attempts + 1):
        got_open = False
        try:
            cmd = ["uv", "run", "just-akash", "status", "--dseq", str(dseq), "--json"]
            r = _run(cmd, timeout=30)
            if r.returncode == 0 and r.stdout:
                state = str(json.loads(r.stdout).get("state", "")).strip().lower()
                if state in _SETTLED_STATES:
                    return True
                if state in _OPEN_STATES:
                    saw_open = got_open = True
                # An unrecognised value is UNKNOWN, never proof of life — it leaves
                # got_open False, so the window can no longer claim "STILL ACTIVE".
        except Exception:  # noqa: BLE001 — a probe failure must never raise from cleanup
            pass
        if not got_open:
            all_open = False
        if attempt < attempts:
            time.sleep(interval_s)
    # "STILL ACTIVE" (False) requires that EVERY probe positively read open — one
    # stale `active` followed by blips/unknowns is not persistence, it's unknown.
    # Anything short of that is an honest "could not confirm" (None). Both fail the
    # audit closed; they differ only in the message.
    if saw_open and all_open:
        return False
    return None


# ── owner credential selection (#363) ──────────────────────────────────────────────────
#
# ⛔ AN API OUTAGE IS NOT OWNERSHIP EVIDENCE. The old selection called account_address()
# once per configured key and read every exception as "this key is not the owner". During
# a Console API connection-reset window every key failed, none "matched", and a lease the
# run itself created was HELD open (just-akash#362 run 34818093597, dseq 1789370984331).
#
# Three outcomes per key, never two:
#   MATCH    a lookup returned the receipt owner: the ONLY thing that authorises a destroy
#   MISMATCH a lookup returned a different address: the only thing that excludes a key
#   UNKNOWN  every attempt in the bounded budget failed in transport: not evidence either way
# A key whose lookup fails for a non-transport reason (401, malformed JWT) cannot close the
# lease and is skipped, but it never counts as a transport outage.
#
# ⛔ THE CREATE-TIME BINDING IS A HINT, NEVER AUTHORITY. It only orders the lookups: the
# bound key goes first, with a longer budget. A position and a count are weak identity (key
# order can change across runs, and a HELD receipt may be cleaned by a later run or a reaper),
# and the ownership standard forbids selecting a signer without a positive owner match. So
# an UNKNOWN bound key does not close anything: the scan continues, and "no MATCH, some
# UNKNOWN" is the typed OWNER_LOOKUP_UNREACHABLE hold.
# The outcomes, budgets, transport classification and verdicts live in owner_lookup, shared with
# production teardown (#367). Re-exported here under their original names.
from .owner_lookup import (  # noqa: E402, F401 - re-exported under their original names
    BOUND_OWNER_LOOKUP_ATTEMPTS,
    BOUND_OWNER_LOOKUP_BACKOFF_SECONDS,
    NO_CREDENTIAL_MATCHES_OWNER,
    OWNER_LOOKUP_ATTEMPTS,
    OWNER_LOOKUP_BACKOFF_SECONDS,
    OWNER_LOOKUP_UNREACHABLE,
    OWNER_LOOKUP_UNREADABLE,
    unresolved_verdict,
)
from .owner_lookup import is_transport_error as _is_transport_error  # noqa: E402, F401
from .owner_lookup import lookup_owner as _lookup_owner  # noqa: E402


def _select_owner_credential(dseq: str, owner: str, keys: list[str], credential: object):
    """The Console client proven to be ``owner``, or (None, typed verdict).

    Only an address MATCH selects a client. Never logs key material."""
    from .api import AkashConsoleAPI
    from .deployment_receipt import valid_credential_binding

    binding = valid_credential_binding(credential)
    order = list(range(len(keys)))
    bound: int | None = None
    if binding is not None and binding["credential_count"] == len(keys):
        bound = binding["credential_index"]
        order.remove(bound)
        order.insert(0, bound)  # a hint: ordering only
    elif binding is not None:
        _info(f"Cleanup for {dseq}: credential binding does not fit the configured list")
    kinds: set[str] = set()
    for index in order:
        candidate = AkashConsoleAPI(keys[index])
        kind, address = _lookup_owner(candidate, bound=index == bound)
        if kind == "address" and address == owner:
            return candidate, None
        kinds.add(kind)
    return None, unresolved_verdict(kinds)


def robust_destroy(
    dseq: str,
    *,
    owner: str | None = None,
    group: str | None = None,
    groups: list[dict[str, object]] | None = None,
    credential: object = None,
    retries: int = 2,
    audit: bool = True,
) -> bool:
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
    if owner is not None and not is_canonical_akash_address(owner):
        _fail(f"Cleanup held for {dseq}: invalid owner identity")
        return False
    if group is not None and groups is not None:
        _fail(f"Cleanup held for {dseq}: conflicting group identities")
        return False
    if group is not None and (owner is None or not re.fullmatch(r"[A-Za-z0-9._-]+", group)):
        _fail(f"Cleanup held for {dseq}: invalid or owner-less group identity")
        return False
    if owner is not None and group is None and groups is None:
        _fail(f"Cleanup held for {dseq}: owner identity has no group identity")
        return False
    direct_client = None
    if groups is not None:
        if owner is None:
            _fail(f"Cleanup held for {dseq}: complete group population has no owner")
            return False
        from . import chain
        from .wallet_pool import configured_api_keys

        expected = [
            {"gseq": index, "name": entry.get("name") if isinstance(entry, dict) else None}
            for index, entry in enumerate(groups, start=1)
        ]
        if groups != expected or any(
            not isinstance(entry["name"], str)
            or re.fullmatch(r"[A-Za-z0-9._-]+", entry["name"]) is None
            for entry in expected
        ):
            _fail(f"Cleanup held for {dseq}: invalid complete group population")
            return False
        if chain.corroborated_deployment_group_population(owner, str(dseq), groups) != groups:
            _fail(f"Cleanup held for {dseq}: two sources did not prove the exact group population")
            return False
        try:
            keys = configured_api_keys()
        except Exception as e:  # noqa: BLE001 - identity discovery must fail closed
            _fail(f"Cleanup held for {dseq}: could not enumerate owner credentials ({e})")
            return False
        direct_client, verdict = _select_owner_credential(str(dseq), owner, keys, credential)
        if direct_client is None:
            _fail(
                f"Cleanup held for {dseq}: {verdict} — no configured credential was proven "
                "to be the receipt owner"
            )
            return False
    # Clamp negative retries so a caller mistake (or signal-handler default
    # of retries=1 minus a typo) never silently skips the destroy loop. Empty
    # range with retries<0 used to issue ZERO destroy commands but still
    # return True from the audit — a silent leak. Clamp to 0 (one attempt).
    retries = max(retries, 0)
    last_err = ""
    for attempt in range(1, retries + 2):
        try:
            if direct_client is not None:
                direct_client.close_deployment(str(dseq))
                r = subprocess.CompletedProcess([], 0, "Deployment closed", "")
            elif owner is None or group is None:
                command = ["just", "destroy", str(dseq)]
                r = _run(command, input_text="y\n", timeout=60)
            else:
                command = [
                    "uv",
                    "run",
                    "just-akash",
                    "destroy",
                    "--dseq",
                    str(dseq),
                    "--expected-owner",
                    owner,
                    "-y",
                    "--expected-group",
                    group,
                ]
                r = _run(command, input_text="y\n", timeout=60)
            if _destroy_succeeded(r):
                _pass(
                    f"destroy reported success for {dseq} (attempt {attempt}) "
                    f"— settlement not yet verified"
                )
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
    audit_started = time.monotonic()
    try:
        time.sleep(2)
        settled = (
            _confirm_settled(dseq, owner)
            if owner is not None
            else _confirm_settled_single_reader(dseq)
        )
    except Exception as e:  # noqa: BLE001 — cleanup must never raise
        _fail(f"Audit: probe raised ({type(e).__name__}) — treating as a possible leak")
        return False
    if settled is True:
        _pass(f"Audit: deployment {dseq} confirmed settled (no escrow held)")
        return True
    elapsed_s = time.monotonic() - audit_started
    _fail(
        f"Audit: deployment {dseq} close issued, settlement not observed after "
        f"{elapsed_s:.1f} s "
        "— manual cleanup required"
    )
    return False


def destroy_owned_deployment(
    dseq: str,
    *,
    owner: str | None = None,
    group: str | None = None,
    retries: int = 2,
    audit: bool = True,
) -> bool:
    """Resolve owner before the first close byte, then run owner-scoped cleanup.

    Owner resolution failure is a hold: a shared wallet DSEQ without its owner is
    insufficient authority to select and verify a deployment.
    """
    if owner is None:
        try:
            owner = resolve_deployment_owner(dseq)
        except Exception as exc:  # noqa: BLE001 — cleanup reports and holds
            _fail(f"Cleanup held for {dseq}: owner could not be resolved ({exc})")
            return False
    if group is None:
        # The legacy CLI cannot bind an owner without a group. Read a candidate
        # singleton group, then let the destroy command independently corroborate
        # that exact owner/group pair before it selects a wallet or closes anything.
        from . import chain

        try:
            names = chain.deployment_group_names(owner, str(dseq))
        except Exception as exc:  # noqa: BLE001 - discovery failure is a hold
            _fail(f"Cleanup held for {dseq}: singleton group lookup failed ({exc})")
            return False
        if len(names) != 1:
            _fail(f"Cleanup held for {dseq}: exact singleton group could not be resolved")
            return False
        group = names[0]
    return robust_destroy(dseq, owner=owner, group=group, retries=retries, audit=audit)


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
                owner = (ref or {}).get("owner") or None
                if owner is None:
                    try:
                        owner = resolve_deployment_owner(dseq)
                    except Exception as exc:  # noqa: BLE001 — signal cleanup holds safely
                        _fail(f"Cleanup held for {dseq}: owner could not be resolved ({exc})")
                        cleaned_any = True
                        continue
                group = (ref or {}).get("group") or None
                groups = (ref or {}).get("groups")
                if isinstance(groups, list):
                    robust_destroy(
                        dseq,
                        owner=owner,
                        groups=groups,
                        credential=(ref or {}).get("credential"),
                        retries=1,
                        audit=True,
                    )
                elif group is None:
                    robust_destroy(dseq, owner=owner, retries=1, audit=True)
                else:
                    robust_destroy(dseq, owner=owner, group=group, retries=1, audit=True)
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
