"""Exact returned-JIT-only cleanup after a positive parent-attested no-create fence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from . import scanner_cleanup_provenance as K
from . import scanner_provenance as P
from .scanner_prelease_fence import INTENT, TERMINAL, fence_record

TARGET = "just-akash/scanner-unused-jit-target/v1"
BUNDLE = "just-akash/scanner-unused-jit-bundle/v1"
OPERATION = "just-akash/scanner-unused-jit-remove/v1"
closed = K.closed
validate_locator = K.validate_locator
slot = K.slot
ROOT_PATHS = K.ROOT_PATHS
JOB_PREFIX = K.JOB_PREFIX
STEP = K.STEP
canonical_bytes = P.canonical_bytes
digest = P.digest
positive = P.positive
record_sha256 = P.record_sha256
require = P.require
validate_binding = P.validate_binding


def validate_actor(value):
    a = K.validate_actor(value)
    P.require(a["mode"] == "terminal")
    return a


def validate_target(producer, value):
    b = P.validate_binding(producer)
    t = closed(
        value,
        {
            "schema",
            "returned_runner_id",
            "runner_group_id",
            "jit_ack_record_sha256",
            "no_create_record_sha256",
        },
    )
    P.require(t["schema"] == TARGET and t["runner_group_id"] == b["runner_group_id"])
    P.positive(t["returned_runner_id"])
    P.positive(t["runner_group_id"])
    P.digest(t["jit_ack_record_sha256"])
    P.digest(t["no_create_record_sha256"])
    return t


def validate_bundle(actor, value):
    a = validate_actor(actor)
    v = closed(
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
    P.require(v["schema"] == BUNDLE and v["actor"] == a)
    b = P.validate_binding(v["producer"])
    P.require(b["sdk_sha"] == a["sdk_sha"])
    P.require({k: b[k] for k in a["target_locator"]} == a["target_locator"])
    t = validate_target(b, v["target"])
    r = closed(v["records"], {"jit", "fence_intent", "no_create"})
    j, f, n = (
        P.validate_record(r["jit"]),
        fence_record(r["fence_intent"]),
        fence_record(r["no_create"]),
    )
    P.require(j["kind"] == "jit" and j["phase"] == "ACK" and j["binding"] == b)
    P.require(
        f["schema"] == INTENT and n["schema"] == TERMINAL and f["binding"] == n["binding"] == b
    )
    P.require(n["parent_record_sha256"] == P.record_sha256(f))
    P.require(f["parent_record_sha256"] == P.record_sha256(j))
    P.require(
        t["returned_runner_id"]
        == j["details"]["runner_id"]
        == f["details"]["returned_runner_id"]
        == n["details"]["returned_runner_id"]
    )
    P.require(
        f["details"]
        == {
            k: val
            for k, val in n["details"].items()
            if k not in ("child_exit_code", "child_signal", "create_frames_reserved")
        }
    )
    P.require(
        t["jit_ack_record_sha256"] == P.record_sha256(j) == f["details"]["jit_ack_record_sha256"]
    )
    P.require(t["no_create_record_sha256"] == P.record_sha256(n))
    P.require(
        v["phase_inventory"]
        == {
            "jit": ["NO_WIRE", "UNKNOWN", "ACK"],
            "lease": ["FENCE", "NO_CREATE"],
            "lease_bind": [],
        }
    )
    P.require(type(v["evidence"]) is list and len(v["evidence"]) == 5)
    phases, ids = set(), set()
    for item in v["evidence"]:
        e = closed(
            item, {"id", "name", "digest", "size_in_bytes", "record_sha256", "kind", "phase"}
        )
        P.positive(e["id"])
        P.positive(e["size_in_bytes"])
        P.require(e["id"] not in ids and e["size_in_bytes"] <= 1048576)
        ids.add(e["id"])
        P.require(type(e["digest"]) is str and e["digest"].startswith("sha256:"))
        P.digest(e["digest"][7:])
        P.digest(e["record_sha256"])
        pair = e["kind"], e["phase"]
        P.require(
            pair
            in {
                ("jit", "NO_WIRE"),
                ("jit", "UNKNOWN"),
                ("jit", "ACK"),
                ("lease", "FENCE"),
                ("lease", "NO_CREATE"),
            }
            and pair not in phases
        )
        phases.add(pair)
        name = (
            f"scanner-v2-{b['repository_id']}-{b['run_id']}-a{b['run_attempt']}"
            f"-r{b['sdk_round']}-{e['kind']}-j{b['producer_job_id']}-{e['phase'].lower()}"
        )
        P.require(e["name"] == name)
        for pair2, record in [
            (("jit", "ACK"), j),
            (("lease", "FENCE"), f),
            (("lease", "NO_CREATE"), n),
        ]:
            if pair == pair2:
                P.require(e["record_sha256"] == P.record_sha256(record))
    native = closed(
        v["native"],
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
        P.require(native[key] == b[field])
    P.require(
        native["producer_job_status"] == "completed"
        and native["actor_native_run_id"] == a["run_id"]
    )
    return v


def validate_cleanup_record(value):
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
        },
    )
    a, b = validate_actor(r["actor"]), P.validate_binding(r["producer"])
    t = validate_target(b, r["target"])
    P.require(r["schema"] == OPERATION and r["kind"] == "remove" and r["authority"] == "candidate")
    P.require({k: b[k] for k in a["target_locator"]} == a["target_locator"])
    P.require(
        r["operation_id"] == slot(a["target_locator"]) + "-remove"
        and r["target_sha256"] == P.record_sha256(t)
    )
    phases = {"NO_WIRE": 0, "UNKNOWN": 1, "ACK": 2}
    P.require(
        r["phase"] in phases
        and type(r["phase_index"]) is int
        and r["phase_index"] == phases[r["phase"]]
    )
    P.require(
        r["parent_record_sha256"] is None
        if r["phase"] == "NO_WIRE"
        else bool(P.digest(r["parent_record_sha256"]))
    )
    d = closed(
        r["details"],
        {"returned_runner_id", "runner_group_id", "source_record_sha256"}
        | ({"response_received"} if r["phase"] == "ACK" else set()),
    )
    P.require(
        d["returned_runner_id"] == t["returned_runner_id"]
        and d["runner_group_id"] == t["runner_group_id"]
    )
    P.require(d["source_record_sha256"] == t["no_create_record_sha256"])
    if r["phase"] == "ACK":
        P.require(d["response_received"] is True)
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


class CleanupRecorder(Protocol):
    def persist(self, record: dict[str, Any]) -> dict[str, Any]: ...


class UnusedOperation:
    """One actor phase chain. Poison before persistence; no implicit unknown reconciliation."""

    def __init__(
        self,
        *,
        actor: object,
        producer: Mapping[str, object],
        target: object,
        kind: str,
        recorder: CleanupRecorder,
    ):
        self.actor = validate_actor(actor)
        self.producer = validate_binding(dict(producer))
        self.target = validate_target(self.producer, target)
        require(kind == "remove")
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
                schema=OPERATION,
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
