"""Scanner-v2 public phase protocol; acknowledged storage is a required host dependency.

Records and acknowledgements are recovery evidence, never native create/close authority.
There is intentionally no local-file or JSON-ACK transport fallback.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Literal, Protocol, TypedDict, cast

ROLE = "bb-native-trufflehog-v2"
IMAGE = (
    "docker.io/digitalfrontierunipessoallda/akash-runner@sha256:"
    "e71bf4be0e3a59a57ff791b0fc3bc3a31f216f66807b6ac41f1a23346321baa4"
)
PROVIDERS = frozenset(
    {
        "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk",
        "akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z",
        "akash1z9nr23cgweu45g2jktfx95v7g2xp8qlsa3ys2x",
    }
)
Kind = Literal["jit", "lease", "lease_bind"]
Phase = Literal["NO_WIRE", "UNKNOWN", "ACK"]


class ScannerProvenanceHeld(RuntimeError):
    """Fixed error text; storage prose/credentials never escape through diagnostics."""


def require(value: object) -> None:
    if not value:
        raise ScannerProvenanceHeld("Scanner-v2 provenance was not verified")


def positive(value: object) -> int:
    require(type(value) is int and 0 < cast(int, value) < 2**63)
    return cast(int, value)


def digest(value: object) -> str:
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", cast(str, value)) is not None)
    return cast(str, value)


def canonical_bytes(value: object) -> bytes:
    """Exact UTF-8 public record encoding shared with the hosted recorder."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def record_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def placement(run: int, attempt: int, sdk_round: int) -> str:
    positive(run)
    positive(attempt)
    require(type(sdk_round) is int and 1 <= sdk_round <= 3)
    return f"borduas-th-run-{run}-a{attempt}-r{sdk_round}-end"


def runner_name(run: int, attempt: int, sdk_round: int) -> str:
    placement(run, attempt, sdk_round)
    return f"dfci-bb-th-{run}-a{attempt}-r{sdk_round}"


def validate_binding(value: Mapping[str, object]) -> dict[str, Any]:
    if isinstance(value, ScannerBinding):
        value = value.to_dict()
    expected = {
        "repository",
        "repository_id",
        "workflow_path",
        "workflow_sha",
        "native_head_sha",
        "sdk_sha",
        "event",
        "ref",
        "run_id",
        "run_attempt",
        "sdk_round",
        "producer_job_id",
        "role",
        "image",
        "providers",
        "resources",
        "runner_name",
        "runner_label",
        "akash_group",
        "runner_group_id",
    }
    require(type(value) is dict and set(value) == expected)
    result = json.loads(canonical_bytes(value))
    require(
        result["repository"] == "Borduas-Holdings/Blazing-Back"
        and type(result["repository_id"]) is int
        and result["repository_id"] == 1071436278
    )
    require(result["workflow_path"] == ".github/workflows/secret-scanning.yml")
    require(result["event"] == "workflow_dispatch" and result["ref"] == "refs/heads/main")
    for key in ("workflow_sha", "native_head_sha", "sdk_sha"):
        require(
            type(result[key]) is str and re.fullmatch(r"[0-9a-f]{40}", result[key]) is not None
        )
    require(result["workflow_sha"] == result["native_head_sha"])
    run, attempt = positive(result["run_id"]), positive(result["run_attempt"])
    positive(result["producer_job_id"])
    positive(result["runner_group_id"])
    group = placement(run, attempt, result["sdk_round"])
    name = runner_name(run, attempt, result["sdk_round"])
    require(
        result["akash_group"] == group and result["runner_name"] == result["runner_label"] == name
    )
    require(result["role"] == ROLE and result["image"] == IMAGE)
    require(result["resources"] == {"cpu": "4", "memory": "16Gi", "storage": "96Gi", "count": 1})
    require(type(result["resources"].get("count")) is int)
    owners = result["providers"]
    require(
        type(owners) is list
        and len(owners) == 3
        and all(type(v) is str for v in owners)
        and set(owners) == PROVIDERS
    )
    return cast(dict[str, Any], result)


class ScannerBinding(Mapping[str, object]):
    """Captured public producer/profile fields; it does not authenticate the producer."""

    def __init__(self, values: Mapping[str, object]):
        self._values = validate_binding(dict(values))

    def __getitem__(self, name: str) -> object:
        return json.loads(canonical_bytes(self._values[name]))

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattr__(self, name: str) -> Any:
        if name in self._values:
            return json.loads(canonical_bytes(self._values[name]))
        raise AttributeError(name)

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(canonical_bytes(self._values)))


def producer_identity(binding: Mapping[str, object]) -> dict[str, object]:
    return {
        key: binding[key]
        for key in (
            "repository_id",
            "run_id",
            "run_attempt",
            "workflow_sha",
            "native_head_sha",
            "producer_job_id",
        )
    }


def profile_sha256(binding: Mapping[str, object]) -> str:
    return record_sha256(
        {
            key: binding[key]
            for key in (
                "role",
                "image",
                "resources",
                "providers",
                "runner_name",
                "runner_group_id",
                "akash_group",
                "sdk_sha",
            )
        }
    )


class DurableAck(TypedDict):
    schema: str
    backend: str
    operation_id: str
    phase: str
    record_sha256: str
    parent_record_sha256: str | None
    artifact_id: int
    artifact_digest: str
    record_readback_sha256: str
    producer: dict[str, object]
    exclusive: bool
    created: bool


class DurableRecorder(Protocol):
    def persist(self, record: dict[str, object]) -> DurableAck:
        """Exclusively publish immutable record, native-read back bytes/producer, then ACK.

        Concrete hosted backend must enforce this; this protocol is not an attestation.
        Existing phase objects, missing proof, partial writes or ambiguity must refuse.
        """
        ...


class MissingDurableRecorder:
    def persist(self, record: dict[str, object]) -> DurableAck:
        raise ScannerProvenanceHeld(
            "Scanner-v2 durable receipt transport is unavailable; no wire permitted"
        )


def validate_ack(record: Mapping[str, object], value: object) -> DurableAck:
    keys = set(DurableAck.__annotations__)
    require(type(value) is dict and set(cast(dict, value)) == keys)
    ack = cast(dict[str, Any], value)
    require(
        ack["schema"] == "just-akash/scanner-durable-ack/v1"
        and ack["backend"] == "github-actions-artifact/v4"
    )
    require(ack["operation_id"] == record["operation_id"] and ack["phase"] == record["phase"])
    expected = record_sha256(record)
    require(ack["record_sha256"] == ack["record_readback_sha256"] == expected)
    require(ack["parent_record_sha256"] == record["parent_record_sha256"])
    positive(ack["artifact_id"])
    require(
        type(ack["artifact_digest"]) is str
        and re.fullmatch(r"sha256:[0-9a-f]{64}", ack["artifact_digest"]) is not None
    )
    expected_producer = producer_identity(cast(Mapping[str, object], record["binding"]))
    require(type(ack["producer"]) is dict and set(ack["producer"]) == set(expected_producer))
    require(
        all(
            type(ack["producer"][key]) is type(value) and ack["producer"][key] == value
            for key, value in expected_producer.items()
        )
    )
    require(ack["exclusive"] is True and ack["created"] is True)
    return cast(DurableAck, json.loads(canonical_bytes(ack)))


def _details(
    binding: Mapping[str, Any], kind: Kind, phase: Phase, value: Mapping[str, object]
) -> dict[str, Any]:
    jit = {"runner_name", "runner_group_id", "labels", "request_body_sha256"}
    lease = {
        "owner",
        "group_population",
        "public_request_sha256",
        "wire_request_sha256",
        "returned_runner_id",
        "jit_ack_record_sha256",
        "deposit_usd",
    }
    lease_bind = {
        "owner",
        "dseq",
        "provider",
        "gseq",
        "returned_runner_id",
        "jit_ack_record_sha256",
        "deployment_ack_record_sha256",
        "public_request_sha256",
        "wire_request_sha256",
    }
    expected = (jit if kind == "jit" else lease if kind == "lease" else lease_bind) | (
        {"runner_id"}
        if kind == "jit" and phase == "ACK"
        else {"dseq"}
        if kind == "lease" and phase == "ACK"
        else {"lease_response_received"}
        if kind == "lease_bind" and phase == "ACK"
        else set()
    )
    require(type(value) is dict and set(value) == expected)
    result = json.loads(canonical_bytes(value))
    if kind == "jit":
        require(
            result["runner_name"] == binding["runner_name"]
            and result["runner_group_id"] == binding["runner_group_id"]
        )
        require(type(result["runner_group_id"]) is int)
        require(result["labels"] == ["self-hosted", "linux", "akash", binding["runner_label"]])
        digest(result["request_body_sha256"])
        if phase == "ACK":
            positive(result["runner_id"])
    else:
        from .address import is_canonical_akash_address

        require(type(result["owner"]) is str and is_canonical_akash_address(result["owner"]))
        if kind == "lease_bind":
            require(
                type(result["dseq"]) is str
                and re.fullmatch(r"[1-9][0-9]{0,19}", result["dseq"]) is not None
            )
            require(int(result["dseq"]) <= 2**64 - 1)
            require(type(result["provider"]) is str and result["provider"] in PROVIDERS)
            require(type(result["gseq"]) is int and result["gseq"] == 1)
            positive(result["returned_runner_id"])
            for key in (
                "jit_ack_record_sha256",
                "deployment_ack_record_sha256",
                "public_request_sha256",
                "wire_request_sha256",
            ):
                digest(result[key])
            if phase == "ACK":
                require(result["lease_response_received"] is True)
            return cast(dict[str, Any], result)
        require(result["group_population"] == [{"gseq": 1, "name": binding["akash_group"]}])
        require(type(result["group_population"][0]["gseq"]) is int)
        digest(result["public_request_sha256"])
        digest(result["wire_request_sha256"])
        digest(result["jit_ack_record_sha256"])
        positive(result["returned_runner_id"])
        require(result["deposit_usd"] == "6")
        if phase == "ACK":
            require(
                type(result["dseq"]) is str
                and re.fullmatch(r"[1-9][0-9]{0,19}", result["dseq"]) is not None
            )
            require(int(result["dseq"]) <= 2**64 - 1)
    return cast(dict[str, Any], result)


class ScannerOperation:
    """Single-use phase state. Storage ACK failure never silently narrows UNKNOWN."""

    def __init__(
        self, binding: Mapping[str, object], kind: Kind, recorder: DurableRecorder | None = None
    ):
        require(kind in ("jit", "lease", "lease_bind"))
        self._binding = validate_binding(binding)
        self._kind: Kind = kind
        self._recorder = recorder if recorder is not None else MissingDurableRecorder()
        self._operation_id = (
            f"scanner-v2-{self._binding['repository_id']}-{self._binding['run_id']}"
            f"-a{self._binding['run_attempt']}-r{self._binding['sdk_round']}-{kind}"
        )
        self._state = "NEW"
        self._record: dict[str, object] | None = None
        self._intent: dict[str, Any] | None = None
        self._ack: DurableAck | None = None

    @property
    def binding(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(canonical_bytes(self._binding)))

    @property
    def operation_id(self) -> str:
        return self._operation_id

    @property
    def public_record(self) -> dict[str, object]:
        require(self._record is not None)
        return cast(dict[str, object], json.loads(canonical_bytes(self._record)))

    def _persist(self, phase: Phase, details: Mapping[str, object]) -> DurableAck:
        data = _details(self._binding, self._kind, phase, details)
        if self._intent is not None:
            restored = dict(data)
            restored.pop(
                "runner_id"
                if self._kind == "jit"
                else "dseq"
                if self._kind == "lease"
                else "lease_response_received",
                None,
            )
            require(restored == self._intent)
        parent = record_sha256(self._record) if self._record is not None else None
        record: dict[str, object] = {
            "schema": "just-akash/scanner-operation/v2",
            "operation_id": self._operation_id,
            "kind": self._kind,
            "phase": phase,
            "phase_index": {"NO_WIRE": 0, "UNKNOWN": 1, "ACK": 2}[phase],
            "parent_record_sha256": parent,
            "profile_sha256": profile_sha256(self._binding),
            "binding": json.loads(canonical_bytes(self._binding)),
            "details": data,
            "authority": "candidate",
        }
        accepted: DurableAck | None = None
        try:
            accepted = validate_ack(
                record,
                self._recorder.persist(
                    cast(dict[str, object], json.loads(canonical_bytes(record)))
                ),
            )
        except Exception:
            accepted = None
        # Outside the handler: no transport body/reason or credential exception context.
        require(accepted is not None)
        self._record = record
        self._ack = accepted
        if phase == "NO_WIRE":
            self._intent = data
        return cast(DurableAck, json.loads(canonical_bytes(accepted)))

    def no_wire(self, details: Mapping[str, object]) -> DurableAck:
        require(self._state == "NEW")
        self._state = "HELD"
        ack = self._persist("NO_WIRE", details)
        self._state = "NO_WIRE"
        return ack

    def before_wire(self, details: Mapping[str, object]) -> DurableAck:
        require(self._state == "NO_WIRE")
        self._state = "UNKNOWN"
        return self._persist("UNKNOWN", details)

    def ack(self, details: Mapping[str, object]) -> DurableAck:
        require(self._state == "UNKNOWN")
        require(self._record is not None and self._record.get("phase") == "UNKNOWN")
        self._state = "HELD_UNKNOWN"
        ack = self._persist("ACK", details)
        self._state = "ACK"
        return ack


def validate_record(value: object) -> dict[str, Any]:
    """Strict public shape validation, without treating a record as authentication."""
    require(type(value) is dict)
    row = cast(dict[str, Any], value)
    require(
        set(row)
        == {
            "schema",
            "operation_id",
            "kind",
            "phase",
            "phase_index",
            "parent_record_sha256",
            "profile_sha256",
            "binding",
            "details",
            "authority",
        }
    )
    require(row["schema"] == "just-akash/scanner-operation/v2" and row["authority"] == "candidate")
    binding = validate_binding(row["binding"])
    kind, phase = row["kind"], row["phase"]
    require(kind in ("jit", "lease", "lease_bind") and phase in ("NO_WIRE", "UNKNOWN", "ACK"))
    expected = (
        f"scanner-v2-{binding['repository_id']}-{binding['run_id']}"
        f"-a{binding['run_attempt']}-r{binding['sdk_round']}-{kind}"
    )
    require(row["operation_id"] == expected)
    require(
        type(row["phase_index"]) is int
        and row["phase_index"] == {"NO_WIRE": 0, "UNKNOWN": 1, "ACK": 2}[phase]
    )
    if phase == "NO_WIRE":
        require(row["parent_record_sha256"] is None)
    else:
        digest(row["parent_record_sha256"])
    require(row["profile_sha256"] == profile_sha256(binding))
    _details(binding, kind, phase, row["details"])
    return cast(dict[str, Any], json.loads(canonical_bytes(row)))
