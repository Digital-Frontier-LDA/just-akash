"""Held source-owned legacy lease boundary; no acquisition or settlement authority.

The registered backend must durably retain all pending/live debt, validate the
original caller deadline at each callback, and approve every fresh outer attempt.
These typed callback returns are observations of that backend call, not permits
created by this module. Registration remains unavailable until reviewed code
supplies exact source pins and a concrete authenticated backend.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class AtomicAdmissionHeld(RuntimeError):
    """Pre-wire refusal, containing only a fixed diagnostic."""


class LeaseWireUnknown(Exception):
    """Not a RuntimeError: ordinary deploy retry handling must not catch this."""


class UnknownKind(str, Enum):
    TRANSPORT_OR_RESPONSE = "TRANSPORT_OR_RESPONSE"
    CANCELLED = "CANCELLED"
    BOOKKEEPING = "BOOKKEEPING"
    AMBIGUOUS_HTTP = "AMBIGUOUS_HTTP"


@dataclass(frozen=True)
class LeaseSend:
    dseq: str
    provider: str
    gseq: int
    oseq: int
    manifest: str = field(repr=False)

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.manifest.encode()).hexdigest()


@dataclass(frozen=True)
class LeaseIdentity:
    dseq: str
    provider: str
    gseq: int
    oseq: int
    manifest_sha256: str


@dataclass(frozen=True)
class LeaseSource:
    api_sha256: str
    hook_sha256: str


@dataclass(frozen=True)
class LeaseBinding:
    operation_id: str
    attempt_id: str
    client: object = field(repr=False, compare=False)
    send: LeaseIdentity = field(repr=False)
    body_sha256: str
    source: LeaseSource


@dataclass(frozen=True)
class HTTPObservation:
    status: int
    effect_ambiguous: bool
    settlement: bool = False


@dataclass(frozen=True)
class RecordedOutcome:
    binding: LeaseBinding = field(repr=False)
    state: str  # Exactly OBSERVED_HTTP or UNKNOWN; never settled/released.


class LeaseAdmissionBackend:
    """Code-only concrete integration seam; no default implementation.

    before_send receives only manifest/body digests, never raw manifest/body.
    The actual client remains inside the trusted hosted process.
    before_send atomically binds a NEW attempt, keeps prior charges and durable
    pre-wire UNKNOWN, and checks source/owner/resources/original deadline. A
    returned HTTP observation NEVER itself authorizes another attempt. Both
    callbacks must retain debt and enforce the SAME original deadline; late,
    failed or lost callback acknowledgement is UNKNOWN, never a replay grant.
    """

    def before_send(self, client, send, *, body_sha256, source) -> LeaseBinding:
        raise NotImplementedError

    def observed_http(self, binding, observation: HTTPObservation) -> RecordedOutcome:
        raise NotImplementedError

    def retain_unknown(self, binding, reason: UnknownKind) -> RecordedOutcome:
        raise NotImplementedError


@dataclass(frozen=True)
class Registration:
    source: LeaseSource
    backend: LeaseAdmissionBackend = field(repr=False)


def _reviewed_hook() -> Registration | None:
    # Root must supply an exact reviewed source registration; no env override.
    return None


def require_available() -> Registration:
    try:
        value = _reviewed_hook()
    except BaseException:
        raise AtomicAdmissionHeld("atomic-admission-unavailable") from None
    if (
        type(value) is not Registration
        or type(value.source) is not LeaseSource
        or not isinstance(value.backend, LeaseAdmissionBackend)
        or not all(
            re.fullmatch(r"[0-9a-f]{64}", s or "")
            for s in (value.source.api_sha256, value.source.hook_sha256)
        )
    ):
        raise AtomicAdmissionHeld("atomic-admission-unavailable")
    return value


def _source_sha256(path: str) -> str:
    # Capture only this explicitly registered public module, never credentials.
    fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 1024 * 1024:
            raise AtomicAdmissionHeld("atomic-admission-source-unqualified")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        after = os.fstat(fd)
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if len(raw) != before.st_size or any(
            getattr(before, k) != getattr(after, k) for k in fields
        ):
            raise AtomicAdmissionHeld("atomic-admission-source-unqualified")
        result = hashlib.sha256(raw).hexdigest()
    finally:
        os.close(fd)
    return result


class LeaseAttempt:
    def __init__(self, registration, client, send):
        self.registration = registration
        self.client = client
        self.send = send
        self.identity = LeaseIdentity(
            send.dseq, send.provider, send.gseq, send.oseq, send.manifest_sha256
        )
        self.binding = None
        self.state = "PREPARED"
        self.http_status = None

    def before_open(self, client, method, endpoint, body, *, api_source):
        registered = require_available()
        if registered is not self.registration:
            raise AtomicAdmissionHeld("atomic-admission-registration-changed")
        registered_backend = registered.backend
        registered_source = registered.source
        if (
            self.state != "PREPARED"
            or client is not self.client
            or method != "POST"
            or endpoint != "/v1/leases"
            or type(body) is not bytes
        ):
            raise AtomicAdmissionHeld("atomic-admission-wire-mismatch")
        expected = {
            "manifest": self.send.manifest,
            "leases": [
                {
                    "dseq": self.send.dseq,
                    "gseq": self.send.gseq,
                    "oseq": self.send.oseq,
                    "provider": self.send.provider,
                }
            ],
        }
        # Use the unchanged original json.dumps encoding, not an equivalent dict.
        import json

        if body != json.dumps(expected).encode("utf-8"):
            raise AtomicAdmissionHeld("atomic-admission-wire-mismatch")
        try:
            if (
                _source_sha256(api_source) != self.registration.source.api_sha256
                or _source_sha256(__file__) != self.registration.source.hook_sha256
            ):
                raise AtomicAdmissionHeld("atomic-admission-source-unqualified")
            binding = self.registration.backend.before_send(
                client,
                self.identity,
                body_sha256=hashlib.sha256(body).hexdigest(),
                source=self.registration.source,
            )
        except BaseException:
            raise AtomicAdmissionHeld("atomic-admission-binding-unqualified") from None
        if (
            type(binding) is not LeaseBinding
            or binding.client is not client
            or binding.send is not self.identity
            or binding.source is not self.registration.source
            or binding.body_sha256 != hashlib.sha256(body).hexdigest()
            or not all(
                type(s) is str and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", s)
                for s in (binding.operation_id, binding.attempt_id)
            )
        ):
            raise AtomicAdmissionHeld("atomic-admission-binding-unqualified")
        # Rejoin after the backend callback as the final lower-boundary check.
        # Typed construction is not reviewed registration or a transport grant.
        if (
            require_available() is not registered
            or self.registration is not registered
            or registered.backend is not registered_backend
            or registered.source is not registered_source
        ):
            raise AtomicAdmissionHeld("atomic-admission-registration-changed")
        self.binding = binding
        self.state = "WIRE_UNKNOWN"  # backend has retained pre-wire intent/debt.

    def capture_http(self, status):
        if self.state != "WIRE_UNKNOWN" or type(status) is not int or not 100 <= status <= 599:
            raise LeaseWireUnknown("atomic-lease-unknown-no-retry")
        self.http_status = status

    def unknown(self, reason):
        if self.state == "PREPARED":
            return  # no opener entered; backend owns any incomplete acquisition.
        self.state = "UNKNOWN"
        try:
            recorded = self.registration.backend.retain_unknown(self.binding, reason)
            if (
                type(recorded) is not RecordedOutcome
                or recorded.binding is not self.binding
                or recorded.state != "UNKNOWN"
            ):
                raise ValueError
        except BaseException:
            raise LeaseWireUnknown("atomic-lease-unknown-bookkeeping-no-retry") from None

    def observed_http(self, status, *, effect_ambiguous=False):
        if self.state != "WIRE_UNKNOWN" or type(status) is not int or not 100 <= status <= 599:
            self.unknown(UnknownKind.BOOKKEEPING)
            raise LeaseWireUnknown("atomic-lease-unknown-no-retry")
        observation = HTTPObservation(status, effect_ambiguous or status >= 500 or status == 408)
        try:
            recorded = self.registration.backend.observed_http(self.binding, observation)
            if (
                type(recorded) is not RecordedOutcome
                or recorded.binding is not self.binding
                or recorded.state not in {"OBSERVED_HTTP", "UNKNOWN"}
            ):
                raise ValueError
        except BaseException:
            self.unknown(UnknownKind.BOOKKEEPING)
            raise LeaseWireUnknown("atomic-lease-unknown-no-retry") from None
        # Even a callback ACK cannot make a proxy/server timeout a no-land proof.
        if recorded.state == "UNKNOWN" or observation.effect_ambiguous:
            self.unknown(UnknownKind.AMBIGUOUS_HTTP)
            raise LeaseWireUnknown("atomic-lease-unknown-no-retry")
        self.state = "OBSERVED_HTTP"  # Still charged, never settlement/permission.


def prepare_lease_send(client, *, dseq, provider, manifest, gseq, oseq) -> LeaseAttempt:
    registration = require_available()
    if (
        type(dseq) is not str
        or not dseq.isascii()
        or not dseq.isdecimal()
        or dseq.startswith("0")
        or type(provider) is not str
        or not provider
        or type(manifest) is not str
        or not manifest
        or type(gseq) is not int
        or gseq <= 0
        or type(oseq) is not int
        or oseq <= 0
    ):
        raise AtomicAdmissionHeld("atomic-admission-identity-unqualified")
    return LeaseAttempt(registration, client, LeaseSend(dseq, provider, gseq, oseq, manifest))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args(argv)
    try:
        require_available()
    except AtomicAdmissionHeld:
        print("HELD: reviewed atomic SDK admission bridge not delivered")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
