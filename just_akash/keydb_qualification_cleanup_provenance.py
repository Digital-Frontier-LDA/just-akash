"""Closed public cleanup journals: current actor and historical producer are distinct.

Artifact ACKs are evidence, not chain/registration/delete authority. No implicit retry.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Protocol, cast

from .address import is_canonical_akash_address
from .keydb_qualification_provenance import (
    canonical_bytes,
    digest,
    positive,
    record_sha256,
    require,
    validate_binding,
    validate_record,
)

ROOT_PATHS = {
    "normal": ".github/workflows/keydb-modules-parity.yml",
    "recover": ".github/workflows/keydb-modules-parity.yml",
}
STEP = "Run the acknowledged KeyDB qualification cleanup controller"
JOB_PREFIX = {
    "normal": (
        "Clean up owned KeyDB parity qualification / "
        # The target-specific slot is appended by the closed codec.
        "Close KeyDB qualification known identities "
    ),
    "recover": (
        "Recover owned KeyDB parity qualification / "
        # The target-specific slot is appended by the closed codec.
        "Close KeyDB qualification known identities "
    ),
}


def closed(value: object, keys: set[str]) -> dict[str, Any]:
    require(type(value) is dict and set(cast(dict, value)) == keys)
    return cast(dict[str, Any], json.loads(canonical_bytes(value)))


def validate_locator(value: object) -> dict[str, int]:
    row = closed(value, {"run_id", "run_attempt", "sdk_round"})
    positive(row["run_id"])
    positive(row["run_attempt"])
    require(type(row["sdk_round"]) is int and 1 <= row["sdk_round"] <= 3)
    return row


def slot(locator: object) -> str:
    row = validate_locator(locator)
    return f"keydb-qualification-v1-{row['run_id']}-a{row['run_attempt']}-r{row['sdk_round']}"


def validate_actor(value: object) -> dict[str, Any]:
    row = closed(
        value,
        {
            "repository",
            "repository_id",
            "mode",
            "event",
            "ref",
            "workflow_path",
            "workflow_sha",
            "native_head_sha",
            "sdk_sha",
            "run_id",
            "run_attempt",
            "job_id",
            "job_name",
            "step_name",
            "target_locator",
        },
    )
    require(
        row["repository"] == "Borduas-Holdings/blazing"
        and type(row["repository_id"]) is int
        and row["repository_id"] == 1074974924
    )
    require(row["mode"] in ROOT_PATHS and row["workflow_path"] == ROOT_PATHS[row["mode"]])
    require(row["event"] == "workflow_dispatch")
    require(row["ref"] == "refs/heads/codex/keydb-parity-owned-d8ab-20261024")
    for key in ("workflow_sha", "native_head_sha", "sdk_sha"):
        require(type(row[key]) is str and re.fullmatch(r"[0-9a-f]{40}", row[key]))
    for key in ("run_id", "run_attempt", "job_id"):
        positive(row[key])
    locator = validate_locator(row["target_locator"])
    if row["mode"] == "normal":
        require(
            row["run_id"] == locator["run_id"]
            and row["run_attempt"] == locator["run_attempt"]
            and row["workflow_sha"] == row["native_head_sha"]
        )
    else:
        require(row["run_id"] != locator["run_id"])
    require(row["workflow_sha"] == row["native_head_sha"])
    require(
        row["job_name"] == JOB_PREFIX[row["mode"]] + slot(locator) and row["step_name"] == STEP
    )
    return row


def validate_target(producer: Mapping[str, object], value: object) -> dict[str, Any]:
    b = validate_binding(dict(producer))
    row = closed(
        value,
        {
            "owner",
            "dseq",
            "returned_runner_id",
            "group_population",
            "jit_ack_record_sha256",
            "deployment_ack_record_sha256",
            "lease_ack_record_sha256",
        },
    )
    require(type(row["owner"]) is str and is_canonical_akash_address(row["owner"]))
    require(
        type(row["dseq"]) is str
        and re.fullmatch(r"[1-9][0-9]{0,19}", row["dseq"])
        and int(row["dseq"]) <= 2**64 - 1
    )
    positive(row["returned_runner_id"])
    require(
        row["group_population"] == [{"gseq": 1, "name": b["akash_group"]}]
        and type(row["group_population"][0]["gseq"]) is int
    )
    for key in ("jit_ack_record_sha256", "deployment_ack_record_sha256"):
        digest(row[key])
    if row["lease_ack_record_sha256"] is not None:
        digest(row["lease_ack_record_sha256"])
    return row


def validate_bundle(actor: object, value: object) -> dict[str, Any]:
    a = validate_actor(actor)
    row = closed(
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
    require(
        row["schema"] == "just-akash/keydb-qualification-cleanup-target/v1" and row["actor"] == a
    )
    b = validate_binding(row["producer"])
    target = validate_target(b, row["target"])
    require({k: b[k] for k in ("run_id", "run_attempt", "sdk_round")} == a["target_locator"])
    records = closed(row["records"], {"jit", "deployment", "lease"})
    for key, kind in (("jit", "jit"), ("deployment", "lease"), ("lease", "lease_bind")):
        if key == "lease" and records[key] is None:
            require(target["lease_ack_record_sha256"] is None)
            continue
        record = validate_record(records[key])
        require(
            (record["kind"] == kind or kind == "lease" and record["kind"] == "lease_reconcile")
            and record["phase"] == "ACK"
            and record["binding"] == b
        )
    j, d, lease_record = (records[k] for k in ("jit", "deployment", "lease"))
    require(
        j["details"]["runner_id"]
        == d["details"]["returned_runner_id"]
        == target["returned_runner_id"]
    )
    require(d["details"]["owner"] == target["owner"] and d["details"]["dseq"] == target["dseq"])
    require(
        d["details"]["jit_ack_record_sha256"]
        == target["jit_ack_record_sha256"]
        == record_sha256(j)
    )
    require(target["deployment_ack_record_sha256"] == record_sha256(d))
    if lease_record is not None:
        require(
            lease_record["details"]["owner"] == target["owner"]
            and lease_record["details"]["dseq"] == target["dseq"]
            and lease_record["details"]["returned_runner_id"] == target["returned_runner_id"]
        )
        require(
            lease_record["details"]["jit_ack_record_sha256"] == record_sha256(j)
            and lease_record["details"]["deployment_ack_record_sha256"] == record_sha256(d)
            and target["lease_ack_record_sha256"] == record_sha256(lease_record)
        )
        require(
            d["details"]["public_request_sha256"]
            == lease_record["details"]["public_request_sha256"]
            and d["details"]["wire_request_sha256"]
            == lease_record["details"]["wire_request_sha256"]
        )
    inventory = closed(row["phase_inventory"], {"jit", "deployment", "lease"})
    require(inventory["jit"] == inventory["deployment"] == ["NO_WIRE", "UNKNOWN", "ACK"])
    require(
        type(inventory["lease"]) is list
        and inventory["lease"] == ["NO_WIRE", "UNKNOWN", "ACK"][: len(inventory["lease"])]
    )
    require(
        len(inventory["lease"]) <= 3
        and (len(inventory["lease"]) == 3) == (lease_record is not None)
    )
    require(type(row["evidence"]) is list and len(row["evidence"]) == 6 + len(inventory["lease"]))
    seen = set()
    phases = set()
    for item in row["evidence"]:
        e = closed(
            item, {"id", "name", "digest", "size_in_bytes", "record_sha256", "kind", "phase"}
        )
        positive(e["id"])
        positive(e["size_in_bytes"])
        require(e["size_in_bytes"] <= 1048576 and e["id"] not in seen)
        seen.add(e["id"])
        require(type(e["digest"]) is str and re.fullmatch(r"sha256:[a-f0-9]{64}", e["digest"]))
        digest(e["record_sha256"])
        require(
            e["kind"] in ("jit", "lease", "lease_bind")
            and e["phase"] in ("NO_WIRE", "UNKNOWN", "ACK")
        )
        require((e["kind"], e["phase"]) not in phases)
        phases.add((e["kind"], e["phase"]))
        expected = (
            f"keydb-qualification-v1-{b['repository_id']}-{b['run_id']}-a{b['run_attempt']}"
            f"-r{b['sdk_round']}-{e['kind']}-j{b['producer_job_id']}-{e['phase'].lower()}"
        )
        require(e["name"] == expected)
        if e["phase"] == "ACK":
            name = {"jit": "jit", "lease": "deployment", "lease_bind": "lease"}[e["kind"]]
            require(e["record_sha256"] == record_sha256(records[name]))
    expected_phases = (
        {("jit", v) for v in inventory["jit"]}
        | {("lease", v) for v in inventory["deployment"]}
        | {("lease_bind", v) for v in inventory["lease"]}
    )
    require(phases == expected_phases)
    native = closed(
        row["native"],
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
        require(native[key] == b[field])
    require(
        native["producer_job_status"] == "completed"
        and native["actor_native_run_id"] == a["run_id"]
    )
    require(
        type(native["producer_created_at"]) is str and len(native["producer_created_at"]) <= 40
    )
    return row


def validate_cleanup_record(value: object) -> dict[str, Any]:
    row = closed(
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
    require(
        row["schema"] == "just-akash/keydb-qualification-cleanup-operation/v1"
        and row["authority"] == "candidate"
    )
    a = validate_actor(row["actor"])
    b = validate_binding(row["producer"])
    t = validate_target(b, row["target"])
    require({k: b[k] for k in ("run_id", "run_attempt", "sdk_round")} == a["target_locator"])
    require(row["target_sha256"] == record_sha256(t) and row["kind"] in ("close", "remove"))
    require(row["operation_id"] == slot(a["target_locator"]) + "-" + row["kind"])
    indexes = {"NO_WIRE": 0, "UNKNOWN": 1, "ACK": 2}
    require(
        row["phase"] in indexes
        and type(row["phase_index"]) is int
        and row["phase_index"] == indexes[row["phase"]]
    )
    require(
        row["parent_record_sha256"] is None
        if row["phase"] == "NO_WIRE"
        else bool(digest(row["parent_record_sha256"]))
    )
    details = closed(
        row["details"],
        {"owner", "dseq", "group_population", "returned_runner_id", "source_record_sha256"}
        | ({"response_received"} if row["phase"] == "ACK" else set()),
    )
    for key in ("owner", "dseq", "group_population", "returned_runner_id"):
        require(details[key] == t[key])
    source_key = (
        "deployment_ack_record_sha256" if row["kind"] == "close" else "jit_ack_record_sha256"
    )
    require(details["source_record_sha256"] == t[source_key])
    if row["phase"] == "ACK":
        require(details["response_received"] is True)
    require(len(canonical_bytes(row)) <= 65536)
    return row


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
        row["schema"] == "just-akash/keydb-qualification-cleanup-durable-ack/v1"
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


class CleanupOperation:
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
                schema="just-akash/keydb-qualification-cleanup-operation/v1",
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
