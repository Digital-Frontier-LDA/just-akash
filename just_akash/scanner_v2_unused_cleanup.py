"""Fixed terminal no-create-fenced JIT retirement: no deployment/lease DELETE exists."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, cast

from .scanner_cleanup_transport import RunnerAPI
from .scanner_provenance import require
from .scanner_unused_cleanup_native import NativeGET, actor, locate_environment, producer
from .scanner_unused_cleanup_pipe import PipeUnusedRecorder
from .scanner_unused_cleanup_provenance import UnusedOperation, validate_actor, validate_bundle


class UnusedRemoval:
    def __init__(self, current, bundle, recorder, renew, runner, *, clock=time.monotonic):
        self.actor = validate_actor(current)
        self.bundle = validate_bundle(current, bundle)
        self.producer, self.target = bundle["producer"], bundle["target"]
        self.recorder, self.renew, self.runner, self.clock = recorder, renew, runner, clock
        self.attempted = False
        self.entered = False
        self.ack_received = False
        self.unknown = None
        self.operation = UnusedOperation(
            actor=current,
            producer=self.producer,
            target=self.target,
            kind="remove",
            recorder=recorder,
        )
        self.intent = {
            "returned_runner_id": self.target["returned_runner_id"],
            "runner_group_id": self.target["runner_group_id"],
            "source_record_sha256": self.target["no_create_record_sha256"],
        }

    def idle(self):
        identity, group = self.target["returned_runner_id"], self.target["runner_group_id"]
        self.runner.not_busy(identity, group)
        row = self.runner.runner(identity)
        if row is None:
            require(self.runner.absent(identity))
        else:
            # Positive unused fence does not authorize an unexpectedly active runner.
            require(
                row.get("id") == identity
                and row.get("busy") is False
                and row.get("status") == "offline"
            )

    def history(self):
        self.renew()
        return self.recorder.history(self.producer, self.target)

    def guard(self):
        require(not self.attempted)
        self.attempted = True
        start = self.clock()
        h = self.history()
        require(h["wire_clear"]["remove"] is True)
        held = [r for r in h["current_actor_records"] if r["record"]["phase"] == "UNKNOWN"]
        require(len(held) == 1 and self.unknown is not None)
        require(
            held[0]["artifact_id"] == cast(dict[str, Any], self.unknown)["artifact_id"]
            and held[0]["body_sha256"] == cast(dict[str, Any], self.unknown)["record_sha256"]
        )
        self.idle()
        require(self.clock() - start < 30)
        self.entered = True

    def run(self):
        h = self.history()
        self.idle()
        self.operation.no_wire(self.intent)
        identity = self.target["returned_runner_id"]
        if not self.runner.absent(identity):
            require(h["wire_clear"]["remove"] is True)
            self.unknown = self.operation.before_wire(self.intent)
            try:
                self.runner.remove(identity, self.guard)
                self.operation.ack(self.intent | {"response_received": True})
                self.ack_received = True
            except Exception:
                require(self.entered)
        self.renew()
        require(self.runner.absent(identity))
        return {
            "outcome": "UNUSED_JIT_REMOVED",
            "unused_jit_qualified": True,
            "registration_absent": True,
            "returned_runner_id": identity,
            "target_run_id": self.producer["run_id"],
            "target_run_attempt": self.producer["run_attempt"],
            "target_sdk_round": self.producer["sdk_round"],
            "remove_wire_attempted": self.entered,
            "remove_ack_received": self.ack_received,
        }


def cleanup(environment):
    locate_environment(environment)
    token = environment.get("GH_TOKEN", "")
    current = actor(environment, NativeGET(token))
    require(
        current == validate_actor(json.loads(environment.get("SCANNER_CLEANUP_ACTOR_JSON", "")))
    )
    recorder = PipeUnusedRecorder(current)
    bundle = validate_bundle(current, recorder.read_target())

    def renew():
        get = NativeGET(token)
        require(actor(environment, get) == current)
        producer(current, bundle["producer"], get, bundle["target"])

    renew()
    return UnusedRemoval(current, bundle, recorder, renew, RunnerAPI(token)).run()


def main():
    require(len(sys.argv) == 1)
    result = None
    try:
        result = cleanup(os.environ)
    except Exception:
        result = None
    if result is None:
        raise SystemExit("Unused JIT cleanup HOLD; no removal or retry authority")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        for key, value in result.items():
            text = str(value).lower() if type(value) is bool else str(value)
            require("\n" not in text and "\r" not in text)
            stream.write(key + "=" + text + "\n")


if __name__ == "__main__":
    main()
