"""Closed optional receipt transport with the actual unchanged SDK journal."""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import types
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
subject = importlib.import_module("just_akash.runner_create_receipt")

SHA = "a" * 40
REAL_RUN = subprocess.run
REAL_POPEN = subprocess.Popen
SDK_SHA = "f" * 40
OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"


@pytest.fixture(autouse=True)
def no_external_effect(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("NO_REAL_NETWORK_GIT_OR_CHILD")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(urllib.request, "build_opener", forbidden)
    for name in ("run", "Popen", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)


@pytest.fixture
def case(tmp_path, monkeypatch):
    env = dict(
        RUNNER_CREATE_RECEIPT_CONTRACT=subject.CONTRACT,
        GITHUB_REPOSITORY=subject.REPOSITORY,
        GITHUB_REPOSITORY_ID=str(subject.REPOSITORY_ID),
        GITHUB_REF="refs/heads/main",
        GITHUB_EVENT_NAME="push",
        GITHUB_JOB="pool",
        RUNNER_ENVIRONMENT="github-hosted",
        RUNNER_OS="Linux",
        RUNNER_NAME="GitHub Actions synthetic",
        GITHUB_WORKFLOW_REF=subject.REPOSITORY + "/" + subject.CALLER + "@refs/heads/main",
        GITHUB_WORKFLOW_SHA=SHA,
        GITHUB_SHA=SHA,
        RUNNER_RECEIPT_SDK_REF=SDK_SHA,
        GITHUB_RUN_ID="777",
        GITHUB_RUN_ATTEMPT="1",
        RUNNER_RECEIPT_LABEL="sentry-production-777-1",
        RUNNER_RECEIPT_GROUP="borduas-owned-sentry-a1-run-777-end",
        RUNNER_RECEIPT_TAG="ci-blazing-back-sentry-production",
        RUNNER_TEMP=str(tmp_path),
        RUNNER_RECEIPT_READ_TOKEN="PUBLIC_SYNTHETIC_HOSTED_METADATA_READER",
    )
    repo = dict(
        id=subject.REPOSITORY_ID, full_name=subject.REPOSITORY, private=True, visibility="private"
    )
    run = dict(
        id=777,
        run_attempt=1,
        head_sha=SHA,
        head_branch="main",
        path=subject.CALLER,
        event="push",
        status="in_progress",
        repository=repo,
        head_repository=repo,
        referenced_workflows=[
            dict(path=subject.SDK + "/" + subject.SDK_WORKFLOW + "@" + SDK_SHA, sha=SDK_SHA)
        ],
    )
    job = dict(
        id=501,
        name=subject.NATIVE_JOB,
        run_id=777,
        run_attempt=1,
        head_sha=SHA,
        status="in_progress",
        labels=["ubuntu-latest"],
        runner_name=env["RUNNER_NAME"],
    )
    branch = dict(name="main", protected=True, commit=dict(sha=SHA))
    rows = dict(
        env=env, repo=repo, run=run, job=job, branch=branch, extra=[], calls=[], capture_calls=[]
    )

    def get(path, token):
        assert token == env["RUNNER_RECEIPT_READ_TOKEN"]
        rows["calls"].append(path)
        if path == "/repos/" + subject.REPOSITORY:
            return rows["repo"]
        if path.endswith("/actions/runs/777"):
            return rows["run"]
        if path.endswith("/branches/main"):
            return rows["branch"]
        if path.endswith("/attempts/1/jobs?per_page=100&page=1"):
            jobs = [rows["job"], *rows["extra"]]
            return dict(total_count=len(jobs), jobs=jobs)
        raise AssertionError("CLOSED_SYNTHETIC_ROUTE")

    sources = {n: (ROOT / n).read_bytes() for n in subject.SOURCE_FILES}

    def capture(context, root):
        assert root == ROOT and context["sdk_sha"] == SDK_SHA
        rows["capture_calls"].append(True)
        return dict(sources)

    monkeypatch.setattr(subject, "sdk_capture", capture)
    rows.update(get=get, sources=sources)
    return rows


def start(case):
    context = subject.begin(case["env"], ROOT, case["get"])
    decoder = subject.journal_decoder(context, ROOT)
    journal = sys.modules["just_akash.deployment_receipt"]
    return context, subject.storage(case["env"], context), decoder, journal


def write_journal(case, state="create_response_received", round_number=1):
    context, folder, decoder, journal = start(case)
    group = context["group"]
    sdl = (
        f"version: '2.0'\nservices:\n  runner:\n    image: private.example/synthetic\n"
        "    env: [RUNNER_TOKEN=PUBLIC_SYNTHETIC_TENANT_TOKEN]\nprofiles:\n"
        f"  placement:\n    {group}: {{pricing: {{runner: {{denom: uact, amount: 1}}}}}}\n"
        f"deployment:\n  runner:\n    {group}: {{profile: runner, count: 1}}\n"
    )
    operation = f"gh:1071436278:777:1:{round_number}"
    prepared = journal.prepare_receipt(
        str(folder / f"round-{round_number}.json"),
        operation_id=operation,
        owner=OWNER,
        sdl_content=sdl,
        credential_binding=dict(credential_index=0, credential_count=1),
    )
    if state == "prepared":
        return context, folder, decoder, journal
    submitting = journal.mark_submitting(*prepared)
    if state == "submitting":
        return context, folder, decoder, journal
    journal.mark_create_response_received(
        *submitting,
        dseq="1",
        deployment_response=dict(
            dseq="1",
            manifest="PUBLIC_SYNTHETIC_CONSOLE_BODY",
            txhash="https://synthetic.invalid/?credential=PUBLIC_SYNTHETIC_OPAQUE",
        ),
    )
    return context, folder, decoder, journal


def test_actual_journal_ACK_candidate_seals_without_secret_bytes_and_cleans_after_upload(case):
    context, folder, _, _ = write_journal(case)
    assert subject.round_identity(case["env"], ROOT, 1, "") == ("1", OWNER)
    report = subject.seal(case["env"], ROOT)
    assert report["producer"]["native_job_id"] == 501
    assert report["records"][0]["candidate"]["authority"] == "candidate"
    assert report["close_or_replay_authority"] is False and not report["missing_journal"]
    assert (
        not report["unresolved_submission"] and not report["pre_upload_forced_kill_gap_qualified"]
    )
    text = (folder / "create-candidates.json").read_text()
    for value in [
        "PUBLIC_SYNTHETIC_TENANT_TOKEN",
        "PUBLIC_SYNTHETIC_CONSOLE_BODY",
        "PUBLIC_SYNTHETIC_OPAQUE",
        "PUBLIC_SYNTHETIC_HOSTED_METADATA_READER",
    ]:
        assert value not in text
    assert (
        "transaction_identifiers_digest" in text and "create_transaction_identifiers" not in text
    )
    subject.cleanup(case["env"], "success")
    assert not folder.exists()


@pytest.mark.parametrize("state", ["prepared", "submitting"])
def test_unacknowledged_journal_never_authorizes_outer_retry_and_survives_transport(case, state):
    _, folder, _, _ = write_journal(case, state)
    before = (folder / "round-1.json").read_bytes()
    with pytest.raises(ValueError):
        subject.round_identity(case["env"], ROOT, 1, "")
    assert (folder / "round-1.json").read_bytes() == before
    report = subject.seal(case["env"], ROOT)
    assert report["unresolved_submission"] and not report["close_or_replay_authority"]


def test_missing_journal_is_HOLD_not_no_deployment(case):
    _, folder, _, _ = start(case)
    with pytest.raises(FileNotFoundError):
        subject.round_identity(case["env"], ROOT, 1, "")
    report = subject.seal(case["env"], ROOT)
    assert report["missing_journal"] and not report["close_or_replay_authority"]
    assert not list(folder.glob("round-*.json"))


def test_existing_begin_and_journal_block_same_operation_replay(case):
    context, folder, _, journal = write_journal(case)
    with pytest.raises(FileExistsError):
        subject.begin(case["env"], ROOT, case["get"])
    before = (folder / "round-1.json").read_bytes()
    with pytest.raises(RuntimeError):
        journal.prepare_receipt(
            str(folder / "round-1.json"),
            operation_id="gh:1071436278:777:1:1",
            owner=OWNER,
            sdl_content=(
                f"profiles:\n  placement:\n    {context['group']}: {{}}\n"
                f"deployment:\n  runner:\n    {context['group']}: {{}}\n"
            ),
        )
    assert (folder / "round-1.json").read_bytes() == before


@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped", ""])
def test_failed_upload_keeps_local_recovery_data(case, outcome):
    _, folder, _, _ = write_journal(case)
    subject.seal(case["env"], ROOT)
    with pytest.raises(ValueError):
        subject.cleanup(case["env"], outcome)
    assert (folder / "round-1.json").is_file()


def test_main_move_refuses_next_create_but_still_seals_existing_ACK(case):
    _, folder, _, _ = write_journal(case)
    case["branch"]["commit"]["sha"] = "b" * 40
    with pytest.raises(ValueError):
        subject.authorize_round(case["env"], ROOT, 2, case["get"])
    assert not (folder / "round-2.json").exists()
    assert subject.seal(case["env"], ROOT)["records"][0]["candidate"]["dseq"] == "1"


def test_conflicting_legacy_identity_never_overwrites_ACK(case):
    _, folder, _, _ = write_journal(case)
    before = (folder / "round-1.json").read_bytes()
    with pytest.raises(ValueError):
        subject.round_identity(case["env"], ROOT, 1, "2")
    assert (folder / "round-1.json").read_bytes() == before


@pytest.mark.parametrize(
    "target,key,value",
    [
        ("env", "GITHUB_REF", "refs/pull/1/merge"),
        ("env", "RUNNER_CREATE_RECEIPT_CONTRACT", "unknown"),
        (
            "env",
            "GITHUB_WORKFLOW_REF",
            subject.REPOSITORY + "/.github/workflows/other.yml@refs/heads/main",
        ),
        ("env", "RUNNER_RECEIPT_LABEL", "sentry-777-1"),
        ("env", "RUNNER_RECEIPT_GROUP", "borduas-sentry-run-777-end"),
        ("env", "GITHUB_JOB", "build"),
        ("env", "RUNNER_ENVIRONMENT", "self-hosted"),
        ("repo", "id", True),
        ("run", "run_attempt", True),
        ("run", "path", ".github/workflows/other.yml"),
        ("branch", "protected", False),
        ("job", "head_sha", "b" * 40),
        ("job", "runner_name", "other-hosted-runner"),
    ],
)
def test_actual_native_or_immutable_caller_mismatch_refuses_before_local_receipt(
    case, target, key, value
):
    case[target][key] = value
    with pytest.raises(ValueError):
        subject.begin(case["env"], ROOT, case["get"])
    assert not list(Path(case["env"]["RUNNER_TEMP"]).iterdir())


def test_actual_callee_reference_is_bound_not_caller_workflow_sha(case):
    case["run"]["referenced_workflows"][0]["sha"] = "0" * 40
    with pytest.raises(ValueError):
        subject.begin(case["env"], ROOT, case["get"])
    assert not list(Path(case["env"]["RUNNER_TEMP"]).iterdir())


def test_duplicate_native_job_refuses(case):
    case["extra"].append(copy.deepcopy(case["job"]))
    with pytest.raises(ValueError):
        subject.begin(case["env"], ROOT, case["get"])


@pytest.mark.parametrize("change", ["operation", "group", "duplicate_json", "symlink", "mode"])
def test_forged_or_unheld_journal_refuses(case, change):
    _, folder, _, _ = write_journal(case)
    p = folder / "round-1.json"
    raw = p.read_bytes()
    if change == "operation":
        p.write_bytes(raw.replace(b"gh:1071436278:777:1:1", b"gh:1071436278:777:2:1"))
    elif change == "group":
        row = json.loads(raw)
        row["group_population"][0]["name"] = "other"
        p.write_text(json.dumps(row))
    elif change == "duplicate_json":
        p.write_bytes(b'{"state":"prepared",' + raw[1:])
    elif change == "symlink":
        p.unlink()
        p.symlink_to(folder / "source.json")
    else:
        p.chmod(0o644)
    with pytest.raises((ValueError, RuntimeError, OSError)):
        subject.round_identity(case["env"], ROOT, 1, "")


def test_captured_native_journal_module_bytes_are_used_after_path_replacement(case, monkeypatch):
    context, _, _, _ = start(case)
    original = case["sources"]["just_akash/deployment_receipt.py"]
    assert b"def decode_receipt" in original
    decoder = subject.journal_decoder(context, ROOT)
    assert decoder.__code__.co_filename.endswith("just_akash/deployment_receipt.py")
    changed = dict(case["sources"])
    changed["just_akash/address.py"] += b"\nraise AssertionError('unadmitted helper')\n"
    monkeypatch.setattr(subject, "sdk_capture", lambda *args: changed)
    with pytest.raises(ValueError):
        subject.journal_decoder(context, ROOT)


def test_round_reauthorization_checks_source_and_existing_round(case):
    write_journal(case)
    subject.authorize_round(case["env"], ROOT, 2, case["get"])
    with pytest.raises(ValueError):
        subject.authorize_round(case["env"], ROOT, 1, case["get"])


def test_workflow_supported_upload_and_old_mode_guards():
    value = yaml.safe_load((ROOT / subject.SDK_WORKFLOW).read_bytes())
    on = value.get("on", value.get(True))
    job = value["jobs"]["pool"]
    steps = job["steps"]
    assert on["workflow_call"]["inputs"]["runner-create-receipt-contract"]["default"] == ""
    assert value["permissions"] == {"contents": "read"}
    before = next(i for i, s in enumerate(steps) if s.get("id") == "receipt_guard")
    pat = next(i for i, s in enumerate(steps) if s.get("id") == "pat")
    assert before < pat and "!= ''" in steps[before]["if"]
    upload = next(s for s in steps if s.get("id") == "receipt_artifact")
    assert upload["uses"] == "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    assert upload["with"]["overwrite"] is False and upload["with"]["if-no-files-found"] == "error"
    assert "always()" in upload["if"] and "steps.receipt_seal.outcome == 'success'" in upload["if"]
    body = next(s["run"] for s in steps if s.get("id") == "provision")
    assert body.index("runner_create_receipt authorize") < body.index('"${JA[@]}" deploy')
    assert (
        '--receipt-operation-id "gh:1071436278:${RUN_ID}:${GITHUB_RUN_ATTEMPT}:${attempt}"' in body
    )
    assert body.index("runner_create_receipt round") < body.index("# ⭐ PUBLISH THE DSEQ")
    assert "create_ambiguous=true" in body and '"${RECEIPT_ARGS[@]}"' in body


def private_profile_function():
    tree = ast.parse((ROOT / "just_akash/runner_image.py").read_bytes())
    function = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "validate_private_profile"
    )
    providers = next(
        n
        for n in tree.body
        if isinstance(n, ast.Assign) and n.targets[0].id == "NATIVE_READER_PROVIDERS"
    )
    namespace = {"os": os, "re": __import__("re"), "json": json}
    exec(
        compile(
            ast.Module(body=[providers, function], type_ignores=[]),
            "public-fixed-private-profile",
            "exec",
        ),
        namespace,
    )
    return namespace["validate_private_profile"], namespace["NATIVE_READER_PROVIDERS"]


@pytest.mark.parametrize("label_role", ["manual", "production"])
def test_original_private_profile_and_new_isolated_role_both_preserve_canonical_image_contract(
    case, monkeypatch, label_role
):
    function, providers = private_profile_function()
    env = dict(
        case["env"],
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
        PRIVATE_PROFILE_SOURCE=subject.SDK,
        PRIVATE_PROFILE_ORG="Borduas-Holdings",
        PRIVATE_PROFILE_OWNED_PROVIDERS=json.dumps(sorted(providers)),
        PRIVATE_PROFILE_POOL_SIZE="1",
        PRIVATE_PROFILE_MIN_POOL_SIZE="1",
        PRIVATE_PROFILE_CPU="2",
        PRIVATE_PROFILE_MEMORY="6Gi",
        PRIVATE_PROFILE_STORAGE="40Gi",
        PRIVATE_PROFILE_EPHEMERAL="true",
        PRIVATE_PROFILE_LABEL="sentry-production-777-1",
        PRIVATE_PROFILE_PLACEMENT="borduas-owned-sentry-a1-run-777-end",
        PRIVATE_PROFILE_TAG_PREFIX="ci-blazing-back-sentry-production",
    )
    if label_role == "manual":
        env.update(
            RUNNER_CREATE_RECEIPT_CONTRACT="",
            PRIVATE_PROFILE_LABEL="sentry-777-1",
            PRIVATE_PROFILE_PLACEMENT="borduas-sentry-run-777-end",
            PRIVATE_PROFILE_TAG_PREFIX="ci-blazing-back-sentry",
        )
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert function("bb-ce1") == env["PRIVATE_PROFILE_LABEL"]
    if label_role == "production":
        monkeypatch.setenv("RUNNER_CREATE_RECEIPT_CONTRACT", "")
        with pytest.raises(ValueError):
            function("bb-ce1")
        monkeypatch.setenv("RUNNER_CREATE_RECEIPT_CONTRACT", subject.CONTRACT)
        monkeypatch.setenv("PRIVATE_PROFILE_MEMORY", "16Gi")
        with pytest.raises(ValueError):
            function("bb-ce1")


def test_ambiguous_callee_versions_do_not_bind_the_first_matching_row(case):
    case["run"]["referenced_workflows"].append(
        dict(path=subject.SDK + "/" + subject.SDK_WORKFLOW + "@" + "b" * 40, sha="b" * 40)
    )
    with pytest.raises(ValueError):
        subject.begin(case["env"], ROOT, case["get"])


def test_dangling_recovery_leaf_is_not_an_unused_round(case):
    _, folder, _, _ = start(case)
    (folder / "round-1.json").symlink_to(folder / "absent.json")
    with pytest.raises(ValueError):
        subject.authorize_round(case["env"], ROOT, 1, case["get"])


@pytest.mark.parametrize("mode,expected_exit", [("ack", 0), ("submitting", 1), ("disabled", 0)])
def test_actual_post_deploy_shell_cannot_reach_retry_after_unresolved_receipt(
    case, tmp_path, monkeypatch, mode, expected_exit
):
    workflow = yaml.safe_load((ROOT / subject.SDK_WORKFLOW).read_bytes())
    body = next(s["run"] for s in workflow["jobs"]["pool"]["steps"] if s.get("id") == "provision")
    start_at = body.index('RECEIPT_OWNER=""')
    finish = body.index("# ⭐ PUBLISH THE DSEQ", start_at)
    snippet = body[start_at:finish]
    output = tmp_path / "shell-output"
    script = (
        """set -euo pipefail
python3() {
  if [ "$PUBLIC_MODE" = ack ]; then
    printf '%s\\n' '1 akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee'
    return 0
  fi
  if [ "$PUBLIC_MODE" = submitting ]; then return 2; fi
  echo 'disabled mode unexpectedly invoked receipt helper' >&2; return 9
}
DSEQ=prior
attempt=1
"""
        + snippet
        + '\nprintf "REACHED_AFTER_GUARD=%s/%s\\n" "$DSEQ" "$RECEIPT_OWNER"\n'
    )
    monkeypatch.setattr(subprocess, "Popen", REAL_POPEN)
    monkeypatch.setattr(subprocess, "run", REAL_RUN)
    result = REAL_RUN(
        ["/bin/bash", "-c", script],
        env={
            "PATH": "/usr/bin:/bin",
            "PUBLIC_MODE": mode,
            "RUNNER_CREATE_RECEIPT_CONTRACT": "" if mode == "disabled" else subject.CONTRACT,
            "GITHUB_OUTPUT": str(output),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    assert result.returncode == expected_exit
    if mode == "submitting":
        assert b"REACHED_AFTER_GUARD" not in result.stdout
        assert output.read_text().splitlines() == [
            "deployment_outcome=unknown",
            "create_ambiguous=true",
            "failure_reason=CREATE_OUTCOME_AMBIGUOUS",
        ]
    elif mode == "ack":
        assert b"REACHED_AFTER_GUARD=1/akash1" in result.stdout
    else:
        assert result.stdout == b"REACHED_AFTER_GUARD=prior/\n" and not output.exists()


DEFAULT = ROOT / "just_akash/runner_create_receipt.py"


@pytest.fixture
def custody_subject(monkeypatch):
    path = Path(os.environ.get("SDK_CUSTODY_SUBJECT_SOURCE", str(DEFAULT)))
    spec = importlib.util.spec_from_file_location("_custody_subject", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def forbidden(*args, **kwargs):
        raise AssertionError("NO_NETWORK_OR_CHILD")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    return module


def snapshot(custody_subject, tmp_path, monkeypatch):
    names = set(custody_subject.SOURCE_FILES) | {"just_akash/deploy.py", "just_akash/api.py"}
    originals = {}
    for name in names:
        raw = (ROOT / name).read_bytes()
        originals[name] = raw
        out = tmp_path / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(raw)
    sha = "a" * 40

    def git(argv, **kwargs):
        assert argv[:3] == ["/usr/bin/git", "-C", str(tmp_path)]
        if argv[3:] == ["rev-parse", "HEAD"]:
            raw = (sha + "\n").encode()
        else:
            assert len(argv) == 5 and argv[3] == "show" and argv[4].startswith(sha + ":")
            raw = originals[argv[4][41:]]
        return types.SimpleNamespace(returncode=0, stdout=raw)

    monkeypatch.setattr(custody_subject.subprocess, "run", git)
    return sha, originals


@pytest.mark.parametrize("name", ["just_akash/deploy.py", "just_akash/api.py"])
def test_actual_capture_rejects_uncommitted_create_runtime_before_execution(
    custody_subject, tmp_path, monkeypatch, name
):
    sha, originals = snapshot(custody_subject, tmp_path, monkeypatch)
    with (tmp_path / name).open("ab") as out:
        out.write(b'\nraise AssertionError("UNADMITTED_CREATE_RUNTIME_EXECUTED")\n')
    with pytest.raises(ValueError, match="OWNED_CREATE_RECEIPT_HOLD_NO_RETRY"):
        custody_subject.sdk_capture({"sdk_sha": sha}, tmp_path)


def test_actual_capture_admits_empty_original_package_and_complete_runtime(
    custody_subject, tmp_path, monkeypatch
):
    sha, originals = snapshot(custody_subject, tmp_path, monkeypatch)
    sources = custody_subject.sdk_capture({"sdk_sha": sha}, tmp_path)
    assert set(sources) == set(custody_subject.SOURCE_FILES) and len(sources) == 29
    assert sources["just_akash/__init__.py"] == b""
    assert sources == {n: originals[n] for n in custody_subject.SOURCE_FILES}


@pytest.mark.parametrize("name", ["just_akash/deploy.py", "just_akash/api.py"])
def test_actual_cli_uses_captured_runtime_after_path_swap(
    custody_subject, tmp_path, monkeypatch, name
):
    sha, originals = snapshot(custody_subject, tmp_path, monkeypatch)
    captured = custody_subject.sdk_capture({"sdk_sha": sha}, tmp_path)

    def capture(context, root):
        assert context == {"sdk_sha": sha} and root == tmp_path
        (tmp_path / name).write_bytes(
            originals[name] + b'\nraise AssertionError("SWAPPED_PATH_EXECUTED")\n'
        )
        return captured

    monkeypatch.setattr(custody_subject, "sdk_capture", capture)
    monkeypatch.setenv("AKASH_PROVIDERS", "")
    previous = dict(sys.modules)
    with pytest.raises(SystemExit) as result:
        custody_subject.execute_cli(
            sha,
            tmp_path,
            ["deploy", "--no-backup-fallback"],
            {n: hashlib.sha256(v).hexdigest() for n, v in captured.items()},
        )
    assert result.value.code == 2
    assert {
        k: v for k, v in sys.modules.items() if k == "just_akash" or k.startswith("just_akash.")
    } == {k: v for k, v in previous.items() if k == "just_akash" or k.startswith("just_akash.")}


@pytest.mark.parametrize("command", ["resolve-owner", "destroy", "verify-closed"])
def test_supported_callback_cli_uses_original_parser_without_pool_identity(
    custody_subject, tmp_path, monkeypatch, command
):
    sha, originals = snapshot(custody_subject, tmp_path, monkeypatch)
    sources = custody_subject.sdk_capture({"sdk_sha": sha}, tmp_path)
    with pytest.raises(SystemExit) as result:
        custody_subject.execute_cli(
            sha,
            tmp_path,
            [command, "--help"],
            {n: hashlib.sha256(v).hexdigest() for n, v in sources.items()},
        )
    assert result.value.code == 0


@pytest.mark.parametrize("change", ["wrong-sha", "missing-api", "unsupported-command"])
def test_callback_source_or_command_mismatch_refuses_before_cli_import(
    custody_subject, tmp_path, monkeypatch, change
):
    sha, originals = snapshot(custody_subject, tmp_path, monkeypatch)
    sources = custody_subject.sdk_capture({"sdk_sha": sha}, tmp_path)
    pins = {n: hashlib.sha256(v).hexdigest() for n, v in sources.items()}
    argv = ["resolve-owner", "--help"]
    if change == "wrong-sha":
        pins["just_akash/api.py"] = "0" * 64
    elif change == "missing-api":
        pins.pop("just_akash/api.py")
    else:
        argv = ["destroy-all"]

    def import_forbidden(*args, **kwargs):
        raise AssertionError("UNADMITTED_CLI_IMPORTED")

    monkeypatch.setattr(custody_subject.importlib, "import_module", import_forbidden)
    with pytest.raises(ValueError, match="OWNED_CREATE_RECEIPT_HOLD_NO_RETRY"):
        custody_subject.execute_cli(sha, tmp_path, argv, pins)


def test_actual_pool_execute_keeps_native_identity_and_no_replay_gate(
    custody_subject, tmp_path, monkeypatch
):
    sha, originals = snapshot(custody_subject, tmp_path, monkeypatch)
    sources = custody_subject.sdk_capture({"sdk_sha": sha}, tmp_path)
    calls = []

    def authorize(env, root, number, getter):
        calls.append(("authorize", number))
        assert env == {"closed": "producer"} and root == tmp_path and number == 2
        return {"sdk_source_pins": {}}, sources

    def execute(captured, root, argv):
        calls.append(("execute", argv))
        assert captured is sources and root == tmp_path
        return 7

    monkeypatch.setattr(custody_subject, "authorize_round", authorize)
    monkeypatch.setattr(custody_subject, "_execute_captured", execute)
    assert (
        custody_subject.execute_round({"closed": "producer"}, tmp_path, 2, ["deploy"], getter=None)
        == 7
    )
    assert calls == [("authorize", 2), ("execute", ["deploy"])]


def test_workflow_direct_file_gate_precedes_any_package_or_create_import(custody_subject):
    workflow = yaml.safe_load((ROOT / custody_subject.SDK_WORKFLOW).read_bytes())
    steps = workflow["jobs"]["pool"]["steps"]
    gate = next(s for s in steps if s.get("id") == "receipt_guard")
    assert gate["run"] == "python3 -I just_akash/runner_create_receipt.py begin"
    assert (
        gate["env"]["RUNNER_RECEIPT_READ_TOKEN"]
        == "${{ secrets.GH_RUNNER_PAT || env.GH_RUNNER_PAT }}"
    )
    provision = next(s["run"] for s in steps if s.get("id") == "provision")
    assert (
        provision.index("python3 -I just_akash/runner_create_receipt.py authorize")
        < provision.index(
            "DEPLOY_JA=(uv run --with . python -I just_akash/runner_create_receipt.py execute"
        )
        < provision.index('"${JA[@]}" deploy')
    )
    assert "python3 -m just_akash.runner_create_receipt" not in "\n".join(
        str(s.get("run", "")) for s in steps
    )
    assert any(s.get("id") == "credentials_sops" for s in steps)
    assert steps.index(next(s for s in steps if s.get("id") == "credentials_sops")) < steps.index(
        gate
    )


def test_full_private_profile_renderer_accepts_closed_role_and_preserves_original_prefix(
    custody_subject, tmp_path, monkeypatch
):
    # Execute the whole actual profile module after its public import dependency is installed.
    package = types.ModuleType("just_akash")
    package.__path__ = [str(ROOT / "just_akash")]
    monkeypatch.setitem(sys.modules, "just_akash", package)
    spec = importlib.util.spec_from_file_location(
        "just_akash.runner_image", ROOT / "just_akash/runner_image.py"
    )
    image = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(image)
    env = {
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_REPOSITORY": "Borduas-Holdings/Blazing-Back",
        "GITHUB_RUN_ID": "777",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_WORKFLOW_REF": (
            "Borduas-Holdings/Blazing-Back/.github/workflows/build-sentry-cli.yml@refs/heads/main"
        ),
        "RUNNER_CREATE_RECEIPT_CONTRACT": "sentry-private-production/v1",
        "RUNNER_REGISTRY_HOST": "https://index.docker.io/v1/",
        "RUNNER_REGISTRY_USERNAME": "jobordu",
        "PRIVATE_PROFILE_SOURCE": "Digital-Frontier-LDA/just-akash",
        "PRIVATE_PROFILE_ORG": "Borduas-Holdings",
        "PRIVATE_PROFILE_POOL_SIZE": "1",
        "PRIVATE_PROFILE_MIN_POOL_SIZE": "1",
        "PRIVATE_PROFILE_CPU": "2",
        "PRIVATE_PROFILE_MEMORY": "6Gi",
        "PRIVATE_PROFILE_STORAGE": "40Gi",
        "PRIVATE_PROFILE_EPHEMERAL": "true",
        "PRIVATE_PROFILE_SOPS": "true",
        "PRIVATE_PROFILE_AGE_PRESENT": "true",
        "PRIVATE_PROFILE_PASSWORD_PRESENT": "false",
        "PRIVATE_PROFILE_OWNED_PROVIDERS": json.dumps(sorted(image.NATIVE_READER_PROVIDERS)),
        "PRIVATE_PROFILE_LABEL": "sentry-production-777-1",
        "PRIVATE_PROFILE_PLACEMENT": "borduas-owned-sentry-a1-run-777-end",
        "PRIVATE_PROFILE_TAG_PREFIX": "ci-blazing-back-sentry-production",
    }
    for key in ["RUNNER_PUBLIC_PROFILE", "RUNNER_IMAGE", "RUNNER_REGISTRY_PASSWORD"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    role_calls = []
    monkeypatch.setattr(
        image,
        "verify_native_reader_role",
        lambda username, token: role_calls.append((username, token)),
    )
    label = env["PRIVATE_PROFILE_LABEL"]
    template = tmp_path / "runner.yaml"
    template.write_text(
        'version: "2.0"\nservices:\n  runner:\n'
        "    image: ghcr.io/digital-frontier-lda/df-akash-runner@"
        + image.NATIVE_READER_IMAGE.rsplit("@", 1)[1]
        + (
            "\n    env:\n      - RUNNER_TOKEN=@@RUNNER_TOKEN@@\n"
            "      - ORG_NAME=Borduas-Holdings\n      - RUNNER_SCOPE=org\n"
            "      - RUNNER_NAME_PREFIX=just-akash-"
        )
        + label
        + "\n      - LABELS=self-hosted,linux,akash,"
        + label
        + "\n      - EPHEMERAL=true\n      - RUNNER_WORKDIR=/_work\n      - RUN_AS_ROOT=true\n"
    )
    image.configure(
        template,
        image="",
        host=env["RUNNER_REGISTRY_HOST"],
        username="jobordu",
        password="PUBLIC_SYNTHETIC_READER",
        reader_from_sops=True,
        private_profile="bb-ce1",
    )
    rendered = yaml.safe_load(template.read_bytes())["services"]["runner"]
    assert rendered["image"] == image.PRIVATE_BB_CE1_IMAGE
    assert rendered["credentials"]["password"] == "PUBLIC_SYNTHETIC_READER"
    assert "RUNNER_NAME_PREFIX=just-akash-" + label in rendered["env"]
    assert role_calls == [("jobordu", "PUBLIC_SYNTHETIC_READER")]
    monkeypatch.setenv("PRIVATE_PROFILE_CPU", "4")
    with pytest.raises(ValueError):
        image.validate_private_profile("bb-ce1")


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("command", ["tag", "destroy", "verify-closed"])
def test_actual_postcreate_array_routing_preserves_original_commands(
    tmp_path, monkeypatch, enabled, command
):
    workflow = yaml.safe_load((ROOT / subject.SDK_WORKFLOW).read_bytes())
    body = next(s["run"] for s in workflow["jobs"]["pool"]["steps"] if s.get("id") == "provision")
    initial = re.findall(r"^ *JA=\(uv run --with \. just-akash\)$", body, re.MULTILINE)
    override = re.findall(
        (
            r"^ *DEPLOY_JA=\(uv run --with \. python -I "
            r'just_akash/runner_create_receipt.py execute --round "\$attempt" --\)$'
        ),
        body,
        re.MULTILINE,
    )
    assert len(initial) == len(override) == 1
    assert '"${JA[@]}" ' + command in body
    script = (
        'set -euo pipefail\nattempt=2\nuv() { printf "%s\\n" "$4" "$5"; }\n' + initial[0] + "\n"
    )
    if enabled:
        script += override[0] + "\n"
    script += '"${JA[@]}" ' + command + " --dseq 1\n"
    monkeypatch.setattr(subprocess, "run", REAL_RUN)
    monkeypatch.setattr(subprocess, "Popen", REAL_POPEN)
    result = REAL_RUN(
        ["/bin/bash", "-c", script],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["just-akash", command]
