"""The pool's on-chain identity must bind run, attempt, group, and broker operation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from just_akash.jit_pool import operation_identity

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/runner-pool.yml"


def test_idv2_placement_carries_the_complete_lifecycle_identity() -> None:
    deployment, label = operation_identity(
        "just-akash-e2epool",
        "e2epool",
        operation="41",
        run_id="34228480597",
        run_attempt="3",
    )
    assert deployment == (
        "just-akash-e2epool-idv2-class-ci-runner-g1-op-41-attempt-3-run-34228480597-end"
    )
    assert label == "e2epool-idv2-g1-op-41-attempt-3-run-34228480597"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation", "0"),
        ("operation", "01"),
        ("run_id", "abc"),
        ("run_attempt", ""),
    ],
)
def test_noncanonical_identity_parts_are_refused(field: str, value: str) -> None:
    values = {"operation": "41", "run_id": "34228480597", "run_attempt": "3"}
    values[field] = value
    with pytest.raises(ValueError):
        operation_identity(
            "just-akash-e2epool",
            "e2epool",
            operation=values["operation"],
            run_id=values["run_id"],
            run_attempt=values["run_attempt"],
        )


def test_legacy_or_prestamped_placement_is_refused() -> None:
    for placement in (
        "just-akash-e2epool-run-34228480597-end",
        "just-akash-e2epool-idv2-class-ci-runner-g1-op-1-attempt-1-run-2-end",
    ):
        with pytest.raises(ValueError):
            operation_identity(
                placement,
                "e2epool",
                operation="41",
                run_id="34228480597",
                run_attempt="3",
            )


def test_real_call_site_passes_server_attempt_and_broker_operation_unchanged() -> None:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    render = next(step for step in doc["jobs"]["pool"]["steps"] if step.get("id") == "render")
    assert render["env"]["CREATE_OPERATION"] == "${{ inputs.create-operation }}"
    assert render["env"]["GH_RUN_ID"] == "${{ github.run_id }}"
    assert render["env"]["GH_RUN_ATTEMPT"] == "${{ github.run_attempt }}"
    body = render["run"]
    target = (
        '--create-operation "$CREATE_OPERATION" --run-id "$GH_RUN_ID" \\\n'
        '  --run-attempt "$GH_RUN_ATTEMPT"'
    )
    assert body.count(target) == 1


def test_call_site_mutation_drops_the_attempt_binding() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    target = '--run-attempt "$GH_RUN_ATTEMPT"'
    assert source.count(target) == 1
    mutant = source.replace(target, '--run-attempt "1"')
    assert target not in mutant


def test_docs_do_not_claim_the_ephemeral_tag_is_the_chain_identity() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    head = text[: text.index("name: Akash runner pool")]
    assert "so a sweeper can reap this run's lease" not in head
