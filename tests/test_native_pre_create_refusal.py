"""A typed native preflight refusal proves no create, without hiding later outcomes."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def test_outcome_prefers_every_provision_result_over_pre_create_refusal():
    job = workflow("runner-pool.yml")["jobs"]["pool"]
    assert job["outputs"]["deployment_outcome"] == (
        "${{ steps.provision.outputs.deployment_outcome || "
        "(steps.pat.outputs.native_pre_create_refused == 'true' && 'no-deployment' || '') }}"
    )
    steps = job["steps"]
    pat_index = next(i for i, step in enumerate(steps) if step.get("id") == "pat")
    create_index = next(i for i, step in enumerate(steps) if step.get("id") == "provision")
    assert pat_index < create_index
    assert "continue-on-error" not in steps[pat_index]
    assert "always()" not in steps[create_index].get("if", "")
    assert steps[create_index]["run"].index('echo "deployment_outcome=unknown"') < steps[
        create_index
    ]["run"].index('"${JA[@]}" deploy')


@pytest.mark.parametrize(
    "scope,helper_rc,caller,refused",
    [
        ("true", 1, "Borduas-Holdings/blazing", True),
        ("true", 0, "Borduas-Holdings/blazing", False),
        ("true", 0, "foreign/blazing", True),
        ("false", 1, "Borduas-Holdings/blazing", False),
    ],
)
def test_actual_pat_step_emits_refusal_only_before_an_immediate_exit(
    tmp_path, scope, helper_rc, caller, refused
):
    steps = workflow("runner-pool.yml")["jobs"]["pool"]["steps"]
    body = next(step["run"] for step in steps if step.get("id") == "pat")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python3"
    python.write_text(f"#!/bin/sh\ncat >/dev/null\nexit {helper_rc}\n")
    python.chmod(0o755)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'HTTP/2.0 201 Created' '{\"token\":\"REGISTRATIONCANARY\"}'\n"
    )
    gh.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", body],
        capture_output=True,
        text=True,
        timeout=10,
        env={
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "GH_TOKEN": "PATCANARY",
            "ORG": "Borduas-Holdings",
            "CALLER": caller,
            "RUNNER_NATIVE_REPOSITORY_SCOPE": scope,
            "RUNNER_NATIVE_PULL_READER": "true",
            "READER_SOPS": "true",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        },
    )
    text = output.read_text() if output.exists() else ""
    assert ("native_pre_create_refused=true" in text) is refused
    assert (result.returncode != 0) is refused
    if refused:
        values = {
            key: value
            for key, _separator, value in (line.partition("=") for line in text.splitlines())
        }
        assert values.get("failure_reason") == "NATIVE_READER_REPOSITORY_UNQUALIFIED"
    assert "deployment_outcome=" not in text, (
        "do not redefine the unconditional create-boundary marker"
    )


@pytest.mark.parametrize(
    "outcome,ambiguous,expected",
    [
        ("no-deployment", "false", 0),
        ("no-deployment", "true", 1),
        ("unknown", "false", 1),
        ("created", "false", 1),
        ("", "false", 1),
    ],
)
def test_actual_close_never_accepts_an_ambiguous_create_as_noop(
    tmp_path, outcome, ambiguous, expected
):
    body = next(
        step["run"]
        for step in workflow("runner-teardown.yml")["jobs"]["teardown"]["steps"]
        if step.get("id") == "close"
    )
    output = tmp_path / "output"
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", body],
        capture_output=True,
        text=True,
        timeout=10,
        env={
            **os.environ,
            "DSEQ": "",
            "DEPLOYMENT_OUTCOME": outcome,
            "CREATE_AMBIGUOUS": ambiguous,
            "GITHUB_OUTPUT": str(output),
            "DEPLOYMENT_GROUP": "",
            "TAG_PREFIX": "",
            "WALLET_ADDRESS": "",
        },
    )
    assert result.returncode == expected
    text = output.read_text()
    assert ("closed=noop" in text) is (expected == 0)
    assert ("held_reason=CREATE_OUTCOME_AMBIGUOUS" in text) is (expected != 0)
