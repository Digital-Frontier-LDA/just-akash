"""Offline exact pure-code observation fixture; API/owner runtime imports excluded."""

from __future__ import annotations

import base64
import json
import re
from typing import Any, cast

from .address import is_canonical_akash_address
from .scanner_provenance import canonical_bytes, require

MAX_ENTRIES = 1000


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
