#!/usr/bin/env python3
"""
End-to-end lifecycle test for Akash deployments.

Tests the real user workflow by calling `just` targets:
  just up → just list → just status → just connect (SSH verify) → just destroy → just list

Requires AKASH_API_KEY, AKASH_PROVIDERS, and SSH_PUBKEY in environment.
AKASH_PROVIDERS_BACKUP is optional; if set, a backup-selected provider is
also accepted by the tier-aware assertion.

Usage:
    just test
"""

import json
import os
import re
import subprocess
import sys
import time

from ._e2e import (
    assert_provider_in_tiers,
    install_signal_cleanup,
    resolve_tiers,
)
from ._e2e import (
    destroy_owned_deployment as robust_destroy,
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
    print(f"{BOLD}  Akash Lifecycle Test (SSH){RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")

    log_step(1, "Validate environment")

    for var in ("AKASH_API_KEY", "AKASH_PROVIDERS", "SSH_PUBKEY"):
        if os.environ.get(var):
            log_pass(f"{var} is set")
        else:
            log_fail(f"{var} not set")
            sys.exit(1)

    preferred, backup, allowed = resolve_tiers()
    log_info(f"Preferred providers: {len(preferred)}")
    for p in preferred:
        log_info(f"  {p}")
    if backup:
        log_info(f"Backup providers: {len(backup)}")
        for p in backup:
            log_info(f"  {p}")

    ssh_key = None
    for candidate in [
        os.path.expanduser(f"~/.ssh/id_ed25519_akash_node{i}") for i in range(1, 4)
    ] + [os.path.expanduser("~/.ssh/id_ed25519")]:
        if os.path.exists(candidate):
            ssh_key = candidate
            break
    if not ssh_key:
        log_fail("No SSH private key found")
        sys.exit(1)
    log_pass(f"SSH key: {ssh_key}")

    # Install SIGINT/SIGTERM handlers BEFORE we create the deployment so
    # interrupts during `just up` also trigger cleanup.
    install_signal_cleanup(dseq_ref)

    log_step(2, "just list — check initial state")

    r = run("just list")
    if r.returncode != 0:
        log_fail(f"just list failed: {r.stderr.strip()}")
        sys.exit(1)
    log_pass("API reachable")
    log_info(r.stdout.strip())

    log_step(3, "just up — deploy SSH instance")

    r = run("just up", timeout=300)
    output = r.stdout + r.stderr
    print(output)

    # Try to recover DSEQ even on failure so cleanup can run.
    m = re.search(r"DSEQ[:\s]+(\d+)", output)
    if m:
        dseq_ref["dseq"] = m.group(1)

    if r.returncode != 0:
        log_fail("just up failed")
        failures.append(f"up: exit {r.returncode}")
        if dseq_ref["dseq"]:
            robust_destroy(dseq_ref["dseq"])
        _summary(failures)
        sys.exit(1)

    if not dseq_ref["dseq"]:
        log_fail("Could not parse DSEQ from 'just up' output")
        failures.append("up: no dseq in output")
        _summary(failures)
        sys.exit(1)

    dseq = dseq_ref["dseq"]
    log_pass(f"Deployed: DSEQ={dseq}")

    # Wrap all post-deploy work in try/finally so the deployment is destroyed
    # no matter what raises below (assertion, subprocess error, KeyboardInterrupt).
    try:
        log_step(4, f"just status {dseq} — verify our provider")

        log_info("Waiting 10s for lease propagation...")
        time.sleep(10)

        # Prefer JSON status for a clean provider extraction.
        import contextlib

        provider_addr = None
        rj = run(f"uv run just-akash status --dseq {dseq} --json", timeout=30)
        if rj.returncode == 0:
            with contextlib.suppress(json.JSONDecodeError, AttributeError):
                provider_addr = json.loads(rj.stdout).get("provider")

        r = run(f"just status {dseq}")
        status_output = r.stdout
        print(status_output)

        if r.returncode != 0:
            log_fail(f"just status failed: {r.stderr.strip()}")
            failures.append("status: failed")
        else:
            if not provider_addr:
                # Fallback: extract from "Provider: <addr>" in TTY output.
                pm = re.search(r"Provider:\s+(akash1\S+)", status_output)
                provider_addr = pm.group(1) if pm else None

            if not assert_provider_in_tiers(provider_addr, preferred, backup):
                failures.append("status: foreign or missing provider")

            if "ssh -p" in status_output:
                log_pass("SSH connection info available")
            else:
                log_info("No SSH info in status (port may still be propagating)")

        log_step(5, f"just connect {dseq} — SSH into container")

        ssh_match = re.search(r"ssh -p (\d+) root@(\S+)", status_output)

        if not ssh_match:
            log_info("Retrying status for SSH details...")
            time.sleep(5)
            r = run(f"just status {dseq}")
            status_output = r.stdout
            ssh_match = re.search(r"ssh -p (\d+) root@(\S+)", status_output)

        if not ssh_match:
            log_fail("Could not extract SSH host:port from status")
            failures.append("connect: no SSH endpoint")
        else:
            ssh_port = ssh_match.group(1)
            ssh_host = ssh_match.group(2)
            log_info(f"Target: {ssh_host}:{ssh_port}")
            log_info("Probing SSH (retrying up to 3 min for sshd to start)...")

            connected = False
            for attempt in range(1, 19):
                try:
                    result = subprocess.run(
                        [
                            "ssh",
                            "-o",
                            "StrictHostKeyChecking=no",
                            "-o",
                            "UserKnownHostsFile=/dev/null",
                            "-o",
                            "ConnectTimeout=10",
                            "-o",
                            "BatchMode=yes",
                            "-i",
                            ssh_key,
                            "-p",
                            ssh_port,
                            f"root@{ssh_host}",
                            "echo akash-ssh-ok",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    if "akash-ssh-ok" in result.stdout:
                        connected = True
                        break
                except (subprocess.TimeoutExpired, OSError):
                    pass
                print(f"\r  SSH attempt {attempt}/18 — waiting for sshd...", end="", flush=True)
                time.sleep(10)
            print()

            if connected:
                log_pass(f"SSH connection to {ssh_host}:{ssh_port} SUCCEEDED")
            else:
                log_fail("SSH connection FAILED after 18 attempts")
                failures.append(f"connect: SSH failed to {ssh_host}:{ssh_port}")
    finally:
        log_step(6, f"Cleanup: destroy {dseq}")
        if not robust_destroy(dseq):
            failures.append("cleanup: destroy or audit failed")
        # Once destroyed, drop the ref so the SIGINT handler doesn't double-destroy.
        dseq_ref["dseq"] = None

    log_step(7, "just list — final audit")
    r = run("just list")
    # Word-boundary match: don't false-positive when our dseq is a substring
    # of another active deployment (e.g. "123" inside "12345").
    if not re.search(rf"(?<!\d){re.escape(dseq)}(?!\d)", r.stdout):
        log_pass(f"Deployment {dseq} no longer in list")
    else:
        log_fail(f"Deployment {dseq} still in list")
        failures.append("cleanup: still active")

    _summary(failures)
    sys.exit(1 if failures else 0)


def _summary(failures: list):
    passed = TOTAL_STEPS - len(failures)
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    if failures:
        print(f"{RED}{BOLD}  FAILED{RESET} — {passed}/{TOTAL_STEPS} steps passed")
        for f in failures:
            print(f"  {RED}x{RESET} {f}")
    else:
        print(f"{GREEN}{BOLD}  ALL PASSED{RESET} — {TOTAL_STEPS}/{TOTAL_STEPS} steps")
    print(f"{BOLD}{'=' * 60}{RESET}\n")


if __name__ == "__main__":
    main()
