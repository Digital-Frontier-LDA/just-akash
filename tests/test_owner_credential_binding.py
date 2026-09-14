"""Cleanup must not read an API outage as "no credential owns this lease" (#363).

just-akash#362 CI run 34818093597: the Console API was resetting connections, every
configured key's `account_address()` raised, the old selection read each failure as "not
the owner", and cleanup HELD paid lease 1789370984331 open.

The fix has two halves, each tested here against the REAL `robust_destroy`:

1. The receipt records which configured credential created the deployment
   (`credential_binding`, a position and a count, never key material). It is a HINT: the
   bound key is looked up first, with a longer budget. It is never destroy authority; only
   a lookup that returns the receipt owner selects a signer (ownership standard).
2. A key lookup has three outcomes, not two. A transport failure is UNKNOWN and retried
   within a bounded budget. Only a successful lookup returning a different address
   excludes a key. "Nothing matched because nothing could be read" is the typed
   OWNER_LOOKUP_UNREACHABLE, distinct from NO_CREDENTIAL_MATCHES_OWNER.

Only the network edges are stubbed: the Console client, the two-source group population
read, the post-close settlement audit, and the backoff clock.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from just_akash import _e2e, api, chain, paid_create, wallet_pool
from just_akash import deploy as deploy_module
from just_akash.api import AkashAPIError
from just_akash.deployment_receipt import (
    CredentialBinding,
    credential_binding_for,
    decode_receipt,
    mark_create_response_received,
    mark_submitting,
    prepare_receipt,
)

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
OTHER = "akash1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"
GROUPS = [{"gseq": 1, "name": "just-akash-e2e.abc123def456"}]
KEYS = ["console-key-alpha-0123456789abcdef", "console-key-bravo-fedcba9876543210"]


class _FakeConsole:
    """A Console client whose address lookup follows a per-key script."""

    scripts: dict[str, list] = {}
    lookups: dict[str, int] = {}
    closes: list[tuple[str, str]] = []
    sleeps: list[float] = []
    order: list[str] = []
    proofs: list[tuple[str, str, object]] = []

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def account_address(self) -> str:
        _FakeConsole.lookups[self.api_key] = _FakeConsole.lookups.get(self.api_key, 0) + 1
        _FakeConsole.order.append(self.api_key)
        script = _FakeConsole.scripts[self.api_key]
        outcome = script[min(_FakeConsole.lookups[self.api_key], len(script)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close_deployment(self, dseq: str) -> dict:
        _FakeConsole.closes.append((self.api_key, dseq))
        return {"closed": True}


@pytest.fixture
def console(monkeypatch):
    _FakeConsole.scripts, _FakeConsole.lookups, _FakeConsole.closes = {}, {}, []
    _FakeConsole.order, _FakeConsole.proofs = [], []
    sleeps: list[float] = []
    monkeypatch.setattr(api, "AkashConsoleAPI", _FakeConsole)
    monkeypatch.setattr(wallet_pool, "configured_api_keys", lambda: list(KEYS))

    def population(owner, dseq, groups):
        _FakeConsole.proofs.append(("population", str(dseq), (owner, groups)))
        return groups

    def settled(dseq, owner):
        _FakeConsole.proofs.append(("settled", str(dseq), owner))
        return True

    monkeypatch.setattr(chain, "corroborated_deployment_group_population", population)
    monkeypatch.setattr(_e2e, "_confirm_settled", settled)
    monkeypatch.setattr(_e2e.time, "sleep", sleeps.append)
    _FakeConsole.sleeps = sleeps
    return _FakeConsole


def _destroy(credential=None) -> bool:
    return _e2e.robust_destroy(
        "1001", owner=OWNER, groups=GROUPS, credential=credential, retries=0
    )


def _never_prints_keys(capsys) -> str:
    out = capsys.readouterr()
    text = out.out + out.err
    assert not any(key in text for key in KEYS), "cleanup output leaked key material"
    return text


# ── acceptance 1: a binding orders the lookups but is never destroy authority ─────────

BOUND = {"credential_index": 1, "credential_count": 2}
# The bound key's budget: 5 attempts, exponential backoff 2+4+8+16 = 30s in total.
BOUND_BACKOFFS = [2.0, 4.0, 8.0, 16.0]


def test_a_bound_key_that_stays_unreachable_holds_without_destroying(console, capsys) -> None:
    console.scripts = {key: [ConnectionResetError("reset by peer")] for key in KEYS}

    assert _destroy(BOUND) is False
    assert console.closes == [], "an unproven signer must never close anything"
    assert console.order[0] == KEYS[1], "the bound key is looked up first"
    assert console.lookups == {
        KEYS[1]: _e2e.BOUND_OWNER_LOOKUP_ATTEMPTS,
        KEYS[0]: _e2e.OWNER_LOOKUP_ATTEMPTS,
    }
    assert console.sleeps[: len(BOUND_BACKOFFS)] == BOUND_BACKOFFS
    assert sum(console.sleeps[: len(BOUND_BACKOFFS)]) == 30.0
    text = _never_prints_keys(capsys)
    assert _e2e.OWNER_LOOKUP_UNREACHABLE in text


def test_a_bound_key_that_recovers_within_its_budget_closes_the_exact_lease(
    console, capsys
) -> None:
    recovers_on = 4
    console.scripts = {
        KEYS[1]: [ConnectionResetError("reset")] * (recovers_on - 1) + [OWNER],
        KEYS[0]: [OTHER],
    }

    assert _destroy(BOUND) is True
    assert console.order == [KEYS[1]] * recovers_on, "matched on the bound key before any other"
    assert console.closes == [(KEYS[1], "1001")]
    assert ("population", "1001", (OWNER, GROUPS)) in console.proofs
    assert ("settled", "1001", OWNER) in console.proofs
    # The lookup backoffs come first; robust_destroy then waits 2s before its settlement audit.
    assert console.sleeps == [*BOUND_BACKOFFS[: recovers_on - 1], 2]
    _never_prints_keys(capsys)


# ── acceptance 2: no binding + an outage is typed UNREACHABLE after a bounded retry ────


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionResetError("reset by peer"),
        TimeoutError("timed out"),
        RuntimeError("Connection error: <urlopen error [Errno 54] Connection reset by peer>"),
        AkashAPIError("API Error (503): upstream", status=503),
        AkashAPIError("API Error (429): slow down", status=429),
    ],
    ids=["reset", "timeout", "urlerror", "http-503", "http-429"],
)
def test_an_unbound_outage_is_owner_lookup_unreachable_after_a_bounded_retry(
    console, capsys, failure
) -> None:
    console.scripts = {key: [failure] for key in KEYS}

    assert _destroy() is False
    assert console.closes == []
    assert console.lookups == {key: _e2e.OWNER_LOOKUP_ATTEMPTS for key in KEYS}
    assert _e2e.OWNER_LOOKUP_ATTEMPTS > 1, "a single attempt is not a retry budget"
    assert len(console.sleeps) == len(KEYS) * (_e2e.OWNER_LOOKUP_ATTEMPTS - 1)
    text = _never_prints_keys(capsys)
    assert _e2e.OWNER_LOOKUP_UNREACHABLE in text
    assert _e2e.NO_CREDENTIAL_MATCHES_OWNER not in text


def test_a_transient_failure_that_recovers_within_the_budget_still_matches(console) -> None:
    console.scripts = {KEYS[0]: [ConnectionResetError("reset"), OWNER], KEYS[1]: [OTHER]}

    assert _destroy() is True
    assert console.closes == [(KEYS[0], "1001")]
    assert console.lookups[KEYS[0]] == 2


# ── acceptance 3 (opposite leg): a successful different address still excludes ────────


def test_a_successful_lookup_of_a_different_address_excludes_that_key(console, capsys) -> None:
    console.scripts = {KEYS[0]: [OTHER], KEYS[1]: [OWNER]}

    assert _destroy() is True
    assert console.closes == [(KEYS[1], "1001")]
    assert console.lookups == {KEYS[0]: 1, KEYS[1]: 1}


def test_a_binding_whose_key_reads_a_different_address_is_not_trusted(console) -> None:
    console.scripts = {KEYS[0]: [OTHER], KEYS[1]: [OWNER]}

    assert _destroy({"credential_index": 0, "credential_count": 2}) is True
    assert console.closes == [(KEYS[1], "1001")], "a proven mismatch overrides the binding"


def test_when_every_key_reads_a_different_address_the_verdict_is_no_match(console, capsys) -> None:
    console.scripts = {key: [OTHER] for key in KEYS}

    assert _destroy() is False
    assert console.closes == []
    text = _never_prints_keys(capsys)
    assert _e2e.NO_CREDENTIAL_MATCHES_OWNER in text
    assert _e2e.OWNER_LOOKUP_UNREACHABLE not in text


def test_all_keys_unreadable_is_its_own_verdict_not_an_outage(console, capsys) -> None:
    """Every key 401s: no address was read, so neither "no match" nor "unreachable" is true."""
    console.scripts = {
        key: [AkashAPIError("API Error (401): bad key", status=401)] for key in KEYS
    }

    assert _destroy() is False
    assert console.lookups == {key: 1 for key in KEYS}, "a 401 is not retried"
    text = _never_prints_keys(capsys)
    assert _e2e.OWNER_LOOKUP_UNREADABLE in text
    assert _e2e.NO_CREDENTIAL_MATCHES_OWNER not in text
    assert _e2e.OWNER_LOOKUP_UNREACHABLE not in text


def test_one_unreadable_key_beside_a_proven_mismatch_is_no_match(console, capsys) -> None:
    console.scripts = {
        KEYS[0]: [AkashAPIError("API Error (403): forbidden", status=403)],
        KEYS[1]: [OTHER],
    }

    assert _destroy() is False
    assert _e2e.NO_CREDENTIAL_MATCHES_OWNER in _never_prints_keys(capsys)


def test_a_binding_for_a_different_configured_list_is_ignored(console) -> None:
    console.scripts = {key: [ConnectionResetError("reset")] for key in KEYS}

    assert _destroy({"credential_index": 1, "credential_count": 3}) is False
    assert console.closes == []


# ── the binding is written at create and carried to cleanup ────────────────────────────


SDL = """---
version: "2.0"
services:
  app:
    image: example.invalid/image:latest
    expose: []
profiles:
  compute:
    app:
      resources:
        cpu: {units: 1}
        memory: {size: 1Gi}
        storage: {size: 1Gi}
  placement:
    just-akash-e2e.abc123def456:
      pricing:
        app: {denom: uakt, amount: 1}
deployment:
  app:
    just-akash-e2e.abc123def456:
      profile: app
      count: 1
"""


def _private(tmp_path: Path) -> Path:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    return directory / "create.json"


def test_credential_binding_is_a_position_and_count_only() -> None:
    assert credential_binding_for(KEYS, KEYS[1]) == {"credential_index": 1, "credential_count": 2}
    assert credential_binding_for(KEYS, "not-configured") is None
    assert credential_binding_for(KEYS, None) is None


def test_the_binding_round_trips_through_every_receipt_state(tmp_path: Path) -> None:
    path = _private(tmp_path)
    binding: CredentialBinding = {"credential_index": 1, "credential_count": 2}
    submitting = mark_submitting(
        *prepare_receipt(
            str(path),
            operation_id="op-363",
            owner=OWNER,
            sdl_content=SDL,
            credential_binding=binding,
        )
    )
    assert dict(decode_receipt(path.read_bytes())).get("credential_binding") == binding
    mark_create_response_received(*submitting, dseq="1001", deployment_response={"dseq": "1001"})
    decoded = decode_receipt(path.read_bytes())
    assert dict(decoded).get("credential_binding") == binding
    assert not any(key in path.read_text() for key in KEYS)


def test_a_receipt_without_a_binding_still_decodes(tmp_path: Path) -> None:
    path = _private(tmp_path)
    prepare_receipt(str(path), operation_id="op-363", owner=OWNER, sdl_content=SDL)
    assert "credential_binding" not in decode_receipt(path.read_bytes())


@pytest.mark.parametrize(
    "binding",
    [
        {"credential_index": 2, "credential_count": 2},
        {"credential_index": -1, "credential_count": 2},
        {"credential_index": True, "credential_count": 2},
        {"credential_index": 0, "credential_count": 1, "key": "leak"},
    ],
    ids=["index-out-of-range", "negative", "bool", "extra-field"],
)
def test_a_malformed_binding_is_rejected_on_decode(tmp_path: Path, binding) -> None:
    path = _private(tmp_path)
    prepare_receipt(str(path), operation_id="op-363", owner=OWNER, sdl_content=SDL)
    document = json.loads(path.read_text())
    document["credential_binding"] = binding
    with pytest.raises(RuntimeError, match="credential binding is invalid"):
        decode_receipt(json.dumps(document).encode())


def test_paid_create_cleanup_forwards_the_receipt_binding(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(
        paid_create, "robust_destroy", lambda dseq, **kwargs: calls.append((dseq, kwargs)) or True
    )
    binding = {"credential_index": 1, "credential_count": 2}
    ref = {"dseq": "1001", "owner": OWNER, "groups": GROUPS, "credential": binding}
    assert paid_create.verified_cleanup(ref) is True
    assert calls == [("1001", {"owner": OWNER, "groups": GROUPS, "credential": binding})]


@patch("just_akash.deploy.AkashConsoleAPI")
def test_deploy_writes_the_creating_credential_into_the_receipt(
    mock_api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AKASH_API_KEY", KEYS[0])
    monkeypatch.delenv("AKASH_API_KEYS", raising=False)
    monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
    monkeypatch.setattr(deploy_module, "_check_wallet_credit", lambda *_args, **_kwargs: None)
    sdl_path = tmp_path / "sdl.yaml"
    sdl_path.write_text(SDL)
    receipt = _private(tmp_path)
    client = mock_api.return_value
    client.api_key = KEYS[0]
    client.account_address.return_value = OWNER

    def stop_at_create(*_args, **_kwargs):
        raise RuntimeError("create stopped")

    client.create_deployment.side_effect = stop_at_create
    with pytest.raises(RuntimeError, match="Failed to create deployment"):
        deploy_module.deploy(
            sdl_path=str(sdl_path), receipt_path=str(receipt), receipt_operation_id="op-363"
        )
    durable = json.loads(receipt.read_text())
    assert durable["credential_binding"] == {"credential_index": 0, "credential_count": 1}
    assert KEYS[0] not in receipt.read_text()


# ── R2: the REAL Console client's raise surface, with only urlopen patched ─────────────


class _Response:
    def __init__(self, body: bytes, *, truncated: bool = False) -> None:
        self._body, self._truncated, self.status = body, truncated, 200

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def read(self) -> bytes:
        if self._truncated:
            import http.client

            raise http.client.IncompleteRead(self._body[:5], len(self._body))
        return self._body


def _jwt_for(address: str) -> bytes:
    import base64

    claims = base64.urlsafe_b64encode(json.dumps({"iss": address}).encode()).decode().rstrip("=")
    return json.dumps({"data": {"token": f"header.{claims}.signature"}}).encode()


@pytest.fixture
def real_console(monkeypatch):
    """The real AkashConsoleAPI; urlopen answers JWT mints from a script, closes with {}."""
    import io
    import urllib.error
    from email.message import Message

    state: dict = {"mints": [], "closes": [], "sleeps": []}
    monkeypatch.setattr(wallet_pool, "configured_api_keys", lambda: [KEYS[0]])
    monkeypatch.setattr(
        chain, "corroborated_deployment_group_population", lambda owner, dseq, groups: groups
    )
    monkeypatch.setattr(_e2e, "_confirm_settled", lambda dseq, owner: True)
    monkeypatch.setattr(_e2e.time, "sleep", state["sleeps"].append)

    def urlopen(request, *args, **kwargs):
        url = request.full_url
        if url.endswith("/v1/create-jwt-token"):
            outcome = state["script"][min(len(state["mints"]), len(state["script"]) - 1)]
            state["mints"].append(outcome)
            if outcome == "truncated":
                return _Response(_jwt_for(OWNER), truncated=True)
            if outcome == "408":
                raise urllib.error.HTTPError(
                    url, 408, "Request Timeout", Message(), io.BytesIO(b'{"message":"timeout"}')
                )
            return _Response(_jwt_for(outcome))
        state["closes"].append((request.get_method(), url.rsplit("/", 1)[-1]))
        return _Response(b"{}")

    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    return state


@pytest.mark.parametrize("failure", ["truncated", "408"], ids=["incomplete-read", "http-408"])
def test_a_real_client_transport_failure_is_retried_then_matches(real_console, failure) -> None:
    real_console["script"] = [failure, OWNER]

    assert _destroy() is True
    assert real_console["mints"] == [failure, OWNER], "the first failure must be retried"
    assert real_console["closes"] == [("DELETE", "1001")]


@pytest.mark.parametrize("failure", ["truncated", "408"], ids=["incomplete-read", "http-408"])
def test_a_real_client_transport_failure_that_persists_is_unreachable(
    real_console, capsys, failure
) -> None:
    real_console["script"] = [failure]

    assert _destroy() is False
    assert len(real_console["mints"]) == _e2e.OWNER_LOOKUP_ATTEMPTS
    assert real_console["closes"] == []
    assert _e2e.OWNER_LOOKUP_UNREACHABLE in _never_prints_keys(capsys)


# ── R1: every receipt-identity consumer forwards the binding (derived, not listed) ─────


def _receipt_identity_sites():
    """Every place in just_akash that copies a receipt's group population into cleanup
    identity, and every robust_destroy call that passes a complete group population."""
    import ast

    package = Path(__file__).resolve().parents[1] / "just_akash"
    updates, destroys = [], []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            where = f"{path.name}:{node.lineno}"
            groups = keywords.get("groups")
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "update"
                and groups is not None
                and ast.unparse(groups) == "receipt['group_population']"
            ):
                updates.append((where, keywords.get("credential")))
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name == "robust_destroy" and groups is not None:
                destroys.append((where, keywords.get("credential")))
    return updates, destroys


def test_every_receipt_identity_site_forwards_the_credential_binding() -> None:
    import ast

    updates, destroys = _receipt_identity_sites()
    # Measured 2026-09-14: 9 identity copies (paid_create 1, smoke_providers 2, test_lifecycle 2,
    # test_secrets_e2e 2, test_shell_e2e 2) and 3 group-population destroys (paid_create,
    # test_secrets_e2e, the _e2e signal handler). A floor, so a blind walk cannot pass.
    assert len(updates) >= 9, updates
    assert len(destroys) >= 3, destroys
    missing_updates = [
        where
        for where, value in updates
        if value is None or ast.unparse(value) != "receipt.get('credential_binding')"
    ]
    assert not missing_updates, (
        f"receipt identity copied without its credential binding: {missing_updates}"
    )
    missing_destroys = [where for where, value in destroys if value is None]
    assert not missing_destroys, (
        f"robust_destroy called without the credential: {missing_destroys}"
    )
