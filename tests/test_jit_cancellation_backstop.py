"""The cross-job label backstop covers cancellation before an ID is journaled."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from just_akash import jit_pool

ROOT = Path(__file__).parents[1]
POOL_PATH = ROOT / ".github/workflows/runner-pool.yml"
TEARDOWN_PATH = ROOT / ".github/workflows/runner-teardown.yml"
IMAGE = (
    "ghcr.io/digital-frontier-lda/df-akash-runner@sha256:"
    "5b43d797d92bb081d2e085046b48579ebf0448a48fcc32f562eafa0c811b4a32"  # pragma: allowlist secret
)


def _workflow(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _deregister_body() -> str:
    workflow = _workflow(TEARDOWN_PATH)
    steps = workflow["jobs"]["teardown"]["steps"]
    return next(step["run"] for step in steps if step.get("id") == "dereg")


def test_failed_pool_handoff_forwards_the_operation_label_without_step_outputs() -> None:
    workflow = _workflow(POOL_PATH)
    teardown = workflow["jobs"]["teardown"]
    assert teardown["if"] == "always() && needs.pool.result != 'success'"
    label = teardown["with"]["runner-label"]
    assert "needs.pool.outputs" not in label
    assert "inputs.create-operation" in label
    assert "github.run_attempt" in label
    assert "github.run_id" in label


def test_cancellation_after_first_create_is_found_and_deleted_by_operation_label(
    tmp_path: Path,
) -> None:
    created: list[dict[str, object]] = []
    private = tmp_path / "private"
    private.mkdir(mode=0o700)

    def cancelled_after_create(_org: str, payload: dict[str, object]) -> dict[str, object]:
        name = payload["name"]
        labels = payload["labels"]
        assert isinstance(name, str)
        assert isinstance(labels, list)
        created.append(
            {
                "id": 701,
                "name": name,
                "status": "offline",
                "busy": False,
                "labels": [{"name": label} for label in labels],
            }
        )
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        jit_pool.prepare_attempt(
            org="example",
            group_id=17,
            raw_slots='["unit-1"]',
            runner_label="pool-run-123",
            operation_label="pool-run-123-idv2-g1-op-1-attempt-1-run-123",
            repository="org/repo",
            run_id="123",
            run_attempt="1",
            provider_attempt=1,
            image=IMAGE,
            placement="repo-run-123-end",
            cpu="4",
            memory="16Gi",
            storage="30Gi",
            journal_path=private / "identities.json",
            sdl_path=private / "pool.yaml",
            generate=cancelled_after_create,
            delete=lambda _org, _runner_id: None,
        )
    assert json.loads((private / "identities.json").read_text())["runners"] == []

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    deleted = tmp_path / "deleted"
    listing = json.dumps({"runners": created})
    gh = fake_bin / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case " $* " in\n'
        '  *" -X DELETE "*) printf "%s\\n" "$*" >> "$DELETED" ;;\n'
        f"  *) printf '%s\\n' '{listing}' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "GH_TOKEN": "not-a-real-token",
        "ORG": "example",
        "RUNNER_LABEL": "pool-run-123-idv2-g1-op-1-attempt-1-run-123",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "DELETED": str(deleted),
    }
    completed = subprocess.run(  # noqa: S603 -- fixed local bash, isolated fake gh
        ["/bin/bash", "-e", "-c", _deregister_body()],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "orgs/example/actions/runners/701" in deleted.read_text(encoding="utf-8")


def test_operation_label_handoff_call_site_mutation_is_applied_and_observable() -> None:
    source = POOL_PATH.read_text(encoding="utf-8")
    target = (
        "      runner-label: ${{ format('{0}-idv2-g1-op-{1}-attempt-{2}-run-{3}', "
        "inputs.runner-label, inputs.create-operation, github.run_attempt, github.run_id) }}\n"
    )
    assert source.count(target) == 1
    mutant = source.replace(target, "      runner-label: ''\n")
    parsed = yaml.safe_load(mutant)
    assert parsed["jobs"]["teardown"]["with"]["runner-label"] == ""
