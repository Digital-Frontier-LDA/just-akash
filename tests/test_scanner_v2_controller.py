"""Actual fixed controller boundaries with closed native metadata/readiness shims."""

from __future__ import annotations

import copy
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_scanner_jit_v2 import CONFIG, binding  # noqa: E402

from just_akash import runner_scanner_caller as old_caller  # noqa: E402
from just_akash import scanner_v2_controller as subject  # noqa: E402
from just_akash.scanner_lease_provenance import public_sdl_request  # noqa: E402


def environment():
    b = binding()
    return dict(
        RUNNER_PRIVATE_PROFILE=subject.PROFILE,
        SCANNER_PUBLIC_PROFILE="",
        SCANNER_IMAGE="",
        SCANNER_REGISTRY_HOST="https://index.docker.io/v1/",
        SCANNER_REGISTRY_USERNAME="jobordu",
        SCANNER_REGISTRY_SOPS="true",
        SCANNER_NATIVE_PULL_READER="false",
        SCANNER_NATIVE_REPOSITORY_SCOPE="false",
        SCANNER_CREDENTIALS_SOPS="false",
        SCANNER_EPHEMERAL="true",
        SCANNER_POOL_SIZE="1",
        SCANNER_MIN_POOL_SIZE="1",
        SCANNER_CPU="4",
        SCANNER_MEMORY="16Gi",
        SCANNER_STORAGE="96Gi",
        SCANNER_SELECT="cheapest",
        SCANNER_ORG="Borduas-Holdings",
        SCANNER_DIRECT_PASSWORD_PRESENT="false",
        SCANNER_PROVIDERS=__import__("json").dumps(sorted(subject.PROVIDERS)),
        SCANNER_OWNED_PROVIDERS=__import__("json").dumps(sorted(subject.PROVIDERS)),
        GITHUB_REPOSITORY=subject.REPOSITORY,
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_REF="refs/heads/main",
        RUNNER_ENVIRONMENT="github-hosted",
        GITHUB_JOB="scanner_v2",
        GITHUB_SHA=b.workflow_sha,
        GITHUB_WORKFLOW_SHA=b.workflow_sha,
        GITHUB_WORKFLOW_REF=subject.REPOSITORY + "/" + subject.WORKFLOW + "@refs/heads/main",
        GITHUB_RUN_ID="77",
        GITHUB_RUN_ATTEMPT="2",
        SCANNER_SDK_SHA=b.sdk_sha,
        SCANNER_SDK_ROUND="1",
        SCANNER_PRODUCER_JOB_ID="88",
        SCANNER_RUNNER_GROUP_ID="37",
        RUNNER_NAME="GitHub Actions 999",
    )


def native_job():
    return dict(
        id=88,
        run_id=77,
        run_attempt=2,
        head_sha="a" * 40,
        name=subject.NATIVE_JOB,
        status="in_progress",
        conclusion=None,
        runner_name="GitHub Actions 999",
        runner_id=999,
        labels=["ubuntu-latest"],
        steps=[dict(name=subject.NATIVE_STEP, status="in_progress", conclusion=None)],
    )


class Native:
    def __init__(self, change=None):
        self.routes = []
        self.change = change

    def __call__(self, route):
        self.routes.append(route)
        if route == subject.ROOT:
            return dict(
                id=1071436278,
                full_name=subject.REPOSITORY,
                private=True,
                owner=dict(id=208689568, login="Borduas-Holdings"),
            )
        if route.endswith("/git/ref/heads/main"):
            return dict(object=dict(sha="a" * 40))
        if "/jobs?" in route:
            return dict(total_count=1, jobs=[native_job()])
        if "/actions/jobs/" in route:
            value = native_job()
            if self.change:
                value.update(self.change)
            return value
        assert route.endswith("/actions/runs/77")
        return dict(
            id=77,
            run_attempt=2,
            event="workflow_dispatch",
            path=subject.WORKFLOW,
            head_sha="a" * 40,
            head_branch="main",
            status="in_progress",
            conclusion=None,
            repository=dict(id=1071436278),
            head_repository=dict(id=1071436278),
            referenced_workflows=[
                dict(
                    path=subject.SDK_REPOSITORY + "/" + subject.SDK_WORKFLOW + "@" + "b" * 40,
                    sha="b" * 40,
                )
            ],
        )


def test_actual_native_context_and_repeat_direct_job_before_group(monkeypatch):
    native = Native()
    events = []
    monkeypatch.setattr(old_caller, "NativeGET", lambda token: native)
    monkeypatch.setattr(
        subject,
        "verify_group",
        lambda group, request, token: events.append(("group", group, len(native.routes))),
    )
    subject.admit_native(binding(), environment(), "PUBLIC_HOSTED_TOKEN")
    assert native.routes.count(subject.ROOT + "/actions/jobs/88") == 2
    assert native.routes.count(subject.ROOT + "/git/ref/heads/main") == 2
    assert events == [("group", 37, len(native.routes))]
    assert all("/registration-token" not in route for route in native.routes)


@pytest.mark.parametrize(
    "change",
    [
        dict(status="queued"),
        dict(runner_id=0),
        dict(head_sha="c" * 40),
        dict(name="FOREIGN"),
        dict(run_attempt=1),
    ],
)
def test_native_drift_refuses_before_group_or_mint(monkeypatch, change):
    native = Native(change)
    groups = []
    monkeypatch.setattr(old_caller, "NativeGET", lambda token: native)
    monkeypatch.setattr(subject, "verify_group", lambda *args: groups.append(args))
    with pytest.raises(subject.ControllerHeld):
        subject.admit_native(binding(), environment(), "PUBLIC_HOSTED_TOKEN")
    assert groups == []


def test_actual_input_binding_and_missing_new_group_never_borrow_default():
    assert subject.inputs(environment()) == binding(providers=sorted(subject.PROVIDERS)).to_dict()
    for changes in (
        dict(SCANNER_RUNNER_GROUP_ID=""),
        dict(SCANNER_ORG="Digital-Frontier-LDA"),
        dict(SCANNER_CREDENTIALS_SOPS="true"),
        dict(GITHUB_EVENT_NAME="pull_request"),
        dict(SCANNER_STORAGE="30Gi"),
    ):
        env = environment()
        env.update(changes)
        with pytest.raises(subject.ControllerHeld):
            subject.inputs(env)


def test_actual_renderer_matches_frozen_real_deploy_wrapper_without_management_env(tmp_path):
    path = tmp_path / "runner.yaml"
    b = binding()
    guest = dict(
        RUNNER_JIT_CONFIG=CONFIG,
        EPHEMERAL="true",
        RUNNER_EXECUTION_USER="root",
        RUNNER_NAME=b.runner_name,
    )
    subject.write_sdl(path, b, guest, "PUBLIC_REGISTRY_READER")
    raw = path.read_text()
    public = public_sdl_request(raw, b, CONFIG)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert public["services"]["runner"]["image"] == subject.PRIVATE_IMAGE
    assert CONFIG not in str(public) and "PUBLIC_REGISTRY_READER" not in str(public)
    assert all(
        key not in raw
        for key in ("GH_TOKEN", "RUNNER_TOKEN", "ACCESS_TOKEN", "SOPS_AGE_KEY", "AKASH_API_KEY")
    )
    with pytest.raises(FileExistsError):
        subject.write_sdl(path, b, guest, "PUBLIC_REGISTRY_READER")


def test_returned_id_readiness_has_no_label_only_or_foreign_id_success():
    b = binding()
    calls = []
    times = [0]
    row = dict(
        id=789,
        name=b.runner_name,
        status="online",
        busy=False,
        labels=[dict(name=x) for x in ("self-hosted", "Linux", "akash", b.runner_label)],
    )
    subject.wait_for_returned_runner(
        789,
        b,
        "PUBLIC_HOSTED_TOKEN",
        lambda: calls.append("fresh-admission"),
        read=lambda rid, token: copy.deepcopy(row),
        clock=lambda: times[0],
        sleep=lambda seconds: times.__setitem__(0, times[0] + seconds),
    )
    assert calls == ["fresh-admission"]
    row["id"] = 790
    with pytest.raises(subject.ControllerHeld):
        subject.wait_for_returned_runner(
            789,
            b,
            "PUBLIC_HOSTED_TOKEN",
            lambda: calls.append("wrong"),
            read=lambda rid, token: row,
            clock=lambda: times[0],
            sleep=lambda seconds: None,
        )
    assert calls == ["fresh-admission"]


def test_absent_registration_times_out_without_success_or_replacement():
    times = [0]
    reads = []

    def read(rid, token):
        reads.append(rid)
        return None

    with pytest.raises(subject.ControllerHeld):
        subject.wait_for_returned_runner(
            789,
            binding(),
            "PUBLIC_HOSTED_TOKEN",
            lambda: pytest.fail("no admission success"),
            read=read,
            clock=lambda: times[0],
            sleep=lambda seconds: times.__setitem__(0, times[0] + seconds),
        )
    assert set(reads) == {789} and len(reads) == 120 and times[0] == 240
