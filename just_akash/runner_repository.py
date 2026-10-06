"""Hosted, fixed private Blazing runner authority; no caller-selected API origin."""

import json
import os
import re
import urllib.request
from contextlib import suppress
from datetime import datetime, timezone
from http.client import HTTPException


class NativeReaderRepositoryError(ValueError):
    """Fixed hold stage for unverified repository registration authority."""


class _GroupNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Repository response was not verified")
        result[key] = value
    return result


def _native_repository_request(*, registration: bool = False) -> dict:
    """Fixed repository only; native HTTP errors/bodies never escape this boundary."""
    token = os.environ.get("GH_TOKEN", "")
    if not token or len(token) > 4096 or any(not 33 <= ord(c) < 127 for c in token):
        raise NativeReaderRepositoryError("Native reader repository authority was not verified")
    path = "/repos/Borduas-Holdings/blazing"
    if registration:
        path += "/actions/runners/registration-token"
    try:
        request = urllib.request.Request(
            "https://api.github.com" + path,
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "just-akash-native-reader-policy",
            },
            method="POST" if registration else "GET",
        )
        with urllib.request.build_opener(_GroupNoRedirect()).open(request, timeout=20) as response:
            if response.status != (201 if registration else 200):
                raise ValueError("Unverified repository response")
            raw = response.read(1024 * 1024 + 1)
        if len(raw) <= 1024 * 1024:
            document = json.loads(raw, object_pairs_hook=_unique_object)
            if isinstance(document, dict):
                return document
    except (OSError, HTTPException, ValueError, TypeError, RecursionError):
        pass
    raise NativeReaderRepositoryError("Native reader repository authority was not verified")


def verify_native_reader_repository_identity() -> None:
    """Fresh server-derived identity for the fixed private repository only."""
    identity = _native_repository_request()
    if (
        type(identity.get("id")) is not int
        or identity["id"] != 1074974924
        or identity.get("full_name") != "Borduas-Holdings/blazing"
        or identity.get("private") is not True
    ):
        raise NativeReaderRepositoryError("Native reader repository identity was not verified")


def _registration_expiry_valid(value: object, now: datetime) -> bool:
    """Accept bounded RFC3339 instants without discarding sub-microsecond TTL bounds."""
    if not isinstance(value, str) or not 20 <= len(value) <= 40:
        return False
    shape = re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.(?P<fraction>[0-9]{1,9}))?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
        value,
    )
    if shape is None:
        return False
    with suppress(ValueError, TypeError, OverflowError):
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
        delta = expiry - now
        # Python datetime retains six fractional digits. Preserve the remaining
        # nanoseconds so a value just beyond either TTL boundary cannot round in.
        fraction_ns = int((shape.group("fraction") or "").ljust(9, "0"))
        remaining_ns = (
            (delta.days * 86400 + delta.seconds) * 1_000_000_000
            + delta.microseconds * 1000
            + fraction_ns % 1000
        )
        return 0 < remaining_ns <= 65 * 60 * 1_000_000_000
    return False


def verify_native_reader_repository() -> None:
    """Prove exact private identity and actual POST authority; discard its token."""
    verify_native_reader_repository_identity()
    document = _native_repository_request(registration=True)
    token, expiry = document.get("token"), document.get("expires_at")
    valid_expiry = _registration_expiry_valid(expiry, datetime.now(timezone.utc))
    if (
        not isinstance(token, str)
        or re.fullmatch(r"[A-Za-z0-9]{1,4096}", token) is None
        or not valid_expiry
    ):
        raise NativeReaderRepositoryError("Native reader registration POST was not verified")
    verify_native_reader_repository_identity()
