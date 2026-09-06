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

    r = run("just up", timeout=300)
    output = r.stdout + r.stderr
    print(output)

    m = re.search(r"DSEQ[:\s]+(\d+)", output)
    if m:
        dseq_ref["dseq"] = m.group(1)

    if r.returncode != 0:
        log_fail(
            f"just up failed (rc={r.returncode}):\nstdout: {r.stdout!r}\nstderr: {r.stderr!r}"
        )
        if dseq_ref["dseq"]:
            robust_destroy(dseq_ref["dseq"])
        sys.exit(1)

    if not dseq_ref["dseq"]:
        log_fail("Could not parse DSEQ from `just up` output")
        sys.exit(1)

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
        gate_elapsed: float | None = None
        # ⛔ _NOPOLL, not None and not _ABSENT. These start as "no document was
        # ever parsed"; a successful parse overwrites them with the key's value or
        # with _ABSENT. Keeping those two apart is the point: for ssh_host, ABSENT
        # is the ordinary "no endpoint yet" reading — it is DATA — and a plain None
        # or False would make it indistinguishable from a poll that never landed.
        gate_status: object = _NOPOLL
        gate_ssh: object = _NOPOLL
        # Provider workload activation can lag well past 35s on a busy provider;
        # poll up to ~95s before declaring a timeout to avoid flaky CI failures.
        max_attempts = 18
        poll_interval = 5
        for attempt in range(1, max_attempts + 1):
            r = run(f"uv run just-akash status --dseq {dseq} --json", timeout=30)
            try:
                status_data = json.loads(r.stdout)
                provider_addr = status_data.get("provider")
                # Captured BEFORE the condition so a run that never satisfies it
                # still reports what the last poll actually saw.
                gate_attempt = attempt
                gate_elapsed = time.monotonic() - gate_t0
                gate_status = status_data.get("status", _ABSENT)
                # NOT bool(...): bool() maps an ABSENT key and a present-but-empty
                # value onto the same False, and "unreported must not render as
                # False" is the whole discipline here.
                gate_ssh = status_data.get("ssh_host", _ABSENT)
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
        log_info(
            "GATE dseq={} attempt={} elapsed={} status={} ssh_host={}".format(
                dseq,
                gate_attempt if gate_attempt is not None else "unreported",
                f"{gate_elapsed:.1f}s" if gate_elapsed is not None else "unreported",
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
