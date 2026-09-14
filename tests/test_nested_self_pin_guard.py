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
    identity_scope,
    identity_target,
    identity_violations,
    pin_format_violations,
    pr_touches_pin,
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


# ── review repairs (DEV2 on #365) ───────────────────────────────────────────


def test_a_missing_base_raises_and_is_never_judged_against_head(history):
    """MR1: a HEAD fallback in reference_ref would reopen #364. It must raise instead."""
    root, _base, _pr = history

    with pytest.raises(LookupError, match="no-such-base-364"):
        reference_ref(root, "no-such-base-364")
    with pytest.raises(LookupError, match="no-such-base-364"):
        prepare_history(root, "no-such-base-364", [])


def test_an_undecidable_base_fails_under_ci_instead_of_skipping(history, monkeypatch):
    """MR2: the real guard's preparation must FAIL under CI, never skip."""
    from tests.test_runner_pool_workflow import _prepared_reference

    root, _base, pr_only = history
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_BASE_REF", "no-such-base-364")

    try:
        with pytest.raises(AssertionError, match="cannot decide nested-pin ancestry under CI"):
            _prepared_reference(root, [_pin(pr_only)])
    except pytest.skip.Exception:
        pytest.fail(
            "the nested-pin guard SKIPPED under CI: the surface it protects went unchecked"
        )


@pytest.mark.parametrize("ref", ["main", "runner-teardown-v9", "a" * 39, "A" * 40])
def test_every_self_pin_must_be_a_40_hex_sha(ref):
    violations = pin_format_violations([SelfPin("any-workflow.yml", "job", CALLED, ref)])

    assert len(violations) == 1
    assert "not a 40-hex commit SHA" in violations[0]


def test_a_full_sha_passes_the_format_rule():
    assert pin_format_violations([SelfPin("w.yml", "j", CALLED, "0" * 40)]) == []


def _drifted_base(root: Path) -> str:
    """main after a squash that edited the called file while the pool still pins the old copy."""
    _git(root, "checkout", "-q", "main")
    pool = root / ".github/workflows/runner-pool.yml"
    pool.write_text(
        f"jobs:\n  teardown:\n    uses: {SELF_REPO}/{CALLED}@{_git(root, 'rev-parse', 'HEAD')}\n"
    )
    _git(root, "add", str(pool))
    _git(root, "commit", "-q", "-m", "pool pins A")
    pinned = _git(root, "rev-parse", "HEAD")
    _commit(root, "teardown: v2\n", "S: teardown edit squashed, pin not yet moved")
    return pinned


def _pool_pin(root: Path) -> SelfPin:
    import yaml

    doc = yaml.safe_load((root / ".github/workflows/runner-pool.yml").read_text())
    return discover_self_pins({"runner-pool.yml": doc})[0]


def test_an_unrelated_pr_on_a_drifted_base_skips_identity_with_a_note(history, monkeypatch):
    root, _base, _pr = history
    _drifted_base(root)
    _git(root, "checkout", "-q", "-b", "unrelated")
    (root / "README.md").write_text("docs\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "unrelated change")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    reference = reference_ref(root, "main")
    pin = _pool_pin(root)

    checked, notes = identity_scope(root, [pin], reference)

    assert checked == []
    assert len(notes) == 1
    assert "PENDING REPIN" in notes[0]
    assert identity_violations(root, checked, identity_target(reference)) == []
    assert reachability_violations(root, [pin], reference) == []  # reachability still applies


def test_a_pr_touching_the_called_file_on_a_drifted_base_is_identity_checked(history, monkeypatch):
    root, _base, _pr = history
    _drifted_base(root)
    _git(root, "checkout", "-q", "-b", "edits-teardown")
    _commit(root, "teardown: v3\n", "another teardown edit")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    reference = reference_ref(root, "main")
    pin = _pool_pin(root)

    checked, notes = identity_scope(root, [pin], reference)

    assert checked == [pin]
    assert notes == []
    assert len(identity_violations(root, checked, identity_target(reference))) == 1


def test_a_pr_moving_the_pin_line_is_identity_checked(history, monkeypatch):
    root, base, _pr = history
    _git(root, "checkout", "-q", "main")
    pool = root / ".github/workflows/runner-pool.yml"
    pool.write_text(f"jobs:\n  teardown:\n    uses: {SELF_REPO}/{CALLED}@{base}\n")
    _git(root, "add", str(pool))
    _git(root, "commit", "-q", "-m", "pool pins A")
    _git(root, "checkout", "-q", "-b", "moves-pin")
    moved = _git(root, "rev-parse", "HEAD")
    pool.write_text(f"jobs:\n  teardown:\n    uses: {SELF_REPO}/{CALLED}@{moved}\n")
    _git(root, "add", str(pool))
    _git(root, "commit", "-q", "-m", "move pin")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")

    checked, _notes = identity_scope(root, [_pool_pin(root)], reference_ref(root, "main"))

    assert checked == [_pool_pin(root)]


def test_off_a_pull_request_every_pin_is_identity_checked(history, monkeypatch):
    root, base, _pr = history
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)

    checked, notes = identity_scope(root, [_pin(base)], reference_ref(root, "main"))

    assert checked == [_pin(base)]
    assert notes == []


@pytest.mark.parametrize(
    "guard",
    [
        "test_the_nested_teardown_pin_matches_the_file_it_calls",
        "test_the_nested_teardown_pin_is_reachable_from_main",
    ],
)
@pytest.mark.parametrize("ref", ["main", "runner-teardown-v9"])
def test_both_real_guards_refuse_a_non_sha_self_pin_in_any_workflow(
    guard, ref, history, monkeypatch
):
    """The format rule must be WIRED into each guard, not only exist in the helper."""
    import tests.test_runner_pool_workflow as module

    root, base, _pr = history
    pins = [SelfPin("some-other-workflow.yml", "call", CALLED, ref)]
    monkeypatch.setattr(module, "_self_pins_and_root", lambda: (root, pins))
    monkeypatch.setattr(module, "_prepared_reference", lambda _root, _pins: "refs/heads/main")

    with pytest.raises(AssertionError, match="not a 40-hex commit SHA"):
        getattr(module, guard)()


# ── DEV2 delta review of f0694e3: S1, P2, P3 ────────────────────────────────


def test_off_a_pull_request_an_untouched_pin_on_a_drifted_main_is_still_identity_checked(
    history, monkeypatch
):
    """S1. A push to main after a teardown-editing squash: HEAD == main, so the diff against main
    is EMPTY and the pin is "untouched". Scoping must not apply off a pull request, or a stale
    post-merge teardown would never go red anywhere."""
    root, _base, _pr = history
    _drifted_base(root)  # leaves main checked out, drifted, pin not moved
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    reference = reference_ref(root, "main")
    pin = _pool_pin(root)
    assert not pr_touches_pin(root, pin, reference), "fixture must not touch the pin"

    checked, notes = identity_scope(root, [pin], reference)

    assert checked == [pin]
    assert notes == []
    stale = identity_violations(root, checked, identity_target(reference))
    assert len(stale) == 1
    assert "STALE copy" in stale[0]


def test_an_undecidable_merge_base_is_identity_checked_not_skipped(history, monkeypatch):
    """P2. A base with no common history cannot say what the PR touched, so it is checked."""
    root, base, _pr = history
    _git(root, "checkout", "-q", "--orphan", "unrelated-base")
    _git(root, "rm", "-rq", "--cached", ".")
    (root / "UNRELATED").write_text("x\n")
    _git(root, "add", "UNRELATED")
    _git(root, "commit", "-q", "-m", "unrelated root")
    _git(root, "checkout", "-q", "-f", "pr")
    monkeypatch.setenv("GITHUB_BASE_REF", "unrelated-base")
    reference = reference_ref(root, "unrelated-base")
    unrelated = subprocess.run(
        ["git", "-C", str(root), "merge-base", reference, "HEAD"], capture_output=True, check=False
    )
    assert unrelated.returncode != 0, "fixture must make merge-base undecidable"
    pin = SelfPin("runner-pool.yml", "teardown", CALLED, base)

    checked, notes = identity_scope(root, [pin], reference)

    assert checked == [pin]
    assert notes == []


def test_a_pin_in_a_workflow_new_to_the_pr_is_identity_checked(history, monkeypatch):
    """P3. The workflow does not exist on base, so the pin was added by this PR."""
    root, base, _pr = history
    _git(root, "checkout", "-q", "main")
    _git(root, "checkout", "-q", "-b", "adds-workflow")
    new = root / ".github/workflows/new-caller.yml"
    new.write_text(f"jobs:\n  call:\n    uses: {SELF_REPO}/{CALLED}@{base}\n")
    _git(root, "add", str(new))
    _git(root, "commit", "-q", "-m", "new workflow pinning the teardown")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    reference = reference_ref(root, "main")
    pin = SelfPin("new-caller.yml", "call", CALLED, base)
    changed = _git(root, "diff", "--name-only", "main", "HEAD").split()
    assert CALLED not in changed, (
        "fixture must reach the new-workflow leg, not the changed-file leg"
    )

    checked, notes = identity_scope(root, [pin], reference)

    assert checked == [pin]
    assert notes == []
