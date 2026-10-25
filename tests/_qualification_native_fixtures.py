"""Actual native-field shapes; synthetic source, hosted IDs and statuses only."""

from __future__ import annotations

import copy

from _qualification_fixtures import actor

from just_akash import keydb_qualification_cleanup_native as subject


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
            name="Run original KeyDB qualification on owned Podman runner "
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
            workflow_id=308813680,
            run_attempt=a["run_attempt"] if current else 2,
            event=a["event"] if current else "workflow_dispatch",
            path=a["workflow_path"] if current else ".github/workflows/keydb-modules-parity.yml",
            head_branch="codex/keydb-parity-owned-d8ab-20261024",
            head_sha=a["native_head_sha"] if current else "a" * 40,
            status="in_progress" if current or self.mode == "normal" else "completed",
            conclusion=None if current or self.mode == "normal" else "cancelled",
            repository=dict(id=1074974924),
            head_repository=dict(id=1074974924),
            referenced_workflows=[
                dict(
                    path=subject.SDK + "keydb-qualification-cleanup.yml@" + "c" * 40, sha="c" * 40
                ),
                dict(path=subject.SDK + "runner-pool.yml@" + "b" * 40, sha="b" * 40),
            ],
        )

    def __call__(self, path):
        self.paths.append(path)
        if path == subject.ROOT:
            value = dict(
                id=1074974924,
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
            self.mode == "recover" and path.endswith("/actions/runs/42")
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
        GITHUB_REF="refs/heads/codex/keydb-parity-owned-d8ab-20261024",
        RUNNER_ENVIRONMENT="github-hosted",
        GITHUB_JOB="cleanup",
        GITHUB_EVENT_NAME=a["event"],
        KEYDB_QUALIFICATION_CLEANUP_MODE=native.mode,
        GITHUB_RUN_ID=str(a["run_id"]),
        GITHUB_RUN_ATTEMPT=str(a["run_attempt"]),
        GITHUB_SHA=a["workflow_sha"],
        GITHUB_WORKFLOW_SHA=a["workflow_sha"],
        GITHUB_WORKFLOW_REF=subject.REPOSITORY
        + "/"
        + a["workflow_path"]
        + "@refs/heads/codex/keydb-parity-owned-d8ab-20261024",
        KEYDB_QUALIFICATION_TARGET_RUN_ID="42",
        KEYDB_QUALIFICATION_TARGET_RUN_ATTEMPT="2",
        KEYDB_QUALIFICATION_TARGET_ROUND="1",
        KEYDB_QUALIFICATION_CLEANUP_SDK_SHA="c" * 40,
        KEYDB_QUALIFICATION_ACTOR_JOB_ID="77",
        RUNNER_NAME="GitHub Actions 8",
    )
    return env
