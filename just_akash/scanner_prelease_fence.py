"""Positive pure-preparation fence. Never infer no create from missing records."""

from __future__ import annotations

import signal
from collections.abc import Callable
from typing import Any, cast

from . import scanner_provenance as P

PROFILE = "bb-native-trufflehog-v2-fenced"
INTENT = "just-akash/scanner-no-create-intent/v1"
TERMINAL = "just-akash/scanner-no-create-terminal/v1"
EXIT = 66
STAGES = {"guest_contract", "private_scratch", "reader_path_preflight"}
RUNTIME = "just-akash/scanner-may-create-reservation/v1"
RUNTIME_ACK = "just-akash/scanner-may-create-ack/v1"


def runtime_reservation(value: object) -> dict[str, Any]:
    """A live-parent control request, never a durable producer artifact."""
    P.require(type(value) is dict)
    row = dict(cast(dict[str, Any], value))
    P.require(
        set(row)
        == {
            "schema",
            "operation_id",
            "binding",
            "producer_profile",
            "returned_runner_id",
            "jit_ack_record_sha256",
            "state",
        }
    )
    b = P.validate_binding(row["binding"])
    P.require(
        row["schema"] == RUNTIME
        and row["producer_profile"] == PROFILE
        and row["state"] == "MAY_CREATE"
    )
    P.require(
        row["operation_id"]
        == (
            f"scanner-v2-{b['repository_id']}-{b['run_id']}-a{b['run_attempt']}-r{b['sdk_round']}-lease"
        )
    )
    P.positive(row["returned_runner_id"])
    P.digest(row["jit_ack_record_sha256"])
    P.require(len(P.canonical_bytes(row)) <= 65536)
    return row


def validate_runtime_ack(request: object, value: object) -> dict[str, Any]:
    request = runtime_reservation(request)
    P.require(type(value) is dict)
    ack = dict(cast(dict[str, Any], value))
    P.require(
        set(ack)
        == {
            "schema",
            "operation_id",
            "request_sha256",
            "binding_sha256",
            "state",
            "acknowledged",
            "scope",
        }
    )
    P.require(ack["schema"] == RUNTIME_ACK and ack["operation_id"] == request["operation_id"])
    P.require(ack["request_sha256"] == P.record_sha256(request))
    P.require(ack["binding_sha256"] == P.record_sha256(request["binding"]))
    P.require(ack["state"] == "MAY_CREATE" and ack["acknowledged"] is True)
    P.require(ack["scope"] == "CURRENT_PARENT_SESSION_ONLY")
    return ack


def fence_record(value: object) -> dict[str, Any]:
    require = P.require
    require(type(value) is dict)
    row = dict(cast(dict[str, Any], value))
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
    b = P.validate_binding(row["binding"])
    require(row["kind"] == "prelease_fence" and row["authority"] == "candidate")
    require(
        row["operation_id"]
        == (
            f"scanner-v2-{b['repository_id']}-{b['run_id']}-a{b['run_attempt']}"
            f"-r{b['sdk_round']}-lease"
        )
    )
    require(row["profile_sha256"] == P.profile_sha256(b))
    terminal = row["schema"] == TERMINAL
    require(terminal or row["schema"] == INTENT)
    require(row["phase"] == ("NO_CREATE" if terminal else "FENCE"))
    require(type(row["phase_index"]) is int and row["phase_index"] == (1 if terminal else 0))
    d = row["details"]
    keys = {
        "producer_profile",
        "returned_runner_id",
        "jit_ack_record_sha256",
        "stage",
        "create_state",
    }
    require(
        type(d) is dict
        and set(d)
        == keys
        | ({"child_exit_code", "child_signal", "create_frames_reserved"} if terminal else set())
    )
    require(
        d["producer_profile"] == PROFILE and d["stage"] in STAGES and d["create_state"] == "FENCED"
    )
    P.positive(d["returned_runner_id"])
    P.digest(d["jit_ack_record_sha256"])
    P.digest(row["parent_record_sha256"])
    if terminal:
        require(type(d["child_exit_code"]) is int and d["child_exit_code"] == EXIT)
        require(
            d["child_signal"] is None
            and type(d["create_frames_reserved"]) is int
            and d["create_frames_reserved"] == 0
        )
    else:
        require(row["parent_record_sha256"] == d["jit_ack_record_sha256"])
    require(len(P.canonical_bytes(row)) <= 65536)
    return row


def validate_ack(record: dict[str, object], value: object) -> Any:
    if record.get("schema") not in (INTENT, TERMINAL):
        return P.validate_ack(record, value)
    r = fence_record(record)
    P.require(type(value) is dict)
    a = dict(cast(dict[str, Any], value))
    P.require(
        set(a)
        == {
            "schema",
            "backend",
            "operation_id",
            "phase",
            "record_sha256",
            "parent_record_sha256",
            "artifact_id",
            "artifact_digest",
            "record_readback_sha256",
            "producer",
            "exclusive",
            "created",
        }
    )
    P.require(
        a["schema"] == "just-akash/scanner-durable-ack/v1"
        and a["backend"] == "github-actions-artifact/v4"
    )
    P.require(a["exclusive"] is True and a["created"] is True)
    for key in ("operation_id", "phase", "parent_record_sha256"):
        P.require(a[key] == r[key])
    P.require(a["record_sha256"] == a["record_readback_sha256"] == P.record_sha256(r))
    P.positive(a["artifact_id"])
    P.require(type(a["artifact_digest"]) is str and a["artifact_digest"].startswith("sha256:"))
    P.digest(a["artifact_digest"][7:])
    expected = {
        "repository_id": r["binding"]["repository_id"],
        "run_id": r["binding"]["run_id"],
        "run_attempt": r["binding"]["run_attempt"],
        "workflow_sha": r["binding"]["workflow_sha"],
        "native_head_sha": r["binding"]["native_head_sha"],
        "producer_job_id": r["binding"]["producer_job_id"],
    }
    P.require(a["producer"] == expected)
    return a


class Interrupted(BaseException):
    """Signals can never become an ordinary preparation exception/fence."""


class PreparationFenced(Exception):
    """Expected fixed exit; parent attestation still required after child death."""


class PreleaseFence:
    def __init__(self, binding, minted, recorder, admission):
        self.binding = P.validate_binding(binding.to_dict())
        record = P.validate_record(minted.public_record)
        P.validate_ack(record, minted.ack)
        P.require(
            record["phase"] == "ACK"
            and record["kind"] == "jit"
            and record["binding"] == self.binding
        )
        P.require(record["details"]["runner_id"] == minted.handoff.runner_id)
        self.jit_sha = P.record_sha256(record)
        self.runner_id = minted.handoff.runner_id
        self.recorder, self.admission = recorder, admission
        self.state = "PURE_PREPARATION"
        self.signalled = False
        self.handlers = {}

    def __enter__(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, self.interrupt)
        return self

    def __exit__(self, *_args):
        for sig, handler in self.handlers.items():
            signal.signal(sig, handler)

    def interrupt(self, *_args):
        self.signalled = True
        self.state = "HELD"
        raise Interrupted()

    def may_create(self):
        # Irreversible BEFORE imports/helper/deploy/NO_WIRE/UNKNOWN, not after wire.
        P.require(self.state == "PURE_PREPARATION" and not self.signalled)
        self.state = "MAY_CREATE"
        b = self.binding
        request = runtime_reservation(
            {
                "schema": RUNTIME,
                "operation_id": (
                    f"scanner-v2-{b['repository_id']}-{b['run_id']}-a{b['run_attempt']}"
                    f"-r{b['sdk_round']}-lease"
                ),
                "binding": b,
                "producer_profile": PROFILE,
                "returned_runner_id": self.runner_id,
                "jit_ack_record_sha256": self.jit_sha,
                "state": "MAY_CREATE",
            }
        )
        # The parent poisons its own fence capability before replying. No helper
        # import/unlock follows a missing, partial or mismatched control ACK.
        reserve = getattr(self.recorder, "reserve_runtime", None)
        P.require(callable(reserve))
        validate_runtime_ack(request, cast("Callable[[dict[str, Any]], object]", reserve)(request))
        P.require(self.state == "MAY_CREATE" and not self.signalled)

    def failure(self, stage: str):
        P.require(stage in STAGES and self.state == "PURE_PREPARATION" and not self.signalled)
        self.state = "FENCED"  # poison BEFORE any attestation request/ACK
        self.admission()
        b = self.binding
        record = fence_record(
            {
                "schema": INTENT,
                "operation_id": (
                    f"scanner-v2-{b['repository_id']}-{b['run_id']}-a{b['run_attempt']}"
                    f"-r{b['sdk_round']}-lease"
                ),
                "kind": "prelease_fence",
                "phase": "FENCE",
                "phase_index": 0,
                "parent_record_sha256": self.jit_sha,
                "profile_sha256": P.profile_sha256(b),
                "binding": b,
                "details": {
                    "producer_profile": PROFILE,
                    "returned_runner_id": self.runner_id,
                    "jit_ack_record_sha256": self.jit_sha,
                    "stage": stage,
                    "create_state": "FENCED",
                },
                "authority": "candidate",
            }
        )
        validate_ack(record, self.recorder.persist(record))
        self.admission()
        P.require(self.state == "FENCED" and not self.signalled)
        raise PreparationFenced()
