"""PUBLIC offline fixture: real ScannerOperation/Pipe, synthetic irreversible counter only."""

import json
import os
from pathlib import Path

from .scanner_durable_pipe import PipeDurableRecorder
from .scanner_provenance import ScannerOperation


def main():
    if any(key.startswith("ACTIONS_") for key in os.environ) or "GITHUB_TOKEN" in os.environ:
        raise AssertionError("Artifact runtime leaked into the public fixture")
    expected = os.environ["PUBLIC_EXPECTED_MANAGER"]
    if os.environ["GH_TOKEN"] != expected:
        raise AssertionError("Hosted manager environment was not preserved")
    root = Path(os.environ["PUBLIC_FIXTURE_ROOT"])
    values = json.loads((root / "public-record-fixture.json").read_bytes())
    op = ScannerOperation(values["binding"], "jit", PipeDurableRecorder())
    op.no_wire(values["details"])
    op.before_wire(values["details"])
    # Only reached after actual durable ACK validation for NO_WIRE and UNKNOWN.
    (root / "synthetic-wire.txt").write_text("ONE_SYNTHETIC_EFFECT_NO_NETWORK\n")
    op.ack(dict(values["details"], runner_id=991))
    (root / "controller-completed.txt").write_text("COMPLETE\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("PUBLIC_FIXTURE_CONTROLLER_HOLD") from None
