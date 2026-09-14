"""Production teardown must not read a Console outage as "no credential owns this lease" (#367).

Sibling of #363/#366, on the path `runner-teardown.yml` actually runs: `just-akash destroy
--expected-owner` → `wallet_pool._raw_client_for_bound_owner`, and the legacy `--dseq` path →
`wallet_pool.select_client_for_dseq`. Both did `except RuntimeError: continue` per credential.
The real AkashConsoleAPI wraps connect-phase failures as `RuntimeError("Connection error: …")`,
so during an API outage every credential was skipped as if it did not own the lease.

⛔ Driven through the REAL AkashConsoleAPI: only `urllib.request.urlopen` and the backoff clock are
patched, so the exceptions are the ones the client actually raises, not ones a fake invents.
"""

from __future__ import annotations

import base64
import io
import json
import time
import urllib.error
from email.message import Message

import pytest

from just_akash import api, chain, owner_lookup, wallet_pool

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
OTHER = "akash1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"
GROUP = "just-akash-runner.abc123"
DSEQ = "1789370984331"
KEYS = ["console-key-alpha-0123456789abcdef", "console-key-bravo-fedcba9876543210"]


class _Response:
    def __init__(self, body: dict) -> None:
        self._body, self.status = json.dumps(body).encode(), 200

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _jwt(address: str) -> dict:
    claims = base64.urlsafe_b64encode(json.dumps({"iss": address}).encode()).decode().rstrip("=")
    return {"data": {"token": f"header.{claims}.signature"}}


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "err", Message(), io.BytesIO(b'{"message":"x"}'))


RESET = "reset"


@pytest.fixture
def console(monkeypatch):
    """Per-key scripts for JWT mints (owner lookup) and deployment reads, answered at urlopen."""
    state: dict = {"mint": {}, "read": {}, "calls": [], "sleeps": []}
    monkeypatch.setattr(wallet_pool, "configured_api_keys", lambda: list(KEYS))
    monkeypatch.setattr(time, "sleep", state["sleeps"].append)

    def outcome(kind: str, key: str):
        script = state[kind][key]
        n = sum(1 for c in state["calls"] if c == (kind, key))
        return script[min(n, len(script)) - 1] if script else RESET

    def urlopen(request, *args, **kwargs):
        key = request.headers.get("X-api-key") or request.get_header("X-api-key")
        url = request.full_url
        kind = "mint" if url.endswith("/v1/create-jwt-token") else "read"
        state["calls"].append((kind, key))
        result = outcome(kind, key)
        if result == RESET:
            raise urllib.error.URLError(ConnectionResetError(54, "Connection reset by peer"))
        if isinstance(result, int):
            raise _http_error(url, result)
        return _Response(result)

    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    return state


def _no_chain(monkeypatch, names=None):
    calls = []

    def corroborated(owner, dseq, group):
        calls.append((owner, dseq, group))
        if names is None:
            raise AssertionError("chain corroboration must not run without a proven owner match")
        return names

    monkeypatch.setattr(chain, "corroborated_deployment_group_names", corroborated)
    return calls


def _mints(state, key):
    return sum(1 for c in state["calls"] if c == ("mint", key))


# ── --expected-owner: the runner-teardown path ─────────────────────────────


def test_an_outage_on_every_credential_is_owner_lookup_unreachable(console, monkeypatch) -> None:
    console["mint"] = {key: [RESET] for key in KEYS}
    _no_chain(monkeypatch)

    with pytest.raises(RuntimeError) as raised:
        wallet_pool.select_client_for_bound_owner(DSEQ, OWNER, GROUP)

    message = str(raised.value)
    assert message.startswith(owner_lookup.OWNER_LOOKUP_UNREACHABLE), message
    assert "was not reported" not in message
    assert getattr(raised.value, "verdict", None) == owner_lookup.OWNER_LOOKUP_UNREACHABLE
    assert all(_mints(console, key) == owner_lookup.OWNER_LOOKUP_ATTEMPTS for key in KEYS)


def test_a_transient_reset_is_retried_and_then_selects_the_owner(console, monkeypatch) -> None:
    console["mint"] = {KEYS[0]: [RESET, _jwt(OWNER)], KEYS[1]: [_jwt(OTHER)]}
    _no_chain(monkeypatch, names=[GROUP])

    client = wallet_pool._raw_client_for_bound_owner(DSEQ, OWNER, GROUP)

    assert client.api_key == KEYS[0]
    assert _mints(console, KEYS[0]) == 2


def test_a_different_address_excludes_a_key_and_the_matching_key_is_selected(
    console, monkeypatch
) -> None:
    console["mint"] = {KEYS[0]: [_jwt(OTHER)], KEYS[1]: [_jwt(OWNER)]}
    calls = _no_chain(monkeypatch, names=[GROUP])

    client = wallet_pool._raw_client_for_bound_owner(DSEQ, OWNER, GROUP)

    assert client.api_key == KEYS[1]
    assert calls == [(OWNER, DSEQ, GROUP)]
    assert _mints(console, KEYS[0]) == 1, "a proven mismatch is not retried"


def test_a_proven_mismatch_on_every_key_keeps_its_own_verdict(console, monkeypatch) -> None:
    console["mint"] = {key: [_jwt(OTHER)] for key in KEYS}
    _no_chain(monkeypatch)

    with pytest.raises(RuntimeError) as raised:
        wallet_pool.select_client_for_bound_owner(DSEQ, OWNER, GROUP)

    assert str(raised.value).startswith(owner_lookup.NO_CREDENTIAL_MATCHES_OWNER)
    assert "was not reported by any configured Console credential" in str(raised.value)


def test_an_outage_beside_a_mismatch_is_still_unreachable(console, monkeypatch) -> None:
    console["mint"] = {KEYS[0]: [_jwt(OTHER)], KEYS[1]: [RESET]}
    _no_chain(monkeypatch)

    with pytest.raises(RuntimeError) as raised:
        wallet_pool.select_client_for_bound_owner(DSEQ, OWNER, GROUP)

    assert str(raised.value).startswith(owner_lookup.OWNER_LOOKUP_UNREACHABLE)


def test_an_authorisation_failure_is_not_retried_and_is_not_an_outage(
    console, monkeypatch
) -> None:
    console["mint"] = {key: [401] for key in KEYS}
    _no_chain(monkeypatch)

    with pytest.raises(RuntimeError) as raised:
        wallet_pool.select_client_for_bound_owner(DSEQ, OWNER, GROUP)

    assert str(raised.value).startswith(owner_lookup.OWNER_LOOKUP_UNREADABLE)
    assert all(_mints(console, key) == 1 for key in KEYS)


def test_the_evidence_gated_closer_uses_the_same_selection(console, monkeypatch) -> None:
    console["mint"] = {key: [RESET] for key in KEYS}
    _no_chain(monkeypatch)
    monkeypatch.setattr(
        chain, "owner_close_evidence", lambda *a: pytest.fail("no evidence without an owner match")
    )

    with pytest.raises(RuntimeError, match=owner_lookup.OWNER_LOOKUP_UNREACHABLE):
        wallet_pool.authorize_client_for_bound_owner(DSEQ, OWNER, GROUP)


# ── --dseq: the legacy read-back path ──────────────────────────────────────


def _deployment() -> dict:
    return {"data": {"deployment": {"id": {"dseq": DSEQ}}}}


def test_a_dseq_read_outage_on_every_wallet_is_owner_lookup_unreachable(console) -> None:
    console["read"] = {key: [RESET] for key in KEYS}

    with pytest.raises(RuntimeError) as raised:
        wallet_pool.select_client_for_dseq(DSEQ)

    message = str(raised.value)
    assert message.startswith(owner_lookup.OWNER_LOOKUP_UNREACHABLE), message
    assert "was not readable under any" not in message
    assert all(
        sum(1 for c in console["calls"] if c == ("read", key))
        == owner_lookup.OWNER_LOOKUP_ATTEMPTS
        for key in KEYS
    )


def test_a_dseq_another_wallet_cannot_read_is_skipped_and_the_reader_selected(console) -> None:
    console["read"] = {KEYS[0]: [404], KEYS[1]: [_deployment()]}

    client = wallet_pool.select_client_for_dseq(DSEQ)

    assert client.api_key == KEYS[1]
    assert sum(1 for c in console["calls"] if c == ("read", KEYS[0])) == 1


def test_a_dseq_no_wallet_can_read_keeps_its_own_message(console) -> None:
    console["read"] = {key: [404] for key in KEYS}

    with pytest.raises(
        RuntimeError, match="was not readable under any of 2 configured Console wallets"
    ):
        wallet_pool.select_client_for_dseq(DSEQ)
