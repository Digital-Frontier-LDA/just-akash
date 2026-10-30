"""Pure GitHub source-fence controls; the fake runner cannot publish anything."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

SPEC = importlib.util.spec_from_file_location(
    "release_source_guard", Path(__file__).parents[1] / ".github/scripts/verify_release_source.py"
)
assert SPEC is not None and SPEC.loader is not None
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
SOURCE, MAIN, TAG_OBJECT = "a" * 40, "b" * 40, "c" * 40


@pytest.fixture
def github(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname="just-akash"\nversion="1.44.0"\n')
    monkeypatch.setenv("GITHUB_REPOSITORY", "Digital-Frontier-LDA/just-akash")
    monkeypatch.setenv("GITHUB_REF_NAME", "v1.44.0")
    w = SimpleNamespace(fault=None, calls=[], annotated=False)

    def runner(args, *, timeout):
        assert 0 < timeout <= 20
        w.calls.append(args)
        if args[:2] == ("git", "rev-parse"):
            return SOURCE
        if args[:2] == ("git", "fetch"):
            return ""
        if args[:2] == ("git", "merge-base"):
            if w.fault == "not-main-ancestor":
                raise subprocess.CalledProcessError(1, args)
            return ""
        assert args[:2] == ("gh", "api")
        path = args[2]
        if "/git/ref/tags/" in path:
            data = {
                "object": {
                    "type": "tag" if w.annotated else "commit",
                    "sha": TAG_OBJECT
                    if w.annotated
                    else MAIN
                    if w.fault == "moved-tag"
                    else SOURCE,
                }
            }
        elif "/git/tags/" in path:
            data = {
                "object": {
                    "type": "tag" if w.fault == "tag-cycle" else "commit",
                    "sha": TAG_OBJECT
                    if w.fault == "tag-cycle"
                    else MAIN
                    if w.fault == "moved-tag"
                    else SOURCE,
                }
            }
        elif "/git/ref/heads/main" in path:
            data = {"object": {"sha": MAIN}}
        elif "/releases/tags/" in path:
            if w.fault == "existing-release":
                return "{}"
            raise subprocess.CalledProcessError(
                1, args, stderr="(HTTP 403)" if w.fault == "unreadable-release" else "(HTTP 404)"
            )
        elif "/actions/workflows/" in path:
            workflow = path.split("/actions/workflows/", 1)[1].split("/")[0]
            run_id = list(m.EXPECTED_GATES).index(workflow) + 1
            row = {
                "id": run_id + (10 if w.fault == "newer-run" else 0),
                "head_sha": MAIN if w.fault == "wrong-head" else SOURCE,
                "head_branch": "main",
                "event": "push",
                "status": "in_progress" if w.fault == "pending-run" else "completed",
                "conclusion": "success",
                "run_attempt": 3 if w.fault == "new-attempt" else 2,
            }
            population = [] if w.fault == "missing-run" else [row]
            data = {"total_count": len(population), "workflow_runs": population}
        else:
            run_id = int(path.split("/actions/runs/", 1)[1].split("/")[0]) % 10
            workflow = list(m.EXPECTED_GATES)[run_id - 1]
            population = [
                {
                    "name": name,
                    "head_sha": SOURCE,
                    "status": "completed",
                    "conclusion": "skipped"
                    if w.fault == "skipped-job"
                    else "failure"
                    if w.fault == "failed-security" and workflow == "security.yml"
                    else "success",
                }
                for name in m.EXPECTED_GATES[workflow]
            ]
            if w.fault == "missing-job":
                population.pop()
            if w.fault == "duplicate-job":
                population.append(copy.deepcopy(population[0]))
            data = {
                "total_count": len(population) + (1 if w.fault == "truncated-jobs" else 0),
                "jobs": population,
            }
        return json.dumps(data)

    w.runner = runner
    return w


@pytest.mark.parametrize("annotated", [False, True])
def test_initial_and_final_source_fences_verify_peeled_remote_tag_and_exact_attempts(
    github, annotated
):
    github.annotated = annotated
    initial = m.verify_release_source(runner=github.runner)
    final = m.verify_release_source(
        expected_source=SOURCE, recorded_evidence=initial, runner=github.runner
    )
    assert initial == final
    assert all(args[0] == "git" or args[:2] == ("gh", "api") for args in github.calls)
    assert not any("release" in args and "create" in args for args in github.calls)


@pytest.mark.parametrize(
    "fault",
    [
        "missing-run",
        "pending-run",
        "wrong-head",
        "skipped-job",
        "missing-job",
        "duplicate-job",
        "failed-security",
        "truncated-jobs",
        "existing-release",
        "unreadable-release",
        "not-main-ancestor",
        "moved-tag",
        "new-attempt",
        "newer-run",
    ],
)
def test_changed_or_incomplete_source_evidence_blocks_final_publication(github, fault):
    initial = m.verify_release_source(runner=github.runner)
    github.fault = fault
    with pytest.raises((m.ReleaseHeld, subprocess.CalledProcessError)):
        m.verify_release_source(
            expected_source=SOURCE, recorded_evidence=initial, runner=github.runner
        )


@pytest.mark.parametrize("fault", ["moved-tag", "tag-cycle"])
def test_annotated_remote_tag_change_or_cycle_is_a_hold(github, fault):
    github.annotated = True
    initial = m.verify_release_source(runner=github.runner)
    github.fault = fault
    with pytest.raises(m.ReleaseHeld):
        m.verify_release_source(
            expected_source=SOURCE, recorded_evidence=initial, runner=github.runner
        )


def test_version_mismatch_refuses_before_external_reads(github, monkeypatch):
    monkeypatch.setenv("GITHUB_REF_NAME", "v1.44.1")
    with pytest.raises(m.ReleaseHeld):
        m.verify_release_source(runner=github.runner)
    assert github.calls == []


def test_recorded_source_mismatch_cannot_replace_reviewed_commit(github):
    with pytest.raises(m.ReleaseHeld):
        m.verify_release_source(expected_source=MAIN, runner=github.runner)


# Jobs a push to main never runs, each with its exact `if:`. Any other conditional job fails the
# pin: it must be classified here (with a condition that excludes push) or made a release gate.
NOT_ON_PUSH = {
    "secrets.yml": {
        "report": "always() && (github.event_name == 'schedule' || github.event_name == "
        "'workflow_dispatch') && github.ref == 'refs/heads/main'",
    },
}
REPO = Path(__file__).resolve().parents[1]


def _push_gated_jobs(workflow: str, root: Path = REPO) -> set[str]:
    """The check names of a workflow's jobs that run on a push to main.

    A job without `name:` is checked under its job id, as GitHub names it. A job with an
    `if:` must be in NOT_ON_PUSH with exactly that condition, and the condition must not
    admit a push; otherwise this raises."""
    wf = yaml.safe_load((root / ".github/workflows" / workflow).read_text(encoding="utf-8"))
    known = NOT_ON_PUSH.get(workflow, {})
    gated = set()
    for job_id, job in wf["jobs"].items():
        cond = job.get("if")
        if cond is None:
            gated.add(job.get("name", job_id))
            continue
        assert " ".join(str(cond).split()) == known.get(job_id), (
            f"{workflow}: job {job_id!r} has an unclassified if: {cond!r}; add it to "
            "NOT_ON_PUSH (if a push never runs it) or make it a release gate"
        )
        assert "'push'" not in cond and "github.event_name == 'schedule'" in cond
    return gated


def test_every_secret_scan_job_that_runs_on_push_is_a_release_gate():
    """openmix-g442m: a Secret Scan job that runs on push to main must gate a release, so a
    red plant test cannot ship. The daily report job is excluded only because its exact `if:`
    (schedule/dispatch on main) is pinned in NOT_ON_PUSH: on push it is always `skipped`, and
    the fence holds on any skipped expected job."""
    assert m.EXPECTED_GATES["secrets.yml"] == _push_gated_jobs("secrets.yml")
    assert "Secret Scan plant tests" in m.EXPECTED_GATES["secrets.yml"]
    assert "Report all-branch scan" not in m.EXPECTED_GATES["secrets.yml"]


def _gated_with(tmp_path: Path, jobs: dict) -> set[str]:
    wf_dir = tmp_path / ".github/workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "secrets.yml").write_text(yaml.safe_dump({"jobs": jobs}), encoding="utf-8")
    return _push_gated_jobs("secrets.yml", root=tmp_path)


def test_a_conditional_job_that_may_run_on_push_is_not_silently_excluded(tmp_path):
    jobs = {"late": {"name": "Late scan", "if": "github.event_name == 'push'"}}
    with pytest.raises(AssertionError, match="unclassified if"):
        _gated_with(tmp_path, jobs)


def test_a_job_without_a_name_is_gated_under_its_job_id(tmp_path):
    assert _gated_with(tmp_path, {"lint": {"runs-on": "x"}}) == {"lint"}
