"""Default-off hosted signed-receipt custody DATA; never admission or release authority.

The existing artifact action supplies extracted files. Native metadata joins its
archive digest; this module does not attest raw ZIP bytes or archive membership.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

import httpx

from . import guardian_legacy_lease_backend as backend
from . import legacy_capacity_cleanup as cleanup
from .hosted_legacy_cli import HostedCallResult

SCHEMA = "hosted-legacy-consume-custody/v1"
BUNDLE = "consume-custody.json"
CLOSED = "closed-observation.json"
MAX_BYTES = 4 * 1024 * 1024
MAX_HEADERS = 16384  # Retained HTTPX headers, AFTER HTTPX parses them.
MAX_WIRES = 16
REPOSITORIES = {
    "Borduas-Holdings/blazing": 1074974924,
    "Borduas-Holdings/Blazing-Back": 1071436278,
}


class CustodyHeld(Exception):
    """Finite uncertainty; neither retry nor zero debt follows from this error."""


def require(value: object) -> None:
    if not value:
        raise CustodyHeld("HOSTED_CUSTODY_UNKNOWN_NO_RETRY")


def integer(value: object) -> bool:
    return type(value) is int and 0 < cast(int, value) < 2**63


def digest(value: object, size: int = 64) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{" + str(size) + "}", value) is not None


def object_map(value: object) -> dict[str, object]:
    require(type(value) is dict and all(type(k) is str for k in cast(dict, value)))
    return cast("dict[str, object]", value)


def strict(raw: bytes) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result

    def number(value: str) -> float:
        result = float(value)
        require(math.isfinite(result))
        return result

    def whole(value: str) -> int:
        require(len(value) <= 20)
        result = int(value)
        require(-(2**63) < result < 2**63)
        return result

    def invalid(_: str) -> object:
        raise CustodyHeld("HOSTED_CUSTODY_UNKNOWN_NO_RETRY")

    return json.loads(
        raw, object_pairs_hook=pairs, parse_float=number, parse_int=whole, parse_constant=invalid
    )


@dataclass(frozen=True, repr=False)
class CustodyRegistration:
    """Trusted source configuration, NOT accepted from workflow JSON or HTTP."""

    receipt_policy: backend.ReceiptPolicy
    purpose: str
    repository: str
    run_id: int
    run_attempt: int
    caller_head_sha: str
    caller_workflow: str
    sdk_checkout_sha: str
    callee_sha: str
    sdk_parent_ref: str
    operation_id: str
    artifact_name: str
    directory: Path
    original_end: float
    actions_read_token: Callable[[], str] = field(repr=False)
    consume_wire: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        require(type(self.receipt_policy) is backend.ReceiptPolicy)
        require(self.purpose in {"deploy", "destroy"} and self.repository in REPOSITORIES)
        require(integer(self.run_id) and integer(self.run_attempt))
        require(
            all(
                digest(s, 40)
                for s in (self.caller_head_sha, self.sdk_checkout_sha, self.callee_sha)
            )
        )
        require(self.sdk_parent_ref in backend.SDK_PARENTS)
        require(
            self.caller_workflow == ".github/workflows/owned-private-podman-images.yml"
            and self.repository == "Borduas-Holdings/blazing"
        )  # First purpose is explicitly manual and source-pinned; BB is not migrated.
        require(self.operation_id == f"owned-private-images-{self.run_id}-{self.run_attempt}")
        require(self.artifact_name == f"signed-consume-{self.run_id}-{self.run_attempt}")
        require(type(self.directory) is type(Path()) and self.directory.is_absolute())
        require(type(self.original_end) in (int, float) and math.isfinite(self.original_end))
        require(callable(self.actions_read_token))
        require(self.consume_wire is None or type(self.consume_wire) is bytes)


class Completion(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def exit_status(self) -> int | None: ...

    @property
    def locator_unknown(self) -> bool: ...


def _reviewed_hosted_custody() -> CustodyRegistration | None:
    # Root still owes legitimate registration/key/source/hosted bootstrap wiring.
    # No ambient flag, artifact, policy JSON or constructor activates this source.
    return None


def _configuration() -> CustodyRegistration:
    value = _reviewed_hosted_custody()
    require(type(value) is CustodyRegistration)
    return cast(CustodyRegistration, value)


def _check(reg: CustodyRegistration, end: float) -> None:
    require(_reviewed_hosted_custody() is reg and time.monotonic() < end <= reg.original_end)


def _end(reg: CustodyRegistration, end: float | None) -> float:
    require(end is None or type(end) in (int, float) and math.isfinite(end))
    finish = min(reg.original_end, time.monotonic() + 30, end if end is not None else math.inf)
    _check(reg, finish)
    return finish


def context(reg: CustodyRegistration) -> dict[str, object]:
    return dict(
        repository=reg.repository,
        repository_id=REPOSITORIES[reg.repository],
        owner_id=208689568,
        run_id=reg.run_id,
        run_attempt=reg.run_attempt,
        caller_head_sha=reg.caller_head_sha,
        caller_workflow=reg.caller_workflow,
        sdk_checkout_sha=reg.sdk_checkout_sha,
        callee_sha=reg.callee_sha,
        sdk_parent_ref=reg.sdk_parent_ref,
        api_sha256=backend.API_SHA256,
        hook_sha256=backend.HOOK_SHA256,
        operation_id=reg.operation_id,
    )


def _directory(reg: CustodyRegistration, *, create: bool = False) -> int:
    path = reg.directory
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    # Every component, not just the final directory, is opened without following links.
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            require(part not in {".", ".."})
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        metadata = os.fstat(fd)
        require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid())
        require(metadata.st_mode & 0o077 == 0)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(fd: int, name: str) -> bytes:
    source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        before = os.fstat(source)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid())
        require(before.st_nlink == 1 and 0 < before.st_size <= MAX_BYTES)
        with os.fdopen(os.dup(source), "rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        after = os.fstat(source)
        keys = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        require(
            len(raw) == before.st_size
            and all(getattr(before, k) == getattr(after, k) for k in keys)
        )
        return raw
    finally:
        os.close(source)


def _write(fd: int, name: str, raw: bytes, *, first: bool) -> None:
    require(0 < len(raw) <= MAX_BYTES)
    temporary = name if first else name + ".pending"
    target = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
    )
    try:
        with os.fdopen(os.dup(target), "wb") as stream:
            require(stream.write(raw) == len(raw))
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(target)
    if not first:
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
    os.fsync(fd)


def prepare() -> None:
    """Begin exactly one invocation before dispatch; no grant or zero-debt claim."""
    reg = _configuration()
    end = _end(reg, None)
    require(reg.purpose == "deploy")
    fd = _directory(reg, create=True)
    try:
        require(not os.listdir(fd))
        body = dict(schema=SCHEMA, context=context(reg), state="begun", completion=None, wires=[])
        _write(fd, BUNDLE, backend.canonical(body), first=True)
    finally:
        os.close(fd)
    _check(reg, end)  # Includes held-file and directory retirement.


def _locator(wire: bytes, reg: CustodyRegistration) -> dict[str, object]:
    claims = object_map(cleanup.historical_consume(wire, reg.receipt_policy))
    identity = object_map(claims["identity"])
    require(claims["operation_id"] == reg.operation_id)
    require(
        identity["source_revision"] == reg.caller_head_sha
        and identity["sdk_parent_ref"] == reg.sdk_parent_ref
        and identity["api_sha256"] == backend.API_SHA256
        and identity["hook_sha256"] == backend.HOOK_SHA256
    )
    return claims  # Historical locator only; original expiry never renews send authority.


def _bundle(raw: bytes, reg: CustodyRegistration) -> tuple[dict[str, object], tuple[bytes, ...]]:
    data = object_map(strict(raw))
    require(set(data) == {"schema", "context", "state", "completion", "wires"})
    require(backend.canonical(data) == raw and data["schema"] == SCHEMA)
    require(data["context"] == context(reg) and data["state"] in {"begun", "finished"})
    require(type(data["wires"]) is list and len(cast(list, data["wires"])) <= MAX_WIRES)
    if data["state"] == "begun":
        require(data["completion"] is None and data["wires"] == [])
    wires: list[bytes] = []
    seen: set[str] = set()
    for item in cast("list[object]", data["wires"]):
        entry = object_map(item)
        require(set(entry) == {"sha256", "wire"} and type(entry["wire"]) is str)
        wire = base64.b64decode(cast(str, entry["wire"]), validate=True)
        require(base64.b64encode(wire).decode() == entry["wire"])
        value = hashlib.sha256(wire).hexdigest()
        require(value == entry["sha256"] and value not in seen)
        _locator(wire, reg)
        seen.add(value)
        wires.append(wire)
    return data, tuple(wires)


def capture(result: HostedCallResult | None, completion: Completion, original_end: float) -> None:
    """Bootstrap callback: AFTER inner backend retirement, never a pre-wire ACK."""
    reg = _configuration()
    end = _end(reg, original_end)
    kind, status, unknown = completion.kind, completion.exit_status, completion.locator_unknown
    require(kind in {"return", "system-exit", "cancel", "exception"})
    require(status is None or type(status) is int and 0 <= status <= 255)
    require(type(unknown) is bool and (result is None or type(result) is HostedCallResult))
    if result is not None:
        require(result.command == reg.purpose and type(result.locator_custody_unknown) is bool)
        unknown = unknown or result.locator_custody_unknown or not result.entered
    fd = _directory(reg)
    try:
        if reg.purpose == "deploy":
            require(os.listdir(fd) == [BUNDLE])
            old, _ = _bundle(_read(fd, BUNDLE), reg)
            require(old["state"] == "begun")
            values = result.consume_locators if result is not None else None
            require(values is None or type(values) is tuple and len(values) <= MAX_WIRES)
            wires = []
            seen: set[str] = set()
            for wire in values or ():
                require(type(wire) is bytes)
                _locator(wire, reg)
                value = hashlib.sha256(wire).hexdigest()
                require(value not in seen)
                seen.add(value)
                wires.append(dict(sha256=value, wire=base64.b64encode(wire).decode()))
            body = dict(
                schema=SCHEMA,
                context=context(reg),
                state="finished",
                wires=wires,
                completion=dict(
                    kind=kind, exit_status=status, locator_unknown=unknown or values is None
                ),
            )
            _write(fd, BUNDLE, backend.canonical(body), first=False)
        else:
            require(
                result is not None
                and type(result.cleanup_observation) is cleanup.ClosedCapacityObservation
            )
            result = cast(HostedCallResult, result)
            require(type(reg.consume_wire) is bytes and not unknown)
            observation = cast(cleanup.ClosedCapacityObservation, result.cleanup_observation)
            require(result.entered and observation.execution_closed is True)
            require(observation.settlement_proven is False and observation.lease_permit is False)
            bundle, wires = _bundle(_read(fd, BUNDLE), reg)
            require(bundle["state"] == "finished" and wires == (reg.consume_wire,))
            consume = _locator(cast(bytes, reg.consume_wire), reg)
            cleanup.decode_closed(
                observation.wire,
                reg.receipt_policy,
                reg.consume_wire,
                consume,
                observation.claims["close_tx_hash"],
                now=time.time(),
            )
            require(set(os.listdir(fd)) == {BUNDLE})
            body = dict(
                context=context(reg),
                consume=base64.b64encode(cast(bytes, reg.consume_wire)).decode(),
                closed=base64.b64encode(observation.wire).decode(),
            )
            _write(fd, CLOSED, backend.canonical(body), first=True)
    finally:
        os.close(fd)
    _check(reg, end)


@dataclass
class _Budget:
    end: float
    requests: int = 0
    body: int = 0
    headers: int = 0


def _metadata(
    url: str, token: str, reg: CustodyRegistration, budget: _Budget
) -> dict[str, object]:
    _check(reg, budget.end)
    require(budget.requests < 2)
    budget.requests += 1  # Includes failed transport entries; no retry or refill.
    with httpx.Client(
        verify=True,
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(min(5, budget.end - time.monotonic())),
    ) as session:
        _check(reg, budget.end)
        with session.stream(
            "GET",
            url,
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "Accept-Encoding": "identity",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        ) as response:
            require(response.status_code == 200 and str(response.url) == url)
            fields: dict[bytes, bytes] = {}
            for key, value in response.headers.raw:
                budget.headers += len(key) + len(value) + 4
                require(budget.headers <= MAX_HEADERS and re.fullmatch(rb"[A-Za-z0-9-]+", key))
                key = key.lower()
                require(key not in fields and key != b"link")
                require(all(c >= 32 and c != 127 for c in value))
                fields[key] = value
            require(
                fields.get(b"content-type", b"").split(b";", 1)[0].lower()
                in {b"application/json", b"application/vnd.github+json"}
            )
            require(fields.get(b"content-encoding", b"identity").lower() == b"identity")
            require(fields.get(b"age", b"0") == b"0")
            length = fields.get(b"content-length")
            require(length is None or re.fullmatch(rb"[0-9]{1,10}", length) is not None)
            require(not (length is not None and b"transfer-encoding" in fields))
            require(fields.get(b"transfer-encoding", b"chunked").lower() == b"chunked")
            require(length is None or 0 < int(length) <= MAX_BYTES - budget.body)
            raw = bytearray()
            for chunk in response.iter_raw(chunk_size=4096):
                _check(reg, budget.end)
                budget.body += len(chunk)
                require(budget.body <= MAX_BYTES)
                raw.extend(chunk)
            require(length is None or len(raw) == int(length))
        _check(reg, budget.end)
    _check(reg, budget.end)  # Includes both response and client retirement.
    return object_map(strict(bytes(raw)))


def read_consumes(
    artifact_id: int, uploaded_digest: str, *, end: float | None = None
) -> tuple[bytes, ...]:
    """Internally acquire native metadata; signed historical locators are DATA only."""
    try:
        reg = _configuration()
        finish = _end(reg, end)
        require(reg.purpose == "destroy" and integer(artifact_id))
        require(type(uploaded_digest) is str and uploaded_digest.startswith("sha256:"))
        require(digest(uploaded_digest.removeprefix("sha256:")))
        token = reg.actions_read_token()
        require(
            type(token) is str and 0 < len(token) <= 8192 and all(32 < ord(c) < 127 for c in token)
        )
        budget = _Budget(finish)
        base = "https://api.github.com/repos/" + reg.repository
        artifact = _metadata(base + f"/actions/artifacts/{artifact_id}", token, reg, budget)
        workflow = object_map(artifact["workflow_run"])
        require(type(artifact["id"]) is int and artifact["id"] == artifact_id)
        require(artifact["name"] == reg.artifact_name and artifact["digest"] == uploaded_digest)
        require(artifact["expired"] is False and integer(artifact["size_in_bytes"]))
        require(cast(int, artifact["size_in_bytes"]) <= MAX_BYTES)
        for key, expected in (
            ("id", reg.run_id),
            ("repository_id", REPOSITORIES[reg.repository]),
            ("head_repository_id", REPOSITORIES[reg.repository]),
        ):
            require(type(workflow[key]) is int and workflow[key] == expected)
        require(workflow["head_sha"] == reg.caller_head_sha)
        attempt = _metadata(
            base + f"/actions/runs/{reg.run_id}/attempts/{reg.run_attempt}", token, reg, budget
        )
        repository = object_map(attempt["repository"])
        owner = object_map(repository["owner"])
        require(type(attempt["id"]) is int and attempt["id"] == reg.run_id)
        require(type(attempt["run_attempt"]) is int and attempt["run_attempt"] == reg.run_attempt)
        require(
            attempt["head_sha"] == reg.caller_head_sha and attempt["event"] == "workflow_dispatch"
        )
        require(
            attempt["path"] == reg.caller_workflow
            and attempt["status"] in {"in_progress", "completed"}
        )
        require(repository["full_name"] == reg.repository and repository["private"] is True)
        require(type(repository["id"]) is int and repository["id"] == REPOSITORIES[reg.repository])
        require(type(owner["id"]) is int and owner["id"] == 208689568)
        require(owner["login"] == "Borduas-Holdings" and owner["type"] == "Organization")
        fd = _directory(reg)
        try:
            require(os.listdir(fd) == [BUNDLE])  # Dedicated exact extracted-file population.
            raw = _read(fd, BUNDLE)
        finally:
            os.close(fd)
        budget.body += len(raw)
        require(budget.body <= MAX_BYTES)
        data, wires = _bundle(raw, reg)
        require(data["state"] == "finished" and len(wires) == 1)
        completion = object_map(data["completion"])
        require(set(completion) == {"kind", "exit_status", "locator_unknown"})
        require(completion["kind"] in {"return", "system-exit", "cancel", "exception"})
        require(
            completion["exit_status"] is None
            or type(completion["exit_status"]) is int
            and 0 <= cast(int, completion["exit_status"]) <= 255
        )
        require(completion["locator_unknown"] is False)
        _check(reg, finish)
        return wires
    except Exception:
        raise CustodyHeld("HOSTED_CUSTODY_UNKNOWN_NO_RETRY") from None


def assert_closed() -> None:
    reg = _configuration()
    end = _end(reg, None)
    require(reg.purpose == "destroy" and type(reg.consume_wire) is bytes)
    fd = _directory(reg)
    try:
        require(set(os.listdir(fd)) == {BUNDLE, CLOSED})
        raw = _read(fd, CLOSED)
    finally:
        os.close(fd)
    data = object_map(strict(raw))
    require(set(data) == {"context", "consume", "closed"} and backend.canonical(data) == raw)
    require(data["context"] == context(reg))
    wire = base64.b64decode(cast(str, data["consume"]), validate=True)
    require(wire == reg.consume_wire and base64.b64encode(wire).decode() == data["consume"])
    closed = base64.b64decode(cast(str, data["closed"]), validate=True)
    require(base64.b64encode(closed).decode() == data["closed"])
    envelope = object_map(strict(closed))
    require(type(envelope["payload"]) is str)
    payload = object_map(strict(base64.b64decode(cast(str, envelope["payload"]), validate=True)))
    consume = _locator(wire, reg)
    cleanup.decode_closed(
        closed, reg.receipt_policy, wire, consume, payload["close_tx_hash"], now=time.time()
    )
    _check(reg, end)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "assert-closed"))
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare()
        else:
            assert_closed()
            print("shared_closed=true")
        return 0
    except Exception:
        print("shared_custody=UNKNOWN; no retry or release")
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
