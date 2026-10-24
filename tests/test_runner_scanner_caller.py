"""Closed scanner caller, group, and private image boundaries; wholly offline."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from http.client import BadStatusLine
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
runner_image = importlib.import_module("just_akash.runner_image")

spec = importlib.util.spec_from_file_location(
    "runner_scanner_caller", ROOT / "just_akash/runner_scanner_caller.py"
)
assert spec is not None and spec.loader is not None
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)
SHA, HEAD, SDK = "a" * 40, "b" * 40, "c" * 40


def fixture(event="workflow_dispatch"):
    ref = "refs/pull/123/merge" if event == "pull_request" else "refs/heads/main"
    env = dict(
        RUNNER_PRIVATE_PROFILE="bb-native-trufflehog",
        ORG="Borduas-Holdings",
        SCANNER_ADMISSION_STEP=subject.STEP,
        NATIVE_CALLER_CONTRACT=subject.CONTRACT,
        NATIVE_CALLER_SDK_REF=SDK,
        NATIVE_CALLER_SDK_REPOSITORY=subject.SDK_REPOSITORY,
        NATIVE_CALLER_REPOSITORY_SCOPE="false",
        NATIVE_CALLER_PULL_READER="false",
        NATIVE_CALLER_REGISTRY_SOPS="true",
        NATIVE_CALLER_CREDENTIALS_SOPS="false",
        GITHUB_REPOSITORY=subject.REPOSITORY,
        GITHUB_EVENT_NAME=event,
        GITHUB_REF=ref,
        GITHUB_SHA=SHA,
        GITHUB_WORKFLOW_SHA=SHA,
        GITHUB_WORKFLOW_REF=subject.REPOSITORY + "/" + subject.ROOT_WORKFLOW + "@" + ref,
        GITHUB_JOB="pool",
        GITHUB_RUN_ID="77",
        GITHUB_RUN_ATTEMPT="2",
        RUNNER_ENVIRONMENT="github-hosted",
        RUNNER_NAME="PUBLIC_HOSTED_RUNNER",
    )
    env.update(
        {
            "SCANNER_POOL_SIZE": "1",
            "SCANNER_MIN_POOL_SIZE": "1",
            "SCANNER_CPU": "4",
            "SCANNER_MEMORY": "16Gi",
            "SCANNER_STORAGE": "96Gi",
            "SCANNER_EPHEMERAL": "true",
            "SCANNER_PROVIDER_SELECT": "cheapest",
            "SCANNER_IMAGE": "",
            "SCANNER_REGISTRY_HOST": "https://index.docker.io/v1/",
            "SCANNER_REGISTRY_USERNAME": "jobordu",
            "SCANNER_AGE_PRESENT": "true",
            "SCANNER_PASSWORD_PRESENT": "false",
            "SCANNER_LABEL": "trufflehog-77-2",
            "SCANNER_PLACEMENT": "borduas-native-trufflehog-run-77-end",
            "SCANNER_TAG_PREFIX": "ci-blazing-back-native-trufflehog",
            "SCANNER_OWNED_PROVIDERS": json.dumps(sorted(runner_image.NATIVE_READER_PROVIDERS)),
        }
    )
    native_head = HEAD if event == "pull_request" else SHA
    repo = dict(id=subject.REPOSITORY_ID, full_name=subject.REPOSITORY, private=True)
    run = dict(
        id=77,
        run_attempt=2,
        path=subject.ROOT_WORKFLOW,
        event=event,
        head_sha=native_head,
        head_branch="feature/m06" if event == "pull_request" else "main",
        status="in_progress",
        conclusion=None,
        repository=repo,
        head_repository=repo,
        referenced_workflows=[
            dict(path=subject.SDK_REPOSITORY + "/" + subject.SDK_WORKFLOW + "@" + SDK, sha=SDK)
        ],
    )
    pull = dict(
        number=123,
        state="open",
        merge_commit_sha=SHA,
        head=dict(sha=HEAD, ref="feature/m06", repo=repo),
        base=dict(ref="main", repo=repo),
    )
    job = dict(
        id=88,
        name=subject.JOB,
        run_id=77,
        run_attempt=2,
        head_sha=native_head,
        status="in_progress",
        conclusion=None,
        runner_name=env["RUNNER_NAME"],
        runner_id=999,
        labels=["ubuntu-latest"],
        steps=[dict(name=subject.STEP, status="in_progress", conclusion=None)],
    )
    data = {
        subject.ROOT: repo,
        subject.ROOT + "/actions/runs/77": run,
        subject.ROOT + "/pulls/123": pull,
        subject.ROOT + "/git/ref/heads/main": dict(object=dict(sha=SHA)),
        subject.ROOT + "/actions/runs/77/attempts/2/jobs?per_page=100&page=1": dict(
            total_count=1, jobs=[job]
        ),
        subject.ROOT + "/actions/jobs/88": job,
    }
    group_root = "/orgs/Borduas-Holdings/actions/runner-groups"
    group = dict(
        id=5,
        default=True,
        visibility="selected",
        allows_public_repositories=False,
        restricted_to_workflows=False,
        selected_workflows=[],
    )
    data.update(
        {
            group_root + "?per_page=100&page=1": dict(total_count=1, runner_groups=[group]),
            group_root + "?per_page=100&page=2": dict(total_count=1, runner_groups=[]),
            group_root + "/5": group,
            group_root + "/5/repositories?per_page=100&page=1": dict(
                total_count=1, repositories=[repo]
            ),
            group_root + "/5/repositories?per_page=100&page=2": dict(
                total_count=1, repositories=[]
            ),
        }
    )
    calls = []

    def get(route):
        calls.append(route)
        return copy.deepcopy(data[route])

    return env, data, calls, get


def test_actual_main_entry_admits_exact_private_bb_job():
    env, _, calls, get = fixture()
    subject.admit(env, get)
    assert len(calls) == 21
    assert sum("runner-groups" in p for p in calls) == 12


@pytest.mark.parametrize(
    "field,value",
    [
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_REPOSITORY", "Borduas-Holdings/blazing"),
        ("RUNNER_ENVIRONMENT", "self-hosted"),
        ("GITHUB_JOB", "scanner"),
        ("NATIVE_CALLER_PULL_READER", "true"),
        ("NATIVE_CALLER_REPOSITORY_SCOPE", "true"),
        ("NATIVE_CALLER_CREDENTIALS_SOPS", "true"),
        ("RUNNER_PRIVATE_PROFILE", "bb-ce1"),
        ("GITHUB_WORKFLOW_SHA", HEAD),
    ],
)
def test_wrong_authority_refuses_before_registration_or_credentials(field, value):
    env, _, _, get = fixture()
    env[field] = value
    with pytest.raises(subject.CallerHeld):
        subject.admit(env, get)


@pytest.mark.parametrize(
    "change",
    [
        "public",
        "other_repo",
        "main_moved",
        "wrong_sdk",
        "wrong_runner",
        "group_all",
        "group_public",
        "group_bb_absent",
    ],
)
def test_native_job_source_and_group_fail_closed(change):
    env, data, _, get = fixture()
    if change == "public":
        data[subject.ROOT]["private"] = False
    elif change == "other_repo":
        data[subject.ROOT]["id"] = 3
    elif change == "main_moved":
        data[subject.ROOT + "/git/ref/heads/main"]["object"]["sha"] = HEAD
    elif change == "wrong_sdk":
        data[subject.ROOT + "/actions/runs/77"]["referenced_workflows"][0]["sha"] = HEAD
    elif change == "wrong_runner":
        data[subject.ROOT + "/actions/jobs/88"]["runner_name"] = "other"
    elif change == "group_all":
        data["/orgs/Borduas-Holdings/actions/runner-groups/5"]["visibility"] = "all"
    elif change == "group_public":
        data["/orgs/Borduas-Holdings/actions/runner-groups/5"]["allows_public_repositories"] = True
    elif change == "group_bb_absent":
        data["/orgs/Borduas-Holdings/actions/runner-groups/5/repositories?per_page=100&page=1"][
            "repositories"
        ] = [dict(id=1074974924, full_name="Borduas-Holdings/blazing", private=True)]
    with pytest.raises(subject.CallerHeld):
        subject.admit(env, get)


@pytest.mark.parametrize(
    "stage",
    [
        subject.STEP,
        "Preflight — the runner PAT must still be valid",
        "Provision (retry down candidates, tag before waiting)",
    ],
)
def test_each_real_active_boundary_is_independently_admitted(stage):
    env, data, _, get = fixture()
    env["SCANNER_ADMISSION_STEP"] = stage
    data[subject.ROOT + "/actions/jobs/88"]["steps"][0]["name"] = stage
    subject.admit(env, get)


@pytest.mark.parametrize("where", ["open", "read"])
def test_native_transport_error_has_only_fixed_diagnostic(monkeypatch, where):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, *args):
            raise BadStatusLine("PUBLIC_OPAQUE_QUERY_SENTINEL")

    class Opener:
        def open(self, *args, **kwargs):
            if where == "open":
                raise BadStatusLine("PUBLIC_OPAQUE_QUERY_SENTINEL")
            return Response()

    monkeypatch.setattr(subject.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(subject.CallerHeld) as caught:
        subject.NativeGET("PUBLIC_SYNTHETIC_TOKEN")(subject.ROOT)
    assert "SENTINEL" not in str(caught.value)
    assert caught.value.__context__ is None


def profile():
    return dict(
        RUNNER_PRIVATE_PROFILE="bb-native-trufflehog",
        RUNNER_PUBLIC_PROFILE="",
        RUNNER_IMAGE="",
        RUNNER_REGISTRY_HOST="https://index.docker.io/v1/",
        RUNNER_REGISTRY_USERNAME="jobordu",
        RUNNER_REGISTRY_PASSWORD="",
        PRIVATE_PROFILE_SOPS="true",
        PRIVATE_PROFILE_AGE_PRESENT="true",
        PRIVATE_PROFILE_PASSWORD_PRESENT="false",
        RUNNER_NATIVE_PULL_READER="false",
        RUNNER_NATIVE_REPOSITORY_SCOPE="false",
        RUNNER_ENVIRONMENT="github-hosted",
        GITHUB_REPOSITORY=subject.REPOSITORY,
        PRIVATE_PROFILE_SOURCE=subject.SDK_REPOSITORY,
        PRIVATE_PROFILE_ORG="Borduas-Holdings",
        PRIVATE_PROFILE_EPHEMERAL="true",
        PRIVATE_PROFILE_POOL_SIZE="1",
        PRIVATE_PROFILE_MIN_POOL_SIZE="1",
        PRIVATE_PROFILE_CPU="4",
        PRIVATE_PROFILE_MEMORY="16Gi",
        PRIVATE_PROFILE_STORAGE="96Gi",
        SCANNER_NATIVE_ADMITTED="true",
        GITHUB_RUN_ID="77",
        GITHUB_RUN_ATTEMPT="2",
        PRIVATE_PROFILE_LABEL="trufflehog-77-2",
        PRIVATE_PROFILE_PLACEMENT="borduas-native-trufflehog-run-77-end",
        PRIVATE_PROFILE_TAG_PREFIX="ci-blazing-back-native-trufflehog",
        PRIVATE_PROFILE_OWNED_PROVIDERS=json.dumps(sorted(runner_image.NATIVE_READER_PROVIDERS)),
    )


def test_scanner_role_is_distinct_from_ce1_and_reader_roles(monkeypatch):
    env = profile()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert runner_image.validate_private_profile("bb-native-trufflehog") == "trufflehog-77-2"
    with pytest.raises(ValueError):
        runner_image.validate_private_profile("bb-ce1")
    monkeypatch.setenv("RUNNER_NATIVE_PULL_READER", "true")
    with pytest.raises(ValueError):
        runner_image.validate_private_profile("bb-native-trufflehog")


@pytest.mark.parametrize(
    "field,value",
    [
        ("SCANNER_NATIVE_ADMITTED", ""),
        ("PRIVATE_PROFILE_POOL_SIZE", "2"),
        ("PRIVATE_PROFILE_STORAGE", "512Gi"),
        ("PRIVATE_PROFILE_PLACEMENT", "borduas-apps-run-77-end"),
        ("PRIVATE_PROFILE_OWNED_PROVIDERS", "[]"),
    ],
)
def test_scanner_fixed_resources_ownership_and_admission(field, value, monkeypatch):
    env = profile()
    env[field] = value
    for key, item in env.items():
        monkeypatch.setenv(key, item)
    with pytest.raises(ValueError):
        runner_image.validate_private_profile("bb-native-trufflehog")


def test_workflow_gate_precedes_sops_and_all_registration_and_deploy_edges():
    workflow = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())
    pool = workflow["jobs"]["pool"]
    steps = pool["steps"]
    gate = next(i for i, s in enumerate(steps) if s.get("id") == "scanner_caller")
    assert gate == 1
    assert gate < next(i for i, s in enumerate(steps) if s.get("id") == "credentials_sops")
    pat = next(s for s in steps if s.get("id") == "pat")
    assert pat["run"].index("runner_scanner_caller.py") < pat["run"].index("registration-token")
    provision = next(s for s in steps if s.get("id") == "provision")["run"]
    assert "verify_scanner_caller || exit 1\n  RESP=$(gh api --method POST" in provision
    assert "verify_scanner_caller || exit 1\n  AKASH_PROVIDERS=''" in provision
    assert (
        "needs.pool.outputs.scanner-native-admitted == 'true'"
        in workflow["jobs"]["teardown"]["if"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("SCANNER_CPU", "8"),
        ("SCANNER_EPHEMERAL", "false"),
        ("SCANNER_IMAGE", "other"),
        ("SCANNER_AGE_PRESENT", "false"),
        ("SCANNER_OWNED_PROVIDERS", "[]"),
    ],
)
def test_wrong_profile_refuses_before_any_native_or_registration_request(field, value):
    env, _, calls, get = fixture()
    env[field] = value
    with pytest.raises(subject.CallerHeld):
        subject.admit(env, get)
    assert calls == []


def test_actual_private_renderer_keeps_credentials_out_of_guest(monkeypatch, tmp_path):
    env = profile()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    actual = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())
    render = next(s for s in actual["jobs"]["pool"]["steps"] if s.get("id") == "render")["run"]
    text = render.split("cat > /tmp/runner-sdl.yaml <<SDL\n", 1)[1].split("\nSDL", 1)[0]
    mapping = {
        "ORG": "Borduas-Holdings",
        "RUNNER_LABEL": "trufflehog-77-2",
        "EPHEMERAL": "true",
        "CPU": "4",
        "MEMORY": "16Gi",
        "STORAGE": "96Gi",
        "PLACEMENT_KEY": "borduas-native-trufflehog-run-77-end",
        "POOL_SIZE": "1",
    }
    for key, value in mapping.items():
        text = text.replace("${" + key + "}", value)
    path = tmp_path / "runner-sdl.yaml"
    path.write_text(text)
    verified = []
    monkeypatch.setattr(
        runner_image,
        "verify_native_reader_role",
        lambda username, password: verified.append(username),
    )
    runner_image.configure(
        path,
        image="",
        host="https://index.docker.io/v1/",
        username="jobordu",
        password="PUBLIC_SYNTHETIC_READER",
        reader_from_sops=True,
        private_profile="bb-native-trufflehog",
    )
    result = yaml.safe_load(path.read_text())
    runner = result["services"]["runner"]
    assert runner["image"] == runner_image.NATIVE_READER_IMAGE
    assert runner["credentials"] == dict(
        host="https://index.docker.io/v1/", username="jobordu", password="PUBLIC_SYNTHETIC_READER"
    )
    assert not any(
        "DOCKERHUB" in row or "AGE" in row or "PAT" in row or "PUBLIC_SYNTHETIC_READER" in row
        for row in runner["env"]
    )
    assert "RUNNER_TOKEN=@@RUNNER_TOKEN@@" in runner["env"]
    assert verified == ["jobordu"]


def test_actual_workflow_wrapper_refuses_substituted_helper_before_execution(tmp_path):
    import subprocess

    workflow = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())
    step = next(s for s in workflow["jobs"]["pool"]["steps"] if s.get("id") == "scanner_caller")
    checkout = tmp_path / "checkout"
    (checkout / "just_akash").mkdir(parents=True)
    marker = tmp_path / "PUBLIC_EXECUTION_MARKER"
    (checkout / "just_akash/runner_scanner_caller.py").write_text(
        "from pathlib import Path\nPath(" + repr(str(marker)) + ').write_text("EXECUTED")\n'
    )
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]], cwd=checkout, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert not marker.exists()
    assert "Optional hosted caller source was not verified" in result.stderr
