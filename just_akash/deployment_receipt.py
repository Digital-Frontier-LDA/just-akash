"""Local recovery receipt for an opt-in Akash deployment create.

The receipt is a recovery handle, not close authority. It records what the caller
expected and what the create endpoint returned early enough that later auction,
lease, or readiness failures cannot erase the DSEQ. A cleanup path must still verify
current chain state and ownership before acting.

This is a migration primitive, not fleet-standard conformance. Local fsync survives a
process crash but does not prove persistence across ephemeral-host loss. The record also
lacks authoritative producer/workflow/backend provenance, so it cannot grant Guardian
create or close authority by itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

import yaml
from yaml.nodes import MappingNode, ScalarNode

from .address import is_canonical_akash_address

RECEIPT_TYPE = "just-akash/deployment-create/v1"
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_TRANSACTION_KEYS = (
    "txhash",
    "txHash",
    "tx_hash",
    "transactionHash",
    "transaction_hash",
    "transactionId",
    "transaction_id",
)


class _DeploymentReceiptBase(TypedDict):
    receipt_type: str
    operation_id: str
    expected_owner: str
    group_population: list[dict[str, object]]
    group_population_digest: str
    artifact_digest: str
    prepared_at: str


class PreparedDeploymentReceipt(_DeploymentReceiptBase):
    state: Literal["prepared"]


class SubmittingDeploymentReceipt(_DeploymentReceiptBase):
    state: Literal["submitting"]
    submitting_at: str


class CreateResponseDeploymentReceipt(SubmittingDeploymentReceipt):
    state: Literal["create_response_received"]
    authority: Literal["candidate"]
    signer_owner_candidate: str
    owner_evidence_source: Literal["console_account_address"]
    dseq: str
    create_response_digest: str
    create_transaction_identifiers: dict[str, str]
    response_received_at: str


DeploymentReceipt = (
    PreparedDeploymentReceipt | SubmittingDeploymentReceipt | CreateResponseDeploymentReceipt
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode()


def artifact_identity(sdl_content: str) -> tuple[list[dict[str, object]], str, str]:
    """Derive ordered on-chain group identity and exact submitted-byte digest."""

    document = yaml.compose(sdl_content)
    if not isinstance(document, MappingNode):
        raise RuntimeError("receipt SDL must be a YAML mapping")

    def child(mapping: MappingNode, wanted: str) -> MappingNode:
        matches = [
            value
            for key, value in mapping.value
            if isinstance(key, ScalarNode) and key.value == wanted
        ]
        if len(matches) != 1 or not isinstance(matches[0], MappingNode):
            raise RuntimeError(f"receipt SDL must contain exactly one mapping at {wanted!r}")
        return matches[0]

    placements = child(child(document, "profiles"), "placement")
    if any(
        not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str"
        for key, _ in placements.value
    ):
        raise RuntimeError("receipt placement group names must be scalar strings")
    names = [key.value for key, _ in placements.value if isinstance(key, ScalarNode)]
    if not names:
        raise RuntimeError("receipt requires at least one placement group in the submitted SDL")
    if len(names) != len(set(names)):
        raise RuntimeError("receipt group population contains duplicate placement names")

    deployment = child(document, "deployment")
    deployment_order: list[str] = []
    for service_key, service_value in deployment.value:
        if not isinstance(service_key, ScalarNode) or not isinstance(service_value, MappingNode):
            raise RuntimeError("receipt deployment entries must map service names to placements")
        for placement_key, _ in service_value.value:
            if (
                not isinstance(placement_key, ScalarNode)
                or placement_key.tag != "tag:yaml.org,2002:str"
            ):
                raise RuntimeError("receipt deployment placement names must be scalar strings")
            if placement_key.value not in deployment_order:
                deployment_order.append(placement_key.value)
    if deployment_order != names:
        raise RuntimeError(
            "receipt requires profiles.placement and deployment references to contain the "
            "same complete group population in the same gseq order"
        )
    population: list[dict[str, object]] = [
        {"gseq": index, "name": name} for index, name in enumerate(names, start=1)
    ]
    population_digest = sha256_bytes(_canonical_bytes(population))
    artifact_digest = sha256_bytes(sdl_content.encode("utf-8"))
    return population, population_digest, artifact_digest


def _private_parent(path: Path) -> Path:
    if not path.is_absolute():
        raise RuntimeError("receipt path must be absolute")
    parent = path.parent
    try:
        info = parent.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"receipt parent does not exist: {parent}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("receipt parent must be a real directory, not a symlink")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeError("receipt parent must be owned by the current user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("receipt parent must have exact private mode 0700")
    return parent


def _reject_existing_leaf(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise RuntimeError(f"receipt path already exists; refusing to overwrite: {path}")


def _fsync_parent(parent: Path) -> None:
    directory_fd = os.open(parent, os.O_RDONLY | _O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_temp(parent: Path, path: Path, payload: bytes) -> Path:
    temporary = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
    return temporary


def _create_durable(path: Path, payload: bytes) -> None:
    parent = _private_parent(path)
    _reject_existing_leaf(path)
    temporary = _write_temp(parent, path, payload)
    try:
        # link(), unlike replace(), cannot overwrite a leaf created after our lstat.
        os.link(temporary, path, follow_symlinks=False)
        os.unlink(temporary)
        _fsync_parent(parent)
    except FileExistsError as exc:
        raise RuntimeError(
            f"receipt path appeared concurrently; refusing to overwrite: {path}"
        ) from exc
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _read_owned_prepared(path: Path, expected: bytes) -> None:
    fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("prepared receipt must remain a private regular file (mode 0600)")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise RuntimeError("prepared receipt must remain owned by the current user")
        with os.fdopen(fd, "rb", closefd=True) as stream:
            fd = -1
            actual = stream.read()
    finally:
        if fd >= 0:
            os.close(fd)
    if actual != expected:
        raise RuntimeError(
            "prepared receipt changed before create completed; refusing to overwrite"
        )


def _replace_prepared(path: Path, expected: bytes, payload: bytes) -> None:
    parent = _private_parent(path)
    _read_owned_prepared(path, expected)
    temporary = _write_temp(parent, path, payload)
    try:
        # The private, current-user-owned parent excludes an untrusted leaf-swap race.
        os.replace(temporary, path)
        try:
            _fsync_parent(parent)
        except Exception:
            # Restore the last durable state as the visible recovery seed. A caller must
            # never infer response persistence from a rename whose directory fsync failed.
            rollback = _write_temp(parent, path, expected)
            try:
                os.replace(rollback, path)
                with suppress(Exception):
                    _fsync_parent(parent)
            finally:
                with suppress(FileNotFoundError):
                    rollback.unlink()
            raise
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def prepare_receipt(
    path: str,
    *,
    operation_id: str,
    expected_owner: str,
    expected_groups: list[str],
    expected_artifact_digest: str,
    sdl_content: str,
) -> tuple[Path, PreparedDeploymentReceipt, bytes]:
    """Validate caller expectations and durably create the pre-send receipt."""

    population, population_digest, artifact_digest = artifact_identity(sdl_content)
    derived_groups = [str(group["name"]) for group in population]
    if derived_groups != expected_groups:
        raise RuntimeError(
            "receipt expected group population does not equal the complete submitted "
            "SDL population"
        )
    if not is_canonical_akash_address(expected_owner):
        raise RuntimeError("receipt expected owner must be a canonical akash1 address")
    if not isinstance(operation_id, str) or not re.fullmatch(
        r"[A-Za-z0-9._:-]{1,128}", operation_id
    ):
        raise RuntimeError("receipt operation ID has an invalid or empty shape")
    if not isinstance(expected_artifact_digest, str) or not re.fullmatch(
        r"[0-9A-Fa-f]{64}", expected_artifact_digest
    ):
        raise RuntimeError("receipt artifact digest must be exactly 64 hexadecimal characters")
    normalized_digest = expected_artifact_digest.lower()
    if normalized_digest != artifact_digest:
        raise RuntimeError(
            f"receipt artifact digest mismatch: expected {normalized_digest}, "
            f"derived {artifact_digest}"
        )
    receipt: PreparedDeploymentReceipt = {
        "receipt_type": RECEIPT_TYPE,
        "state": "prepared",
        "operation_id": operation_id,
        "expected_owner": expected_owner,
        "group_population": population,
        "group_population_digest": population_digest,
        "artifact_digest": artifact_digest,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
    }
    encoded = _canonical_bytes(receipt)
    receipt_path = Path(path)
    _create_durable(receipt_path, encoded)
    return receipt_path, receipt, encoded


def mark_create_response_received(
    path: Path,
    submitting: SubmittingDeploymentReceipt,
    submitting_bytes: bytes,
    *,
    dseq: object,
    deployment_response: dict[str, Any],
) -> CreateResponseDeploymentReceipt:
    """Record a Console response candidate without claiming chain-created authority."""

    if isinstance(dseq, bool) or not re.fullmatch(r"[1-9][0-9]*", str(dseq)):
        raise RuntimeError("create returned a non-canonical DSEQ; submitting receipt retained")
    dseq_number = int(str(dseq))
    if dseq_number > (2**64 - 1):
        raise RuntimeError("create returned a DSEQ outside uint64; submitting receipt retained")
    transaction = {
        key: value
        for key in _TRANSACTION_KEYS
        if isinstance((value := deployment_response.get(key)), str) and value
    }
    response_receipt: CreateResponseDeploymentReceipt = {
        **submitting,
        "state": "create_response_received",
        "authority": "candidate",
        "signer_owner_candidate": submitting["expected_owner"],
        "owner_evidence_source": "console_account_address",
        "dseq": str(dseq),
        "create_response_digest": sha256_bytes(_canonical_bytes(deployment_response)),
        "create_transaction_identifiers": transaction,
        "response_received_at": datetime.now(timezone.utc).isoformat(),
    }
    _replace_prepared(path, submitting_bytes, _canonical_bytes(response_receipt))
    return response_receipt


def mark_submitting(
    path: Path,
    prepared: PreparedDeploymentReceipt,
    prepared_bytes: bytes,
) -> tuple[Path, SubmittingDeploymentReceipt, bytes]:
    """Durably record that the non-idempotent create request is about to be sent."""

    submitting: SubmittingDeploymentReceipt = {
        **prepared,
        "state": "submitting",
        "submitting_at": datetime.now(timezone.utc).isoformat(),
    }
    encoded = _canonical_bytes(submitting)
    _replace_prepared(path, prepared_bytes, encoded)
    return path, submitting, encoded


def decode_receipt(payload: bytes) -> DeploymentReceipt:
    """Decode only local recovery states; chain-created authority is intentionally absent."""

    try:
        document = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("deployment receipt is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("receipt_type") != RECEIPT_TYPE:
        raise RuntimeError("deployment receipt has an unknown type")
    state = document.get("state")
    if state not in {"prepared", "submitting", "create_response_received"}:
        raise RuntimeError(f"deployment receipt has unsupported authority state {state!r}")
    base_keys = {
        "receipt_type",
        "state",
        "operation_id",
        "expected_owner",
        "group_population",
        "group_population_digest",
        "artifact_digest",
        "prepared_at",
    }
    state_keys = {
        "prepared": set(),
        "submitting": {"submitting_at"},
        "create_response_received": {
            "submitting_at",
            "authority",
            "signer_owner_candidate",
            "owner_evidence_source",
            "dseq",
            "create_response_digest",
            "create_transaction_identifiers",
            "response_received_at",
        },
    }
    if set(document) != base_keys | state_keys[state]:
        raise RuntimeError("deployment receipt fields do not match its typed state")
    required_strings = (
        "operation_id",
        "expected_owner",
        "group_population_digest",
        "artifact_digest",
        "prepared_at",
    )
    if any(
        not isinstance(document.get(key), str) or not document[key] for key in required_strings
    ):
        raise RuntimeError("deployment receipt has missing or invalid identity fields")
    if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", document["operation_id"]) is None:
        raise RuntimeError("deployment receipt operation ID is invalid")
    if not is_canonical_akash_address(document["expected_owner"]):
        raise RuntimeError("deployment receipt has a non-canonical expected owner")
    population = document.get("group_population")
    if not isinstance(population, list) or not population:
        raise RuntimeError("deployment receipt has an invalid group population")
    expected_population = [
        {"gseq": index, "name": entry.get("name") if isinstance(entry, dict) else None}
        for index, entry in enumerate(population, start=1)
    ]
    if population != expected_population or any(
        not isinstance(entry["name"], str) or not entry["name"] for entry in expected_population
    ):
        raise RuntimeError("deployment receipt group population is not canonical")
    if document["group_population_digest"] != sha256_bytes(_canonical_bytes(population)):
        raise RuntimeError("deployment receipt group population digest does not match")
    if re.fullmatch(r"[0-9a-f]{64}", document["artifact_digest"]) is None:
        raise RuntimeError("deployment receipt artifact digest is invalid")
    try:
        prepared_at = datetime.fromisoformat(document["prepared_at"])
    except ValueError as exc:
        raise RuntimeError("deployment receipt prepared timestamp is invalid") from exc
    if prepared_at.tzinfo is None:
        raise RuntimeError("deployment receipt prepared timestamp has no timezone")
    if state in {"submitting", "create_response_received"}:
        submitting_at_raw = document.get("submitting_at")
        if not isinstance(submitting_at_raw, str):
            raise RuntimeError("submitting receipt has no timestamp")
        try:
            submitting_at = datetime.fromisoformat(submitting_at_raw)
        except ValueError as exc:
            raise RuntimeError("submitting receipt timestamp is invalid") from exc
        if submitting_at.tzinfo is None:
            raise RuntimeError("submitting receipt timestamp has no timezone")
        if prepared_at > submitting_at:
            raise RuntimeError("submitting receipt predates preparation")
    if state == "create_response_received" and document.get("authority") != "candidate":
        raise RuntimeError("create response receipt must remain candidate evidence")
    if state == "create_response_received":
        if not is_canonical_akash_address(document.get("signer_owner_candidate")):
            raise RuntimeError("create response receipt has invalid owner candidate")
        if document["signer_owner_candidate"] != document["expected_owner"]:
            raise RuntimeError("create response receipt owner candidate changed")
        if document.get("owner_evidence_source") != "console_account_address":
            raise RuntimeError("create response receipt has invalid owner evidence source")
        dseq = document.get("dseq")
        if (
            isinstance(dseq, bool)
            or not isinstance(dseq, str)
            or not re.fullmatch(r"[1-9][0-9]*", dseq)
            or int(dseq) > 2**64 - 1
        ):
            raise RuntimeError("create response receipt has invalid DSEQ")
        if re.fullmatch(r"[0-9a-f]{64}", str(document.get("create_response_digest"))) is None:
            raise RuntimeError("create response receipt digest is invalid")
        transaction = document.get("create_transaction_identifiers")
        if not isinstance(transaction, dict) or any(
            key not in _TRANSACTION_KEYS or not isinstance(value, str) or not value
            for key, value in transaction.items()
        ):
            raise RuntimeError("create response receipt transaction metadata is invalid")
        received_raw = document.get("response_received_at")
        if not isinstance(received_raw, str):
            raise RuntimeError("create response receipt timestamp is invalid")
        try:
            received_at = datetime.fromisoformat(received_raw)
        except ValueError as exc:
            raise RuntimeError("create response receipt timestamp is invalid") from exc
        if received_at.tzinfo is None:
            raise RuntimeError("create response receipt timestamp has no timezone")
        if submitting_at > received_at:
            raise RuntimeError("create response receipt predates submission")
    return cast(DeploymentReceipt, document)
