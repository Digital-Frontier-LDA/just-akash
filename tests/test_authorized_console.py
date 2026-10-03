"""Actual opener effects under publicly confirmed create/redemption authority."""

from __future__ import annotations

import json
import threading
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


def authority(*, prepared_change=None):
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
        canonical_payload_digest(create_body(SDL, 0.5)),
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
def test_signed_foreign_backend_or_group_never_sends(client, monkeypatch, change):
    opener = transport(monkeypatch)
    with pytest.raises(CreateHeld):
        client.submit(**authority(prepared_change=change))
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


def test_owner_credential_mismatch_never_sends(client, monkeypatch):
    opener = transport(monkeypatch)
    monkeypatch.setattr(client, "account_address", lambda: "foreign-owner")
    with pytest.raises(CreateHeld, match="credential owner"):
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
