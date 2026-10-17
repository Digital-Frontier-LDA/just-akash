"""Opt-in durable JIT mint observation, never controller or create authority.

Caller operation/run/source strings are recorded claims, not authenticated context.
UNKNOWN survives success, malformed/lost ACKs and candidate persistence failure.
Neither receipt contains credentials or qualifies registration, delivery or cleanup.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .deployment_receipt import (
    _canonical_bytes,
    _create_durable,
    _private_parent,
    _read_owned_prepared,
    _reject_existing_leaf,
)

if TYPE_CHECKING:
    from .github_jit import JitPolicy


def response_path(path: Path) -> Path:
    return path.with_suffix(".response.json")


def inspect_slot(
    path: str, operation_id: str, run_id: str, run_attempt: str, source_revision: str
) -> Path:
    """Validate data and refuse a used private slot before any GitHub request."""
    if (
        not isinstance(operation_id, str)
        or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", operation_id) is None
        or any(
            not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,19}", value) is None
            for value in (run_id, run_attempt)
        )
        or any(int(value) >= 2**64 for value in (run_id, run_attempt))
        or not isinstance(source_revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_revision) is None
    ):
        raise RuntimeError("invalid JIT receipt observation identifiers")
    target = Path(path)
    if ".." in target.parts or response_path(target) == target:
        raise RuntimeError("invalid JIT receipt slot")
    _private_parent(target)
    _reject_existing_leaf(target)
    _reject_existing_leaf(response_path(target))
    return target


@dataclass(frozen=True)
class PendingMint:
    path: Path
    intent: bytes


def prepare(
    path: Path,
    *,
    policy: JitPolicy,
    operation_id: str,
    run_id: str,
    run_attempt: str,
    source_revision: str,
    runner_name: str,
    labels: tuple[str, ...],
    producer_revision: str | None,
) -> PendingMint:
    """Fsync an immutable intent at the actual policy-verified POST boundary."""
    value = {
        "schema": "just-akash/jit-mint-attempt/v1",
        "state": "UNKNOWN",
        "operation_id": operation_id,
        "controller_claim": {
            "run_id": run_id,
            "run_attempt": run_attempt,
            "source_revision": source_revision,
            "authenticated": False,
        },
        "policy": {
            "revision": policy.revision,
            "repository_id": policy.repository_id,
            "repository_name": policy.repository_name,
            "group_id": policy.group_id,
            "workflows": list(policy.workflows),
            "non_reusable_workflow": policy.non_reusable_workflow,
            "source_workflow_revision": policy.source_workflow_revision,
            "source_workflow_branch": policy.source_workflow_branch,
        },
        "producer_workflow_revision": producer_revision,
        "request_path": policy.api_root + "/runners/generate-jitconfig",
        "runner_name": runner_name,
        "labels": list(labels),
        "work_folder": "_work",
        "registration_verified": False,
        "delivery_verified": False,
        "chain_allocation_verified": False,
        "publication_authority": False,
    }
    encoded = _canonical_bytes(value)
    _create_durable(path, encoded)
    return PendingMint(path, encoded)


def response_received(pending: PendingMint, runner_id: int, runner_name: str) -> None:
    """Persist only the validated returned runner identity, never the config."""
    _read_owned_prepared(pending.path, pending.intent)
    value = {
        "schema": "just-akash/jit-mint-response/v1",
        "state": "RESPONSE_CANDIDATE",
        "intent_sha256": hashlib.sha256(pending.intent).hexdigest(),
        "runner_id": runner_id,
        "runner_name": runner_name,
        "registration_verified": False,
        "delivery_verified": False,
        "chain_allocation_verified": False,
        "publication_authority": False,
    }
    _create_durable(response_path(pending.path), _canonical_bytes(value))
