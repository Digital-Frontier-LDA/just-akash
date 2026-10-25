"""Actual public Pipe/Fence functions, no provider/native mutation/network."""

import json
import os
from types import SimpleNamespace

from just_akash.keydb_qualification_fence import Interrupted, PreleaseFence, PreparationFenced
from just_akash.keydb_qualification_fenced_pipe import PipeFencedRecorder
from just_akash.keydb_qualification_provenance import KeyDBOperation, KeyDBQualificationBinding

b = KeyDBQualificationBinding(json.loads(os.environ["PUBLIC_BINDING"]))
r = PipeFencedRecorder()
op = KeyDBOperation(b, "jit", r)
d = {
    "runner_name": b["runner_name"],
    "runner_group_id": b["runner_group_id"],
    "labels": ["self-hosted", "linux", "akash", b["runner_label"]],
    "request_body_sha256": "0" * 64,
}
op.no_wire(d)
op.before_wire(d)
a = op.ack(d | {"runner_id": 991})
minted = SimpleNamespace(
    public_record=op.public_record, ack=a, handoff=SimpleNamespace(runner_id=991)
)
if os.environ["PUBLIC_MODE"] == "legacy":
    raise SystemExit(0)
f = PreleaseFence(b, minted, r, lambda: None)
try:
    with f:
        mode = os.environ["PUBLIC_MODE"]
        if mode == "started":
            f.may_create()
        if mode == "signal":
            f.interrupt()
        if mode == "sigterm":
            import signal

            os.kill(os.getpid(), signal.SIGTERM)
        f.failure("private_scratch")
except PreparationFenced:
    if mode == "wrong-exit":
        raise SystemExit(1) from None
    if mode == "sigterm-after-intent":
        import signal

        os.kill(os.getpid(), signal.SIGTERM)
    if mode == "killed-after-intent":
        import signal

        os.kill(os.getpid(), signal.SIGKILL)
    raise SystemExit(66) from None
except Interrupted:
    raise SystemExit(77) from None
except Exception:
    raise SystemExit(1) from None
