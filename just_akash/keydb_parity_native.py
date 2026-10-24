"""Own fixed Blazing transport for KeyDB native admission; no legacy policy reuse."""

from __future__ import annotations

import json
import re
import time
import urllib.request
from http.client import HTTPException
from typing import Any, cast

ROOT = "/repos/Borduas-Holdings/blazing"


class CallerHeld(ValueError):
    """Fixed failure text; remote responses and credentials are never diagnostic text."""


def require(value: object) -> None:
    if not value:
        raise CallerHeld("Optional hosted native caller was not verified")


def positive(value: object) -> int:
    require(type(value) is int and 0 < cast(int, value) < 2**63)
    return cast(int, value)


def object_value(value: object) -> dict[str, Any]:
    require(type(value) is dict)
    return cast(dict[str, Any], value)


def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NativeGET:
    """Fixed-origin, bounded, no-redirect GET transport. Never retries a request."""

    def __init__(self, token: str):
        require(0 < len(token) <= 4096 and all(33 <= ord(c) < 127 for c in token))
        self.token = token
        self.end = time.monotonic() + 120
        self.calls = 0

    def __call__(self, route: str) -> dict[str, Any]:
        require(
            re.fullmatch(
                r"(?:" + re.escape(ROOT) + r"(?:|/actions/runs/[1-9][0-9]{0,18}"
                r"(?:/attempts/[1-9][0-9]{0,18}/jobs\?per_page=100&page=[1-4])?"
                r"|/actions/jobs/[1-9][0-9]{0,18}|/pulls/[1-9][0-9]{0,18}"
                r"|/git/ref/heads/main)|/orgs/Borduas-Holdings/actions/runner-groups"
                r"(?:\?per_page=100&page=[12]|/[1-9][0-9]{0,18}"
                r"(?:/repositories\?per_page=100&page=[12])?))",
                route,
            )
            is not None
        )
        self.calls += 1
        remaining = self.end - time.monotonic()
        require(self.calls <= 26 and remaining > 0)
        document: dict[str, Any] | None = None
        try:
            request = urllib.request.Request(
                "https://api.github.com" + route,
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "just-akash-hosted-caller",
                },
                method="GET",
            )
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(
                request, timeout=min(10, remaining)
            ) as response:
                require(response.status == 200)
                raw = response.read(1024 * 1024 + 1)
            require(len(raw) <= 1024 * 1024 and time.monotonic() < self.end)
            document = object_value(json.loads(raw, object_pairs_hook=unique))
        except (OSError, HTTPException, ValueError, TypeError, RecursionError):
            pass
        # Deliberately outside the handler so raw transport exception context cannot escape.
        require(document is not None)
        return cast(dict[str, Any], document)
