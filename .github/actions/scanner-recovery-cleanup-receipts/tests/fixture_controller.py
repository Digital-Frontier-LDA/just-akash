"""Actual new pipe/phase functions; only the final wire is a public sentinel file."""

import json
import os
from pathlib import Path

from just_akash.scanner_recovery_pipe import PipeRecoveryRecorder
from just_akash.scanner_recovery_provenance import RecoveryOperation


def main():
    root = Path(os.environ["PUBLIC_FIXTURE_ROOT"])
    if any(k.startswith("ACTIONS_") for k in os.environ) or "GITHUB_TOKEN" in os.environ:
        raise ValueError("PUBLIC_TOKEN_BOUNDARY")
    actor = json.loads(os.environ["SCANNER_CLEANUP_ACTOR_JSON"])
    recorder = PipeRecoveryRecorder(actor)
    source = recorder.read_target()
    witness = json.loads((root / "public-witness.json").read_bytes())
    bundle = recorder.admit_derivation(witness)
    if source["records"]["deployment"]["phase"] != "UNKNOWN":
        raise ValueError("PUBLIC_UNKNOWN_LOST")
    e, t = bundle["derivation"], bundle["target"]
    reference = {
        "artifact_id": e["artifact_id"],
        "artifact_digest": e["artifact_digest"],
        "record_sha256": e["body_sha256"],
    }
    operations, intents = {}, {}
    for kind in ("close", "remove"):
        operations[kind] = RecoveryOperation(
            actor=actor,
            producer=bundle["producer"],
            target=t,
            kind=kind,
            recorder=recorder,
            derivation_evidence=reference,
        )
        intents[kind] = {
            k: t[k] for k in ("owner", "dseq", "group_population", "returned_runner_id")
        }
        intents[kind]["source_record_sha256"] = t[
            "deployment_unknown_record_sha256" if kind == "close" else "jit_ack_record_sha256"
        ]
        operations[kind].no_wire(intents[kind])
    ack = operations["close"].before_wire(intents["close"])
    h = recorder.history(bundle["producer"], t)
    if h["wire_clear"]["close"] is not True or not any(
        x["artifact_id"] == ack["artifact_id"] and x["body_sha256"] == ack["record_sha256"]
        for x in h["current_actor_records"]
    ):
        raise ValueError("PUBLIC_MISSING_HELD_UNKNOWN")
    (root / "synthetic-effect.txt").write_text("ONE_PUBLIC_SENTINEL_NO_NETWORK\n")
    operations["close"].ack(intents["close"] | {"response_received": True})


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("PUBLIC_RECOVERY_PIPE_HOLD") from None
