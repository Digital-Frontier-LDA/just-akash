"""Actual consumer entry refuses reused attempts before scanning, without manager keys."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

FILE = (
    Path(__file__).resolve().parent
    / "fixtures/scanner_v2_future_bb/scanner_v2_consumer_admission.py"
)
spec = importlib.util.spec_from_file_location("consumer", FILE)
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


def fixture():
    env = dict(
        GITHUB_REPOSITORY=subject.REPO,
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_REF="refs/heads/main",
        GITHUB_JOB="owned_trufflehog_v2",
        RUNNER_ENVIRONMENT="self-hosted",
        GITHUB_RUN_ID="42",
        GITHUB_RUN_ATTEMPT="2",
        ACKNOWLEDGED_RUNNER_ID="191",
        GITHUB_WORKFLOW_SHA="a" * 40,
        GITHUB_SHA="a" * 40,
        SCANNER_PRODUCER_SDK_SHA="b" * 40,
        GITHUB_WORKFLOW_REF=subject.REPO + "/" + subject.WORKFLOW + "@refs/heads/main",
        ACKNOWLEDGED_TARGETS='["self-hosted","linux","akash","dfci-bb-th-42-a2-r1"]',
        ACKNOWLEDGED_GROUP="borduas-th-run-42-a2-r1-end",
        ACKNOWLEDGED_JIT_RECORD_SHA="d" * 64,
    )
    own = dict(
        id=92,
        name="Run native TruffleHog v2 and original scope oracles scanner-v2-42-a2-r1",
        run_id=42,
        run_attempt=2,
        head_sha="a" * 40,
        status="in_progress",
        conclusion=None,
        runner_id=191,
        runner_group_id=7,
        runner_group_name="bb-native-trufflehog-v2",
        steps=[dict(name=subject.STEP, status="in_progress", conclusion=None)],
    )
    producer = dict(
        id=91,
        name=subject.PRODUCER,
        run_id=42,
        run_attempt=2,
        head_sha="a" * 40,
        status="completed",
        conclusion="success",
        runner_id=8,
        labels=["ubuntu-latest"],
        steps=[dict(name=subject.PRODUCER_STEP, status="completed", conclusion="success")],
    )
    calls = []

    def get(path):
        calls.append(path)
        if path == subject.ROOT:
            return dict(id=1071436278, full_name=subject.REPO, private=True)
        if "/jobs?" in path:
            return dict(total_count=2, jobs=copy.deepcopy([own, producer]))
        if "/actions/jobs/" in path:
            return copy.deepcopy(own if path.endswith("/92") else producer)
        return dict(
            id=42,
            run_attempt=2,
            head_sha="a" * 40,
            head_branch="main",
            path=subject.WORKFLOW,
            event="workflow_dispatch",
            status="in_progress",
            conclusion=None,
            repository=dict(id=1071436278),
            head_repository=dict(id=1071436278),
            referenced_workflows=[
                dict(
                    path="Digital-Frontier-LDA/just-akash/.github/workflows/runner-pool.yml@"
                    + "b" * 40,
                    sha="b" * 40,
                )
            ],
        )

    return env, own, producer, get, calls


def test_actual_current_slot_positive_with_original_producer_success():
    env, _, _, get, calls = fixture()
    assert subject.admit(env, get) == 191
    assert (
        subject.ROOT + "/actions/jobs/91" in calls and subject.ROOT + "/actions/jobs/92" in calls
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("ACKNOWLEDGED_TARGETS", '["self-hosted","linux","akash","dfci-bb-th-42-a1-r1"]'),
        ("ACKNOWLEDGED_GROUP", "borduas-th-run-42-a1-r1-end"),
        ("GITHUB_RUN_ATTEMPT", "3"),
    ],
)
def test_cached_old_attempt_slot_refuses_before_any_native_task_or_get(field, value):
    env, _, _, get, calls = fixture()
    env[field] = value
    with pytest.raises(ValueError):
        subject.admit(env, get)
    assert calls == []


@pytest.mark.parametrize(
    "bad", ["old_producer_attempt", "wrong_assigned_id", "producer_skipped", "foreign_group"]
)
def test_actual_native_attempt_assignment_and_executed_producer_are_required(bad):
    env, own, producer, get, _ = fixture()
    if bad == "old_producer_attempt":
        producer["run_attempt"] = 1
    if bad == "wrong_assigned_id":
        own["runner_id"] = 192
    if bad == "producer_skipped":
        producer["conclusion"] = "skipped"
        producer["steps"][0]["conclusion"] = "skipped"
    if bad == "foreign_group":
        own["runner_group_name"] = "Default"
    with pytest.raises(ValueError):
        subject.admit(env, get)
