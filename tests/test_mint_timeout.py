"""Real async cancellation/retirement of the installed Python3.10-compatible context."""

import asyncio
import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from just_akash._hosted import mint_owner_status_delegation as mint

REAL_CLIENT = httpx.AsyncClient


def _segment(value):
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _token():
    now = int(time.time())
    claims = {
        "iss": "public-synthetic-owner",
        "iat": now,
        "nbf": now,
        "exp": now + 30,
        "version": "v1",
        "leases": {"access": "scoped", "scope": ["status"]},
    }
    return ".".join(
        (_segment({"alg": "ES256KADR36", "typ": "JWT"}), _segment(claims), _segment(b"s" * 64))
    )


@pytest.fixture
def world(monkeypatch):
    w = SimpleNamespace(
        requests=[], closes=[], clients=[], mode="success", entered=asyncio.Event()
    )

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            w.entered.set()
            if w.mode in {"timeout", "cancel"}:
                await asyncio.sleep(10)
            yield json.dumps({"data": {"token": _token()}}).encode()

        async def aclose(self):
            w.closes.append("response")

    def handle(request):
        w.requests.append(request)
        assert str(request.url) == mint.URL and request.method == "POST"
        assert request.headers["x-api-key"] == "PUBLIC_SYNTHETIC_SELECTED_KEY"
        return httpx.Response(200, stream=Stream(), headers={"Content-Type": "application/json"})

    class Client(REAL_CLIENT):
        async def __aexit__(self, *args):
            value = await super().__aexit__(*args)
            w.closes.append("client")
            if w.mode == "late_client":
                await asyncio.sleep(10)
            return value

    def factory(**kwargs):
        assert kwargs["verify"] and not kwargs["trust_env"] and not kwargs["follow_redirects"]
        c = Client(transport=httpx.MockTransport(handle), **kwargs)
        w.clients.append(c)
        return c

    monkeypatch.setattr(mint.httpx, "AsyncClient", factory)
    return w


def test_shorter_original_end_success_closes_every_owned_context(world):
    async def exercise():
        result = await mint.mint_owner_status_delegation(
            "PUBLIC_SYNTHETIC_SELECTED_KEY", end=time.monotonic() + 1
        )
        assert result.expires_at > time.time()
        assert len(world.requests) == 1 and world.closes == ["response", "client"]
        assert all(c.is_closed for c in world.clients)

    asyncio.run(exercise())


def test_real_fail_after_deadline_cancels_stream_and_retires_once(world):
    async def exercise():
        world.mode = "timeout"
        with pytest.raises(mint.DelegationMintHeld):
            await mint.mint_owner_status_delegation(
                "PUBLIC_SYNTHETIC_SELECTED_KEY", end=time.monotonic() + 0.03
            )
        assert len(world.requests) == 1 and world.closes == ["response", "client"]
        assert all(c.is_closed for c in world.clients)

    asyncio.run(exercise())


def test_external_cancel_propagates_after_real_context_retirement(world):
    async def exercise():
        world.mode = "cancel"
        task = asyncio.create_task(
            mint.mint_owner_status_delegation(
                "PUBLIC_SYNTHETIC_SELECTED_KEY", end=time.monotonic() + 1
            )
        )
        await world.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.done() and task.cancelled()
        assert len(world.requests) == 1 and world.closes == ["response", "client"]
        assert all(c.is_closed for c in world.clients)

    asyncio.run(exercise())


def test_client_retirement_crossing_original_end_never_returns_a_token(world):
    async def exercise():
        world.mode = "late_client"
        with pytest.raises(mint.DelegationMintHeld):
            await mint.mint_owner_status_delegation(
                "PUBLIC_SYNTHETIC_SELECTED_KEY", end=time.monotonic() + 0.03
            )
        assert len(world.requests) == 1 and world.closes == ["response", "client"]
        assert all(c.is_closed for c in world.clients)

    asyncio.run(exercise())
