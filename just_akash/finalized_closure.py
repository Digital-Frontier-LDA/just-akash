"""Read-only, registered-source adapter for the core execution-closure envelope.

This observes closure; it never closes a deployment, frees reservations, or
proves payment settlement. Runtime adoption still needs the durable broker.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from akash_lease_core.chain_identity import DeploymentKey, is_canonical_akash_owner
from akash_lease_core.create_journal import (
    ExecutionClosure,
    ExecutionClosureProofMode,
    PreparedGroup,
    canonical_group_population_digest,
    canonical_journal_bytes,
    canonical_payload_digest,
)

from . import chain
from .owner_lookup import Deadline

_CLOSE_TYPE = "/akash.deployment.v1beta4.MsgCloseDeployment"
_PAGE_SIZE = 200
_MAX_LEASES = 10_000


class ClosureUnverified(RuntimeError):
    """The observations cannot support a finalized execution-closure claim."""


def _number(value: object, *, zero: bool = False) -> int:
    if type(value) is int:
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        raise ClosureUnverified("noncanonical chain number")
    pattern = r"0|[1-9][0-9]{0,19}" if zero else r"[1-9][0-9]{0,19}"
    if re.fullmatch(pattern, text) is None or int(text) > 2**64 - 1:
        raise ClosureUnverified("noncanonical chain number")
    return int(text)


def _document(reader, source, path: str, height: int) -> dict:
    doc = chain._read_source_document(reader, source, path, height=height)
    if doc is None:
        raise ClosureUnverified("registered source unavailable")
    return doc


def _close_transaction(reader, source, subject: DeploymentKey, txhash: str, height: int):
    doc = _document(reader, source, f"/cosmos/tx/v1beta1/txs/{txhash}", height)
    response = doc.get("tx_response")
    tx = doc.get("tx")
    if not isinstance(response, dict) or not isinstance(tx, dict):
        raise ClosureUnverified("close transaction unreadable")
    tx_height = _number(response.get("height"))
    if (
        response.get("txhash") != txhash
        or type(response.get("code")) is not int
        or response["code"] != 0
        or tx_height > height
    ):
        raise ClosureUnverified("close transaction is unsuccessful or not finalized")
    messages = (tx.get("body") or {}).get("messages")
    signatures = tx.get("signatures")
    signers = (tx.get("auth_info") or {}).get("signer_infos")
    if (
        not isinstance(messages, list)
        or any(not isinstance(message, dict) for message in messages)
        or not isinstance(signatures, list)
        or not signatures
        or any(chain._canonical_base64_bytes(sig) is None for sig in signatures)
        or not isinstance(signers, list)
        or len(signers) != len(signatures)
        or any(not isinstance(signer, dict) for signer in signers)
    ):
        raise ClosureUnverified("close transaction is not a signed decoded transaction")
    matches = [
        message
        for message in messages
        if message.get("@type") == _CLOSE_TYPE
        and message.get("id") == {"owner": subject.owner, "dseq": subject.dseq}
    ]
    if len(matches) != 1:
        raise ClosureUnverified("transaction does not close the exact deployment")
    # Reuse the complete raw/decoded block reader: the tx endpoint's hash and
    # decoded message must bind to a raw transaction in its claimed block.
    block = chain._creation_block_population(reader, source, tx_height)
    if block is None or txhash not in block["raw_hashes"]:
        raise ClosureUnverified("close transaction absent from its inclusion block")
    index = block["raw_hashes"].index(txhash)
    if chain._canonical_document_hash(tx) != block["fingerprints"][index]:
        raise ClosureUnverified("close transaction and inclusion block disagree")
    return {
        "height": tx_height,
        "txhash": txhash,
        "block_hash": block["block_hash"],
        "block_time": block["block_time"].isoformat(),
        "transaction_digest": block["fingerprints"][index],
        "block_population": block["raw_hashes"],
    }


def _lease_population(reader, source, subject: DeploymentKey, height: int, group_count: int):
    """Exhaust offset pages at one height, with a stable explicit total."""
    rows: dict[tuple[str, ...], str] = {}
    offset = 0
    total = None
    for _ in range((_MAX_LEASES // _PAGE_SIZE) + 1):
        query = urllib.parse.urlencode(
            {
                "filters.owner": subject.owner,
                "filters.dseq": subject.dseq,
                "pagination.offset": offset,
                "pagination.limit": _PAGE_SIZE,
                "pagination.count_total": "true",
            }
        )
        doc = _document(reader, source, f"/akash/market/v1beta5/leases/list?{query}", height)
        page = doc.get("leases")
        pagination = doc.get("pagination")
        if not isinstance(page, list) or not isinstance(pagination, dict):
            raise ClosureUnverified("lease population has no completeness evidence")
        current_total = _number(pagination.get("total"), zero=True)
        if current_total > _MAX_LEASES or (total is not None and current_total != total):
            raise ClosureUnverified("lease population is oversized or changed while paginating")
        total = current_total
        if len(page) != min(_PAGE_SIZE, total - offset):
            raise ClosureUnverified("lease population is truncated")
        for row in page:
            lease = row.get("lease") if isinstance(row, dict) else None
            identity = lease.get("id") if isinstance(lease, dict) else None
            if not isinstance(identity, dict) or not isinstance(lease, dict):
                raise ClosureUnverified("lease identity unreadable")
            if identity.get("owner") != subject.owner or str(identity.get("dseq")) != subject.dseq:
                raise ClosureUnverified("lease belongs to another deployment")
            numbers = tuple(
                str(_number(identity.get(field), zero=field == "bseq"))
                for field in ("gseq", "oseq", "bseq")
            )
            provider = identity.get("provider")
            if (
                not isinstance(provider, str)
                or not is_canonical_akash_owner(provider)
                or int(numbers[0]) > group_count
            ):
                raise ClosureUnverified("lease has an unknown group or invalid provider")
            key = (subject.owner, subject.dseq, *numbers, provider)
            if key in rows or lease.get("state") not in {"closed", "insufficient_funds"}:
                raise ClosureUnverified("lease is duplicated or still active")
            rows[key] = lease["state"]
        offset += len(page)
        if offset == total:
            if "next_key" not in pagination or pagination["next_key"] not in (None, ""):
                raise ClosureUnverified("lease population has no agreeing end marker")
            return tuple((*key, state) for key, state in sorted(rows.items()))
        if not isinstance(pagination.get("next_key"), str) or not pagination["next_key"]:
            raise ClosureUnverified("lease population ended before its total")
    raise ClosureUnverified("lease population did not terminate")


def _observe(
    operation_id: str,
    subject: DeploymentKey,
    groups: tuple[PreparedGroup, ...],
    close_tx_hash: str,
    *,
    reader,
    sources,
    clock: Callable[[], datetime],
) -> dict[str, Any]:
    if (
        not isinstance(operation_id, str)
        or not operation_id.strip()
        or operation_id != operation_id.strip()
        or not operation_id.isascii()
        or len(operation_id) > 128
        or not isinstance(subject, DeploymentKey)
    ):
        raise ValueError("exact subject and canonical operation ID required")
    now = clock()
    group_digest = canonical_group_population_digest(groups)
    if not isinstance(close_tx_hash, str) or re.fullmatch(r"[0-9A-F]{64}", close_tx_hash) is None:
        raise ValueError("close transaction hash must be 64 uppercase hexadecimal characters")
    population = tuple((str(group.gseq), group.group_name) for group in groups)
    # This proves the exact group count against the signed creation transaction,
    # and validates independent operators/gateways/caches plus a fresh common tip.
    identity = chain._owner_close_evidence(
        subject.owner,
        subject.dseq,
        groups[0].group_name,
        sources=sources,
        reader=reader,
        now=now,
        expected_population=population,
    )
    if identity is None:
        raise ClosureUnverified("complete creation identity or common finalized height unverified")
    height = identity["height"]
    observations = []
    for source in sources:
        if source["source_id"] not in identity["source_ids"]:
            raise ClosureUnverified("registered source did not prove creation identity")
        close = _close_transaction(reader, source, subject, close_tx_hash, height)
        if close["height"] < identity["creation_height"]:
            raise ClosureUnverified("close transaction predates creation")
        close_time = chain._rfc3339(close["block_time"])
        observation_time = chain._rfc3339(identity["block_time"])
        if close_time is None or observation_time is None or close_time > observation_time:
            raise ClosureUnverified("close inclusion block is later than finalized observation")
        query = urllib.parse.urlencode({"id.owner": subject.owner, "id.dseq": subject.dseq})
        doc = _document(
            reader, source, f"/akash/deployment/v1beta4/deployments/info?{query}", height
        )
        snapshot = chain._deployment_snapshot_at_height(doc, subject.owner, subject.dseq)
        if snapshot != (population, identity["creation_height"]):
            raise ClosureUnverified("closed deployment lost its complete creation population")
        if doc["deployment"].get("state") != "closed" or any(
            group.get("state") != "closed" for group in doc["groups"]
        ):
            raise ClosureUnverified("deployment or group still active")
        leases = _lease_population(reader, source, subject, height, len(groups))
        # Escrow is diagnostic only. A closed escrow without a complete payment
        # observation is not a settlement proof and cannot release financial exposure.
        escrow = doc.get("escrow_account")
        escrow_state = None
        if isinstance(escrow, dict):
            state = escrow.get("state")
            if (
                escrow.get("id")
                != {"scope": "deployment", "xid": f"{subject.owner}/{subject.dseq}"}
                or not isinstance(state, dict)
                or state.get("owner") != subject.owner
            ):
                raise ClosureUnverified("escrow identity does not match deployment")
            escrow_state = state.get("state")
        observations.append(
            {
                "source_id": source["source_id"],
                "close": close,
                "leases": leases,
                "escrow_state": escrow_state,
                "deployment_digest": chain._canonical_document_hash(doc),
            }
        )
    if len(observations) != 2 or any(
        item["close"] != observations[0]["close"] or item["leases"] != observations[0]["leases"]
        for item in observations[1:]
    ):
        raise ClosureUnverified("registered sources disagree on closure")
    finished = clock()
    expires = chain._rfc3339(identity["expires_at"])
    if finished < now or expires is None or finished >= expires:
        raise ClosureUnverified("closure observation expired during collection")
    evidence = {
        "schema": "just-akash-finalized-execution-closure/v1",
        "operation_id": operation_id,
        "identity": identity,
        "observations": observations,
        "observed_at": finished.isoformat(),
        "registry": sources,
    }
    encoded = json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    closure = ExecutionClosure(
        operation_id=operation_id,
        subject=subject,
        chain_id=identity["chain_id"],
        proof_mode=ExecutionClosureProofMode.EXACT_FINALIZED_HEIGHT,
        source_a=sources[0]["source_id"],
        source_b=sources[1]["source_id"],
        operator_identity_a=sources[0]["operator"],
        operator_identity_b=sources[1]["operator"],
        trust_path_a=sources[0]["gateway_ancestry"],
        trust_path_b=sources[1]["gateway_ancestry"],
        operator_independence_verified=True,
        source_a_height=height,
        source_b_height=height,
        common_finality_height=height,
        close_transaction_height=observations[0]["close"]["height"],
        group_population_digest=group_digest,
        lease_population_digest=canonical_payload_digest(
            json.dumps(observations[0]["leases"], separators=(",", ":")).encode()
        ),
        evidence_digest=canonical_payload_digest(encoded),
        observed_at=int(finished.timestamp()),
    )
    return {
        "execution_closed": True,
        "settlement_proven": False,
        "closure": json.loads(canonical_journal_bytes(closure)),
        "evidence": evidence,
    }


def observe_finalized_closure(
    operation_id: str,
    subject: DeploymentKey,
    groups: tuple[PreparedGroup, ...],
    close_tx_hash: str,
) -> dict[str, Any]:
    """Observe through the immutable registry, without ambient endpoint overrides.

    The operation ID is a correlation value, not authenticated broker provenance.
    Persisting this report does not by itself transition a journal or reservation.
    """
    if os.environ.get("AKASH_REST_URL") is not None or (
        chain._source_registry_digest(chain.OWNER_CORROBORATION_SOURCES_V2)
        != chain.OWNER_CORROBORATION_REGISTRY_SHA256
    ):
        raise ClosureUnverified("closure requires the pinned source registry")

    deadline = Deadline(budget=30.0)

    def registered_read(path, *, base, height=None):
        remaining = deadline.remaining()
        if remaining <= 0:
            raise ClosureUnverified("closure observation deadline exhausted")
        return chain._lcd_get(
            path, timeout=min(15.0, remaining), base=base, height=height, follow_redirects=False
        )

    try:
        return _observe(
            operation_id,
            subject,
            groups,
            close_tx_hash,
            reader=registered_read,
            sources=chain.OWNER_CORROBORATION_SOURCES_V2,
            clock=lambda: datetime.now(timezone.utc),
        )
    except (
        chain.ChainResponseError,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ) as exc:
        # Never dump endpoint response bodies or caller-controlled exception text.
        raise ClosureUnverified("finalized execution closure remains unverified") from exc
