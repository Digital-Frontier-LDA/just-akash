"""Retain original PR checks while scheduling exactly one job at a time."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ORDER = [
    "runner-conformance",
    "invariants",
    "lint",
    "typecheck",
    "unit-tests",
    "symbol-presence-check",
    "gitleaks",
    "secret-scan-plants",
    "trufflehog",
    "detect-secrets",
    "semgrep",
    "dependency-audit",
    "e2e-shell",
    "e2e-secrets",
    "dependabot-coverage-note",
    "report",
]
ORIGINAL_JOB_HASHES = {
    "lint": "81aee752701fa834dfb33a0036d46d001bf7ce618d0021a65d2f9ff2dd9abfa7",
    "typecheck": "f49df038188d683a9025d74886be9c743b2205b656922304e64586ac014eb687",
    "unit-tests": "0dde3c9207e6c1208dfc7014085c51bad8e9d7985c84393c53356af6c70176e4",
    "symbol-presence-check": "e59698a9f270ad324bc89f12da3092393277909b8ad9129905d9be1ef96bdc33",
    "e2e-shell": "a650cb34816caeaad585251b473faed8e2c97b47534214d3edeb3e3cb6e20178",
    "e2e-secrets": "0137de95822ddc840a53d07a51dd641934c4573c3631059087494e718ff484d2",
    "dependabot-coverage-note": "696e27c7787f5845fa88a3f33e4fdd7630399b93f63f97cb5815bcbdbe6eff14",
    "invariants": "1ff937bdbb8146376a1d5b8c5ef90668d71b8a05c714433aa88829ac33057c20",
    "gitleaks": "f81ed68c8aba42eb2836ab2411b6012b866d84b9be1ab1420ccce03264ab480d",
    "report": "ff5e604143d6a6dbdffd1171c8e43c665b3ea49f0ade804f64ff7eb3e714385f",
    "secret-scan-plants": "ac29b58832957eeb4892a6ca7c2a088396a93cf53b101aab356ddbf949cf52dd",
    "trufflehog": "e3fd8d1d20c4d8a7f98c7eb0127b46e5bf23ec42cb48afbc888a3d10ab3d2c3b",
    "detect-secrets": "0d59be84e9f8ce8dc8cfd85f04f5e4e3dc06e82e3e22047e0a898002a479bc92",
    "semgrep": "1522051b9894d8949a7d8fc04f02cecef7cc8d7f51f073caa01c264a9a8577c7",
    "dependency-audit": "c7c0bbc609a16e431bf334f286f2b87ea7989dab2d3931668c2c580431045d70",
}
ORIGINAL_WORKFLOW_HASHES = {
    "ci": "ea610a7f763ac3cb0602391925dd36ee52372a02cffe688397815853ad81c637",
    "secrets": "3077ee464681c328b95d82a3c982c965c04fe3e977be7e536d3bf224520a53e1",
    "security": "4481c67a8f71a1f5a53bd2c8acb79583b6996d3e7a78bb8f931fe90c6500a95a",
    "runner-conformance": "dce0dabd561f52142838356ded65b7c085005003a23f212a90ee13af5c150782",
}
CONFORMANCE_STEPS_HASH = "7f9f3482017f2ac8014414058c21020f3df8c1b69b1905b53334b164a6049dd3"
REQUIRED = {
    "Ruff lint + format",
    "Unit tests",
    "Gitleaks",
    "TruffleHog",
    "detect-secrets",
    "Pyright type check",
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def document():
    return yaml.safe_load((ROOT / ".github/workflows/pr.yml").read_text())


def check_projection(doc):
    jobs = doc["jobs"]
    assert list(jobs) == ORDER
    for key, expected in ORIGINAL_JOB_HASHES.items():
        actual = copy.deepcopy(jobs[key])
        actual.pop("needs", None)
        actual.pop("if", None)
        assert digest(actual) == expected, key
    assert digest(jobs["runner-conformance"]["steps"]) == CONFORMANCE_STEPS_HASH


def check_serial(doc):
    jobs = doc["jobs"]
    assert list(jobs) == ORDER
    assert "needs" not in jobs[ORDER[0]]
    for index, key in enumerate(ORDER):
        job = jobs[key]
        assert job["runs-on"] == "ubuntu-latest"
        assert "uses" not in job and "strategy" not in job
        assert "!cancelled()" in job["if"] or "always()" in job["if"]
        if index:
            assert ORDER[index - 1] in job["needs"]
            assert set(job["needs"]) <= set(ORDER[:index])
            assert "github.base_ref == 'main'" in job["if"]
    assert doc["concurrency"]["cancel-in-progress"] is False


def test_all_original_job_commands_permissions_inputs_and_timeouts_are_retained():
    check_projection(document())


def test_one_ordinary_pr_root_and_all_non_pr_routes_are_preserved():
    workflows = ROOT / ".github/workflows"
    roots = []
    for path in workflows.glob("*.yml"):
        doc = yaml.safe_load(path.read_text())
        triggers = doc.get(True, doc.get("on"))
        if isinstance(triggers, dict) and "pull_request" in triggers:
            roots.append(path.name)
        assert not (isinstance(triggers, dict) and "pull_request_target" in triggers)
    assert roots == ["pr.yml"]
    for name, expected in ORIGINAL_WORKFLOW_HASHES.items():
        raw = (workflows / (name + ".yml")).read_text()
        if name == "runner-conformance":
            assert raw.count("  workflow_dispatch:\n") == 1
            original = raw.replace("  workflow_dispatch:\n", "  pull_request:\n", 1)
        else:
            assert "  pull_request:\n" not in raw
            needle = "    branches: [main]\n"
            assert raw.count(needle) == 1
            original = raw.replace(needle, needle + "  pull_request:\n    branches: [main]\n", 1)
        assert hashlib.sha256(original.encode()).hexdigest() == expected


def test_required_checks_remain_genuine_unique_jobs():
    doc = document()
    names = [job["name"] for job in doc["jobs"].values()]
    for required in REQUIRED:
        assert names.count(required) == 1
    assert doc["permissions"] == {"contents": "read"}
    writers = {
        key for key, job in doc["jobs"].items() if "write" in job.get("permissions", {}).values()
    }
    assert writers == {"report"}
    assert "github.event_name == 'schedule'" in doc["jobs"]["report"]["if"]
    assert "github.event_name == 'workflow_dispatch'" in doc["jobs"]["report"]["if"]


def test_serial_dag_survives_prior_failure_or_skip_without_overlapping_jobs():
    doc = document()
    check_serial(doc)
    # Every original independent job has an explicit status function, so GitHub's
    # implicit success() on newly added needs cannot suppress it after failed or
    # skipped predecessors. All jobs remain blocked until the previous job is terminal.
    for key in ORDER[1:]:
        condition = doc["jobs"][key]["if"]
        assert doc["jobs"][key]["needs"]
        assert "needs." not in condition
        assert "success()" not in condition and "failure()" not in condition


def test_paid_e2e_and_dependabot_predicates_stay_narrow():
    jobs = document()["jobs"]
    for key in ("e2e-shell", "e2e-secrets"):
        condition = jobs[key]["if"]
        assert "github.event.pull_request.head.repo.full_name == github.repository" in condition
        assert "github.event.pull_request.user.login != 'dependabot[bot]'" in condition
        assert jobs[key]["timeout-minutes"] == 45
    assert jobs["e2e-secrets"]["needs"] == ["e2e-shell"]
    assert "always()" in jobs["e2e-secrets"]["if"]
    assert (
        "github.event.pull_request.user.login == 'dependabot[bot]'"
        in jobs["dependabot-coverage-note"]["if"]
    )


@pytest.mark.parametrize("mutation", ["drop-check", "parallel", "cancel-owner", "weaken-scanner"])
def test_retained_scope_and_serial_checks_reject_regressions(mutation):
    doc = document()
    if mutation == "drop-check":
        del doc["jobs"]["unit-tests"]
    elif mutation == "parallel":
        doc["jobs"]["unit-tests"]["needs"] = ["runner-conformance"]
    elif mutation == "cancel-owner":
        doc["concurrency"]["cancel-in-progress"] = True
    else:
        doc["jobs"]["trufflehog"]["steps"][-1]["with"]["extra_args"] = ""
    with pytest.raises(AssertionError):
        check_projection(doc)
        check_serial(doc)


def test_new_pr_workflow_keeps_the_existing_secret_boundary(tmp_path):
    import runpy

    original = runpy.run_path(str(ROOT / ".github/scripts/check_repo_invariants.py"))
    workflow = tmp_path / ".github/workflows/pr.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text((ROOT / ".github/workflows/pr.yml").read_text())
    assert original["check_secrets"](tmp_path) == []
    workflow.write_text(workflow.read_text() + "\n# ${{ secrets.UNDECLARED_TEST_KEY }}\n")
    assert original["check_secrets"](tmp_path)
