"""just-akash#364: the nested self-pin guard, proven against real git histories.

The guards in test_runner_pool_workflow.py run against this repository, where the current pin
is healthy, so they cannot show that a PR-only pin goes red. These build the histories that do.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.nested_self_pins import (
    SELF_REPO,
    SelfPin,
    ShallowHistory,
    base_branch,
    discover_self_pins,
    identity_target,
    identity_violations,
    prepare_history,
    pull_request_base,
    reachability_violations,
    reference_ref,
)

CALLED = ".github/workflows/runner-teardown.yml"


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _commit(root: Path, body: str, message: str) -> str:
    (root / CALLED).write_text(body)
    _git(root, "add", CALLED)
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path):
    """main: A (teardown v1). pr: C (teardown v2), checked out — the PR's working copy."""
    root = tmp_path / "repo"
    (root / ".github/workflows").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    base_commit = _commit(root, "teardown: v1\n", "A")
    _git(root, "checkout", "-q", "-b", "pr")
    pr_commit = _commit(root, "teardown: v2\n", "C")
    return root, base_commit, pr_commit


def _pin(ref: str) -> SelfPin:
    return SelfPin("runner-pool.yml", "teardown", CALLED, ref)


def test_a_pr_only_pin_is_red_before_merge(history):
    root, _base, pr_only = history

    violations = reachability_violations(root, [_pin(pr_only)], reference_ref(root, "main"))

    assert len(violations) == 1
    assert "not an ancestor of refs/heads/main" in violations[0]


def test_a_base_ancestor_pin_is_green(history):
    root, base, _pr = history

    assert reachability_violations(root, [_pin(base)], reference_ref(root, "main")) == []


def test_a_pr_that_edits_the_called_file_can_be_green(history, monkeypatch):
    """The rule is satisfiable: pin stays at base, and identity is judged against base."""
    root, base, _pr = history
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    reference = reference_ref(root, "main")

    assert reachability_violations(root, [_pin(base)], reference) == []
    assert identity_violations(root, [_pin(base)], identity_target(reference)) == []


def test_bumping_the_pin_into_the_pr_is_red_on_both_rules(history, monkeypatch):
    root, _base, pr_only = history
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    reference = reference_ref(root, "main")

    assert len(reachability_violations(root, [_pin(pr_only)], reference)) == 1
    assert len(identity_violations(root, [_pin(pr_only)], identity_target(reference))) == 1


def test_after_the_squash_merge_main_names_the_one_line_repin(history, monkeypatch):
    root, base, _pr = history
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "-q", "--squash", "pr")
    _git(root, "commit", "-q", "-m", "S (squash of pr)")
    squash = _git(root, "rev-parse", "HEAD")
    reference = reference_ref(root, "main")

    stale = identity_violations(root, [_pin(base)], identity_target(reference))
    assert len(stale) == 1
    assert f"`uses: {SELF_REPO}/{CALLED}@{squash}`" in stale[0]
    assert identity_violations(root, [_pin(squash)], identity_target(reference)) == []
    assert reachability_violations(root, [_pin(squash)], reference) == []


def test_the_identity_target_follows_the_event(monkeypatch):
    monkeypatch.setenv("GITHUB_BASE_REF", "release")
    assert pull_request_base() == "release"
    assert base_branch() == "release"
    assert identity_target("refs/remotes/origin/release") == "refs/remotes/origin/release"
    monkeypatch.delenv("GITHUB_BASE_REF")
    assert pull_request_base() is None
    assert base_branch() == "main"
    assert identity_target("refs/remotes/origin/main") is None


def test_a_shallow_checkout_is_refused_not_misread(history, tmp_path):
    """A depth-1 clone that cannot deepen must raise, never report a good pin as orphaned."""
    source, base, _pr = history
    clone = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--branch", "main", f"file://{source}", str(clone)],
        check=True,
        capture_output=True,
    )
    assert _git(clone, "rev-parse", "--is-shallow-repository") == "true"
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "unreachable"))

    with pytest.raises(ShallowHistory):
        prepare_history(clone, "main", [_pin(base)])


def test_a_shallow_checkout_that_can_deepen_is_deepened(history, tmp_path):
    source, base, _pr = history
    clone = tmp_path / "deepen"
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--depth",
            "1",
            "--no-single-branch",
            f"file://{source}",
            str(clone),
        ],
        check=True,
        capture_output=True,
    )

    reference = prepare_history(clone, "main", [_pin(base)])

    assert _git(clone, "rev-parse", "--is-shallow-repository") == "false"
    assert reachability_violations(clone, [_pin(base)], reference) == []


def test_discovery_parses_job_and_step_uses_and_ignores_everything_else():
    workflows = {
        "a.yml": {
            "jobs": {
                "call": {"uses": f"{SELF_REPO}/.github/workflows/runner-teardown.yml@{'a' * 40}"},
                "steps": {
                    "steps": [
                        {"uses": f"{SELF_REPO}/.github/workflows/probe.yml@runner-teardown-v2"},
                        {"uses": "actions/checkout@" + "b" * 40},
                        {"run": f"echo {SELF_REPO}/.github/workflows/x.yml@{'c' * 40}"},
                    ]
                },
                "other": {
                    "uses": "Digital-Frontier-LDA/akash-github-runner/.github/workflows/r.yml@"
                    + "d" * 40
                },
            }
        },
        "b.yml": None,
    }

    pins = discover_self_pins(workflows)

    assert pins == [
        SelfPin("a.yml", "call", ".github/workflows/runner-teardown.yml", "a" * 40),
        SelfPin("a.yml", "steps", ".github/workflows/probe.yml", "runner-teardown-v2"),
    ]
