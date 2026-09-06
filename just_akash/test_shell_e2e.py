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

#: Every shape deploy.py writes a DSEQ in, notably `DSEQ=12345` from the flush=True
#: `_log` at deploy.py:1104 — the one emission that SURVIVES a kill. Failure path only;
#: see the comment at its use site for why widening the success path would be a bug.
_DSEQ_ANY_RE = re.compile(r"DSEQ[=:\s]+(\d+)")


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
    while True:
        text = None
        try:
            if os.stat(_DEPLOY_TEE_LOG).st_mtime >= started_at:
                with open(_DEPLOY_TEE_LOG, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
        except OSError:
            text = None
        if text is not None:
            found = sorted(set(_DSEQ_ANY_RE.findall(text)))
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                log_fail(
                    f"{_DEPLOY_TEE_LOG} names more than one DSEQ ({', '.join(found)}); "
                    "a re-deploy round supersedes an earlier one and this file cannot "
                    "say which is live. Closing none of them."
                )
                return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(1.0)


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
        returncode = -1
        log_fail(
            f"`just up` exceeded its {exc.timeout:.0f}s timeout and was SIGKILLed. "
            "Anything it created is still on chain and none of its own cleanup ran."
        )
    deploy_ended_at = time.time()
    output = deploy_out + deploy_err
    print(output)

    m = _DSEQ_SUMMARY_RE.search(output)
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
        candidates = sorted(set(_DSEQ_ANY_RE.findall(output)))
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
    recovered_dseq = None
    if not dseq_ref["dseq"] and (returncode != 0 or not m):
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

    dseq = dseq_ref["dseq"]
    log_pass(f"Deployed DSEQ={dseq}")

    # ── Steps 3-5 with cleanup guarantee ─────────────────────
    try:
        # ── Step 3: Poll for lease readiness + verify provider tier ───
        log_step(3, f"Wait for lease readiness + verify provider tier (DSEQ={dseq})")

        log_info("Waiting 10s for lease propagation...")
        time.sleep(10)

        lease_ready = False
        provider_addr = None
        # Provider workload activation can lag well past 35s on a busy provider;
        # poll up to ~95s before declaring a timeout to avoid flaky CI failures.
        max_attempts = 18
        poll_interval = 5
        for attempt in range(1, max_attempts + 1):
            r = run(f"uv run just-akash status --dseq {dseq} --json", timeout=30)
            try:
                status_data = json.loads(r.stdout)
                provider_addr = status_data.get("provider")
                if status_data.get("status") == "ready" or status_data.get("ssh_host"):
                    lease_ready = True
                    break
            except (json.JSONDecodeError, TypeError):
                pass
            if attempt < max_attempts:
                log_info(
                    f"Attempt {attempt}/{max_attempts} — lease not ready yet, "
                    f"retrying in {poll_interval}s..."
                )
                time.sleep(poll_interval)

        if not lease_ready:
            failures.append("lease_timeout")
            # 10s initial sleep + a poll_interval sleep after every attempt but
            # the last (the final check has no trailing sleep).
            max_wait = 10 + (max_attempts - 1) * poll_interval
            log_fail(f"Lease not active after {max_wait} seconds")
        else:
            log_pass("Lease is active and ready")

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
            r = run(f"uv run just-akash status --dseq {dseq} --json", timeout=30)
            try:
                status_data = json.loads(r.stdout)
                ssh_host = status_data.get("ssh_host")
                ssh_port = str(status_data.get("ssh_port", ""))
            except (json.JSONDecodeError, TypeError):
                pass

            if not ssh_key or not ssh_host or not ssh_port:
                log_info(
                    "SSH key or endpoint not available — skipping SSH cross-check (non-fatal)"
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
