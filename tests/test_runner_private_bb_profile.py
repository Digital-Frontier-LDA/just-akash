"""Private BB bootstrap preserves ce1 identity and excludes privileged tenant secrets."""

from __future__ import annotations

import copy
import os
import subprocess
import sys

import pytest
import yaml

from just_akash import runner_image as subject
from tests.test_runner_public_bb_profile import STEPS, context, template


def private_context(monkeypatch, role="apps"):
    label, values = context(monkeypatch, role)
    private = {
        key.replace("PUBLIC_PROFILE_", "PRIVATE_PROFILE_"): value
        for key, value in values.items()
        if key.startswith("PUBLIC_PROFILE_")
    }
    private.pop("PRIVATE_PROFILE_CREDENTIALS_PRESENT")
    private.update(
        {
            "RUNNER_PUBLIC_PROFILE": "",
            "RUNNER_PRIVATE_PROFILE": "bb-ce1",
            "RUNNER_REGISTRY_HOST": "https://index.docker.io/v1/",
            "RUNNER_REGISTRY_USERNAME": "jobordu",
            "PRIVATE_PROFILE_SOPS": "true",
            "PRIVATE_PROFILE_AGE_PRESENT": "true",
            "PRIVATE_PROFILE_PASSWORD_PRESENT": "false",
        }
    )
    for key, value in private.items():
        monkeypatch.setenv(key, value)
    return label


@pytest.mark.parametrize("role", ["fast", "sentry", "apps"])
def test_private_generated_payload_preserves_env_and_adds_only_reader_credentials(
    tmp_path, monkeypatch, role
):
    path = template(tmp_path, monkeypatch, role)
    before = yaml.safe_load(path.read_text())
    label = private_context(monkeypatch, role)
    calls = []
    monkeypatch.setattr(
        subject, "verify_native_reader_role", lambda user, token: calls.append((user, token))
    )
    subject.configure(
        path,
        image="",
        host="https://index.docker.io/v1/",
        username="jobordu",
        password="reader-canary",
        reader_from_sops=True,
        private_profile="bb-ce1",
    )
    expected = copy.deepcopy(before)
    expected["services"]["runner"]["image"] = subject.PRIVATE_BB_CE1_IMAGE
    expected["services"]["runner"]["credentials"] = {
        "host": "https://index.docker.io/v1/",
        "username": "jobordu",
        "password": "reader-canary",
    }
    actual = yaml.safe_load(path.read_text())
    assert actual == expected
    assert calls == [("jobordu", "reader-canary")]
    assert subject.validate_private_profile("bb-ce1") == label
    assert actual["services"]["runner"]["image"].endswith(
        subject.PUBLIC_BB_CE1_IMAGE.split("@")[1]
    )
    assert len(actual["services"]["runner"]["env"]) == 8
    assert f"RUNNER_NAME_PREFIX=just-akash-{label}" in actual["services"]["runner"]["env"]
    assert "reader-canary" not in "\n".join(actual["services"]["runner"]["env"])
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["output", "runner.yaml"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_REPOSITORY", "Borduas-Holdings/blazing"),
        ("RUNNER_ENVIRONMENT", "self-hosted"),
        ("RUNNER_PUBLIC_PROFILE", "bb-ce1"),
        ("RUNNER_PRIVATE_PROFILE", "foreign"),
        ("RUNNER_IMAGE", subject.PRIVATE_BB_CE1_IMAGE),
        ("RUNNER_REGISTRY_HOST", "https://evil.invalid/"),
        ("RUNNER_REGISTRY_USERNAME", "foreign"),
        ("RUNNER_REGISTRY_PASSWORD", "direct-canary"),
        ("RUNNER_NATIVE_PULL_READER", "true"),
        ("RUNNER_NATIVE_REPOSITORY_SCOPE", "true"),
        ("PRIVATE_PROFILE_SOPS", "false"),
        ("PRIVATE_PROFILE_AGE_PRESENT", "false"),
        ("PRIVATE_PROFILE_PASSWORD_PRESENT", "true"),
        ("PRIVATE_PROFILE_SOURCE", "foreign/sdk"),
        ("PRIVATE_PROFILE_ORG", "foreign"),
        ("PRIVATE_PROFILE_POOL_SIZE", "2"),
        ("PRIVATE_PROFILE_MIN_POOL_SIZE", "0"),
        ("PRIVATE_PROFILE_LABEL", "apps-124-2"),
        ("PRIVATE_PROFILE_PLACEMENT", "borduas-apps-run-124-end"),
        ("PRIVATE_PROFILE_TAG_PREFIX", "foreign"),
        ("PRIVATE_PROFILE_EPHEMERAL", "false"),
        ("PRIVATE_PROFILE_OWNED_PROVIDERS", "[]"),
        ("PRIVATE_PROFILE_OWNED_PROVIDERS", "{}"),
        ("PRIVATE_PROFILE_OWNED_PROVIDERS", '["foreign"]'),
        ("PRIVATE_PROFILE_OWNED_PROVIDERS", " " * 513),
        ("GITHUB_RUN_ID", "0"),
        ("GITHUB_RUN_ID", str(2**64)),
        ("GITHUB_RUN_ATTEMPT", "2\n"),
    ],
)
def test_private_admission_refuses_foreign_roles_and_credential_transport_before_read(
    tmp_path, monkeypatch, key, value
):
    private_context(monkeypatch)
    monkeypatch.setenv(key, value)
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


@pytest.mark.parametrize(
    "options",
    [
        {"native_reader": True},
        {"reader_from_sops": False},
        {"image": subject.PRIVATE_BB_CE1_IMAGE},
        {"public_profile": "bb-ce1"},
        {"host": "https://evil.invalid/"},
        {"username": "foreign"},
    ],
)
def test_direct_configure_cannot_bypass_private_transport(tmp_path, monkeypatch, options):
    path = template(tmp_path, monkeypatch, "apps")
    private_context(monkeypatch)
    before = path.read_bytes()
    monkeypatch.setattr(
        subject, "verify_native_reader_role", lambda *a, **k: pytest.fail("must refuse")
    )
    settings = {
        "image": "",
        "host": "https://index.docker.io/v1/",
        "username": "jobordu",
        "password": "reader-canary",
        "reader_from_sops": True,
        "private_profile": "bb-ce1",
    }
    settings.update(options)
    with pytest.raises(ValueError):
        subject.configure(path, **settings)
    assert path.read_bytes() == before


def test_fresh_writer_role_refusal_preserves_template(tmp_path, monkeypatch):
    path = template(tmp_path, monkeypatch, "apps")
    private_context(monkeypatch)
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
            password="writer-canary",
            reader_from_sops=True,
            private_profile="bb-ce1",
        )
    assert path.read_bytes() == before


def test_cli_requires_sops_before_template_or_authentication(tmp_path, monkeypatch):
    private_context(monkeypatch)
    path = tmp_path / "absent"
    monkeypatch.setattr(
        subject, "verify_native_reader_role", lambda *a, **k: pytest.fail("must refuse")
    )
    monkeypatch.setattr(sys, "argv", ["runner_image", "--sdl", str(path)])
    assert subject.main() == 1
    assert not path.exists()


def test_preparation_guard_refuses_before_sops_and_lease(tmp_path, monkeypatch):
    private_context(monkeypatch)
    guard = next(step for step in STEPS if step.get("id") == "private_profile")
    checkout = next(
        step
        for step in STEPS
        if step.get("name") == "Check out the caller encrypted reader bundle"
    )
    assert STEPS.index(guard) < STEPS.index(checkout)
    assert guard["if"] == "inputs.runner-private-profile != ''"
    assert "SOPS_AGE_KEY" not in guard["env"]
    assert "RUNNER_REGISTRY_PASSWORD" not in guard["env"]
    assert len(guard["env"]) == 19
    assert (
        guard["env"]["PRIVATE_PROFILE_PASSWORD_PRESENT"]
        == "${{ secrets.RUNNER_REGISTRY_PASSWORD != '' }}"
    )
    output = tmp_path / "output"
    env = {**os.environ, "GITHUB_OUTPUT": str(output), "PRIVATE_PROFILE_AGE_PRESENT": "false"}
    result = subprocess.run(
        ["bash", "-e", "-c", guard["run"]], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert output.read_text().splitlines() == [
        "deployment_outcome=no-deployment",
        "failure_reason=PRIVATE_PROFILE_UNQUALIFIED",
    ]


def test_private_cli_check_does_not_decrypt_or_touch_template(tmp_path, monkeypatch):
    private_context(monkeypatch)
    path = tmp_path / "absent"
    monkeypatch.setattr(
        subject, "read_pull_credentials", lambda *a, **k: pytest.fail("must not decrypt")
    )
    monkeypatch.setattr(
        sys, "argv", ["runner_image", "--sdl", str(path), "--check-private-profile"]
    )
    assert subject.main() == 0
    assert not path.exists()


def test_cli_decrypts_only_hosted_reader_then_verifies_role_and_writes_private_payload(
    tmp_path, monkeypatch
):
    path = template(tmp_path, monkeypatch, "apps")
    private_context(monkeypatch)
    bundle = tmp_path / "registry-pull.sops.env"
    bundle.write_text("encrypted-fixture")
    calls = []

    def decrypt(selected, **options):
        assert selected == bundle
        assert options == {"username": "jobordu", "password": "", "native_reader": False}
        calls.append("decrypt-reader")
        return "jobordu", "reader-canary"

    def verify(user, token):
        assert (user, token) == ("jobordu", "reader-canary")
        calls.append("fresh-role")

    monkeypatch.setattr(subject, "read_pull_credentials", decrypt)
    monkeypatch.setattr(subject, "verify_native_reader_role", verify)
    monkeypatch.setattr(
        sys, "argv", ["runner_image", "--sdl", str(path), "--sops-env-file", str(bundle)]
    )
    assert subject.main() == 0
    assert calls == ["decrypt-reader", "fresh-role"]
    runner = yaml.safe_load(path.read_text())["services"]["runner"]
    assert runner["image"] == subject.PRIVATE_BB_CE1_IMAGE
    assert runner["credentials"]["password"] == "reader-canary"
    assert len(runner["env"]) == 8
    assert not any(
        "reader-canary" in entry or "AGE" in entry or "GH_TOKEN" in entry
        for entry in runner["env"]
    )


def test_private_profile_is_opt_in_and_denial_is_exported_without_overriding_creation():
    from tests.test_runner_public_bb_profile import DOC

    on = DOC["on"] if "on" in DOC else DOC[True]
    item = on["workflow_call"]["inputs"]["runner-private-profile"]
    assert item["type"] == "string"
    assert item["default"] == ""
    assert item["required"] is False
    output = DOC["jobs"]["pool"]["outputs"]["deployment_outcome"]
    assert output.index("steps.provision.outputs.deployment_outcome") < output.index(
        "steps.private_profile.outputs.deployment_outcome"
    )
    assert (
        "steps.private_profile.outputs.failure_reason"
        in DOC["jobs"]["pool"]["outputs"]["failure_reason"]
    )


@pytest.mark.parametrize("mutation", ["env", "image", "credentials", "link"])
def test_private_malformed_template_is_rejected_without_partial_credentials(
    tmp_path, monkeypatch, mutation
):
    path = template(tmp_path, monkeypatch, "apps")
    private_context(monkeypatch)
    original = path.read_text()
    if mutation == "env":
        path.write_text(
            original.replace("RUNNER_NAME_PREFIX=just-akash-", "RUNNER_NAME_PREFIX=foreign-")
        )
    elif mutation == "image":
        path.write_text(original.replace("@sha256:", ":other@sha256:"))
    elif mutation == "credentials":
        path.write_text(
            original.replace("    env:", "    credentials:\n      password: foreign\n    env:")
        )
    else:
        target = tmp_path / "target"
        path.replace(target)
        path.symlink_to(target)
    before = path.read_bytes()
    monkeypatch.setattr(subject, "verify_native_reader_role", lambda *a, **k: None)
    with pytest.raises(ValueError):
        subject.configure(
            path,
            image="",
            host="https://index.docker.io/v1/",
            username="jobordu",
            password="reader-canary",
            reader_from_sops=True,
            private_profile="bb-ce1",
        )
    assert path.read_bytes() == before
    assert b"reader-canary" not in path.read_bytes()


def test_private_destination_preserves_public_root_without_reusing_legacy_mirror_tags():
    assert subject.PRIVATE_BB_CE1_IMAGE == (
        "docker.io/digitalfrontierunipessoallda/df-akash-runner@sha256:"
        "ce1b123c98e273479e08e6315fc81f7017957c23dbf88878076e75572b7b18cc"  # pragma: allowlist secret
    )
    assert (
        subject.PRIVATE_BB_CE1_IMAGE.split("@", 1)[1]
        == subject.PUBLIC_BB_CE1_IMAGE.split("@", 1)[1]
    )
    assert (
        subject.PRIVATE_BB_CE1_IMAGE.split("@", 1)[1]
        != subject.NATIVE_READER_IMAGE.split("@", 1)[1]
    )
