"""credentials-sops on the pool: same contract as runner-teardown.yml's.

A Digital-Frontier-LDA caller may not reference secrets.AKASH_API_KEY or
secrets.GH_RUNNER_PAT (df-cicd baseline, secrets-only-sops), so the pool reads them from
the caller's own SOPS file. The decrypt has to precede the PAT preflight, every direct
secret must still win, and the pool's own rollback must read the same file: otherwise a
failed provision is the one lease nothing can close.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def pool():
    return yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())


def test_opt_in_and_only_the_age_key_is_added():
    call = pool()[True]["workflow_call"]
    assert call["inputs"]["credentials-sops"]["type"] == "boolean"
    assert call["inputs"]["credentials-sops"]["default"] is False
    assert call["secrets"]["CREDENTIALS_AGE_KEY"]["required"] is False


def _step():
    return next(
        s for s in pool()["jobs"]["pool"]["steps"] if s.get("id") == "credentials_sops"
    )


def test_decrypt_precedes_the_pat_preflight_in_one_step_with_no_checkout_or_local_action():
    steps = pool()["jobs"]["pool"]["steps"]
    load = next(i for i, s in enumerate(steps) if s.get("id") == "credentials_sops")
    preflight = next(i for i, s in enumerate(steps) if s.get("id") == "pat")
    assert steps[0]["with"]["path"] == ".just-akash"
    assert 0 < load < preflight
    step = steps[load]
    assert step["if"] == "inputs.credentials-sops"
    # A checkout here would precede the public-profile admission; a local `uses:` is unpinned.
    assert "uses" not in step and "with" not in step
    assert step["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "CALLER": "${{ github.repository }}",
        "CALLER_SHA": "${{ github.sha }}",
        "CREDENTIALS_AGE_KEY": "${{ secrets.CREDENTIALS_AGE_KEY }}",
    }


def _run(tmp_path, *, plain, age_key="AGE-SECRET-KEY-1STUB", gh_rc=0, sum_rc=0):
    """Execute the real step script with stub curl, sha256sum, gh and sops on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.txt"
    (tmp_path / "plain.env").write_text(plain)
    stubs = {
        # curl writes a fake sops binary that prints the plaintext fixture.
        "curl": (
            'out=""; prev=""; for a in "$@"; do [ "$prev" = "--output" ] && out="$a"; prev="$a"; done\n'
            f'printf \'#!/bin/bash\\necho "sops key=${{SOPS_AGE_KEY:-}} $*" >> {log}\\ncat {tmp_path}/plain.env\\n\' > "$out"\n'
        ),
        "sha256sum": f'echo "sha256sum $*" >> {log}; exit {sum_rc}\n',
        "gh": f'echo "gh $*" >> {log}; echo ciphertext; exit {gh_rc}\n',
    }
    for name, body in stubs.items():
        (bindir / name).write_text("#!/bin/bash\n" + body)
        (bindir / name).chmod(0o755)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "GH_TOKEN": "job-token",
        "CALLER": "Example-Org/consumer",
        "CALLER_SHA": "a" * 40,
        "CREDENTIALS_AGE_KEY": age_key,
        "GITHUB_ENV": str(tmp_path / "github_env"),
    }
    proc = subprocess.run(
        ["bash", "-c", _step()["run"]], env=env, capture_output=True, text=True
    )
    exported = (tmp_path / "github_env").read_text() if (tmp_path / "github_env").exists() else ""
    calls = log.read_text() if log.exists() else ""
    return proc, exported, calls


def _exported_keys(exported):
    return [line.split("<<", 1)[0] for line in exported.splitlines() if "<<" in line]


def test_only_the_three_credentials_are_exported_each_masked_first(tmp_path):
    proc, exported, calls = _run(
        tmp_path,
        plain="AKASH_API_KEY=ak-1\nGH_RUNNER_PAT=ghp-2\nUNRELATED=keep-out\nAKASH_API_KEYS=k3\n",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _exported_keys(exported) == ["AKASH_API_KEY", "GH_RUNNER_PAT", "AKASH_API_KEYS"]
    assert "keep-out" not in exported and "keep-out" not in proc.stdout
    for value in ("ak-1", "ghp-2", "k3"):
        assert f"::add-mask::{value}" in proc.stdout
        # The value appears on stdout only inside its mask command.
        assert proc.stdout.count(value) == 1
    # The caller's exact revision, through the contents API; the binary checksum is checked.
    assert f"contents/secrets/akash-runner.sops.env?ref={'a' * 40}" in calls
    assert "repos/Example-Org/consumer/" in calls
    assert "sha256sum --check" in calls
    # The age key reaches sops, and only through the one call.
    assert "sops key=AGE-SECRET-KEY-1STUB --decrypt" in calls


@pytest.mark.parametrize("age_key", ["", "   "])
def test_an_empty_age_key_refuses_before_any_fetch(tmp_path, age_key):
    proc, exported, calls = _run(tmp_path, plain="GH_RUNNER_PAT=ghp\n", age_key=age_key)
    assert proc.returncode == 1
    assert "CREDENTIALS_AGE_KEY is empty" in proc.stdout
    assert exported == "" and calls == ""


def test_a_failed_checksum_never_decrypts(tmp_path):
    proc, exported, calls = _run(tmp_path, plain="GH_RUNNER_PAT=ghp\n", sum_rc=1)
    assert proc.returncode != 0
    assert "sops " not in calls and exported == ""


def test_a_missing_file_refuses_by_name(tmp_path):
    proc, exported, _ = _run(tmp_path, plain="GH_RUNNER_PAT=ghp\n", gh_rc=1)
    assert proc.returncode == 1
    assert "has no readable secrets/akash-runner.sops.env" in proc.stdout
    assert exported == ""


def test_a_file_without_any_credential_refuses(tmp_path):
    proc, exported, _ = _run(tmp_path, plain="UNRELATED=x\nGH_RUNNER_PAT=\n")
    assert proc.returncode == 1
    assert "holds none of AKASH_API_KEY" in proc.stdout
    assert exported == ""


def test_preflight_and_provision_fall_back_to_the_sops_value_and_a_direct_secret_wins():
    steps = {s.get("id"): s for s in pool()["jobs"]["pool"]["steps"] if s.get("id")}
    assert steps["pat"]["env"]["GH_TOKEN"] == "${{ secrets.GH_RUNNER_PAT || env.GH_RUNNER_PAT }}"
    env = steps["provision"]["env"]
    assert env["AKASH_API_KEY"] == "${{ secrets.AKASH_API_KEY || env.AKASH_API_KEY }}"
    assert env["AKASH_API_KEYS"] == "${{ secrets.AKASH_API_KEYS || env.AKASH_API_KEYS }}"
    assert env["GH_TOKEN"] == "${{ secrets.GH_RUNNER_PAT || env.GH_RUNNER_PAT }}"
    # The native-reader decrypt step (#427) reads the same PAT; a SOPS caller must reach it too.
    reader = next(
        s for s in pool()["jobs"]["pool"]["steps"] if s.get("name", "").endswith("mirror from SOPS")
    )
    assert reader["env"]["GH_TOKEN"] == (
        "${{ inputs.runner-native-pull-reader && (secrets.GH_RUNNER_PAT || env.GH_RUNNER_PAT) || '' }}"
    )


def test_the_rollback_teardown_reads_the_same_caller_file():
    rollback = pool()["jobs"]["teardown"]
    assert rollback["with"]["credentials-sops"] == "${{ inputs.credentials-sops }}"
    assert rollback["secrets"]["CREDENTIALS_AGE_KEY"] == "${{ secrets.CREDENTIALS_AGE_KEY }}"
