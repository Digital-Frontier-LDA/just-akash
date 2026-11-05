"""HOSTED-only raw GHA inputs, never verified principals or capacity authority.

The vendor domain is documented. Opaque path/query and request token are trusted
ONLY as one hosted job-service-connection environment snapshot, not signed path
or runner attestation. The server must independently verify all native joins.
"""

from __future__ import annotations

import base64
import importlib
import json
import logging
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict, cast
from urllib.parse import quote, unquote_plus, urlsplit

import httpx

from .hosted_legacy_cli import HostedGHAPrincipals

if TYPE_CHECKING:

    class _JWTClaims(TypedDict):
        iss: str
        aud: str
        iat: int
        nbf: int
        exp: int


MAX_CALLS = 19
MAX_BODY = 16 * 1024
MAX_HEADERS = 16 * 1024  # retained parsed representation, not raw/preparse TLS
MAX_REQUEST_SECONDS = 5.0
ISSUER = "https://token.actions.githubusercontent.com"
_URL_ENV = "ACTIONS_ID_TOKEN_REQUEST_URL"
_REQUEST_CREDENTIAL_NAME = "ACTIONS_ID_TOKEN_REQUEST_TOKEN"
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.actions\.githubusercontent\.com")
_HEADER = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


class HostedOIDCUnknown(Exception):
    """Finite permanent refusal for this original acquisition operation."""


def _require(value):
    if not value:
        raise HostedOIDCUnknown("hosted-oidc-UNKNOWN-no-refresh")


@dataclass(frozen=True)
class HostedOIDCPolicy:
    audience: str
    principal_lifetime_seconds: int = 30


def _reviewed_hosted_oidc_policy():
    # Root must adopt an exact reviewed hosted source/capsule and permissions.
    # No environment flag, caller endpoint or typed object activates this leaf.
    return None


def _secret(value):
    _require(type(value) is str and 0 < len(value) <= 16384)
    _require(all(33 <= ord(c) <= 126 for c in value))
    return value


def _strict(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            _require(key not in result)
            result[key] = value
        return result

    def finite(value):
        number = float(value)
        _require(math.isfinite(number))
        return number

    def constant(_value):
        raise HostedOIDCUnknown("hosted-oidc-UNKNOWN-no-refresh")

    return json.loads(
        raw.decode("utf-8", errors="strict"),
        object_pairs_hook=pairs,
        parse_float=finite,
        parse_constant=constant,
    )


def _endpoint(raw, audience):
    _require(type(raw) is str and 0 < len(raw) <= 4096)
    _require(all(33 <= ord(c) <= 126 for c in raw) and "\\" not in raw and "#" not in raw)
    _require(re.search(r"%(?![0-9a-fA-F]{2})", raw) is None)
    parsed = urlsplit(raw)
    host = parsed.hostname
    _require(parsed.scheme == "https" and host is not None and _HOST.fullmatch(host))
    _require(parsed.netloc in (host, cast(str, host) + ":443") and parsed.port in (None, 443))
    _require(parsed.username is None and parsed.password is None and not parsed.fragment)
    _require(parsed.path.startswith("/") and not parsed.path.startswith("//"))
    # HTTPX must preserve the runner's opaque path/query byte-for-byte. Explicit
    # :443 canonicalizes only the transport origin, never opaque signed data.
    opaque = parsed.path + (("?" + parsed.query) if "?" in raw else "")
    _require(httpx.URL(raw).raw_path == opaque.encode("ascii"))
    seen = []
    for field in parsed.query.split("&") if parsed.query else ():
        name, separator, value = field.partition("=")
        if unquote_plus(name, errors="strict") == "audience":
            _require(separator == "=")
            seen.append(unquote_plus(value, errors="strict"))
    _require(len(seen) <= 1 and (not seen or seen[0] == audience))
    if seen:
        final = raw
    else:
        separator = "" if raw.endswith("?") else "&" if "?" in raw else "?"
        final = raw + separator + "audience=" + quote(audience, safe="")
    _require(len(final) <= 8192)
    expected = httpx.URL(final)
    _require(
        expected.raw_path
        == (
            opaque
            + ("" if seen else cast(str, separator) + "audience=" + quote(audience, safe=""))
        ).encode("ascii")
    )
    return expected


def _headers(response):
    raw = response.headers.raw
    _require(type(raw) is list and len(raw) <= 128)
    seen = {}
    total = 0
    for name, value in cast("list[tuple[bytes, bytes]]", raw):
        _require(type(name) is bytes and type(value) is bytes and _HEADER.fullmatch(name))
        _require(all(32 <= c <= 126 or c == 9 for c in value))
        total += len(name) + len(value) + 4
        _require(total <= MAX_HEADERS)
        key = name.lower()
        _require(key not in seen and key not in (b"link", b"location", b"transfer-encoding"))
        seen[key] = value
    _require(
        seen.get(b"content-type", b"").lower()
        in (b"application/json", b"application/json; charset=utf-8")
    )
    _require(seen.get(b"content-encoding", b"identity").lower() == b"identity")
    length = seen.get(b"content-length", b"")
    _require(re.fullmatch(rb"[1-9][0-9]{0,4}", length) and int(length) <= MAX_BODY)
    return int(length)


def _jwt_exp(token, audience, now):
    _require(type(token) is str and 0 < len(token) <= MAX_BODY)
    _require(re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token))
    parts = []
    for value in token.split("."):
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        _require(base64.urlsafe_b64encode(decoded).decode().rstrip("=") == value)
        parts.append(decoded)
    header, claims = _strict(parts[0]), _strict(parts[1])
    _require(type(header) is dict and type(claims) is dict and bool(parts[2]))
    _require(header.get("alg") == "RS256" and header.get("typ") == "JWT")
    _require(
        type(header.get("kid")) is str
        and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", cast(str, header["kid"]))
    )
    _require(not set(header).intersection({"jku", "jwk", "x5u", "crit", "b64"}))
    claims = cast("_JWTClaims", claims)
    _require(claims.get("iss") == ISSUER and claims.get("aud") == audience)
    _require(all(type(claims.get(k)) is int and claims[k] > 0 for k in ("iat", "nbf", "exp")))
    _require(claims["iat"] <= now and claims["nbf"] <= now < claims["exp"])
    _require(now - claims["iat"] <= 30)
    # Parsing is a private freshness/shape screen, NOT signature verification.
    return claims["exp"]


@contextmanager
def _private_network_logs():
    """Retire only our same-thread filters; preserve other threads' logging."""
    importlib.import_module("httpcore")  # eager logger registration; no wire
    thread = threading.get_ident()

    class Private(logging.Filter):
        def filter(self, record):
            network = record.name in ("httpx", "httpcore") or record.name.startswith(
                ("httpx.", "httpcore.")
            )
            return not (record.thread == thread and network)

    owned = Private()
    names = {"httpx", "httpcore"}
    names.update(
        name
        for name in logging.Logger.manager.loggerDict
        if name.startswith(("httpx.", "httpcore."))
    )
    loggers = [logging.getLogger(name) for name in names]
    handlers = {
        handler for logger in loggers + [logging.getLogger()] for handler in logger.handlers
    }
    # Parent logger filters do not inspect propagated child records. Existing
    # reachable handlers additionally filter new network child logger records.
    targets = [*loggers, *handlers]
    for target in targets:
        target.addFilter(owned)
    try:
        yield
    finally:
        intact = all(owned in target.filters for target in targets)
        for target in targets:
            target.removeFilter(owned)
        _require(intact)


class HostedOIDCAcquirer:
    """One original operation,19 total attempts; failure seals it permanently.

    Instances/tokens have no public repr fields. Construct once in the reviewed
    hosted bootstrap; reusing original bounds is mandatory, not per-call reset.
    """

    def __init__(self, *, policy, actions_read_token, end, original_deadline_at):
        self._sealed = True
        self._calls = 0
        self._lock = threading.Lock()
        try:
            _require(type(policy) is HostedOIDCPolicy and policy is _reviewed_hosted_oidc_policy())
            _require(
                type(policy.audience) is str and re.fullmatch(r"[!-~]{1,256}", policy.audience)
            )
            _require(
                type(policy.principal_lifetime_seconds) is int
                and 0 < policy.principal_lifetime_seconds <= 30
            )
            _require(type(end) in (int, float) and math.isfinite(end))
            _require(type(original_deadline_at) is int and original_deadline_at > 0)
            self._policy, self._end, self._deadline = policy, end, original_deadline_at
            self._read = _secret(actions_read_token)
            self._base = os.environ.get(_URL_ENV)
            self._token = _secret(os.environ.get(_REQUEST_CREDENTIAL_NAME))
            self._url = _endpoint(self._base, policy.audience)
            self._sealed = False
            self._check()
        except BaseException:
            self._sealed = True
            raise HostedOIDCUnknown("hosted-oidc-unavailable-no-refresh") from None

    def _check(self, attempt_end=None):
        _require(not self._sealed and self._policy is _reviewed_hosted_oidc_policy())
        _require(
            os.environ.get(_URL_ENV) == self._base
            and os.environ.get(_REQUEST_CREDENTIAL_NAME) == self._token
        )
        _require(time.monotonic() < self._end and time.time() < self._deadline)
        if attempt_end is not None:
            _require(time.monotonic() < attempt_end)

    def __call__(self):
        if not self._lock.acquire(blocking=False):
            self._sealed = True
            raise HostedOIDCUnknown("hosted-oidc-concurrent-UNKNOWN-no-refresh")
        try:
            try:
                self._check()
                _require(self._calls < MAX_CALLS)
                self._calls += 1  # claim before any client/wire, never refill on failure
                attempt_end = min(self._end, time.monotonic() + MAX_REQUEST_SECONDS)
                with _private_network_logs():
                    self._check(attempt_end)
                    with httpx.Client(
                        trust_env=False,
                        follow_redirects=False,
                        verify=True,
                        http2=False,
                        timeout=max(0.001, attempt_end - time.monotonic()),
                    ) as client:
                        self._check(attempt_end)
                        with client.stream(
                            "GET",
                            self._url,
                            headers={
                                "Authorization": "Bearer " + cast(str, self._token),
                                "Accept": "application/json",
                                "Accept-Encoding": "identity",
                            },
                        ) as response:
                            self._check(attempt_end)
                            _require(response.status_code == 200 and not response.history)
                            _require(
                                response.url == self._url and str(response.url) == str(self._url)
                            )
                            expected = _headers(response)
                            raw = bytearray()
                            for chunk in response.iter_raw(chunk_size=4096):
                                self._check(attempt_end)
                                _require(
                                    type(chunk) is bytes and len(raw) + len(chunk) <= MAX_BODY
                                )
                                raw.extend(chunk)
                            _require(len(raw) == expected)
                            data = _strict(bytes(raw))
                            _require(type(data) is dict and set(data) == {"value"})
                            token = cast("dict[str, str]", data)["value"]
                            expiration = _jwt_exp(token, self._policy.audience, time.time())
                        self._check(attempt_end)  # response retirement included
                    self._check(attempt_end)  # client retirement included
                self._check(attempt_end)  # logging-filter retirement included
                now = time.time()
                until = int(
                    min(now + self._policy.principal_lifetime_seconds, expiration, self._deadline)
                )
                _require(now < until)
                return HostedGHAPrincipals(token, self._read, until)
            except BaseException:
                self._sealed = True
                raise HostedOIDCUnknown("hosted-oidc-UNKNOWN-no-refresh") from None
        finally:
            self._lock.release()


def make_hosted_oidc_acquirer(*, policy, actions_read_token, end, original_deadline_at):
    """Bind once as Registration.acquire_principals; no ambient installation."""
    return HostedOIDCAcquirer(
        policy=policy,
        actions_read_token=actions_read_token,
        end=end,
        original_deadline_at=original_deadline_at,
    )
