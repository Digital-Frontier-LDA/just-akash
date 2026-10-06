"""Private Blazing pools retain their full original shape and reader boundary."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

import pytest
import yaml

from just_akash import runner_image as subject
from tests.test_runner_private_bb_profile import READ_FIXTURE
from tests.test_runner_public_bb_profile import STEPS


def context(monkeypatch, role="fast"):
    label, size, tag = {
        "fast": ("fast-pool-123-2", "4", "ci-blazing-fast"),
        "e2e": ("e2epool-123-2", "2", "ci-blazing-e2e"),
    }[role]
    values = {
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_REPOSITORY": "Borduas-Holdings/blazing",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "RUNNER_PUBLIC_PROFILE": "",
        "RUNNER_PRIVATE_PROFILE": "blazing-aaf",
        "RUNNER_IMAGE": "",
        "RUNNER_REGISTRY_HOST": "https://index.docker.io/v1/",
        "RUNNER_REGISTRY_USERNAME": "jobordu",
        "RUNNER_REGISTRY_PASSWORD": "",
        "RUNNER_NATIVE_PULL_READER": "false",
        "RUNNER_NATIVE_REPOSITORY_SCOPE": "false",
        "PRIVATE_PROFILE_SOURCE": "Digital-Frontier-LDA/just-akash",
        "PRIVATE_PROFILE_ORG": "Borduas-Holdings",
        "PRIVATE_PROFILE_LABEL": label,
        "PRIVATE_PROFILE_POOL_SIZE": size,
        "PRIVATE_PROFILE_MIN_POOL_SIZE": "",
        "PRIVATE_PROFILE_PLACEMENT": "borduas-runner-run-123-end",
        "PRIVATE_PROFILE_TAG_PREFIX": tag,
        "PRIVATE_PROFILE_EPHEMERAL": "false",
        "PRIVATE_PROFILE_SOPS": "true",
        "PRIVATE_PROFILE_AGE_PRESENT": "true",
        "PRIVATE_PROFILE_PASSWORD_PRESENT": str(False).lower(),
        "PRIVATE_PROFILE_OWNED_PROVIDERS": json.dumps(sorted(subject.NATIVE_READER_PROVIDERS)),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return label, values


def template(tmp_path, monkeypatch, role):
    label, values = context(monkeypatch, role)
    step = next(step for step in STEPS if step.get("id") == "render")
    path = tmp_path / "runner.yaml"
    env = {
        **os.environ,
        "ORG": "Borduas-Holdings",
        "RUNNER_LABEL": label,
        "POOL_SIZE": values["PRIVATE_PROFILE_POOL_SIZE"],
        "CPU": "4" if role == "fast" else "2",
        "MEMORY": "8Gi",
        "STORAGE": "20Gi",
        "EPHEMERAL": "false",
        "PLACEMENT_KEY": values["PRIVATE_PROFILE_PLACEMENT"],
        "GH_RUN_ID": "123",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
    }
    script = step["run"].replace("/tmp/runner-sdl.yaml", str(path))
    result = subprocess.run(
        ["bash", "-e", "-c", script], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    return path


@pytest.mark.parametrize("role", ["fast", "e2e"])
@pytest.mark.parametrize("minimum", ["", "full"])
def test_private_generated_pools_preserve_every_resource_env_and_original_aaf_digest(
    tmp_path, monkeypatch, role, minimum
):
    path = template(tmp_path, monkeypatch, role)
    before = yaml.safe_load(path.read_text())
    if minimum == "full":
        monkeypatch.setenv(
            "PRIVATE_PROFILE_MIN_POOL_SIZE", os.environ["PRIVATE_PROFILE_POOL_SIZE"]
        )
    calls = []
    monkeypatch.setattr(subject, "verify_native_reader_role", lambda *args: calls.append(args))
    subject.configure(
        path,
        image="",
        host="https://index.docker.io/v1/",
        username="jobordu",
        password=READ_FIXTURE,
        reader_from_sops=True,
        private_profile="blazing-aaf",
    )
    expected = copy.deepcopy(before)
    expected["services"]["runner"]["image"] = subject.PRIVATE_BLAZING_AAF_IMAGE
    expected["services"]["runner"]["credentials"] = {
        "host": "https://index.docker.io/v1/",
        "username": "jobordu",
        "password": READ_FIXTURE,
    }
    actual = yaml.safe_load(path.read_text())
    assert actual == expected
    assert calls == [("jobordu", READ_FIXTURE)]
    assert (
        actual["services"]["runner"]["image"].split("@", 1)[1]
        == before["services"]["runner"]["image"].split("@", 1)[1]
    )
    assert len(actual["services"]["runner"]["env"]) == 8
    assert READ_FIXTURE not in "\n".join(actual["services"]["runner"]["env"])
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["output", "runner.yaml"]


@pytest.mark.parametrize("role", ["fast", "e2e"])
@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_REPOSITORY", "Borduas-Holdings/Blazing-Back"),
        ("RUNNER_ENVIRONMENT", "self-hosted"),
        ("RUNNER_PUBLIC_PROFILE", "bb-ce1"),
        ("RUNNER_REGISTRY_HOST", "https://evil.invalid/"),
        ("RUNNER_REGISTRY_USERNAME", "foreign"),
        ("RUNNER_REGISTRY_PASSWORD", "direct-canary"),
        ("RUNNER_IMAGE", subject.PRIVATE_BLAZING_AAF_IMAGE),
        ("RUNNER_NATIVE_PULL_READER", "true"),
        ("RUNNER_NATIVE_REPOSITORY_SCOPE", "true"),
        ("PRIVATE_PROFILE_SOPS", "false"),
        ("PRIVATE_PROFILE_AGE_PRESENT", "false"),
        ("PRIVATE_PROFILE_PASSWORD_PRESENT", "true"),
        ("PRIVATE_PROFILE_SOURCE", "foreign/sdk"),
        ("PRIVATE_PROFILE_ORG", "foreign"),
        ("PRIVATE_PROFILE_LABEL", "fast-pool-123-3"),
        ("PRIVATE_PROFILE_PLACEMENT", "borduas-runner-run-124-end"),
        ("PRIVATE_PROFILE_TAG_PREFIX", "ci-blazing-back-fast-pool"),
        ("PRIVATE_PROFILE_POOL_SIZE", "1"),
        ("PRIVATE_PROFILE_MIN_POOL_SIZE", "1"),
        ("PRIVATE_PROFILE_EPHEMERAL", "true"),
        ("PRIVATE_PROFILE_OWNED_PROVIDERS", "[]"),
        ("PRIVATE_PROFILE_OWNED_PROVIDERS", "{}"),
        ("PRIVATE_PROFILE_OWNED_PROVIDERS", '["foreign"]'),
        ("PRIVATE_PROFILE_OWNED_PROVIDERS", " " * 513),
        ("GITHUB_RUN_ID", "0"),
        ("GITHUB_RUN_ID", str(2**64)),
        ("GITHUB_RUN_ATTEMPT", "2\n"),
    ],
)
def test_foreign_or_partial_pool_refuses_before_decryption_or_template(
    tmp_path, monkeypatch, role, key, value
):
    context(monkeypatch, role)
    monkeypatch.setenv(key, value)
    absent = tmp_path / "absent"
    monkeypatch.setattr(
        subject, "read_pull_credentials", lambda *a, **k: pytest.fail("decrypt refused")
    )
    monkeypatch.setattr(
        subject, "verify_native_reader_role", lambda *a, **k: pytest.fail("role refused")
    )
    monkeypatch.setattr(
        sys, "argv", ["runner_image", "--sdl", str(absent), "--sops-env-file", str(absent)]
    )
    assert subject.main() == 1
    assert not absent.exists()


@pytest.mark.parametrize("role", ["fast", "e2e"])
def test_cli_check_uses_existing_hosted_guard_without_secret_or_lease(tmp_path, monkeypatch, role):
    context(monkeypatch, role)
    guard = next(step for step in STEPS if step.get("id") == "private_profile")
    checkout = next(
        step
        for step in STEPS
        if step.get("name") == "Check out the caller encrypted reader bundle"
    )
    assert STEPS.index(guard) < STEPS.index(checkout)
    assert not {"SOPS_AGE_KEY", "RUNNER_REGISTRY_PASSWORD"} & set(guard["env"])
    output = tmp_path / "output"
    env = {**os.environ, "GITHUB_OUTPUT": str(output)}
    result = subprocess.run(
        ["bash", "-e", "-c", guard["run"]], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0 and not output.exists()
    env["PRIVATE_PROFILE_AGE_PRESENT"] = "false"
    result = subprocess.run(
        ["bash", "-e", "-c", guard["run"]], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert output.read_text().splitlines() == [
        "deployment_outcome=no-deployment",
        "failure_reason=PRIVATE_PROFILE_" + "UNQUALIFIED",
    ]


@pytest.mark.parametrize("role", ["fast", "e2e"])
def test_writer_role_failure_preserves_original_template(tmp_path, monkeypatch, role):
    path = template(tmp_path, monkeypatch, role)
    before = path.read_bytes()

    def refuse(*_):
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
            private_profile="blazing-aaf",
        )
    assert path.read_bytes() == before
