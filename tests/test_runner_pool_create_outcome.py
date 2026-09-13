"""The runner pool publishes its typed create outcome across both workflow boundaries.

The behaviour of the outcome itself -- which branch publishes created, no-deployment or
unknown, and that no later round downgrades it -- is tested by running the real provision
step in tests/test_runner_pool_outcome_is_monotonic.py (#347). The text checks that used to
live here read the script without running the loop, so they could not see a second round.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github/workflows/runner-pool.yml"
DOC = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
CALL = (DOC.get("on") or DOC.get(True))["workflow_call"]


def test_typed_outcome_is_published_through_both_workflow_boundaries() -> None:
    assert CALL["outputs"]["deployment_outcome"]["value"] == (
        "${{ jobs.pool.outputs.deployment_outcome }}"
    )
    assert DOC["jobs"]["pool"]["outputs"]["deployment_outcome"] == (
        "${{ steps.provision.outputs.deployment_outcome }}"
    )
    teardown_with = DOC["jobs"]["teardown"]["with"]
    assert teardown_with["deployment-outcome"] == ("${{ needs.pool.outputs.deployment_outcome }}")
    assert teardown_with["dseq"] == (
        "${{ needs.pool.outputs.deployment_outcome == 'created' "
        "&& needs.pool.outputs.dseq || '' }}"
    )
    for key in ("held_reason", "held_dseq", "held_deployment_group"):
        assert CALL["outputs"][key]["value"] == f"${{{{ jobs.teardown.outputs.{key} }}}}"
