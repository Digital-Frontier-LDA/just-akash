"""Actual terminal selector routes executed v2 only; skipped/absent is not CLEANED."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

FILE = (
    Path(__file__).resolve().parent
    / "fixtures/scanner_v2_future_bb/scanner_v2_callback_admission.py"
)
spec = importlib.util.spec_from_file_location("callback", FILE)
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


def fixture():
    env = dict(
        GITHUB_REPOSITORY=subject.REPO,
        GITHUB_EVENT_NAME="workflow_run",
        GITHUB_REF="refs/heads/main",
        RUNNER_ENVIRONMENT="github-hosted",
        GITHUB_WORKFLOW_REF=subject.REPO
        + "/.github/workflows/native-scanner-v2-cleanup.yml@refs/heads/main",
    )
    event = dict(action="completed", workflow_run=dict(id=42, run_attempt=2))
    pool = dict(
        id=91,
        run_id=42,
        run_attempt=2,
        head_sha="a" * 40,
        name=subject.PRODUCER,
        status="completed",
        conclusion="failure",
        runner_id=8,
        started_at="2026-10-24T10:00:00Z",
        steps=[dict(name=subject.PRODUCER_STEP, status="completed", conclusion="failure")],
    )
    rows = [pool]

    def get(path):
        if path == subject.ROOT:
            return dict(id=1071436278, private=True, full_name=subject.REPO)
        if "/jobs?" in path:
            return dict(total_count=len(rows), jobs=copy.deepcopy(rows))
        if "/actions/jobs/" in path:
            return copy.deepcopy(pool)
        return dict(
            id=42,
            run_attempt=2,
            status="completed",
            conclusion="cancelled",
            path=subject.WORKFLOW,
            event="workflow_dispatch",
            head_branch="main",
            head_sha="a" * 40,
            repository=dict(id=1071436278),
            head_repository=dict(id=1071436278),
        )

    return env, event, pool, rows, get


@pytest.mark.parametrize("state", ["failure", "cancelled", "timed_out", "success"])
def test_real_executed_provisioning_failure_or_success_routes_to_full_sdk_admission(state):
    env, event, pool, _, get = fixture()
    pool["conclusion"] = state
    pool["steps"][0]["conclusion"] = state
    assert subject.admit(env, event, get) is True


@pytest.mark.parametrize("state", ["absent", "skipped"])
def test_complete_absent_or_skipped_v2_routes_no_cleanup_without_false_qualification(state):
    env, event, pool, rows, get = fixture()
    if state == "absent":
        rows.clear()
    else:
        pool["conclusion"] = "skipped"
    assert subject.admit(env, event, get) is False


@pytest.mark.parametrize("state", ["queued", "unstarted", "controller_skipped"])
def test_incomplete_or_nonexecuted_controller_cannot_reach_cleanup(state):
    env, event, pool, _, get = fixture()
    if state == "queued":
        pool.update(status="queued", conclusion=None)
    if state == "unstarted":
        pool["started_at"] = None
    if state == "controller_skipped":
        pool["steps"][0]["conclusion"] = "skipped"
    with pytest.raises(ValueError):
        subject.admit(env, event, get)
