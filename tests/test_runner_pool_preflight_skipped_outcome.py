"""Known pre-create PAT failure never hides an attempted/ambiguous create outcome."""

import ast
import itertools
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOC = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())
EXPRESSION = DOC["jobs"]["pool"]["outputs"]["deployment_outcome"]
ORIGINAL_EXPRESSION = (
    "${{ steps.provision.outputs.deployment_outcome || "
    "steps.public_profile.outputs.deployment_outcome || "
    "steps.private_profile.outputs.deployment_outcome || "
    "(steps.pat.outputs.native_pre_create_refused == 'true' && 'no-deployment' || '') }}"
)


def evaluate(expression, steps):
    """Evaluate only the source's string/bool operator subset, not Actions runtime."""
    text = expression.removeprefix("${{").removesuffix("}}").strip()
    tree = ast.parse(text.replace("&&", " and ").replace("||", " or "), mode="eval")
    allowed = (
        ast.Expression,
        ast.BoolOp,
        ast.Compare,
        ast.Attribute,
        ast.Name,
        ast.Load,
        ast.Constant,
        ast.And,
        ast.Or,
        ast.Eq,
        ast.NotEq,
    )
    assert all(isinstance(node, allowed) for node in ast.walk(tree))
    return eval(
        compile(tree, "<fixed source expression>", "eval"),
        {"__builtins__": {}},
        {"steps": steps},
    )


def context(pat="success", provision="success", **changes):
    provision_values = {
        "deployment_outcome": "",
        "dseq": "",
        "create_ambiguous": "",
    }
    provision_values.update(changes.get("outputs", {}))
    return SimpleNamespace(
        pat=SimpleNamespace(
            outcome=pat,
            outputs=SimpleNamespace(
                native_pre_create_refused=changes.get("native", "")
            ),
        ),
        provision=SimpleNamespace(
            outcome=provision,
            conclusion=changes.get("conclusion", provision),
            outputs=SimpleNamespace(**provision_values),
        ),
        public_profile=SimpleNamespace(
            outputs=SimpleNamespace(deployment_outcome=changes.get("public", ""))
        ),
        private_profile=SimpleNamespace(
            outputs=SimpleNamespace(deployment_outcome=changes.get("private", ""))
        ),
    )


@pytest.mark.parametrize(
    "pat,provision",
    itertools.product(("success", "failure", "cancelled", "skipped", ""), repeat=2),
)
def test_only_actual_failed_preflight_and_explicitly_skipped_provision_get_new_noop(
    pat, provision
):
    steps = context(pat, provision)
    old = evaluate(ORIGINAL_EXPRESSION, steps)
    expected = "no-deployment" if (pat, provision) == ("failure", "skipped") else old
    assert evaluate(EXPRESSION, steps) == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"conclusion": ""},
        {"conclusion": "failure"},
        {"outputs": {"dseq": "123"}},
        {"outputs": {"create_ambiguous": "true"}},
        {"outputs": {"deployment_outcome": "unknown"}},
        {"outputs": {"deployment_outcome": "created", "dseq": "123"}},
    ],
)
def test_unmeasured_skip_or_known_or_ambiguous_create_never_gets_new_noop(changes):
    steps = context("failure", "skipped", **changes)
    assert evaluate(EXPRESSION, steps) == evaluate(ORIGINAL_EXPRESSION, steps)


@pytest.mark.parametrize(
    "changes",
    [
        {"native": "true"},
        {"public": "no-deployment"},
        {"private": "no-deployment"},
        {"outputs": {"deployment_outcome": "created"}},
        {"outputs": {"deployment_outcome": "unknown"}},
    ],
)
def test_original_explicit_outcomes_and_profile_refusals_are_conserved(changes):
    steps = context(**changes)
    assert evaluate(EXPRESSION, steps) == evaluate(ORIGINAL_EXPRESSION, steps)


def test_actual_missing_pat_still_fails_before_token_mint_or_following_step(tmp_path):
    pat = next(step for step in DOC["jobs"]["pool"]["steps"] if step.get("id") == "pat")
    assert "continue-on-error" not in pat
    script = tmp_path / "preflight.sh"
    script.write_text(pat["run"])
    output = tmp_path / "output"
    marker = tmp_path / "unexpected-token-mint"
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\nprintf reached > "$UNEXPECTED_MINT"\nexit 99\n')
    gh.chmod(0o700)
    result = subprocess.run(
        ["/bin/bash", "-e", str(script)],
        env={
            "PATH": str(tmp_path),
            "GH_TOKEN": "",
            "GITHUB_OUTPUT": str(output),
            "UNEXPECTED_MINT": str(marker),
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert output.read_text() == "failure_reason=RUNNER_PAT_MISSING\n"
    assert "GH_RUNNER_PAT is not set" in result.stdout
    assert not marker.exists()
    assert "deployment_outcome=" not in output.read_text()
