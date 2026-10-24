"""Actual public protocol validation; no SDK initializer, actors or network."""

import importlib
import json
import sys
import types
from pathlib import Path


def main():
    directory = Path(__file__).parent
    production = directory.parents[3] / "just_akash"
    component = directory.parents[1] / "source" / "just_akash"
    source = production if (production / "scanner_provenance.py").is_file() else component
    if not (source / "scanner_provenance.py").is_file():
        raise AssertionError("The actual public schema source was not present")
    namespace = types.ModuleType("just_akash")
    namespace.__dict__["__path__"] = [str(source)]
    sys.modules["just_akash"] = namespace
    protocol = importlib.import_module("just_akash.scanner_provenance")
    values = json.loads(sys.stdin.buffer.read())
    records = []
    for value in values:
        validated = protocol.validate_record(value)
        if protocol.canonical_bytes(validated) != protocol.canonical_bytes(value):
            raise AssertionError("Canonical public record changed")
        records.append(
            {
                "raw": protocol.canonical_bytes(value).decode(),
                "sha256": protocol.record_sha256(value),
            }
        )
    sys.stdout.write(json.dumps(records))


if __name__ == "__main__":
    main()
