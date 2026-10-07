"""Fresh state context and complete immutable inclusion remain distinct contracts."""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlsplit

import pytest

from just_akash import chain
from just_akash import execution_observation as m
from tests.test_execution_observation import observe
from tests.test_execution_observation import world as world


def sibling(world, height):
    tx = {
        "body": {"messages": [{"@type": "/cosmos.bank.v1beta1.MsgSend"}]},
        "auth_info": {"signer_infos": [{"sequence": "9"}]},
        "signatures": [base64.b64encode(b"sibling-signature").decode()],
    }
    world.txs[height].append(tx)
    world.raw[height].append(base64.b64encode(b"sibling-at-" + str(height).encode()).decode())


def test_pruned_one_source_state_only_locates_candidate_then_both_sources_prove_close(world):
    parent = world.reader

    def read(path, *, base, height=None):
        if (
            base == world.sources[0]["url"]
            and "/deployments/info" in path
            and height is not None
            and height < 200
        ):
            raise RuntimeError("pruned state")
        return parent(path, base=base, height=height)

    world.reader = read
    result = observe(world)
    assert result.closure is not None and result.closure.close_transaction_height == 150
    assert result.no_further_close_needed and not result.payment_settlement_proven
    for base in (source["url"] for source in world.sources):
        assert any(
            selected == base and "/txs/block/150?" in path and context == 200
            for selected, path, context in world.calls
        )
    assert any(
        "/deployments/info" in path and context not in (None, 200)
        for _, path, context in world.calls
    )


def test_pruned_both_state_locators_cannot_fabricate_close_history(world):
    parent = world.reader

    def read(path, *, base, height=None):
        if "/deployments/info" in path and height is not None and height < 200:
            raise RuntimeError("pruned state")
        return parent(path, base=base, height=height)

    world.reader = read
    result = observe(world)
    assert result.execution_state is m.ExecutionState.CLOSED
    assert result.no_further_close_needed and result.closure is None
    assert result.reason is m.ObservationReason.CLOSE_HISTORY_UNKNOWN


def test_fabricated_single_source_candidate_needs_actual_two_source_signed_block(world):
    parent = world.reader

    def read(path, *, base, height=None):
        if "/deployments/info" in path and height is not None and height < 200:
            if base == world.sources[0]["url"]:
                raise RuntimeError("pruned state")
            value = parent(path, base=base, height=height)
            closed = height >= 120
            value["deployment"]["state"] = "closed" if closed else "active"
            for group in value["groups"]:
                group["state"] = "closed" if closed else "open"
            return value
        return parent(path, base=base, height=height)

    world.reader = read
    result = observe(world)
    assert result.no_further_close_needed and result.closure is None
    assert result.close_transaction is None


def test_all_immutable_requests_pin_fresh_context_and_keep_exact_old_inclusion(world):
    result = observe(world)
    assert result.closure is not None
    historical = [
        (path, height) for _, path, height in world.calls if "/cosmos/tx/v1beta1/" in path
    ]
    assert historical and all(context == 200 for _, context in historical)
    assert any("/txs/block/100?" in path for path, _ in historical)
    assert any("/txs/block/150?" in path for path, _ in historical)
    assert any(
        parse_qs(urlsplit(path).query).get("query") == ["tx.height=150"] for path, _ in historical
    )


@pytest.mark.parametrize("height", [100, 150])
def test_failed_sibling_is_retained_in_complete_consensus_not_used_as_target(world, height):
    sibling(world, height)

    def mutate(_base, path, _context, doc):
        if parse_qs(urlsplit(path).query).get("query") == [f"tx.height={height}"]:
            doc["tx_responses"][1]["code"] = 5
        return doc

    world.mutate = mutate
    result = observe(world)
    assert result.closure is not None and result.close_transaction is not None
    assert result.closure.close_transaction_height == 150


@pytest.mark.parametrize("height", [100, 150])
def test_sibling_execution_code_disagreement_with_equal_raw_block_never_proves_closure(
    world, height
):
    sibling(world, height)

    def mutate(base, path, _context, doc):
        if base == world.sources[1]["url"] and parse_qs(urlsplit(path).query).get("query") == [
            f"tx.height={height}"
        ]:
            doc["tx_responses"][1]["code"] = 5
        return doc

    world.mutate = mutate
    result = observe(world)
    assert result.closure is None
    assert result.execution_state is (
        m.ExecutionState.UNKNOWN if height == 100 else m.ExecutionState.CLOSED
    )
    assert result.no_further_close_needed is (height == 150)


@pytest.mark.parametrize(
    "code", [-1, 2**32, True, "0"], ids=["negative", "overflow", "boolean", "string"]
)
@pytest.mark.parametrize("height", [100, 150], ids=["creation", "close"])
def test_malformed_indexed_code_never_proves_closure(world, height, code):
    def mutate(_base, path, _context, doc):
        if parse_qs(urlsplit(path).query).get("query") == [f"tx.height={height}"]:
            doc["tx_responses"][0]["code"] = code
        return doc

    world.mutate = mutate
    result = observe(world)
    assert result.closure is None
    assert result.execution_state is (
        m.ExecutionState.UNKNOWN if height == 100 else m.ExecutionState.CLOSED
    )
    assert result.no_further_close_needed is (height == 150)


@pytest.mark.parametrize(
    "fault",
    [
        "missing-source",
        "truncated",
        "missing-total",
        "wrong-height",
        "wrong-hash",
        "noninteger-code",
        "negative-code",
        "overflow-code",
        "string-code",
        "changed-decoded",
        "wrong-chain",
        "wrong-block-height",
        "wrong-time",
        "duplicate-hash",
    ],
)
def test_close_population_faults_hold_history_preserving_positive_current_snapshot(world, fault):
    parent = world.reader

    def read(path, *, base, height=None):
        if (
            fault == "missing-source"
            and base == world.sources[1]["url"]
            and "/txs/block/150?" in path
        ):
            raise RuntimeError("unavailable block")
        doc = parent(path, base=base, height=height)
        if parse_qs(urlsplit(path).query).get("query") == ["tx.height=150"]:
            if fault == "truncated":
                doc["total"] = "2"
            if fault == "missing-total":
                doc.pop("total")
            if fault == "wrong-height":
                doc["tx_responses"][0]["height"] = "200"
            if fault == "wrong-hash":
                doc["tx_responses"][0]["txhash"] = "F" * 64
            if fault == "noninteger-code":
                doc["tx_responses"][0]["code"] = True
            if fault == "negative-code":
                doc["tx_responses"][0]["code"] = -1
            if fault == "overflow-code":
                doc["tx_responses"][0]["code"] = 2**32
            if fault == "string-code":
                doc["tx_responses"][0]["code"] = "0"
            if fault == "changed-decoded":
                doc["txs"][0]["auth_info"]["signer_infos"][0]["sequence"] = "999"
        if "/txs/block/150?" in path:
            if fault == "wrong-chain":
                doc["block"]["header"]["chain_id"] = "other"
            if fault == "wrong-block-height":
                doc["block"]["header"]["height"] = "200"
            if fault == "wrong-time":
                doc["block"]["header"]["time"] = "2099-01-01T00:00:00Z"
            if fault == "duplicate-hash":
                doc["block"]["data"]["txs"] *= 2
                doc["txs"] *= 2
                doc["pagination"]["total"] = "2"
        return doc

    world.reader = read
    result = observe(world)
    assert result.execution_state is m.ExecutionState.CLOSED
    assert result.no_further_close_needed and result.closure is None


@pytest.mark.parametrize("context", [True, False, 0, -1, 99, 2**64, "200", None])
def test_history_context_cannot_follow_data_from_future_or_use_noncanonical_state(context):
    if context is None:
        assert chain._history_context(100, None) == 100
    else:
        with pytest.raises(chain.ChainResponseError):
            chain._history_context(100, context)
