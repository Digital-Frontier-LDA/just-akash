"""Causal hosted-source controls; original issuer/group policy runs against fixtures."""

import base64
import io
import json
import os
import shlex
import socket
import subprocess
import urllib.error
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from just_akash import runner_image as sdk
from just_akash import runner_sentry_admission as subject
from just_akash.workload_identity import Identity, format_identity

ROOT = Path(__file__).resolve().parents[1]
READER = "fixture-reader-token"


class Response:
    status = 200

    def __init__(self, document):
        self.raw = json.dumps(document).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, amount):
        return self.raw[:amount]


@pytest.fixture
def profile(tmp_path, monkeypatch):
    def no_wire(*_args, **_kwargs):
        raise AssertionError("Real network is forbidden in source controls")

    monkeypatch.setattr(socket.socket, "connect", no_wire)
    monkeypatch.setenv("RUNNER_SENTRY_MINT_ADMISSION", "true")
    values = {
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_REPOSITORY": subject.REPOSITORY,
        "SENTRY_ADMISSION_SOURCE": "Digital-Frontier-LDA/just-akash",
        "RUNNER_NATIVE_PULL_READER": "false",
        "RUNNER_NATIVE_REPOSITORY_SCOPE": "false",
        "SENTRY_ADMISSION_SOPS": "true",
        "SENTRY_ADMISSION_PRIVATE_PROFILE": "",
        "SENTRY_ADMISSION_PUBLIC_PROFILE": "",
        "ORG": "Borduas-Holdings",
        "POOL_SIZE": "1",
        "MIN_POOL_SIZE": "1",
        "TAG_PREFIX": "ci-blazing-back-sentry",
        "PROVIDER_SELECT": "cheapest",
        "RUN_ID": "123456",
        "GITHUB_RUN_ATTEMPT": "2",
        "RUNNER_LABEL": "sentry-123456-2",
        "GH_TOKEN": "fixture-hosted-github-token",
        "SENTRY_ADMISSION_OWNED_PROVIDERS": json.dumps(sorted(sdk.NATIVE_READER_PROVIDERS)),
        "SENTRY_ADMISSION_PROVIDERS": json.dumps(
            [
                {"address": address, "preferred": True}
                for address in sorted(sdk.NATIVE_READER_PROVIDERS)
            ]
        ),
    }
    placement = format_identity(
        Identity("borduas-sentry-", subject.REPOSITORY, "ci-runner", 1, 123456, 2),
        {"borduas-sentry-": subject.REPOSITORY},
    )
    values["DEPLOYMENT_GROUP"] = placement
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    document = {
        "version": "2.0",
        "services": {
            "runner": {
                "image": "ghcr.io/digital-frontier-lda/df-akash-runner@"
                + sdk.NATIVE_READER_IMAGE.rsplit("@", 1)[1],
                "env": [
                    "RUNNER_TOKEN=@@RUNNER_TOKEN@@",
                    "ORG_NAME=Borduas-Holdings",
                    "RUNNER_SCOPE=org",
                    "RUNNER_NAME_PREFIX=just-akash-sentry-123456-2",
                    "LABELS=self-hosted,linux,akash,sentry-123456-2",
                    "EPHEMERAL=true",
                    "RUNNER_WORKDIR=/_work",
                    "RUN_AS_ROOT=true",
                ],
                "expose": [{"port": 80, "as": 80, "to": [{"global": True}]}],
            }
        },
        "profiles": {
            "compute": {
                "runner": {
                    "resources": {
                        "cpu": {"units": 2},
                        "memory": {"size": "6Gi"},
                        "storage": {"size": "40Gi"},
                    }
                }
            },
            "placement": {placement: {"pricing": {"runner": {"denom": "uact", "amount": 100000}}}},
        },
        "deployment": {"runner": {placement: {"profile": "runner", "count": 1}}},
    }
    path = tmp_path / "rendered.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    # The unchanged original configure code performs credential serialization and
    # mode-0600 atomic replacement. No test reads/decrypts any actual secret.
    sdk.configure(
        path,
        image=sdk.NATIVE_READER_IMAGE,
        host="https://index.docker.io/v1/",
        username="jobordu",
        password=READER,
    )
    return path


@pytest.fixture
def transport(monkeypatch, profile):
    group = {
        "id": 1,
        "default": True,
        "visibility": "selected",
        "allows_public_repositories": False,
        "restricted_to_workflows": False,
        "selected_workflows": [],
    }
    private = {
        "id": subject.RID,
        "full_name": subject.REPOSITORY,
        "private": True,
        "visibility": "private",
    }
    repositories = [
        {"id": rid, "full_name": name, "private": True}
        for rid, name in sdk.NATIVE_READER_REPOSITORIES.items()
    ]
    state = {
        "events": [],
        "group": group,
        "private": private,
        "repositories": repositories,
        "scope": "repo:read",
        "change": None,
    }

    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    class Opener:
        def open(self, request, *, timeout):
            assert 0 < timeout <= 20
            state["events"].append((request.full_url, request.method))
            if state["change"]:
                state["change"](request, state)
            parsed = urlsplit(request.full_url)
            if request.full_url == "https://hub.docker.com/v2/auth/token":
                assert request.method == "POST" and json.loads(request.data) == {
                    "identifier": "jobordu",
                    "secret": READER,
                }
                return Response(
                    {
                        "access_token": part({"alg": "HS256"})
                        + "."
                        + part({"scope": state["scope"]})
                        + ".Zml4dHVyZQ"
                    }
                )
            assert parsed.netloc == "api.github.com" and request.method == "GET"
            if parsed.path == "/repos/" + subject.REPOSITORY:
                return Response(state["private"])
            assert parsed.path.startswith(sdk._GROUP_ROOT)
            if parsed.path.endswith("/repositories"):
                return Response(
                    {
                        "total_count": len(state["repositories"]),
                        "repositories": []
                        if parsed.query.endswith("page=2")
                        else state["repositories"],
                    }
                )
            if parsed.path == sdk._GROUP_ROOT:
                return Response(
                    {
                        "total_count": 1,
                        "runner_groups": []
                        if parsed.query.endswith("page=2")
                        else [state["group"]],
                    }
                )
            assert parsed.path == sdk._GROUP_ROOT + "/1"
            return Response(state["group"])

    monkeypatch.setattr(subject.urllib.request, "build_opener", lambda *_: Opener())
    return state


def test_original_policies_run_fresh_before_each_admission(profile, transport):
    subject.verify_sentry_mint_admission(profile)
    first = list(transport["events"])
    subject.verify_sentry_mint_admission(profile)
    assert transport["events"] == first + first
    assert first.count(("https://hub.docker.com/v2/auth/token", "POST")) == 1
    assert first.count(("https://api.github.com/repos/" + subject.REPOSITORY, "GET")) == 2
    assert all(
        method == "GET" or url == "https://hub.docker.com/v2/auth/token" for url, method in first
    )


def test_actual_workflow_render_and_original_configure_satisfy_strict_contract(profile, transport):
    workflow = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())
    render = next(row for row in workflow["jobs"]["pool"]["steps"] if row.get("id") == "render")[
        "run"
    ]
    start = render.index("cat > /tmp/runner-sdl.yaml <<SDL\n")
    end = render.index("\nSDL\n", start) + len("\nSDL\n")
    script = render[start:end].replace("/tmp/runner-sdl.yaml", shlex.quote(str(profile)), 1)
    env = {
        "PATH": "/usr/bin:/bin",
        "ORG": "Borduas-Holdings",
        "RUNNER_LABEL": "sentry-123456-2",
        "EPHEMERAL": "true",
        "CPU": "2",
        "MEMORY": "6Gi",
        "STORAGE": "40Gi",
        "POOL_SIZE": "1",
        "PLACEMENT_KEY": os.environ["DEPLOYMENT_GROUP"],
    }
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0 and result.stdout == result.stderr == b""
    sdk.configure(
        profile,
        image=sdk.NATIVE_READER_IMAGE,
        host="https://index.docker.io/v1/",
        username="jobordu",
        password=READER,
    )
    subject.verify_sentry_mint_admission(profile)


def test_fresh_next_issuer_observation_refuses_role_change(profile, transport):
    subject.verify_sentry_mint_admission(profile)
    first_count = len(transport["events"])
    transport["scope"] = "repo:write"
    with pytest.raises(sdk.NativeReaderRoleError):
        subject.verify_sentry_mint_admission(profile)
    assert len(transport["events"]) == first_count + 1


def test_bb_membership_changed_between_extra_snapshots_refuses(profile, transport):
    def change(request, state):
        path = sdk._GROUP_ROOT + "/1/repositories?per_page=100&page=1"
        if (
            request.full_url == "https://api.github.com" + path
            and state["events"].count((request.full_url, "GET")) == 4
        ):
            state["repositories"] = state["repositories"][:1]

    transport["change"] = change
    with pytest.raises(subject.SentryAdmissionError):
        subject.verify_sentry_mint_admission(profile)


def test_original_blazing_only_positive_then_sentry_refuses_missing_bb(profile, transport):
    transport["repositories"] = transport["repositories"][:1]
    sdk.verify_native_reader_group()  # Causal original-policy positive.
    with pytest.raises(subject.SentryAdmissionError):
        subject.verify_sentry_mint_admission(profile)


@pytest.mark.parametrize("scope", ["repo:write", "repo:admin", "repo:read repo:write", "unknown"])
def test_fresh_original_role_refuses_nonreader(profile, transport, scope):
    transport["scope"] = scope
    with pytest.raises(sdk.NativeReaderRoleError):
        subject.verify_sentry_mint_admission(profile)
    assert len(transport["events"]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", True),
        ("id", 1074974924),
        ("full_name", "foreign/repository"),
        ("private", False),
        ("visibility", "public"),
    ],
)
def test_private_identity_refusals(profile, transport, field, value):
    transport["private"][field] = value
    with pytest.raises(subject.SentryAdmissionError):
        subject.verify_sentry_mint_admission(profile)


def test_fresh_second_private_observation_refuses_change(profile, transport):
    def change(request, state):
        if (
            request.full_url == "https://api.github.com/repos/" + subject.REPOSITORY
            and state["events"].count((request.full_url, "GET")) == 2
        ):
            state["private"]["private"] = False

    transport["change"] = change
    with pytest.raises(subject.SentryAdmissionError):
        subject.verify_sentry_mint_admission(profile)


def test_original_group_oracle_refuses_absent_blazing(profile, transport):
    transport["repositories"] = transport["repositories"][1:]
    with pytest.raises(sdk.NativeReaderGroupError):
        subject.verify_sentry_mint_admission(profile)


@pytest.mark.parametrize(
    "field,value",
    [
        ("visibility", "all"),
        ("allows_public_repositories", True),
        ("restricted_to_workflows", True),
        ("selected_workflows", ["foreign.yml"]),
    ],
)
def test_original_group_policy_refusals(profile, transport, field, value):
    transport["group"][field] = value
    with pytest.raises(sdk.NativeReaderGroupError):
        subject.verify_sentry_mint_admission(profile)


@pytest.mark.parametrize(
    "field,value",
    [
        ("RUNNER_ENVIRONMENT", "self-hosted"),
        ("GITHUB_REPOSITORY", "Borduas-Holdings/blazing"),
        ("RUN_ID", "0123456"),
        ("GITHUB_RUN_ATTEMPT", "3"),
        ("POOL_SIZE", "2"),
        ("MIN_POOL_SIZE", ""),
        ("RUNNER_NATIVE_PULL_READER", "true"),
        ("SENTRY_ADMISSION_SOPS", "false"),
        ("SENTRY_ADMISSION_PRIVATE_PROFILE", "bb-ce1"),
        ("SENTRY_ADMISSION_OWNED_PROVIDERS", '["foreign"]'),
    ],
)
def test_scope_refusal_before_any_transport(profile, transport, monkeypatch, field, value):
    monkeypatch.setenv(field, value)
    with pytest.raises(ValueError):
        subject.verify_sentry_mint_admission(profile)
    assert transport["events"] == []


@pytest.mark.parametrize(
    "mutation", ["image", "cpu", "memory", "storage", "count_bool", "env", "credentials"]
)
def test_rendered_contract_refusal_before_transport(profile, transport, mutation):
    doc = yaml.safe_load(profile.read_text())
    if mutation == "image":
        doc["services"]["runner"]["image"] += "foreign"
    elif mutation in ("cpu", "memory", "storage"):
        doc["profiles"]["compute"]["runner"]["resources"][mutation] = (
            {"units": 4} if mutation == "cpu" else {"size": "96Gi"}
        )
    elif mutation == "count_bool":
        next(iter(doc["deployment"]["runner"].values()))["count"] = True
    elif mutation == "env":
        doc["services"]["runner"]["env"].append("DOCKERHUB_PULL_TOKEN=foreign")
    else:
        doc["services"]["runner"]["credentials"]["username"] = "foreign"
    profile.write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(ValueError):
        subject.verify_sentry_mint_admission(profile)
    assert transport["events"] == []


@pytest.mark.parametrize("kind", ["public_mode", "symlink", "duplicate", "alias", "oversize"])
def test_file_boundary_refuses(profile, transport, tmp_path, kind):
    if kind == "public_mode":
        profile.chmod(0o644)
    elif kind == "symlink":
        alias = tmp_path / "alias.yaml"
        alias.symlink_to(profile)
        profile = alias
    elif kind == "duplicate":
        profile.write_text(profile.read_text() + '\nversion: "2.0"\n')
    elif kind == "alias":
        profile.write_text("version: &alias '2.0'\nservices: *alias\n")
    else:
        profile.write_bytes(b"x" * (subject.MAX_SDL + 1))
    with pytest.raises((ValueError, OSError)):
        subject.verify_sentry_mint_admission(profile)
    assert transport["events"] == []


def test_failed_main_emits_only_fixed_safe_stage(profile, transport, monkeypatch, capsys):
    def refuse(_path):
        raise ValueError("must-never-escape-fixture-secret-body")

    monkeypatch.setattr(subject, "verify_sentry_mint_admission", refuse)
    assert subject.main() == 1
    assert capsys.readouterr().out == "Sentry before-mint admission was not verified\n"


def test_private_http_error_body_is_closed_without_read_or_chained_exception(monkeypatch):
    class UnreadBody(io.BytesIO):
        def read(self, *_args):
            raise AssertionError("Error bodies must never be read")

    body = UnreadBody(b"must-never-escape-fixture-error-body")
    monkeypatch.setenv("GH_TOKEN", "fixture-github-token")

    class Opener:
        def open(self, request, *, timeout):
            assert request.method == "GET" and timeout == 20
            raise urllib.error.HTTPError(request.full_url, 403, "fixture-error", {}, body)

    monkeypatch.setattr(subject.urllib.request, "build_opener", lambda *_: Opener())
    with pytest.raises(subject.SentryAdmissionError) as caught:
        subject._private_repository()
    assert body.closed and caught.value.__cause__ is caught.value.__context__ is None


def test_workflow_opt_in_precedes_original_mint_inside_retry_and_refusal_conserves_outcome(
    tmp_path,
):
    workflow = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())
    inputs = workflow[True]["workflow_call"]["inputs"]
    assert inputs["runner-sentry-mint-admission"]["default"] is False
    step = next(row for row in workflow["jobs"]["pool"]["steps"] if row.get("id") == "provision")
    assert "SOPS_AGE_KEY" not in step["env"]
    source = step["run"]
    start = source.index('if [ "${RUNNER_SENTRY_MINT_ADMISSION:-false}" = true ]')
    end = source.index("# Recheck server admission", start)
    block = source[start:end]
    assert (
        source.index("for attempt")
        < start
        < source.index(
            'RESP=$(gh api --method POST "${RUNNER_COLLECTION}/registration-token" -i 2>&1)'
        )
    )
    assert start < source.index('if [ "${RUNNER_NATIVE_PULL_READER:-false}" = true ] && ! python3')
    fake = tmp_path / "python3"
    fake.write_text(
        '#!/bin/bash\n[[ "$*" = "-m just_akash.runner_sentry_admission" ]] || exit 9\n'
        'printf x >> "$TEST_CALLS"\n[[ $(wc -c < "$TEST_CALLS") -lt 2 ]]\n'
    )
    fake.chmod(0o700)
    for observed, unknown in (("", "0"), ("123", "0"), ("", "1")):
        calls, minted, output = [
            tmp_path / (stem + (observed or "none") + unknown)
            for stem in ("calls", "minted", "output")
        ]
        script = (
            "set -u\nfor attempt in 1 2 3; do\n" + block + '\nprintf x >> "$TEST_MINTED"\ndone\n'
        )
        env = {
            "PATH": str(tmp_path) + ":/usr/bin:/bin",
            "RUNNER_SENTRY_MINT_ADMISSION": "true",
            "CREATED_DSEQ": observed,
            "UNCLASSIFIED_ATTEMPT": unknown,
            "GITHUB_OUTPUT": str(output),
            "TEST_CALLS": str(calls),
            "TEST_MINTED": str(minted),
        }
        result = subprocess.run(
            ["/bin/bash", "-c", script],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
        )
        assert (
            result.returncode == 1 and calls.read_bytes() == b"xx" and minted.read_bytes() == b"x"
        )
        assert ("deployment_outcome=no-deployment" in output.read_text()) is (
            observed == "" and unknown == "0"
        )
        assert "failure_reason=SENTRY_MINT_ADMISSION_UNQUALIFIED" in output.read_text()
    calls, minted = tmp_path / "dormant-calls", tmp_path / "dormant-minted"
    env.update(
        RUNNER_SENTRY_MINT_ADMISSION="false", TEST_CALLS=str(calls), TEST_MINTED=str(minted)
    )
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0 and not calls.exists() and minted.read_bytes() == b"xxx"
