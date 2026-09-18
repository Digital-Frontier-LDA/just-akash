"""Read the LAST akash-diag error-level code from a JSON-lines log file.

Used by `.github/workflows/runner-pool.yml` to surface structured failure
reasons to consumers without grepping English text. Every new Code enum member
becomes a typed `failure_reason` automatically — without enumerating codes
here, because enumerating is the bug.

The ``last error`` rule is load-bearing: per-provider codes
(PROVIDER_OFFLINE, PROVIDER_INVALID_VERSION, PROVIDER_NO_BID,
PROVIDER_UNKNOWN, PROVIDER_STATUS_QUERY_FAILED) are emitted at
``level == "warning"`` and must NOT surface as ``failure_reason`` — they are
per-provider diagnostics, not terminal verdicts. Terminal codes
(NO_BIDS_RECEIVED, BIDS_MALFORMED, BIDS_STALE, BIDS_FOREIGN_ONLY) are emitted
at ``level == "error"`` and ARE the verdict. Filtering to errors keeps the
verdict and discards the diagnostics; "last" is then the correct verdict
because the terminal error follows the warnings in emission order.

See ``akash-gate-misreports-its-cause.md`` for the two-layer defect this
helper exists to fix at the machine-readable layer.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def read_last_error_code(log_path: str | Path) -> str:
    """Return the LAST ``{"type":"akash-diag","level":"error","code":"X"}`` event's ``code``.

    Returns an empty string if the file is missing, empty, unreadable, or
    contains no error-level akash-diag events. Malformed JSON lines are
    skipped without raising — diagnostics must never break the operation
    they report on.
    """
    last = ""
    try:
        with open(log_path) as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if (
                    isinstance(event, dict)
                    and event.get("type") == "akash-diag"
                    and event.get("level") == "error"
                ):
                    code = event.get("code") or ""
                    if code:
                        last = code
    except (FileNotFoundError, IndexError, OSError):
        pass
    return last


def _main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.stdout.write(read_last_error_code(path))


if __name__ == "__main__":
    _main()
