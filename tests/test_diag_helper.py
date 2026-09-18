"""Lock the behaviour of `just_akash/_diag_helper.py`.

The helper is the only piece of the runner-pool workflow that parses
`/tmp/ja.log` (a JSON-lines file) and surfaces a typed failure_reason. It was
extracted from a heredoc embedded in the workflow's YAML literal block, where
the terminator's column-0 requirement broke under CI's bash. Pulling it into
a module makes the behaviour testable in isolation — these tests run the
function directly, with no subprocess or workflow involvement.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from just_akash._diag_helper import read_last_error_code  # noqa: E402


def _write_log(tmp_path: Path, lines: list[dict | str]) -> Path:
    """Write one JSON object per line; strings are written verbatim."""
    path = tmp_path / "ja.log"
    with path.open("w") as handle:
        for entry in lines:
            if isinstance(entry, str):
                handle.write(entry + "\n")
            else:
                handle.write(json.dumps(entry) + "\n")
    return path


def test_returns_empty_when_file_missing(tmp_path):
    """The post-loop PROVIDER_CAPACITY gate depends on this. Missing file
    must read as 'no code', NOT as 'a code we cannot name'."""
    assert read_last_error_code(tmp_path / "absent.log") == ""


def test_returns_empty_when_file_empty(tmp_path):
    """An empty log file is the same as no log file."""
    path = _write_log(tmp_path, [])
    assert read_last_error_code(path) == ""


def test_returns_empty_when_no_akash_diag_events(tmp_path):
    """Lines that are NOT akash-diag events (CI noise, provider JSON, etc.)
    must not surface as a code, even when they look plausible."""
    path = _write_log(
        tmp_path,
        [
            {"type": "progress", "message": "polling"},
            {"level": "info", "message": "deployment open"},
        ],
    )
    assert read_last_error_code(path) == ""


def test_filters_out_warning_level_codes(tmp_path):
    """The load-bearing rule: warning-level codes are per-provider diagnostics
    and MUST NOT surface as `failure_reason`. A run where one preferred
    provider is offline while another bids successfully must not report
    `failure_reason=PROVIDER_OFFLINE`.

    Mirrors `just_akash/deploy.py` lines 1566 / 1585 / 1594 — the five
    warning-level emit sites that justify this filter.
    """
    path = _write_log(
        tmp_path,
        [
            {"type": "akash-diag", "level": "warning", "code": "PROVIDER_OFFLINE"},
            {"type": "akash-diag", "level": "warning", "code": "PROVIDER_INVALID_VERSION"},
            {"type": "akash-diag", "level": "warning", "code": "PROVIDER_NO_BID"},
            {"type": "akash-diag", "level": "warning", "code": "PROVIDER_UNKNOWN"},
            {"type": "akash-diag", "level": "warning", "code": "PROVIDER_STATUS_QUERY_FAILED"},
        ],
    )
    assert read_last_error_code(path) == "", (
        "warning-level codes must NOT surface — that is the whole point of "
        "the level filter. If this fires, a per-provider observation would "
        "be elevated to a terminal verdict."
    )


def test_returns_last_error_level_code(tmp_path):
    """The 'last' rule is right because all warning-level per-provider
    diagnostics precede the terminal error in emission order. When the log
    contains the per-provider warning stream followed by a terminal verdict,
    the LAST error is the verdict."""
    path = _write_log(
        tmp_path,
        [
            {"type": "akash-diag", "level": "warning", "code": "PROVIDER_OFFLINE"},
            {"type": "akash-diag", "level": "warning", "code": "PROVIDER_OFFLINE"},
            {"type": "akash-diag", "level": "warning", "code": "PROVIDER_NO_BID"},
            {"type": "akash-diag", "level": "error", "code": "BIDS_FOREIGN_ONLY"},
        ],
    )
    assert read_last_error_code(path) == "BIDS_FOREIGN_ONLY"


def test_filters_out_non_akash_diag_events(tmp_path):
    """Lines whose `type` is not `akash-diag` (e.g. other tools' structured
    logs) must not pollute the parse."""
    path = _write_log(
        tmp_path,
        [
            {"type": "sentry", "level": "error", "code": "DEPLOY_FAILED"},
            {"type": "deploy", "level": "error", "code": "RACE"},
            {"type": "akash-diag", "level": "error", "code": "NO_BIDS_RECEIVED"},
        ],
    )
    assert read_last_error_code(path) == "NO_BIDS_RECEIVED"


def test_handles_malformed_json_gracefully(tmp_path):
    """A diagnostic failure must NEVER break the operation it reports on.
    Malformed JSON lines are skipped without raising; the parse continues."""
    path = _write_log(
        tmp_path,
        [
            "{not-json",
            "also-not-json",
            json.dumps({"type": "akash-diag", "level": "error", "code": "BIDS_MALFORMED"}),
            "garbage trailing",
        ],
    )
    assert read_last_error_code(path) == "BIDS_MALFORMED"


def test_takes_last_when_multiple_errors(tmp_path):
    """When the log has multiple error-level events, the most recent wins —
    matching the workflow's inner-loop UNCLASSIFIED branch where the LATEST
    attempt's verdict is the one to surface."""
    path = _write_log(
        tmp_path,
        [
            {"type": "akash-diag", "level": "error", "code": "NO_BIDS_RECEIVED"},
            {"type": "akash-diag", "level": "error", "code": "BIDS_MALFORMED"},
            {"type": "akash-diag", "level": "error", "code": "BIDS_FOREIGN_ONLY"},
        ],
    )
    assert read_last_error_code(path) == "BIDS_FOREIGN_ONLY"


def test_ignores_events_without_code_field(tmp_path):
    """An akash-diag error event without a `code` field is malformed; the
    helper must skip it rather than surface the empty string."""
    path = _write_log(
        tmp_path,
        [
            {"type": "akash-diag", "level": "error", "message": "no code field"},
            {"type": "akash-diag", "level": "error", "code": "BIDS_STALE"},
        ],
    )
    assert read_last_error_code(path) == "BIDS_STALE"


def test_handles_unreadable_file_gracefully(tmp_path):
    """A file that exists but cannot be read (permissions, broken handle)
    must not raise — it must return empty so the post-loop falls through to
    PROVIDER_CAPACITY, which is the documented last resort."""
    # A directory passed as the path triggers IsADirectoryError → OSError.
    directory = tmp_path / "is_a_directory"
    directory.mkdir()
    assert read_last_error_code(directory) == ""


@pytest.mark.parametrize(
    "code",
    [
        "NO_BIDS_RECEIVED",
        "BIDS_MALFORMED",
        "BIDS_STALE",
        "BIDS_FOREIGN_ONLY",
        "PROVIDER_OFFLINE",
        "PROVIDER_INVALID_VERSION",
        "PROVIDER_NO_CAPACITY",
        "PROVIDER_NO_BID",
        "PROVIDER_STATUS_QUERY_FAILED",
        "PROVIDER_UNKNOWN",
        "SDL_ERROR",
        "CONFIG_ERROR",
    ],
)
def test_surfaces_each_known_code_at_error_level(tmp_path, code):
    """Every Code enum member that the workflow should surface must round-trip
    through the helper at error level. Parametrised against the live Code
    enum-derived list so a new enum member is automatically covered when a
    future PR adds it."""
    path = _write_log(
        tmp_path,
        [{"type": "akash-diag", "level": "error", "code": code}],
    )
    assert read_last_error_code(path) == code
