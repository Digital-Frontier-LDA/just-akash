"""Actual workflow route/source bindings, never copied admission implementation."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def selected(expression, profile):
    text = expression.removeprefix("${{").removesuffix("}} ").removesuffix("}}").strip()
    text = text.replace("inputs.runner-private-profile", "profile").replace(
        "needs.pool.result", "result"
    )
    text = text.replace("always()", "True").replace(
        "needs.pool.outputs.scanner-native-admitted", "admitted"
    )
    text = text.replace("||", " or ").replace("&&", " and ")

    def evaluate(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return {"profile": profile, "result": "failure", "admitted": "true"}[node.id]
        if isinstance(node, ast.BoolOp):
            rows = [evaluate(v) for v in node.values]
            return all(rows) if isinstance(node.op, ast.And) else any(rows)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = evaluate(node.left), evaluate(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        raise AssertionError("Unsupported actual workflow route expression")

    return evaluate(ast.parse(text, mode="eval").body)


@pytest.mark.parametrize(
    "profile,expected",
    [
        ("bb-native-trufflehog-v2-fenced", {"pool": False, "scanner_v2": True, "teardown": False}),
        ("bb-native-trufflehog-v2", {"pool": False, "scanner_v2": True, "teardown": False}),
        (
            "bb-native-trufflehog-v2-reconciled",
            {"pool": False, "scanner_v2": True, "teardown": False},
        ),
        ("", {"pool": True, "scanner_v2": False, "teardown": True}),
    ],
)
def test_actual_runner_pool_closed_route_vetoes_legacy_for_new_profile(profile, expected):
    doc = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_bytes())
    assert set(doc["jobs"]) == set(expected)
    assert {k: selected(v["if"], profile) for k, v in doc["jobs"].items()} == expected
    controller = [
        s
        for s in doc["jobs"]["scanner_v2"]["steps"]
        if s.get("name") == "Run the acknowledged scanner-v2 controller"
    ]
    assert len(controller) == 1 and not controller[0].get("continue-on-error", False)
    assert controller[0]["uses"] == "./.just-akash/.github/actions/scanner-fenced-receipts"


def test_unused_cleanup_has_no_provider_secret_owner_dseq_or_lease_qualification_contract():
    doc = yaml.safe_load((ROOT / ".github/workflows/scanner-unused-jit-cleanup.yml").read_bytes())
    contract = doc[True]["workflow_call"]
    assert set(contract["secrets"]) == {"GH_RUNNER_PAT"}
    assert set(contract["outputs"]) == {
        "unused_jit_qualified",
        "registration_absent",
        "returned_runner_id",
    }
    step = doc["jobs"]["cleanup"]["steps"][0]["run"]
    assert "normal:workflow_dispatch" not in step and "terminal:workflow_run" in step
    assert set(doc["jobs"]) == {"cleanup"}
    assert doc["jobs"]["cleanup"]["concurrency"]["cancel-in-progress"] is False
    assert not {"AKASH_API_KEY", "AKASH_API_KEYS", "SOPS_AGE_KEY"} & set(
        doc["jobs"]["cleanup"]["steps"][-1]["env"]
    )


def test_pure_controller_cannot_import_helpers_before_the_irreversible_boundary():
    tree = ast.parse((ROOT / "just_akash/scanner_v2_fenced_controller.py").read_bytes())
    f = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "provision")
    block = next(n for n in f.body if isinstance(n, ast.With))
    mark = next(
        i
        for i, n in enumerate(block.body)
        if isinstance(n, ast.Expr)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Attribute)
        and n.value.func.attr == "may_create"
    )
    assert not any(
        isinstance(n, ast.ImportFrom) and n.module in ("runner_image", "scanner_lease_provenance")
        for row in block.body[:mark]
        for n in ast.walk(row)
    )
    after = block.body[mark + 1]
    assert (
        isinstance(after, ast.Expr)
        and isinstance(after.value, ast.Call)
        and isinstance(after.value.func, ast.Name)
        and after.value.func.id == "enable_runtime"
    )
    assert {n.module for n in block.body[mark + 2 :] if isinstance(n, ast.ImportFrom)} == {
        "runner_image",
        "scanner_lease_provenance",
    }
