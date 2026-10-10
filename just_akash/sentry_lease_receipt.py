"""Opt-in Sentry lease-attempt capture at the SDK's actual Console lease wire.

UNKNOWN is durable before the request and is never overwritten or reusable.
A separate response candidate records only a digest, never a manifest or token.
Neither file establishes chain creation, runner binding, publication authority,
or the still-unimplemented whole-route 600-second controller.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .deployment_receipt import (
    _canonical_bytes,
    _create_durable,
    _private_parent,
    _read_owned_prepared,
    _reject_existing_leaf,
    artifact_identity,
    decode_receipt,
)
from .request_profile import derive_resource_profiles
from .runner_image import NATIVE_READER_PROVIDERS

PROFILE = {
    "cpu_millicores": 2000,
    "memory_bytes": 6 * 1024**3,
    "storage_bytes": 40 * 1024**3,
    "gpu_count": 0,
    "replicas": 1,
}


def response_path(path: Path) -> Path:
    return path.with_suffix(".response.json")


def inspect_slot(path: str, create_path: str) -> Path:
    """Refuse a prior UNKNOWN/candidate before any new SDK create is attempted."""
    target = Path(path)
    if (
        target == Path(create_path)
        or response_path(target) == Path(create_path)
        or ".." in target.parts
        or response_path(target) == target
    ):
        raise RuntimeError("Sentry lease receipt slot is invalid")
    _private_parent(target)
    _reject_existing_leaf(target)
    _reject_existing_leaf(response_path(target))
    return target


def payload_profile(sdl: str) -> tuple[str, str]:
    """Derive the exact single-replica submitted request; no admitted authority."""
    population, _, digest = artifact_identity(sdl)
    if len(population) != 1 or population[0]["gseq"] != 1:
        raise RuntimeError("Sentry lease receipt requires exactly one group")
    group = str(population[0]["name"])
    identity = re.fullmatch(
        r"borduas-sentry-idv1-class-ci-runner-g1-attempt-([1-9][0-9]{0,19})-run-([1-9][0-9]{0,19})-end",
        group,
    )
    if identity is None or any(int(value) >= 2**64 for value in identity.groups()):
        raise RuntimeError("Sentry lease receipt requires versioned placement identity")
    derived = derive_resource_profiles(sdl)
    if derived.unavailable_reason is not None or set(derived.profiles) != {1}:
        raise RuntimeError("Sentry lease resource profile could not be derived")
    profile = derived.profiles[1]
    shape = {
        key: getattr(profile, key)
        for key in ("cpu_millicores", "memory_bytes", "storage_bytes", "gpu_count")
    }
    shape["replicas"] = len(profile.replicas)
    if shape != PROFILE or derived.sdl_sha256 != digest:
        raise RuntimeError("Sentry lease receipt requires original single-runner resources")
    return group, digest


def _create_candidate(path: Path) -> tuple[dict, bytes]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        initial = os.fstat(fd)
        if (
            not stat.S_ISREG(initial.st_mode)
            or stat.S_IMODE(initial.st_mode) != 0o600
            or initial.st_uid != os.getuid()
            or initial.st_nlink != 1
            or not 0 < initial.st_size <= 65536
        ):
            raise RuntimeError("Sentry create receipt is not private bounded evidence")
        raw = os.read(fd, 65537)
        current = path.lstat()

        def identity(info):
            return (
                info.st_dev,
                info.st_ino,
                info.st_uid,
                info.st_mode,
                info.st_nlink,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )

        if (
            identity(initial) != identity(os.fstat(fd))
            or identity(initial) != identity(current)
            or len(raw) != initial.st_size
        ):
            raise RuntimeError("Sentry create receipt changed during capture")
    finally:
        os.close(fd)
    return dict(decode_receipt(raw)), raw


@dataclass(frozen=True)
class PendingLease:
    path: Path
    intent: bytes


def prepare(
    path: Path,
    *,
    create_path: Path,
    operation_id: str,
    sdl: str,
    dseq: str,
    provider: str,
    lease_group: int,
) -> PendingLease:
    """Capture the actual selected lease arguments, fsync UNKNOWN before wire."""
    group, digest = payload_profile(sdl)
    create, raw = _create_candidate(create_path)
    if (
        create["state"] != "create_response_received"
        or create["authority"] != "candidate"
        or create["operation_id"] != operation_id
        or create["artifact_digest"] != digest
        or create["group_population"] != [{"gseq": 1, "name": group}]
        or create["dseq"] != dseq
        or provider not in NATIVE_READER_PROVIDERS
        or type(lease_group) is not int
        or lease_group != 1
    ):
        raise RuntimeError("Sentry lease attempt disagrees with captured create and owned request")
    value = {
        "schema": "just-akash/sentry-lease-attempt/v1",
        "state": "UNKNOWN",
        "operation_id": operation_id,
        "owner": create["expected_owner"],
        "dseq": dseq,
        "gseq": lease_group,
        "oseq": 1,  # The existing SDK create_lease default, not caller input.
        "provider": provider,
        "group": group,
        "resource_profile": PROFILE,
        "sdl_sha256": digest,
        "deployment_receipt_sha256": hashlib.sha256(raw).hexdigest(),
        "credential_binding": create.get("credential_binding"),
        "chain_allocation_verified": False,
        "runner_binding_verified": False,
        "publication_authority": False,
    }
    encoded = _canonical_bytes(value)
    _create_durable(path, encoded)
    return PendingLease(path, encoded)


def response_received(pending: PendingLease, response: dict) -> None:
    """Retain a response candidate while the immutable UNKNOWN intent survives."""
    _read_owned_prepared(pending.path, pending.intent)
    if type(response) is not dict:
        raise RuntimeError("Sentry lease response is not a candidate object")
    value = {
        "schema": "just-akash/sentry-lease-response/v1",
        "state": "RESPONSE_CANDIDATE",
        "intent_sha256": hashlib.sha256(pending.intent).hexdigest(),
        "response_sha256": hashlib.sha256(_canonical_bytes(response)).hexdigest(),
        "chain_allocation_verified": False,
        "runner_binding_verified": False,
        "publication_authority": False,
    }
    _create_durable(response_path(pending.path), _canonical_bytes(value))
