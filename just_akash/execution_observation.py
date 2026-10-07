"""Read-only finalized execution/escrow observations using the released core types.

This additive API leaves ``_lease_verification.verdict`` unchanged. A finalized
closed snapshot can suppress a duplicate close even if escrow or close history
is unavailable. It becomes an ``ExecutionClosure`` only after a successful,
signed, exact ``MsgCloseDeployment`` is independently recovered. No observation
grants retirement permission or releases financial exposure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from urllib.parse import parse_qs, urlencode, urlsplit

from akash_lease_core import (
    DeploymentKey,
    ExecutionClosure,
    ExecutionClosureProofMode,
    PreparedGroup,
    SettlementEvidence,
    SettlementState,
    canonical_group_population_digest,
)

from . import chain
from ._lease_verification import TERMINAL_STATES, lease_snapshot
from .address import is_canonical_akash_address
from .finalized_closure import _number as _chain_number

logger = logging.getLogger(__name__)

MAX_READS = 512
MAX_RESPONSE_BYTES = 2_097_152
MAX_SECONDS = 120
MAX_GROUPS = 100
MAX_HISTORY_STEPS = 64


class ExecutionState(str, Enum):
    CLOSED = "closed"
    ACTIVE = "active"
    UNKNOWN = "unknown"


class EscrowStatus(str, Enum):
    SETTLED = "settled"
    OVERDRAWN_UNSETTLED = "overdrawn-unsettled"
    UNKNOWN = "unknown"


class ObservationReason(str, Enum):
    EXECUTION_CLOSED = "execution.closed"
    EXECUTION_ACTIVE = "execution.active"
    EXECUTION_UNKNOWN = "execution.unknown"
    CLOSE_HISTORY_UNKNOWN = "close_history.unknown"
    REGISTRY_INVALID = "registry.invalid"


@dataclass(frozen=True)
class CloseTransactionProof:
    height: int
    transaction_hash: str
    transaction_index: int
    block_hash: str
    block_time: str
    block_population_digest: str
    source_ids: tuple[str, str]


@dataclass(frozen=True)
class ExecutionObservation:
    operation_id: str
    subject: DeploymentKey
    execution_state: ExecutionState
    escrow_status: EscrowStatus
    closure: ExecutionClosure | None
    settlement: SettlementEvidence | None
    reason: ObservationReason
    observation_height: int | None
    observed_at: int
    group_count: int
    lease_count: int | None
    snapshot_digest: str | None
    reads: int
    close_transaction: CloseTransactionProof | None = None

    @property
    def payment_settlement_proven(self) -> bool:
        return False

    @property
    def financial_exposure_release_authorized(self) -> bool:
        return False

    @property
    def no_further_close_needed(self) -> bool:
        """False is unknown/active, and never permission to submit a close."""
        return self.execution_state is ExecutionState.CLOSED


class _Held(RuntimeError):
    """Internal refusal; only the public enum survives transport failures."""


def _bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Held
        result[key] = value
    return result


def _invalid_json_constant(_value: str) -> object:
    raise _Held


def _digest(value: object) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _number(value: object) -> str:
    if type(value) not in (str, int) or re.fullmatch(r"0|[1-9][0-9]*", str(value)) is None:
        raise _Held
    return str(value)


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise _Held
    parsed = chain._rfc3339(value)
    if parsed is None:
        raise _Held
    return parsed


def _registry() -> tuple[dict, ...]:
    sources = tuple(chain.OWNER_CORROBORATION_SOURCES_V2)
    if (
        os.environ.get("AKASH_REST_URL") is not None
        or len(sources) != 2
        or chain._source_registry_digest(sources) != chain.OWNER_CORROBORATION_REGISTRY_SHA256
    ):
        raise _Held
    return sources


class _Budget:
    def __init__(
        self, sources: tuple[dict, ...], reader: Callable | None, monotonic: Callable[[], float]
    ):
        self.sources, self.reader, self.monotonic = sources, reader, monotonic
        self.deadline = monotonic() + MAX_SECONDS
        self.reads = 0

    def read(self, path: str, *, base: str, height: int | None = None) -> dict:
        remaining = self.deadline - self.monotonic()
        if (
            remaining <= 0
            or self.reads >= MAX_READS
            or base not in {source["url"] for source in self.sources}
            or len(path) > 8192
            or not path.startswith(("/akash/", "/cosmos/"))
            or "#" in path
        ):
            raise _Held
        self.reads += 1
        if self.reader is not None:
            result = self.reader(path, base=base, height=height)
        else:
            headers = {"Accept": "application/json"}
            if height is not None:
                headers["x-cosmos-block-height"] = str(height)
            request = urllib.request.Request(base.rstrip("/") + path, headers=headers)  # noqa: S310
            opener = urllib.request.build_opener(chain._NoChainRedirect())
            with opener.open(request, timeout=min(15, remaining)) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise _Held
                if height is not None and response.headers.get("x-cosmos-block-height") != str(
                    height
                ):
                    raise _Held
            result = json.loads(
                raw, object_pairs_hook=_json_object, parse_constant=_invalid_json_constant
            )
        if (
            self.monotonic() >= self.deadline
            or not isinstance(result, dict)
            or len(_bytes(result)) > MAX_RESPONSE_BYTES
        ):
            raise _Held
        return result


@dataclass(frozen=True)
class _Snapshot:
    evidence: dict
    state: ExecutionState
    escrow: EscrowStatus
    leases: tuple
    digest: str
    observed_at: int


def _proof(
    subject: DeploymentKey,
    groups: tuple[PreparedGroup, ...],
    sources: tuple[dict, ...],
    budget: _Budget,
    now: datetime,
) -> dict:
    population = [{"gseq": group.gseq, "name": group.group_name} for group in groups]
    evidence = chain._owner_close_evidence(
        subject.owner,
        subject.dseq,
        groups[0].group_name,
        sources=sources,
        reader=budget.read,
        now=now,
        expected_population=tuple((str(g.gseq), g.group_name) for g in groups),
    )
    if not isinstance(evidence, dict):
        raise _Held
    expected_digest = _digest(tuple((str(g.gseq), g.group_name) for g in groups))
    if (
        type(evidence.get("evidence_version")) is not int
        or evidence.get("evidence_version") != 2
        or evidence.get("owner") != subject.owner
        or evidence.get("dseq") != subject.dseq
        or evidence.get("groups") != population
        or type(evidence.get("population_count")) is not int
        or evidence.get("population_count") != len(groups)
        or evidence.get("population_digest") != expected_digest
        or evidence.get("source_ids") != [source["source_id"] for source in sources]
        or evidence.get("registry_version") != chain.OWNER_CORROBORATION_REGISTRY_VERSION
        or evidence.get("registry_digest") != chain.OWNER_CORROBORATION_REGISTRY_SHA256
        or evidence.get("registry_provenance_digest")
        != chain.OWNER_CORROBORATION_REGISTRY_PROVENANCE_SHA256
        or type(evidence.get("height")) is not int
        or evidence["height"] <= 0
        or type(evidence.get("creation_height")) is not int
        or not 0 < evidence["creation_height"] <= evidence["height"]
        or not (
            _time(evidence.get("block_time"))
            <= _time(evidence.get("observed_at"))
            <= now
            < _time(evidence.get("expires_at"))
        )
        or (now - _time(evidence["block_time"])).total_seconds() > 180
        or (_time(evidence["expires_at"]) - _time(evidence["observed_at"])).total_seconds() > 30
    ):
        raise _Held
    return evidence


def _info(
    subject: DeploymentKey,
    groups: tuple[PreparedGroup, ...],
    source: dict,
    height: int,
    budget: _Budget,
) -> tuple[str, tuple, str]:
    path = "/akash/deployment/v1beta4/deployments/info?" + urlencode(
        {"id.owner": subject.owner, "id.dseq": subject.dseq}
    )
    document = budget.read(path, base=source["url"], height=height)
    deployment = document.get("deployment")
    if not isinstance(deployment, dict):
        raise _Held
    identity = deployment.get("id")
    if (
        not isinstance(identity, dict)
        or identity.get("owner") != subject.owner
        or _number(identity.get("dseq")) != subject.dseq
        or deployment.get("state") not in {"active", "closed"}
    ):
        raise _Held
    rows = document.get("groups")
    if not isinstance(rows, list) or len(rows) != len(groups):
        raise _Held
    observed = {}
    wanted = {str(group.gseq): group for group in groups}
    for row in rows:
        if not isinstance(row, dict):
            raise _Held
        key, specification = row.get("id"), row.get("group_spec")
        if (
            not isinstance(key, dict)
            or key.get("owner") != subject.owner
            or _number(key.get("dseq")) != subject.dseq
            or _number(key.get("gseq")) not in wanted
            or not isinstance(specification, dict)
            or specification.get("name") != wanted[_number(key["gseq"])].group_name
            or row.get("state") not in {"open", "paused", "closed"}
        ):
            raise _Held
        expected = wanted[_number(key["gseq"])]
        if expected.gseq in observed:
            raise _Held
        observed[expected.gseq] = (expected.gseq, expected.group_name, row["state"])
    escrow = document.get("escrow_account")
    financial = "unknown"
    if isinstance(escrow, dict):
        key, state = escrow.get("id"), escrow.get("state")
        if (
            isinstance(key, dict)
            and key.get("scope") == "deployment"
            and key.get("xid") == f"{subject.owner}/{subject.dseq}"
            and isinstance(state, dict)
            and state.get("owner") == subject.owner
            and isinstance(state.get("state"), str)
            and state["state"] in {"closed", "overdrawn"}
        ):
            financial = state["state"]
    return deployment["state"], tuple(observed[index] for index in sorted(observed)), financial


def _sample(
    subject: DeploymentKey,
    groups: tuple[PreparedGroup, ...],
    sources: tuple[dict, ...],
    budget: _Budget,
    clock: Callable[[], datetime],
) -> _Snapshot:
    evidence = _proof(subject, groups, sources, budget, clock())
    height = evidence["height"]
    infos = tuple(_info(subject, groups, source, height, budget) for source in sources)
    if infos[0][:2] != infos[1][:2]:
        raise _Held
    maps = []
    for source in sources:
        base = source["url"]
        observed_rows = 0
        reported_total = None
        complete = False

        def read_url(url: str, selected_base: str = base) -> dict:
            nonlocal observed_rows, reported_total, complete
            if not url.startswith(selected_base + "/akash/market/v1beta5/leases/list?"):
                raise _Held
            query = parse_qs(urlsplit(url).query, strict_parsing=True)
            if query.get("filters.owner") != [subject.owner] or query.get("filters.dseq") != [
                subject.dseq
            ]:
                raise _Held
            # Offset pagination makes count_total effective on every page. Cosmos
            # cursor pagination may legitimately report total=0 after the first
            # page; never treat that ambiguity as a completeness certificate.
            path = "/akash/market/v1beta5/leases/list?" + urlencode(
                {
                    "filters.owner": subject.owner,
                    "filters.dseq": subject.dseq,
                    "pagination.limit": "200",
                    "pagination.offset": str(observed_rows),
                    "pagination.count_total": "true",
                }
            )
            document = budget.read(path, base=selected_base, height=height)
            rows, pagination = document.get("leases"), document.get("pagination")
            if (
                complete
                or not isinstance(rows, list)
                or len(rows) > 200
                or not isinstance(pagination, dict)
                or "next_key" not in pagination
                or "total" not in pagination
            ):
                raise _Held
            total = int(_number(pagination["total"]))
            cursor = pagination["next_key"]
            if total > 10_000 or (reported_total is not None and total != reported_total):
                raise _Held
            if cursor not in (None, "") and (not isinstance(cursor, str) or not rows):
                raise _Held
            if len(rows) != min(200, total - observed_rows):
                raise _Held
            for row in rows:
                lease = row.get("lease") if isinstance(row, dict) else None
                identity = lease.get("id") if isinstance(lease, dict) else None
                if (
                    not isinstance(identity, dict)
                    or identity.get("owner") != subject.owner
                    or str(_chain_number(identity.get("dseq"))) != subject.dseq
                    or str(_chain_number(identity.get("gseq")))
                    not in {str(g.gseq) for g in groups}
                    or not is_canonical_akash_address(identity.get("provider"))
                ):
                    raise _Held
                _chain_number(identity.get("oseq"))
                _chain_number(identity.get("bseq"), zero=True)
            reported_total = total
            observed_rows += len(rows)
            if observed_rows > total or (cursor not in (None, "") and observed_rows >= total):
                raise _Held
            complete = cursor in (None, "")
            if complete and observed_rows != total:
                raise _Held
            return document

        leases = lease_snapshot(base, subject.dseq, subject.owner, read_url)
        if not complete or leases is not None and len(leases) != reported_total:
            raise _Held
        if leases is None or any(
            key[2] not in {str(g.gseq) for g in groups} or not is_canonical_akash_address(key[5])
            for key in leases
        ):
            raise _Held
        maps.append(leases)
    finished = clock()
    if maps[0] != maps[1] or not _time(evidence["observed_at"]) <= finished < _time(
        evidence["expires_at"]
    ):
        raise _Held
    closed = (
        infos[0][0] == "closed"
        and all(row[2] == "closed" for row in infos[0][1])
        and set(maps[0].values()).issubset(TERMINAL_STATES)
    )
    escrow = EscrowStatus.UNKNOWN
    if closed and infos[0][2] == infos[1][2]:
        escrow = {
            "closed": EscrowStatus.SETTLED,
            "overdrawn": EscrowStatus.OVERDRAWN_UNSETTLED,
        }.get(infos[0][2], EscrowStatus.UNKNOWN)
    population = tuple(sorted(maps[0].items()))
    return _Snapshot(
        evidence,
        ExecutionState.CLOSED if closed else ExecutionState.ACTIVE,
        escrow,
        population,
        _digest((evidence, infos, population)),
        int(finished.timestamp()),
    )


def _closed_at(
    subject: DeploymentKey,
    groups: tuple[PreparedGroup, ...],
    sources: tuple[dict, ...],
    height: int,
    budget: _Budget,
) -> bool:
    infos = tuple(_info(subject, groups, source, height, budget) for source in sources)
    if infos[0][:2] != infos[1][:2]:
        raise _Held
    return infos[0][0] == "closed"


def _signed_close(source: dict, height: int, subject: DeploymentKey, budget: _Budget) -> tuple:
    # The existing SDK exhausts the exact block, validates totals, canonical raw
    # transaction bytes/hashes and decoded fingerprints. Never scan the wallet.
    block = chain._creation_block_population(budget.read, source, height)
    if block is None:
        raise _Held
    matches = []
    for index, tx in enumerate(block["txs"]):
        body = tx.get("body")
        messages = body.get("messages") if isinstance(body, dict) else None
        if not isinstance(messages, list) or any(not isinstance(msg, dict) for msg in messages):
            raise _Held
        for message in messages:
            key = message.get("id")
            if (
                isinstance(message.get("@type"), str)
                and re.fullmatch(
                    r"/akash\.deployment\.v1beta[1-4]\.MsgCloseDeployment", message["@type"]
                )
                and isinstance(key, dict)
                and key.get("owner") == subject.owner
                and _number(key.get("dseq")) == subject.dseq
            ):
                txhash = block["raw_hashes"][index]
                document = budget.read(
                    "/cosmos/tx/v1beta1/txs/" + txhash, base=source["url"], height=height
                )
                response, observed_tx = document.get("tx_response"), document.get("tx")
                signatures = tx.get("signatures")
                auth = tx.get("auth_info")
                signers = auth.get("signer_infos") if isinstance(auth, dict) else None
                if (
                    not isinstance(response, dict)
                    or type(response.get("code")) is not int
                    or response["code"] != 0
                    or response.get("txhash") != txhash
                    or _number(response.get("height")) != str(height)
                    or chain._canonical_document_hash(observed_tx) != block["fingerprints"][index]
                    or not isinstance(signatures, list)
                    or not signatures
                    or any(chain._canonical_base64_bytes(sig) is None for sig in signatures)
                    or not isinstance(signers, list)
                    or len(signers) != len(signatures)
                    or any(not isinstance(signer, dict) for signer in signers)
                ):
                    raise _Held
                matches.append((height, txhash, index))
    if len(matches) != 1:
        raise _Held
    return (
        block["block_hash"],
        block["raw_hashes"],
        block["fingerprints"],
        matches[0],
        block["block_time"],
    )


def _recover_close(
    snapshot: _Snapshot,
    subject: DeploymentKey,
    groups: tuple[PreparedGroup, ...],
    sources: tuple[dict, ...],
    budget: _Budget,
) -> CloseTransactionProof:
    low, high = snapshot.evidence["creation_height"], snapshot.evidence["height"]
    # Deployment closure is terminal. Find the first closed finalized state,
    # corroborating every probe at the same height on the two fixed trust paths.
    if not _closed_at(subject, groups, sources, low, budget):
        for _step in range(MAX_HISTORY_STEPS):
            if high - low <= 1:
                break
            middle = (low + high) // 2
            if _closed_at(subject, groups, sources, middle, budget):
                high = middle
            else:
                low = middle
        else:
            raise _Held
    else:
        high = low
    if not _closed_at(subject, groups, sources, high, budget):
        raise _Held
    proofs = tuple(_signed_close(source, high, subject, budget) for source in sources)
    if proofs[0] != proofs[1]:
        raise _Held
    match = proofs[0][3]
    close_time = proofs[0][4]
    if (
        not _time(snapshot.evidence["creation_block_time"])
        <= close_time
        <= _time(snapshot.evidence["block_time"])
    ):
        raise _Held
    if (
        high == snapshot.evidence["creation_height"]
        and proofs[0][0] != snapshot.evidence["creation_block_hash"]
    ):
        raise _Held
    return CloseTransactionProof(
        high,
        match[1],
        match[2],
        proofs[0][0],
        close_time.isoformat(),
        _digest((*proofs[0][:3], close_time.isoformat())),
        (sources[0]["source_id"], sources[1]["source_id"]),
    )


def observe_execution(
    operation_id: str,
    subject: DeploymentKey,
    groups: tuple[PreparedGroup, ...],
    *,
    recover_close_transaction: bool = True,
    _reader: Callable | None = None,
    _clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    _monotonic: Callable[[], float] = time.monotonic,
) -> ExecutionObservation:
    """Observe exact generic groups; recover closure without any mutation capability.

    The underscore dependencies are internal fault-test boundaries. Default
    transport is bounded, redirect-free, height-echo verified and credential-free.
    Missing history retains a positive dated closed snapshot but cannot fabricate
    the core's mandatory close transaction height or authorize accounting release.
    """
    if (
        not isinstance(operation_id, str)
        or not operation_id.isascii()
        or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", operation_id) is None
        or not isinstance(subject, DeploymentKey)
        or type(recover_close_transaction) is not bool
    ):
        raise ValueError("typed exact operation and deployment are required")
    canonical_group_population_digest(groups)
    if len(groups) > MAX_GROUPS:
        raise ValueError("group observation exceeds its bound")
    observed = _clock()
    if observed.tzinfo is None:
        raise ValueError("observation clock requires a timezone")
    try:
        sources = _registry()
    except Exception:  # noqa: BLE001 — refusal cannot expose environment/transport text
        logger.warning("execution observation held: registry.invalid")
        return ExecutionObservation(
            operation_id,
            subject,
            ExecutionState.UNKNOWN,
            EscrowStatus.UNKNOWN,
            None,
            None,
            ObservationReason.REGISTRY_INVALID,
            None,
            int(observed.timestamp()),
            len(groups),
            None,
            None,
            0,
        )
    budget = _Budget(sources, _reader, _monotonic)
    try:
        snapshot = _sample(subject, groups, sources, budget, _clock)
    except Exception:  # noqa: BLE001 — emit typed unknown, never raw exception contents
        logger.warning("execution observation held: execution.unknown")
        return ExecutionObservation(
            operation_id,
            subject,
            ExecutionState.UNKNOWN,
            EscrowStatus.UNKNOWN,
            None,
            None,
            ObservationReason.EXECUTION_UNKNOWN,
            None,
            int(_clock().timestamp()),
            len(groups),
            None,
            None,
            budget.reads,
        )
    closure = None
    close_transaction = None
    reason = (
        ObservationReason.EXECUTION_CLOSED
        if snapshot.state is ExecutionState.CLOSED
        else ObservationReason.EXECUTION_ACTIVE
    )
    if snapshot.state is ExecutionState.CLOSED and recover_close_transaction:
        try:
            close_transaction = _recover_close(snapshot, subject, groups, sources, budget)
            close_height = close_transaction.height
            # Historical lookup may outlive the first short proof lease. Obtain a
            # new complete finalized snapshot before constructing durable evidence.
            fresh = _sample(subject, groups, sources, budget, _clock)
            if (
                fresh.state is not ExecutionState.CLOSED
                or fresh.evidence["height"] < close_height
                or not _time(fresh.evidence["creation_block_time"])
                <= _time(close_transaction.block_time)
                <= _time(fresh.evidence["block_time"])
            ):
                raise _Held
            snapshot = fresh
            closure = ExecutionClosure(
                operation_id=operation_id,
                subject=subject,
                chain_id=snapshot.evidence["chain_id"],
                proof_mode=ExecutionClosureProofMode.EXACT_FINALIZED_HEIGHT,
                source_a=sources[0]["url"],
                source_b=sources[1]["url"],
                operator_identity_a=sources[0]["operator"],
                operator_identity_b=sources[1]["operator"],
                trust_path_a=sources[0]["gateway_ancestry"] + "/" + sources[0]["cache_ancestry"],
                trust_path_b=sources[1]["gateway_ancestry"] + "/" + sources[1]["cache_ancestry"],
                operator_independence_verified=True,
                source_a_height=snapshot.evidence["height"],
                source_b_height=snapshot.evidence["height"],
                common_finality_height=snapshot.evidence["height"],
                close_transaction_height=close_height,
                group_population_digest=canonical_group_population_digest(groups),
                lease_population_digest=_digest(snapshot.leases),
                evidence_digest=_digest((snapshot.digest, close_transaction.__dict__)),
                observed_at=snapshot.observed_at,
            )
        except Exception:  # noqa: BLE001 — no guessed height or second close on lost history
            logger.warning("execution observation held: close_history.unknown")
            close_transaction = None
            reason = ObservationReason.CLOSE_HISTORY_UNKNOWN
    settlement = None
    if snapshot.state is ExecutionState.CLOSED:
        settlement = SettlementEvidence(
            operation_id,
            subject,
            # Closed escrow is operational state, not a complete payment proof.
            # This read-only adapter measures no financial settlement contract.
            SettlementState.UNMEASURED,
            sources[0]["url"],
            sources[1]["url"],
            snapshot.digest,
            snapshot.observed_at,
        )
    return ExecutionObservation(
        operation_id,
        subject,
        snapshot.state,
        snapshot.escrow,
        closure,
        settlement,
        reason,
        snapshot.evidence["height"],
        snapshot.observed_at,
        len(groups),
        len(snapshot.leases),
        snapshot.digest,
        budget.reads,
        close_transaction,
    )
