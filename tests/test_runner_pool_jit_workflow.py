from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

WF = Path(
    os.environ.get(
        "RUNNER_POOL_JIT_WF", Path(__file__).parents[1] / ".github/workflows/runner-pool.yml"
    )
)
SOURCE = WF.read_text(encoding="utf-8")
DOC = yaml.safe_load(SOURCE)
CALL = (DOC.get("on") or DOC.get(True))["workflow_call"]
INPUTS = CALL["inputs"]
OUTPUTS = CALL["outputs"]
STEPS = DOC["jobs"]["pool"]["steps"]


def _step(step_id: str) -> Any:
    return next(step for step in STEPS if step.get("id") == step_id)


def _code(body: str) -> str:
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))


def test_topology_inputs_are_required_and_have_no_unsafe_default() -> None:
    for name in ("runner-group-id", "runner-slots", "runner-image", "create-operation"):
        assert INPUTS[name]["required"] is True
        assert "default" not in INPUTS[name]
    assert "pool-size" not in INPUTS
    assert "min-pool-size" not in INPUTS
    assert "ephemeral" not in INPUTS


def test_group_authority_is_read_back_and_bound_to_the_caller_before_jit_creation() -> None:
    binding = _step("group-binding")
    assert binding["env"]["CALLER_REPOSITORY_ID"] == "${{ github.repository_id }}"
    assert binding["env"]["CALLER_WORKFLOW_REF"] == "${{ github.workflow_ref }}"
    assert "verify-binding" in binding["run"]
    assert '--group-id "$RUNNER_GROUP_ID"' in binding["run"]
    assert '--github-output "$GITHUB_OUTPUT"' in binding["run"]
    assert STEPS.index(binding) < STEPS.index(_step("provision"))


def test_idv2_identity_uses_server_attempt_and_broker_operation_without_fallback() -> None:
    render = _step("render")
    assert render["env"]["CREATE_OPERATION"] == "${{ inputs.create-operation }}"
    assert render["env"]["GH_RUN_ID"] == "${{ github.run_id }}"
    assert render["env"]["GH_RUN_ATTEMPT"] == "${{ github.run_attempt }}"
    body = _code(render["run"])
    assert "just_akash.jit_pool identity" in body
    assert '--create-operation "$CREATE_OPERATION"' in body
    assert '--run-attempt "$GH_RUN_ATTEMPT"' in body
    assert '--runner-label "$OPERATION_LABEL"' in body
    assert "-run-${GH_RUN_ID}-end" not in body


def test_configs_are_created_inside_every_provider_attempt_before_akash_create() -> None:
    body = _code(_step("provision")["run"])
    assert _step("provision")["env"]["VERIFIED_RUNNER_GROUP_ID"] == (
        "${{ steps.group-binding.outputs.verified_group_id }}"
    )
    loop = body.index('for attempt in $(seq 1 "$MAX_ATTEMPTS")')
    prepare = body.index('"${JIT[@]}" prepare', loop)
    deploy = body.index('"${JA[@]}" deploy', prepare)
    assert loop < prepare < deploy
    assert '--group-id "$VERIFIED_RUNNER_GROUP_ID"' in body[prepare:deploy]
    assert '--slots "$RUNNER_SLOTS"' in body[prepare:deploy]
    assert '--operation-label "$OPERATION_LABEL"' in body[prepare:deploy]
    assert '--provider-attempt "$attempt"' in body[prepare:deploy]


def test_safe_identity_outputs_are_published_before_create_and_configs_are_not() -> None:
    body = _step("provision")["run"]
    publish = body.index('cat "$JIT_OUTPUT" >> "$GITHUB_OUTPUT"')
    deploy = body.index('"${JA[@]}" deploy')
    assert publish < deploy
    for name in ("runner-ids", "runner-names", "runner-labels", "runner-targets"):
        assert name in OUTPUTS
    assert "encoded_jit_config" not in "\n".join(OUTPUTS)


def test_akash_create_uses_a_bound_crash_durable_receipt() -> None:
    body = _code(_step("provision")["run"])
    select_owner = body.index('select-owner --deposit-usd "$REQUIRED_DEPOSIT_USD"')
    deploy = body.index('"${JA[@]}" deploy')
    assert select_owner < deploy
    for flag in (
        "--receipt-path",
        "--receipt-expected-owner",
        "--receipt-expected-group",
        "--receipt-artifact-sha256",
        "--receipt-operation-id",
    ):
        assert flag in body[deploy:]
    operation = 'RECEIPT_OPERATION="broker:${CREATE_OPERATION}:github:${RUN_ID}:${RUN_ATTEMPT}:g1"'
    assert operation in body


def test_unsettled_create_stops_before_any_provider_retry() -> None:
    body = _code(_step("provision")["run"])
    start = body.index("failure_reason=CREATE_SETTLEMENT_UNOBSERVED")
    branch = body[start : body.index("fi", start)]
    assert "exit 1" in branch
    assert "continue" not in branch


def test_every_failed_attempt_calls_exact_registration_cleanup_before_retry() -> None:
    body = _step("provision")["run"]
    assert body.count("cleanup_jit_attempt || exit 1") == 9
    assert "JIT_REGISTRATION_CLEANUP_FAILED" in body
    assert '"${JIT[@]}" cleanup --journal "$JIT_JOURNAL"' in body


def test_cancelled_provision_has_a_same_job_exact_id_cleanup_call_site() -> None:
    rollback = next(
        step for step in STEPS if step.get("name", "").startswith("Roll back failed JIT")
    )
    assert rollback["if"] == "always() && steps.provision.outcome != 'success'"
    assert "just_akash.jit_pool cleanup" in rollback["run"]
    assert "identities.json" in rollback["run"]


def test_landing_gate_requires_the_complete_exact_id_population() -> None:
    body = _code(_step("provision")["run"])
    assert 'observe --journal "$JIT_JOURNAL"' in body
    wait = body[body.index("while [") : body.index('if [ "$API_OK"', body.index("while ["))]
    assert "runner-groups/" not in wait
    assert 'if [ "${ONLINE:-0}" -lt "${POOL_SIZE}" ]; then' in body
    assert "Partial pool accepted" not in body
    assert "MIN_POOL" not in body


def test_no_secret_config_or_legacy_credential_is_dumped() -> None:
    render = _step("render")["run"]
    provision = _step("provision")["run"]
    assert "cat /tmp/runner-sdl" not in provision
    assert "grep -vE 'ACCESS_TOKEN'" not in render
    assert "ACCESS_TOKEN=${GH_RUNNER_PAT}" not in SOURCE
    assert "python -m just_akash.jit_pool" in SOURCE


def test_effect_mutations_are_targeted_and_break_the_contract(tmp_path: Path) -> None:
    mutations = {
        "prepare call site": ('"${JIT[@]}" prepare', '"${JIT[@]}" disabled-prepare'),
        "group wiring": (
            '--org "$ORG" --group-id "$VERIFIED_RUNNER_GROUP_ID" --slots "$RUNNER_SLOTS"',
            '--org "$ORG" --group-id "$RUNNER_GROUP_ID" --slots "$RUNNER_SLOTS"',
        ),
        "verified group handoff": (
            "          VERIFIED_RUNNER_GROUP_ID: ${{ steps.group-binding.outputs."
            "verified_group_id }}",
            "          VERIFIED_RUNNER_GROUP_ID: ${{ inputs.runner-group-id }}",
        ),
        "attempt freshness": ('--provider-attempt "$attempt"', '--provider-attempt "1"'),
        "identity publication": (
            'cat "$JIT_OUTPUT" >> "$GITHUB_OUTPUT"',
            ": # publication deleted",
        ),
        "deployment receipt call site": (
            '--receipt-path "$RECEIPT_PATH"',
            '--disabled-receipt-path "$RECEIPT_PATH"',
        ),
        "all-or-nothing": ('-lt "${POOL_SIZE}"', '-lt "0"'),
        "group binding call site": (
            "python -m just_akash.jit_pool verify-binding",
            "python -m just_akash.jit_pool disabled-binding",
        ),
        "cancel cleanup call site": (
            "python -m just_akash.jit_pool cleanup --journal",
            "python -m just_akash.jit_pool disabled-cleanup --journal",
        ),
        "cross-job exact-id handoff": (
            "      runner-ids: ${{ needs.pool.outputs.runner-ids }}",
            "      runner-ids: '[]'",
        ),
        "broker operation call site": (
            '--create-operation "$CREATE_OPERATION"',
            '--create-operation "$GH_RUN_ID"',
        ),
        "run attempt identity call site": (
            '--create-operation "$CREATE_OPERATION" --run-id "$GH_RUN_ID" \\\n'
            '            --run-attempt "$GH_RUN_ATTEMPT"',
            '--create-operation "$CREATE_OPERATION" --run-id "$GH_RUN_ID" \\\n'
            '            --run-attempt "1"',
        ),
    }
    for label, (target, replacement) in mutations.items():
        assert SOURCE.count(target) == 1, f"{label}: mutation target count changed"
        mutant = SOURCE.replace(target, replacement)
        path = tmp_path / f"{label.replace(' ', '-')}.yml"
        path.write_text(mutant, encoding="utf-8")
        parsed = yaml.safe_load(mutant)
        steps = parsed["jobs"]["pool"]["steps"]
        body = next(step["run"] for step in steps if step.get("id") == "provision")
        if label == "prepare call site":
            assert '"${JIT[@]}" prepare' not in body
        elif label == "group wiring":
            assert '--org "$ORG" --group-id "$VERIFIED_RUNNER_GROUP_ID" --slots' not in body
        elif label == "verified group handoff":
            provision = next(step for step in steps if step.get("id") == "provision")
            assert provision["env"]["VERIFIED_RUNNER_GROUP_ID"] == "${{ inputs.runner-group-id }}"
        elif label == "attempt freshness":
            assert '--provider-attempt "$attempt"' not in body
        elif label == "identity publication":
            assert 'cat "$JIT_OUTPUT" >> "$GITHUB_OUTPUT"' not in body
        elif label == "deployment receipt call site":
            assert '--receipt-path "$RECEIPT_PATH"' not in body
        elif label == "group binding call site":
            all_steps = "\n".join(str(step.get("run", "")) for step in steps)
            assert "jit_pool verify-binding" not in all_steps
        elif label == "cancel cleanup call site":
            all_steps = "\n".join(str(step.get("run", "")) for step in steps)
            assert "jit_pool cleanup --journal" not in all_steps
        elif label == "cross-job exact-id handoff":
            assert parsed["jobs"]["teardown"]["with"]["runner-ids"] == "[]"
        elif label == "broker operation call site":
            render = next(step for step in steps if step.get("id") == "render")
            assert '--create-operation "$CREATE_OPERATION"' not in render["run"]
        elif label == "run attempt identity call site":
            render = next(step for step in steps if step.get("id") == "render")
            assert '--run-attempt "$GH_RUN_ATTEMPT"' not in render["run"]
        else:
            assert '-lt "${POOL_SIZE}"' not in body
