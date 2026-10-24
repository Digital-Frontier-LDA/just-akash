"""PUBLIC fixture: actual pipe/removal engine, synthetic exact-ID-only transport."""

import json
import os
from pathlib import Path

from just_akash.scanner_unused_cleanup_pipe import PipeUnusedRecorder
from just_akash.scanner_v2_unused_cleanup import UnusedRemoval

actor = json.loads(os.environ["SCANNER_CLEANUP_ACTOR_JSON"])
recorder = PipeUnusedRecorder(actor)
bundle = recorder.read_target()
marker = Path(os.environ["PUBLIC_EFFECT_MARKER"])


class Runner:
    removed = False

    def not_busy(self, identity, group):
        assert identity == bundle["target"]["returned_runner_id"]
        assert group == bundle["target"]["runner_group_id"]

    def runner(self, identity):
        return None if self.removed else {"id": identity, "busy": False, "status": "offline"}

    def absent(self, identity):
        assert identity == bundle["target"]["returned_runner_id"]
        return self.removed

    def remove(self, identity, guard):
        assert not self.removed
        guard()
        marker.write_text(str(identity))
        self.removed = True


result = UnusedRemoval(actor, bundle, recorder, lambda: None, Runner()).run()
assert result["unused_jit_qualified"] is True
assert "cleanup_qualified" not in result and "dseq" not in result
