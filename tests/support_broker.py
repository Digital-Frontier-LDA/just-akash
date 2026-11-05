import base64
import json

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REAL_HTTPX_CLIENT = httpx.Client
REAL_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


class Clock:
    value = 100.0
    utc = 1800000001

    def mono(self):
        return self.value

    def wall(self):
        return self.utc


class Stream(httpx.SyncByteStream):
    def __init__(self, raw, world):
        self.raw, self.world = (raw, world)

    def __iter__(self):
        yield self.raw

    def close(self):
        self.world.closes += 1
        if self.world.close_late:
            self.world.clock.value = 1000.0
        if self.world.close_error:
            raise OSError("SECRET_CLOSE_SENTINEL")


class Broker:
    def __init__(self, m, clock):
        self.m, self.clock = (m, clock)
        self.key = Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32)
        self.policy = m.ReceiptPolicy(
            "9" * 40, "closed-broker", "closed-key", self.key.public_key().public_bytes_raw(), 60
        )
        self.requests, self.phases = ([], [])
        self.previous = None
        self.attempt = 0
        self.closes = 0
        self.mutate = lambda c: None
        self.status = 200
        self.response_headers = {}
        self.raw_override = None
        self.fail_phase = None
        self.late_phase = None
        self.close_late = False
        self.close_error = False
        self.options = []
        self.mint_requests = []
        self.mint_closes = 0
        self.mint_fail = False
        self.delegation_token = None

    def mint_client(self, **kwargs):

        class MintStream(httpx.AsyncByteStream):
            def __init__(self, raw, world):
                self.raw, self.world = (raw, world)

            async def __aiter__(self):
                yield self.raw

            async def aclose(self):
                self.world.mint_closes += 1

        def segment(value):
            return (
                base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
                .decode()
                .rstrip("=")
            )

        def handler(request):
            assert str(request.url) == "https://console-api.akash.network/v1/create-jwt-token"
            assert request.method == "POST" and request.headers["x-api-key"] == "RAM_ONLY_TOKEN"
            assert "authorization" not in request.headers
            assert json.loads(request.content) == {
                "data": {"ttl": 30, "leases": {"access": "scoped", "scope": ["status"]}}
            }
            self.mint_requests.append(request)
            if self.mint_fail:
                raise httpx.ReadError("SECRET_MINT_ERROR")
            now = self.clock.utc
            claims = dict(
                iss="closed-owner",
                iat=now,
                nbf=now,
                exp=now + 30,
                version="v1",
                leases={"access": "scoped", "scope": ["status"]},
            )
            self.delegation_token = (
                segment({"alg": "ES256KADR36", "typ": "JWT"})
                + "."
                + segment(claims)
                + "."
                + base64.urlsafe_b64encode(b"s" * 64).decode().rstrip("=")
            )
            raw = json.dumps({"data": {"token": self.delegation_token}}).encode()
            return httpx.Response(
                200,
                headers={"content-type": "application/json", "content-length": str(len(raw))},
                stream=MintStream(raw, self),
            )

        return REAL_HTTPX_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    def client(self, **kwargs):
        self.options.append(kwargs)
        return REAL_HTTPX_CLIENT(transport=httpx.MockTransport(self.handle), **kwargs)

    def handle(self, request):
        assert request.method == "POST"
        assert request.headers["authorization"] == "Bearer PUBLIC_SYNTHETIC_OIDC"
        assert request.headers["x-github-actions-read-token"] == "PUBLIC_SYNTHETIC_READ"
        assert "x-akash-console-key" not in request.headers
        assert "x-api-key" not in request.headers
        assert request.headers["x-akash-owner-status-delegation"] == self.delegation_token
        assert request.headers["accept-encoding"] == "identity"
        assert request.url.host == "sentinel.digitalfrontier.so"
        assert request.url.scheme == "https"
        assert not request.url.query
        phase = request.url.path.rsplit("/", 1)[-1]
        assert request.url.path == self.m.PATHS[phase]
        self.phases.append(phase)
        payload = json.loads(request.content)
        self.requests.append(payload)
        if phase == self.fail_phase:
            raise httpx.ReadError("SECRET_HTTP_ERROR_SENTINEL")
        if phase == self.late_phase:
            self.clock.value = 1000.0
        if phase == "reserve":
            self.attempt += 1
        parent = "a" * 64
        c = dict(
            schema=self.m.SCHEMA,
            audience="legacy-lease-boundary-observation",
            broker_identity=self.policy.broker_identity,
            key_id=self.policy.key_id,
            policy_revision=self.policy.revision,
            phase=phase,
            parent_digest=parent,
            parent_revision=1 if phase == "reserve" else 2,
            parent_revision_digest="b" * 64,
            provider_revision=10 + (phase != "reserve"),
            provider_digest="c" * 64,
            operation_id=payload["operation_id"],
            attempt=self.attempt,
            attempt_id=parent + "." + str(self.attempt),
            binding_digest="d" * 64,
            capacity_evidence_digest="e" * 64,
            placement_state="reserved" if phase == "reserve" else "unknown",
            predecessor_sha256=None if phase == "reserve" else self.m.sha(self.previous),
            identity=payload["identity"],
            original_deadline_at=1800000900,
            observed_at=self.clock.utc,
            valid_until=self.clock.utc + 30,
        )
        self.mutate(c)
        body = self.m.canonical(c)
        wire = self.m.canonical(
            dict(
                payload=base64.b64encode(body).decode(),
                signature=base64.b64encode(self.key.sign(self.m.DOMAIN + body)).decode(),
            )
        )
        self.previous = wire
        if self.raw_override is not None:
            wire = self.raw_override
        headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Content-Length": str(len(wire)),
        }
        headers.update(self.response_headers)
        return httpx.Response(self.status, headers=headers, stream=Stream(wire, self))
