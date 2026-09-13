"""Crash-contained, receipt-bound execution for commands that spend Akash escrow."""

from __future__ import annotations

import os
import re
import secrets
import signal
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path

from . import chain
from ._e2e import robust_destroy
from .deployment_receipt import DeploymentReceipt, decode_receipt
from .provenance import run_id_of


def receipt_environment(label: str, *, unique: bool = False) -> tuple[Path, str, dict[str, str]]:
    root = os.environ.get("RUNNER_TEMP")
    if root:
        parent = Path(root) / "just-akash-deployment-receipts"
        parent.mkdir(mode=0o700, parents=False, exist_ok=True)
        parent.chmod(0o700)
    else:
        parent = Path(tempfile.mkdtemp(prefix="just-akash-deployment-receipts-"))
    suffix = f"-{secrets.token_hex(8)}" if unique else ""
    path = parent / f"{label}{suffix}.json"
    operation_id = f"{label}-{secrets.token_hex(8)}"
    return (
        path,
        operation_id,
        {
            "JUST_AKASH_RECEIPT_PATH": str(path),
            "JUST_AKASH_RECEIPT_OPERATION_ID": operation_id,
        },
    )


def run_process_group(
    argv: list[str], *, env: dict[str, str], timeout: int
) -> tuple[subprocess.CompletedProcess, bool]:
    process = subprocess.Popen(  # noqa: S603 - argv is always an explicit vector
        argv,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )

    def terminate_group() -> tuple[str, str]:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            return process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            return process.communicate()

    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr), False
    except subprocess.TimeoutExpired:
        stdout, stderr = terminate_group()
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr), True
    except BaseException:
        terminate_group()
        raise


def receipt_identity(path: Path) -> tuple[DeploymentReceipt, str | None]:
    receipt = decode_receipt(path.read_bytes())
    dseq = str(receipt["dseq"]) if receipt["state"] == "create_response_received" else None
    return receipt, dseq


def delete_receipt(path: Path) -> None:
    path.unlink(missing_ok=True)
    with suppress(OSError):
        path.parent.rmdir()


def verified_cleanup(ref: dict) -> bool:
    dseq = ref.get("dseq")
    owner = ref.get("owner")
    groups = ref.get("groups")
    if not dseq or not isinstance(owner, str) or not isinstance(groups, list):
        return False
    if not robust_destroy(str(dseq), owner=owner, groups=groups):
        return False
    ref["dseq"] = None
    path = ref.get("receipt_path")
    if isinstance(path, Path):
        delete_receipt(path)
    return True


def _receipt_run_id(receipt: DeploymentReceipt) -> str:
    names = [entry.get("name") for entry in receipt["group_population"]]
    if not all(isinstance(name, str) for name in names):
        raise RuntimeError("receipt population contains an invalid group name")
    run_ids = {run_id_of(name) for name in names if isinstance(name, str)}
    if len(run_ids) != 1 or "" in run_ids:
        raise RuntimeError("receipt population has no single agreeing provenance run stamp")
    return next(iter(run_ids))


def reconcile_receipt(path: Path, operation_id: str, started_at: float, ref: dict) -> str | None:
    """Recover a response DSEQ or safely close one uniquely corroborated submit."""
    receipt, dseq = receipt_identity(path)
    if receipt["operation_id"] != operation_id:
        return None
    run_id = _receipt_run_id(receipt)
    if receipt["operation_id"] == run_id:
        return None
    ref.update(
        owner=receipt["expected_owner"],
        groups=receipt["group_population"],
        receipt_path=path,
    )
    if dseq is not None:
        return dseq
    if receipt["state"] != "submitting":
        return None
    active = chain.list_active_deployments(receipt["expected_owner"])
    if active is None:
        return None
    since_ms = int(started_at * 1000)
    matches: list[str] = []
    for row in active:
        identity = row.get("id") if isinstance(row, dict) else None
        candidate = (
            identity.get("dseq")
            if isinstance(identity, dict)
            else row.get("dseq")
            if isinstance(row, dict)
            else None
        )
        if not re.fullmatch(r"[1-9][0-9]*", str(candidate)) or int(str(candidate)) < since_ms:
            continue
        candidate_text = str(candidate)
        if (
            chain.corroborated_deployment_group_population(
                receipt["expected_owner"], candidate_text, receipt["group_population"]
            )
            == receipt["group_population"]
        ):
            matches.append(candidate_text)
    if len(matches) != 1:
        return None
    ref["dseq"] = matches[0]
    verified_cleanup(ref)
    return None
