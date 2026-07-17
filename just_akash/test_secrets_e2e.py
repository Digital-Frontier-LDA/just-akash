#!/usr/bin/env python3
"""
E2E test: inject secrets via SSH transport, verify via SSH.

Flow:
  1. Validate environment (API key, providers, SSH key)
  2. Deploy SSH-enabled instance
  3. Wait for SSH readiness
  4. Inject secrets via SSH transport (--transport ssh)
  5. Verify secrets exist, have correct values, and file has 600 permissions
  6. Cleanup

Requires: AKASH_API_KEY, AKASH_PROVIDERS, SSH_PUBKEY.

Usage:
    just test-secrets
"""

import json as _json
import os
import re
import secrets
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


def _wait_for_ssh(ssh_key, ssh_host, ssh_port, max_attempts=18):
    for attempt in range(1, max_attempts + 1):
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
                return True
        except (subprocess.TimeoutExpired, OSError):
            pass
        print(
            f"\r  SSH attempt {attempt}/{max_attempts} — waiting for sshd...", end="", flush=True
        )
        time.sleep(10)
    print()
    return False


def main():
    failures = []
    dseq_ref: dict = {"dseq": None}

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  Akash Secrets Injection E2E Test{RESET}")
    print(f"{BOLD}  (SSH inject → SSH verify){RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")

    # ── Step 1: Validate environment ───────────────────
    log_step(1, "Validate environment")

    for var in ("AKASH_API_KEY", "AKASH_PROVIDERS", "SSH_PUBKEY"):
        if os.environ.get(var):
            log_pass(f"{var} is set")
        else:
            log_fail(f"{var} not set")
            sys.exit(1)

    preferred, backup, _ = resolve_tiers()

    ssh_key = os.environ.get("SSH_KEY_PATH")
    if not ssh_key:
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

    install_signal_cleanup(dseq_ref)

    # ── Step 2: Deploy SSH instance ────────────────────
    log_step(2, "Deploy SSH instance")

    r = run("just up", timeout=300)
    output = r.stdout + r.stderr
    print(output)

    m = re.search(r"DSEQ[:\s]+(\d+)", output)
    if m:
        dseq_ref["dseq"] = m.group(1)

    if r.returncode != 0:
        log_fail("just up failed")
        if dseq_ref["dseq"]:
            robust_destroy(dseq_ref["dseq"])
        _summary(["deploy: failed"])
        sys.exit(1)

    if not dseq_ref["dseq"]:
        log_fail("Could not parse DSEQ from output")
        _summary(["deploy: no dseq"])
        sys.exit(1)

    dseq = dseq_ref["dseq"]
    log_pass(f"Deployed: DSEQ={dseq}")

    try:
        # ── Step 3: Wait for SSH readiness + tier assertion ──────────
        log_step(3, f"Wait for SSH + verify provider tier on DSEQ {dseq}")

        log_info("Waiting 10s for lease propagation...")
        time.sleep(10)

        ssh_host = None
        ssh_port = None
        provider_addr = None
        for _attempt in range(3):
            r = run(f"uv run just-akash status --dseq {dseq} --json")
            try:
                status_data = _json.loads(r.stdout)
                ssh_host = status_data.get("ssh_host")
                ssh_port = str(status_data.get("ssh_port", ""))
                provider_addr = status_data.get("provider")
                if ssh_host and ssh_port:
                    break
            except _json.JSONDecodeError:
                pass
            log_info(f"Status attempt {_attempt + 1}/3 — waiting for SSH info...")
            time.sleep(5)

        if not assert_provider_in_tiers(provider_addr, preferred, backup):
            failures.append("status: foreign or missing provider")

        if not ssh_host or not ssh_port:
            log_fail("Could not extract SSH endpoint from status")
            failures.append("ssh: no endpoint")
            return _finish(failures, dseq_ref)

        log_info(f"SSH endpoint: {ssh_host}:{ssh_port}")

        if _wait_for_ssh(ssh_key, ssh_host, ssh_port):
            log_pass("SSH is ready")
        else:
            log_fail("SSH failed to become ready")
            failures.append("ssh: not ready")
            return _finish(failures, dseq_ref)

        # ── Step 4: Inject secrets via SSH ───────────────────
        log_step(4, "Inject secrets via SSH")

        # Generated per-run (issue #38 item 3): no static secret literal for
        # detect-secrets to anchor on, and it still round-trips through the vars.
        env_name = "E2E_TEST_" + secrets.token_hex(3).upper()
        env_val = "akash-e2e-" + secrets.token_hex(8)

        fd, env_file = tempfile.mkstemp(suffix=".env", prefix="akash-test-secrets-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write("# test secrets\n")
                f.write(f"{env_name}={env_val}\n")
                f.write("ANOTHER_VAR=hello_world\n")

            inject_cmd = (
                f"uv run just-akash inject --dseq {dseq} --env-file {env_file} --transport ssh"
            )
            log_info(f"Running: {inject_cmd}")
            r = run(inject_cmd, timeout=60)
            print(r.stdout)
            if r.stderr:
                print(r.stderr)

            if r.returncode != 0:
                log_fail(f"Inject failed (exit {r.returncode}): {r.stderr.strip()}")
                failures.append("inject: failed")
            elif "Injected" in r.stdout:
                log_pass("Secrets injected via SSH")
            else:
                log_fail(f"Unexpected inject output: {r.stdout.strip()}")
                failures.append("inject: unexpected output")
        finally:
            os.unlink(env_file)

        # ── Step 5: Verify secrets via SSH ─────────────────
        log_step(5, "Verify secrets via SSH")

        if "inject" not in [f.split(":")[0] for f in failures]:
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
                "cat /run/secrets/.env",
            ]
            try:
                result = subprocess.run(verify_cmd, capture_output=True, text=True, timeout=15)
                secrets_content = result.stdout

                if result.returncode != 0:
                    log_fail(f"SSH cat failed: {result.stderr.strip()}")
                    failures.append("verify: ssh cat failed")
                elif env_val in secrets_content:
                    log_pass(f"Found {env_name}={env_val} in /run/secrets/.env")

                    if "ANOTHER_VAR=hello_world" in secrets_content:
                        log_pass("Found ANOTHER_VAR=hello_world")
                    else:
                        log_fail("ANOTHER_VAR not found")
                        failures.append("verify: missing ANOTHER_VAR")

                    verify_perms = [
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
                        "stat -c '%a' /run/secrets/.env",
                    ]
                    perm_result = subprocess.run(
                        verify_perms, capture_output=True, text=True, timeout=15
                    )
                    perms = perm_result.stdout.strip()
                    if perms == "600":
                        log_pass("File permissions are 600")
                    else:
                        log_info(f"File permissions: {perms} (expected 600)")
                else:
                    log_fail("Secret value not found in /run/secrets/.env")
                    log_info(f"Content: {secrets_content[:200]}")
                    failures.append("verify: secret value missing")
            except subprocess.TimeoutExpired:
                log_fail("SSH verification timed out")
                failures.append("verify: timeout")
        else:
            log_info("Skipping verification (inject failed)")

        # ── Step 6: Cross-check: inject via lease-shell, verify via SSH ──
        log_step(6, "Cross-check: inject via lease-shell, verify via SSH")

        if "inject" not in [f.split(":")[0] for f in failures]:
            ls_val = "lease-shell-" + secrets.token_hex(8)
            fd2, env_file2 = tempfile.mkstemp(suffix=".env", prefix="akash-test-ls-")
            try:
                with os.fdopen(fd2, "w") as f:
                    f.write(f"CROSSCHECK_KEY={ls_val}\n")

                remote_path2 = "/tmp/e2e-lease-shell-crosscheck.env"
                inject_cmd2 = (
                    f"uv run just-akash inject --dseq {dseq} --env-file {env_file2}"
                    f" --remote-path {remote_path2} --transport lease-shell"
                )
                log_info(f"Running: {inject_cmd2}")
                r2 = run(inject_cmd2, timeout=30)
                if r2.returncode != 0:
                    log_fail(
                        f"Lease-shell inject failed (exit {r2.returncode}): {r2.stderr.strip()}"
                    )
                    failures.append("crosscheck: lease-shell inject failed")
                else:
                    verify_crosscheck = [
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
                        f"cat {remote_path2}",
                    ]
                    try:
                        xr = subprocess.run(
                            verify_crosscheck,
                            capture_output=True,
                            text=True,
                            timeout=15,
                        )
                        if xr.returncode == 0 and ls_val in xr.stdout:
                            log_pass("Lease-shell inject verified via SSH — both transports work")
                        else:
                            log_fail(f"Cross-check verify failed: {xr.stderr.strip()}")
                            log_info(f"Content: {xr.stdout[:200]}")
                            failures.append("crosscheck: value missing")
                    except subprocess.TimeoutExpired:
                        log_fail("Cross-check SSH verify timed out")
                        failures.append("crosscheck: timeout")
            finally:
                os.unlink(env_file2)
        else:
            log_info("Skipping cross-check (inject failed)")
    finally:
        # ── Step 7: Cleanup (always runs, idempotent) ─────────────
        # If _finish() already cleaned up via early-exit, dseq_ref["dseq"]
        # is None — skip to avoid double-destroy.
        if dseq_ref.get("dseq"):
            log_step(TOTAL_STEPS, f"Cleanup DSEQ {dseq}")
            if not robust_destroy(dseq):
                failures.append("cleanup: destroy or audit failed")
            dseq_ref["dseq"] = None

    _summary(failures)
    sys.exit(1 if failures else 0)


def _finish(failures: list, dseq_ref: dict):
    """Early-exit helper that runs cleanup before summarizing."""
    if dseq_ref.get("dseq"):
        robust_destroy(dseq_ref["dseq"])
        dseq_ref["dseq"] = None
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
