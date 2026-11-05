"""Explicit hosted SDK registration for the existing Guardian legacy-lease broker.

No route/server/deployed acquisition is supplied here. The fixed production host
is source-declared mesh-only. Root must approve this complete source, provide the
existing policy verification key and privately held actual credential/context
suppliers, and explicitly install this registration inside the original deadline.
Server-side authentication, native acquisition, CAS and one-use consume remain
mandatory; client-constructed objects and HTTP observations are not authority.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict, cast

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import atomic_lease_admission as hook

if TYPE_CHECKING:

    class _ReceiptClaims(TypedDict):
        schema: str
        audience: str
        broker_identity: str
        key_id: str
        policy_revision: str
        phase: str
        parent_digest: str
        parent_revision: int
        parent_revision_digest: str
        provider_revision: int
        provider_digest: str
        operation_id: str
        attempt: int
        attempt_id: str
        binding_digest: str
        capacity_evidence_digest: str
        placement_state: str
        predecessor_sha256: str | None
        identity: dict[str, str | int]
        original_deadline_at: int
        observed_at: int
        valid_until: int

    class _AttemptRecord(TypedDict):
        reserve: HTTPReceipt
        consume: HTTPReceipt | None
        outcome: HTTPReceipt | None
        outcome_entered: bool
        binding: hook.LeaseBinding | None
        identity: dict[str, str | int]
        operation_id: str

    class _OperationContext(Protocol):
        @property
        def operation_id(self) -> str: ...

    class _MintResult(Protocol):
        @property
        def token(self) -> str: ...
        @property
        def expires_at(self) -> int: ...


ORIGIN = "https://sentinel.digitalfrontier.so"
PATHS = {
    name: "/api/v1/ci/akash/legacy-lease/" + name for name in ("reserve", "consume", "outcome")
}
PHASE_SECONDS = {"reserve": 90.0, "consume": 30.0, "outcome": 10.0}
HOSTED_MINT_SHA256 = "80ceb0c5089a8cf583d24e436fdbb46cdcd51a3d8c7d475541ced4d11994acc9"
MAX_POSTS = 18  # Existing at most3 lease attempts x2 orders x3 broker phases.
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_WIRE_BYTES = 16384
MAX_HEADER_BYTES = 65536
SCHEMA = "sentinel-legacy-lease-send-observation/v1"
DOMAIN = (SCHEMA + "\0").encode()
API_SHA256 = "a823068ed2d4662ef632713c52b408322c5d727204b32844b5fdf15216c94fe5"
HOOK_SHA256 = "6770c631130ddc2001e0b920d61779b1c7f858c4943af707b2b3549a228b0315"
SDK_PARENTS = frozenset(
    {
        "a85f7147c9e6d36807cff54d4823d9146b7e43d2",
        "2393294697c091344ae74a7191e0834a520a845a",
    }
)
CLAIMS = frozenset(
    {
        "schema",
        "audience",
        "broker_identity",
        "key_id",
        "policy_revision",
        "phase",
        "parent_digest",
        "parent_revision",
        "parent_revision_digest",
        "provider_revision",
        "provider_digest",
        "operation_id",
        "attempt",
        "attempt_id",
        "binding_digest",
        "capacity_evidence_digest",
        "placement_state",
        "predecessor_sha256",
        "identity",
        "original_deadline_at",
        "observed_at",
        "valid_until",
    }
)
IDENTITY = frozenset(
    {
        "chain_id",
        "owner",
        "dseq",
        "provider",
        "gseq",
        "oseq",
        "body_sha256",
        "manifest_sha256",
        "source_revision",
        "sdk_parent_ref",
        "api_sha256",
        "hook_sha256",
    }
)


class BrokerHeld(Exception):
    """Finite no-retry failure; never contains a response or credentials."""


def require(ok, code="BROKER_HOLD_NO_RETRY"):
    if not ok:
        raise BrokerHeld(code)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def strict(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out)
            out[key] = value
        return out

    def reject(_):
        raise BrokerHeld("BROKER_HOLD_NO_RETRY")

    return json.loads(raw, object_pairs_hook=pairs, parse_float=reject, parse_constant=reject)


def text(value, limit=128):
    return (
        type(value) is str
        and 0 < len(value) <= limit
        and value.isascii()
        and all(33 <= ord(c) <= 126 for c in value)
    )


def digest(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


@dataclass(frozen=True)
class ReceiptPolicy:
    revision: str
    broker_identity: str
    key_id: str
    public_key: bytes = field(repr=False)
    lifetime_seconds: int = 60

    def __post_init__(self):
        require(
            type(self.revision) is str and re.fullmatch(r"[0-9a-f]{40}", self.revision) is not None
        )
        require(
            text(self.broker_identity, 256)
            and type(self.key_id) is str
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.key_id) is not None
        )
        require(
            type(self.public_key) is bytes
            and len(self.public_key) == 32
            and type(self.lifetime_seconds) is int
            and 1 <= self.lifetime_seconds <= 300
        )


@dataclass(frozen=True, repr=False)
class PrincipalCredentials:
    github_oidc: str
    github_read_token: str
    console_key: str


@dataclass(frozen=True, repr=False)
class PreparedLeaseContext:
    operation_id: str
    chain_id: str
    owner_candidate: str
    key_candidate: str
    slot: str
    source_revision: str
    original_deadline_at: int
    sdl_content: str
    request_body: bytes


@dataclass(frozen=True)
class HTTPReceipt:
    url: str
    status: int
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    wire: bytes = field(repr=False)
    claims: _ReceiptClaims = field(repr=False)
    received: float


def decode_receipt(wire, policy, *, now):
    """Strict independent client verification of the Root47e server wire codec."""
    try:
        require(
            type(policy) is ReceiptPolicy
            and type(wire) is bytes
            and 0 < len(wire) <= MAX_WIRE_BYTES
        )
        envelope = strict(wire)
        require(
            type(envelope) is dict
            and set(envelope) == {"payload", "signature"}
            and canonical(envelope) == wire
        )
        envelope = cast("dict[str, str]", envelope)
        values = []
        for key in ("payload", "signature"):
            require(type(envelope[key]) is str)
            value = base64.b64decode(envelope[key], validate=True)
            require(base64.b64encode(value).decode() == envelope[key])
            values.append(value)
        body, signature = values
        require(len(signature) == 64)
        Ed25519PublicKey.from_public_bytes(policy.public_key).verify(signature, DOMAIN + body)
        claims = strict(body)
        require(type(claims) is dict and set(claims) == CLAIMS and canonical(claims) == body)
        claims = cast("_ReceiptClaims", claims)
        require(
            (
                claims["schema"],
                claims["audience"],
                claims["broker_identity"],
                claims["key_id"],
                claims["policy_revision"],
            )
            == (
                SCHEMA,
                "legacy-lease-boundary-observation",
                policy.broker_identity,
                policy.key_id,
                policy.revision,
            )
        )
        for key in ("parent_revision", "provider_revision", "attempt"):
            require(type(claims[key]) is int and 0 < claims[key] < 2**63)
        for key in (
            "parent_digest",
            "parent_revision_digest",
            "provider_digest",
            "binding_digest",
            "capacity_evidence_digest",
        ):
            require(digest(claims[key]))
        require(
            text(claims["operation_id"])
            and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", claims["operation_id"]) is not None
        )
        require(claims["attempt_id"] == claims["parent_digest"] + "." + str(claims["attempt"]))
        phase = claims["phase"]
        require(
            phase in PATHS
            and claims["placement_state"] == ("reserved" if phase == "reserve" else "unknown")
        )
        require(
            claims["predecessor_sha256"] is None
            if phase == "reserve"
            else digest(claims["predecessor_sha256"])
        )
        identity = claims["identity"]
        require(type(identity) is dict and set(identity) == IDENTITY)
        for key, value in identity.items():
            require(
                type(value) is int and 0 < value < 2**63
                if key in {"gseq", "oseq"}
                else type(value) is str
            )
        observed, until, original = (
            claims["observed_at"],
            claims["valid_until"],
            claims["original_deadline_at"],
        )
        require(
            type(now) is int
            and type(observed) is int
            and type(until) is int
            and type(original) is int
            and 0 < observed <= now < until <= original
            and until <= observed + policy.lifetime_seconds
        )
        return claims
    except BaseException:
        raise BrokerHeld("BROKER_RECEIPT_REFUSED_NO_RETRY") from None


def _read_source(path, expected):
    fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= 1024 * 1024)
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        after = os.fstat(fd)
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        require(
            len(raw) == before.st_size
            and all(getattr(before, k) == getattr(after, k) for k in fields)
            and sha(raw) == expected
        )
    finally:
        os.close(fd)
    return raw


def _code_shape(code):
    return (
        code.co_code,
        code.co_consts,
        code.co_names,
        code.co_varnames,
        code.co_argcount,
        code.co_posonlyargcount,
        code.co_kwonlyargcount,
        code.co_flags,
        code.co_freevars,
        code.co_cellvars,
    )


class GuardianLeaseBackend(hook.LeaseAdmissionBackend):
    def __init__(
        self,
        *,
        client,
        sdk_parent_ref,
        policy,
        context_supplier: Callable[[hook.LeaseIdentity], object],
        credentials_supplier: Callable[[object], object],
        end: float,
        original_deadline_at: int,
        clock: Callable[[], float],
        utc_clock: Callable[[], float],
    ):
        require(sdk_parent_ref in SDK_PARENTS and type(policy) is ReceiptPolicy)
        require(
            callable(context_supplier)
            and callable(credentials_supplier)
            and callable(clock)
            and callable(utc_clock)
        )
        require(
            type(end) in (int, float)
            and math.isfinite(end)
            and type(original_deadline_at) is int
            and original_deadline_at > 0
        )
        self._client, self._sdk_parent, self._policy = client, sdk_parent_ref, policy
        self._client_headers = None
        self._client_key = None
        if client is not None:
            require(type(client.headers) is dict and type(client.api_key) is str)
            self._client_headers = dict(client.headers)  # private RAM only.
            self._client_key = client.api_key
        self._context_supplier, self._credentials_supplier = (
            context_supplier,
            credentials_supplier,
        )
        self._end, self._deadline_at, self._clock, self._utc = (
            end,
            original_deadline_at,
            clock,
            utc_clock,
        )
        self._blocked = False
        self._posts = 0
        self._mint_posts = 0
        self._records: dict[str, _AttemptRecord] = {}
        self._active = False
        self._pending_send: HTTPReceipt | None = None
        self._api_path = str(Path(hook.__file__).with_name("api.py"))
        require(
            Path(self._api_path).is_absolute()
            and Path(self._api_path).name == "api.py"
            and Path(self._api_path).parent.name == "just_akash"
        )
        self._rejoin()

    def _check(self):
        now = self._clock()
        require(
            type(now) in (int, float)
            and math.isfinite(now)
            and now < self._end
            and type(self._utc()) in (int, float)
            and self._utc() < self._deadline_at,
            "BROKER_ORIGINAL_DEADLINE_NO_RETRY",
        )
        return self._end - now

    def _pending_checkpoint(self):
        # A signed consume may be much shorter than the original caller end.
        # Retain it through final source retirement AND the hook resolver's
        # actual lower-boundary check. Expiry never releases or grants retry.
        pending = self._pending_send
        if pending is not None:
            now = self._utc()
            if not (
                not self._blocked
                and type(now) in (int, float)
                and math.isfinite(now)
                and pending.claims["observed_at"] <= now < pending.claims["valid_until"]
            ):
                self._blocked = True
                raise BrokerHeld("BROKER_CONSUME_EXPIRED_NO_RETRY")

    def _registration_checkpoint(self):
        self._check()
        self._pending_checkpoint()

    def _rejoin(self):
        self._check()
        raw = _read_source(self._api_path, API_SHA256)
        _read_source(hook.__file__, HOOK_SHA256)
        if self._client is None:
            self._check()
            return  # Availability before wallet selection is not a send grant.
        require(
            type(self._client).__name__ == "AkashConsoleAPI"
            and self._client.base_url == "https://console-api.akash.network"
        )
        require(
            not any(
                name in vars(self._client)
                for name in ("_request", "_open_request", "create_lease")
            )
        )
        require(
            self._client.api_key == self._client_key
            and self._client.headers == self._client_headers
            and set(cast("dict[str, str]", self._client_headers))
            == {"x-api-key", "Content-Type", "User-Agent", "Accept"}
            and cast("dict[str, str]", self._client_headers)["x-api-key"] == self._client_key
            and cast("dict[str, str]", self._client_headers)["Content-Type"]
            == cast("dict[str, str]", self._client_headers)["Accept"]
            == "application/json"
            and cast("dict[str, str]", self._client_headers)["User-Agent"]
            == "akash-just-targets/1.0"
        )
        # Preserve the original module/compiler context (including imports).
        # Compile-only: no API source or imported code is executed here.
        compiled_module = compile(ast.parse(raw), self._api_path, "exec", dont_inherit=True)
        compiled_class = next(
            code
            for code in compiled_module.co_consts
            if isinstance(code, types.CodeType) and code.co_name == "AkashConsoleAPI"
        )
        for name in ("create_lease", "_request", "_open_request"):
            method = getattr(type(self._client), name)
            require(
                type(method) is types.FunctionType
                and method.__code__.co_filename == self._api_path
            )
            expected = next(
                code
                for code in compiled_class.co_consts
                if isinstance(code, types.CodeType) and code.co_name == name
            )
            require(_code_shape(method.__code__) == _code_shape(expected))
        self._check()
        self._pending_checkpoint()

    def _identity(self, context, send, body_sha256):
        require(type(context) is PreparedLeaseContext)
        require(
            text(context.operation_id)
            and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", context.operation_id) is not None
        )
        require(
            all(
                text(value, 256)
                for value in (
                    context.chain_id,
                    context.owner_candidate,
                    context.key_candidate,
                    context.slot,
                )
            )
        )
        require(
            type(context.source_revision) is str
            and re.fullmatch(r"[0-9a-f]{40}", context.source_revision) is not None
            and context.original_deadline_at == self._deadline_at
        )
        require(
            type(context.sdl_content) is str
            and 0 < len(context.sdl_content.encode()) <= 1024 * 1024
        )
        require(
            type(context.request_body) is bytes
            and 0 < len(context.request_body) <= 2 * 1024 * 1024
            and sha(context.request_body) == body_sha256
        )
        body = strict(context.request_body)
        require(
            type(body) is dict
            and set(body) == {"manifest", "leases"}
            and type(body["manifest"]) is str
            and sha(body["manifest"].encode()) == send.manifest_sha256
        )
        require(
            type(body["leases"]) is list
            and body["leases"]
            == [
                {
                    "dseq": send.dseq,
                    "provider": send.provider,
                    "gseq": send.gseq,
                    "oseq": send.oseq,
                }
            ]
        )
        require(type(body["leases"][0]["gseq"]) is int and type(body["leases"][0]["oseq"]) is int)
        return {
            "chain_id": context.chain_id,
            "owner": context.owner_candidate,
            "dseq": send.dseq,
            "provider": send.provider,
            "gseq": send.gseq,
            "oseq": send.oseq,
            "body_sha256": body_sha256,
            "manifest_sha256": send.manifest_sha256,
            "source_revision": context.source_revision,
            "sdk_parent_ref": self._sdk_parent,
            "api_sha256": API_SHA256,
            "hook_sha256": HOOK_SHA256,
        }

    def _mint_status_delegation(self, selected_key: str, *, end: float) -> _MintResult:
        # This fixed private package sibling is the same reviewed HOSTED helper.
        # Packaging source does not install a policy or accept caller paths/tokens.
        path = Path(__file__).parent / "_hosted" / "mint_owner_status_delegation.py"
        require(self._mint_posts < MAX_POSTS)
        self._mint_posts += 1  # Charged attempt, never refunded/retried.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise BrokerHeld("BROKER_HOSTED_SYNC_CONTEXT_REQUIRED")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        module_name = "_held_hosted_status_mint_" + str(id(self))
        module = None
        try:
            before = os.fstat(fd)
            require(stat.S_ISREG(before.st_mode) and before.st_size == 8404)
            raw = os.read(fd, 8405)
            require(len(raw) == 8404 and sha(raw) == HOSTED_MINT_SHA256)
            require(module_name not in sys.modules)
            module = types.ModuleType(module_name)
            module.__file__ = str(path)
            sys.modules[module_name] = module
            types.FunctionType(compile(raw, str(path), "exec"), module.__dict__)()
            self._rejoin()
            require(self._clock() < end)
            result = asyncio.run(module.mint_owner_status_delegation(selected_key, end=end))
            require(type(result) is module.HostedStatusDelegation)
            require(type(result.expires_at) is int and self._utc() < result.expires_at)
            after = os.fstat(fd)
            fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            require(all(getattr(before, k) == getattr(after, k) for k in fields))
            require(self._clock() < end)
        finally:
            if module is not None and sys.modules.get(module_name) is module:
                del sys.modules[module_name]
            os.close(fd)
        self._rejoin()
        require(self._clock() < end and self._utc() < result.expires_at)
        return result

    def _reject_management_key_payload(self, payload):
        # Private equality only: never persist/hash/log the selected key. Check
        # the decoded nested original lease body as well as the broker envelope.
        key = cast(str, self._client_key)
        require(type(key) is str and bool(key))

        def inspect(value):
            if type(value) is str:
                require(key not in value)
            elif type(value) is dict:
                for name, child in value.items():
                    inspect(name)
                    inspect(child)
            elif type(value) in (tuple, list):
                for child in value:
                    inspect(child)

        inspect(payload)
        if "request_body" in payload:
            inspect(strict(payload["request_body"]))

    def _credentials(self, *, end):
        value = self._credentials_supplier(self._client)
        require(type(value) is PrincipalCredentials)
        value = cast(PrincipalCredentials, value)
        require(
            all(
                text(s, 16384)
                for s in (value.github_oidc, value.github_read_token, value.console_key)
            )
        )
        # The selected actual SDK key is compared privately; never hashed/logged.
        require(
            type(self._client.api_key) is str
            and value.console_key == self._client.api_key == self._client_key
        )
        require(value.console_key not in value.github_oidc)
        require(value.console_key not in value.github_read_token)
        delegation = self._mint_status_delegation(value.console_key, end=end)
        require(value.console_key not in delegation.token)
        return {
            "Authorization": "Bearer " + value.github_oidc,
            "X-GitHub-Actions-Read-Token": value.github_read_token,
            "X-Akash-Owner-Status-Delegation": delegation.token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }, delegation.expires_at

    def _exchange(self, phase, payload):
        require(not self._blocked and phase in PATHS and self._posts < MAX_POSTS)
        self._rejoin()
        body = canonical(payload)
        require(len(body) <= MAX_REQUEST_BYTES)
        self._reject_management_key_payload(payload)
        end = min(self._end, self._clock() + PHASE_SECONDS[phase])
        headers, delegation_until = self._credentials(end=end)
        self._posts += 1  # Entered once before the first possible broker wire.
        try:
            remaining = min(self._check(), end - self._clock())
            require(remaining > 0)
            with httpx.Client(
                verify=True,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(remaining),
            ) as client:
                # Client construction/source/FD retirement cannot consume the
                # independent delegated JWT lifetime before its one broker send.
                self._check()
                require(self._clock() < end and self._utc() < delegation_until)
                with client.stream(
                    "POST", ORIGIN + PATHS[phase], content=body, headers=headers
                ) as response:
                    require(
                        str(response.url) == ORIGIN + PATHS[phase] and response.status_code == 200
                    )
                    fields = response.headers.raw
                    require(
                        len(fields) <= 128
                        and sum(len(k) + len(v) + 4 for k, v in fields) <= MAX_HEADER_BYTES
                    )
                    parsed = {}
                    for key, value in fields:
                        name = key.lower()
                        require(
                            name not in parsed
                            and re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) is not None
                            and all((c >= 32 and c != 127) or c == 9 for c in value)
                        )
                        require(
                            name
                            not in {
                                b"authorization",
                                b"proxy-authorization",
                                b"set-cookie",
                                b"cookie",
                                b"x-api-key",
                                b"x-akash-console-key",
                            }
                        )
                        parsed[name] = value
                    require(
                        parsed.get(b"content-type", b"").split(b";", 1)[0].lower()
                        in {b"application/json", b"application/vnd.api+json"}
                    )
                    require(
                        parsed.get(b"content-encoding", b"identity").lower() in {b"", b"identity"}
                    )
                    require(
                        b"no-store"
                        in {
                            v.strip().lower()
                            for v in parsed.get(b"cache-control", b"").split(b",")
                        }
                    )
                    require(parsed.get(b"pragma", b"").lower() == b"no-cache")
                    age = parsed.get(b"age")
                    require(
                        age is None
                        or re.fullmatch(rb"[0-9]{1,10}", age) is not None
                        and int(age) == 0
                    )
                    length = parsed.get(b"content-length")
                    require(
                        length is None
                        or re.fullmatch(rb"[0-9]{1,10}", length) is not None
                        and 0 < int(length) <= MAX_WIRE_BYTES
                    )
                    wire = bytearray()
                    for chunk in response.iter_raw(chunk_size=4096):
                        self._check()
                        require(self._clock() < end and len(wire) + len(chunk) <= MAX_WIRE_BYTES)
                        wire.extend(chunk)
                    require(length is None or len(wire) == int(length))
                    raw = bytes(wire)
                    actual_headers = tuple(
                        (k.decode("ascii"), v.decode("latin1")) for k, v in fields
                    )
                self._check()
                require(self._clock() < end)
            self._rejoin()
            require(self._clock() < end)
            claims = decode_receipt(raw, self._policy, now=int(self._utc()))
            self._check()
            require(self._clock() < end)
            return HTTPReceipt(
                ORIGIN + PATHS[phase], 200, actual_headers, raw, claims, self._clock()
            )
        except BaseException:
            self._blocked = True
            raise BrokerHeld("BROKER_EXCHANGE_UNKNOWN_NO_RETRY") from None

    def _join(self, observation, phase, identity, context, previous=None):
        c = observation.claims
        require(
            c["phase"] == phase
            and c["operation_id"] == context.operation_id
            and c["identity"] == identity
            and c["original_deadline_at"] == self._deadline_at
        )
        if previous is not None:
            p = previous.claims
            require(c["predecessor_sha256"] == sha(previous.wire))
            require(
                all(
                    c[k] == p[k]
                    for k in (
                        "parent_digest",
                        "operation_id",
                        "attempt",
                        "attempt_id",
                        "binding_digest",
                        "capacity_evidence_digest",
                    )
                )
            )
            require(c["parent_revision"] == p["parent_revision"] + (phase == "consume"))
            require(c["provider_revision"] >= p["provider_revision"] + (phase == "consume"))
        self._rejoin()

    def before_send(self, client, send, *, body_sha256, source):
        if self._client is None:
            require(type(client.headers) is dict and type(client.api_key) is str)
            self._client = client
            self._client_headers = dict(client.headers)
            self._client_key = client.api_key
        require(
            not self._blocked
            and not self._active
            and client is self._client
            and type(send) is hook.LeaseIdentity
            and type(source) is hook.LeaseSource
            and (source.api_sha256, source.hook_sha256) == (API_SHA256, HOOK_SHA256)
        )
        self._active = True
        try:
            self._rejoin()
            context = cast(PreparedLeaseContext, self._context_supplier(send))
            identity = self._identity(context, send, body_sha256)
            common = {
                "operation_id": context.operation_id,
                "owner_candidate": context.owner_candidate,
                "key_candidate": context.key_candidate,
                "slot": context.slot,
                "identity": identity,
                "original_deadline_at": self._deadline_at,
                "sdl_content": context.sdl_content,
                "request_body": context.request_body.decode("utf-8"),
            }
            reserve = self._exchange(
                "reserve",
                dict(common, schema="sentinel-legacy-lease-reserve-request/v1"),
            )
            self._join(reserve, "reserve", identity, context)
            attempt_id = reserve.claims["attempt_id"]
            require(attempt_id not in self._records)
            record: _AttemptRecord = {
                "reserve": reserve,
                "consume": None,
                "outcome": None,
                "outcome_entered": False,
                "binding": None,
                "identity": identity,
                "operation_id": context.operation_id,
            }
            self._records[attempt_id] = record
            consume = self._exchange(
                "consume",
                dict(
                    common,
                    schema="sentinel-legacy-lease-consume-request/v1",
                    attempt=reserve.claims["attempt"],
                    attempt_id=attempt_id,
                    parent_observation=reserve.wire.decode("utf-8"),
                ),
            )
            self._pending_send = consume
            self._join(consume, "consume", identity, context, reserve)
            record["consume"] = (
                consume  # Signed server RESERVED->UNKNOWN happened BEFORE this ACK.
            )
            binding = hook.LeaseBinding(
                context.operation_id, attempt_id, client, send, body_sha256, source
            )
            record["binding"] = binding
            self._rejoin()
            return binding
        except BaseException:
            self._blocked = True
            raise BrokerHeld("BROKER_BEFORE_SEND_HELD_NO_RETRY") from None
        finally:
            self._active = False

    def retained_cleanup_locators(self):
        # Exact authenticated historical consumes are locators only. Lost ACK
        # or a partial reserve never manufactures a replacement locator.
        require(self._posts == 0 or bool(self._records), "BROKER_CLEANUP_LOCATOR_UNMEASURED")
        require(
            all(row["consume"] is not None for row in self._records.values()),
            "BROKER_CLEANUP_LOCATOR_UNMEASURED",
        )
        return tuple(cast(HTTPReceipt, row["consume"]).wire for row in self._records.values())

    def _record(self, binding):
        record = (
            self._records.get(binding.attempt_id) if type(binding) is hook.LeaseBinding else None
        )
        require(
            record is not None and record["binding"] is binding and record["consume"] is not None
        )
        return cast("_AttemptRecord", record)

    def _retire_pending_send(self, binding):
        record = self._record(binding)
        require(self._pending_send is None or self._pending_send is record["consume"])
        # Only an actual observed/UNKNOWN callback retires this send epoch.
        # The signed durable consume record remains UNKNOWN, never released.
        self._pending_send = None
        return record

    def _outcome(self, binding, *, kind, status=None, reason=None):
        record = self._record(binding)
        require(not record["outcome_entered"])
        record["outcome_entered"] = True
        wire = cast(HTTPReceipt, record["consume"])
        payload = {
            "schema": "sentinel-legacy-lease-outcome-request/v1",
            "operation_id": binding.operation_id,
            "attempt": wire.claims["attempt"],
            "attempt_id": binding.attempt_id,
            "identity": record["identity"],
            "original_deadline_at": self._deadline_at,
            "parent_observation": wire.wire.decode("utf-8"),
            "kind": kind,
            "http_status": status,
            "reason": reason,
        }
        outcome = self._exchange("outcome", payload)
        context = types.SimpleNamespace(operation_id=binding.operation_id)
        self._join(outcome, "outcome", record["identity"], context, wire)
        record["outcome"] = outcome

    def observed_http(self, binding, observation):
        require(
            type(observation) is hook.HTTPObservation
            and observation.settlement is False
            and type(observation.status) is int
            and 100 <= observation.status <= 599
        )
        try:
            self._retire_pending_send(binding)
            self._outcome(binding, kind="OBSERVED_HTTP", status=observation.status)
            return hook.RecordedOutcome(binding, "OBSERVED_HTTP")
        except BaseException:
            self._blocked = True
            raise BrokerHeld("BROKER_OUTCOME_UNKNOWN_NO_RETRY") from None

    def retain_unknown(self, binding, reason):
        record = self._retire_pending_send(binding)
        require(type(reason) is hook.UnknownKind)
        if (
            record["outcome_entered"]
            or self._blocked
            or self._clock() >= self._end
            or self._utc() >= self._deadline_at
        ):
            # The past verified consume already retained durable UNKNOWN. This
            # neither ACKs an unobserved outcome nor authorizes another attempt.
            self._blocked = True
            return hook.RecordedOutcome(binding, "UNKNOWN")
        try:
            self._outcome(binding, kind="UNKNOWN", reason=reason.value)
            self._blocked = True
            return hook.RecordedOutcome(binding, "UNKNOWN")
        except BaseException:
            self._blocked = True
            raise BrokerHeld("BROKER_UNKNOWN_NOTE_UNACKNOWLEDGED_NO_RETRY") from None


class InstalledRootRegistration:
    """Explicit code-only owned install; ambient SDK resolver stays default-off."""

    def __init__(self, backend: GuardianLeaseBackend):
        require(type(backend) is GuardianLeaseBackend)
        self.backend = backend
        self.registration = hook.Registration(hook.LeaseSource(API_SHA256, HOOK_SHA256), backend)
        self._entered = False
        self._old = None
        self._resolver = None

    def __enter__(self):
        require(not self._entered and hook._reviewed_hook() is None)
        self.backend._rejoin()
        self._old = hook._reviewed_hook

        def resolver():
            self.backend._registration_checkpoint()
            return self.registration

        self._resolver = resolver
        hook._reviewed_hook = self._resolver
        self._entered = True
        return self.registration

    def __exit__(self, exc_type, error, traceback):
        self.backend._blocked = True
        if self._entered and hook._reviewed_hook is self._resolver:
            hook._reviewed_hook = self._old
        elif error is None:
            raise BrokerHeld("BROKER_REGISTRATION_RETIREMENT_UNCERTAIN")
        # Never overwrite another resolver or mask an existing primary failure.
        return False


def install_root_broker_registration(
    *,
    client=None,
    sdk_parent_ref,
    policy,
    context_supplier,
    credentials_supplier,
    end,
    original_deadline_at,
    clock=time.monotonic,
    utc_clock=time.time,
):
    """Root must call explicitly with approved policy/source/private suppliers.

    No token acquisition, env lookup, HTTP discovery, new key or API request runs
    at import. A concrete backend is installed only inside the returned context.
    Request candidates are always server-authenticated/acquired, never evidence.
    """
    try:
        return InstalledRootRegistration(
            GuardianLeaseBackend(
                client=client,
                sdk_parent_ref=sdk_parent_ref,
                policy=policy,
                context_supplier=context_supplier,
                credentials_supplier=credentials_supplier,
                end=end,
                original_deadline_at=original_deadline_at,
                clock=clock,
                utc_clock=utc_clock,
            )
        )
    except BaseException:
        raise BrokerHeld("BROKER_REGISTRATION_REFUSED") from None
