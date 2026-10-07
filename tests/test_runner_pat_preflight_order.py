"""PAT refusal precedes outcome writers, and every refusal stops before submission."""

import copy
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOC = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())


def admission(steps):
    pat = [index for index, step in enumerate(steps) if step.get("id") == "pat"]
    assert len(pat) == 1
    assert "continue-on-error" not in steps[pat[0]]
    writers = [
        index
        for index, step in enumerate(steps)
        if any(
            "deployment_outcome=" in line
            for line in step.get("run", "").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    ]
    assert writers and min(writers) > pat[0]
    provision = next(step for step in steps if step.get("id") == "provision")
    code = [
        line.strip()
        for line in provision["run"].splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert code[:2] == [
        "set -uo pipefail",
        'echo "deployment_outcome=unknown" >> "$GITHUB_OUTPUT"',
    ]


def test_pat_preflight_precedes_every_private_public_or_create_outcome_writer():
    admission(DOC["jobs"]["pool"]["steps"])


@pytest.mark.parametrize("mutation", ["pat-after-private", "early-outcome", "late-unknown"])
def test_original_admission_obligation_rejects_each_ordering_regression(mutation):
    steps = copy.deepcopy(DOC["jobs"]["pool"]["steps"])
    index = next(index for index, step in enumerate(steps) if step.get("id") == "pat")
    if mutation == "pat-after-private":
        step = steps.pop(index)
        private = next(
            index for index, step in enumerate(steps) if step.get("id") == "private_profile"
        )
        steps.insert(private + 1, step)
    elif mutation == "early-outcome":
        steps.insert(index, {"run": 'echo "deployment_outcome=no-deployment" >> "$GITHUB_OUTPUT"'})
    else:
        provision = next(step for step in steps if step.get("id") == "provision")
        provision["run"] = provision["run"].replace(
            'echo "deployment_outcome=unknown" >> "$GITHUB_OUTPUT"', "", 1
        )
    with pytest.raises(AssertionError):
        admission(steps)


@pytest.mark.parametrize(
    "scope,native,reader,caller,org,denied,reason",
    [
        (
            "unexpected",
            "true",
            "true",
            "Borduas-Holdings/blazing",
            "Borduas-Holdings",
            False,
            True,
        ),
        ("true", "false", "true", "Borduas-Holdings/blazing", "Borduas-Holdings", False, True),
        ("true", "true", "false", "Borduas-Holdings/blazing", "Borduas-Holdings", False, True),
        ("true", "true", "true", "foreign/repo", "Borduas-Holdings", False, True),
        ("true", "true", "true", "Borduas-Holdings/blazing", "foreign", False, True),
        ("true", "true", "true", "Borduas-Holdings/blazing", "Borduas-Holdings", True, True),
        ("true", "true", "true", "Borduas-Holdings/blazing", "Borduas-Holdings", False, False),
        ("false", "false", "false", "Borduas-Holdings/blazing", "Borduas-Holdings", False, False),
    ],
)
def test_actual_pat_native_scope_branches_exit_before_any_following_command(
    tmp_path, scope, native, reader, caller, org, denied, reason
):
    step = next(step for step in DOC["jobs"]["pool"]["steps"] if step.get("id") == "pat")
    body = step["run"].split("# ONE request, not two.", 1)[0]
    assert body.count("case ") == 1 and body.count("esac") == 1
    package = tmp_path / "just_akash"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "runner_repository.py").write_text(
        "def verify_native_reader_repository():\n"
        + ("    raise ValueError('SYNTHETIC_PRIVATE_CANARY')\n" if denied else "    pass\n")
    )
    output = tmp_path / "output"
    marker = tmp_path / "following-command"
    script = tmp_path / "step.sh"
    script.write_text(body + '\nprintf reached > "$TEST_MARKER"\n')
    python_binary = shutil.which("python3")
    assert python_binary is not None
    environment = {
        "PATH": str(Path(python_binary).parent) + os.pathsep + os.defpath,
        "PYTHONPATH": str(tmp_path),
        "GH_TOKEN": "SYNTHETIC_PAT",
        "RUNNER_NATIVE_REPOSITORY_SCOPE": scope,
        "RUNNER_NATIVE_PULL_READER": native,
        "READER_SOPS": reader,
        "CALLER": caller,
        "ORG": org,
        "GITHUB_OUTPUT": str(output),
        "TEST_MARKER": str(marker),
    }
    result = subprocess.run(
        ["bash", "-e", str(script)], cwd=tmp_path, env=environment, capture_output=True, timeout=5
    )
    assert result.returncode == (1 if reason else 0)
    assert marker.exists() is (scope == "false")
    emitted = output.read_text() if output.exists() else ""
    assert bool(re.search(r"^failure_reason=", emitted, re.MULTILINE)) is reason
    assert "deployment_outcome=" not in emitted
    assert "SYNTHETIC_PRIVATE_CANARY" not in (result.stdout + result.stderr).decode()
