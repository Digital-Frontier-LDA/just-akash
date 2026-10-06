"""The original complete B topology uses only hosted-reader SDL transport."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

import pytest
import yaml

from just_akash import runner_image as subject
from tests.test_runner_private_bb_profile import READ_FIXTURE, private_context
from tests.test_runner_public_bb_profile import STEPS


def b_context(monkeypatch, count="1"):
    private_context(monkeypatch)
    values = {
        "PRIVATE_PROFILE_POOL_SIZE": count,
        "PRIVATE_PROFILE_MIN_POOL_SIZE": count,
        "PRIVATE_PROFILE_LABEL": "b-tier-123-2",
        "PRIVATE_PROFILE_PLACEMENT": "dfci-infra-b-tier-run-123-end",
        "PRIVATE_PROFILE_TAG_PREFIX": "ci-blazing-back-b-tier",
        "PRIVATE_PROFILE_EPHEMERAL": "true",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def render(tmp_path, count="1", changes=None):
    path = tmp_path / "runner.yaml"
    step = next(step for step in STEPS if step.get("id") == "render")
    env = {
        **os.environ,
        "ORG": "Borduas-Holdings",
        "RUNNER_LABEL": "b-tier-123-2",
        "POOL_SIZE": count,
        "CPU": "2",
        "MEMORY": "8Gi",
        "STORAGE": "20Gi",
        "EPHEMERAL": "true",
        "PLACEMENT_KEY": "dfci-infra-b-tier-run-123-end",
        "GH_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "RUNNER_PUBLIC_PROFILE": "",
        "RUNNER_PRIVATE_PROFILE": "bb-ce1",
    }
    env.update(changes or {})
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"].replace("/tmp/runner-sdl.yaml", str(path))],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return path, result


@pytest.mark.parametrize("count", ["1", "2", "3"])
def test_complete_original_topology_preserves_generated_resources_and_environment(
    tmp_path, monkeypatch, count
):
    b_context(monkeypatch, count)
    path, result = render(tmp_path, count)
    assert result.returncode == 0
    before = yaml.safe_load(path.read_text())
    assert before["deployment"]["runner"] == {
        "dfci-infra-b-tier-run-123-end": {"profile": "runner", "count": int(count)}
    }
    assert before["profiles"]["compute"]["runner"]["resources"] == {
        "cpu": {"units": 2},
        "memory": {"size": "8Gi"},
        "storage": {"size": "20Gi"},
    }
    calls = []
    monkeypatch.setattr(
        subject, "verify_native_reader_role", lambda user, token: calls.append((user, token))
    )
    subject.configure(
        path,
        image="",
        host="https://index.docker.io/v1/",
        username="jobordu",
        password=READ_FIXTURE,
        reader_from_sops=True,
        private_profile="bb-ce1",
    )
    expected = copy.deepcopy(before)
    expected["services"]["runner"]["image"] = subject.PRIVATE_BB_CE1_IMAGE
    expected["services"]["runner"]["credentials"] = {
        "host": "https://index.docker.io/v1/",
        "username": "jobordu",
        "password": READ_FIXTURE,
    }
    actual = yaml.safe_load(path.read_text())
    assert actual == expected
    assert calls == [("jobordu", READ_FIXTURE)]
    assert subject.validate_private_profile("bb-ce1") == "b-tier-123-2"
    runner_env = actual["services"]["runner"]["env"]
    assert len(runner_env) == 8
    fields = dict(entry.split("=", 1) for entry in runner_env)
    assert len(fields) == 8
    assert fields["RUNNER_NAME_PREFIX"] == "just-akash-b-tier-123-2"
    assert fields["EPHEMERAL"] == "true"
    assert READ_FIXTURE not in "\n".join(runner_env)
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(item.name for item in tmp_path.iterdir()) == ["output", "runner.yaml"]


@pytest.mark.parametrize(
    "count,minimum",
    [
        ("1", ""),
        ("2", ""),
        ("3", ""),
        ("2", "1"),
        ("3", "2"),
        ("1", "2"),
        ("1", "3"),
        ("2", "3"),
        ("3", "1"),
        ("0", "0"),
        ("4", "4"),
        ("01", "01"),
        ("２", "２"),
        ("", ""),
    ],
)
def test_partial_or_unbounded_topology_refuses_before_reader_access(
    tmp_path, monkeypatch, count, minimum
):
    b_context(monkeypatch, count)
    monkeypatch.setenv("PRIVATE_PROFILE_MIN_POOL_SIZE", minimum)
    refuse_before_read(tmp_path, monkeypatch)


def refuse_before_read(tmp_path, monkeypatch):
    path = tmp_path / "absent.yaml"
    monkeypatch.setattr(
        subject, "read_pull_credentials", lambda *a, **k: pytest.fail("must not decrypt")
    )
    monkeypatch.setattr(
        subject, "verify_native_reader_role", lambda *a, **k: pytest.fail("must not authenticate")
    )
    monkeypatch.setattr(
        sys, "argv", ["runner_image", "--sdl", str(path), "--sops-env-file", str(path)]
    )
    assert subject.main() == 1
    assert not path.exists()


@pytest.mark.parametrize("count", ["1", "2", "3"])
def test_missing_minimum_never_selects_a_partial_b_pool(tmp_path, monkeypatch, count):
    b_context(monkeypatch, count)
    monkeypatch.delenv("PRIVATE_PROFILE_MIN_POOL_SIZE")
    refuse_before_read(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_REPOSITORY", "Borduas-Holdings/blazing"),
        ("RUNNER_ENVIRONMENT", "self-hosted"),
        ("RUNNER_PUBLIC_PROFILE", "bb-ce1"),
        ("RUNNER_REGISTRY_HOST", "https://evil.invalid/"),
        ("RUNNER_REGISTRY_USERNAME", "foreign"),
        ("RUNNER_REGISTRY_PASSWORD", "direct-canary"),
        ("RUNNER_IMAGE", subject.PRIVATE_BB_CE1_IMAGE),
        ("RUNNER_NATIVE_PULL_READER", "true"),
        ("RUNNER_NATIVE_REPOSITORY_SCOPE", "true"),
        ("PRIVATE_PROFILE_SOPS", "false"),
        ("PRIVATE_PROFILE_AGE_PRESENT", "false"),
        ("PRIVATE_PROFILE_PASSWORD_PRESENT", "true"),
        ("PRIVATE_PROFILE_SOURCE", "foreign/sdk"),
        ("PRIVATE_PROFILE_ORG", "foreign"),
        ("PRIVATE_PROFILE_LABEL", "b-tier-124-2"),
        ("PRIVATE_PROFILE_LABEL", "b-tier-123-3"),
        ("PRIVATE_PROFILE_PLACEMENT", "dfci-infra-b-tier-run-124-end"),
        ("PRIVATE_PROFILE_PLACEMENT", "dfci-infra-runner-run-123-end"),
        ("PRIVATE_PROFILE_TAG_PREFIX", "ci-blazing-back-fast-pool"),
        ("PRIVATE_PROFILE_EPHEMERAL", "false"),
        ("PRIVATE_PROFILE_OWNED_PROVIDERS", "[]"),
        ("PRIVATE_PROFILE_OWNED_PROVIDERS", json.dumps(["foreign"] * 3)),
        (
            "PRIVATE_PROFILE_OWNED_PROVIDERS",
            json.dumps([sorted(subject.NATIVE_READER_PROVIDERS)[0]] * 3),
        ),
        ("GITHUB_RUN_ID", "0"),
        ("GITHUB_RUN_ID", str(2**64)),
        ("GITHUB_RUN_ATTEMPT", "2\n"),
    ],
)
def test_b_identity_and_hosted_reader_authority_are_required_before_read(
    tmp_path, monkeypatch, key, value
):
    b_context(monkeypatch, "3")
    monkeypatch.setenv(key, value)
    refuse_before_read(tmp_path, monkeypatch)


@pytest.mark.parametrize("count", ["1", "2", "3"])
def test_hosted_preparation_checks_full_count_without_decryption(tmp_path, monkeypatch, count):
    b_context(monkeypatch, count)
    path = tmp_path / "absent"
    monkeypatch.setattr(
        subject, "read_pull_credentials", lambda *a, **k: pytest.fail("must not decrypt")
    )
    monkeypatch.setattr(
        sys, "argv", ["runner_image", "--sdl", str(path), "--check-private-profile"]
    )
    assert subject.main() == 0
    assert not path.exists()


@pytest.mark.parametrize("count", ["1", "2", "3"])
@pytest.mark.parametrize("admitted", [True, False])
def test_actual_preparation_step_holds_partial_counts_before_sops(
    tmp_path, monkeypatch, count, admitted
):
    b_context(monkeypatch, count)
    if not admitted:
        monkeypatch.setenv("PRIVATE_PROFILE_MIN_POOL_SIZE", "")
    task_bin = tmp_path / "bin"
    task_bin.mkdir()
    shim = task_bin / "python3"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    shim.chmod(0o700)
    output = tmp_path / "output"
    guard = next(step for step in STEPS if step.get("id") == "private_profile")
    result = subprocess.run(
        ["bash", "-e", "-c", guard["run"]],
        env={
            **os.environ,
            "PATH": str(task_bin) + os.pathsep + os.environ["PATH"],
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == (0 if admitted else 1)
    if admitted:
        assert not output.exists()
    else:
        assert output.read_text().splitlines() == [
            "deployment_outcome=no-deployment",
            "failure_reason=PRIVATE_PROFILE_" + "UNQUALIFIED",
        ]


@pytest.mark.parametrize(
    "changes",
    [
        {"RUNNER_PRIVATE_PROFILE": ""},
        {"RUNNER_PUBLIC_PROFILE": "bb-ce1", "RUNNER_PRIVATE_PROFILE": ""},
        {"RUNNER_PUBLIC_PROFILE": "bb-ce1"},
        {"GITHUB_REPOSITORY": "Borduas-Holdings/blazing"},
        {"ORG": "foreign"},
        {"PLACEMENT_KEY": "dfci-infra-b-tier-run-124-end"},
        {"RUNNER_LABEL": "b-tier-123-3"},
        {"GITHUB_RUN_ATTEMPT": "3"},
        {"POOL_SIZE": "4"},
        {"POOL_SIZE": ""},
        {"EPHEMERAL": "false"},
    ],
)
def test_actual_renderer_rejects_public_or_foreign_reserved_b_identity(
    tmp_path, monkeypatch, changes
):
    b_context(monkeypatch)
    path, result = render(tmp_path, changes=changes)
    assert result.returncode == 2
    assert not path.exists()


@pytest.mark.parametrize("count", ["1", "2", "3"])
def test_fresh_reader_role_rejection_preserves_full_b_template(tmp_path, monkeypatch, count):
    b_context(monkeypatch, count)
    path, result = render(tmp_path, count)
    assert result.returncode == 0
    before = path.read_bytes()

    def refuse(user, token):
        raise subject.NativeReaderRoleError("fixed refusal")

    monkeypatch.setattr(subject, "verify_native_reader_role", refuse)
    with pytest.raises(subject.NativeReaderRoleError):
        subject.configure(
            path,
            image="",
            host="https://index.docker.io/v1/",
            username="jobordu",
            password=READ_FIXTURE,
            reader_from_sops=True,
            private_profile="bb-ce1",
        )
    assert path.read_bytes() == before
