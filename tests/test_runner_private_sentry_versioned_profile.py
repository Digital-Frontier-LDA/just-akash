"""Versioned Sentry placement joins private admission to strict lifecycle naming."""

import pytest
import yaml

from just_akash import runner_image as subject
from just_akash.workload_identity import Identity, format_identity, parse_identity
from tests.test_runner_private_bb_profile import private_context
from tests.test_runner_public_bb_profile import template

REGISTER = {"borduas-sentry-": "Borduas-Holdings/Blazing-Back"}


def versioned_context(monkeypatch):
    label = private_context(monkeypatch, "sentry")
    identity = Identity(
        "borduas-sentry-", REGISTER["borduas-sentry-"], "ci-runner", 1, run=123, attempt=2
    )
    placement = format_identity(identity, REGISTER)
    monkeypatch.setenv("PRIVATE_PROFILE_PLACEMENT", placement)
    for key, value in (("CPU", "2"), ("MEMORY", "6Gi"), ("STORAGE", "40Gi")):
        monkeypatch.setenv("PRIVATE_PROFILE_" + key, value)
    return label, placement, identity


def versioned_template(tmp_path, monkeypatch):
    path = template(tmp_path, monkeypatch, "sentry")
    label, placement, identity = versioned_context(monkeypatch)
    text = path.read_text().replace("borduas-sentry-run-123-end", placement)
    text = text.replace("cpu: { units: 4 }", "cpu: { units: 2 }")
    text = text.replace("memory: { size: 16Gi }", "memory: { size: 6Gi }")
    path.write_text(text.replace("storage: { size: 30Gi }", "storage: { size: 40Gi }"))
    return path, label, placement, identity


def configure(path):
    subject.configure(
        path,
        image="",
        host="https://index.docker.io/v1/",
        username="jobordu",
        password="reader-fixture",
        reader_from_sops=True,
        private_profile="bb-ce1",
    )


def test_versioned_private_sentry_is_exact_strict_cleanup_identity(monkeypatch):
    label, placement, identity = versioned_context(monkeypatch)
    assert parse_identity(placement, REGISTER) == identity
    assert subject.validate_private_profile("bb-ce1") == label


def test_versioned_sentry_private_payload_preserves_original_resources_and_secret_boundary(
    tmp_path, monkeypatch
):
    path, label, placement, identity = versioned_template(tmp_path, monkeypatch)
    before = yaml.safe_load(path.read_text())
    calls = []
    monkeypatch.setattr(
        subject, "verify_native_reader_role", lambda user, token: calls.append((user, token))
    )
    configure(path)
    after = yaml.safe_load(path.read_text())
    assert parse_identity(next(iter(after["deployment"]["runner"])), REGISTER) == identity
    assert after["profiles"] == before["profiles"] and after["deployment"] == before["deployment"]
    assert after["profiles"]["compute"]["runner"]["resources"] == {
        "cpu": {"units": 2},
        "memory": {"size": "6Gi"},
        "storage": {"size": "40Gi"},
    }
    runner = after["services"]["runner"]
    assert runner["image"] == subject.PRIVATE_BB_CE1_IMAGE
    assert runner["credentials"] == {
        "host": "https://index.docker.io/v1/",
        "username": "jobordu",
        "password": "reader-fixture",
    }
    assert runner["env"] == before["services"]["runner"]["env"]
    assert f"RUNNER_NAME_PREFIX=just-akash-{label}" in runner["env"]
    assert not any(
        "reader-fixture" in value or "AGE" in value or "GH_TOKEN" in value
        for value in runner["env"]
    )
    assert calls == [("jobordu", "reader-fixture")]
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "key,value",
    [
        (
            "PRIVATE_PROFILE_PLACEMENT",
            "borduas-sentry-idv1-class-ci-runner-g2-attempt-2-run-123-end",
        ),
        (
            "PRIVATE_PROFILE_PLACEMENT",
            "borduas-sentry-idv1-class-ci-payload-g1-attempt-2-run-123-end",
        ),
        (
            "PRIVATE_PROFILE_PLACEMENT",
            "borduas-sentry-idv1-class-ci-runner-g1-attempt-1-run-123-end",
        ),
        (
            "PRIVATE_PROFILE_PLACEMENT",
            "borduas-sentry-idv1-class-ci-runner-g1-attempt-2-run-124-end",
        ),
        (
            "PRIVATE_PROFILE_PLACEMENT",
            "borduas-sentry-idv1-class-ci-runner-g1-attempt-02-run-123-end",
        ),
        ("PRIVATE_PROFILE_CPU", "4"),
        ("PRIVATE_PROFILE_MEMORY", "16Gi"),
        ("PRIVATE_PROFILE_STORAGE", "96Gi"),
        ("RUNNER_NATIVE_PULL_READER", "true"),
        ("RUNNER_NATIVE_REPOSITORY_SCOPE", "true"),
    ],
)
def test_versioned_sentry_mismatch_refuses_before_authentication_or_write(
    tmp_path, monkeypatch, key, value
):
    path, *_ = versioned_template(tmp_path, monkeypatch)
    before = path.read_bytes()
    monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        subject,
        "verify_native_reader_role",
        lambda *a: pytest.fail("must refuse before issuer authentication"),
    )
    with pytest.raises(ValueError):
        configure(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "original,replacement",
    [
        ("cpu: { units: 2 }", "cpu: { units: 4 }"),
        ("memory: { size: 6Gi }", "memory: { size: 16Gi }"),
        ("storage: { size: 40Gi }", "storage: { size: 96Gi }"),
        ("count: 1", "count: 2"),
        ("attempt-2-run-123-end", "attempt-1-run-123-end"),
    ],
)
def test_versioned_sentry_actual_sdl_mismatch_refuses_before_authentication(
    tmp_path, monkeypatch, original, replacement
):
    path, *_ = versioned_template(tmp_path, monkeypatch)
    assert original in path.read_text()
    path.write_text(path.read_text().replace(original, replacement))
    before = path.read_bytes()
    monkeypatch.setattr(
        subject,
        "verify_native_reader_role",
        lambda *a: pytest.fail("must refuse actual mismatched SDL"),
    )
    with pytest.raises(ValueError):
        configure(path)
    assert path.read_bytes() == before
