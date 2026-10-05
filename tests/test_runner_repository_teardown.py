"""Fixed private repository cleanup preserves existing org defaults and refuses busy races."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


@pytest.mark.parametrize(
    "repository,caller,org,label,expected",
    [
        (
            "Borduas-Holdings/blazing",
            "Borduas-Holdings/blazing",
            "Borduas-Holdings",
            "podman-images-123-2",
            True,
        ),
        (
            "Borduas-Holdings/Blazing-Back",
            "Borduas-Holdings/blazing",
            "Borduas-Holdings",
            "podman-images-123-2",
            False,
        ),
        (
            "Borduas-Holdings/blazing",
            "Other/blazing",
            "Borduas-Holdings",
            "podman-images-123-2",
            False,
        ),
        (
            "Borduas-Holdings/blazing",
            "Borduas-Holdings/blazing",
            "Other",
            "podman-images-123-2",
            False,
        ),
        (
            "Borduas-Holdings/blazing",
            "Borduas-Holdings/blazing",
            "Borduas-Holdings",
            "foreign-operation",
            False,
        ),
    ],
)
@pytest.mark.parametrize("became_busy", [False, True])
@pytest.mark.parametrize(
    "busy,identity_ok", [(False, True), (True, True), (None, True), (False, False)]
)
def test_actual_teardown_selects_repository_or_holds_without_any_api(
    tmp_path, repository, caller, org, label, expected, busy, identity_ok, became_busy
):
    body = next(
        s["run"]
        for s in workflow("runner-teardown.yml")["jobs"]["teardown"]["steps"]
        if s.get("id") == "dereg"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    gh = bin_dir / "gh"
    python = bin_dir / "python3"
    python.write_text(
        "#!/bin/sh\ncat >/dev/null\nprintf 'identity\\n' >> \"$CHECKS\"\nexit "
        + ("0" if identity_ok else "1")
        + "\n"
    )
    python.chmod(0o755)
    runner = {
        "id": 701,
        "name": f"just-akash-{label}-aabbcc",
        "busy": busy,
        "labels": [{"name": n} for n in ("self-hosted", "akash", label)],
    }
    changed_runner = runner | {"busy": True} if became_busy else runner
    gh.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n'
        'case " $* " in *" -X DELETE "*) exit 0 ;; *) '
        "if [ -e \"$READ_SEEN\" ]; then printf '%s\\n' '"
        + json.dumps(changed_runner)
        + "'; else touch \"$READ_SEEN\"; printf '%s\\n' '"
        + json.dumps(runner)
        + "'; fi ;; esac\n"
    )
    gh.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", body],
        env={
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "GH_TOKEN": "PATCANARY",
            "ORG": org,
            "REPOSITORY": repository,
            "CALLER": caller,
            "RUN_ID": "123",
            "RUN_ATTEMPT": "2",
            "RUNNER_LABEL": label,
            "RUNNER_IDS_JSON": "[701]",
            "GITHUB_OUTPUT": str(tmp_path / "output"),
            "CALLS": str(calls),
            "CHECKS": str(tmp_path / "checks"),
            "READ_SEEN": str(tmp_path / "read-seen"),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    if expected and busy is False and identity_ok and not became_busy:
        assert (tmp_path / "checks").read_text().splitlines() == ["identity", "identity"]
        actual = calls.read_text()
        assert "repos/Borduas-Holdings/blazing/actions/runners/701" in actual
        assert "-X DELETE repos/Borduas-Holdings/blazing/actions/runners/701" in actual
        assert "orgs/" not in actual
    else:
        assert not calls.exists() or "-X DELETE" not in calls.read_text()
        assert "deregister_failed=unmeasured" in (tmp_path / "output").read_text()
