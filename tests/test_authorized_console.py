"""Actual opener effects under publicly confirmed create/redemption authority."""

from __future__ import annotations

import base64
import inspect
import json
import logging
import textwrap
import threading
import traceback
import types
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from akash_lease_core import (
    EMPTY_RESERVATION_POPULATION_DIGEST,
    AdmissionRequest,
    AdmissionScope,
    AdmissionState,
    AuthenticatedBrokerEvidence,
    CensusStatus,
    ContainmentCensus,
    ContainmentLimits,
    CreateExposureEvidence,
    LifecycleIdentity,
    OwnerBudgetScope,
    OwnerCandidate,
    OwnerEvidenceKind,
    PermitPresentation,
    PermitRevocationStatus,
    PersistenceConfirmation,
    PreparedCreate,
    PreparedGroup,
    ProducerProvenance,
    ScopeBudget,
    canonical_group_population_digest,
    canonical_journal_digest,
    canonical_payload_digest,
    confirm_persisted_reservation,
    confirm_redeemed_permit,
    redeem_create_permit,
    reserve_capacity,
)

from just_akash.authorized_console import (
    CONSOLE_BACKEND,
    AuthorizedConsoleCreate,
    CreateHeld,
    CreateUnknown,
    create_body,
    create_runtime_body,
)

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
NOW = 1_800_000_000
REVISION = "a" * 40
BROKER = "trusted-ci-controller"
SDL = json.dumps(
    {
        "version": "2.0",
        "services": {"runner": {"image": "example.invalid/runner"}},
        "profiles": {"placement": {"ci-slot": {"pricing": {"runner": {"amount": 1}}}}},
        "deployment": {"runner": {"ci-slot": {"profile": "runner", "count": 1}}},
    }
)


def authority(*, prepared_change=None, request_body=None):
    """Use public reserve/confirm/redeem/confirm APIs; never private issuance."""
    producer = ProducerProvenance(
        "example/repo",
        "123",
        "456",
        "https://token.actions.githubusercontent.com",
        "akash-create",
        "repo:example/repo:ref:refs/heads/main",
        "example/repo/.github/workflows/ci.yml@refs/heads/main",
        REVISION,
        "refs/heads/main",
        "synthetic-jti",
        "1" * 64,
    )
    groups = (PreparedGroup(1, "ci-slot"),)
    prepared = PreparedCreate(
        "ci-create:42:1:1",
        1,
        producer,
        LifecycleIdentity("example/repo", "ci-runner", run=42, run_attempt=1),
        OwnerCandidate(
            CONSOLE_BACKEND,
            OWNER,
            OwnerEvidenceKind.AUTHENTICATED_MEDIATOR,
            "authenticated Console account",
            "2" * 64,
        ),
        canonical_payload_digest(create_body(SDL, 0.5) if request_body is None else request_body),
        canonical_payload_digest(SDL.encode()),
        groups,
        canonical_group_population_digest(groups),
        REVISION,
        NOW,
    )
    if prepared_change:
        prepared = prepared_change(prepared)
    scope = AdmissionScope(
        "akashnet-2", OWNER, producer.issuer, "456", "123", "example/repo", "ci-runner"
    )
    # Other purposes occupy the shared wallet. They are accounted for, not
    # excluded from the owner or treated as CI cleanup candidates.
    census = ContainmentCensus(
        3,
        0,
        100,
        0,
        EMPTY_RESERVATION_POPULATION_DIGEST,
        CensusStatus.VERIFIED,
        CensusStatus.VERIFIED,
        CensusStatus.VERIFIED,
        CensusStatus.VERIFIED,
        NOW - 1,
        NOW + 30,
        "3" * 64,
    )
    limits = ContainmentLimits(10, 1, 1_000_000, REVISION, REVISION, "test policy", NOW + 30)
    state = AdmissionState(
        (
            ScopeBudget(OwnerBudgetScope("akashnet-2", OWNER), limits, census),
            ScopeBudget(
                scope,
                replace(limits, max_active_deployments=1),
                replace(census, active_deployments=0, financial_exposure_uact=0),
            ),
        )
    )
    exposure = CreateExposureEvidence(
        "akashnet-2",
        prepared.operation_id,
        canonical_journal_digest(prepared),
        prepared.request_digest,
        prepared.sdl_digest,
        prepared.owner_candidate.backend,
        REVISION,
        500_000,
        "test policy",
        "4" * 64,
        NOW,
        NOW + 30,
    )
    request = AdmissionRequest(prepared, scope, exposure, NOW)
    proposal = reserve_capacity(state, request, expected_revision=0, now=NOW).proposal
    assert proposal is not None
    broker = AuthenticatedBrokerEvidence(
        BROKER, "test", "test", BROKER, REVISION, "5" * 64, NOW, NOW + 30
    )

    def confirmation(proposed):
        return PersistenceConfirmation(
            prepared.operation_id,
            proposed.revision,
            canonical_journal_digest(proposed),
            broker,
            "6" * 64,
            NOW,
        )

    reserved = proposal.proposed_state
    permit = confirm_persisted_reservation(
        proposal, state, request, reserved, confirmation(reserved)
    )
    presentation = PermitPresentation(
        canonical_journal_digest(permit),
        permit.presenter_identity,
        permit.audience,
        permit.policy_bindings,
        PermitRevocationStatus.ACTIVE,
        "7" * 64,
        NOW,
    )
    redemption = redeem_create_permit(
        reserved, permit, presentation, expected_revision=reserved.revision
    )
    submitted = redemption.proposed_state
    authorization = confirm_redeemed_permit(
        redemption, permit, presentation, reserved, submitted, confirmation(submitted)
    )
    return {
        "request": request,
        "permit": permit,
        "authorization": authorization,
        "sdl_content": SDL,
        "deposit": 0.5,
    }


def runtime_authority(*, runtime_limit_hours=2, prepared_change=None):
    args = authority(
        prepared_change=prepared_change,
        request_body=create_runtime_body(SDL, runtime_limit_hours),
    )
    args.pop("deposit")
    return {**args, "runtime_limit_hours": runtime_limit_hours}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("just_akash.authorized_console.time.time", lambda: NOW)
    client = AuthorizedConsoleCreate(
        "synthetic-api-key", owner=OWNER, policy_revision=REVISION, broker=BROKER
    )
    monkeypatch.setattr(client, "account_address", lambda: OWNER)
    return client


def transport(monkeypatch, *, failure=None, response=None):
    output = MagicMock()
    output.status = 200
    output.read.return_value = json.dumps({"data": response or {"dseq": "123"}}).encode()
    output.__enter__.return_value = output
    opener = MagicMock(return_value=output, side_effect=failure)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", opener)
    return opener


def test_shared_account_authority_reaches_exact_post_once(client, monkeypatch):
    opener = transport(monkeypatch)
    args = authority()
    assert client.submit(**args) == {"dseq": "123"}
    opener.assert_called_once()
    request = opener.call_args.args[0]
    assert request.full_url == "https://console-api.akash.network/v1/deployments"
    assert request.data == create_body(SDL, 0.5)
    with pytest.raises(CreateHeld, match="consumed"):
        client.submit(**args)
    assert opener.call_count == 1


@pytest.mark.parametrize(
    "field,value",
    [("sdl_content", SDL + " "), ("deposit", 0.75), ("authorization", {}), ("permit", {})],
)
def test_changed_request_or_wire_authority_never_sends(client, monkeypatch, field, value):
    opener = transport(monkeypatch)
    args = authority()
    args[field] = value
    with pytest.raises(CreateHeld):
        client.submit(**args)
    opener.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        lambda p: replace(
            p,
            owner_candidate=replace(
                p.owner_candidate, backend=replace(CONSOLE_BACKEND, identifier="foreign-console")
            ),
        ),
        lambda p: replace(
            p,
            groups=(PreparedGroup(1, "foreign-slot"),),
            group_population_digest=canonical_group_population_digest(
                (PreparedGroup(1, "foreign-slot"),)
            ),
        ),
    ],
)
@pytest.mark.parametrize("runtime_method", [False, True])
def test_signed_foreign_backend_or_group_never_sends(client, monkeypatch, change, runtime_method):
    opener = transport(monkeypatch)
    method = client.submit_runtime_limit if runtime_method else client.submit
    args = (
        runtime_authority(prepared_change=change)
        if runtime_method
        else authority(prepared_change=change)
    )
    with pytest.raises(CreateHeld):
        method(**args)
    opener.assert_not_called()


def test_owner_read_delay_cannot_outlive_authority_at_actual_opener(client, monkeypatch):
    opener = transport(monkeypatch)
    args = authority()

    def delayed_owner():
        monkeypatch.setattr("just_akash.authorized_console.time.time", lambda: NOW + 6)
        return OWNER

    monkeypatch.setattr(client, "account_address", delayed_owner)
    with pytest.raises(CreateHeld):
        client.submit(**args)
    opener.assert_not_called()


@pytest.mark.parametrize("runtime_method", [False, True])
def test_owner_credential_mismatch_never_sends(client, monkeypatch, runtime_method):
    opener = transport(monkeypatch)
    monkeypatch.setattr(client, "account_address", lambda: "foreign-owner")
    with pytest.raises(CreateHeld, match="credential owner"):
        if runtime_method:
            client.submit_runtime_limit(**runtime_authority())
        else:
            client.submit(**authority())
    opener.assert_not_called()


def test_timeout_is_unknown_never_restores_create_slot_or_echoes_failure(client, monkeypatch):
    opener = transport(monkeypatch, failure=TimeoutError("synthetic-secret-echo"))
    args = authority()
    with pytest.raises(CreateUnknown) as caught:
        client.submit(**args)
    assert "synthetic-secret-echo" not in str(caught.value)
    with pytest.raises(CreateHeld):
        client.submit(**args)
    assert opener.call_count == 1


def test_invalid_success_response_is_unknown_without_repost(client, monkeypatch):
    opener = transport(monkeypatch, response={"dseq": "01"})
    args = authority()
    with pytest.raises(CreateUnknown):
        client.submit(**args)
    with pytest.raises(CreateHeld):
        client.submit(**args)
    assert opener.call_count == 1


def test_inherited_unguarded_create_is_refused(client, monkeypatch):
    opener = transport(monkeypatch)
    with pytest.raises(CreateHeld, match="durably redeemed"):
        client.create_deployment(SDL)
    opener.assert_not_called()


@pytest.mark.parametrize("failure", ["owner-read", "malformed-sdl"])
def test_preflight_failure_is_held_and_removes_transport_authority(client, monkeypatch, failure):
    opener = transport(monkeypatch)
    args = authority()
    if failure == "owner-read":

        def unavailable_owner():
            raise TimeoutError("synthetic-secret-echo")

        monkeypatch.setattr(client, "account_address", unavailable_owner)
    else:
        args["sdl_content"] = "{}"
    with pytest.raises(CreateHeld) as caught:
        client.submit(**args)
    assert "synthetic-secret-echo" not in str(caught.value)
    # An abandoned binding must not license a later POST at the lower boundary.
    with pytest.raises(CreateHeld, match="No bound"):
        client._open_request(
            urllib.request.Request(
                "https://console-api.akash.network/v1/deployments",
                data=create_body(SDL, 0.5),
                method="POST",
            )
        )
    opener.assert_not_called()


def test_concurrent_submit_cannot_cross_the_same_pending_opener(client, monkeypatch):
    opener = transport(monkeypatch)
    args = authority()
    entered, release = threading.Event(), threading.Event()
    validate = client._validate
    calls = 0

    def pending_opener(body):
        nonlocal calls
        calls += 1
        validate(body)
        if calls == 2:
            entered.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr(client, "_validate", pending_opener)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.submit, **args)
        try:
            assert entered.wait(timeout=5)
            with pytest.raises(CreateHeld):
                client.submit(**args)
        finally:
            release.set()
        assert first.result(timeout=5) == {"dseq": "123"}
    opener.assert_called_once()


@pytest.mark.parametrize("runtime_method", [False, True])
def test_real_owner_probe_uses_only_status_scope_before_the_exact_create(
    client, monkeypatch, runtime_method
):
    monkeypatch.delattr(client, "account_address")
    claim = base64.urlsafe_b64encode(json.dumps({"iss": OWNER}).encode()).decode().rstrip("=")
    token = "synthetic-header." + claim + ".synthetic-signature"
    paths = []

    def response_for(request, **_kwargs):
        paths.append(request.full_url)
        if request.full_url.endswith("/create-jwt-token"):
            assert json.loads(request.data) == {
                "data": {"ttl": 30, "leases": {"access": "scoped", "scope": ["status"]}}
            }
            data = {"token": token}
        else:
            assert request.full_url.endswith("/v1/deployments")
            data = {"dseq": "123"}
        response = MagicMock()
        response.read.return_value = json.dumps({"data": data}).encode()
        response.__enter__.return_value = response
        return response

    opener = MagicMock(side_effect=response_for)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", opener)
    method = client.submit_runtime_limit if runtime_method else client.submit
    args = runtime_authority() if runtime_method else authority()
    assert method(**args) == {"dseq": "123"}
    assert paths == [
        "https://console-api.akash.network/v1/create-jwt-token",
        "https://console-api.akash.network/v1/deployments",
    ]
    with pytest.raises(CreateHeld):
        method(**args)
    assert len(paths) == 2


@pytest.mark.parametrize("hours", [2, 0.25, 2.0, 1e308, 2**256])
def test_runtime_encoder_preserves_exact_guardian_wire_without_deposit(hours):
    assert create_runtime_body(SDL, hours) == json.dumps(
        {"data": {"sdl": SDL, "runtimeLimitHours": hours}}
    ).encode("utf-8")


@pytest.mark.parametrize(
    "hours",
    [
        True,
        False,
        0,
        -1,
        0.0,
        -0.25,
        float("nan"),
        float("inf"),
        -float("inf"),
        "2",
        None,
        {},
        [],
        2j,
    ],
)
def test_malformed_runtime_limit_sends_nothing(client, monkeypatch, hours):
    opener = transport(monkeypatch)
    args = runtime_authority()
    args["runtime_limit_hours"] = hours
    with pytest.raises(CreateHeld, match="runtime-limit create payload"):
        client.submit_runtime_limit(**args)
    assert client._binding is None
    opener.assert_not_called()


@pytest.mark.parametrize("sdl", ["", None, True, {}])
def test_runtime_encoder_rejects_malformed_sdl(sdl):
    with pytest.raises(CreateHeld, match="runtime-limit create payload"):
        create_runtime_body(sdl, 2)


@pytest.mark.parametrize("hours", [2, 0.25, 2.0])
def test_authorized_runtime_create_reaches_exact_real_opener_once(client, monkeypatch, hours):
    opener = transport(monkeypatch)
    args = runtime_authority(runtime_limit_hours=hours)
    assert client.submit_runtime_limit(**args) == {"dseq": "123"}
    opener.assert_called_once()
    request = opener.call_args.args[0]
    assert request.full_url == "https://console-api.akash.network/v1/deployments"
    assert request.get_method() == "POST"
    assert request.data == create_runtime_body(SDL, hours)
    assert request.get_header("X-api-key") == "synthetic-api-key"
    assert client._binding is None
    with pytest.raises(CreateHeld, match="consumed"):
        client.submit_runtime_limit(**args)
    assert opener.call_count == 1


@pytest.mark.parametrize("first_runtime", [False, True])
def test_cross_method_duplicate_cannot_reuse_the_client(client, monkeypatch, first_runtime):
    opener = transport(monkeypatch)
    first, first_args = (
        (client.submit_runtime_limit, runtime_authority())
        if first_runtime
        else (client.submit, authority())
    )
    second, second_args = (
        (client.submit, authority())
        if first_runtime
        else (client.submit_runtime_limit, runtime_authority())
    )
    assert first(**first_args) == {"dseq": "123"}
    with pytest.raises(CreateHeld, match="consumed"):
        second(**second_args)
    assert opener.call_count == 1


@pytest.mark.parametrize("runtime_method", [False, True])
def test_deposit_and_runtime_payload_variants_cannot_be_mixed(client, monkeypatch, runtime_method):
    opener = transport(monkeypatch)
    method, args, extra = (
        (client.submit_runtime_limit, runtime_authority(), {"deposit": 0.5})
        if runtime_method
        else (client.submit, authority(), {"runtime_limit_hours": 2})
    )
    with pytest.raises(TypeError):
        method(**args, **extra)
    opener.assert_not_called()


@pytest.mark.parametrize("runtime_method", [False, True])
def test_authority_for_other_wire_variant_never_sends(client, monkeypatch, runtime_method):
    opener = transport(monkeypatch)
    if runtime_method:
        args = authority()
        args.pop("deposit")
        args["runtime_limit_hours"] = 2
        method = client.submit_runtime_limit
    else:
        args = runtime_authority()
        args.pop("runtime_limit_hours")
        args["deposit"] = 0.5
        method = client.submit
    with pytest.raises(CreateHeld, match="exact current request"):
        method(**args)
    opener.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("sdl_content", SDL + " "),
        ("runtime_limit_hours", 3),
        ("authorization", {}),
        ("permit", {}),
    ],
)
def test_runtime_request_or_untyped_authority_tampering_never_sends(
    client, monkeypatch, field, value
):
    opener = transport(monkeypatch)
    args = runtime_authority()
    args[field] = value
    with pytest.raises(CreateHeld):
        client.submit_runtime_limit(**args)
    opener.assert_not_called()


@pytest.mark.parametrize("tamper", ["hours", "sdl", "mixed", "missing-hours"])
def test_runtime_actual_opener_rejects_post_preflight_payload_changes(client, monkeypatch, tamper):
    opener = transport(monkeypatch)
    original = client._open_request

    def changed_request(request):
        payload = json.loads(request.data)
        if tamper == "hours":
            payload["data"]["runtimeLimitHours"] = 3
        elif tamper == "sdl":
            payload["data"]["sdl"] += " "
        elif tamper == "mixed":
            payload["data"]["deposit"] = 0.5
        else:
            del payload["data"]["runtimeLimitHours"]
        request.data = json.dumps(payload).encode()
        return original(request)

    monkeypatch.setattr(client, "_open_request", changed_request)
    with pytest.raises(CreateHeld, match="exact current request"):
        client.submit_runtime_limit(**runtime_authority())
    assert client._dispatched is False
    assert client._binding is None
    opener.assert_not_called()


@pytest.mark.parametrize("first_runtime", [False, True])
def test_cross_method_concurrency_shares_the_pending_opener_lock(
    client, monkeypatch, first_runtime
):
    opener = transport(monkeypatch)
    first, first_args = (
        (client.submit_runtime_limit, runtime_authority())
        if first_runtime
        else (client.submit, authority())
    )
    second, second_args = (
        (client.submit, authority())
        if first_runtime
        else (client.submit_runtime_limit, runtime_authority())
    )
    entered, release = threading.Event(), threading.Event()
    validate = client._validate
    calls = 0

    def pending_opener(body):
        nonlocal calls
        calls += 1
        validate(body)
        if calls == 2:
            entered.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr(client, "_validate", pending_opener)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(first, **first_args)
        try:
            assert entered.wait(timeout=5)
            with pytest.raises(CreateHeld, match="already in progress"):
                second(**second_args)
        finally:
            release.set()
        assert result.result(timeout=5) == {"dseq": "123"}
    opener.assert_called_once()


@pytest.mark.parametrize("failure", ["timeout", "invalid-dseq"])
def test_runtime_unknown_outcome_never_allows_other_variant_replay(client, monkeypatch, failure):
    opener = transport(
        monkeypatch,
        failure=TimeoutError("synthetic-private-response") if failure == "timeout" else None,
        response={"dseq": "01"} if failure == "invalid-dseq" else None,
    )
    with pytest.raises(CreateUnknown) as caught:
        client.submit_runtime_limit(**runtime_authority())
    assert "synthetic-private-response" not in str(caught.value)
    with pytest.raises(CreateHeld, match="consumed"):
        client.submit(**authority())
    assert opener.call_count == 1


def test_runtime_owner_delay_is_held_at_actual_opener(client, monkeypatch):
    opener = transport(monkeypatch)
    args = runtime_authority()

    def delayed_owner():
        monkeypatch.setattr("just_akash.authorized_console.time.time", lambda: NOW + 6)
        return OWNER

    monkeypatch.setattr(client, "account_address", delayed_owner)
    with pytest.raises(CreateHeld, match="exact current request"):
        client.submit_runtime_limit(**args)
    opener.assert_not_called()


def test_removing_actual_opener_validation_exposes_runtime_expiry_effect(client, monkeypatch):
    """The real socket effect discriminates the opener guard from preflight."""
    opener = transport(monkeypatch)
    args = runtime_authority()

    def delayed_owner():
        monkeypatch.setattr("just_akash.authorized_console.time.time", lambda: NOW + 6)
        return OWNER

    original = AuthorizedConsoleCreate._open_request
    source = textwrap.dedent(inspect.getsource(original))
    guard = "        self._validate(request.data)\n"
    assert source.count(guard) == 1
    namespace = dict(original.__globals__)
    exec(source.replace(guard, "", 1), namespace)
    mutant = namespace[original.__name__]
    # Explicit super avoids requiring a compiler-created __class__ cell in an
    # independently compiled mutation while preserving the same real parent.
    namespace["super"] = lambda: super(AuthorizedConsoleCreate, client)
    monkeypatch.setattr(client, "_open_request", types.MethodType(mutant, client))
    monkeypatch.setattr(client, "account_address", delayed_owner)
    assert client.submit_runtime_limit(**args) == {"dseq": "123"}
    opener.assert_called_once()


@pytest.mark.parametrize("failure", [False, True])
def test_runtime_private_payload_and_response_stay_out_of_logs_and_errors(
    client, monkeypatch, caplog, failure
):
    caplog.set_level(logging.DEBUG, logger="akash.api")
    private = "synthetic-runtime-JIT-do-not-echo"
    payload = json.loads(SDL)
    payload["services"]["runner"]["env"] = [f"RUNNER_JIT_CONFIG={private}"]
    private_sdl = json.dumps(payload)
    args = runtime_authority(
        prepared_change=lambda p: replace(
            p,
            request_digest=canonical_payload_digest(create_runtime_body(private_sdl, 2)),
            sdl_digest=canonical_payload_digest(private_sdl.encode()),
        )
    )
    args["sdl_content"] = private_sdl
    opener = transport(
        monkeypatch,
        failure=TimeoutError(private) if failure else None,
        response={"dseq": "123", "private-response": private},
    )
    if failure:
        with pytest.raises(CreateUnknown) as caught:
            client.submit_runtime_limit(**args)
        visible = (
            caplog.text + str(caught.value) + "".join(traceback.format_exception(caught.value))
        )
    else:
        result = client.submit_runtime_limit(**args)
        assert result["private-response"] == private
        visible = caplog.text
    assert private not in visible
    assert "synthetic-api-key" not in visible
    opener.assert_called_once()
