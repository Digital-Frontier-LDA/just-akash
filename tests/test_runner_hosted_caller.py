"""Offline native metadata boundaries; no credential, GitHub or Akash operations."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "runner_hosted_caller", ROOT / "just_akash/runner_hosted_caller.py"
)
assert spec is not None and spec.loader is not None
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)
SHA, HEAD, SDK = "a" * 40, "b" * 40, "c" * 40


def fixture(event="pull_request"):
    ref = "refs/pull/123/merge" if event == "pull_request" else "refs/heads/main"
    env = dict(
        NATIVE_CALLER_CONTRACT=subject.CONTRACT,
        NATIVE_CALLER_SDK_REF=SDK,
        NATIVE_CALLER_SDK_REPOSITORY=subject.SDK_REPOSITORY,
        NATIVE_CALLER_REPOSITORY_SCOPE="true",
        NATIVE_CALLER_PULL_READER="true",
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
    calls = []

    def get(route):
        calls.append(route)
        return copy.deepcopy(data[route])

    return env, data, calls, get


@pytest.mark.parametrize("event", ["pull_request", "workflow_dispatch"])
def test_real_entry_admits_only_current_hosted_pool_and_source(event):
    env, _, calls, get = fixture(event)
    subject.admit(env, get)
    assert len(calls) == 9
    assert all(route.startswith(subject.ROOT) for route in calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("NATIVE_CALLER_CONTRACT", "other"),
        ("GITHUB_REPOSITORY", "attacker/fork"),
        ("RUNNER_ENVIRONMENT", "self-hosted"),
        ("GITHUB_JOB", "execute"),
        ("GITHUB_SHA", HEAD),
        ("GITHUB_WORKFLOW_SHA", HEAD),
        ("GITHUB_WORKFLOW_REF", subject.REPOSITORY + "/other.yml@refs/pull/123/merge"),
        ("GITHUB_EVENT_NAME", "pull_request_target"),
        ("GITHUB_REF", "refs/heads/feature"),
        ("NATIVE_CALLER_SDK_REF", "main"),
        ("NATIVE_CALLER_SDK_REPOSITORY", "attacker/sdk"),
        ("NATIVE_CALLER_REPOSITORY_SCOPE", "false"),
        ("NATIVE_CALLER_PULL_READER", "false"),
        ("NATIVE_CALLER_REGISTRY_SOPS", "false"),
        ("NATIVE_CALLER_CREDENTIALS_SOPS", "true"),
        ("GITHUB_RUN_ID", "077"),
    ],
)
def test_wrong_local_binding_refuses_without_operations(field, value):
    env, _, _, get = fixture()
    env[field] = value
    with pytest.raises(subject.CallerHeld):
        subject.admit(env, get)


@pytest.mark.parametrize(
    "target,key,value",
    [
        ("repo", "id", True),
        ("repo", "private", False),
        ("run", "path", ".github/workflows/ci.yml"),
        ("run", "run_attempt", 1),
        ("run", "head_sha", SHA),
        ("run", "status", "completed"),
        ("run", "referenced_workflows", []),
        (
            "run",
            "referenced_workflows",
            [dict(path=subject.SDK_REPOSITORY + "/" + subject.SDK_WORKFLOW + "@main", sha=SDK)],
        ),
        ("pull", "merge_commit_sha", HEAD),
        ("pull", "head", dict(sha=HEAD, ref="feature/m06", repo=dict(id=1))),
        ("pull", "base", dict(ref="other", repo=dict(id=subject.REPOSITORY_ID))),
        ("job", "name", "Other / Provision ephemeral runner pool on Akash"),
        ("job", "runner_name", "OTHER_RUNNER"),
        ("job", "head_sha", SHA),
        ("job", "run_attempt", True),
        ("job", "labels", ["self-hosted"]),
        ("job", "steps", [dict(name=subject.STEP, status="completed", conclusion="success")]),
    ],
)
def test_wrong_native_identity_refuses_before_any_write_or_decrypt(target, key, value):
    env, data, _, get = fixture()
    route = dict(
        repo=subject.ROOT,
        run=subject.ROOT + "/actions/runs/77",
        pull=subject.ROOT + "/pulls/123",
        job=subject.ROOT + "/actions/jobs/88",
    )[target]
    data[route][key] = value
    with pytest.raises(subject.CallerHeld):
        subject.admit(env, get)


def test_native_source_move_on_final_recheck_refuses():
    env, data, _, original = fixture("workflow_dispatch")
    checks = 0

    def get(route):
        nonlocal checks
        if route.endswith("/git/ref/heads/main"):
            checks += 1
            if checks == 2:
                data[route]["object"]["sha"] = HEAD
        return original(route)

    with pytest.raises(subject.CallerHeld):
        subject.admit(env, get)


def test_duplicate_or_incomplete_job_population_never_selects_a_job():
    env, data, _, get = fixture()
    route = subject.ROOT + "/actions/runs/77/attempts/2/jobs?per_page=100&page=1"
    data[route]["jobs"] *= 2
    data[route]["total_count"] = 2
    with pytest.raises(subject.CallerHeld):
        subject.admit(env, get)
    data[route]["jobs"] = []
    with pytest.raises(subject.CallerHeld):
        subject.admit(env, get)


def test_optional_workflow_gate_precedes_sensitive_steps_and_refusal_skips_rollback():
    workflow = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_bytes())
    events = workflow.get("on", workflow.get(True))
    assert events["workflow_call"]["inputs"]["native-caller-contract"]["default"] == ""
    pool = workflow["jobs"]["pool"]
    steps = pool["steps"]
    gate = next(i for i, step in enumerate(steps) if step.get("id") == "native_caller")
    sensitive = [
        i
        for i, step in enumerate(steps)
        if step.get("id") in {"credentials_sops", "pat", "private_profile", "provision"}
    ]
    assert all(gate < i for i in sensitive)
    assert steps[gate]["env"]["GH_TOKEN"] == "${{ secrets.GH_RUNNER_PAT }}"
    assert "native-caller-admitted == 'true'" in workflow["jobs"]["teardown"]["if"]
    assert "inputs.native-caller-contract == ''" in workflow["jobs"]["teardown"]["if"]


@pytest.mark.parametrize(
    "case", ["redirect", "oversize", "duplicate", "raw-error", "foreign-path", "budget"]
)
def test_actual_read_transport_is_bounded_and_sanitizes_failures(monkeypatch, case):
    transport = subject.NativeGET("PUBLIC_SYNTHETIC_TOKEN")
    opened = []

    class Response:
        status = 302 if case == "redirect" else 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            assert size == 1024 * 1024 + 1
            if case == "oversize":
                return b"x" * size
            if case == "duplicate":
                return b'{"id":1,"id":2}'
            return b'{"id":1}'

    class Opener:
        def open(self, request, timeout):
            opened.append(request)
            assert request.method == "GET" and request.full_url.startswith(
                "https://api.github.com/repos/Borduas-Holdings/blazing"
            )
            assert timeout <= 10
            if case == "raw-error":
                raise OSError("PUBLIC_OPAQUE_SENTINEL")
            return Response()

    monkeypatch.setattr(subject.urllib.request, "build_opener", lambda *args: Opener())
    route = subject.ROOT
    if case == "foreign-path":
        route = "/repos/attacker/fork"
    if case == "budget":
        transport.calls = 14
    with pytest.raises(subject.CallerHeld) as error:
        transport(route)
    assert "PUBLIC_OPAQUE_SENTINEL" not in str(error.value)
    assert error.value.__context__ is None and error.value.__cause__ is None
    if case in {"foreign-path", "budget"}:
        assert opened == []


@pytest.mark.parametrize("changed", [False, True])
def test_actual_workflow_entry_executes_only_captured_fixed_source(monkeypatch, tmp_path, changed):
    import builtins

    env, _, calls, get = fixture()
    workflow = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_bytes())
    step = next(s for s in workflow["jobs"]["pool"]["steps"] if s.get("id") == "native_caller")
    inline = step["run"].split("<<'PYCALLER'\n", 1)[1].split("\nPYCALLER", 1)[0]
    folder = tmp_path / "just_akash"
    folder.mkdir()
    raw = (ROOT / "just_akash/runner_hosted_caller.py").read_bytes()
    (folder / "runner_hosted_caller.py").write_bytes(
        raw + (b"\n# PUBLIC_SUBSTITUTION\n" if changed else b"")
    )
    monkeypatch.chdir(tmp_path)
    for key, value in dict(env, GH_TOKEN="PUBLIC_SYNTHETIC_TOKEN").items():
        monkeypatch.setenv(key, value)
    executed = []

    def captured_exec(code, namespace):
        builtins.exec(code, namespace)
        # The actual fixed source is executed; replace only its read-only network boundary.
        namespace["NativeGET"] = lambda token: get
        executed.append(True)

    if changed:
        with pytest.raises(SystemExit):
            builtins.exec(
                compile(inline, "actual-native-caller-inline", "exec"), {"exec": captured_exec}
            )
        assert executed == [] and calls == []
    else:
        builtins.exec(
            compile(inline, "actual-native-caller-inline", "exec"), {"exec": captured_exec}
        )
        assert executed == [True] and len(calls) == 9


@pytest.mark.parametrize("authority_drift", [False, True])
def test_required_native_job_projection_allows_only_unrelated_metadata_changes(authority_drift):
    env, data, _, original = fixture()
    direct = subject.ROOT + "/actions/jobs/88"

    def get(route):
        row = original(route)
        if route == direct:
            # Native completed schema includes these fields; neither grants authority.
            row.update(
                completed_at=None,
                workflow_name="PUBLIC_DIFFERENT_DISPLAY",
                extra_future_field=True,
            )
            row["steps"][0]["started_at"] = "PUBLIC_DIFFERENT_TIME"
            if authority_drift:
                row["runner_id"] += 1
        return row

    if authority_drift:
        with pytest.raises(subject.CallerHeld):
            subject.admit(env, get)
    else:
        subject.admit(env, get)
    assert data[direct]["runner_id"] == 999
