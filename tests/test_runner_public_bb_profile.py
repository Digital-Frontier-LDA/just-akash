"""Fixed public BB ce1 payload selection cannot broaden private registry admission."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from just_akash import runner_image as subject

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/runner-pool.yml"
DOC = yaml.safe_load(WORKFLOW.read_text())
STEPS = DOC["jobs"]["pool"]["steps"]


def context(monkeypatch, role="fast"):
    for key in (
        "RUNNER_IMAGE",
        "RUNNER_REGISTRY_HOST",
        "RUNNER_REGISTRY_USERNAME",
        "RUNNER_REGISTRY_PASSWORD",
        "SOPS_AGE_KEY",
        "RUNNER_REGISTRY_AGE_KEY",
        "DOCKERHUB_PULL_USERNAME",
        "DOCKERHUB_PULL_TOKEN",
        "RUNNER_NATIVE_PULL_READER",
        "RUNNER_NATIVE_REPOSITORY_SCOPE",
    ):
        monkeypatch.delenv(key, raising=False)
    label, placement, tag, ephemeral = {
        "fast": (
            "fast-pool-123",
            "dfci-infra-runner-run-123-end",
            "ci-blazing-back-fast-pool",
            "false",
        ),
        "sentry": ("sentry-123-2", "borduas-sentry-run-123-end", "ci-blazing-back-sentry", "true"),
        "apps": ("apps-123-2", "borduas-apps-run-123-end", "ci-blazing-back-apps", "true"),
    }[role]
    values = {
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_REPOSITORY": "Borduas-Holdings/Blazing-Back",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "PUBLIC_PROFILE_SOURCE": "Digital-Frontier-LDA/just-akash",
        "PUBLIC_PROFILE_ORG": "Borduas-Holdings",
        "PUBLIC_PROFILE_POOL_SIZE": "1",
        "PUBLIC_PROFILE_MIN_POOL_SIZE": "1",
        "PUBLIC_PROFILE_EPHEMERAL": ephemeral,
        "PUBLIC_PROFILE_LABEL": label,
        "PUBLIC_PROFILE_PLACEMENT": placement,
        "PUBLIC_PROFILE_TAG_PREFIX": tag,
        "PUBLIC_PROFILE_OWNED_PROVIDERS": json.dumps(sorted(subject.NATIVE_READER_PROVIDERS)),
        "PUBLIC_PROFILE_SOPS": "false",
        "PUBLIC_PROFILE_CREDENTIALS_PRESENT": "false",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return label, values


def template(tmp_path, monkeypatch, role="fast"):
    label, values = context(monkeypatch, role)
    path = tmp_path / "runner.yaml"
    step = next(step for step in STEPS if step.get("id") == "render")
    script = step["run"].replace("/tmp/runner-sdl.yaml", str(path))
    env = {
        **os.environ,
        "ORG": "Borduas-Holdings",
        "RUNNER_LABEL": label,
        "POOL_SIZE": "1",
        "CPU": "4",
        "MEMORY": "16Gi",
        "STORAGE": "30Gi",
        "EPHEMERAL": values["PUBLIC_PROFILE_EPHEMERAL"],
        "PLACEMENT_KEY": values["PUBLIC_PROFILE_PLACEMENT"],
        "GH_RUN_ID": "123",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "RUNNER_PUBLIC_PROFILE": "bb-ce1",
    }
    result = subprocess.run(
        ["bash", "-e", "-c", script], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    return path


@pytest.mark.parametrize("role", ["fast", "sentry", "apps"])
def test_actual_generated_template_selects_only_ce1_and_preserves_reaped_name(
    tmp_path, monkeypatch, role
):
    path = template(tmp_path, monkeypatch, role)
    before = yaml.safe_load(path.read_text())
    subject.configure(path, image="", host="", username="", password="", public_profile="bb-ce1")
    after = yaml.safe_load(path.read_text())
    expected = copy.deepcopy(before)
    expected["services"]["runner"]["image"] = subject.PUBLIC_BB_CE1_IMAGE
    assert after == expected
    runner_env = dict(entry.split("=", 1) for entry in after["services"]["runner"]["env"])
    reaper = yaml.safe_load((WORKFLOW.parent / "reap-stale-runners.yml").read_text())
    backstop = reaper["jobs"]["reap-stale-runners"]
    assert backstop["uses"].endswith("@5d82c5973e01b0067e61e7b65ab97579aed5ffd9")
    assert backstop["with"]["name-prefixes"] == "just-akash-"
    assert runner_env["RUNNER_NAME_PREFIX"].startswith(backstop["with"]["name-prefixes"])
    assert not runner_env["RUNNER_NAME_PREFIX"].startswith("df-core-")
    assert path.stat().st_mode & 0o777 == 0o600
    assert all("credentials" not in service for service in after["services"].values())
    assert any(
        entry == "RUNNER_TOKEN=@@RUNNER_TOKEN@@" for entry in after["services"]["runner"]["env"]
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_REPOSITORY", "Borduas-Holdings/blazing"),
        ("RUNNER_ENVIRONMENT", "self-hosted"),
        ("PUBLIC_PROFILE_SOURCE", "foreign/just-akash"),
        ("PUBLIC_PROFILE_ORG", "foreign"),
        ("GITHUB_RUN_ID", "0"),
        ("GITHUB_RUN_ATTEMPT", "2\n"),
        ("GITHUB_RUN_ID", str(2**64)),
        ("PUBLIC_PROFILE_POOL_SIZE", "2"),
        ("PUBLIC_PROFILE_MIN_POOL_SIZE", "0"),
        ("PUBLIC_PROFILE_LABEL", "fast-pool-124"),
        ("PUBLIC_PROFILE_LABEL", "other-123"),
        ("PUBLIC_PROFILE_PLACEMENT", "dfci-infra-runner-run-124-end"),
        ("PUBLIC_PROFILE_TAG_PREFIX", "ci-foreign"),
        ("PUBLIC_PROFILE_EPHEMERAL", "true"),
        ("PUBLIC_PROFILE_OWNED_PROVIDERS", "[]"),
        ("PUBLIC_PROFILE_OWNED_PROVIDERS", '["foreign"]'),
        ("PUBLIC_PROFILE_OWNED_PROVIDERS", "{}"),
        ("PUBLIC_PROFILE_OWNED_PROVIDERS", " " * 513),
        ("PUBLIC_PROFILE_OWNED_PROVIDERS", "[" * 3000 + "]" * 3000),
        ("PUBLIC_PROFILE_SOPS", "true"),
        ("PUBLIC_PROFILE_CREDENTIALS_PRESENT", "true"),
        ("RUNNER_NATIVE_PULL_READER", "true"),
        ("RUNNER_NATIVE_REPOSITORY_SCOPE", "true"),
        ("RUNNER_IMAGE", "echo-canary"),
        ("RUNNER_REGISTRY_HOST", "echo-canary"),
        ("RUNNER_REGISTRY_USERNAME", "echo-canary"),
        ("RUNNER_REGISTRY_PASSWORD", "echo-canary"),
        ("SOPS_AGE_KEY", "echo-canary"),
        ("RUNNER_REGISTRY_AGE_KEY", "echo-canary"),
        ("DOCKERHUB_PULL_TOKEN", "echo-canary"),
    ],
)
def test_foreign_role_ownership_or_credentials_hold_without_template_change(
    tmp_path, monkeypatch, key, value
):
    path = template(tmp_path, monkeypatch)
    original = path.read_bytes()
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError) as caught:
        subject.configure(
            path, image="", host="", username="", password="", public_profile="bb-ce1"
        )
    assert "echo-canary" not in str(caught.value) and path.read_bytes() == original


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_REPOSITORY", "Borduas-Holdings/blazing"),
        ("RUNNER_ENVIRONMENT", "self-hosted"),
        ("PUBLIC_PROFILE_SOURCE", "foreign/just-akash"),
        ("PUBLIC_PROFILE_ORG", "foreign"),
        ("PUBLIC_PROFILE_LABEL", "apps-124-2"),
        ("PUBLIC_PROFILE_LABEL", "apps-123-1"),
        ("PUBLIC_PROFILE_PLACEMENT", "borduas-apps-run-124-end"),
        ("PUBLIC_PROFILE_PLACEMENT", "borduas-sentry-run-123-end"),
        ("PUBLIC_PROFILE_TAG_PREFIX", "ci-blazing-back-sentry"),
        ("PUBLIC_PROFILE_EPHEMERAL", "false"),
        ("PUBLIC_PROFILE_POOL_SIZE", "2"),
        ("PUBLIC_PROFILE_MIN_POOL_SIZE", ""),
        ("PUBLIC_PROFILE_MIN_POOL_SIZE", "0"),
        ("PUBLIC_PROFILE_MIN_POOL_SIZE", "2"),
        ("PUBLIC_PROFILE_OWNED_PROVIDERS", "[]"),
        ("PUBLIC_PROFILE_OWNED_PROVIDERS", '["foreign"]'),
        ("PUBLIC_PROFILE_SOPS", "true"),
        ("PUBLIC_PROFILE_CREDENTIALS_PRESENT", "true"),
        ("RUNNER_NATIVE_PULL_READER", "true"),
        ("RUNNER_NATIVE_REPOSITORY_SCOPE", "true"),
        ("RUNNER_IMAGE", "echo-canary"),
        ("RUNNER_REGISTRY_HOST", "echo-canary"),
        ("RUNNER_REGISTRY_USERNAME", "echo-canary"),
        ("RUNNER_REGISTRY_PASSWORD", "echo-canary"),
        ("SOPS_AGE_KEY", "echo-canary"),
        ("DOCKERHUB_PULL_TOKEN", "echo-canary"),
    ],
)
def test_apps_role_holds_foreign_or_cross_role_or_credential_delivery(
    tmp_path, monkeypatch, capsys, key, value
):
    path = template(tmp_path, monkeypatch, "apps")
    original = path.read_bytes()
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError) as caught:
        subject.configure(
            path, image="", host="", username="", password="", public_profile="bb-ce1"
        )
    captured = capsys.readouterr()
    assert path.read_bytes() == original
    assert "echo-canary" not in str(caught.value) + captured.out + captured.err
    assert "add-mask" not in captured.out


@pytest.mark.parametrize("role", ["fast", "sentry"])
def test_existing_roles_keep_optional_minimum_pool_contract(monkeypatch, role):
    label, _ = context(monkeypatch, role)
    monkeypatch.delenv("PUBLIC_PROFILE_MIN_POOL_SIZE")
    assert subject.validate_public_profile("bb-ce1") == label


@pytest.mark.parametrize(
    "kwargs",
    [
        {"image": "echo-canary"},
        {"password": "echo-canary"},  # pragma: allowlist secret
        {"native_reader": True},
        {"reader_from_sops": True},
        {"public_profile": "foreign"},
    ],
)
def test_direct_api_cannot_combine_public_profile_with_private_options(
    tmp_path, monkeypatch, kwargs
):
    path = template(tmp_path, monkeypatch)
    original = path.read_bytes()
    options = dict(image="", host="", username="", password="", public_profile="bb-ce1")
    with pytest.raises(ValueError):
        subject.configure(path, **(options | kwargs))
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "mutation",
    [
        "image",
        "extra-image",
        "credential",
        "quoted-credential",
        "reader-env",
        "pat-env",
        "duplicate-env",
        "scope",
        "prefix",
        "symlink",
        "hardlink",
        "oversized",
    ],
)
def test_noncanonical_template_is_not_a_public_profile(tmp_path, monkeypatch, mutation):
    path = template(tmp_path, monkeypatch)
    text = path.read_text()
    if mutation == "image":
        text = text.replace("image: ghcr.io/", "image: foreign.io/")
    elif mutation == "extra-image":
        text += "\nimage: foreign\n"
    elif mutation in ("credential", "quoted-credential"):
        key = "credentials" if mutation == "credential" else '"credentials"'
        text = text.replace("    env:", "    " + key + ": {password: echo-canary}\n    env:")
    elif mutation in ("reader-env", "pat-env", "duplicate-env"):
        entry = (
            "DOCKERHUB_PULL_TOKEN=echo-canary"
            if mutation == "reader-env"
            else "ACCESS_TOKEN=echo-canary"
            if mutation == "pat-env"
            else "RUNNER_TOKEN=@@RUNNER_TOKEN@@"
        )
        text = text.replace("    env:", "    env:\n      - " + entry)
    elif mutation == "scope":
        text = text.replace("RUNNER_SCOPE=org", "RUNNER_SCOPE=repo")
    elif mutation == "prefix":
        text = text.replace("just-akash-fast-pool", "foreign-fast-pool")
    elif mutation == "oversized":
        text += "#" + "x" * 65536
    path.write_text(text)
    if mutation == "symlink":
        original = tmp_path / "original"
        path.rename(original)
        path.symlink_to(original)
    elif mutation == "hardlink":
        os.link(path, tmp_path / "other")
    original = path.read_bytes()
    with pytest.raises(ValueError):
        subject.configure(
            path, image="", host="", username="", password="", public_profile="bb-ce1"
        )
    assert path.read_bytes() == original


@pytest.mark.parametrize("role", ["fast", "sentry", "apps"])
def test_profile_plus_sops_is_refused_before_decryption_or_mask(
    tmp_path, monkeypatch, capsys, role
):
    path = template(tmp_path, monkeypatch, role)
    monkeypatch.setenv("RUNNER_PUBLIC_PROFILE", "bb-ce1")
    monkeypatch.setattr(
        sys, "argv", ["runner", "--sdl", str(path), "--sops-env-file", "unread-cipher"]
    )
    monkeypatch.setattr(
        subject, "read_pull_credentials", lambda *args, **kwargs: pytest.fail("must never decrypt")
    )
    assert subject.main() == 1
    assert "add-mask" not in capsys.readouterr().out


@pytest.mark.parametrize("role", ["fast", "sentry", "apps"])
def test_default_off_keeps_current_rendered_bytes_and_never_checks_profile(
    tmp_path, monkeypatch, role
):
    path = template(tmp_path, monkeypatch, role)
    original = path.read_bytes()
    monkeypatch.setattr(
        subject, "validate_public_profile", lambda *args: pytest.fail("default must not gate")
    )
    subject.configure(path, image="", host="", username="", password="")
    assert path.read_bytes() == original


def test_workflow_new_input_is_false_default_and_profile_admission_precedes_sops():
    call = DOC["on"] if "on" in DOC else DOC[True]
    assert call["workflow_call"]["inputs"]["runner-public-profile"] == {
        "description": (
            "Opt-in fixed public BB ce1 payload; empty preserves the existing qualified "
            "runner contract."
        ),
        "required": False,
        "type": "string",
        "default": "",
    }
    guard = next(step for step in STEPS if step.get("id") == "public_profile")
    assert STEPS.index(guard) < next(
        i for i, step in enumerate(STEPS) if "sparse-checkout" in step.get("with", {})
    )
    assert guard["if"] == "inputs.runner-public-profile != ''"
    assert all(
        "${{ secrets." not in value
        for key, value in guard["env"].items()
        if key != "PUBLIC_PROFILE_CREDENTIALS_PRESENT"
    )
    assert (
        guard["env"]["PUBLIC_PROFILE_CREDENTIALS_PRESENT"]
        == "${{ secrets.RUNNER_REGISTRY_PASSWORD != '' || secrets.RUNNER_REGISTRY_AGE_KEY != '' }}"
    )
    assert "deployment_outcome=no-deployment" in guard["run"] and "exit 1" in guard["run"]
    assert DOC["jobs"]["pool"]["outputs"]["deployment_outcome"].startswith(
        "${{ steps.provision.outputs.deployment_outcome || "
        "steps.public_profile.outputs.deployment_outcome"
    )


@pytest.mark.parametrize("role", ["fast", "sentry", "apps"])
def test_early_profile_check_is_executed_and_writes_only_fixed_refusal(
    tmp_path, monkeypatch, role
):
    _, env = context(monkeypatch, role)
    guard = next(step for step in STEPS if step.get("id") == "public_profile")
    script = guard["run"].replace("python3", str(sys.executable))
    output = tmp_path / "guard-output"
    values = {**os.environ, **env, "RUNNER_PUBLIC_PROFILE": "bb-ce1", "GITHUB_OUTPUT": str(output)}
    good = subprocess.run(
        ["bash", "-e", "-c", script], env=values, capture_output=True, text=True, check=False
    )
    assert good.returncode == 0 and not output.exists()
    bad = subprocess.run(
        ["bash", "-e", "-c", script],
        env={**values, "PUBLIC_PROFILE_CREDENTIALS_PRESENT": "true"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 1
    assert (
        output.read_text() == "deployment_outcome=no-deployment\n"
        "failure_reason=PUBLIC_PROFILE_UNQUALIFIED\n"  # pragma: allowlist secret
    )


@pytest.mark.parametrize("outcome", ["created", "unknown", "no-deployment"])
def test_actual_provision_outcome_wins_over_pre_create_refusal(outcome):
    expression = DOC["jobs"]["pool"]["outputs"]["deployment_outcome"]
    operands = expression.removeprefix("${{ ").split(" || ")[:2]
    values = {
        "steps.provision.outputs.deployment_outcome": outcome,
        "steps.public_profile.outputs.deployment_outcome": "no-deployment",
    }
    assert next(values[operand] for operand in operands if values[operand]) == outcome
    # Once the actual producer has no outcome, the proved pre-create refusal applies.
    values["steps.provision.outputs.deployment_outcome"] = ""
    assert next(values[operand] for operand in operands if values[operand]) == "no-deployment"


@pytest.mark.parametrize(
    "changes",
    [
        {"RUNNER_PUBLIC_PROFILE": ""},
        {"RUNNER_PUBLIC_PROFILE": "foreign"},
        {"GITHUB_REPOSITORY": "Borduas-Holdings/blazing"},
        {"PLACEMENT_KEY": "dfci-infra-runner-run-124-end"},
        {"RUNNER_LABEL": "fast-pool-124"},
    ],
)
def test_actual_render_keeps_sibling_placement_refused_outside_fixed_fast_tuple(
    tmp_path, monkeypatch, changes
):
    label, values = context(monkeypatch)
    path = tmp_path / "runner.yaml"
    script = next(step for step in STEPS if step.get("id") == "render")["run"]
    script = script.replace("/tmp/runner-sdl.yaml", str(path))
    env = {
        **os.environ,
        "ORG": "Borduas-Holdings",
        "RUNNER_LABEL": label,
        "POOL_SIZE": "1",
        "CPU": "4",
        "MEMORY": "16Gi",
        "STORAGE": "30Gi",
        "EPHEMERAL": "false",
        "PLACEMENT_KEY": values["PUBLIC_PROFILE_PLACEMENT"],
        "GH_RUN_ID": "123",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "RUNNER_PUBLIC_PROFILE": "bb-ce1",
        **changes,
    }
    result = subprocess.run(
        ["bash", "-e", "-c", script], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert not path.exists()
