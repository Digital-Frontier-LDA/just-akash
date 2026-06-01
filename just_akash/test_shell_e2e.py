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
        log_fail(f"just up failed (rc={r.returncode})")
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
                log_fail(f"exec failed (rc={r.returncode}):\n{r.stderr}")
                failures.append("exec_failed")
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
                    log_fail(f"inject failed (rc={r.returncode}):\n{r.stderr}")
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
                        log_fail(f"inject: expected permissions 600, got: {perms!r}")
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
                        log_fail(
                            f"SSH cross-check failed (rc={xr.returncode}): {xr.stderr.strip()}"
                        )
                        log_info(f"Content: {xr.stdout[:200]}")
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
