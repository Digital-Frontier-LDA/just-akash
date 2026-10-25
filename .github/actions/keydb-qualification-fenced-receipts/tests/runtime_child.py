"""Real volatile handshake over actual FD pipe; marker substitutes runtime entry."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

from just_akash.keydb_qualification_fence import PreleaseFence
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
with PreleaseFence(b, minted, r, lambda: None) as fence:
    fence.may_create()
    Path(os.environ["PUBLIC_RUNTIME_MARKER"]).write_text(fence.state)
