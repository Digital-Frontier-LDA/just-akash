"""Issue #335: the created group identity reaches every destructive boundary."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
POOL_PATH = ROOT / ".github/workflows/runner-pool.yml"
TEARDOWN_PATH = ROOT / ".github/workflows/runner-teardown.yml"
POOL_SRC = POOL_PATH.read_text()
TEARDOWN_SRC = TEARDOWN_PATH.read_text()
EXPECTED_GROUP = "borduas-runner-run-42-end"


def _on(document: dict) -> dict:
    return document.get("on") or document[True]


def _code(body: str) -> str:
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))


def _assert_group_handoffs(pool_source: str, teardown_source: str) -> None:
    pool = yaml.safe_load(pool_source)
    teardown = yaml.safe_load(teardown_source)
    pool_call = _on(pool)["workflow_call"]
    pool_job = pool["jobs"]["pool"]
    render = next(step for step in pool_job["steps"] if step.get("name") == "Render runner SDL")
    provision = next(step for step in pool_job["steps"] if step.get("id") == "provision")
    nested = pool["jobs"]["teardown"]
    teardown_call = _on(teardown)["workflow_call"]
    close = next(
        step for step in teardown["jobs"]["teardown"]["steps"] if step.get("id") == "close"
    )

    assert render.get("id") == "render"
    render_code = _code(render["run"])
    publication = 'echo "deployment_group=${PLACEMENT_KEY}" >> "$GITHUB_OUTPUT"'
    assert render_code.count(publication) == 1
    assert render_code.index('PLACEMENT_KEY="${PLACEMENT_KEY}-run-${GH_RUN_ID:-}-end"') < (
        render_code.index(publication)
    )
    assert render_code.index(publication) < render_code.index("cat > /tmp/runner-sdl.yaml")

    assert pool_job["outputs"].get("deployment_group") == (
        "${{ steps.render.outputs.deployment_group }}"
    )
    assert pool_call["outputs"].get("deployment_group", {}).get("value") == (
        "${{ jobs.pool.outputs.deployment_group }}"
    )
    assert provision["env"].get("DEPLOYMENT_GROUP") == (
        "${{ steps.render.outputs.deployment_group }}"
    )

    provision_code = _code(provision["run"])
    rollback_lines = [
        line
        for line in provision_code.splitlines()
        if '"${JA[@]}" destroy' in line and '--expected-owner "$WALLET"' in line
    ]
    assert len(rollback_lines) == 5, (
        "mutation target count changed: expected exactly five owner-bound immediate rollback calls"
    )
    for line in rollback_lines:
        assert line.count('--expected-group "$DEPLOYMENT_GROUP"') == 1

    assert nested["with"].get("deployment-group") == ("${{ needs.pool.outputs.deployment_group }}")
    group_input = teardown_call["inputs"].get("deployment-group", {})
    assert group_input.get("required") is False
    assert group_input.get("default") == ""
    assert close["env"].get("DEPLOYMENT_GROUP") == "${{ inputs.deployment-group }}"

    close_code = _code(close["run"])
    assert close_code.count('RESOLVE_ARGS+=(--expected-group "$DEPLOYMENT_GROUP")') == 1
    assert close_code.count('DESTROY_ARGS+=(--expected-group "$DEPLOYMENT_GROUP")') == 1


def test_render_publishes_the_name_used_by_both_sdl_group_maps(tmp_path):
    pool = yaml.safe_load(POOL_SRC)
    render = next(
        step for step in pool["jobs"]["pool"]["steps"] if step.get("name") == "Render runner SDL"
    )
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", render["run"]],
        env={
            **os.environ,
            "GH_RUNNER_PAT": "secret",
            "ORG": "Digital-Frontier-LDA",
            "RUNNER_LABEL": "exact-group-test",
            "POOL_SIZE": "1",
            "CPU": "1",
            "MEMORY": "1Gi",
            "STORAGE": "1Gi",
            "EPHEMERAL": "true",
            "PLACEMENT_KEY": "borduas-runner",
            "GH_RUN_ID": "42",
            "GITHUB_OUTPUT": str(output),
        },
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text().splitlines() == [f"deployment_group={EXPECTED_GROUP}"]
    rendered = yaml.safe_load(Path("/tmp/runner-sdl.yaml").read_text())
    assert list(rendered["profiles"]["placement"]) == [EXPECTED_GROUP]
    assert list(rendered["deployment"]["runner"]) == [EXPECTED_GROUP]


def _replace_once(source: str, old: str, new: str) -> str:
    assert source.count(old) == 1, (
        f"mutation target count changed for {old!r}: expected 1, found {source.count(old)}"
    )
    return source.replace(old, new, 1)


def _sever_rollback(source: str, index: int) -> str:
    needle = '--expected-group "$DEPLOYMENT_GROUP" '
    lines = source.splitlines(keepends=True)
    targets = [
        line_index
        for line_index, line in enumerate(lines)
        if '"${JA[@]}" destroy' in line and '--expected-owner "$WALLET"' in line
    ]
    assert len(targets) == 5, (
        f"rollback mutation target count changed: expected 5, found {len(targets)}"
    )
    target = targets[index]
    assert lines[target].count(needle) == 1
    lines[target] = lines[target].replace(needle, "", 1)
    return "".join(lines)


MUTATIONS = [
    (
        "render-step-id",
        lambda p, t: (_replace_once(p, "        id: render\n", "        id: severed\n"), t),
    ),
    (
        "render-step-output",
        lambda p, t: (
            _replace_once(
                p,
                'echo "deployment_group=${PLACEMENT_KEY}" >> "$GITHUB_OUTPUT"',
                'echo "deployment_group=" >> "$GITHUB_OUTPUT"',
            ),
            t,
        ),
    ),
    (
        "step-to-job-output",
        lambda p, t: (
            _replace_once(
                p,
                "deployment_group: ${{ steps.render.outputs.deployment_group }}",
                "deployment_group: ''",
            ),
            t,
        ),
    ),
    (
        "job-to-reusable-output",
        lambda p, t: (
            _replace_once(
                p,
                "value: ${{ jobs.pool.outputs.deployment_group }}",
                "value: ''",
            ),
            t,
        ),
    ),
    (
        "render-output-to-rollback-env",
        lambda p, t: (
            _replace_once(
                p,
                "DEPLOYMENT_GROUP: ${{ steps.render.outputs.deployment_group }}",
                "DEPLOYMENT_GROUP: ''",
            ),
            t,
        ),
    ),
    *[
        (
            f"immediate-rollback-{index + 1}",
            lambda p, t, index=index: (_sever_rollback(p, index), t),
        )
        for index in range(5)
    ],
    (
        "job-output-to-nested-teardown-input",
        lambda p, t: (
            _replace_once(
                p,
                "deployment-group: ${{ needs.pool.outputs.deployment_group }}",
                "deployment-group: ''",
            ),
            t,
        ),
    ),
    (
        "teardown-input",
        lambda p, t: (
            p,
            _replace_once(t, "      deployment-group:\n", "      severed-group:\n"),
        ),
    ),
    (
        "teardown-input-to-env",
        lambda p, t: (
            p,
            _replace_once(
                t,
                "DEPLOYMENT_GROUP: ${{ inputs.deployment-group }}",
                "DEPLOYMENT_GROUP: ''",
            ),
        ),
    ),
    (
        "env-to-resolve-owner-cli",
        lambda p, t: (
            p,
            _replace_once(
                t,
                'RESOLVE_ARGS+=(--expected-group "$DEPLOYMENT_GROUP")',
                ": # severed resolve-owner group binding",
            ),
        ),
    ),
    (
        "env-to-destroy-cli",
        lambda p, t: (
            p,
            _replace_once(
                t,
                'DESTROY_ARGS+=(--expected-group "$DEPLOYMENT_GROUP")',
                ": # severed destroy group binding",
            ),
        ),
    ),
]


def test_all_exact_group_handoffs_are_present():
    _assert_group_handoffs(POOL_SRC, TEARDOWN_SRC)


@pytest.mark.parametrize("label,mutate", MUTATIONS, ids=[item[0] for item in MUTATIONS])
def test_severing_each_exact_group_handoff_is_detected(label, mutate):
    mutated_pool, mutated_teardown = mutate(POOL_SRC, TEARDOWN_SRC)
    assert (mutated_pool, mutated_teardown) != (POOL_SRC, TEARDOWN_SRC), label
    with pytest.raises((AssertionError, StopIteration)):
        _assert_group_handoffs(mutated_pool, mutated_teardown)
