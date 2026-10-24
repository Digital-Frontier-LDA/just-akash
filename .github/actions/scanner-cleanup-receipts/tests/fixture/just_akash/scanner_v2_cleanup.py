"""Actual typed cleanup pipe/phase chain; public synthetic effects only."""

import json
import os
from pathlib import Path

from .scanner_cleanup_pipe import PipeCleanupRecorder
from .scanner_cleanup_provenance import CleanupOperation


def main():
    if any(k.startswith("ACTIONS_") for k in os.environ) or "GITHUB_TOKEN" in os.environ:
        raise RuntimeError("Artifact runtime escaped into fixed controller")
    if os.environ.get("GH_TOKEN") != "PUBLIC_MANAGER_NONAUTH":
        raise RuntimeError("Hosted manager fixture was not preserved")
    actor = json.loads(os.environ["SCANNER_CLEANUP_ACTOR_JSON"])
    recorder = PipeCleanupRecorder(actor)
    bundle = recorder.read_target()
    producer, target = bundle["producer"], bundle["target"]
    ops = {
        kind: CleanupOperation(
            actor=actor, producer=producer, target=target, kind=kind, recorder=recorder
        )
        for kind in ("close", "remove")
    }
    details = {}
    for kind, op in ops.items():
        source = "deployment_ack_record_sha256" if kind == "close" else "jit_ack_record_sha256"
        details[kind] = {
            k: target[k] for k in ("owner", "dseq", "group_population", "returned_runner_id")
        }
        details[kind]["source_record_sha256"] = target[source]
        op.no_wire(details[kind])
    h = recorder.history(producer, target)
    if not h["wire_clear"]["close"]:
        raise RuntimeError("Prior close cannot be repeated")
    ack = ops["close"].before_wire(details["close"])
    h = recorder.history(producer, target)
    if not any(r["body_sha256"] == ack["record_sha256"] for r in h["current_actor_records"]):
        raise RuntimeError("Current phase is not the actual held acknowledgement")
    root = Path(os.environ["PUBLIC_FIXTURE_ROOT"])
    (root / "synthetic-effect.txt").write_text("ONE_PUBLIC_SYNTHETIC_CLOSE_NO_NETWORK\n")
    ops["close"].ack(dict(details["close"], response_received=True))
    (root / "completed.txt").write_text("COMPLETE\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback

        Path(os.environ["PUBLIC_FIXTURE_ROOT"]).joinpath("public-fixture-error.json").write_text(
            json.dumps(
                {
                    "exception_class": type(exc).__name__,
                    "frames": [
                        {"file": Path(x.filename).name, "function": x.name, "line": x.lineno}
                        for x in traceback.extract_tb(exc.__traceback__)
                    ],
                }
            )
        )
        raise SystemExit("PUBLIC_CLEANUP_FIXTURE_HOLD") from None
