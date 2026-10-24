"""Actual current-actor/historical-producer and consumer finality boundaries."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_scanner_v2_cleanup_engine import actor, binding, target  # noqa: E402

from just_akash import scanner_cleanup_native as subject  # noqa: E402


class Native:
    def __init__(self, mode="normal"):
        self.mode = mode
        self.actor = actor(mode)
        self.paths = []
        self.change = None
        self.work = dict(
            id=92,
            run_id=42,
            run_attempt=2,
            head_sha="a" * 40,
            name="Run native TruffleHog v2 and original scope oracles "
            + subject.slot(self.actor["target_locator"]),
            status="completed",
            conclusion="success",
            runner_id=191,
            runner_name="Owned runner 191",
        )
        self.producer = dict(
            id=91,
            run_id=42,
            run_attempt=2,
            head_sha="a" * 40,
            name=subject.PRODUCER_JOB,
            status="completed",
            conclusion="success",
            started_at="2026-10-24T10:00:00Z",
            runner_id=10,
            runner_name="GitHub Actions 10",
            labels=["ubuntu-latest"],
            steps=[dict(name=subject.PRODUCER_STEP, status="completed", conclusion="success")],
        )
        self.current = dict(
            id=77,
            run_id=self.actor["run_id"],
            run_attempt=self.actor["run_attempt"],
            head_sha=self.actor["native_head_sha"],
            name=self.actor["job_name"],
            status="in_progress",
            conclusion=None,
            runner_id=8,
            runner_name="GitHub Actions 8",
            labels=["ubuntu-latest"],
            steps=[dict(name=subject.STEP, status="in_progress", conclusion=None)],
        )

    def run(self, current):
        a = self.actor
        return dict(
            id=a["run_id"] if current else 42,
            run_attempt=a["run_attempt"] if current else 2,
            event=a["event"] if current else "workflow_dispatch",
            path=a["workflow_path"] if current else ".github/workflows/secret-scanning.yml",
            head_branch="main",
            head_sha=a["native_head_sha"] if current else "a" * 40,
            status="in_progress" if current or self.mode == "normal" else "completed",
            conclusion=None if current or self.mode == "normal" else "cancelled",
            repository=dict(id=1071436278),
            head_repository=dict(id=1071436278),
            referenced_workflows=[
                dict(path=subject.SDK + "scanner-v2-cleanup.yml@" + "c" * 40, sha="c" * 40),
                dict(path=subject.SDK + "runner-pool.yml@" + "b" * 40, sha="b" * 40),
            ],
        )

    def __call__(self, path):
        self.paths.append(path)
        if path == subject.ROOT:
            value = dict(
                id=1071436278,
                full_name=subject.REPOSITORY,
                private=True,
                owner=dict(id=208689568, login="Borduas-Holdings"),
            )
        elif "/actions/jobs/" in path:
            identity = int(path.rsplit("/", 1)[1])
            value = {77: self.current, 91: self.producer, 92: self.work}[identity]
        elif "/jobs?" in path:
            current = f"/runs/{self.actor['run_id']}/attempts/{self.actor['run_attempt']}/" in path
            rows = (
                [self.current, self.producer, self.work]
                if current and self.mode == "normal"
                else [self.current]
                if current
                else [self.producer, self.work]
            )
            value = dict(total_count=len(rows), jobs=rows)
        elif "/attempts/" in path or (
            self.mode == "terminal" and path.endswith("/actions/runs/42")
        ):
            value = self.run(False)
        else:
            value = self.run(True)
        value = copy.deepcopy(value)
        if self.change:
            value = self.change(path, value)
        return value


def environment(native, tmp_path):
    a = native.actor
    env = dict(
        GITHUB_REPOSITORY=subject.REPOSITORY,
        GITHUB_REF="refs/heads/main",
        RUNNER_ENVIRONMENT="github-hosted",
        GITHUB_JOB="cleanup",
        GITHUB_EVENT_NAME=a["event"],
        GITHUB_RUN_ID=str(a["run_id"]),
        GITHUB_RUN_ATTEMPT=str(a["run_attempt"]),
        GITHUB_SHA="a" * 40,
        GITHUB_WORKFLOW_SHA=a["workflow_sha"],
        GITHUB_WORKFLOW_REF=subject.REPOSITORY + "/" + a["workflow_path"] + "@refs/heads/main",
        SCANNER_TARGET_RUN_ID="42",
        SCANNER_TARGET_RUN_ATTEMPT="2",
        SCANNER_TARGET_ROUND="1",
        SCANNER_CLEANUP_SDK_SHA="c" * 40,
        SCANNER_ACTOR_JOB_ID="77",
        RUNNER_NAME="GitHub Actions 8",
    )
    if native.mode == "terminal":
        path = tmp_path / "public-event.json"
        path.write_text(
            json.dumps(
                dict(
                    action="completed",
                    workflow_run=dict(id=42, run_attempt=2),
                    repository=dict(id=1071436278),
                )
            )
        )
        env["GITHUB_EVENT_PATH"] = str(path)
    return env


@pytest.mark.parametrize("mode", ["normal", "terminal"])
def test_actual_actor_and_historical_native_producer_are_distinct(mode, tmp_path):
    native = Native(mode)
    env = environment(native, tmp_path)
    before = env.copy()
    current = subject.actor(env, native)
    subject.producer(current, binding(), native, target())
    assert env == before and current == native.actor
    if mode == "terminal":
        assert (
            current["run_id"] != binding()["run_id"]
            and current["native_head_sha"] != current["workflow_sha"]
        )
    assert subject.ROOT + "/actions/runs/42/attempts/2" in native.paths


@pytest.mark.parametrize("state", ["queued", "in_progress"])
def test_finished_provisioning_never_authorizes_close_while_consumer_pending(tmp_path, state):
    native = Native()
    native.work.update(status=state, conclusion=None, runner_id=0, runner_name="")
    current = subject.actor(environment(native, tmp_path), native)
    with pytest.raises(subject.CleanupHeld):
        subject.producer(current, binding(), native, target())


@pytest.mark.parametrize("conclusion", ["skipped", "cancelled", "timed_out", "failure"])
def test_authoritative_terminal_no_assignment_allows_known_id_cleanup(tmp_path, conclusion):
    native = Native()
    native.work.update(status="completed", conclusion=conclusion, runner_id=0, runner_name="")
    current = subject.actor(environment(native, tmp_path), native)
    subject.producer(current, binding(), native, target())


def test_foreign_assigned_consumer_id_cannot_substitute_for_returned_id(tmp_path):
    native = Native()
    native.work["runner_id"] = 192
    current = subject.actor(environment(native, tmp_path), native)
    with pytest.raises(subject.CleanupHeld):
        subject.producer(current, binding(), native, target())


@pytest.mark.parametrize(
    "change",
    [
        dict(conclusion="skipped"),
        dict(status="queued", conclusion=None),
        dict(runner_id=0),
        dict(started_at=None),
    ],
)
def test_skipped_unstarted_sdk_producer_is_not_execution_evidence(tmp_path, change):
    native = Native("terminal")
    native.producer.update(change)
    current = subject.actor(environment(native, tmp_path), native)
    with pytest.raises(subject.CleanupHeld):
        subject.producer(current, binding(), native, target())


def test_foreign_head_repository_or_wrong_historical_attempt_refuses(tmp_path):
    native = Native("terminal")
    env = environment(native, tmp_path)
    current = subject.actor(env, native)

    def change(path, value):
        if path.endswith("/attempts/2"):
            value["head_repository"]["id"] = 9
        return value

    native.change = change
    with pytest.raises(subject.CleanupHeld):
        subject.producer(current, binding(), native, target())


def test_callback_cannot_forge_current_actor_by_replacing_github_run_with_target(tmp_path):
    native = Native("terminal")
    env = environment(native, tmp_path)
    env["GITHUB_RUN_ID"] = "42"
    with pytest.raises(subject.CleanupHeld):
        subject.actor(env, native)


def test_terminal_handler_holds_while_a_later_producer_rerun_is_active(tmp_path):
    native = Native("terminal")
    current = subject.actor(environment(native, tmp_path), native)

    def change(path, value):
        if path.endswith("/actions/runs/42"):
            value.update(run_attempt=3, status="in_progress", conclusion=None)
        return value

    native.change = change
    with pytest.raises(subject.CleanupHeld):
        subject.producer(current, binding(), native, target())


def test_terminal_handler_keeps_historical_attempt_after_later_terminal_rerun(tmp_path):
    native = Native("terminal")
    current = subject.actor(environment(native, tmp_path), native)

    def change(path, value):
        if path.endswith("/actions/runs/42"):
            value.update(run_attempt=3, status="completed", conclusion="cancelled")
        return value

    native.change = change
    historical = subject.producer(current, binding(), native, target())
    assert historical["run_attempt"] == 2 and binding()["run_attempt"] == 2
