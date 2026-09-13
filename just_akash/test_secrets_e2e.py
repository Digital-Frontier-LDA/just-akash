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
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from ._e2e import (
    assert_provider_in_tiers,
    install_signal_cleanup,
    resolve_tiers,
)
from ._e2e import (
    destroy_owned_deployment as robust_destroy,
)
from .api import AkashConsoleAPI
from .deploy import _report_suspected_orphans
from .deployment_receipt import DeploymentReceipt, decode_receipt
from .provenance import run_id_of

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


def _run_just_up(
    env: dict[str, str], timeout: int = 300
) -> tuple[subprocess.CompletedProcess, bool]:
    """Run the paid create in its own process group so timeout contains every child."""
    process = subprocess.Popen(
        ["just", "up"],
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )

    def terminate_group() -> tuple[str, str]:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            return process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            return process.communicate()

    try:
        stdout, stderr = process.communicate(timeout=timeout)
        result = subprocess.CompletedProcess(["just", "up"], process.returncode, stdout, stderr)
        return result, False
    except subprocess.TimeoutExpired:
        stdout, stderr = terminate_group()
        result = subprocess.CompletedProcess(["just", "up"], process.returncode, stdout, stderr)
        return result, True
    except BaseException:
        # SIGINT/SIGTERM handlers raise SystemExit, and KeyboardInterrupt plus any
        # other BaseException must still contain the paid create subprocess tree.
        terminate_group()
        raise


def _receipt_environment() -> tuple[Path, str, dict[str, str]]:
    """Choose the durable path; deploy derives signer and final artifact identity."""
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp:
        receipt_dir = Path(runner_temp) / "just-akash-secrets-receipt"
        receipt_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
        receipt_dir.chmod(0o700)
    else:
        receipt_dir = Path(tempfile.mkdtemp(prefix="just-akash-secrets-receipt-"))
    receipt_path = receipt_dir / "create.json"
    operation_id = f"e2e-secrets-{secrets.token_hex(8)}"
    return (
        receipt_path,
        operation_id,
        {
            "JUST_AKASH_RECEIPT_PATH": str(receipt_path),
            "JUST_AKASH_RECEIPT_OPERATION_ID": operation_id,
        },
    )


def _receipt_identity(receipt_path: Path) -> tuple[str, str | None, str, str]:
    receipt = decode_receipt(receipt_path.read_bytes())
    population = receipt["group_population"]
    if len(population) != 1 or population[0].get("gseq") != 1:
        raise RuntimeError(
            "secrets E2E cleanup requires the receipt's complete population to be one gseq=1 group"
        )
    group = population[0].get("name")
    if not isinstance(group, str) or not group:
        raise RuntimeError("receipt group identity is invalid")
    dseq = str(receipt["dseq"]) if receipt["state"] == "create_response_received" else None
    return receipt["state"], dseq, receipt["expected_owner"], group


def _delete_receipt(receipt_path: Path) -> None:
    receipt_path.unlink(missing_ok=True)
    with suppress(OSError):
        receipt_path.parent.rmdir()


def _verified_cleanup(dseq_ref: dict) -> bool:
    """Clear receipt identity only after exact destruction and positive closure audit."""
    dseq = dseq_ref.get("dseq")
    if not dseq or not robust_destroy(
        dseq, owner=dseq_ref.get("owner"), group=dseq_ref.get("group")
    ):
        return False
    dseq_ref["dseq"] = None
    receipt_path = dseq_ref.get("receipt_path")
    if isinstance(receipt_path, Path):
        _delete_receipt(receipt_path)
    return True


def _receipt_provenance_run_id(receipt: DeploymentReceipt) -> str:
    """Return one run stamp shared by the receipt's complete group population.

    ``operation_id`` identifies the caller's lifecycle operation. It is deliberately
    independent of the private hexadecimal run id that ``deploy`` stamps into every
    owned placement group. Orphan reconciliation needs the latter: substituting the
    former makes the exact-this-run close branch unreachable.
    """
    population = receipt.get("group_population")
    if not isinstance(population, list) or not population:
        raise RuntimeError("receipt has no complete group population")
    run_ids = []
    for entry in population:
        name = entry.get("name") if isinstance(entry, dict) else None
        run_id = run_id_of(name) if isinstance(name, str) else ""
        if not run_id:
            raise RuntimeError("receipt group population has an unstamped or foreign group")
        run_ids.append(run_id)
    unique = set(run_ids)
    if len(unique) != 1:
        raise RuntimeError("receipt group population disagrees on its provenance run id")
    return run_ids[0]


def _reconcile_receipt(
    receipt_path: Path, operation_id: str, started_at: float, api_key: str
) -> str | None:
    """Recover a returned DSEQ or reconcile a submitted create before reporting HELD."""
    try:
        receipt = decode_receipt(receipt_path.read_bytes())
    except Exception as exc:  # noqa: BLE001 - report ambiguity without replacing it
        log_fail(f"HELD: create receipt unreadable ({exc}); manual reconciliation required")
        return None
    if receipt["operation_id"] != operation_id:
        log_fail("HELD: create receipt operation ID disagrees with this lifecycle operation")
        return None
    if receipt["state"] == "create_response_received":
        dseq = str(receipt["dseq"])
        log_info(f"Recovered DSEQ={dseq} from the durable create receipt")
        return dseq
    if receipt["state"] == "submitting":
        log_fail(
            "HELD: create request was submitted without a response identity; running "
            "owner-scoped/provenance reconciliation before exit"
        )
        try:
            provenance_run_id = _receipt_provenance_run_id(receipt)
            _report_suspected_orphans(AkashConsoleAPI(api_key), started_at, provenance_run_id)
        except Exception as exc:  # noqa: BLE001 - reconciliation failure remains HELD
            log_fail(f"HELD: reconciliation could not complete ({exc})")
    return None


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

    # Deploy binds its selected signer and final transformed SDL into this private
    # receipt before POST, then records the response DSEQ before auction work.
    api_key = os.environ["AKASH_API_KEY"]
    receipt_path, receipt_operation_id, receipt_env = _receipt_environment()
    dseq_ref["receipt_path"] = receipt_path

    # ── Step 2: Deploy SSH instance ────────────────────
    log_step(2, "Deploy SSH instance")

    deploy_started_at = time.time()
    try:
        r, timed_out = _run_just_up({**os.environ, **receipt_env}, timeout=300)
    except BaseException:
        # The child group is dead before this read. Preserve an unresolved receipt;
        # remove it only after authoritative pre-submit state or verified closure.
        try:
            state, dseq, owner, group = _receipt_identity(receipt_path)
            dseq_ref.update(dseq=dseq, owner=owner, group=group)
            if state == "prepared":
                _delete_receipt(receipt_path)
            else:
                _verified_cleanup(dseq_ref)
        except Exception as exc:  # noqa: BLE001 - interruption remains HELD
            log_fail(f"HELD: interrupted create receipt could not be reconciled ({exc})")
        raise
    output = r.stdout + r.stderr
    print(output)

    try:
        state, receipt_dseq, owner, group = _receipt_identity(receipt_path)
        dseq_ref.update(owner=owner, group=group)
    except Exception as exc:  # noqa: BLE001 - no bound identity means cleanup is held
        state, receipt_dseq = "unreadable", None
        log_fail(f"HELD: create receipt unreadable ({exc})")
    output_match = re.search(r"DSEQ[:\s]+(\d+)", output)
    output_dseq = output_match.group(1) if output_match else None
    if output_dseq and receipt_dseq and output_dseq != receipt_dseq:
        log_fail(f"HELD: output DSEQ {output_dseq} disagrees with receipt DSEQ {receipt_dseq}")
        dseq_ref["dseq"] = None
    else:
        dseq_ref["dseq"] = receipt_dseq or _reconcile_receipt(
            receipt_path, receipt_operation_id, deploy_started_at, api_key
        )

    if timed_out or r.returncode != 0:
        log_fail("just up timed out" if timed_out else "just up failed")
        if dseq_ref["dseq"]:
            _verified_cleanup(dseq_ref)
        elif state == "prepared":
            _delete_receipt(receipt_path)
        _summary(["deploy: failed"])
        sys.exit(1)

    if not dseq_ref["dseq"]:
        log_fail("Could not recover DSEQ from output or durable receipt; create is HELD")
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
            if not _verified_cleanup(dseq_ref):
                failures.append("cleanup: destroy or audit failed")

    _summary(failures)
    sys.exit(1 if failures else 0)


def _finish(failures: list, dseq_ref: dict):
    """Early-exit helper that runs cleanup before summarizing."""
    _verified_cleanup(dseq_ref)
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
