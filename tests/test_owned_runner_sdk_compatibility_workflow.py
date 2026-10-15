"""The isolated SDK release runs the original quality gates without paid deployment jobs."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compatible_release_keeps_every_original_quality_command_and_permission():
    original = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    compatible = yaml.safe_load(
        (ROOT / ".github/workflows/owned-runner-sdk-compatibility.yml").read_text()
    )
    expected = {"lint", "typecheck", "unit-tests", "symbol-presence-check"}
    assert set(compatible["jobs"]) == expected
    assert compatible["jobs"] == {key: original["jobs"][key] for key in expected}
    assert compatible["on"] == {"pull_request": {"branches": ["release/owned-runner-sdk-06589"]}}
    assert compatible["permissions"] == original["permissions"] == {"contents": "read"}
    assert compatible["concurrency"]["cancel-in-progress"] is False
