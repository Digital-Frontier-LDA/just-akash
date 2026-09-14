"""Frozen executable population for repository Akash close entry points."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name("fixtures") / "akash_close_sites.json"
SCRIPT = ROOT / "scripts" / "census_akash_close_sites.py"


def _module():
    spec = importlib.util.spec_from_file_location("census_akash_close_sites", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_close_site_population_matches_the_reviewed_manifest():
    actual = _module().census(ROOT)
    expected = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert actual, "close-site scan found zero calls; that is a broken scan, not a clean repo"
    assert sum(row["count"] for row in actual) >= 30
    assert actual == expected, (
        "executable Akash close-site population changed; review the added/removed site and "
        "update the manifest in the same commit"
    )


def test_census_has_a_python_and_shell_planted_positive(tmp_path):
    (tmp_path / "just_akash").mkdir()
    (tmp_path / "canary").mkdir()
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "just_akash" / "closer.py").write_text(
        "def reap(client, dseq):\n    client.close_deployment(dseq)\n",
        encoding="utf-8",
    )
    (tmp_path / ".github" / "workflows" / "close.yml").write_text(
        "jobs:\n  close:\n    steps:\n"
        "      - run: just-akash destroy --dseq 7\n"
        '      - run: "${JA[@]}" destroy --dseq 8\n',
        encoding="utf-8",
    )
    actual = _module().census(tmp_path)
    assert actual == [
        {
            "kind": "python",
            "path": "just_akash/closer.py",
            "scope": "reap",
            "callee": "close_deployment",
            "count": 1,
        },
        {
            "kind": "shell",
            "path": ".github/workflows/close.yml",
            "scope": "",
            "callee": "destroy",
            "count": 2,
        },
    ]


def test_census_does_not_count_file_close_or_comments(tmp_path):
    (tmp_path / "just_akash").mkdir()
    (tmp_path / "canary").mkdir()
    (tmp_path / ".github").mkdir()
    (tmp_path / "just_akash" / "files.py").write_text(
        "def finish(stream):\n    stream.close()\n",
        encoding="utf-8",
    )
    (tmp_path / "Justfile").write_text("# just-akash destroy --dseq 7\n", encoding="utf-8")
    assert _module().census(tmp_path) == []
