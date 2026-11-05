"""HOSTED controller only: mint a short status token using its selected client key.

Never package this helper or its management key into the tenant runner. Source
placement/custody is owned by the hosted controller, not proven by an env flag.
No principal/lease permission is inferred from this HTTPS response or constructor.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict, cast

import anyio
import httpx

URL = "https://console-api.akash.network/v1/create-jwt-token"
MAX_BODY = 65536
MAX_HEADERS = 16384


if TYPE_CHECKING:

    class _MintRequiredClaims(TypedDict):
        iss: str
        iat: int
        nbf: int
        exp: int
        version: str
        leases: dict[str, object]

    class _MintClaims(_MintRequiredClaims, total=False):
        jti: str


class DelegationMintHeld(ValueError):
    """Finite error; never include credential, token, body or transport prose."""


def _require(value):
    if not value:
        raise DelegationMintHeld("owner status delegation mint unavailable")


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            _require(key not in value)
            value[key] = item
        return value

    def reject(_):
        raise DelegationMintHeld("owner status delegation mint unavailable")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject)


def _segment(value):
    _require(
        type(value) is str
        and 0 < len(value) <= 16384
        and re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None
    )
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    _require(base64.urlsafe_b64encode(raw).decode().rstrip("=") == value)
    return raw


@dataclass(frozen=True, repr=False)
class HostedStatusDelegation:
    token: str = field(repr=False)
    minted_request_started_at: int
    expires_at: int

    def __repr__(self):
        return "HostedStatusDelegation(<private>)"

    __str__ = __repr__


async def mint_owner_status_delegation(
    selected_console_api_key: str, *, end: float
) -> HostedStatusDelegation:
    """One fixed POST/5s, further shrunk by the same original phase's end.

    selected_console_api_key belongs to the actual selected hosted SDK client.
    No key lookup/selection, ambient environment, token renewal or tenant export
    is performed here. The caller sends ONLY result.token to the broker.
    """
    try:
        started = time.monotonic()
        _require(type(end) in (int, float) and math.isfinite(end))
        finish = min(started + 5, end)
        _require(time.monotonic() < finish)
        _require(
            type(selected_console_api_key) is str
            and re.fullmatch(r"[!-~]{1,8192}", selected_console_api_key) is not None
        )
        request_started_at = int(time.time())
        with anyio.fail_after(finish - time.monotonic()):
            async with httpx.AsyncClient(
                verify=True,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(min(5, finish - time.monotonic())),
            ) as client:
                async with client.stream(
                    "POST",
                    URL,
                    headers={
                        "x-api-key": selected_console_api_key,
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                    json={
                        "data": {"ttl": 30, "leases": {"access": "scoped", "scope": ["status"]}}
                    },
                ) as response:
                    _require(
                        time.monotonic() < finish
                        and response.status_code == 200
                        and str(response.url) == URL
                    )
                    fields = response.headers.raw
                    _require(
                        len(fields) <= 128
                        and sum(len(k) + len(v) + 4 for k, v in fields) <= MAX_HEADERS
                    )
                    headers = {}
                    for key, value in fields:
                        _require(re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) is not None)
                        _require(all(x == 9 or 32 <= x < 127 for x in value))
                        name = key.lower()
                        _require(
                            name not in headers
                            and name
                            not in {
                                b"link",
                                b"set-cookie",
                                b"authorization",
                                b"proxy-authorization",
                            }
                        )
                        headers[name] = value
                    _require(
                        headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
                        == b"application/json"
                    )
                    _require(
                        headers.get(b"content-encoding", b"identity").lower() in {b"", b"identity"}
                    )
                    transfer, length = (
                        headers.get(b"transfer-encoding"),
                        headers.get(b"content-length"),
                    )
                    _require(transfer is None or transfer.lower() == b"chunked")
                    _require(transfer is None or length is None)
                    _require(
                        length is None
                        or (
                            re.fullmatch(rb"[0-9]{1,10}", length) is not None
                            and int(length) <= MAX_BODY
                        )
                    )
                    body = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=4096):
                        _require(time.monotonic() < finish and len(body) + len(chunk) <= MAX_BODY)
                        body.extend(chunk)
                    _require(length is None or len(body) == int(length))
                _require(time.monotonic() < finish)
            _require(time.monotonic() < finish)
        envelope = _json(bytes(body))
        _require(
            type(envelope) is dict
            and set(envelope) == {"data"}
            and type(envelope["data"]) is dict
            and set(envelope["data"]) == {"token"}
        )
        token = cast("dict[str, dict[str, str]]", envelope)["data"]["token"]
        _require(type(token) is str and len(token) <= 16384 and len(token.split(".")) == 3)
        parts = token.split(".")
        header = _json(_segment(parts[0]))
        claims = _json(_segment(parts[1]))
        signature = _segment(parts[2])
        _require(
            header == {"alg": "ES256KADR36", "typ": "JWT"}
            and len(signature) == 64
            and type(claims) is dict
        )
        _require(
            set(claims)
            in (
                {"iss", "iat", "nbf", "exp", "version", "leases"},
                {"iss", "iat", "nbf", "exp", "version", "leases", "jti"},
            )
        )
        claims = cast("_MintClaims", claims)
        now = int(time.time())
        iat, nbf, exp = claims.get("iat"), claims.get("nbf"), claims.get("exp")
        _require(
            all(type(x) is int for x in (iat, nbf, exp))
            and 0 < nbf <= iat <= now < exp
            and request_started_at <= iat
            and 0 < exp - iat <= 30
        )
        _require(
            claims["version"] == "v1"
            and claims["leases"] == {"access": "scoped", "scope": ["status"]}
        )
        _require(type(claims["iss"]) is str and len(claims["iss"]) <= 128)
        _require(
            "jti" not in claims
            or (
                type(claims["jti"]) is str
                and re.fullmatch(r"[!-~]{1,256}", claims["jti"]) is not None
            )
        )
        _require(time.monotonic() < finish and int(time.time()) < exp)
        return HostedStatusDelegation(token, request_started_at, exp)
    except asyncio.CancelledError:
        raise asyncio.CancelledError() from None
    except Exception:
        raise DelegationMintHeld("owner status delegation mint unavailable") from None
