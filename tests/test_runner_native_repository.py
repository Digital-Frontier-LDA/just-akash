"""Repository confinement needs real hosted POST authority before reader delivery."""

from pathlib import Path

import pytest
import yaml

from just_akash import runner_image as image
from tests.test_runner_native_reader import LABEL, decrypt, options, scope, template
from tests.test_runner_repository_authority import authority
from tests.test_runner_repository_teardown import workflow

ROOT = Path(__file__).parents[1]
REPO = {"id": 1074974924, "full_name": "Borduas-Holdings/blazing", "private": True}


def test_repository_payload_preserves_reader_and_fixed_runner_identity(tmp_path, monkeypatch):
    scope(monkeypatch)
    monkeypatch.setenv("RUNNER_NATIVE_REPOSITORY_SCOPE", "true")
    calls = authority(monkeypatch)
    path = template(tmp_path)
    image.configure(path, **options(), native_reader=True, reader_from_sops=True)
    runner = yaml.safe_load(path.read_text())["services"]["runner"]
    env = dict(v.split("=", 1) for v in runner["env"])
    assert env["RUNNER_SCOPE"] == "repo" and env["REPO_NAME"] == "blazing"
    assert env["ORG_NAME"] == "Borduas-Holdings" and env["RUNNER_TOKEN"] == "@@RUNNER_TOKEN@@"
    assert env["RUNNER_NAME_PREFIX"] == f"just-akash-{LABEL}"
    assert env["DOCKERHUB_PULL_TOKEN"] == options()["password"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert calls == [False, True, False]


def test_post_failure_prevents_sops_decryption(tmp_path, monkeypatch, capsys):
    scope(monkeypatch)
    monkeypatch.setenv("RUNNER_NATIVE_REPOSITORY_SCOPE", "true")
    authority(monkeypatch, grant={})
    cipher, calls = decrypt(tmp_path, monkeypatch)
    path = template(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["runner-image", "--sdl", str(path), "--sops-env-file", str(cipher)]
    )
    before = path.read_bytes()
    assert image.main() == 1
    assert calls == [] and path.read_bytes() == before
    assert "NATIVE_READER_REPOSITORY_UNQUALIFIED" in capsys.readouterr().out


@pytest.mark.parametrize("value,native", [("true", False), ("1", True), ("TRUE", True)])
def test_scope_cannot_be_enabled_without_native_reader(monkeypatch, value, native):
    monkeypatch.setenv("RUNNER_NATIVE_REPOSITORY_SCOPE", value)
    with pytest.raises(image.NativeReaderRepositoryError):
        image.native_repository_scope(native_reader=native)


def test_all_pool_api_sites_and_rollback_share_fixed_scope():
    doc = workflow("runner-pool.yml")
    assert (
        doc[True]["workflow_call"]["inputs"]["runner-native-repository-scope"]["default"] is False
    )
    steps = doc["jobs"]["pool"]["steps"]
    preflight = next(s for s in steps if s.get("id") == "pat")["run"]
    provision = next(s for s in steps if s.get("id") == "provision")["run"]
    assert "verify_native_reader_repository()" in preflight
    assert preflight.index("READER_SOPS") < preflight.index("RESP=$(gh api --method POST")
    assert provision.count('"${RUNNER_COLLECTION}/registration-token"') == 2
    assert '"${RUNNER_COLLECTION}?per_page=100"' in provision
    assert "verify_native_reader_admission()" in provision
    rollback = doc["jobs"]["teardown"]["with"]
    assert doc["jobs"]["teardown"]["uses"].endswith("@810e75fb5cd0963f80329d54d11e1b00cecf527f")
    assert rollback["just-akash-ref"] == (
        "${{ inputs.runner-native-pull-reader && '"
        "810e75fb5cd0963f80329d54d11e1b00cecf527f' "  # pragma: allowlist secret
        "|| inputs.just-akash-ref }}"
    )
    assert (
        rollback["github-repository"]
        == "${{ inputs.runner-native-repository-scope && 'Borduas-Holdings/blazing' || '' }}"
    )


@pytest.mark.parametrize(
    "patch,expected",
    [
        ({}, True),
        ({"RUNNER_NATIVE_PULL_READER": "false"}, False),
        ({"READER_SOPS": "false"}, False),
        ({"CALLER": "Borduas-Holdings/Blazing-Back"}, False),
        ({"ORG": "Other"}, False),
        ({"RUNNER_NATIVE_REPOSITORY_SCOPE": "TRUE"}, False),
    ],
)
def test_actual_repository_preflight_refuses_foreign_or_non_reader_scope_before_post(
    tmp_path, patch, expected
):
    import os
    import subprocess

    body = next(
        s["run"]
        for s in workflow("runner-pool.yml")["jobs"]["pool"]["steps"]
        if s.get("id") == "pat"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    gh = bin_dir / "gh"
    gh.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n'
        "printf '%s\\n' 'HTTP/2.0 201 Created' '{\"token\":\"REGISTRATIONCANARY\"}'\n"
    )
    gh.chmod(0o755)
    python = bin_dir / "python3"
    python.write_text(
        "#!/bin/sh\ncat >/dev/null\nprintf 'identity-post-identity\\n' >> \"$CALLS\"\n"
    )
    python.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "GH_TOKEN": "PATCANARY",
        "ORG": "Borduas-Holdings",
        "CALLER": "Borduas-Holdings/blazing",
        "RUNNER_NATIVE_PULL_READER": "true",
        "RUNNER_NATIVE_REPOSITORY_SCOPE": "true",
        "READER_SOPS": "true",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "CALLS": str(calls),
        **patch,
    }
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", body], env=env, capture_output=True, text=True, timeout=10
    )
    if expected:
        assert result.returncode == 0, result.stderr
        assert calls.read_text().splitlines() == ["identity-post-identity"]
    else:
        assert result.returncode != 0 and not calls.exists()
        assert "NATIVE_READER_REPOSITORY_UNQUALIFIED" in (tmp_path / "output").read_text()
