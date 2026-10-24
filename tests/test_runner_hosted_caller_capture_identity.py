"""The actual captured entry accepts read-only atime movement, retaining stable identity."""

from __future__ import annotations

import builtins
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class Metadata:
    """Keep every original native attribute; change exactly one requested observation."""

    def __init__(self, original: os.stat_result, changed: str):
        self.original = original
        self.changed = changed

    def __getattr__(self, name: str) -> Any:
        value = getattr(self.original, name)
        if self.changed == "atime" and name in {"st_atime", "st_atime_ns"}:
            return value + (1_000_000_000 if name.endswith("_ns") else 1)
        if name == self.changed:
            return value + 1
        return value


@pytest.mark.parametrize("changed", ["atime", "st_mtime_ns", "st_ctime_ns", "st_ino"])
def test_actual_entry_accepts_only_read_only_metadata_variation(changed, tmp_path, monkeypatch):
    workflow = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_bytes())
    step = next(s for s in workflow["jobs"]["pool"]["steps"] if s.get("id") == "native_caller")
    inline = step["run"].split("<<'PYCALLER'\n", 1)[1].split("\nPYCALLER", 1)[0]
    target = tmp_path / "just_akash/runner_hosted_caller.py"
    target.parent.mkdir()
    target.write_bytes((ROOT / "just_akash/runner_hosted_caller.py").read_bytes())
    before = target.stat()
    after = Metadata(before, changed)
    original_fstat, original_lstat = os.fstat, Path.lstat
    reads = 0
    calls = []

    def fstat(fd):
        nonlocal reads
        value = original_fstat(fd)
        if (value.st_dev, value.st_ino) == (before.st_dev, before.st_ino):
            reads += 1
            return before if reads == 1 else after
        return value

    def lstat(path):
        value = original_lstat(path)
        if (value.st_dev, value.st_ino) == (before.st_dev, before.st_ino):
            return before if reads <= 1 else after
        return value

    def captured_exec(code, namespace):
        builtins.exec(code, namespace)
        namespace["main"] = lambda: calls.append("captured fixed helper main")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(Path, "lstat", lstat)
    if changed == "atime":
        builtins.exec(compile(inline, "actual-workflow-capture", "exec"), {"exec": captured_exec})
        assert calls == ["captured fixed helper main"]
    else:
        with pytest.raises(SystemExit):
            builtins.exec(
                compile(inline, "actual-workflow-capture", "exec"), {"exec": captured_exec}
            )
        assert calls == []
