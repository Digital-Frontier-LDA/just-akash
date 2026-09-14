"""The whole owner-lookup phase is wall-clock bounded (#370).

⛔ WHY. #366's budgets bound RETRIES; #369 bounded each socket operation at 180s. A full
Console stall can still cost 180s per attempt × attempts per key × N keys — about 79
minutes for 8 keys — and the job is then killed mid-cleanup with no typed verdict. Here
the phase gets ONE monotonic deadline: on expiry the caller raises (or returns) the typed
OWNER_LOOKUP_UNREACHABLE, recording the attempts each credential made, by POSITION (key
material never enters a message).

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
    Deadline,
    OwnerLookupUnresolved,
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
    state: dict = {"mint": {}}
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

    def urlopen(request, *args, **kwargs):
        key = request.headers.get("X-api-key") or request.get_header("X-api-key")
        script = state["mint"].get(key) or _jwt(OTHER)
        result = script[0] if isinstance(script, list) else script
        if isinstance(script, list):
            script.pop(0)
        if result == STALL:
            time.sleep(30)  # the no-byte stall, inside the worker; the join bounds it
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
    assert f"({_TEST_DEADLINE:.0f}s)" in str(excinfo.value)
    assert "unproven, not disproven" in str(excinfo.value)
    assert "attempts per credential position:" in str(excinfo.value)
    # The bound: ended by the deadline (plus scheduling slack), never by the 30s the
    # workers would have slept, and never by the calling job's timeout.
    assert elapsed < _TEST_DEADLINE + 2.0, f"deadline bound only on paper: {elapsed:.2f}s"


def test_a_drip_is_bounded_by_the_wall_clock_not_by_inactivity() -> None:
    """An ACTIVELY progressing call — the drip's essential shape: bytes arriving,
    the socket never idle, the body never complete — is bounded by the join, not
    by any inactivity timeout."""

    def dripping_call() -> int:
        dripped = 0
        for _ in range(200):  # 40s of continuous progress at 0.2s per chunk
            time.sleep(0.2)
            dripped += 1
        return dripped

    deadline = Deadline(_TEST_DEADLINE)
    started = time.monotonic()
    answered, value = bounded_call(dripping_call, deadline)
    elapsed = time.monotonic() - started
    assert answered is False and value is None
    assert elapsed < _TEST_DEADLINE + 2.0, f"the drip outlived the wall clock: {elapsed:.2f}s"


def test_a_recovery_before_the_deadline_still_selects_the_owner(console, monkeypatch) -> None:
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
            time.sleep(_TEST_DEADLINE + 1.0)  # the answer arrives AFTER the deadline
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
    """The --dseq wallet-probe loop shares the same phase deadline."""

    def urlopen(request, *args, **kwargs):
        time.sleep(30)
        raise AssertionError("unreachable: the join must abandon first")

    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    started = time.monotonic()
    with pytest.raises(OwnerLookupUnresolved) as excinfo:
        wallet_pool.select_client_for_dseq(DSEQ)
    elapsed = time.monotonic() - started
    assert excinfo.value.verdict == "OWNER_LOOKUP_UNREACHABLE"
    assert "wall-clock deadline" in str(excinfo.value)
    assert "attempts per wallet position:" in str(excinfo.value)
    assert elapsed < _TEST_DEADLINE + 2.0


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


def _owner_lookup_jobs() -> list[tuple[str, str, int | None]]:
    """(workflow, job, timeout-minutes) for every job whose steps invoke the lookup."""
    found: list[tuple[str, str, int | None]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (doc.get("jobs") or {}).items():
            body = json.dumps(job)
            if not any(marker in body for marker in _OWNER_LOOKUP_JOB_MARKERS):
                continue
            found.append((path.name, job_name, job.get("timeout-minutes")))
    return found


def test_every_owner_lookup_job_timeout_exceeds_deadline_plus_margin() -> None:
    jobs = _owner_lookup_jobs()
    # ⛔ POPULATION FIRST: a finder that locates zero invoking jobs is broken, not
    # satisfied — the deadline would then be protected by nothing.
    assert len(jobs) >= 4, f"owner-lookup job finder located only {jobs}; widen it"
    violations = [
        f"{workflow}:{job} has timeout-minutes={minutes}, needs >= {_REQUIRED_MINUTES}"
        for workflow, job, minutes in jobs
        if minutes is None or minutes < _REQUIRED_MINUTES
    ]
    assert not violations, (
        "a job that runs the owner lookup can be killed before deadline+margin — "
        f"deadline {OWNER_LOOKUP_DEADLINE_SECONDS}s + margin {OWNER_LOOKUP_JOB_MARGIN_SECONDS}s "
        f"= {_REQUIRED_MINUTES} min: {violations}"
    )
