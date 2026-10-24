"""Actual pre-token organization target is part of scanner admission, not a label."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "scanner_original_public_fixture", ROOT / "tests/test_runner_scanner_caller.py"
)
assert spec is not None and spec.loader is not None
original = importlib.util.module_from_spec(spec)
spec.loader.exec_module(original)


@pytest.mark.parametrize(
    "step",
    [
        original.subject.STEP,
        "Preflight — the runner PAT must still be valid",
        "Provision (retry down candidates, tag before waiting)",
    ],
)
@pytest.mark.parametrize("org", ["", None, "PUBLIC_FOREIGN_ORG", "Borduas-Holdings "])
def test_actual_target_refuses_before_any_metadata_or_token_request(step, org):
    env, data, calls, get = original.fixture()
    env["ORG"] = org
    env["SCANNER_ADMISSION_STEP"] = step
    data[original.subject.ROOT + "/actions/jobs/88"]["steps"][0]["name"] = step
    with pytest.raises(original.subject.CallerHeld):
        original.subject.admit(env, get)
    assert calls == []


def test_every_actual_early_and_posting_step_binds_the_same_input():
    workflow = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())
    selected = [
        row
        for row in workflow["jobs"]["pool"]["steps"]
        if row.get("id") in ("scanner_caller", "pat", "provision")
    ]
    assert len(selected) == 3
    assert all(row["env"]["ORG"] == "${{ inputs.github-org }}" for row in selected)
    assert all("runner_scanner_caller.py" in row["run"] for row in selected)
    pat = next(row for row in selected if row["id"] == "pat")["run"]
    assert "orgs/${ORG}/actions/runners/registration-token" in pat
    provision = next(row for row in selected if row["id"] == "provision")["run"]
    assert 'RUNNER_COLLECTION="orgs/${ORG}/actions/runners"' in provision
    assert (
        "verify_scanner_caller || exit 1\n  "
        'RESP=$(gh api --method POST "${RUNNER_COLLECTION}/registration-token"'
    ) in provision


def test_all_actual_registration_token_posts_have_native_admission_before_them():
    import re

    workflow = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())
    steps = workflow["jobs"]["pool"]["steps"]
    sites = []
    for step in steps:
        run = step.get("run", "")
        for match in re.finditer(
            r"(?m)^([^\n]*gh api --method POST [^\n]*/registration-token[^\n]*)$", run
        ):
            sites.append((step["id"], match.group(1).strip()))
            if step["id"] == "pat":
                assert run.index("namespace[") < match.start()
            else:
                assert step["id"] == "provision"
                assert run[: match.start()].rstrip().endswith("verify_scanner_caller || exit 1")
    assert len(sites) == 3
    assert [key for key, _ in sites] == ["pat", "provision", "provision"]
    assert "VERDICT_RESP=" in sites[-1][1]


@pytest.mark.parametrize("profile", ["", "bb-ce1", "blazing-aaf"])
def test_actual_optional_guard_is_a_noop_for_unchanged_roles(profile, tmp_path):
    import subprocess

    workflow = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())
    run = next(s for s in workflow["jobs"]["pool"]["steps"] if s.get("id") == "provision")["run"]
    start = run.index("verify_scanner_caller() {")
    end = run.index("\n# Fail closed before the first possible submit.", start)
    actual_function = run[start:end]
    marker = tmp_path / "PUBLIC_INTERPRETER_CALL"
    script = 'set -euo pipefail\npython3() { printf "CALLED" >> "$PUBLIC_MARKER"; return 99; }\n'
    script += actual_function + "\nverify_scanner_caller\n"
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env=dict(PATH="/usr/bin:/bin", RUNNER_PRIVATE_PROFILE=profile, PUBLIC_MARKER=str(marker)),
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 0
    assert not marker.exists()
