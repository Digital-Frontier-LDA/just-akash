"""The whole owner-lookup phase is wall-clock bounded (#370).

⛔ WHY. #366's budgets bound RETRIES; #369 bounded each socket operation at 180s. A full
Console stall can still cost 180s per attempt × attempts per key × N keys — about 79
minutes for 8 keys — and the job is then killed mid-cleanup with no typed verdict. Here
the phase gets ONE monotonic deadline: on expiry the caller raises (or returns) the typed
OWNER_LOOKUP_UNREACHABLE, recording the attempts each credential made, by POSITION (key
material never enters a message).

⚠ THE INFORMATIVE ATTEMPTS RECORD LIVES ON THE POST-LOOP UNREACHABLE message and
the _e2e log line. Under per-credential shares the expiry raise is effectively always
"tried 0" (a share spends itself before the next key is reached), so a test asserting
attempts against the expiry text measures nothing — the post-loop surface is where a
key[-6:] leak or a missing record is actually observable (#378 review, Y4 decision).

The per-request bound is enforced OUTSIDE the socket: a drip server — bytes trickling in,
never stopping — resets the socket timeout with every chunk and defeats it (measured on
#369's review: 12.3s against a 0.5s socket timeout). So each attempt runs in a daemon
worker and the caller joins it for the REMAINING wall clock. An abandoned worker's late
answer is discarded by construction — pinned by test below — and a lookup is READ-ONLY
with respect to lease lifecycle (a JWT mint or a deployment GET cannot close, destroy or
transfer anything), so a lingering worker cannot act on the world.

⚠ The drip is pinned at the bounded_call level, not through the patched-urlopen console
fixture: below the real socket layer a drip and a stall present the same shape to the
caller ("the call never returns"), and the thing to prove here is that the wall clock
bounds BOTH. The socket-layer half of the drip measurement (active chunks resetting the
read timeout) was made against the real stack on #369's review and is cited above.
"""

from __future__ import annotations

import base64
import json
import math
import re
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest
import yaml

from just_akash import api, chain, owner_lookup, wallet_pool
from just_akash.owner_lookup import (
    OWNER_LOOKUP_DEADLINE_SECONDS,
    OWNER_LOOKUP_JOB_MARGIN_SECONDS,
    OWNER_LOOKUP_UNREACHABLE_EXIT_CODE,
    Deadline,
    OwnerLookupUnresolved,
    ask,
    bounded_call,
)

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
OTHER = "akash1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"
DSEQ = "1789370984331"
KEYS = ["console-key-alpha-0123456789abcdef", "console-key-bravo-fedcba9876543210"]
WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# The production deadline is 600s; every test here shrinks it so the bound is measured
# in seconds. The constant is read at CALL time, and patched in BOTH modules that
# imported it by name (owner_lookup for the Deadline default, wallet_pool for the
# expiry message) — patching one and not the other is how a test comes to measure
# nothing.
_TEST_DEADLINE = 2.0

# A job that invokes the owner lookup must clear deadline + margin, or its own
# timeout can kill the cleanup the deadline was built to protect.
_REQUIRED_MINUTES = math.ceil(
    (OWNER_LOOKUP_DEADLINE_SECONDS + OWNER_LOOKUP_JOB_MARGIN_SECONDS) / 60
)

_OWNER_LOOKUP_JOB_MARKERS = (
    "destroy --dseq",
    "destroy --expected-owner",
    "resolve-owner",
    "just_akash.test_shell_e2e",
    "just_akash.test_secrets_e2e",
    # smoke_providers reaches owner lookup through robust_destroy →
    # _select_owner_credential (#378 repair): the smoke and its reap-sweep are
    # lookup windows too.
    "just_akash.smoke_providers",
)


class _Response:
    def __init__(self, body: dict) -> None:
        self._body, self.status = json.dumps(body).encode(), 200

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _jwt(address: str) -> dict:
    claims = base64.urlsafe_b64encode(json.dumps({"iss": address}).encode()).decode().rstrip("=")
    return {"data": {"token": f"header.{claims}.signature"}}


STALL = "stall"


@pytest.fixture
def console(monkeypatch):
    """Per-key mint scripts at urlopen: JWT bodies, resets, or a no-byte stall."""
    state: dict = {"mint": {}, "read": {}}
    for module in (owner_lookup, wallet_pool):
        monkeypatch.setattr(module, "OWNER_LOOKUP_DEADLINE_SECONDS", _TEST_DEADLINE)
    monkeypatch.setattr(wallet_pool, "configured_api_keys", lambda: list(KEYS))
    # Corroboration must never be reached in the failure tests; the recovery test
    # overrides this with a passing stub for its own proven match.
    monkeypatch.setattr(
        chain,
        "corroborated_deployment_group_names",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("corroboration reached without a proven match")
        ),
    )

    def _resolve(request):
        key = request.headers.get("X-api-key") or request.get_header("X-api-key")
        kind = "mint" if request.full_url.endswith("/v1/create-jwt-token") else "read"
        script = state[kind].get(key) or (_jwt(OTHER) if kind == "mint" else {"dseq": DSEQ})
        result = script[0] if isinstance(script, list) else script
        if isinstance(script, list):
            script.pop(0)
        return result

    def urlopen(request, *args, **kwargs):
        result = _resolve(request)
        if result == STALL:
            # ⚠ Block on an Event, NOT time.sleep: this runs inside an abandoned
            # daemon worker that outlives the test, and a LATER test that
            # instruments the global time.sleep (the #366 bound-backoff test
            # does) would record this worker's sleeps as its own. Measured.
            threading.Event().wait(30)
            raise AssertionError("unreachable: the join must abandon first")
        if isinstance(result, Exception):
            raise result
        return _Response(result)

    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    return state


def test_a_full_stall_ends_in_the_deadline_not_the_job_timeout(console) -> None:
    console["mint"][KEYS[0]] = STALL
    console["mint"][KEYS[1]] = STALL
    started = time.monotonic()
    with pytest.raises(OwnerLookupUnresolved) as excinfo:
        wallet_pool._raw_client_for_bound_owner(DSEQ, OWNER, "g")
    elapsed = time.monotonic() - started
    assert excinfo.value.verdict == "OWNER_LOOKUP_UNREACHABLE"
    assert "unproven, not disproven" in str(excinfo.value)
    # The bound: ended by the deadline (plus scheduling slack), never by the 30s the
    # workers would have slept, and never by the calling job's timeout.
    assert elapsed < _TEST_DEADLINE + 2.0, f"deadline bound only on paper: {elapsed:.2f}s"


def test_a_drip_is_bounded_by_the_wall_clock_not_by_inactivity() -> None:
    """An ACTIVELY progressing call — the drip's essential shape: bytes arriving,
    the socket never idle, the body never complete — is bounded by the join, not
    by any inactivity timeout."""

    def dripping_call() -> int:
        dripped = 0
        for _ in range(200):  # 40s of continuous progress, never completing
            threading.Event().wait(0.2)  # Event, not time.sleep: see the STALL note
            dripped += 1
        return dripped

    deadline = Deadline(_TEST_DEADLINE)
    started = time.monotonic()
    answered, value = bounded_call(dripping_call, deadline)
    elapsed = time.monotonic() - started
    assert answered is False and value is None
    assert elapsed < _TEST_DEADLINE + 2.0, f"the drip outlived the wall clock: {elapsed:.2f}s"


def test_a_recovery_before_the_deadline_still_selects_the_owner(console, monkeypatch) -> None:
    # A share must absorb a transient reset AND its backoff: with 2 keys the first
    # share is half the phase budget, so the phase budget here is generous (6s → 3s
    # share > reset + 1s backoff + answer).
    for module in (owner_lookup, wallet_pool):
        monkeypatch.setattr(module, "OWNER_LOOKUP_DEADLINE_SECONDS", 6.0)
    console["mint"][KEYS[0]] = [
        urllib.error.URLError(ConnectionResetError(54, "Connection reset by peer")),
        _jwt(OWNER),
    ]
    monkeypatch.setattr(chain, "corroborated_deployment_group_names", lambda *a, **k: ["g"])
    client = wallet_pool._raw_client_for_bound_owner(DSEQ, OWNER, "g")
    assert client is not None


def test_an_abandoned_late_answer_is_discarded(console, monkeypatch) -> None:
    """The worker outlives the deadline and THEN answers with the owner's JWT: the
    late answer must not retroactively select anything."""
    late = {"armed": True}

    def urlopen(request, *args, **kwargs):
        if late["armed"]:
            late["armed"] = False
            threading.Event().wait(_TEST_DEADLINE + 1.0)  # answer arrives AFTER the deadline
            return _Response(_jwt(OWNER))
        return _Response(_jwt(OTHER))

    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    started = time.monotonic()
    with pytest.raises(OwnerLookupUnresolved) as excinfo:
        wallet_pool._raw_client_for_bound_owner(DSEQ, OWNER, "g")
    elapsed = time.monotonic() - started
    assert excinfo.value.verdict == "OWNER_LOOKUP_UNREACHABLE"
    assert elapsed < _TEST_DEADLINE + 2.0
    # Let the abandoned worker finish answering, then prove nothing consumed it:
    # a lookup is read-only, so the late mint cannot act on the world either.
    time.sleep(1.2)
    assert excinfo.value.verdict == "OWNER_LOOKUP_UNREACHABLE"  # still discarded


def test_a_dseq_read_stall_ends_typed_with_attempts(console, monkeypatch) -> None:
    """The --dseq wallet-probe loop shares the phase deadline on BOTH legs: a stall
    ends in the post-loop UNREACHABLE with the attempts record (the informative
    surface under shares), and a pre-expired ceiling raises the expiry text with
    its (empty) record — the loop-top check the Y2b mutation removed."""

    def urlopen(request, *args, **kwargs):
        threading.Event().wait(30)  # Event, not time.sleep — abandoned workers linger
        raise AssertionError("unreachable: the join must abandon first")

    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    started = time.monotonic()
    with pytest.raises(OwnerLookupUnresolved) as excinfo:
        wallet_pool.select_client_for_dseq(DSEQ)
    elapsed = time.monotonic() - started
    assert excinfo.value.verdict == "OWNER_LOOKUP_UNREACHABLE"
    assert "unproven, not disproven" in str(excinfo.value)
    assert "attempts per wallet position:" in str(excinfo.value)
    assert elapsed < _TEST_DEADLINE + 2.0
    # The expiry leg: a ceiling in the past records what the loop actually tried.
    monkeypatch.setenv(owner_lookup.OWNER_LOOKUP_DEADLINE_AT_ENV, str(time.time() - 5))
    with pytest.raises(OwnerLookupUnresolved) as expired:
        wallet_pool.select_client_for_dseq(DSEQ)
    assert expired.value.verdict == "OWNER_LOOKUP_UNREACHABLE"
    assert "expired" in str(expired.value)
    assert "attempts per wallet position: []" in str(expired.value)


def test_every_lingering_worker_is_a_daemon_and_cannot_block_exit(console) -> None:
    """(Review condition on #370.) An abandoned worker must be a DAEMON: a drip-fed
    worker can outlive the deadline by minutes, and neither the process exit nor the
    job's completion may wait for it."""
    console["mint"][KEYS[0]] = STALL
    console["mint"][KEYS[1]] = STALL
    with pytest.raises(OwnerLookupUnresolved):
        wallet_pool._raw_client_for_bound_owner(DSEQ, OWNER, "g")
    lingering = [
        t for t in threading.enumerate() if t is not threading.main_thread() and t.is_alive()
    ]
    assert lingering, "expected at least one abandoned worker from the stall"
    assert all(t.daemon for t in lingering), [t.name for t in lingering if not t.daemon]


def test_the_process_exits_promptly_with_a_lingering_worker() -> None:
    """The exit-half of the same condition: a fresh interpreter abandons a 30s call
    at the deadline and EXITS while the worker is still sleeping."""
    code = (
        "from just_akash.owner_lookup import Deadline, bounded_call\n"
        "import time\n"
        f"answered, _ = bounded_call(lambda: time.sleep(30), Deadline({_TEST_DEADLINE}))\n"
        "assert not answered\n"
        "print('returned-before-worker')\n"
    )
    started = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stderr[-500:]
    assert "returned-before-worker" in proc.stdout
    assert elapsed < 15, f"process waited for the abandoned worker: {elapsed:.1f}s"


def test_abandoned_worker_count_is_bounded_by_the_keys(console) -> None:
    """(Review condition on #370.) Worst case ONE abandoned worker per credential:
    thread growth is bounded by the attempt budgets, never by the stall's duration."""
    console["mint"][KEYS[0]] = STALL
    console["mint"][KEYS[1]] = STALL
    baseline = threading.active_count()
    with pytest.raises(OwnerLookupUnresolved):
        wallet_pool._raw_client_for_bound_owner(DSEQ, OWNER, "g")
    lingerers = threading.active_count() - baseline
    assert 0 < lingerers <= len(KEYS), (
        f"{lingerers} lingering worker(s); at most one abandoned per key is acceptable"
    )


def test_no_console_call_starts_after_the_deadline(console, monkeypatch) -> None:
    """(Review Y3.) Measured on the previous shape: calls began +0.51s and +1.0s PAST
    the deadline. Two pinned properties:
    - bounded_call is never ENTERED with an already-expired deadline (the top-of-attempt
      check returns first). The expired FLAG at entry is the deterministic witness —
      timestamps at this boundary differ by thread-scheduling microseconds, not by a
      testable margin, and the worker's own timestamps race the assertion.
    - no urlopen starts beyond the budget plus scheduling slack (the capped backoff)."""
    starts: list[float] = []
    entered_expired: list[bool] = []
    t0 = time.monotonic()

    real_bounded_call = owner_lookup.bounded_call

    def recording_bounded_call(call, deadline):
        entered_expired.append(deadline.expired)
        return real_bounded_call(call, deadline)

    def urlopen(request, *args, **kwargs):
        starts.append(time.monotonic() - t0)
        raise urllib.error.URLError(ConnectionResetError(54, "Connection reset by peer"))

    monkeypatch.setattr(owner_lookup, "bounded_call", recording_bounded_call)
    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    with pytest.raises(OwnerLookupUnresolved):
        wallet_pool._raw_client_for_bound_owner(DSEQ, OWNER, "g")
    assert starts, "no Console call was made — the test measured nothing"
    assert entered_expired and not any(entered_expired), (
        f"bounded_call was entered with an expired deadline {sum(entered_expired)} "
        "time(s): a doomed call was started with budget it did not have"
    )
    latest = max(starts)
    assert latest < _TEST_DEADLINE + 0.25, (
        f"a Console call started {latest:.2f}s in, past the {_TEST_DEADLINE:.0f}s "
        f"deadline ({len(starts)} calls: {[round(t, 2) for t in starts]})"
    )


def test_a_bound_backoff_never_outruns_the_ceiling(monkeypatch) -> None:
    """(DEV1 Y3c.) A bound credential's exponential backoff (2+4+8+16 = 30s
    uncapped) must be capped by the remaining budget: with a 2s deadline the phase
    ends at ~2s, not ~6s past its ceiling. Real sleeps, real clock."""

    def resetting() -> str:
        raise urllib.error.URLError(ConnectionResetError(54, "reset by peer"))

    # Budget 5.0 makes the cap load-bearing: the SECOND bound backoff (4s) exceeds
    # the ~3s remaining, so uncapped the phase ends ~6s; capped it ends ~5s.
    deadline = Deadline(5.0)
    started = time.monotonic()
    kind, _ = ask(resetting, bound=True, deadline=deadline)
    elapsed = time.monotonic() - started
    assert kind == "unknown"
    assert elapsed < 5.5, f"the bound backoff overran the ceiling: {elapsed:.1f}s"


def test_a_dripping_first_key_cannot_starve_a_later_key_that_answers(console) -> None:
    """⭐ THE SHARE (#378 review): each untried credential gets remaining/untried, so
    key 1's drip burns only ITS share and key 2 — which would answer the owner — is
    still tried inside the phase budget."""
    console["mint"][KEYS[0]] = STALL  # would consume the WHOLE budget without shares
    console["mint"][KEYS[1]] = _jwt(OWNER)
    import just_akash.chain as chain_mod

    saved = chain.corroborated_deployment_group_names
    chain_mod.corroborated_deployment_group_names = lambda *a, **k: ["g"]
    try:
        client = wallet_pool._raw_client_for_bound_owner(DSEQ, OWNER, "g")
    finally:
        chain_mod.corroborated_deployment_group_names = saved
    assert client is not None


def test_the_share_keeps_a_dseq_read_from_starvation(console) -> None:
    """(S1b.) A stalling FIRST wallet must not starve a later wallet that can read
    the deployment — the per-wallet share bounds the first to its own slice."""
    console["read"][KEYS[0]] = STALL
    console["read"][KEYS[1]] = {"dseq": DSEQ}
    client = wallet_pool.select_client_for_dseq(DSEQ)
    assert client is not None


def test_the_share_keeps_the_e2e_scan_from_starvation(console) -> None:
    """(S1c.) A stalling FIRST credential must not starve a later one that answers
    the owner — the per-credential share bounds the first to its own slice."""
    from just_akash import _e2e

    console["mint"][KEYS[0]] = STALL
    console["mint"][KEYS[1]] = _jwt(OWNER)
    candidate, verdict = _e2e._select_owner_credential(DSEQ, OWNER, list(KEYS), None)
    assert candidate is not None and verdict is None


def test_the_step_wide_env_ceiling_binds_across_the_budget(console, monkeypatch) -> None:
    """OWNER_LOOKUP_DEADLINE_AT is the per-STEP ceiling: even with the process budget
    at its 600s default, a step that exported a 1s ceiling ends in ~1s."""
    monkeypatch.setenv(owner_lookup.OWNER_LOOKUP_DEADLINE_AT_ENV, str(time.time() + 1.0))
    monkeypatch.setattr(wallet_pool, "OWNER_LOOKUP_DEADLINE_SECONDS", 600.0)
    monkeypatch.setattr(owner_lookup, "OWNER_LOOKUP_DEADLINE_SECONDS", 600.0)
    console["mint"][KEYS[0]] = STALL
    console["mint"][KEYS[1]] = STALL
    started = time.monotonic()
    with pytest.raises(OwnerLookupUnresolved) as excinfo:
        wallet_pool._raw_client_for_bound_owner(DSEQ, OWNER, "g")
    elapsed = time.monotonic() - started
    assert excinfo.value.verdict == "OWNER_LOOKUP_UNREACHABLE"
    assert elapsed < 3.0, f"env ceiling did not bind: {elapsed:.2f}s"


def test_an_already_expired_step_budget_records_attempts_and_raises_typed(
    console, monkeypatch
) -> None:
    """The loop-top expiry path: a ceiling in the past means no credential is tried,
    and the typed raise records exactly that (0 tried, attempts by position)."""
    monkeypatch.setenv(owner_lookup.OWNER_LOOKUP_DEADLINE_AT_ENV, str(time.time() - 5))
    with pytest.raises(OwnerLookupUnresolved) as excinfo:
        wallet_pool._raw_client_for_bound_owner(DSEQ, OWNER, "g")
    assert excinfo.value.verdict == "OWNER_LOOKUP_UNREACHABLE"
    assert "expired" in str(excinfo.value)
    assert "attempts per credential position: []" in str(excinfo.value)
    assert "unproven, not disproven" in str(excinfo.value)


def test_a_malformed_step_ceiling_fails_loudly(monkeypatch) -> None:
    """A silently-ignored malformed ceiling IS the unbounded step again."""
    monkeypatch.setenv(owner_lookup.OWNER_LOOKUP_DEADLINE_AT_ENV, "not-a-number")
    with pytest.raises(ValueError, match="epoch seconds"):
        Deadline()


def test_no_key_material_or_suffix_reaches_any_unreachable_path(
    console, capsys, monkeypatch
) -> None:
    """(Review Y4.) The UNREACHABLE surface — exception text on both wallet_pool
    paths, the log line and verdict on the _e2e path — must contain no configured
    key and no SUFFIX of one; attempts are integers by POSITION."""
    from just_akash import _e2e

    console["mint"][KEYS[0]] = STALL
    console["mint"][KEYS[1]] = STALL
    console["read"][KEYS[0]] = STALL
    console["read"][KEYS[1]] = STALL
    messages: list[str] = []
    with pytest.raises(OwnerLookupUnresolved) as excinfo:
        wallet_pool._raw_client_for_bound_owner(DSEQ, OWNER, "g")
    messages.append(str(excinfo.value))
    capsys.readouterr()
    with pytest.raises(OwnerLookupUnresolved) as excinfo:
        wallet_pool.select_client_for_dseq(DSEQ)
    messages.append(str(excinfo.value))
    capsys.readouterr()
    verdict = _e2e._select_owner_credential(DSEQ, OWNER, list(KEYS), None)
    messages.append(verdict[1] or "")
    messages.append(capsys.readouterr().out)
    # The EXPIRY message is a distinct surface (it carries the attempts record);
    # drive it with a ceiling in the past.
    monkeypatch.setenv(owner_lookup.OWNER_LOOKUP_DEADLINE_AT_ENV, str(time.time() - 5))
    with pytest.raises(OwnerLookupUnresolved) as excinfo:
        wallet_pool._raw_client_for_bound_owner(DSEQ, OWNER, "g")
    messages.append(str(excinfo.value))
    for message in messages:
        for key in KEYS:
            assert key not in message
            for size in (4, 6, 8):
                assert key[-size:] not in message, (
                    f"suffix of a configured key leaked: {key[-size:]!r}"
                )


def test_the_cli_exits_with_the_distinct_unreachable_code(monkeypatch) -> None:
    """(Review blocker b.) destroy and resolve-owner exit EX_TEMPFAIL on a typed
    UNREACHABLE, so a step's retry loop can stop instead of re-spending a dead
    budget. Every OTHER lookup failure keeps exit 1."""
    from just_akash import cli

    def _unreachable(*_a, **_k):
        raise OwnerLookupUnresolved("OWNER_LOOKUP_UNREACHABLE", "lookup budget exhausted (test)")

    def _other(*_a, **_k):
        raise OwnerLookupUnresolved("NO_CREDENTIAL_MATCHES_OWNER", "a proven mismatch (test)")

    monkeypatch.setattr(cli, "_require_api_key", lambda: "k")
    monkeypatch.setattr(wallet_pool, "configured_api_keys", lambda: list(KEYS))
    for command in (["destroy", "--dseq", DSEQ, "-y"], ["resolve-owner", "--dseq", DSEQ]):
        monkeypatch.setattr(cli, "_resolve_deployment_client", _unreachable)
        monkeypatch.setattr(sys, "argv", ["just-akash", *command])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == OWNER_LOOKUP_UNREACHABLE_EXIT_CODE, command
        monkeypatch.setattr(cli, "_resolve_deployment_client", _other)
        monkeypatch.setattr(sys, "argv", ["just-akash", *command])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 1, command


def _code(line: str) -> str:
    """The line as CODE: workflow comments explain the constructs they name, and a
    comment mentioning `resolve-owner` is not a lookup."""
    return "" if line.lstrip().startswith("#") else line


def _has_lookup_marker(code_line: str) -> bool:
    """A lookup invocation in CODE: the explicit markers, plus the destroy subcommand
    in any of its invocation forms — including the args-array form
    `"${JA[@]}" destroy "${DESTROY_ARGS[@]}"`, which no literal `destroy --dseq`
    string can see (#378 review, W1)."""
    if any(marker in code_line for marker in _OWNER_LOOKUP_JOB_MARKERS):
        return True
    return bool(re.search(r"(\$JA|\$\{JA\[@\]\}|just-akash)[^|;&]*\bdestroy\b", code_line))


def _lookup_steps_and_jobs(workflows_dir: Path = WORKFLOWS):
    """(workflow, job, timeout-minutes, [(step name, budget seconds or None)]) for every
    job with a lookup-invoking step."""
    found = []
    for path in sorted(workflows_dir.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (doc.get("jobs") or {}).items():
            steps = []
            for st in job.get("steps", []):
                run = st.get("run") or ""
                if not any(_has_lookup_marker(_code(ln)) for ln in run.splitlines()):
                    continue
                budget = None
                for line in run.splitlines():
                    m = re.search(r"OWNER_LOOKUP_DEADLINE_AT=.*\+\s*(\d+)", line)
                    if m and _code(line):
                        budget = float(m.group(1))
                        break
                steps.append((st.get("name") or job_name, budget, run))
            if steps:
                found.append((path.name, job_name, job.get("timeout-minutes"), steps))
    return found


def test_every_lookup_window_has_a_fresh_ceiling_and_every_retry_loop_stops_on_unreachable():
    """(DEV1 interim + parts 1-2.) Per JOB: every retry WINDOW containing a lookup
    carries its OWN fresh OWNER_LOOKUP_DEADLINE_AT export in its preamble (a step-start
    ceiling goes stale while the step's deploys and runner waits spend the clock); every
    lookup marker outside a window is preceded by a step-level export; every retry loop
    over a lookup breaks on the UNREACHABLE exit code; timeout-minutes clears
    exports x budget + margin. TEST_OWNER_LOOKUP_WORKFLOWS_DIR re-runs the derivation
    against a historical tree.

    ⚠ WHICH LEG FIRES WHERE: on b0d6727/5009c4f's trees this goes red through the
    MISSING-EXPORT legs (no ceiling at all, budget None → the window check is never
    reached); the timeout ARITHMETIC leg needs exports present but a timeout too
    small, which no historical tree has — the companion fixture test below fires it
    instead. Verified red on 5009c4f (pool windows + both smoke steps) via the env
    override."""
    import os

    workflows_dir = Path(os.environ.get("TEST_OWNER_LOOKUP_WORKFLOWS_DIR") or WORKFLOWS)
    jobs = _lookup_steps_and_jobs(workflows_dir)
    # ⛔ POPULATION FIRST: a finder that locates zero invoking jobs is broken, not
    # satisfied — the deadline would then be protected by nothing.
    assert len(jobs) >= 4, f"owner-lookup job finder located only {jobs}; widen it"
    # ⛔ AND THE SMOKE POPULATION IS NAMED: removing the smoke_providers marker
    # would blind the finder to provider-smoke.yml silently (the floor of 4 would
    # still hold). This assertion is what makes marker removal red.
    assert any(workflow == "provider-smoke.yml" for workflow, *_ in jobs), (
        "provider-smoke.yml vanished from the lookup population — a marker was "
        "dropped, not the smoke made safe"
    )
    violations = []
    for workflow, job, timeout_minutes, steps in jobs:
        exports_in_job = 0
        for step_name, _budget, run in steps:
            lines = run.splitlines()
            code = [_code(ln) for ln in lines]
            in_window = False
            window_has_lookup = window_stops = window_has_export = False
            for i, (_ln, cl) in enumerate(zip(lines, code, strict=True)):
                if re.search(r"\bfor\b.*;\s*do\b", cl):
                    # The window's PREAMBLE counts: the fresh ceiling may sit in the
                    # lines immediately ABOVE the `for` (that is how the workflows
                    # are written) or inside the window body.
                    preamble = code[max(0, i - 6) : i]
                    in_window, window_has_lookup, window_stops = True, False, False
                    window_has_export = any("OWNER_LOOKUP_DEADLINE_AT=" in c for c in preamble)
                elif in_window and re.match(r"\s*done\b", cl):
                    if window_has_lookup and not window_stops:
                        violations.append(
                            f"{workflow}:{job}/{step_name}: a retry loop over a lookup "
                            f"does not break on exit {OWNER_LOOKUP_UNREACHABLE_EXIT_CODE}"
                        )
                    if window_has_lookup and not window_has_export:
                        violations.append(
                            f"{workflow}:{job}/{step_name}: a lookup retry window with "
                            "no fresh OWNER_LOOKUP_DEADLINE_AT export in its preamble — "
                            "it inherits whatever ceiling is left, stale or none"
                        )
                    in_window = False
                elif in_window:
                    if "OWNER_LOOKUP_DEADLINE_AT=" in cl:
                        window_has_export = True
                    if _has_lookup_marker(cl):
                        window_has_lookup = True
                    if f"-eq {OWNER_LOOKUP_UNREACHABLE_EXIT_CODE}" in cl:
                        window_stops = True
                else:
                    if _has_lookup_marker(cl):
                        before = [
                            c
                            for j, c in enumerate(code[:i])
                            if "OWNER_LOOKUP_DEADLINE_AT=" in c and _code(lines[j])
                        ]
                        if not before:
                            violations.append(
                                f"{workflow}:{job}/{step_name}: a lookup outside any "
                                "retry window with no preceding step-level export"
                            )
                if "OWNER_LOOKUP_DEADLINE_AT=" in cl:
                    exports_in_job += 1
        required = (exports_in_job * 600 + OWNER_LOOKUP_JOB_MARGIN_SECONDS) / 60
        if timeout_minutes is None or timeout_minutes < required:
            violations.append(
                f"{workflow}:{job}: timeout-minutes={timeout_minutes} does not clear "
                f"{exports_in_job} fresh ceiling(s) x 600s + margin "
                f"({OWNER_LOOKUP_JOB_MARGIN_SECONDS:.0f}s) = {required:.1f} min"
            )
    assert not violations, "\n".join(violations)


def test_the_timeout_arithmetic_leg_fires_on_a_too_small_timeout(tmp_path) -> None:
    """The arithmetic leg of the structural rule, fired on a FIXTURE: exports present,
    breaks present, but timeout-minutes below exports x 600 + margin. No historical
    tree exercises this leg (missing-export fires first), so it is proven here."""
    workflow = tmp_path / "fixture.yml"
    workflow.write_text(
        "on: [push]\n"
        "jobs:\n"
        "  closer:\n"
        "    runs-on: ubuntu-latest\n"
        "    timeout-minutes: 5\n"
        "    steps:\n"
        "      - name: close\n"
        "        run: |\n"
        "          export OWNER_LOOKUP_DEADLINE_AT=$(( $(date +%s) + 600 ))\n"
        "          for _try in 1 2 3; do\n"
        '            "$JA" destroy "${DESTROY_ARGS[@]}" 2>&1 | tee /tmp/d.log \
'
        "              || RC=${PIPESTATUS[0]}\n"
        '            [ "$RC" -eq 75 ] && break\n'
        "          done\n",
        encoding="utf-8",
    )
    jobs = _lookup_steps_and_jobs(tmp_path)
    assert len(jobs) == 1, "fixture did not register as a lookup job — finder broken"
    ((workflow_name, job, timeout_minutes, steps),) = jobs
    assert timeout_minutes == 5
    exports = sum(
        1
        for _, _, run in steps
        for ln in run.splitlines()
        if "OWNER_LOOKUP_DEADLINE_AT=" in ln and _code(ln)
    )
    required = (exports * 600 + OWNER_LOOKUP_JOB_MARGIN_SECONDS) / 60
    assert exports == 1 and required > timeout_minutes, (
        "fixture must sit strictly inside the arithmetic violation region"
    )
