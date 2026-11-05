"""Default-off HOSTED cleanup-purpose callback; no Provision-policy substitution.

Historical signed consume is ONLY an opaque SQL locator. The server must freshly
identify the actual cleanup job/source/owner and independently acquire whole
native closure, then perform the original same-store CAS and exact readback.
A destroy result, constructor, HTTP200 or ordinary verify-closed is not that proof.
"""

from __future__ import annotations

import ast
import base64
import math
import re
import time
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import guardian_legacy_lease_backend as sender

URL = sender.ORIGIN + "/api/v1/ci/akash/legacy-lease/close"
if TYPE_CHECKING:

    class _ClosedClaims(TypedDict):
        schema: str
        audience: str
        key_id: str
        verifier_policy_revision: str
        broker_identity: str
        phase: str
        placement_state: str
        identity: dict[str, str | int]
        operation_id: str
        attempt: int
        attempt_id: str
        parent_digest: str
        original_deadline_at: int
        binding_digest: str
        parent_revision: int
        provider_revision: int
        observed_at: int
        valid_until: int
        parent_revision_digest: str
        provider_revision_digest: str
        predecessor_sha256: str
        closure_evidence_digest: str
        close_tx_hash: str
        execution_closed: bool
        settlement_proven: bool


SCHEMA = "sentinel-legacy-lease-closed-observation/v1"
DOMAIN = (SCHEMA + "\0").encode()
KEYS = frozenset(
    {
        "schema",
        "audience",
        "key_id",
        "verifier_policy_revision",
        "broker_identity",
        "phase",
        "identity",
        "operation_id",
        "attempt",
        "attempt_id",
        "parent_digest",
        "parent_revision",
        "parent_revision_digest",
        "provider_revision",
        "provider_revision_digest",
        "binding_digest",
        "predecessor_sha256",
        "close_tx_hash",
        "closure_evidence_digest",
        "placement_state",
        "execution_closed",
        "settlement_proven",
        "original_deadline_at",
        "observed_at",
        "valid_until",
    }
)


class CleanupUnknown(Exception):
    """No retry or release. Fixed diagnostic only, never raw transport/body."""


def require(value):
    if not value:
        raise CleanupUnknown("legacy-capacity-cleanup-UNKNOWN-no-retry")


def close_candidate(result):
    """Two explicit finite source-owned candidate shapes, not a response promise."""
    require(type(result) is dict)
    candidates = []
    if "txhash" in result:
        candidates.append(result["txhash"])
    if "tx_response" in result:
        row = result["tx_response"]
        require(type(row) is dict and "txhash" in row)
        candidates.append(row["txhash"])
    require(bool(candidates))
    for value in candidates:
        require(type(value) is str and re.fullmatch(r"[0-9A-F]{64}", value) is not None)
    require(len(set(candidates)) == 1)
    return candidates[0]


def historical_consume(wire, policy):
    """Authenticates historical bytes, deliberately NOT current send authority."""
    try:
        require(type(wire) is bytes and 0 < len(wire) <= 16384)
        envelope = cast("dict[str, str]", sender.strict(wire))
        payload = sender.strict(base64.b64decode(envelope["payload"], validate=True))
        observed = payload["observed_at"]
        require(type(observed) is int and observed > 0)
        claims = sender.decode_receipt(wire, policy, now=observed)
        require(claims["phase"] == "consume" and claims["placement_state"] == "unknown")
        return claims
    except BaseException:
        raise CleanupUnknown("legacy-capacity-locator-UNKNOWN-no-retry") from None


@dataclass(frozen=True, repr=False)
class ClosedCapacityObservation:
    wire: bytes = field(repr=False)
    claims: _ClosedClaims = field(repr=False)
    execution_closed: bool = True
    settlement_proven: bool = False
    lease_permit: bool = False


def decode_closed(wire, policy, consume_wire, consume, candidate, *, now):
    """Exact new37b response contract; real server vectors/review still required."""
    try:
        require(type(wire) is bytes and 0 < len(wire) <= 16384)
        env = sender.strict(wire)
        require(
            type(env) is dict
            and set(env) == {"payload", "signature"}
            and sender.canonical(env) == wire
        )
        env = cast("dict[str, str]", env)
        body = base64.b64decode(env["payload"], validate=True)
        signature = base64.b64decode(env["signature"], validate=True)
        require(
            base64.b64encode(body).decode() == env["payload"]
            and base64.b64encode(signature).decode() == env["signature"]
            and len(signature) == 64
        )
        Ed25519PublicKey.from_public_bytes(policy.public_key).verify(signature, DOMAIN + body)
        c = sender.strict(body)
        require(type(c) is dict and set(c) == KEYS and sender.canonical(c) == body)
        c = cast("_ClosedClaims", c)
        require(
            (
                c["schema"],
                c["audience"],
                c["key_id"],
                c["verifier_policy_revision"],
                c["broker_identity"],
                c["phase"],
                c["placement_state"],
            )
            == (
                SCHEMA,
                "akash-legacy-capacity-closure-observation",
                policy.key_id,
                policy.revision,
                policy.broker_identity,
                "close",
                "closed",
            )
        )
        require(c["execution_closed"] is True and c["settlement_proven"] is False)
        for name in (
            "identity",
            "operation_id",
            "attempt",
            "attempt_id",
            "parent_digest",
            "original_deadline_at",
            "binding_digest",
        ):
            require(sender.canonical(c[name]) == sender.canonical(consume[name]))
        for name in (
            "parent_revision",
            "provider_revision",
            "attempt",
            "original_deadline_at",
            "observed_at",
            "valid_until",
        ):
            require(type(c[name]) is int and 0 < c[name] < 2**63)
        require(
            c["parent_revision"] >= consume["parent_revision"] + 1
            and c["provider_revision"] >= consume["provider_revision"]
        )
        for name in (
            "parent_digest",
            "parent_revision_digest",
            "provider_revision_digest",
            "binding_digest",
            "predecessor_sha256",
            "closure_evidence_digest",
        ):
            require(sender.digest(c[name]))
        require(
            c["predecessor_sha256"] == sender.sha(consume_wire) and c["close_tx_hash"] == candidate
        )
        require(
            type(now) in (int, float)
            and math.isfinite(now)
            and 0 < c["observed_at"] <= now < c["valid_until"] <= c["observed_at"] + 30
        )
        return ClosedCapacityObservation(wire, c)
    except BaseException:
        raise CleanupUnknown("legacy-capacity-response-UNKNOWN-no-retry") from None


def _unavailable_lease_context(_):
    raise CleanupUnknown("cleanup-purpose-has-no-lease-authority")


class CleanupOnce:
    """Explicit source-owned cleanup registration; no default/ambient activation."""

    def __init__(
        self,
        *,
        client=None,
        consume_wire,
        policy,
        credentials_supplier,
        end,
        cleanup_deadline_at,
        clock=time.monotonic,
        utc_clock=time.time,
    ):
        self.consume = historical_consume(consume_wire, policy)
        self.consume_wire, self.client, self.policy = consume_wire, client, policy
        self.end, self.clock, self.utc = end, clock, utc_clock
        self.entered = False
        self.submitted = False
        self.observation = None
        # Reuse ONLY source/key/credential custody from the corrected HOSTED
        # sender. Never install its Provision resolver or call reserve/consume.
        self.auth = sender.GuardianLeaseBackend(
            client=client,
            sdk_parent_ref=self.consume["identity"]["sdk_parent_ref"],
            policy=policy,
            context_supplier=_unavailable_lease_context,
            credentials_supplier=credentials_supplier,
            end=end,
            original_deadline_at=cleanup_deadline_at,
            clock=clock,
            utc_clock=utc_clock,
        )

    def check(self):
        self.auth._rejoin()
        require(self.clock() < self.end)

    def before_close(self, closer, dseq, expected_owner):
        require(
            not self.entered
            and dseq == self.consume["identity"]["dseq"]
            and expected_owner == self.consume["identity"]["owner"]
        )
        # Actual CLI returns an evidence-gated wrapper. Keep its original
        # final expiry gate on DELETE; extract its private selected client only
        # for fresh HOSTED credential mint, never to bypass that wrapper.
        wallet_path = Path(sender.hook.__file__).with_name("wallet_pool.py")
        wallet = sender._read_source(
            wallet_path, "9e636ec5107d13d17f52fabfda08b06a66c33204f245ed07d9a22f6bc82bf2bf"
        )
        cls = next(
            n
            for n in ast.parse(wallet).body
            if isinstance(n, ast.ClassDef) and n.name == "_AuthorizedBoundOwnerCloser"
        )
        node = next(
            n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "close_deployment"
        )
        future = ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0
        )
        code = compile(
            ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[])),
            str(wallet_path),
            "exec",
            dont_inherit=True,
        )
        expected = next(c for c in code.co_consts if isinstance(c, types.CodeType))
        method = getattr(type(closer), "close_deployment", None)
        require(
            type(closer).__name__ == "_AuthorizedBoundOwnerCloser"
            and type(method) is types.FunctionType
            and method.__code__.co_filename == str(wallet_path)
            and sender._code_shape(method.__code__) == sender._code_shape(expected)
            and "close_deployment" not in vars(closer)
        )
        require(
            type(closer.evidence) is dict
            and closer.evidence.get("owner") == expected_owner
            and str(closer.evidence.get("dseq")) == dseq
        )
        client = closer._client
        if self.client is None:
            require(type(client.headers) is dict and type(client.api_key) is str)
            self.client = client
            self.auth._client = client
            self.auth._client_headers = dict(client.headers)
            self.auth._client_key = client.api_key
        require(client is self.client)
        self.check()
        # The actual method invoked by this wrapper must remain originalAPI.
        raw = sender._read_source(self.auth._api_path, sender.API_SHA256)
        cls = next(
            n
            for n in ast.parse(raw).body
            if isinstance(n, ast.ClassDef) and n.name == "AkashConsoleAPI"
        )
        node = next(
            n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "close_deployment"
        )
        code = compile(
            ast.Module(body=[node], type_ignores=[]),
            self.auth._api_path,
            "exec",
            dont_inherit=True,
        )
        expected = next(c for c in code.co_consts if isinstance(c, types.CodeType))
        actual = type(client).close_deployment
        require(
            type(actual) is types.FunctionType
            and actual.__code__.co_filename == self.auth._api_path
            and sender._code_shape(actual.__code__) == sender._code_shape(expected)
            and "close_deployment" not in vars(client)
        )
        self.check()
        self.entered = True  # One original DELETE only; no replay after any outcome.

    def after_close(self, result):
        require(self.entered and not self.submitted)
        candidate = close_candidate(result)
        self.check()
        end = min(self.end, self.clock() + 30)
        payload = dict(
            schema="sentinel-legacy-lease-close-request/v1",
            operation_id=self.consume["operation_id"],
            attempt=self.consume["attempt"],
            attempt_id=self.consume["attempt_id"],
            identity=self.consume["identity"],
            original_deadline_at=self.consume["original_deadline_at"],
            parent_observation=self.consume_wire.decode("utf-8"),
            close_tx_hash=candidate,
        )
        self.auth._reject_management_key_payload(payload)
        body = sender.canonical(payload)
        require(len(body) <= 65536)
        headers, until = self.auth._credentials(end=end)
        self.submitted = True
        with httpx.Client(
            verify=True,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(min(30, end - self.clock())),
        ) as session:
            self.check()
            require(self.clock() < end and self.utc() < until)
            with session.stream("POST", URL, headers=headers, content=body) as response:
                require(response.status_code == 200 and str(response.url) == URL)
                fields = response.headers.raw
                require(
                    len(fields) <= 128 and sum(len(k) + len(v) + 4 for k, v in fields) <= 65536
                )
                parsed = {}
                for key, value in fields:
                    name = key.lower()
                    require(
                        name not in parsed
                        and re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) is not None
                    )
                    require(all((c >= 32 and c != 127) or c == 9 for c in value))
                    require(
                        name
                        not in {
                            b"authorization",
                            b"proxy-authorization",
                            b"cookie",
                            b"set-cookie",
                            b"x-api-key",
                            b"x-akash-console-key",
                        }
                    )
                    parsed[name] = value
                require(
                    parsed.get(b"content-type", b"").split(b";", 1)[0].lower()
                    == b"application/json"
                )
                require(parsed.get(b"content-encoding", b"identity").lower() in {b"", b"identity"})
                require(
                    b"no-store"
                    in {v.strip().lower() for v in parsed.get(b"cache-control", b"").split(b",")}
                    and parsed.get(b"pragma", b"").lower() == b"no-cache"
                )
                age = parsed.get(b"age")
                require(
                    age is None
                    or (re.fullmatch(rb"[0-9]{1,10}", age) is not None and int(age) == 0)
                )
                length = parsed.get(b"content-length")
                require(
                    length is None
                    or (
                        re.fullmatch(rb"[0-9]{1,10}", length) is not None
                        and 0 < int(length) <= 16384
                    )
                )
                wire = bytearray()
                for chunk in response.iter_raw(chunk_size=4096):
                    self.check()
                    require(self.clock() < end and len(wire) + len(chunk) <= 16384)
                    wire.extend(chunk)
                require(length is None or len(wire) == int(length))
            self.check()
            require(self.clock() < end)
        self.check()
        require(self.clock() < end)
        observation = decode_closed(
            bytes(wire), self.policy, self.consume_wire, self.consume, candidate, now=self.utc()
        )
        self.check()
        require(self.clock() < end and self.utc() < observation.claims["valid_until"])
        self.observation = observation
        return observation


def _reviewed_cleanup() -> CleanupOnce | None:
    # Root must deliver a distinct actually registered cleanup-purpose source.
    # Historical Provision GHA policy is not a teardown registration.
    return None


class InstalledCleanup:
    def __init__(self, callback):
        require(type(callback) is CleanupOnce)
        self.callback = callback
        self.old = None
        self.resolver = None
        self.entered = False

    def __enter__(self):
        global _reviewed_cleanup
        require(not self.entered and _reviewed_cleanup() is None)
        self.callback.check()
        self.old = _reviewed_cleanup
        self.resolver = lambda: self.callback
        _reviewed_cleanup = self.resolver
        self.entered = True
        return self.callback

    def __exit__(self, kind, error, tb):
        global _reviewed_cleanup
        if self.entered and _reviewed_cleanup is self.resolver:
            _reviewed_cleanup = cast("Callable[[], CleanupOnce | None]", self.old)
        elif error is None:
            raise CleanupUnknown("cleanup-registration-retirement-UNKNOWN")
        return False


def install_root_cleanup(**kwargs):
    """Root-only code install; no env flag, source/path override or auto mode."""
    try:
        return InstalledCleanup(CleanupOnce(**kwargs))
    except BaseException:
        raise CleanupUnknown("cleanup-registration-UNKNOWN") from None


def close_deployment_with_cleanup(client, dseq, *, expected_owner=None):
    """Preserve ordinary close when off; opted-in uncertainty is no-retry78."""
    try:
        callback = _reviewed_cleanup()
    except BaseException:
        raise CleanupUnknown("cleanup-registration-UNKNOWN") from None
    if callback is None:
        return client.close_deployment(dseq)
    try:
        require(type(callback) is CleanupOnce)
        callback.before_close(client, dseq, expected_owner)
        require(_reviewed_cleanup() is callback)
        result = client.close_deployment(dseq)
        callback.after_close(result)
        return result
    except BaseException:
        raise CleanupUnknown("legacy-capacity-cleanup-UNKNOWN-no-retry") from None
