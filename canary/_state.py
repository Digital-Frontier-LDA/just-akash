"""Durable-state file loading, shared by the collector and the ensure step.

ONE COPY, DELIBERATELY. This started as the same function pasted into both canary/ensure.py
and canary/collect.py. Two copies of a "degrade, never crash" helper is exactly the shape
that drifts — one gains an exception class the other does not, and the module that kept the
narrower version starts crashing on a file the other one tolerates. Since the whole point of
this helper is that it must never be the reason a run dies, divergence here is worse than
usual.
"""

from __future__ import annotations

import json
import pathlib


def load_json_mapping(path: pathlib.Path) -> dict:
    """Read a JSON object, treating missing/empty/corrupt/non-object as an empty mapping.

    NOT defensive programming for its own sake. `git show BRANCH:file > out` CREATES `out`
    before the command runs, so a first run — where the telemetry branch has no such file
    yet — leaves a zero-byte file behind rather than no file. `json.loads("")` then raises
    and takes the whole run down, which is exactly how the first live dispatch failed.

    An unreadable state file must degrade to "no prior state", never to a crash: the
    collector's job is to publish a reading, and refusing to run because its own bookkeeping
    is unparseable loses the measurement it exists to take — on precisely the run where
    something interesting may be happening.

    UnicodeDecodeError is caught explicitly and is NOT covered by OSError: a truncated or
    binary state file (an interrupted write, a git object read that produced junk) raises it
    from read_text, not from json. Catching only OSError and JSONDecodeError would leave the
    helper crashing on the one corruption case it most plausibly meets.
    """
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
