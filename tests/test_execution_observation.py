"""Exercise actual SDK signed-create/block/lease readers with pure public-chain fixtures."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from akash_lease_core import (
    DeploymentKey,
    ExecutionClosure,
    PreparedGroup,
    SettlementEvidence,
    SettlementState,
    canonical_group_population_digest,
)

from just_akash import chain
from just_akash import execution_observation as m
from just_akash._lease_verification import verdict

OWNER = "akash14n4rkmz64rn0tey0r5g07l8q5x0fh2h4hu44kt"
PROVIDER = "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk"
SUBJECT = DeploymentKey(OWNER, "12345")
GROUPS = (PreparedGroup(1, "generic-first"), PreparedGroup(2, "generic-second"))
NOW = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)


@pytest.fixture
def world():
    sources = tuple(chain.OWNER_CORROBORATION_SOURCES_V2)
    create_tx = {
        "body": {
            "messages": [
                {
                    "@type": "/akash.deployment.v1beta4.MsgCreateDeployment",
                    "id": {"owner": OWNER, "dseq": SUBJECT.dseq},
                    "groups": [{"name": group.group_name} for group in GROUPS],
                }
            ]
        },
        "auth_info": {"signer_infos": [{"sequence": "7"}]},
        "signatures": [base64.b64encode(b"create-signature").decode()],
    }
    close_tx = {
        "body": {
            "messages": [
                {
                    "@type": "/akash.deployment.v1beta4.MsgCloseDeployment",
                    "id": {"owner": OWNER, "dseq": SUBJECT.dseq},
                }
            ]
        },
        "auth_info": {"signer_infos": [{"sequence": "8"}]},
        "signatures": [base64.b64encode(b"close-signature").decode()],
    }
    info = {
        "deployment": {
            "id": {"owner": OWNER, "dseq": SUBJECT.dseq},
            "created_at": "100",
            "state": "closed",
        },
        "groups": [
            {
                "id": {"owner": OWNER, "dseq": SUBJECT.dseq, "gseq": str(group.gseq)},
                "group_spec": {"name": group.group_name},
                "state": "closed",
            }
            for group in GROUPS
        ],
        "escrow_account": {
            "id": {"scope": "deployment", "xid": f"{OWNER}/{SUBJECT.dseq}"},
            "state": {"owner": OWNER, "state": "closed"},
        },
    }
    lease = {
        "lease": {
            "id": {
                "owner": OWNER,
                "dseq": SUBJECT.dseq,
                "gseq": "1",
                "oseq": "1",
                "bseq": "0",
                "provider": PROVIDER,
            },
            "state": "closed",
        }
    }
    w = SimpleNamespace(
        sources=sources,
        calls=[],
        now=NOW,
        close_height=150,
        mutate=None,
        txs={100: [create_tx], 150: [close_tx]},
        raw={
            100: [base64.b64encode(b"raw-create").decode()],
            150: [base64.b64encode(b"raw-close").decode()],
        },
        info=info,
        leases=[lease],
        code=0,
    )

    def block(height):
        return {
            "block_id": {
                "hash": base64.b64encode((b"A" if height == 200 else b"C") * 32).decode()
            },
            "block": {
                "header": {
                    "height": str(height),
                    "chain_id": "akashnet-2",
                    "time": (NOW - timedelta(seconds=2 if height == 200 else 60)).isoformat(),
                },
                "data": {"txs": w.raw[height]},
            },
        }

    def response(height, index):
        return {
            "height": str(height),
            "code": 0 if height == 100 else w.code,
            "txhash": hashlib.sha256(base64.b64decode(w.raw[height][index])).hexdigest().upper(),
        }

    def reader(path, *, base, height=None):
        w.calls.append((base, path, height))
        assert base in {source["url"] for source in sources}
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/blocks/latest"):
            doc = {"block": {"header": {"height": "202", "chain_id": "akashnet-2"}}}
        elif parsed.path.endswith("/blocks/200"):
            doc = {
                "block_id": {"hash": base64.b64encode(b"A" * 32).decode()},
                "block": {
                    "header": {
                        "height": "200",
                        "chain_id": "akashnet-2",
                        "time": (NOW - timedelta(seconds=2)).isoformat(),
                    }
                },
            }
        elif "/deployments/info" in parsed.path:
            assert query == {"id.owner": [OWNER], "id.dseq": [SUBJECT.dseq]}
            doc = copy.deepcopy(w.info)
            if height is not None and height < w.close_height:
                doc["deployment"]["state"] = "active"
                for row in doc["groups"]:
                    row["state"] = "open"
        elif "/leases/list" in parsed.path:
            assert height == 200
            doc = {
                "leases": copy.deepcopy(w.leases),
                "pagination": {"next_key": None, "total": str(len(w.leases))},
            }
        elif "/txs/block/" in parsed.path:
            selected = int(parsed.path.rsplit("/", 1)[-1])
            assert height == selected
            doc = block(selected)
            offset = int(query["pagination.offset"][0])
            limit = int(query["pagination.limit"][0])
            doc.update(
                txs=copy.deepcopy(w.txs[selected][offset : offset + limit]),
                pagination={"next_key": None, "total": str(len(w.txs[selected]))},
            )
        elif parsed.path == "/cosmos/tx/v1beta1/txs":
            selected = int(query["query"][0].removeprefix("tx.height="))
            assert height == selected == 100
            doc = {
                "total": str(len(w.txs[selected])),
                "txs": copy.deepcopy(w.txs[selected]),
                "tx_responses": [
                    response(selected, index) for index in range(len(w.txs[selected]))
                ],
            }
        elif "/cosmos/tx/v1beta1/txs/" in parsed.path:
            selected_hash = parsed.path.rsplit("/", 1)[-1]
            matching = [
                index
                for index in range(len(w.txs[height]))
                if response(height, index)["txhash"] == selected_hash
            ]
            assert len(matching) == 1
            index = matching[0]
            doc = {
                "tx": copy.deepcopy(w.txs[height][index]),
                "tx_response": response(height, index),
            }
        else:
            raise AssertionError("unexpected public-chain read")
        if w.mutate:
            doc = w.mutate(base, path, height, doc)
        return doc

    w.reader = reader
    return w


def observe(world, **kwargs):
    return m.observe_execution(
        "generic-operation-1",
        SUBJECT,
        GROUPS,
        _reader=world.reader,
        _clock=lambda: world.now,
        **kwargs,
    )


@pytest.mark.parametrize(
    "states",
    [
        ("insufficient_funds", "insufficient_funds"),
        ("closed", "insufficient_funds"),
        ("insufficient_funds", "closed"),
    ],
)
@pytest.mark.parametrize("lease_state", ["closed", "insufficient_funds"])
def test_depleted_closed_groups_without_signed_close_suppress_replay_only(
    world, states, lease_state
):
    for row, state in zip(world.info["groups"], states, strict=True):
        row["state"] = state
    world.info["escrow_account"]["state"]["state"] = "overdrawn"
    world.leases[0]["lease"]["state"] = lease_state
    world.txs[150] = []
    world.raw[150] = []
    result = observe(world)
    assert result.execution_state is m.ExecutionState.CLOSED
    assert result.no_further_close_needed
    assert result.escrow_status is m.EscrowStatus.OVERDRAWN_UNSETTLED
    assert result.reason is m.ObservationReason.CLOSE_HISTORY_UNKNOWN
    assert result.closure is None and result.close_transaction is None
    assert result.settlement is not None
    assert result.settlement.state is SettlementState.UNMEASURED
    assert not result.payment_settlement_proven
    assert not result.financial_exposure_release_authorized


def test_depleted_groups_with_exact_successful_signed_close_allow_typed_closure(world):
    for row in world.info["groups"]:
        row["state"] = "insufficient_funds"
    world.info["escrow_account"]["state"]["state"] = "overdrawn"
    world.leases[0]["lease"]["state"] = "insufficient_funds"
    result = observe(world)
    assert result.execution_state is m.ExecutionState.CLOSED
    assert result.no_further_close_needed
    assert isinstance(result.closure, ExecutionClosure)
    assert result.closure.close_transaction_height == 150
    assert result.close_transaction is not None
    assert result.reason is m.ObservationReason.EXECUTION_CLOSED
    assert result.escrow_status is m.EscrowStatus.OVERDRAWN_UNSETTLED
    assert result.settlement is not None
    assert result.settlement.state is SettlementState.UNMEASURED
    assert not result.payment_settlement_proven
    assert not result.financial_exposure_release_authorized


@pytest.mark.parametrize("live_component", ["deployment", "lease", "open-group", "paused-group"])
def test_depleted_groups_do_not_override_any_live_component(world, live_component):
    for row in world.info["groups"]:
        row["state"] = "insufficient_funds"
    if live_component == "deployment":
        world.info["deployment"]["state"] = "active"
    elif live_component == "lease":
        world.leases[0]["lease"]["state"] = "active"
    else:
        world.info["groups"][0]["state"] = live_component.removesuffix("-group")
    result = observe(world)
    assert result.execution_state is m.ExecutionState.ACTIVE
    assert not result.no_further_close_needed
    assert result.closure is None and result.settlement is None


@pytest.mark.parametrize("state", ["future-state", None, True])
def test_depleted_groups_do_not_allow_unknown_group_states(world, state):
    world.info["groups"][0]["state"] = "insufficient_funds"
    world.info["groups"][1]["state"] = state
    result = observe(world)
    assert result.execution_state is m.ExecutionState.UNKNOWN
    assert not result.no_further_close_needed and result.closure is None


def test_distinct_terminal_group_states_between_sources_remain_unknown(world):
    def mutate(base, path, _height, doc):
        if base == world.sources[1]["url"] and "/deployments/info" in path:
            doc["groups"][0]["state"] = "insufficient_funds"
        return doc

    world.mutate = mutate
    result = observe(world)
    assert result.execution_state is m.ExecutionState.UNKNOWN
    assert not result.no_further_close_needed and result.closure is None


def test_actual_signed_create_and_close_readers_produce_released_core_evidence(world):
    result = observe(world)
    assert result.execution_state is m.ExecutionState.CLOSED
    assert result.no_further_close_needed
    assert isinstance(result.closure, ExecutionClosure)
    assert result.closure.close_transaction_height == 150
    assert result.closure.common_finality_height == 200
    assert result.closure.source_a_height == result.closure.source_b_height == 200
    assert result.closure.group_population_digest == canonical_group_population_digest(GROUPS)
    assert isinstance(result.settlement, SettlementEvidence)
    assert result.settlement.state is SettlementState.UNMEASURED
    assert result.close_transaction is not None
    assert result.close_transaction.height == 150
    assert len(result.close_transaction.transaction_hash) == 64
    assert result.reason is m.ObservationReason.EXECUTION_CLOSED
    assert result.group_count == 2 and result.lease_count == 1
    assert any("/txs/block/100?" in path for _, path, _ in world.calls)
    assert any("/txs/block/150?" in path for _, path, _ in world.calls)
    assert any("/cosmos/tx/v1beta1/txs/" in path for _, path, _ in world.calls)
    assert result.reads == len(world.calls) < m.MAX_READS


@pytest.mark.parametrize(
    "state,escrow,financial",
    [
        ("overdrawn", m.EscrowStatus.OVERDRAWN_UNSETTLED, SettlementState.UNMEASURED),
        ("open", m.EscrowStatus.UNKNOWN, SettlementState.UNMEASURED),
        ("future-state", m.EscrowStatus.UNKNOWN, SettlementState.UNMEASURED),
    ],
)
def test_execution_closure_is_independent_of_finance(world, state, escrow, financial):
    world.info["escrow_account"]["state"]["state"] = state
    result = observe(world)
    assert result.closure is not None and result.no_further_close_needed
    assert result.settlement is not None
    assert result.escrow_status is escrow and result.settlement.state is financial


def test_unreadable_escrow_still_allows_typed_execution_closure(world):
    del world.info["escrow_account"]
    result = observe(world)
    assert result.closure is not None and result.escrow_status is m.EscrowStatus.UNKNOWN


@pytest.mark.parametrize(
    "fault",
    [
        "history-missing",
        "transaction-missing",
        "unsigned",
        "failed",
        "wrong-owner",
        "wrong-dseq",
        "wrong-type",
        "duplicate-close",
    ],
)
def test_lost_close_history_preserves_snapshot_and_never_invents_close_height(world, fault):
    if fault == "history-missing":

        def mutate(_base, path, height, doc):
            if "/deployments/info" in path and height is not None and height < 200:
                raise RuntimeError("archive unavailable")
            return doc

        world.mutate = mutate
    elif fault == "transaction-missing":

        def missing_transaction(_base, path, _height, doc):
            if "/cosmos/tx/v1beta1/txs/" in path and "/txs/block/" not in path:
                raise RuntimeError("transaction unavailable")
            return doc

        world.mutate = missing_transaction
    elif fault == "unsigned":
        world.txs[150][0]["signatures"] = []
    elif fault == "failed":
        world.code = 7
    elif fault == "duplicate-close":
        world.txs[150][0]["body"]["messages"] *= 2
    else:
        message = world.txs[150][0]["body"]["messages"][0]
        if fault == "wrong-owner":
            message["id"]["owner"] = PROVIDER
        elif fault == "wrong-dseq":
            message["id"]["dseq"] = "999"
        else:
            message["@type"] = "/akash.market.v1beta5.MsgCloseBid"
    result = observe(world)
    assert result.no_further_close_needed and result.closure is None
    assert result.close_transaction is None
    assert result.reason is m.ObservationReason.CLOSE_HISTORY_UNKNOWN


def test_close_block_source_disagreement_cannot_construct_durable_closure(world):
    def mutate(base, path, _height, doc):
        if base == world.sources[1]["url"] and "/txs/block/150?" in path:
            doc["block_id"]["hash"] = base64.b64encode(b"F" * 32).decode()
        return doc

    world.mutate = mutate
    result = observe(world)
    assert result.no_further_close_needed and result.closure is None


def test_close_raw_block_and_hash_readback_must_bind_same_decoded_transaction(world):
    def mutate(_base, path, _height, doc):
        if "/cosmos/tx/v1beta1/txs/" in path:
            doc["tx"]["body"]["messages"][0]["id"]["dseq"] = "999"
        return doc

    world.mutate = mutate
    assert observe(world).closure is None


def test_incomplete_close_block_pagination_prevents_typed_closure(world):
    def mutate(_base, path, _height, doc):
        if "/txs/block/150?" in path:
            doc["pagination"]["total"] = "2"
        return doc

    world.mutate = mutate
    assert observe(world).closure is None


def test_new_observer_preserves_legacy_verdict_financial_meaning(world):
    world.info["escrow_account"]["state"]["state"] = "overdrawn"
    assert observe(world).closure is not None

    def get(url):
        source = next(s for s in world.sources if url.startswith(s["url"] + "/"))
        return world.reader(url[len(source["url"]) :], base=source["url"], height=200)

    assert (
        verdict(SUBJECT.dseq, OWNER, tuple(s["url"] for s in world.sources), get)["closed"]
        is False
    )


def test_empty_complete_lease_history_is_not_a_standalone_closure_proof(world):
    world.leases = []
    assert observe(world).closure is not None
    world.info["deployment"]["state"] = "active"
    assert not observe(world).no_further_close_needed


@pytest.mark.parametrize("target", ["lease", "group", "deployment"])
def test_any_live_component_prevents_verified_execution_closure(world, target):
    if target == "lease":
        world.leases[0]["lease"]["state"] = "active"
    elif target == "group":
        world.info["groups"][1]["state"] = "paused"
    else:
        world.info["deployment"]["state"] = "active"
    result = observe(world)
    assert not result.no_further_close_needed and result.closure is None
    assert result.settlement is None


def test_generic_complete_population_is_independent_of_response_order(world):
    def mutate(base, path, _height, doc):
        if base == world.sources[1]["url"] and "/deployments/info" in path:
            doc["groups"].reverse()
        return doc

    world.mutate = mutate
    assert observe(world).closure is not None


@pytest.mark.parametrize(
    "fault",
    [
        "source-down",
        "missing-group",
        "wrong-group",
        "extra-group",
        "source-state-disagree",
        "lease-disagree",
        "unknown-provider",
    ],
)
def test_unknown_partial_ambiguous_and_mismatched_snapshots_cannot_suppress_close(world, fault):
    def mutate(base, path, _height, doc):
        if base != world.sources[1]["url"]:
            return doc
        if fault == "source-down":
            raise RuntimeError("Authorization: Bearer planted-secret")
        if "/deployments/info" in path:
            if fault == "missing-group":
                doc["groups"].pop()
            elif fault == "extra-group":
                doc["groups"].append(copy.deepcopy(doc["groups"][0]))
            elif fault == "wrong-group":
                doc["groups"][0]["group_spec"]["name"] = "replacement"
            elif fault == "source-state-disagree":
                doc["groups"][0]["state"] = "open"
        if "/leases/list" in path:
            if fault == "lease-disagree":
                doc["leases"][0]["lease"]["id"]["bseq"] = "1"
            elif fault == "unknown-provider":
                doc["leases"][0]["lease"]["id"]["provider"] = "Bearer planted-secret"
        return doc

    world.mutate = mutate
    result = observe(world)
    assert result.execution_state is m.ExecutionState.UNKNOWN
    assert not result.no_further_close_needed and result.closure is None
    assert "planted-secret" not in json.dumps(asdict(result))


def test_registry_or_environment_override_cannot_select_an_unregistered_voter(world, monkeypatch):
    monkeypatch.setenv("AKASH_REST_URL", "https://untrusted.invalid")
    result = observe(world)
    assert result.reason is m.ObservationReason.REGISTRY_INVALID and world.calls == []


def test_duplicate_or_replacement_registry_fails_before_reads(world, monkeypatch):
    registry = copy.deepcopy(chain.OWNER_CORROBORATION_SOURCES_V2)
    registry[1]["url"] = registry[0]["url"]
    monkeypatch.setattr(chain, "OWNER_CORROBORATION_SOURCES_V2", registry)
    assert observe(world).reason is m.ObservationReason.REGISTRY_INVALID
    assert world.calls == []


def test_explicit_snapshot_mode_makes_no_historical_transaction_calls(world):
    result = observe(world, recover_close_transaction=False)
    assert result.no_further_close_needed and result.closure is None
    assert not any("/txs/block/150?" in path for _, path, _ in world.calls)


def test_repeat_observation_is_idempotent_and_has_no_effect_transport(world):
    before = copy.deepcopy((world.info, world.txs, world.raw, world.leases))
    first, second = observe(world), observe(world)
    assert first == second and first.closure is not None
    assert before == (world.info, world.txs, world.raw, world.leases)
    assert all(path.startswith(("/akash/", "/cosmos/")) for _, path, _ in world.calls)


def test_invalid_untyped_or_incomplete_request_is_rejected_before_reads(world):
    for subject, groups in [("12345", GROUPS), (SUBJECT, ()), (SUBJECT, tuple(reversed(GROUPS)))]:
        with pytest.raises(ValueError):
            m.observe_execution("operation", subject, groups, _reader=world.reader)
    assert world.calls == []


def test_wall_clock_and_read_budget_are_actual_transport_bounds(world, monkeypatch):
    monkeypatch.setattr(m, "MAX_READS", 1)
    result = observe(world)
    assert result.execution_state is m.ExecutionState.UNKNOWN and len(world.calls) <= 1


def test_missing_create_proof_cannot_promote_console_response_to_execution_evidence(world):
    def mutate(_base, path, _height, doc):
        if "/txs/block/100?" in path:
            doc["txs"] = []
        return doc

    world.mutate = mutate
    assert observe(world).execution_state is m.ExecutionState.UNKNOWN


def test_default_http_transport_is_credential_free_bounded_and_height_verified(world, monkeypatch):
    requests = []

    class Response:
        def __init__(self, payload, height):
            self.payload = json.dumps(payload).encode()
            self.headers = {"x-cosmos-block-height": height} if height else {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, cap):
            assert cap == m.MAX_RESPONSE_BYTES + 1
            return self.payload[:cap]

    class Opener:
        def open(self, request, *, timeout):
            requests.append(request)
            assert request.get_method() == "GET"
            assert not any(key.lower() == "authorization" for key in request.headers)
            assert 0 < timeout <= 15
            source = next(s for s in world.sources if request.full_url.startswith(s["url"] + "/"))
            height = request.headers.get("X-cosmos-block-height")
            payload = world.reader(
                request.full_url[len(source["url"]) :],
                base=source["url"],
                height=int(height) if height else None,
            )
            return Response(payload, height)

    def opener(handler):
        assert isinstance(handler, chain._NoChainRedirect)
        return Opener()

    monkeypatch.setattr(m.urllib.request, "build_opener", opener)
    result = m.observe_execution("operation-1", SUBJECT, GROUPS, _clock=lambda: NOW)
    assert result.closure is not None and requests


@pytest.mark.parametrize(
    "fault", ["duplicate-key", "nan", "oversized", "missing-height", "wrong-height"]
)
def test_default_transport_refuses_ambiguous_unbounded_or_unpinned_documents(
    world, monkeypatch, fault
):
    class Response:
        headers = (
            {}
            if fault == "missing-height"
            else {"x-cosmos-block-height": "201" if fault == "wrong-height" else "200"}
        )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, cap):
            if fault == "duplicate-key":
                return b'{"state":"active","state":"closed"}'
            if fault == "nan":
                return b'{"state":NaN}'
            if fault == "oversized":
                return b"x" * cap
            return b"{}"

    class Opener:
        def open(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(m.urllib.request, "build_opener", lambda _handler: Opener())
    budget = m._Budget(world.sources, None, lambda: 0.0)
    with pytest.raises(m._Held):
        budget.read(
            "/akash/deployment/v1beta4/deployments/info", base=world.sources[0]["url"], height=200
        )
    assert budget.reads == 1


@pytest.mark.parametrize(
    "fault", ["foreign-source", "foreign-path", "fragment", "huge-path", "deadline"]
)
def test_transport_bounds_hold_before_any_rpc_call(world, fault):
    budget = m._Budget(world.sources, world.reader, lambda: 0.0)
    base, path = world.sources[0]["url"], "/cosmos/base/tendermint/v1beta1/blocks/latest"
    if fault == "foreign-source":
        base = "https://unregistered.invalid"
    elif fault == "foreign-path":
        path = "/api/cleanup"
    elif fault == "fragment":
        path += "#fragment"
    elif fault == "huge-path":
        path += "x" * 8192
    else:
        budget.monotonic = lambda: 121.0
    with pytest.raises(m._Held):
        budget.read(path, base=base)
    assert world.calls == []


def test_slow_transport_cannot_finish_after_total_deadline(world):
    ticks = iter([0.0, 0.0, 121.0])
    budget = m._Budget(world.sources, world.reader, lambda: next(ticks))
    with pytest.raises(m._Held):
        budget.read("/cosmos/base/tendermint/v1beta1/blocks/latest", base=world.sources[0]["url"])
    assert budget.reads == 1


def test_expired_snapshot_cannot_authorize_duplicate_close_suppression(world):
    def mutate(_base, path, _height, doc):
        if "/leases/list" in path:
            world.now = NOW + timedelta(seconds=31)
        return doc

    world.mutate = mutate
    result = observe(world)
    assert (
        result.execution_state is m.ExecutionState.UNKNOWN and not result.no_further_close_needed
    )


def test_escrow_two_source_disagreement_stays_unknown_while_execution_is_closed(world):
    def mutate(base, path, _height, doc):
        if base == world.sources[1]["url"] and "/deployments/info" in path:
            doc["escrow_account"]["state"]["state"] = "overdrawn"
        return doc

    world.mutate = mutate
    result = observe(world)
    assert result.closure is not None and result.no_further_close_needed
    assert result.escrow_status is m.EscrowStatus.UNKNOWN
    assert result.settlement is not None
    assert result.settlement.state is SettlementState.UNMEASURED


def test_failed_post_history_refresh_preserves_dated_closed_snapshot_without_durable_closure(
    world,
):
    seen_close = False

    def mutate(_base, path, _height, doc):
        nonlocal seen_close
        if "/cosmos/tx/v1beta1/txs/" in path and "/txs/block/" not in path:
            seen_close = True
        if seen_close and path.endswith("/blocks/latest"):
            raise RuntimeError("post-history snapshot unavailable")
        return doc

    world.mutate = mutate
    result = observe(world)
    assert result.no_further_close_needed and result.observation_height == 200
    assert result.closure is None and result.close_transaction is None
    assert result.reason is m.ObservationReason.CLOSE_HISTORY_UNKNOWN


def test_create_and_close_in_one_complete_block_recover_actual_creation_height(world):
    world.close_height = 100
    world.txs[100].append(world.txs[150][0])
    world.raw[100].append(world.raw[150][0])
    result = observe(world)
    assert result.closure is not None
    assert result.closure.close_transaction_height == 100
    assert result.close_transaction is not None and result.close_transaction.transaction_index == 1


def test_exact_close_proof_does_not_confuse_other_deployments_in_same_block(world):
    unrelated = copy.deepcopy(world.txs[150][0])
    unrelated["body"]["messages"][0]["id"]["dseq"] = "88888"
    world.txs[150].append(unrelated)
    world.raw[150].append(base64.b64encode(b"unrelated-close").decode())
    result = observe(world)
    assert result.closure is not None
    assert result.close_transaction is not None and result.close_transaction.transaction_index == 0


@pytest.mark.parametrize(
    "fault",
    [
        "missing-pagination",
        "missing-next-key",
        "missing-total",
        "truncated-nonempty",
        "total-too-low",
        "boolean-total",
        "empty-continuation",
    ],
)
def test_lease_completeness_needs_explicit_consistent_population_end(world, fault):
    def mutate(_base, path, _height, doc):
        if "/leases/list" not in path:
            return doc
        if fault == "missing-pagination":
            del doc["pagination"]
        elif fault == "missing-next-key":
            del doc["pagination"]["next_key"]
        elif fault == "missing-total":
            del doc["pagination"]["total"]
        elif fault == "truncated-nonempty":
            doc["pagination"]["total"] = "2"
        elif fault == "total-too-low":
            doc["pagination"]["total"] = "0"
        elif fault == "boolean-total":
            doc["pagination"]["total"] = True
        else:
            doc["leases"] = []
            doc["pagination"] = {"next_key": "advertised-page", "total": "1"}
        return doc

    world.mutate = mutate
    result = observe(world)
    assert result.execution_state is m.ExecutionState.UNKNOWN
    assert not result.no_further_close_needed and result.closure is None


@pytest.mark.parametrize("contradictory", [False, True])
def test_offset_pages_request_and_reconcile_total_at_every_height_pinned_page(
    world, contradictory
):
    rows = []
    for index in range(201):
        row = copy.deepcopy(world.leases[0])
        row["lease"]["id"]["oseq"] = str(index + 1)
        rows.append(row)

    def mutate(_base, path, _height, doc):
        if "/leases/list" in path:
            query = parse_qs(urlsplit(path).query)
            assert query["pagination.count_total"] == ["true"]
            assert "pagination.key" not in query
            offset = int(query["pagination.offset"][0])
            assert offset in (0, 200)
            doc["leases"] = copy.deepcopy(rows[offset : offset + 200])
            doc["pagination"] = {
                "next_key": "next-row" if offset == 0 else None,
                "total": "202" if contradictory and offset else "201",
            }
        return doc

    world.mutate = mutate
    result = observe(world)
    if contradictory:
        assert result.execution_state is m.ExecutionState.UNKNOWN and result.closure is None
    else:
        assert result.closure is not None and result.lease_count == 201


@pytest.mark.parametrize(
    "fault", ["source-time-disagreement", "future-time", "before-creation-time"]
)
def test_close_block_timestamp_must_corroborate_and_fit_creation_to_finality_window(world, fault):
    def mutate(base, path, _height, doc):
        if "/txs/block/150?" in path:
            if fault == "source-time-disagreement" and base == world.sources[1]["url"]:
                doc["block"]["header"]["time"] = (NOW - timedelta(seconds=59)).isoformat()
            elif fault == "future-time":
                doc["block"]["header"]["time"] = "2099-01-01T00:00:00+00:00"
            elif fault == "before-creation-time":
                doc["block"]["header"]["time"] = (NOW - timedelta(seconds=61)).isoformat()
        return doc

    world.mutate = mutate
    result = observe(world)
    assert result.no_further_close_needed and result.closure is None
    assert result.reason is m.ObservationReason.CLOSE_HISTORY_UNKNOWN


@pytest.mark.parametrize(
    "field,value",
    [
        ("dseq", "012345"),
        ("dseq", "0"),
        ("gseq", "01"),
        ("gseq", "0"),
        ("oseq", "01"),
        ("oseq", "0"),
        ("oseq", True),
        ("oseq", str(2**64)),
        ("bseq", "00"),
        ("bseq", "-1"),
        ("bseq", str(2**64)),
    ],
)
def test_raw_lease_numbers_are_canonical_uint64_before_legacy_normalization(world, field, value):
    world.leases[0]["lease"]["id"][field] = value
    result = observe(world)
    assert result.execution_state is m.ExecutionState.UNKNOWN
    assert result.closure is None and not result.no_further_close_needed


@pytest.mark.parametrize("escrow", ["closed", "overdrawn", "unknown"])
def test_operational_escrow_never_fabricates_complete_payment_settlement(world, escrow):
    world.info["escrow_account"]["state"]["state"] = escrow
    result = observe(world)
    assert result.closure is not None and result.no_further_close_needed
    assert result.settlement is not None and result.settlement.state is SettlementState.UNMEASURED
    assert result.payment_settlement_proven is False
    assert result.financial_exposure_release_authorized is False


def test_backwards_completion_clock_cannot_create_positive_closed_snapshot(world):
    def mutate(_base, path, _height, doc):
        if "/leases/list" in path:
            world.now = NOW - timedelta(seconds=31)
        return doc

    world.mutate = mutate
    result = observe(world, recover_close_transaction=False)
    assert result.execution_state is m.ExecutionState.UNKNOWN
    assert not result.no_further_close_needed and result.closure is None


@pytest.mark.parametrize("unreadable", [None, [], {}, 1, True])
def test_unreadable_operational_escrow_state_does_not_hide_closed_execution(world, unreadable):
    world.info["escrow_account"]["state"]["state"] = unreadable
    result = observe(world)
    assert result.closure is not None and result.no_further_close_needed
    assert result.escrow_status is m.EscrowStatus.UNKNOWN
    assert result.settlement is not None and result.settlement.state is SettlementState.UNMEASURED
