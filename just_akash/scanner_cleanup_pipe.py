"""Fixed public FD3/4 cleanup protocol. No token, effect payload or stdout fallback."""

from __future__ import annotations

import json
import os
import select
import stat
import time
from collections.abc import Mapping
from typing import Any

from .scanner_cleanup_provenance import (
    closed,
    validate_actor,
    validate_bundle,
    validate_cleanup_ack,
    validate_cleanup_record,
    validate_locator,
    validate_target,
)
from .scanner_provenance import (
    ScannerProvenanceHeld,
    canonical_bytes,
    digest,
    positive,
    record_sha256,
    require,
    validate_binding,
)


class PipeCleanupRecorder:
    def __init__(self, actor: object):
        self.actor = validate_actor(actor)
        self._held = False
        try:
            require(
                os.environ.get("SCANNER_CLEANUP_REQUEST_FD") == "3"
                and os.environ.get("SCANNER_CLEANUP_ACK_FD") == "4"
            )
            require(
                all(
                    stat.S_ISFIFO(os.fstat(fd).st_mode) or stat.S_ISSOCK(os.fstat(fd).st_mode)
                    for fd in (3, 4)
                )
            )
            os.set_blocking(3, False)
            os.set_blocking(4, False)
        except Exception:
            raise ScannerProvenanceHeld(
                "Cleanup receipt pipe unavailable; no effect permitted"
            ) from None

    def _request(self, command: str, payload: object, maximum: int) -> Any:
        require(not self._held)
        self._held = True
        result = None
        try:
            raw = (
                canonical_bytes(
                    dict(
                        schema="just-akash/scanner-cleanup-pipe/v1",
                        command=command,
                        actor=self.actor,
                        payload=payload,
                    )
                )
                + b"\n"
            )
            require(len(raw) <= 65537)
            deadline = time.monotonic() + 20
            offset = 0
            while offset < len(raw):
                remaining = deadline - time.monotonic()
                require(remaining > 0)
                _, ready, _ = select.select([], [3], [], remaining)
                if ready:
                    try:
                        n = os.write(3, raw[offset:])
                    except BlockingIOError:
                        continue
                    require(n > 0)
                    offset += n
            data = bytearray()
            while not data.endswith(b"\n"):
                remaining = deadline - time.monotonic()
                require(remaining > 0)
                ready, _, _ = select.select([4], [], [], remaining)
                if ready:
                    try:
                        chunk = os.read(4, maximum + 1 - len(data))
                    except BlockingIOError:
                        continue
                    require(bool(chunk) and len(data) + len(chunk) <= maximum)
                    data.extend(chunk)
                    require(b"\n" not in data or data.endswith(b"\n"))
            result = json.loads(data)
            require(canonical_bytes(result) + b"\n" == bytes(data))
        except Exception:
            result = None
        if result is None:
            raise ScannerProvenanceHeld(
                "Cleanup durable evidence HOLD; no effect or retry permitted"
            )
        return result

    def read_target(self, locator: object = None) -> dict[str, Any]:
        target = validate_locator(self.actor["target_locator"] if locator is None else locator)
        require(target == self.actor["target_locator"])
        result = validate_bundle(self.actor, self._request("READ_TARGET", target, 65536))
        self._held = False
        return result

    def history(self, producer: Mapping[str, object], target: object) -> dict[str, Any]:
        b = validate_binding(dict(producer))
        t = validate_target(b, target)
        value = self._request("HISTORY", dict(producer=b, target=t), 65536)
        row = closed(
            value,
            {
                "schema",
                "actor",
                "producer",
                "target_sha256",
                "complete",
                "wire_clear",
                "handlers",
                "prior_actor_records",
                "current_actor_records",
                "missing",
                "incomplete",
            },
        )
        require(
            row["schema"] == "just-akash/scanner-cleanup-history/v1"
            and row["actor"] == self.actor
            and row["producer"] == b
            and row["target_sha256"] == record_sha256(t)
        )
        require(row["complete"] is True and row["incomplete"] is False and row["missing"] == [])
        wire = closed(row["wire_clear"], {"close", "remove"})
        require(all(type(v) is bool for v in wire.values()))
        require(
            all(
                type(row[k]) is list
                for k in ("handlers", "prior_actor_records", "current_actor_records")
            )
        )
        for kind in ("prior_actor_records", "current_actor_records"):
            for item in row[kind]:
                evidence = closed(
                    item, {"record", "artifact_id", "artifact_digest", "body_sha256"}
                )
                positive(evidence["artifact_id"])
                require(
                    type(evidence["artifact_digest"]) is str
                    and evidence["artifact_digest"][:7] == "sha256:"
                )
                digest(evidence["artifact_digest"][7:])
                record = validate_cleanup_record(evidence["record"])
                require(
                    record["producer"] == b
                    and record["target"] == t
                    and evidence["body_sha256"] == record_sha256(record)
                )
        self._held = False
        return row

    def persist(self, record: dict[str, Any]) -> dict[str, Any]:
        row = validate_cleanup_record(record)
        require(row["actor"] == self.actor)
        result = validate_cleanup_ack(row, self._request("PERSIST", row, 4096))
        self._held = False
        return result
