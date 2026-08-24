"""Controls for #191: the fallback round must report ITS OWN window, and a
status line must never be left open under other stdout output.

⚠ Both are OUTPUT defects, which is exactly why they survived: nothing asserted
on what the poller printed, so a progress bar reading 100% against an expired
deadline while polling continued was invisible to the suite.
"""

from __future__ import annotations

import contextlib
import re
from unittest.mock import MagicMock, patch  # noqa: F401

import just_akash.deploy  # noqa: F401  (patch targets resolve at decoration time)

SDL_YAML = """
version: "2.0"
services:
  web:
    image: python:3.13-slim
"""

# The status line renders "<elapsed>/<total>s". The fallback round's total is
# bid_wait_retry; the collection round's is bid_wait. They must differ.
_TOTALS = re.compile(r"(\d+)/(\d+)s")


def _drive_deploy(tmp_path, monkeypatch, mock_time, MockAPI, *, bids, bid_wait, bid_wait_retry):
    """Run deploy() to completion-or-failure with a mocked clock and client."""
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    # A preferred allowlist that NOBODY bids from forces AuctionStatus.COLLECTING,
    # which is the only path that opens the fallback round.
    monkeypatch.setenv("AKASH_PROVIDERS", "akash1neverbids")
    sdl_file = tmp_path / "sdl.yaml"
    sdl_file.write_text(SDL_YAML)

    client = MockAPI.return_value
    client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
    client.get_bids.return_value = bids
    client.create_lease.side_effect = RuntimeError("lease failed")

    counter = [0.0]

    def advance():
        counter[0] += 1
        return counter[0]

    mock_time.time.side_effect = advance
    mock_time.sleep.return_value = None

    from just_akash.deploy import deploy

    # The lease is made to fail on purpose; these tests assert on OUTPUT, not outcome.
    with contextlib.suppress(Exception):
        deploy(sdl_path=str(sdl_file), bid_wait=bid_wait, bid_wait_retry=bid_wait_retry)


@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_kp_fallback_round_reports_its_own_total_not_the_collection_window(
    MockAPI, mock_time, tmp_path, monkeypatch, capsys
):
    """KP, load-bearing. The fallback round must not report the finished window.

    Before the fix, `_do_poll` closed over the COLLECTION window's `bid_wait` and
    `deadline_iso`, so fallback polling printed 100% against an already-expired
    deadline while it was still polling. A progress bar that says finished while
    it is not is worse than none: it is consulted to decide whether to keep waiting.
    """
    _drive_deploy(
        tmp_path,
        monkeypatch,
        mock_time,
        MockAPI,
        bids=[{"id": {"provider": "akash1other"}, "price": {"amount": 10, "denom": "uakt"}}],
        bid_wait=10,
        bid_wait_retry=30,
    )
    out = capsys.readouterr().out
    totals = {int(m.group(2)) for m in _TOTALS.finditer(out)}
    assert 30 in totals, (
        "the fallback round must report its own total (bid_wait_retry=30); "
        f"observed totals={sorted(totals)}. Reporting only 10 means _do_poll is "
        "still reading the completed collection window."
    )


@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_kn_collection_round_still_reports_the_collection_window(
    MockAPI, mock_time, tmp_path, monkeypatch, capsys
):
    """KN. The fix must not make every round report the fallback total.

    Passing bid_wait_retry everywhere would satisfy the KP and destroy the
    collection window's reporting. Both totals must appear.
    """
    _drive_deploy(
        tmp_path,
        monkeypatch,
        mock_time,
        MockAPI,
        bids=[{"id": {"provider": "akash1other"}, "price": {"amount": 10, "denom": "uakt"}}],
        bid_wait=10,
        bid_wait_retry=30,
    )
    out = capsys.readouterr().out
    totals = {int(m.group(2)) for m in _TOTALS.finditer(out)}
    assert 10 in totals, (
        f"the collection round must still report bid_wait=10; observed totals={sorted(totals)}"
    )


@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_kp_a_log_never_lands_on_the_tail_of_an_open_status_line(
    MockAPI, mock_time, tmp_path, monkeypatch, capsys
):
    """KP, load-bearing. An API error mid-window must not corrupt the status line.

    The status line is printed with `end=""` so the next poll can overwrite it
    with \\r. Anything else writing to stdout in between lands on the SAME line,
    directly after the progress text — and an API error is precisely when the
    output must be legible.
    """
    client_bids = RuntimeError("bid API exploded")
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.setenv("AKASH_PROVIDERS", "akash1neverbids")
    sdl_file = tmp_path / "sdl.yaml"
    sdl_file.write_text(SDL_YAML)

    client = MockAPI.return_value
    client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
    client.get_bids.side_effect = client_bids
    client.create_lease.side_effect = RuntimeError("lease failed")

    counter = [0.0]

    def advance():
        counter[0] += 1
        return counter[0]

    mock_time.time.side_effect = advance
    mock_time.sleep.return_value = None

    from just_akash.deploy import deploy

    with contextlib.suppress(Exception):
        deploy(sdl_path=str(sdl_file), bid_wait=6, bid_wait_retry=12)

    out = capsys.readouterr().out
    # The corruption signature: progress-bar text followed on the SAME line by
    # a log line. `api_error=1` terminates a status line; nothing may follow it
    # on that line except the carriage-return that starts the next one.
    for raw in out.split("\n"):
        for seg in raw.split("\r"):
            if "api_error=1" in seg:
                tail = seg.split("api_error=1", 1)[1]
                assert tail.strip() == "", (
                    f"a log line was written onto the tail of an open status line: {seg!r}"
                )
