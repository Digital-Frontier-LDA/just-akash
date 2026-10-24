"""Bounded, read-only prerequisites for a newly recorded scanner create intent.

These observations never rewrite UNKNOWN or authorize a create/close/runner removal.
Old records without a pre-create baseline and official version cannot be upgraded.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, cast

from .address import is_canonical_akash_address
from .api import CIConsoleAPI, _NoConsoleRedirect
from .owner_lookup import Deadline, bounded_call
from .scanner_provenance import canonical_bytes, require

MAX_ENTRIES = 1000
MAX_PAGE = 100
MAX_PAGES = 10
LIBRARY = "pkg.akt.dev/go/sdl@v0.4.5-rc0"


def manifest_version(value: object) -> str:
    raw = None
    try:
        if type(value) is str:
            raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raw = None
    require(raw is not None and len(raw) == 32 and base64.b64encode(raw).decode() == value)
    return cast(str, value)


def dseq(value: object) -> str:
    require(type(value) is str and re.fullmatch(r"[1-9][0-9]{0,19}", cast(str, value)) is not None)
    require(int(cast(str, value)) <= 2**64 - 1)
    return cast(str, value)


def validate_baseline(value: object, owner: str) -> dict[str, Any]:
    require(type(value) is dict and is_canonical_akash_address(owner))
    row = cast(dict[str, Any], value)
    require(set(row) == {"schema", "owner", "states", "dseqs"})
    require(row["schema"] == "just-akash/scanner-precreate-baseline/v1" and row["owner"] == owner)
    require(row["states"] == ["active", "closed"] and type(row["dseqs"]) is list)
    ids = [dseq(v) for v in row["dseqs"]]
    require(len(ids) <= MAX_ENTRIES and ids == sorted(set(ids), key=int))
    require(len(canonical_bytes(row)) <= 32768)
    return cast(dict[str, Any], json.loads(canonical_bytes(row)))


class _LimitedResponse:
    def __init__(self, response: Any):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self._response.__exit__(*args)

    def read(self):
        raw = None
        try:
            raw = self._response.read(1048577)
        except Exception:
            raw = None
        require(type(raw) is bytes and len(cast(bytes, raw)) <= 1048576)
        return cast(bytes, raw)


class ConsoleReadOnly(CIConsoleAPI):
    """Existing fixed-origin CI client, GET-only and one bounded observation deadline."""

    def __init__(self, selected_client: CIConsoleAPI, deadline: float):
        super().__init__(selected_client.api_key)
        self._deadline = deadline

    def _request(self, method: str, endpoint: str, data: dict[str, Any] | None = None) -> Any:
        require(method == "GET" and data is None and endpoint.startswith("/v1/deployments?"))
        return super()._request(method, endpoint, data)

    def _open_request(self, request: urllib.request.Request) -> Any:
        remaining = self._deadline - time.monotonic()
        require(request.get_method() == "GET" and remaining > 0)
        response = None
        try:
            response = urllib.request.build_opener(_NoConsoleRedirect()).open(
                request, timeout=min(5.0, remaining)
            )
        except Exception:
            response = None
        require(response is not None)
        return _LimitedResponse(response)


def complete_snapshot(selected_client: CIConsoleAPI, owner: str) -> dict[str, Any]:
    """Exhaust active AND closed collections twice; no dropped rows or read retries."""
    require(is_canonical_akash_address(owner))
    deadline = time.monotonic() + 30
    budget = Deadline(30, ceiling_at=time.time() + 30)
    client = ConsoleReadOnly(selected_client, deadline)
    completed = []
    for _pass in range(2):
        seen = {}
        for state in ("active", "closed"):
            skip = 0
            total: Any = None
            for _page in range(MAX_PAGES):
                require(time.monotonic() < deadline)
                doc = None
                try:
                    require(not budget.expired)
                    route = f"/v1/deployments?state={state}&skip={skip}&limit={MAX_PAGE}"
                    finished, value = bounded_call(
                        lambda route=route: client._request("GET", route), budget
                    )
                    if finished:
                        doc = value
                except Exception:
                    doc = None
                require(type(doc) is dict and time.monotonic() < deadline)
                data = cast(dict, doc).get("data", doc)
                require(type(data) is dict)
                rows = data.get("deployments")
                pagination = data.get("pagination")
                require(type(rows) is list and type(pagination) is dict)
                rows = cast(list[Any], rows)
                pagination = cast(dict[str, Any], pagination)
                require(set(pagination) >= {"total", "skip", "limit", "hasMore"})
                require(
                    type(pagination["total"]) is int and 0 <= pagination["total"] <= MAX_ENTRIES
                )
                require(type(pagination["skip"]) is int and pagination["skip"] == skip)
                require(type(pagination["limit"]) is int and pagination["limit"] == MAX_PAGE)
                require(type(pagination["hasMore"]) is bool and len(rows) <= MAX_PAGE)
                if total is None:
                    total = pagination["total"]
                require(pagination["total"] == total and skip + len(rows) <= total)
                for row in rows:
                    require(type(row) is dict and type(row.get("deployment")) is dict)
                    deployment = cast(dict[str, Any], row["deployment"])
                    identity = deployment.get("id")
                    require(type(identity) is dict and identity.get("owner") == owner)
                    identity_dseq = dseq(cast(dict[str, Any], identity).get("dseq"))
                    require(identity_dseq not in seen and deployment.get("state") == state)
                    seen[identity_dseq] = {
                        "owner": owner,
                        "dseq": identity_dseq,
                        "state": state,
                        "version": manifest_version(deployment.get("hash")),
                    }
                    require(len(seen) <= MAX_ENTRIES)
                skip += len(rows)
                if pagination["hasMore"] is False:
                    require(skip == total)
                    break
                require(len(rows) == MAX_PAGE and skip < total)
            else:
                require(False)
        completed.append(seen)
    require(completed[0] == completed[1])
    result = {
        "schema": "just-akash/scanner-precreate-baseline/v1",
        "owner": owner,
        "states": ["active", "closed"],
        "dseqs": sorted(completed[0], key=int),
    }
    return {"baseline": validate_baseline(result, owner), "rows": list(completed[0].values())}


def unique_new_candidate(
    baseline: dict[str, Any], observed: dict[str, Any], owner: str, expected_version: str
) -> str | None:
    """Selection DATA only: caller must add native identity and signed-chain proof."""
    before = validate_baseline(baseline, owner)
    validate_baseline(observed["baseline"], owner)
    version = manifest_version(expected_version)
    rows = observed["rows"]
    require(type(rows) is list and len(rows) == len(observed["baseline"]["dseqs"]))
    rows = cast(list[dict[str, Any]], rows)
    ids = []
    matches = []
    for row in rows:
        require(
            type(row) is dict
            and set(row) == {"owner", "dseq", "state", "version"}
            and row["owner"] == owner
        )
        item = dseq(row["dseq"])
        manifest_version(row["version"])
        require(row["state"] in ("active", "closed"))
        ids.append(item)
        if item not in before["dseqs"] and row["version"] == version:
            matches.append(item)
    require(
        sorted(set(ids), key=int) == observed["baseline"]["dseqs"] and len(ids) == len(set(ids))
    )
    return matches[0] if len(matches) == 1 else None


def official_version_held_helper(
    raw_sdl: str, helper_path: str, expected_binary_sha256: str
) -> str:
    """Hosted Linux executes only captured sealed bytes; SDL travels through stdin."""
    require(hasattr(os, "memfd_create") and Path("/proc/self/fd").is_dir())
    require(
        type(raw_sdl) is str
        and 0 < len(raw_sdl.encode()) <= 65536
        and "ac-secret://" not in raw_sdl
    )
    require(re.fullmatch(r"[0-9a-f]{64}", expected_binary_sha256) is not None)
    import fcntl

    input_fd = os.open(helper_path, os.O_RDONLY | os.O_NOFOLLOW)
    held = None
    try:
        before = os.fstat(input_fd)
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_nlink == 1
            and 0 < before.st_size <= 134217728
        )
        blob = bytearray()
        while len(blob) < before.st_size:
            part = os.read(input_fd, min(1048576, before.st_size - len(blob)))
            require(bool(part))
            blob.extend(part)
        after = os.fstat(input_fd)

        def stable(row):
            return (
                row.st_dev,
                row.st_ino,
                row.st_mode,
                row.st_uid,
                row.st_gid,
                row.st_nlink,
                row.st_size,
                row.st_mtime_ns,
                row.st_ctime_ns,
            )

        require(
            stable(before) == stable(after) == stable(os.stat(helper_path, follow_symlinks=False))
        )
        require(hashlib.sha256(blob).hexdigest() == expected_binary_sha256)
        memfd = getattr(os, "memfd_create", None)
        flags = getattr(os, "MFD_ALLOW_SEALING", None)
        require(callable(memfd) and type(flags) is int)
        held = cast(Any, memfd)("scanner-official-sdl-version", flags)
        offset = 0
        while offset < len(blob):
            written = os.write(held, blob[offset:])
            require(written > 0)
            offset += written
        os.fchmod(held, 0o500)
        names = (
            "F_SEAL_WRITE",
            "F_SEAL_GROW",
            "F_SEAL_SHRINK",
            "F_SEAL_SEAL",
            "F_ADD_SEALS",
            "F_GET_SEALS",
        )
        raw_constants = {name: getattr(fcntl, name, None) for name in names}
        require(all(type(value) is int for value in raw_constants.values()))
        constants = cast(dict[str, int], raw_constants)
        seals = (
            constants["F_SEAL_WRITE"]
            | constants["F_SEAL_GROW"]
            | constants["F_SEAL_SHRINK"]
            | constants["F_SEAL_SEAL"]
        )
        fcntl.fcntl(held, constants["F_ADD_SEALS"], seals)
        require(fcntl.fcntl(held, constants["F_GET_SEALS"]) == seals)
        output = None
        try:
            result = subprocess.run(  # noqa: S603 -- sealed fixed-SHA captured executable only
                [f"/proc/self/fd/{held}"],
                input=raw_sdl.encode(),
                capture_output=True,
                pass_fds=(held,),
                timeout=10,
                check=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
            if result.returncode == 0 and len(result.stdout) <= 1024 and not result.stderr:
                output = json.loads(result.stdout)
        except Exception:
            output = None
        require(
            type(output) is dict
            and set(output) == {"schema", "library", "manifest_version", "wire_request_sha256"}
        )
        output = cast(dict[str, Any], output)
        require(
            output["schema"] == "just-akash/official-sdl-version/v1"
            and output["library"] == LIBRARY
        )
        require(output["wire_request_sha256"] == hashlib.sha256(raw_sdl.encode()).hexdigest())
        return manifest_version(output["manifest_version"])
    finally:
        os.close(input_fd)
        if held is not None:
            os.close(held)


class PrecreateCapture:
    """Actual version and complete baseline must exist before durable NO_WIRE."""

    def __init__(
        self,
        helper_path: str,
        expected_binary_sha256: str,
        *,
        environment: Any = None,
        token: str = "",
        observation_path: str | None = None,
    ):
        self.helper_path = helper_path
        self.binary_sha256 = expected_binary_sha256
        self._used = False
        self._environment, self._token = environment, token
        self._observation_path = observation_path
        self._observed = False
        self._client: CIConsoleAPI | None = None

    def prepare(
        self, selected_client: CIConsoleAPI, owner: str, raw_sdl: str
    ) -> dict[str, object]:
        require(not self._used)
        self._used = True
        self._client = selected_client
        version = official_version_held_helper(raw_sdl, self.helper_path, self.binary_sha256)
        snapshot = complete_snapshot(selected_client, owner)
        return {
            "expected_manifest_version": version,
            "precreate_baseline": snapshot["baseline"],
            "payload_profile": "scanner-private-e71-tcp65535/v1",
        }

    def observe_current_unknown(self, provenance: Any) -> None:
        require(self._client is not None)
        if not self._observed:
            self.observe_unknown(cast(CIConsoleAPI, self._client), provenance)

    def observe_unknown(self, selected_client: CIConsoleAPI, provenance: Any) -> None:
        """One read-only attempt; no new phase, create, close or cleanup authority."""
        require(self._used and not self._observed)
        self._observed = True
        require(self._environment is not None and bool(self._token))
        row = provenance.operation.public_record
        ack = provenance.operation._ack
        require(row["phase"] == "UNKNOWN" and ack is not None)
        value = observe_creation_identity_data(
            selected_client, provenance.binding, row, ack, self._environment, self._token
        )
        if value is not None:
            require(type(self._observation_path) is str)
            path = Path(cast(str, self._observation_path))
            require(path.is_absolute())
            # Local DATA is supplementary only. The original acknowledged UNKNOWN
            # remains immutable; a later actor must independently admit its source.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(canonical_bytes(value) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())


def observe_creation_identity_data(
    selected_client: CIConsoleAPI,
    binding: Any,
    unknown_record: dict[str, object],
    unknown_ack: Any,
    environment: Any,
    token: str,
) -> dict[str, object] | None:
    """Current-producer read-only observation; terminal callback admission is separate.

    Never upgrades legacy81fa UNKNOWN lacking a durable pre-create baseline/version.
    A positive result remains separate DATA, not create replay or close permission.
    """
    from .scanner_provenance import record_sha256, validate_ack, validate_record

    row = validate_record(unknown_record)
    require(
        row["kind"] == "lease_reconcile"
        and row["phase"] == "UNKNOWN"
        and row["binding"] == binding.to_dict()
    )
    validate_ack(row, unknown_ack)
    from . import chain
    from .scanner_v2_controller import admit_native

    admit_native(binding, environment, token)
    details = cast(dict[str, Any], row["details"])
    observed = complete_snapshot(selected_client, details["owner"])
    candidate = unique_new_candidate(
        details["precreate_baseline"],
        observed,
        details["owner"],
        details["expected_manifest_version"],
    )
    if candidate is None:
        return None
    admit_native(binding, environment, token)
    proof = chain.creation_version_population_evidence(
        details["owner"],
        candidate,
        details["group_population"],
        details["expected_manifest_version"],
    )
    if proof is None:
        return None
    require(
        proof.get("evidence_version") == 3
        and proof.get("owner") == details["owner"]
        and proof.get("dseq") == candidate
    )
    require(
        proof.get("groups") == details["group_population"]
        and proof.get("chain_id") == "akashnet-2"
    )
    require(
        proof.get("creation_manifest_version")
        == proof.get("current_manifest_version")
        == details["expected_manifest_version"]
    )
    require(proof.get("registry_digest") == chain.OWNER_CORROBORATION_REGISTRY_SHA256)
    admit_native(binding, environment, token)
    return {
        "schema": "just-akash/scanner-create-positive-observation/v1",
        "authority": "DATA",
        "original_unknown_record_sha256": record_sha256(row),
        "binding": binding.to_dict(),
        "returned_runner_id": details["returned_runner_id"],
        "jit_ack_record_sha256": details["jit_ack_record_sha256"],
        "group_population": details["group_population"],
        "provider": None,
        "owner": details["owner"],
        "dseq": candidate,
        "expected_manifest_version": details["expected_manifest_version"],
        "creation_proof": proof,
        "original_UNKNOWN_unchanged": True,
        "retry_authorized": False,
        "cleanup_qualified": False,
    }
