"""Execute both real CI step bodies under hostile inherited provider settings."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from just_akash._e2e import assert_provider_in_tiers, resolve_tiers
from just_akash.deploy import _resolve_tier

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
OWNED = [
    "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk",
    "akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z",
    "akash1z9nr23cgweu45g2jktfx95v7g2xp8qlsa3ys2x",
]
FOREIGN = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
CASES = [
    ("e2e-shell", "Run E2E lease-shell test", "just_akash.test_shell_e2e"),
    ("e2e-secrets", "Run E2E secrets test", "just_akash.test_secrets_e2e"),
]


def _job_and_step(job_id: str, step_name: str) -> tuple[dict, dict]:
    document = yaml.safe_load(WORKFLOW.read_text())
    job = document["jobs"][job_id]
    matching = [step for step in job["steps"] if step.get("name") == step_name]
    assert len(matching) == 1
    return job, matching[0]


@pytest.mark.parametrize(("job_id", "step_name", "module"), CASES)
@pytest.mark.parametrize("module_status", [0, 73], ids=["success", "failure"])
def test_actual_module_process_cannot_inherit_foreign_provider_fallback(
    job_id, step_name, module, module_status, tmp_path, monkeypatch
):
    """Replace only uv's execution boundary; execute the actual unmodified shell body."""
    _job, step = _job_and_step(job_id, step_name)
    capture = tmp_path / "capture.json"
    helper = tmp_path / "capture.py"
    helper.write_text(
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps({\n"
        " 'argv': sys.argv[1:],\n"
        " 'preferred': os.environ.get('AKASH_PROVIDERS'),\n"
        " 'backup': os.environ.get('AKASH_PROVIDERS_BACKUP'),\n"
        " 'deadline': os.environ.get('OWNER_LOOKUP_STEP_DEADLINE_AT'),\n"
        "}))\n"
        "raise SystemExit(int(os.environ['MODULE_STATUS']))\n"
    )
    shim = tmp_path / "uv"
    shim.write_text('#!/bin/bash\nexec "$CAPTURE_PYTHON" "$CAPTURE_HELPER" "$@"\n')
    shim.chmod(0o700)
    # Simulate values already loaded through SOPS/GITHUB_ENV. Do not load credentials.
    environment = {
        "PATH": str(tmp_path) + os.pathsep + os.defpath,
        "CAPTURE": str(capture),
        "CAPTURE_PYTHON": sys.executable,
        "CAPTURE_HELPER": str(helper),
        "MODULE_STATUS": str(module_status),
        "AKASH_PROVIDERS": FOREIGN,
        "AKASH_PROVIDERS_BACKUP": ",".join([FOREIGN, OWNED[0]]),
        "OWNER_LOOKUP_STEP_DEADLINE_AT": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    before = time.time()
    result = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    after = time.time()
    assert result.returncode == module_status, "original module failure must propagate"
    assert result.stdout == result.stderr == ""
    observed = json.loads(capture.read_text())
    assert observed["argv"] == ["run", "python", "-m", module]
    assert observed["preferred"] == ",".join(OWNED)
    assert observed["backup"] == "", "inherited SOPS backup must not widen selection"
    assert before + 30 * 60 - 240 - 1 <= int(observed["deadline"]) <= after + 30 * 60 - 240
    # Exercise the SAME original E2E and deploy resolvers with the actual child inputs.
    monkeypatch.setenv("AKASH_PROVIDERS", observed["preferred"])
    monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", observed["backup"])
    preferred, backup, union = resolve_tiers()
    assert preferred == union == OWNED
    assert backup == []
    assert _resolve_tier(None, "AKASH_PROVIDERS") == OWNED
    assert _resolve_tier(None, "AKASH_PROVIDERS_BACKUP") == []
    assert all(assert_provider_in_tiers(provider, preferred, backup) for provider in OWNED)
    assert not assert_provider_in_tiers(FOREIGN, preferred, backup)


@pytest.mark.parametrize(("job_id", "step_name", "_module"), CASES)
def test_scope_is_selected_after_sops_without_changing_stage_or_cleanup(
    job_id, step_name, _module
):
    job, step = _job_and_step(job_id, step_name)
    steps = job["steps"]
    execution = steps.index(step)
    sops = next(i for i, item in enumerate(steps) if item.get("name") == "Load SOPS secrets")
    receipt = steps[-1]
    assert sops < execution < len(steps) - 1
    assert receipt["if"] == "always()"
    assert receipt["with"]["if-no-files-found"] == "ignore"
    assert receipt["with"]["retention-days"] == 30
    assert job["timeout-minutes"] == 45
    assert step["timeout-minutes"] == 30
    assert "OWNER_LOOKUP_STEP_DEADLINE_AT=$(( $(date +%s) + 30 * 60 - 240 ))" in step["run"]
    if job_id == "e2e-secrets":
        assert job["needs"] == "e2e-shell"
        assert job["if"].startswith("always() && ")
        keygen = next(
            i for i, item in enumerate(steps) if item.get("name") == "Generate SSH keypair"
        )
        assert sops < keygen < execution
    assert "github.event_name == 'push'" in job["if"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in job["if"]
    assert "github.event.pull_request.user.login != 'dependabot[bot]'" in job["if"]
    assert "github.actor" not in job["if"]
