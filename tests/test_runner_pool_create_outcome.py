"""The runner pool distinguishes no-create proof from missing deployment identity."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github/workflows/runner-pool.yml"
DOC = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
CALL = (DOC.get("on") or DOC.get(True))["workflow_call"]
PROVISION = next(step for step in DOC["jobs"]["pool"]["steps"] if step.get("id") == "provision")
RUN = PROVISION["run"]

KNOWN_NO_BROADCAST_BRANCHES = {
    "no-eligible-provider": "No eligible provider remains",
    "invalid-provider-policy": "provider-select::must be",
    "authoritative-payment-refusal": "PaymentRequiredError|HTTP 402",
}


def test_typed_outcome_is_published_through_both_workflow_boundaries() -> None:
    assert CALL["outputs"]["deployment_outcome"]["value"] == (
        "${{ jobs.pool.outputs.deployment_outcome }}"
    )
    assert DOC["jobs"]["pool"]["outputs"]["deployment_outcome"] == (
        "${{ steps.provision.outputs.deployment_outcome }}"
    )


def test_every_known_no_broadcast_branch_is_live_and_classified_once() -> None:
    outcome_line = 'echo "deployment_outcome=no-deployment" >> "$GITHUB_OUTPUT"'
    assert RUN.count(outcome_line) == len(KNOWN_NO_BROADCAST_BRANCHES)
    for name, marker in KNOWN_NO_BROADCAST_BRANCHES.items():
        assert RUN.count(marker) == 1, f"dead baseline entry: {name} / {marker}"
        marker_at = RUN.index(marker)
        nearby = RUN[max(0, marker_at - 500) : marker_at + 500]
        assert outcome_line in nearby, f"{name} can exit without typed no-deployment proof"


def test_every_pre_submit_exit_has_an_explicit_no_deployment_write() -> None:
    submit = '"${JA[@]}" deploy --sdl'
    assert RUN.count(submit) == 1
    prefix = RUN[: RUN.index(submit)]
    exits = [line for line in prefix.splitlines() if "exit 1" in line]
    assert len(exits) == 2, "pre-submit exit population changed; classify the new branch"
    for line in exits:
        position = prefix.index(line)
        assert "deployment_outcome=no-deployment" in prefix[max(0, position - 400) : position]


def _execute_lines(lines: list[str], output: Path) -> list[str]:
    result = subprocess.run(
        ["/bin/bash", "-c", "\n".join(lines)],
        env={**os.environ, "GITHUB_OUTPUT": str(output), "DSEQ": "42"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return output.read_text(encoding="utf-8").splitlines()


def test_post_broadcast_output_loss_remains_unknown_effect_mutation(tmp_path: Path) -> None:
    initial = 'echo "deployment_outcome=unknown" >> "$GITHUB_OUTPUT"'
    promote = '[ -n "$DSEQ" ] && echo "deployment_outcome=created" >> "$GITHUB_OUTPUT"'
    assert RUN.count(initial) == 1
    assert RUN.count(promote) == 1, "mutation target must exist exactly once"

    output = tmp_path / "original"
    assert _execute_lines([initial, promote], output)[-1] == "deployment_outcome=created"

    mutant = [line for line in (initial, promote) if line != promote]
    assert len(mutant) == 1, "promotion removal must actually apply"
    output = tmp_path / "mutant"
    assert _execute_lines(mutant, output) == ["deployment_outcome=unknown"]


def test_no_deployment_is_never_the_default_or_post_submit_fallback() -> None:
    initial = RUN.index('echo "deployment_outcome=unknown"')
    submit = RUN.index('"${JA[@]}" deploy --sdl')
    assert initial < submit
    after_submit = RUN[submit:]
    no_deployment_sites = [
        line for line in after_submit.splitlines() if "deployment_outcome=no-deployment" in line
    ]
    assert len(no_deployment_sites) == 1
    assert "WALLET_UNDERFUNDED" in after_submit


def test_generic_insufficient_wording_does_not_prove_no_deployment() -> None:
    broad = "PaymentRequiredError|Insufficient balance|HTTP 402"
    exact = "PaymentRequiredError|HTTP 402"
    assert RUN.count(broad) == 1 and RUN.count(exact) == 1
    branch = RUN[RUN.index(broad) : RUN.index("WALLET_UNDERFUNDED")]
    assert branch.index(exact) < branch.index("deployment_outcome=no-deployment")
