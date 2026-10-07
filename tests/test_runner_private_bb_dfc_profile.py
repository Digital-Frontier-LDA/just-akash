"""The complete DFC pair has one fixed hosted-reader runner contract."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

import pytest
import yaml

from just_akash import runner_image as subject
from tests.test_runner_private_bb_b_tier_profile import render
from tests.test_runner_private_bb_profile import READ_FIXTURE, private_context
from tests.test_runner_public_bb_profile import STEPS, context


def dfc_context(monkeypatch):
    private_context(monkeypatch)
    for key, value in {
        "PRIVATE_PROFILE_LABEL": "dfc-images-123-2",
        "PRIVATE_PROFILE_PLACEMENT": "borduas-dfc-images-run-123-end",
        "PRIVATE_PROFILE_TAG_PREFIX": "ci-blazing-back-dfc-images",
        "PRIVATE_PROFILE_POOL_SIZE": "1",
        "PRIVATE_PROFILE_MIN_POOL_SIZE": "1",
        "PRIVATE_PROFILE_EPHEMERAL": "true",
        "PRIVATE_PROFILE_CPU": "4",
        "PRIVATE_PROFILE_MEMORY": "16Gi",
        "PRIVATE_PROFILE_STORAGE": "96Gi",
    }.items():
        monkeypatch.setenv(key, value)


def dfc_render(tmp_path, changes=None):
    values = {
        "RUNNER_LABEL": "dfc-images-123-2",
        "PLACEMENT_KEY": "borduas-dfc-images-run-123-end",
        "CPU": "4",
        "MEMORY": "16Gi",
        "STORAGE": "96Gi",
    }
    values.update(changes or {})
    return render(tmp_path, changes=values)


def configure(path):
    subject.configure(
        path,
        image="",
        host="https://index.docker.io/v1/",
        username="jobordu",
        password=READ_FIXTURE,
        reader_from_sops=True,
        private_profile="bb-ce1",
    )


def test_actual_single_runner_sdl_preserves_all_fields_except_private_reader(
    tmp_path, monkeypatch
):
    dfc_context(monkeypatch)
    path, result = dfc_render(tmp_path)
    assert result.returncode == 0
    before = yaml.safe_load(path.read_text())
    assert before["profiles"]["compute"]["runner"]["resources"] == {
        "cpu": {"units": 4},
        "memory": {"size": "16Gi"},
        "storage": {"size": "96Gi"},
    }
    assert before["deployment"] == {
        "runner": {"borduas-dfc-images-run-123-end": {"profile": "runner", "count": 1}}
    }
    calls = []
    monkeypatch.setattr(subject, "verify_native_reader_role", lambda *args: calls.append(args))
    configure(path)
    expected = copy.deepcopy(before)
    expected["services"]["runner"]["image"] = subject.PRIVATE_BB_CE1_IMAGE
    expected["services"]["runner"]["credentials"] = {
        "host": "https://index.docker.io/v1/",
        "username": "jobordu",
        "password": READ_FIXTURE,
    }
    after = yaml.safe_load(path.read_text())
    assert after == expected
    assert calls == [("jobordu", READ_FIXTURE)]
    assert subject.validate_private_profile("bb-ce1") == "dfc-images-123-2"
    env = after["services"]["runner"]["env"]
    assert len(env) == 8
    assert dict(entry.split("=", 1) for entry in env) == {
        "RUNNER_TOKEN": "@@RUNNER_TOKEN@@",
        "ORG_NAME": "Borduas-Holdings",
        "RUNNER_SCOPE": "org",
        "RUNNER_NAME_PREFIX": "just-akash-dfc-images-123-2",
        "LABELS": "self-hosted,linux,akash,dfc-images-123-2",
        "EPHEMERAL": "true",
        "RUNNER_WORKDIR": "/_work",
        "RUN_AS_ROOT": "true",
    }
    assert READ_FIXTURE not in "\n".join(env)
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(item.name for item in tmp_path.iterdir()) == ["output", "runner.yaml"]


BAD_VALUES = [
    ("GITHUB_REPOSITORY", "Borduas-Holdings/blazing"),
    ("RUNNER_ENVIRONMENT", "self-hosted"),
    ("PRIVATE_PROFILE_SOURCE", "foreign/sdk"),
    ("PRIVATE_PROFILE_ORG", "foreign"),
    ("PRIVATE_PROFILE_LABEL", "dfc-images-123-3"),
    ("PRIVATE_PROFILE_LABEL", "dfc-images-124-2"),
    ("PRIVATE_PROFILE_LABEL", "dfc-images-123-2-anything"),
    ("PRIVATE_PROFILE_LABEL", "apps-123-2"),
    ("PRIVATE_PROFILE_PLACEMENT", "borduas-apps-run-123-end"),
    ("PRIVATE_PROFILE_PLACEMENT", "borduas-dfc-images-run-124-end"),
    ("PRIVATE_PROFILE_TAG_PREFIX", "ci-blazing-back-apps"),
    ("PRIVATE_PROFILE_TAG_PREFIX", "ci-blazing-back-dfc-images-extra"),
    ("PRIVATE_PROFILE_EPHEMERAL", "false"),
    ("PRIVATE_PROFILE_POOL_SIZE", "2"),
    ("PRIVATE_PROFILE_MIN_POOL_SIZE", ""),
    ("PRIVATE_PROFILE_MIN_POOL_SIZE", "2"),
    ("PRIVATE_PROFILE_CPU", "2"),
    ("PRIVATE_PROFILE_CPU", "4.0"),
    ("PRIVATE_PROFILE_MEMORY", "8Gi"),
    ("PRIVATE_PROFILE_MEMORY", "16G"),
    ("PRIVATE_PROFILE_STORAGE", "60Gi"),
    ("PRIVATE_PROFILE_STORAGE", "096Gi"),
    ("PRIVATE_PROFILE_OWNED_PROVIDERS", "[]"),
    ("PRIVATE_PROFILE_OWNED_PROVIDERS", json.dumps(["foreign"] * 3)),
    (
        "PRIVATE_PROFILE_OWNED_PROVIDERS",
        json.dumps([sorted(subject.NATIVE_READER_PROVIDERS)[0]] * 3),
    ),
    ("RUNNER_PUBLIC_PROFILE", "bb-ce1"),
    ("RUNNER_PRIVATE_PROFILE", "blazing-aaf"),
    ("RUNNER_REGISTRY_HOST", "https://foreign.invalid/"),
    ("RUNNER_REGISTRY_USERNAME", "foreign"),
    ("RUNNER_REGISTRY_PASSWORD", "secret-echo-canary"),
    ("RUNNER_IMAGE", subject.PRIVATE_BB_CE1_IMAGE),
    ("RUNNER_NATIVE_PULL_READER", "true"),
    ("RUNNER_NATIVE_REPOSITORY_SCOPE", "true"),
    ("PRIVATE_PROFILE_SOPS", "false"),
    ("PRIVATE_PROFILE_AGE_PRESENT", "false"),
    ("PRIVATE_PROFILE_PASSWORD_PRESENT", "true"),
    ("GITHUB_RUN_ID", "0"),
    ("GITHUB_RUN_ID", str(2**64)),
    ("GITHUB_RUN_ATTEMPT", "2\n"),
]


@pytest.mark.parametrize("key,value", BAD_VALUES)
def test_foreign_identity_resource_or_secret_refuses_before_reader(
    tmp_path, monkeypatch, capsys, key, value
):
    dfc_context(monkeypatch)
    monkeypatch.setenv(key, value)
    path = tmp_path / "absent"
    monkeypatch.setattr(
        subject, "read_pull_credentials", lambda *a, **k: pytest.fail("must not read")
    )
    monkeypatch.setattr(
        subject, "verify_native_reader_role", lambda *a, **k: pytest.fail("must not authenticate")
    )
    monkeypatch.setattr(
        sys, "argv", ["runner_image", "--sdl", str(path), "--sops-env-file", str(path)]
    )
    assert subject.main() == 1
    assert not path.exists()
    assert "secret-echo-canary" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "key",
    [
        "PRIVATE_PROFILE_LABEL",
        "PRIVATE_PROFILE_PLACEMENT",
        "PRIVATE_PROFILE_TAG_PREFIX",
        "PRIVATE_PROFILE_POOL_SIZE",
        "PRIVATE_PROFILE_MIN_POOL_SIZE",
        "PRIVATE_PROFILE_EPHEMERAL",
        "PRIVATE_PROFILE_CPU",
        "PRIVATE_PROFILE_MEMORY",
        "PRIVATE_PROFILE_STORAGE",
        "PRIVATE_PROFILE_OWNED_PROVIDERS",
        "PRIVATE_PROFILE_SOPS",
        "PRIVATE_PROFILE_AGE_PRESENT",
    ],
)
def test_required_dfc_metadata_cannot_be_omitted(monkeypatch, key):
    dfc_context(monkeypatch)
    monkeypatch.delenv(key)
    with pytest.raises(ValueError):
        subject.validate_private_profile("bb-ce1")


@pytest.mark.parametrize("admitted", [True, False])
def test_actual_early_bash_guard_checks_resources_before_sops(tmp_path, monkeypatch, admitted):
    dfc_context(monkeypatch)
    if not admitted:
        monkeypatch.setenv("PRIVATE_PROFILE_STORAGE", "30Gi")
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
        {"CPU": "2"},
        {"MEMORY": "8Gi"},
        {"STORAGE": "60Gi"},
        {"POOL_SIZE": "2"},
        {"PLACEMENT_KEY": "borduas-dfc-images-run-124-end"},
        {"RUNNER_LABEL": "dfc-images-123-2-extra"},
        {"RUNNER_PRIVATE_PROFILE": ""},
        {"RUNNER_PUBLIC_PROFILE": "bb-ce1"},
        {"GITHUB_REPOSITORY": "Borduas-Holdings/blazing"},
        {"ORG": "foreign"},
        {"EPHEMERAL": "false"},
        {"GITHUB_RUN_ATTEMPT": "3"},
    ],
)
def test_actual_renderer_rejects_foreign_or_widened_dfc_identity(tmp_path, monkeypatch, changes):
    dfc_context(monkeypatch)
    path, result = dfc_render(tmp_path, changes)
    assert result.returncode == 2
    assert not path.exists()
    assert result.stderr == "Private DFC runner render was not verified\n"


@pytest.mark.parametrize(
    "old,new",
    [
        ("units: 4", "units: 2"),
        ("size: 16Gi", "size: 8Gi"),
        ("size: 96Gi", "size: 60Gi"),
        ("count: 1", "count: 2"),
        ("borduas-dfc-images-run-123-end:", "borduas-dfc-images-run-124-end:"),
    ],
)
def test_generated_resource_or_placement_substitution_holds_before_auth(
    tmp_path, monkeypatch, old, new
):
    dfc_context(monkeypatch)
    path, result = dfc_render(tmp_path)
    assert result.returncode == 0
    path.write_text(path.read_text().replace(old, new))
    before = path.read_bytes()
    monkeypatch.setattr(
        subject, "verify_native_reader_role", lambda *a: pytest.fail("must refuse")
    )
    with pytest.raises(ValueError):
        configure(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "old,new",
    [
        ("count: 1", "count: 1\n      count: 2"),
        ("cpu: { units: 4 }", "cpu: { units: 4 }\n        cpu: { units: 2 }"),
        ("storage: { size: 96Gi }", "storage: { size: 96Gi }\n        gpu: { units: 1 }"),
    ],
)
def test_extra_or_duplicate_resource_nodes_are_refused(tmp_path, monkeypatch, old, new):
    dfc_context(monkeypatch)
    path, result = dfc_render(tmp_path)
    assert result.returncode == 0
    text = path.read_text()
    assert text.count(old) == 1
    path.write_text(text.replace(old, new))
    before = path.read_bytes()
    monkeypatch.setattr(
        subject, "verify_native_reader_role", lambda *a: pytest.fail("must refuse")
    )
    with pytest.raises(ValueError):
        configure(path)
    assert path.read_bytes() == before


def test_private_resource_inputs_are_bound_at_all_three_hosted_gates():
    gates = [step for step in STEPS if "PRIVATE_PROFILE_LABEL" in step.get("env", {})]
    assert len(gates) == 3
    for gate in gates:
        assert {
            name: gate["env"][name]
            for name in (
                "PRIVATE_PROFILE_CPU",
                "PRIVATE_PROFILE_MEMORY",
                "PRIVATE_PROFILE_STORAGE",
            )
        } == {
            "PRIVATE_PROFILE_CPU": "${{ inputs.cpu }}",
            "PRIVATE_PROFILE_MEMORY": "${{ inputs.memory }}",
            "PRIVATE_PROFILE_STORAGE": "${{ inputs.storage }}",
        }
    names = [step.get("name", "") for step in STEPS]
    assert names.index(
        "Validate the fixed private runner profile before reader preparation"
    ) < names.index("Check out the caller encrypted reader bundle")


def test_fresh_reader_role_denial_preserves_dfc_template(tmp_path, monkeypatch):
    dfc_context(monkeypatch)
    path, result = dfc_render(tmp_path)
    assert result.returncode == 0
    before = path.read_bytes()

    def refuse(*args):
        raise subject.NativeReaderRoleError("fixed refusal")

    monkeypatch.setattr(subject, "verify_native_reader_role", refuse)
    with pytest.raises(subject.NativeReaderRoleError):
        configure(path)
    assert path.read_bytes() == before


def test_dfc_cannot_be_selected_as_public_profile(monkeypatch):
    context(monkeypatch, "apps")
    monkeypatch.setenv("RUNNER_PRIVATE_PROFILE", "")
    for key, value in {
        "PUBLIC_PROFILE_LABEL": "dfc-images-123-2",
        "PUBLIC_PROFILE_PLACEMENT": "borduas-dfc-images-run-123-end",
        "PUBLIC_PROFILE_TAG_PREFIX": "ci-blazing-back-dfc-images",
    }.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        subject.validate_public_profile("bb-ce1")


def test_authority_callback_cannot_substitute_the_verified_resource_snapshot(
    tmp_path, monkeypatch
):
    dfc_context(monkeypatch)
    path, result = dfc_render(tmp_path)
    assert result.returncode == 0
    before = yaml.safe_load(path.read_text())

    def change_template(*args):
        path.write_text(path.read_text().replace("size: 96Gi", "size: 60Gi"))

    monkeypatch.setattr(subject, "verify_native_reader_role", change_template)
    configure(path)
    after = yaml.safe_load(path.read_text())
    assert after["profiles"] == before["profiles"]
    assert after["services"]["runner"]["credentials"]["password"] == READ_FIXTURE
