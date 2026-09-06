#!/usr/bin/env python3
"""
End-to-end lease-shell transport test.

Deploys a container, runs exec/inject via lease-shell WebSocket transport,
verifies outputs, file permissions, multiline content, and cross-checks
inject by reading the file back over SSH (independent transport).

Usage:
    just test-shell

Requires: AKASH_API_KEY, AKASH_PROVIDERS, SSH_PUBKEY in environment.
"""

import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

from ._e2e import (
    assert_provider_in_tiers,
    install_signal_cleanup,
    resolve_tiers,
    robust_destroy,
)

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"

TOTAL_STEPS = 7


def log_step(n, msg):
    print(f"\n{BOLD}[{n}/{TOTAL_STEPS}]{RESET} {msg}")


def log_pass(msg):
    print(f"  {GREEN}PASS{RESET} {msg}")


def log_fail(msg):
    print(f"  {RED}FAIL{RESET} {msg}")


def log_info(msg):
    print(f"  {YELLOW}INFO{RESET} {msg}")


def run(cmd: str, timeout: int = 60, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_text,
    )


#: The deployment SUMMARY line, `DSEQ: 12345` (deploy.py's closing `print`). It is
#: block-buffered — stdout is a pipe here, not a tty — so it is the FIRST thing lost
#: when the child is killed. Authoritative when it arrives, absent when it matters.
_DSEQ_SUMMARY_RE = re.compile(r"DSEQ[:\s]+(\d+)")

# ⛔ `(\d+)` IN BOTH PATTERNS IS A SHELL-INJECTION BARRIER, not a tidy way to say
# "a number". The captured DSEQ is interpolated UNQUOTED into every `run(f"uv run
# just-akash ... --dseq {dseq} ...")` in `main` — `run` passes shell=True — and into
# the `verify it is ours before closing:` line in `report_unnamed_deployment`, which
# prints a command for a HUMAN to paste into their own terminal.
#
# ⛔ CITED BY ANCHOR, NOT BY LINE NUMBER, on purpose: the first version of this note
# gave line numbers, and they were already wrong in the commit that added them —
# the note's own insertion shifted the code it pointed at. A stale pointer sends the
# next reader somewhere else entirely, which is worse than no pointer.
#
# ⛔ SO DO NOT WIDEN THE CAPTURE. `(\d+)` -> `(\S+)` is the natural-looking edit the
# first time a DSEQ turns up in an unexpected shape, and it is one character between
# a world-writable /tmp file and a shell. Widen the SEPARATOR class if you must;
# never the capture. `test_the_dseq_capture_is_digits_only_because_it_reaches_a_shell`
# fails on exactly that edit.
#: Every shape deploy.py writes a DSEQ in, notably `DSEQ=12345` from the flush=True
#: `_log` at deploy.py:1104 — the one emission that SURVIVES a kill. Failure path only;
#: see the comment at its use site for why widening the success path would be a bug.
_DSEQ_ANY_RE = re.compile(r"DSEQ[=:\s]+(\d+)")


#: `O_NOFOLLOW` is POSIX-only; absent it, the S_ISREG check below still applies.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

#: Most bytes read from the tee log in one poll. The path is world-writable, so its
#: size is chosen by whoever wrote it, not by us — and since the poll re-reads once a
#: second until the deadline, an unbounded read is paid for repeatedly. A real deploy
#: log is tens of KB; this is far above that and far below "fills memory".
_TEE_READ_LIMIT = 1 << 20


def _search_streams(pattern: re.Pattern[str], *streams: str) -> re.Match[str] | None:
    """Search each stream on its own — NEVER their concatenation.

    ⛔ `stdout + stderr` joined with no separator can FABRICATE a match across the
    seam, and this is the one code path where that is likely rather than exotic:
    SIGKILL truncates stdout mid-line, so a stdout ending `DSEQ=` beside a stderr
    beginning with digits yields a number **neither stream emitted**. Measured:

        out = "... Deployment created  DSEQ="
        err = "1234567890 bytes written to socket"
        concatenated -> ['1234567890']      <- in neither stream
        per-stream   -> []

    On the failure path that invented value lands in dseq_ref and reaches
    `robust_destroy`. (Reported by CodeRabbit on #279.)
    """
    for stream in streams:
        found = pattern.search(stream)
        if found:
            return found
    return None


def _findall_streams(pattern: re.Pattern[str], *streams: str) -> list[str]:
    """Every match, per stream, never across the seam. See `_search_streams`."""
    out: list[str] = []
    for stream in streams:
        out.extend(pattern.findall(stream))
    return out


def _decoded(stream: object) -> str:
    """`TimeoutExpired.stdout` is BYTES even when the call passed `text=True`.

    ⛔ Measured, because it is silent either way: `subprocess.run(..., text=True,
    timeout=N)` raises with `.stdout` as bytes, so `exc.stdout + exc.stderr` is a
    TypeError and a `str` pattern over it matches nothing at all. Both failures look
    like "the deployment had no DSEQ" and neither is about the deployment.
    """
    if stream is None:
        return ""
    if isinstance(stream, (bytes, bytearray)):
        return bytes(stream).decode("utf-8", "replace")
    return str(stream)


#: `just up` pipes the deploy through `tee` to this FIXED path (see the `up` recipe in
#: the justfile, which then greps the same file for the DSEQ). It is on disk, so unlike
#: the pipe it survives the parent giving up.
_DEPLOY_TEE_LOG = "/tmp/.akash-last-deploy.log"


def _read_tee_log(started_at: float) -> tuple[str, ...]:
    """Read the tee log if it is a regular file this run could have produced.

    ⛔ A WORLD-WRITABLE FIXED PATH IN /tmp IS NOT A TRUSTED FILE. A plain `open()`
    on a FIFO planted at that path BLOCKS FOREVER — verified — which turns a
    recovery attempt into a hang; a symlink redirects the read somewhere else
    entirely. (Reported by CodeRabbit on #279.)

    So: `O_NONBLOCK` so a FIFO cannot wedge us, `O_NOFOLLOW` so a symlink is
    refused outright, and `fstat` on the DESCRIPTOR rather than `stat` on the path
    — which also closes the TOCTOU gap between checking and opening. Anything that
    predates this run reads as no file at all.

    ⚠ `S_ISREG` CHANGES NO BEHAVIOUR TODAY, AND IS STILL WORTH KEEPING — but not for
    "defence in depth", which is the phrase that lets dead layers accumulate. The
    real reason: **O_NONBLOCK's protection is a side effect; S_ISREG states the
    requirement.** What this function needs is "the thing I am reading is a regular
    file". `O_NONBLOCK` never says that — it happens to make a FIFO read raise
    `BlockingIOError` and a directory raise `IsADirectoryError`, and those happen to
    land in the `except OSError` below. Platform behaviour, not a check anyone wrote.

    Measured, so the claim is checkable rather than a slogan:

        FIFO (no writer / live writer)  -> BlockingIOError   via O_NONBLOCK
        directory                       -> IsADirectoryError via O_NONBLOCK
        symlink                         -> refused outright  via O_NOFOLLOW
        anything the above miss         -> S_ISREG

    Deleting `S_ISREG` leaves the entire suite green; a FIFO-with-a-writer test was
    written specifically to isolate it and could not. So it is not covering a
    reachable case on this platform — it is stating the precondition the open flags
    must keep satisfying, and `O_NONBLOCK` is one unrelated edit away from being
    dropped by someone tidying.

    ⛔ Which makes THIS the load-bearing sentence: do NOT delete `O_NONBLOCK` on the
    grounds that `S_ISREG` is here. The type check is the statement of intent; the
    flag is what currently enforces it. Removing the enforcement because the
    statement exists is the exact inversion that makes overlapping guards dangerous.
    """
    try:
        fd = os.open(_DEPLOY_TEE_LOG, os.O_RDONLY | os.O_NONBLOCK | _O_NOFOLLOW)
    except OSError:
        return ()
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_mtime < started_at:
            return ()
        # ⛔ BINARY, not text. A text stream's `seek` only accepts opaque cookies from
        # `tell()`, so the tail read below cannot be expressed in text mode at all.
        # Each segment is decoded on its own; a multi-byte character split by the cut
        # becomes U+FFFD, which no DSEQ pattern can match.
        with os.fdopen(fd, "rb") as fh:
            fd = -1  # fdopen owns it now
            if st.st_size <= _TEE_READ_LIMIT:
                # +1 so a file that grew between fstat and read is still bounded.
                return (fh.read(_TEE_READ_LIMIT + 1).decode("utf-8", "replace"),)
            half = _TEE_READ_LIMIT // 2
            head = fh.read(half)
            fh.seek(-half, os.SEEK_END)
            tail = fh.read(half)
            # ⛔ TWO SEGMENTS, RETURNED APART, NEVER JOINED. A head+tail concatenation
            # is the same defect as the stdout+stderr one closed above: it invents a
            # byte range no writer produced, and a DSEQ matched across that cut would
            # be pure fabrication. A separator sentinel would work only for as long as
            # nobody widened the pattern to match across it — keeping them apart
            # cannot be undone by a later edit to the regex.
            return (head.decode("utf-8", "replace"), tail.decode("utf-8", "replace"))
    except OSError:
        return ()
    finally:
        if fd >= 0:
            os.close(fd)


def _dseq_from_deploy_log(started_at: float, *, wait: float = 15.0) -> str | None:
    """Recover the DSEQ from the file `just up` tees to, once the pipe has yielded none.

    ⛔ TWO MEASURED FACTS, both counter-intuitive, make this the better source:

      1. `subprocess.run(timeout=)` SIGKILLs its DIRECT child only. `sh -c "just up"`
         execs, so `just` IS that child — but the bash recipe it runs, and the
         `uv run just-akash deploy` inside that, are GRANDchildren. Measured: a
         grandchild ran to completion and wrote its work AFTER the parent gave up.
         So the deploy is probably still going, and this file is still growing.
      2. Which is why this WAITS instead of reading once. The instant of the timeout
         is the moment the file is least complete.

    ⛔ THE mtime CHECK PROVES ONE DIRECTION ONLY, and the other direction is what a
    caller would want. `mtime >= started_at` rules out a file OLDER than this run.
    It does NOT establish that a newer file BELONGS to this run — and fact (1) above
    is a direct counterexample: a PREVIOUS run's abandoned grandchild is still alive
    and still writing to this same fixed path, so its output lands after our
    started_at and looks fresh. Overlapping E2E runs are an observed state here, not
    a thought experiment.

    So what comes back is a CANDIDATE, not an identification. It is good enough to
    tell a human where to look and not good enough to destroy anything: the caller
    routes it to `report_unnamed_deployment`, never to `robust_destroy`. Corroborating
    it would need a second source attributable to this run, and by construction there
    is none — we are only here because this run's own output produced nothing.
    """
    deadline = time.monotonic() + wait
    seen: set[str] = set()
    truncated = False
    while True:
        segments = _read_tee_log(started_at)
        # More than one segment means the file exceeded the read limit and only its
        # head and tail were taken. Say so ONCE: a tee log that large is itself a
        # fact worth a human's attention, not something to quietly read around.
        if len(segments) > 1 and not truncated:
            truncated = True
            log_info(
                f"{_DEPLOY_TEE_LOG} exceeds {_TEE_READ_LIMIT} bytes; reading only its "
                "head and tail, so a DSEQ written between them will not be seen."
            )
        seen.update(_findall_streams(_DSEQ_ANY_RE, *segments))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # ⛔ POLL TO THE DEADLINE BEFORE DECIDING, rather than returning on the first
        # unique hit. The whole premise is that the deploy is STILL WRITING, so a
        # second DSEQ (a re-deploy round supersedes an earlier one) can arrive inside
        # the same window. Returning early means the ambiguity check never observes
        # the ambiguity it exists for, and which answer you get depends on timing.
        # (Reported by CodeRabbit on #279.)
        time.sleep(min(1.0, remaining))

    if len(seen) == 1:
        return next(iter(seen))
    if seen:
        log_fail(
            f"{_DEPLOY_TEE_LOG} names more than one DSEQ ({', '.join(sorted(seen))}); "
            "a re-deploy round supersedes an earlier one and this file cannot say "
            "which is live. Naming none of them."
        )
    return None


def report_unnamed_deployment(
    started_at: float, ended_at: float, candidate: str | None = None
) -> None:
    """State, greppably, that a deployment may exist which this run cannot name.

    ⛔ REPORTS, NEVER CLOSES. Naming a thing and closing it are different
    authorities: the harness may destroy the deployment IT created and can identify,
    but a deployment it cannot identify is not its to guess at. Closing an
    unidentified deployment means closing someone else's, and the Console listing
    cannot tell them apart — `list_deployments()` is not scoped server-side (three
    distinct API keys returned byte-identical bodies, measured 2026-08-30; see
    cleanup_stale.py). Owner-scoped enumeration exists on the CHAIN, and that is the
    instrument this window is meant to feed.

    The parent survives the child's death, so this line always gets written even when
    every byte the child produced was lost. A time window plus an owner is enough to
    find the deployment later; nothing at all is what #278 was about.
    """
    log_fail(
        "UNNAMED DEPLOYMENT POSSIBLE — `just up` may have created a deployment that "
        "this run cannot identify."
    )
    if candidate:
        # ⛔ NAMED, NOT CLOSED. This DSEQ came from a fixed path that concurrent runs
        # share, and a previous run's abandoned deploy writes there after our start
        # time (see _dseq_from_deploy_log). "Probably ours" is enough to point a human
        # at it and not enough to authorise destroying it — closing the wrong one is
        # worse than closing none, because it takes down a live deployment and reports
        # success while doing so.
        log_info(f"  candidate DSEQ (UNVERIFIED, not closed): {candidate}")
        # ⛔ THIS PRINTS A COMMAND FOR A HUMAN TO PASTE, so it runs with THEIR
        # privileges, not CI's — the only sink here that escapes this process's
        # constraints. Safe for the same one reason as the rest: `candidate` is
        # digits by construction, coming only from a `(\d+)` capture. It is a
        # separate path from `dseq`, so the note at that binding does not cover it.
        log_info(f"  verify it is ours before closing: just-akash status --dseq {candidate}")
    log_info(
        f"  created-between: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(started_at))}"
        f" .. OPEN (this run gave up at "
        f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ended_at))})"
    )
    # ⛔ OPEN-ENDED ON PURPOSE, and getting this wrong would be worse than saying
    # nothing. The timeout SIGKILLs only the DIRECT child; the deploy runs two levels
    # down and survives (measured). So it may create the deployment AFTER this run has
    # exited, and a window closed at the moment we gave up would exclude the very
    # event it is meant to help find.
    log_info(f"  the deploy may still be running; its own log is {_DEPLOY_TEE_LOG}")
    log_info(
        "  to resolve: enumerate OWNER-SCOPED FROM CHAIN (not the Console listing, "
        "which is not scoped server-side) for deployments created at or after the "
        "start time, and close by hand if one is ours."
    )


#: TWO sentinels, because "we never got a reading" and "we got a reading and the
#: key was not in it" are different facts and one of them is normal.
#:
#: ⛔ Measured against `just-akash status --json` (cli.py), NOT assumed:
#:   "status"   is ALWAYS present and ALWAYS a string — "ready" / "down" /
#:              "unknown". It is never absent and never JSON null.
#:   "ssh_host" is set only `if ssh:`, so it is OMITTED when there is no SSH
#:              endpoint yet. Absent is the ordinary negative reading — it is
#:              DATA, and it is most of what this instrument will see early in a
#:              lease's life.
#: So an earlier revision here was wrong to call JSON null "a legitimate provider
#: reading": null is not currently producible for either key. What IS producible,
#: and what must stay distinguishable, is no-poll vs key-absent.
_NOPOLL = object()  #: no status document was successfully parsed at all
_ABSENT = object()  #: a document WAS parsed and did not carry this key


def _render(value: object, present=repr) -> str:
    """Four outcomes, four strings — never two facts sharing one.

    `unreported` (no parseable poll) and `absent` (polled, key not present) are
    the two that actually occur, and collapsing them is the defect this helper
    exists to prevent: for ssh_host, `absent` is the ordinary "no endpoint yet"
    reading and would otherwise be indistinguishable from a failed poll.

    `null` is retained as a distinct rendering for an explicit JSON null.
    Defensive: neither key produces one today, and if one ever does, it should
    not silently read as either of the other two.
    """
    if value is _NOPOLL:
        return "unreported"
    if value is _ABSENT:
        return "absent"
    if value is None:
        return "null"
    return present(value)


def _diagnose_exec_failure(dseq: str) -> None:
    """Out-of-band battery, run ONLY after an exec failure and BEFORE destroy (#273).

    ⛔ WHY OUT-OF-BAND. The exec reports `rc=0` with empty stdout and empty stderr —
    a connection that returned nothing. That signature is produced by at least two
    different mechanisms (the workload not yet serving; the provider restarting
    underneath), and NOTHING in the run distinguishes them, so every occurrence has
    so far yielded another round of inference. These three probes are the cheapest
    evidence that discriminates, and they must be taken before `destroy` because
    afterwards the lease is gone and the question is unanswerable.

    ⚠ EVERY PROBE IS INDIVIDUALLY WRAPPED. A diagnostic that raises would abort the
    run it is explaining and destroy the very evidence it was added to collect —
    turning a bad exec into a lost lease. Nothing here may change the verdict:
    `failures` is not touched, and the caller has already recorded the outcome.

    ⚠ `--duration` IS NOT OPTIONAL. `logs` and `events` are streaming commands; the
    CLI's own help says the flag exists to avoid "hanging when the provider holds a
    non-follow connection open". Unbounded, this battery would stall for the full
    subprocess timeout on every failure — a diagnostic that costs more than the bug.
    """
    probes = (
        ("status", f"uv run just-akash status --dseq {dseq} --json"),
        ("logs", f"uv run just-akash logs --dseq {dseq} --tail 50 --duration 10"),
        ("events", f"uv run just-akash events --dseq {dseq} --duration 10"),
    )
    for name, cmd in probes:
        try:
            pr = run(cmd, timeout=45)
            log_info(
                f"DIAG {name} rc={pr.returncode}"
                f"\nstdout: {(pr.stdout or '')[:1500]!r}"
                f"\nstderr: {(pr.stderr or '')[:600]!r}"
            )
        except Exception as e:  # noqa: BLE001 — a probe must never abort the run
            log_info(f"DIAG {name} probe raised ({type(e).__name__}: {e}) — continuing")


def main():
    failures = []
    dseq_ref: dict = {"dseq": None}

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  Akash Lease-Shell E2E Test{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")

    # ── Step 1: Validate environment ─────────────────────────
    log_step(1, "Validate environment")

    for var in ("AKASH_API_KEY", "AKASH_PROVIDERS", "SSH_PUBKEY"):
        if not os.environ.get(var):
            log_fail(f"Required env var {var} is not set")
            sys.exit(1)

    log_pass("All required env vars are set")

    preferred, backup, _ = resolve_tiers()
    install_signal_cleanup(dseq_ref)

    # ── Step 2: Deploy via `just up` ─────────────────────────
    log_step(2, "Deploy via `just up`")

    # ⛔ THE PARENT CANNOT RELY ON ANYTHING THE CHILD PROMISED TO DO. That is the
    # class, and this one call held three instances of it (#278):
    #
    #   1. `subprocess.run(timeout=)` kills with SIGKILL. Measured: a child that
    #      installs SIGTERM/SIGINT/SIGHUP handlers AND an atexit AND a try/finally
    #      logs only "CHILD STARTED". So all SEVEN cleanup sites in deploy.py are
    #      guaranteed dead here — and `install_signal_cleanup` above cannot help
    #      either: it is signal-driven, and it reads a dseq_ref populated BELOW.
    #   2. The exception was never caught, so it escaped before the DSEQ was parsed
    #      and before main's try/finally, taking the run with it and leaving a
    #      deployment nothing could name.
    #   3. `TimeoutExpired` CARRIES the output flushed before the kill, and it was
    #      discarded — the identifier was in hand and thrown away.
    #
    # The DSEQ is assigned SERVER-SIDE (POST /v1/deployments returns it), so the
    # parent cannot pre-record it and no amount of local bookkeeping closes the
    # window. What it can do is keep whatever the child flushed, and say so loudly
    # when that is nothing.
    deploy_started_at = time.time()
    try:
        r = run("just up", timeout=300)
        deploy_out, deploy_err, returncode = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired as exc:
        # ⛔ KEPT APART, not concatenated. `TimeoutExpired` carries the two streams
        # separately, so the timeout path can report the same evidence as every other
        # failure path — and #274's invariant requires it: a failure that names its
        # returncode must name its stdout and stderr too, or it is unactionable.
        # Collapsing them into one blob passed every human read of this diff and was
        # caught by that test.
        deploy_out, deploy_err = _decoded(exc.stdout), _decoded(exc.stderr)
        # ⛔ THE REAL SIGNAL, not a placeholder. A negative returncode names the signal
        # that killed the process, so the -1 this used to carry decodes to SIGHUP — a
        # cause that did not happen, asserted by a value nobody would think to check.
        # `subprocess.run` kills a timed-out child with `Popen.kill()`; measured, that
        # child's returncode is -9. We are not guessing what killed it, we sent it.
        returncode = -int(signal.SIGKILL)
        log_fail(
            f"`just up` exceeded its {exc.timeout:.0f}s timeout; its DIRECT CHILD "
            "was SIGKILLed. The deploy runs a level below that and may still be "
            "running — anything it creates is on chain and unattended by this run."
        )
    deploy_ended_at = time.time()
    output = deploy_out + deploy_err
    print(output)

    m = _search_streams(_DSEQ_SUMMARY_RE, deploy_out, deploy_err)
    if m:
        dseq_ref["dseq"] = m.group(1)
    elif returncode != 0:
        # ⛔ WIDER PATTERN, FAILURE PATH ONLY — and the asymmetry is the finding.
        # `DSEQ=` (deploy.py:1104) goes through `_log`, which prints with flush=True,
        # so it is the ONE emission that survives a kill. `DSEQ: ` (deploy.py:2116)
        # is an unflushed print and is the first thing lost. The recoverable line was
        # the one the pattern did not match; the matched line was the one that never
        # arrived. Both halves had to be true for the DSEQ to go missing.
        #
        # ⛔ NOT widened on the success path, deliberately: deploy.py's re-deploy round
        # emits a second `DSEQ=` (deploy.py:1928), so a first-match search would name
        # the SUPERSEDED deployment. Where the summary line exists it is authoritative
        # and nothing here should second-guess it.
        candidates = sorted(set(_findall_streams(_DSEQ_ANY_RE, deploy_out, deploy_err)))
        if len(candidates) == 1:
            dseq_ref["dseq"] = candidates[0]
            log_info(f"Recovered DSEQ={candidates[0]} from what the child flushed before dying")
        elif candidates:
            log_fail(
                "More than one candidate DSEQ survived in the killed child's output — "
                f"{', '.join(candidates)}. Closing none of them: a re-deploy round "
                "supersedes an earlier DSEQ and this output cannot say which is live."
            )

    # ⛔ THE PIPE IS NOT THE ONLY COPY. Before declaring a deployment unnameable, read
    # the file `just up` tees to — it is on disk, the deploy is probably still writing
    # to it, and the recipe already trusts it enough to grep it for the DSEQ itself.
    # ⛔ DELIBERATELY NOT `dseq_ref`. A recovered DSEQ is a CANDIDATE, not this run's
    # identifier, and dseq_ref is what both the destroy branch below and the SIGINT
    # handler act on. Naming it is useful; destroying it is not ours to do — see
    # report_unnamed_deployment for why mtime cannot establish provenance.
    #
    # The condition is just "we have no DSEQ". An earlier draft also tested
    # `(returncode != 0 or not m)`, which is dead: the only assignments to
    # dseq_ref["dseq"] above come from a match, and `\d+` cannot capture a falsy
    # string — so arriving here without a DSEQ already means `m` was None. The extra
    # conjunct implied two cases where there is one, and hid that recovery is meant
    # to run on ANY DSEQ-less exit rather than only on a failing one.
    recovered_dseq = None
    if not dseq_ref["dseq"]:
        recovered_dseq = _dseq_from_deploy_log(deploy_started_at)
        if recovered_dseq:
            log_info(f"Candidate DSEQ={recovered_dseq} found in {_DEPLOY_TEE_LOG}")

    if returncode != 0:
        log_fail(
            f"just up failed (rc={returncode}):\nstdout: {deploy_out!r}\nstderr: {deploy_err!r}"
        )
        if dseq_ref["dseq"]:
            robust_destroy(dseq_ref["dseq"])
        else:
            report_unnamed_deployment(deploy_started_at, deploy_ended_at, recovered_dseq)
        sys.exit(1)

    if not dseq_ref["dseq"]:
        log_fail("Could not parse DSEQ from `just up` output")
        report_unnamed_deployment(deploy_started_at, deploy_ended_at, recovered_dseq)
        sys.exit(1)

    # ⛔ EVERYTHING BELOW INTERPOLATES `dseq` INTO A SHELL. `run` uses shell=True, and
    # every `run(f"uv run just-akash ... {dseq} ...")` below takes it unquoted. It is
    # safe for exactly one reason: it is digits BY CONSTRUCTION — the only writers of
    # `dseq_ref["dseq"]` are `(\d+)` captures, and the /tmp-derived `recovered_dseq` is
    # deliberately kept out of `dseq_ref` (see step 2), so a world-writable file cannot
    # reach these lines. That containment was written for the escrow rule — an
    # unverified value must not reach a privileged sink — and `shell=True` is the
    # second such sink. If you ever assign to `dseq_ref` from a new source, or widen
    # the capture, you are editing this too.
    dseq = dseq_ref["dseq"]
    log_pass(f"Deployed DSEQ={dseq}")

    # ── Steps 3-5 with cleanup guarantee ─────────────────────
    try:
        # ── Step 3: Poll for lease readiness + verify provider tier ───
        log_step(3, f"Wait for lease readiness + verify provider tier (DSEQ={dseq})")

        log_info("Waiting 10s for lease propagation...")
        # ⛔ MEASURED FROM HERE, not from the first poll: the 10s is part of
        # time-to-ready and excluding it would understate every sample by a
        # constant. Every reported elapsed therefore has a 10s floor.
        gate_t0 = time.monotonic()
        time.sleep(10)

        lease_ready = False
        provider_addr = None
        # ── #273 instrumentation. NOT a gate change: the condition below is
        # byte-identical to before. These four values are recorded so a
        # time-to-ready DISTRIBUTION exists — no cap or stricter condition can be
        # sized without one, and none exists today. Recorded on EVERY run,
        # pass or fail, because a histogram built only from failures is not a
        # histogram.
        gate_attempt: int | None = None
        # ⛔ ONE TUPLE, ASSIGNED ATOMICALLY. `gate_attempt` advances on EVERY attempt
        # while a parse succeeds only on SOME, so holding the observation in its own
        # variables let a run report `attempt=18` beside a status actually read at
        # attempt 3 — two numbers describing different moments, printed as one
        # measurement. (Reported by Copilot on #276.) A tuple makes that disagreement
        # UNREPRESENTABLE rather than merely detectable. Fifth instance of this PR's
        # own defect class, so the class gets the fix and not the line.
        #
        # ⛔ Stays None when nothing ever parsed, and unpacks to _NOPOLL — not to None
        # and not to _ABSENT. A successful parse supplies the key's value or _ABSENT.
        # Keeping those apart is the point: for ssh_host, ABSENT is the ordinary "no
        # endpoint yet" reading — it is DATA — and a plain None or False would make it
        # indistinguishable from a poll that never landed.
        last_obs: tuple[int, object, object] | None = None
        # Provider workload activation can lag well past 35s on a busy provider;
        # poll up to ~95s before declaring a timeout to avoid flaky CI failures.
        max_attempts = 18
        poll_interval = 5
        for attempt in range(1, max_attempts + 1):
            # ⛔ THE PROBE IS INSIDE THE TRY. `run(..., timeout=30)` raises
            # subprocess.TimeoutExpired, and with the call outside, that exception
            # escaped the loop, skipped the GATE emission below, and went straight
            # to cleanup — so the run that most needs a verdict, one where the
            # status probe hangs, was the one that emitted none. "Recorded on EVERY
            # run" was true of every BRANCH and false of the exception path.
            # (Reported by CodeRabbit on #276.)
            #
            # A timeout is a spent ATTEMPT, not a fatal error, so the loop continues
            # and the sentinels stay _NOPOLL — an all-timeout run then reports
            # `attempt=18 elapsed=635.0s status=unreported`, which says what happened.
            # (635 = 10s propagation + 18 probes each hitting the 30s timeout + 17
            # x 5s sleeps. This comment first said 95s — the figure you get by
            # assuming the probe returns instantly, the same schedule-not-clock
            # error as the gate_elapsed capture below it. Both are fixed.)
            try:
                # Per ATTEMPT, not per successful parse: this describes the poll we
                # made, which is true whether or not it returned anything.
                gate_attempt = attempt
                r = run(f"uv run just-akash status --dseq {dseq} --json", timeout=30)
                status_data = json.loads(r.stdout)
                provider_addr = status_data.get("provider")
                # NOT bool(...): bool() maps an ABSENT key and a present-but-empty
                # value onto the same False, and "unreported must not render as
                # False" is the whole discipline here.
                last_obs = (
                    attempt,
                    status_data.get("status", _ABSENT),
                    status_data.get("ssh_host", _ABSENT),
                )
                if status_data.get("status") == "ready" or status_data.get("ssh_host"):
                    lease_ready = True
                    break
            except (subprocess.TimeoutExpired, json.JSONDecodeError, TypeError):
                pass
            if attempt < max_attempts:
                log_info(
                    f"Attempt {attempt}/{max_attempts} — lease not ready yet, "
                    f"retrying in {poll_interval}s..."
                )
                time.sleep(poll_interval)

        # ⛔ MEASURED OFF THE CLOCK WHERE IT IS REPORTED — never snapshotted per
        # attempt, never computed from the schedule. Both mistakes were here:
        #
        #   - `gate_elapsed` was captured at the TOP of each iteration, so it excluded
        #     that attempt's own probe. An all-timeout run — the exact failure this
        #     instrumentation exists to characterise — under-reported by a full 30s
        #     timeout, in the reassuring direction. (Reported by Copilot on #276.)
        #   - `max_wait` was arithmetic over max_attempts and poll_interval, which
        #     silently assumes the probe returns instantly. On the timeout path it
        #     claimed 95s for a wait that really ran ~635s.
        #
        # A duration comes off the clock at the moment it is reported. One measured
        # value now feeds both messages, so they cannot drift apart.
        gate_elapsed = time.monotonic() - gate_t0

        if not lease_ready:
            failures.append("lease_timeout")
            log_fail(f"Lease not active after {gate_elapsed:.1f}s")
        else:
            log_pass("Lease is active and ready")

        # ⛔ ONE GREPPABLE LINE PER RUN, whatever the outcome. `status` and
        # `ssh_host` are reported SEPARATELY because they are not equally
        # informative: `status == "ready"` is `deployment.state == "active"`
        # renamed (cli.py), true from the create transaction onward, so it is
        # ~always true here; `ssh_host` requires the provider to have forwarded
        # port 22 and reported host AND externalPort (api.py `_extract_ssh_info`).
        # Collapsing them into one "ready" boolean would destroy the only
        # distinction this measurement exists to make.
        #
        # ⚠ `elapsed` includes the fixed 10s propagation sleep — see gate_t0.
        # ⛔ Unpacked TOGETHER: `observed_at` is the attempt that produced the status
        # beside it, which is not necessarily the last attempt made.
        obs_attempt, gate_status, gate_ssh = (
            last_obs if last_obs is not None else (None, _NOPOLL, _NOPOLL)
        )
        log_info(
            "GATE dseq={} attempt={} observed_at={} elapsed={} status={} ssh_host={}".format(
                dseq,
                gate_attempt if gate_attempt is not None else "unreported",
                obs_attempt if obs_attempt is not None else "unreported",
                f"{gate_elapsed:.1f}s",
                _render(gate_status),
                _render(gate_ssh, lambda v: "present" if v else "empty"),
            )
        )

        if not assert_provider_in_tiers(provider_addr, preferred, backup):
            failures.append("status: foreign or missing provider")

        # ── Step 4: exec via lease-shell ─────────────────────
        log_step(4, f"exec: echo hello from lease-shell (DSEQ={dseq})")

        if not failures:
            r = run(
                f"uv run just-akash exec 'echo hello from lease-shell'"
                f" --dseq {dseq} --transport lease-shell",
                timeout=30,
            )
            if r.returncode == 0 and "hello from lease-shell" in r.stdout:
                log_pass("exec: output verified")
            else:
                # ⛔ NEUTRAL LABEL AND ALL THREE STREAMS. This said "exec failed
                # (rc={rc})" and printed stderr only. The condition is a CONJUNCTION
                # — the command ran AND the output arrived — so when the second limb
                # fails it printed "exec failed (rc=0)", a cause its own evidence
                # refutes, followed by an empty stderr, while hiding the stdout it
                # actually judged. That is what a real failure on main looked like on
                # 2026-09-06 and it told the reader nothing. The token carried the
                # same wrong cause into `failures`.
                log_fail(
                    f"exec: expected output not verified (rc={r.returncode}):"
                    f"\nstdout: {r.stdout!r}\nstderr: {r.stderr!r}"
                )
                failures.append("exec_output_unverified")
                _diagnose_exec_failure(dseq)
        else:
            log_info("Skipping exec step due to prior failures")

        # ── Step 5: inject via lease-shell + verify ───────────
        log_step(5, f"inject .env + verify via exec (DSEQ={dseq})")

        if not failures:
            env_file = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as tmp:
                    tmp.write("TEST_SECRET=injected_value\n")
                    tmp.write("SECOND_KEY=second_value\n")
                    tmp.write("# comment line\n")
                    env_file = tmp.name

                remote_path = "/tmp/e2e-test.env"
                r = run(
                    f"uv run just-akash inject --env-file {env_file}"
                    f" --remote-path {remote_path} --dseq {dseq}"
                    f" --transport lease-shell",
                    timeout=30,
                )
                if r.returncode != 0:
                    log_fail(
                        f"inject failed (rc={r.returncode}):"
                        f"\nstdout: {r.stdout!r}\nstderr: {r.stderr!r}"
                    )
                    failures.append("inject_failed")
                else:
                    log_pass("inject: env file uploaded")

                    r = run(
                        f"uv run just-akash exec 'cat {remote_path}'"
                        f" --dseq {dseq} --transport lease-shell",
                        timeout=30,
                    )
                    if (
                        r.returncode == 0
                        and "injected_value" in r.stdout
                        and "second_value" in r.stdout
                    ):
                        log_pass("inject: verified multiline content via exec")
                    else:
                        log_fail(
                            f"inject verify failed (rc={r.returncode}):"
                            f"\nstdout: {r.stdout!r}\nstderr: {r.stderr!r}"
                        )
                        failures.append("inject_verify_failed")

                    r = run(
                        f"uv run just-akash exec 'stat -c %a {remote_path}'"
                        f" --dseq {dseq} --transport lease-shell",
                        timeout=30,
                    )
                    perms = r.stdout.strip()
                    if r.returncode == 0 and perms == "600":
                        log_pass("inject: file permissions are 600")
                    else:
                        # ⛔ The mirror of the exec defect above: this named the
                        # permissions limb and printed `perms` only, so a NON-ZERO rc
                        # rendered as "expected permissions 600, got: ''" with the
                        # actual failure invisible.
                        log_fail(
                            f"inject: permissions not verified as 600 "
                            f"(rc={r.returncode}, parsed={perms!r}):"
                            f"\nstdout: {r.stdout!r}\nstderr: {r.stderr!r}"
                        )
                        failures.append("inject_permissions_failed")
            finally:
                if env_file and os.path.exists(env_file):
                    os.unlink(env_file)
        else:
            log_info("Skipping inject step due to prior failures")

        # ── Step 6: Cross-check inject via SSH ─────────────────
        log_step(
            6,
            f"Cross-check: read injected file via SSH (DSEQ={dseq})",
        )

        if not failures:
            ssh_key = os.environ.get("SSH_KEY_PATH")
            if not ssh_key:
                for candidate in [
                    os.path.expanduser(f"~/.ssh/id_ed25519_akash_node{i}") for i in range(1, 4)
                ] + [os.path.expanduser("~/.ssh/id_ed25519")]:
                    if os.path.exists(candidate):
                        ssh_key = candidate
                        break

            ssh_host = None
            ssh_port = None
            # Same shape as the readiness poll above, found by sweeping for it
            # rather than by being told: the probe belongs inside the try, or a
            # TimeoutExpired escapes as a traceback instead of the reported
            # "could not resolve ssh host" failure below.
            # ⛔ WHY the probe yielded nothing is a different fact from THAT it did,
            # and this handler was collapsing them. A hung probe and an ordinary "no
            # endpoint yet" both printed "SSH key or endpoint not available" — one is
            # a lease that is not ready, the other is an instrument that did not run,
            # and only the second means the cross-check was never actually attempted.
            #
            # This is the exact collapse `_NOPOLL`/`_ABSENT` removes in the readiness
            # gate above. Sweeping the try into step 6 fixed the ESCAPE here and left
            # the REPORTING collapse behind — the same class, one function down.
            # (Reported by CodeRabbit on #276.)
            ssh_probe = "ok"
            try:
                r = run(f"uv run just-akash status --dseq {dseq} --json", timeout=30)
                status_data = json.loads(r.stdout)
                ssh_host = status_data.get("ssh_host")
                ssh_port = str(status_data.get("ssh_port", ""))
            except subprocess.TimeoutExpired:
                ssh_probe = "timed-out"
            except (json.JSONDecodeError, TypeError):
                ssh_probe = "unparsable"

            if not ssh_key or not ssh_host or not ssh_port:
                # Name WHICH input was missing as well as how the probe fared: three
                # different absences shared one sentence, so the line could not say
                # whether the key was unset or the lease had no endpoint.
                missing = ", ".join(
                    name
                    for name, value in (
                        ("ssh_key", ssh_key),
                        ("ssh_host", ssh_host),
                        ("ssh_port", ssh_port),
                    )
                    if not value
                )
                log_info(
                    f"SSH cross-check skipped (non-fatal): probe={ssh_probe} missing={missing}"
                )
            else:
                remote_path = "/tmp/e2e-test.env"
                verify_cmd = [
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                    "-o",
                    "BatchMode=yes",
                    "-i",
                    ssh_key,
                    "-p",
                    ssh_port,
                    f"root@{ssh_host}",
                    f"cat {remote_path}",
                ]
                try:
                    xr = subprocess.run(
                        verify_cmd,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    if (
                        xr.returncode == 0
                        and "injected_value" in xr.stdout
                        and "second_value" in xr.stdout
                    ):
                        log_pass(
                            "SSH cross-check: file content matches — lease-shell inject is real"
                        )
                    else:
                        # stdout folded INTO the failure, not a following log_info:
                        # one line carries the whole verdict, and a truncating helper
                        # cannot drop the evidence separately from the message.
                        log_fail(
                            f"SSH cross-check failed (rc={xr.returncode}):"
                            f"\nstdout: {xr.stdout[:200]!r}\nstderr: {xr.stderr!r}"
                        )
                        failures.append("ssh_crosscheck_failed")
                except subprocess.TimeoutExpired:
                    log_fail("SSH cross-check timed out")
                    failures.append("ssh_crosscheck_timeout")

    except Exception as e:
        log_fail(f"Unexpected error: {e}")
        failures.append(str(e))
    finally:
        # ── Step 7: Cleanup (always runs, with retry + audit) ──────
        if dseq:
            log_step(TOTAL_STEPS, f"Cleanup: destroy DSEQ={dseq}")
            if not robust_destroy(dseq):
                failures.append("destroy_failed")
            dseq_ref["dseq"] = None

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    if failures:
        log_fail(f"{len(failures)} step(s) failed: {failures}")
        print(f"{BOLD}{'=' * 60}{RESET}\n")
        sys.exit(1)
    else:
        log_pass("All steps passed — lease-shell transport validated end-to-end")
        print(f"{BOLD}{'=' * 60}{RESET}\n")


if __name__ == "__main__":
    main()
