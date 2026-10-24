"""Versioned, public terminal derivation. UNKNOWN stays UNKNOWN; no synthetic ACK.

The witness is read-only DATA. Each destructive wire still needs fresh native,
complete Console, signed-chain, wallet and original full-closure authority.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol, cast

from . import scanner_cleanup_provenance as known
from .scanner_create_observation import dseq, manifest_version, validate_baseline
from .scanner_provenance import (
    canonical_bytes,
    digest,
    positive,
    record_sha256,
    require,
    validate_binding,
    validate_record,
)

closed = known.closed
validate_actor = known.validate_actor
validate_locator = known.validate_locator
slot = known.slot
TARGET = "just-akash/scanner-cleanup-derived-target/v1"
SOURCE = "just-akash/scanner-cleanup-recovery-source/v1"
BUNDLE = "just-akash/scanner-cleanup-recovered-target/v1"
WITNESS = "just-akash/scanner-cleanup-readonly-derivation/v1"
OPERATION = "just-akash/scanner-cleanup-derived-operation/v1"
IDENTITY_KEYS = {
    "chain_id",
    "registry_digest",
    "owner",
    "dseq",
    "groups",
    "creation_manifest_version",
    "creation_height",
    "creation_block_hash",
    "creation_txhash",
    "creation_tx_index",
}
PROOF_KEYS = IDENTITY_KEYS | {
    "evidence_version",
    "registry_version",
    "registry_provenance",
    "registry_provenance_digest",
    "source_ids",
    "height",
    "block_hash",
    "block_time",
    "creation_block_time",
    "gseq",
    "group",
    "population_count",
    "population_digest",
    "observed_at",
    "evaluated_at",
    "expires_at",
    "current_manifest_version",
}


def derived(value: object) -> bool:
    return type(value) is dict and value.get("schema") == TARGET


def identity(value: object) -> dict[str, Any]:
    p = closed(value, PROOF_KEYS)
    require(p["evidence_version"] == 3 and type(p["evidence_version"]) is int)
    require(p["chain_id"] == "akashnet-2" and p["source_ids"] == ["allnodes", "polkachu"])
    require(type(p["registry_version"]) is int and p["registry_version"] == 4)
    require(
        p["registry_digest"] == "ce7726373109617d75c04e5027d8a9ea131c3868d069345d358cad9f96741dbf"
    )
    require(
        p["registry_provenance"]
        == (
            "https://github.com/cosmos/chain-registry/blob/"
            "cc1ed04b31326bc79f3208e571fb71fa0fddb987/akash/chain.json"
        )
    )
    require(
        p["registry_provenance_digest"]
        == "3720728bb01b964132cf1452678c6aaf20f0f5b2df098e2d56ceb577e741b1c2"
    )
    for key in ("height", "creation_height"):
        positive(p[key])
    require(type(p["creation_tx_index"]) is int and p["creation_tx_index"] >= 0)
    for key in ("block_hash", "creation_block_hash", "creation_txhash"):
        require(type(p[key]) is str and re.fullmatch(r"[0-9A-F]{64}", p[key]))
    for key in ("registry_digest", "registry_provenance_digest", "population_digest"):
        digest(p[key])
    for key in ("block_time", "creation_block_time", "observed_at", "evaluated_at", "expires_at"):
        require(type(p[key]) is str and 1 <= len(p[key]) <= 40)
        require(datetime.fromisoformat(p[key]).tzinfo is not None)
    require(p["population_count"] == 1 and type(p["population_count"]) is int)
    require(p["gseq"] == "1" and p["group"] == p["groups"][0]["name"])
    require(p["creation_manifest_version"] == p["current_manifest_version"])
    manifest_version(p["creation_manifest_version"])
    return {k: p[k] for k in sorted(IDENTITY_KEYS)}


def validate_target(producer: Mapping[str, object], value: object) -> dict[str, Any]:
    if not derived(value):
        return known.validate_target(producer, value)
    b = validate_binding(producer)
    t = closed(
        value,
        {
            "schema",
            "owner",
            "dseq",
            "returned_runner_id",
            "group_population",
            "jit_ack_record_sha256",
            "deployment_unknown_record_sha256",
            "expected_manifest_version",
            "precreate_baseline_sha256",
            "creation_identity",
        },
    )
    require(t["schema"] == TARGET)
    # Reuse canonical owner/group validation without manufacturing an ACK field.
    from .address import is_canonical_akash_address

    require(type(t["owner"]) is str and is_canonical_akash_address(t["owner"]))
    dseq(t["dseq"])
    positive(t["returned_runner_id"])
    require(t["group_population"] == [{"gseq": 1, "name": b["akash_group"]}])
    require(type(t["group_population"][0]["gseq"]) is int)
    for key in (
        "jit_ack_record_sha256",
        "deployment_unknown_record_sha256",
        "precreate_baseline_sha256",
    ):
        digest(t[key])
    manifest_version(t["expected_manifest_version"])
    i = closed(t["creation_identity"], IDENTITY_KEYS)
    for key in ("owner", "dseq"):
        require(i[key] == t[key])
    require(i["groups"] == t["group_population"] and i["chain_id"] == "akashnet-2")
    require(i["creation_manifest_version"] == t["expected_manifest_version"])
    digest(i["registry_digest"])
    positive(i["creation_height"])
    require(type(i["creation_tx_index"]) is int and i["creation_tx_index"] >= 0)
    for key in ("creation_block_hash", "creation_txhash"):
        require(type(i[key]) is str and re.fullmatch(r"[0-9A-F]{64}", i[key]))
    return t


def validate_source(actor: object, value: object) -> dict[str, Any]:
    a = validate_actor(actor)
    require(a["mode"] == "terminal")
    r = closed(
        value,
        {
            "schema",
            "actor",
            "producer",
            "target",
            "records",
            "phase_inventory",
            "evidence",
            "native",
        },
    )
    require(r["schema"] == SOURCE and r["actor"] == a and r["target"] is None)
    b = validate_binding(r["producer"])
    require({k: b[k] for k in a["target_locator"]} == a["target_locator"])
    records = closed(r["records"], {"jit", "deployment", "lease"})
    j, u = validate_record(records["jit"]), validate_record(records["deployment"])
    require(j["kind"] == "jit" and j["phase"] == "ACK" and j["binding"] == b)
    require(u["kind"] == "lease_reconcile" and u["phase"] == "UNKNOWN" and u["binding"] == b)
    require(records["lease"] is None and "dseq" not in u["details"])
    require(u["details"]["returned_runner_id"] == j["details"]["runner_id"])
    require(u["details"]["jit_ack_record_sha256"] == record_sha256(j))
    validate_baseline(u["details"]["precreate_baseline"], u["details"]["owner"])
    manifest_version(u["details"]["expected_manifest_version"])
    require(
        r["phase_inventory"]
        == {
            "jit": ["NO_WIRE", "UNKNOWN", "ACK"],
            "deployment": ["NO_WIRE", "UNKNOWN"],
            "lease": [],
        }
    )
    require(type(r["evidence"]) is list and len(r["evidence"]) == 5)
    expected = {
        ("jit", "NO_WIRE"),
        ("jit", "UNKNOWN"),
        ("jit", "ACK"),
        ("lease", "NO_WIRE"),
        ("lease", "UNKNOWN"),
    }
    seen, ids = set(), set()
    for row in r["evidence"]:
        e = closed(
            row, {"id", "name", "digest", "size_in_bytes", "record_sha256", "kind", "phase"}
        )
        positive(e["id"])
        positive(e["size_in_bytes"])
        require(e["id"] not in ids and e["size_in_bytes"] <= 1048576)
        ids.add(e["id"])
        require(type(e["digest"]) is str and re.fullmatch(r"sha256:[a-f0-9]{64}", e["digest"]))
        digest(e["record_sha256"])
        pair = e["kind"], e["phase"]
        require(pair in expected and pair not in seen)
        seen.add(pair)
        name = (
            f"scanner-v2-{b['repository_id']}-{b['run_id']}-a{b['run_attempt']}"
            f"-r{b['sdk_round']}-{e['kind']}-j{b['producer_job_id']}-{e['phase'].lower()}"
        )
        require(e["name"] == name)
        if pair == ("jit", "ACK"):
            require(e["record_sha256"] == record_sha256(j))
        if pair == ("lease", "UNKNOWN"):
            require(e["record_sha256"] == record_sha256(u))
    n = closed(
        r["native"],
        {
            "producer_job_id",
            "producer_run_id",
            "producer_attempt",
            "producer_head_sha",
            "producer_sdk_sha",
            "producer_created_at",
            "producer_job_status",
            "actor_native_run_id",
        },
    )
    for key, field in (
        ("producer_job_id", "producer_job_id"),
        ("producer_run_id", "run_id"),
        ("producer_attempt", "run_attempt"),
        ("producer_head_sha", "native_head_sha"),
        ("producer_sdk_sha", "sdk_sha"),
    ):
        require(n[key] == b[field])
    require(n["producer_job_status"] == "completed" and n["actor_native_run_id"] == a["run_id"])
    require(type(n["producer_created_at"]) is str and 1 <= len(n["producer_created_at"]) <= 40)
    return r


def validate_witness(source: object, value: object) -> dict[str, Any]:
    w = closed(
        value,
        {
            "schema",
            "actor",
            "producer",
            "source_bundle_sha256",
            "target",
            "console_snapshot_sha256",
            "creation_proof",
            "authority",
        },
    )
    a = validate_actor(w["actor"])
    s = validate_source(a, source)
    require(w["schema"] == WITNESS and w["authority"] == "READONLY_DERIVATION")
    require(w["actor"] == s["actor"] and w["producer"] == s["producer"])
    require(w["source_bundle_sha256"] == record_sha256(s))
    digest(w["console_snapshot_sha256"])
    t = validate_target(s["producer"], w["target"])
    require(derived(t))
    u, j = s["records"]["deployment"], s["records"]["jit"]
    require(t["deployment_unknown_record_sha256"] == record_sha256(u))
    for key in ("owner", "group_population", "expected_manifest_version"):
        require(t[key] == u["details"][key])
    require(t["precreate_baseline_sha256"] == record_sha256(u["details"]["precreate_baseline"]))
    require(t["dseq"] not in u["details"]["precreate_baseline"]["dseqs"])
    require(
        t["jit_ack_record_sha256"] == record_sha256(j)
        and t["returned_runner_id"] == j["details"]["runner_id"]
    )
    require(identity(w["creation_proof"]) == t["creation_identity"])
    require(w["creation_proof"]["groups"] == t["group_population"])
    require(len(canonical_bytes(w)) <= 65536)
    return w


def validate_bundle(actor: object, value: object) -> dict[str, Any]:
    require(type(value) is dict)
    value = cast(dict[str, Any], value)
    if value.get("schema") == SOURCE:
        return validate_source(actor, value)
    if value.get("schema") != BUNDLE:
        return known.validate_bundle(actor, value)
    r = closed(
        value,
        {
            "schema",
            "actor",
            "producer",
            "target",
            "records",
            "phase_inventory",
            "evidence",
            "native",
            "derivation",
        },
    )
    source = {k: v for k, v in r.items() if k != "derivation"}
    source.update(schema=SOURCE, target=None)
    validate_source(actor, source)
    e = closed(r["derivation"], {"record", "artifact_id", "artifact_digest", "body_sha256"})
    positive(e["artifact_id"])
    require(
        type(e["artifact_digest"]) is str
        and re.fullmatch(r"sha256:[a-f0-9]{64}", e["artifact_digest"])
    )
    w = validate_witness(source, e["record"])
    require(w["target"] == r["target"] and e["body_sha256"] == record_sha256(w))
    return r


def compatible(prior: object, bundle: dict[str, Any]) -> bool:
    """No schema/target-hash escape: only a real later ACK can bridge its UNKNOWN."""
    p, t = validate_target(bundle["producer"], prior), bundle["target"]
    if p == t:
        return True
    if not derived(p) or derived(t):
        return False
    d = bundle["records"]["deployment"]
    return (
        d["kind"] == "lease_reconcile"
        and d["phase"] == "ACK"
        and d["parent_record_sha256"] == p["deployment_unknown_record_sha256"]
        and all(
            p[k] == t[k]
            for k in (
                "owner",
                "dseq",
                "returned_runner_id",
                "group_population",
                "jit_ack_record_sha256",
            )
        )
        and p["expected_manifest_version"] == d["details"]["expected_manifest_version"]
        and p["precreate_baseline_sha256"] == record_sha256(d["details"]["precreate_baseline"])
    )


def validate_cleanup_record(value: object) -> dict[str, Any]:
    require(type(value) is dict)
    value = cast(dict[str, Any], value)
    if value.get("schema") != OPERATION:
        return known.validate_cleanup_record(value)
    r = closed(
        value,
        {
            "schema",
            "operation_id",
            "kind",
            "phase",
            "phase_index",
            "parent_record_sha256",
            "actor",
            "producer",
            "target",
            "target_sha256",
            "details",
            "authority",
            "derivation_evidence",
        },
    )
    a, b = validate_actor(r["actor"]), validate_binding(r["producer"])
    t = validate_target(b, r["target"])
    require(derived(t) and a["mode"] == "terminal" and r["authority"] == "candidate")
    require({k: b[k] for k in a["target_locator"]} == a["target_locator"])
    require(
        r["kind"] in ("close", "remove")
        and r["operation_id"] == slot(a["target_locator"]) + "-" + r["kind"]
    )
    phases = {"NO_WIRE": 0, "UNKNOWN": 1, "ACK": 2}
    require(
        r["phase"] in phases
        and type(r["phase_index"]) is int
        and r["phase_index"] == phases[r["phase"]]
    )
    require(
        r["parent_record_sha256"] is None
        if r["phase"] == "NO_WIRE"
        else bool(digest(r["parent_record_sha256"]))
    )
    require(r["target_sha256"] == record_sha256(t))
    e = closed(r["derivation_evidence"], {"artifact_id", "artifact_digest", "record_sha256"})
    positive(e["artifact_id"])
    require(
        type(e["artifact_digest"]) is str
        and re.fullmatch(r"sha256:[a-f0-9]{64}", e["artifact_digest"])
    )
    digest(e["record_sha256"])
    d = closed(
        r["details"],
        {"owner", "dseq", "group_population", "returned_runner_id", "source_record_sha256"}
        | ({"response_received"} if r["phase"] == "ACK" else set()),
    )
    require(all(d[k] == t[k] for k in ("owner", "dseq", "group_population", "returned_runner_id")))
    require(
        d["source_record_sha256"]
        == t[
            "deployment_unknown_record_sha256" if r["kind"] == "close" else "jit_ack_record_sha256"
        ]
    )
    if r["phase"] == "ACK":
        require(d["response_received"] is True)
    require(len(canonical_bytes(r)) <= 65536)
    return r


def validate_cleanup_ack(record: object, value: object) -> dict[str, Any]:
    r = validate_cleanup_record(record)
    row = closed(
        value,
        {
            "schema",
            "backend",
            "operation_id",
            "phase",
            "record_sha256",
            "parent_record_sha256",
            "artifact_id",
            "artifact_digest",
            "record_readback_sha256",
            "actor",
            "producer",
            "target_sha256",
            "history_sha256",
            "created",
        },
    )
    require(
        row["schema"] == "just-akash/scanner-cleanup-durable-ack/v1"
        and row["backend"] == "github-actions-artifact/v4"
        and row["created"] is True
    )
    for key in (
        "operation_id",
        "phase",
        "parent_record_sha256",
        "actor",
        "producer",
        "target_sha256",
    ):
        require(row[key] == r[key])
    positive(row["artifact_id"])
    digest(row["history_sha256"])
    require(type(row["artifact_digest"]) is str and row["artifact_digest"][:7] == "sha256:")
    digest(row["artifact_digest"][7:])
    require(row["record_sha256"] == row["record_readback_sha256"] == record_sha256(r))
    return row


class CleanupRecorder(known.CleanupRecorder, Protocol):
    def persist(self, record: dict[str, Any]) -> dict[str, Any]: ...


class RecoveryOperation:
    """One actor phase chain. Poison before persistence; no implicit unknown reconciliation."""

    def __init__(
        self,
        *,
        actor: object,
        producer: Mapping[str, object],
        target: object,
        kind: str,
        recorder: CleanupRecorder,
        derivation_evidence: object = None,
    ):
        self.derivation_evidence = derivation_evidence
        self.actor = validate_actor(actor)
        self.producer = validate_binding(dict(producer))
        self.target = validate_target(self.producer, target)
        require(kind in ("close", "remove"))
        self.kind = kind
        self.recorder = recorder
        self._index = -1
        self._sha = None
        self._intent = None
        self._held = False

    def _phase(self, phase: str, details: dict[str, Any]) -> dict[str, Any]:
        require(not self._held)
        self._held = True
        index = {"NO_WIRE": 0, "UNKNOWN": 1, "ACK": 2}[phase]
        require(index == self._index + 1)
        intent = {k: v for k, v in details.items() if k != "response_received"}
        require(self._intent is None or self._intent == intent)
        row = validate_cleanup_record(
            dict(
                schema=OPERATION
                if derived(self.target)
                else "just-akash/scanner-cleanup-operation/v1",
                operation_id=slot(self.actor["target_locator"]) + "-" + self.kind,
                kind=self.kind,
                phase=phase,
                phase_index=index,
                parent_record_sha256=self._sha,
                actor=self.actor,
                producer=self.producer,
                target=self.target,
                target_sha256=record_sha256(self.target),
                details=details,
                authority="candidate",
                **(
                    {"derivation_evidence": self.derivation_evidence}
                    if derived(self.target)
                    else {}
                ),
            )
        )
        ack = validate_cleanup_ack(row, self.recorder.persist(row))
        self._index = index
        self._sha = record_sha256(row)
        self._intent = intent
        self._held = False
        return ack

    def no_wire(self, details: dict[str, Any]) -> dict[str, Any]:
        return self._phase("NO_WIRE", details)

    def before_wire(self, details: dict[str, Any]) -> dict[str, Any]:
        return self._phase("UNKNOWN", details)

    def ack(self, details: dict[str, Any]) -> dict[str, Any]:
        return self._phase("ACK", details)
