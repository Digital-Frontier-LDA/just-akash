"""Host-only typed receipt pipe; no filesystem/stdout/output-file ACK fallback."""

from __future__ import annotations

import json
import os
import select
import stat
import time
from typing import Any, cast

from .keydb_qualification_fence import runtime_reservation, validate_ack, validate_runtime_ack
from .keydb_qualification_provenance import (
    DurableAck,
    KeyDBQualificationProvenanceHeld,
    canonical_bytes,
)


class PipeFencedRecorder:
    """One acknowledged phase invocation; failure poisons this recorder permanently."""

    def __init__(self) -> None:
        accepted = False
        try:
            accepted = (
                os.environ.get("KEYDB_QUALIFICATION_RECEIPT_REQUEST_FD") == "3"
                and os.environ.get("KEYDB_QUALIFICATION_RECEIPT_ACK_FD") == "4"
                and all(
                    stat.S_ISFIFO(os.fstat(fd).st_mode) or stat.S_ISSOCK(os.fstat(fd).st_mode)
                    for fd in (3, 4)
                )
            )
            if accepted:
                os.set_blocking(3, False)
                os.set_blocking(4, False)
        except Exception:
            accepted = False
        if not accepted:
            raise KeyDBQualificationProvenanceHeld(
                "Scanner-v2 durable receipt pipe is unavailable; no wire permitted"
            )
        self._held = False

    def _exchange(self, record: dict[str, object], validator) -> Any:
        if self._held:
            raise KeyDBQualificationProvenanceHeld(
                "Scanner-v2 receipt is held; no retry permitted"
            )
        self._held = True
        accepted: Any = None
        try:
            raw = canonical_bytes(record)
            if not 0 < len(raw) <= 65536:
                raise ValueError("bounded public receipt required")
            # This additional local bound never replaces original effect clocks.
            deadline = time.monotonic() + 20
            frame = raw + b"\n"
            offset = 0
            while offset < len(frame):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                _, ready, _ = select.select([], [3], [], remaining)
                if ready:
                    try:
                        count = os.write(3, frame[offset:])
                    except BlockingIOError:
                        continue
                    if count <= 0:
                        raise EOFError
                    offset += count
            result = bytearray()
            while not result.endswith(b"\n"):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                ready, _, _ = select.select([4], [], [], remaining)
                if ready:
                    try:
                        chunk = os.read(4, 4097 - len(result))
                    except BlockingIOError:
                        continue
                    if not chunk or len(result) + len(chunk) > 4096:
                        raise EOFError
                    result.extend(chunk)
                    if b"\n" in result and not result.endswith(b"\n"):
                        raise ValueError("single typed ACK required")
            parsed = json.loads(result)
            if canonical_bytes(parsed) + b"\n" != bytes(result):
                raise ValueError("canonical typed ACK required")
            accepted = validator(record, parsed)
        except Exception:
            accepted = None
        # No exception chain, server/body diagnostics or credentials escape this boundary.
        if accepted is None:
            raise KeyDBQualificationProvenanceHeld(
                "Scanner-v2 durable artifact was not acknowledged; no wire permitted"
            )
        self._held = False
        return accepted

    def persist(self, record: dict[str, object]) -> DurableAck:
        return cast(DurableAck, self._exchange(record, validate_ack))

    def reserve_runtime(self, request: dict[str, object]) -> dict[str, Any]:
        request = runtime_reservation(request)
        return cast(dict[str, Any], self._exchange(request, validate_runtime_ack))
