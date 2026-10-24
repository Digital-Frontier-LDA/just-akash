"""Closed current-image DFC profile and actual pool render gate; no live calls."""

import json
import subprocess
import textwrap
from pathlib import Path
from string import Template

import pytest
import yaml

from just_akash import runner_image as image

ROOT = Path(__file__).resolve().parents[1]
E71 = (
    "docker.io/digitalfrontierunipessoallda/akash-runner@sha256:"
    "e71bf4be0e3a59a57ff791b0fc3bc3a31f216f66807b6ac41f1a23346321baa4"
)
RUN, ATTEMPT = "411", "2"


@pytest.fixture
def environment(monkeypatch):
    values = {
        "GITHUB_RUN_ID": RUN,
        "GITHUB_RUN_ATTEMPT": ATTEMPT,
        "GITHUB_REPOSITORY": "Borduas-Holdings/Blazing-Back",
        "GITHUB_REPOSITORY_ID": "1071436278",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_WORKFLOW_REF": (
            "Borduas-Holdings/Blazing-Back/.github/workflows/build-dfc-images.yml@refs/heads/main"
        ),
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_PRIVATE_PROFILE": "bb-dfc-e71",
        "RUNNER_PUBLIC_PROFILE": "",
        "RUNNER_IMAGE": "",
        "RUNNER_REGISTRY_HOST": "https://index.docker.io/v1/",
        "RUNNER_REGISTRY_USERNAME": "jobordu",
        "RUNNER_REGISTRY_PASSWORD": "",
        "PRIVATE_PROFILE_SOPS": "true",
        "PRIVATE_PROFILE_AGE_PRESENT": "true",
        "PRIVATE_PROFILE_PASSWORD_PRESENT": "false",
        "PRIVATE_PROFILE_SOURCE": "Digital-Frontier-LDA/just-akash",
        "PRIVATE_PROFILE_ORG": "Borduas-Holdings",
        "PRIVATE_PROFILE_OWNED_PROVIDERS": json.dumps(sorted(image.NATIVE_READER_PROVIDERS)),
        "PRIVATE_PROFILE_LABEL": f"dfc-images-{RUN}-{ATTEMPT}",
        "PRIVATE_PROFILE_PLACEMENT": f"borduas-dfc-images-run-{RUN}-end",
        "PRIVATE_PROFILE_TAG_PREFIX": "ci-blazing-back-dfc-images",
        "PRIVATE_PROFILE_POOL_SIZE": "1",
        "PRIVATE_PROFILE_MIN_POOL_SIZE": "1",
        "PRIVATE_PROFILE_EPHEMERAL": "true",
        "PRIVATE_PROFILE_CPU": "4",
        "PRIVATE_PROFILE_MEMORY": "16Gi",
        "PRIVATE_PROFILE_STORAGE": "96Gi",
    }
    # Do not inherit an operator's authentication environment into the controls.
    monkeypatch.setattr(image.os, "environ", values.copy())
    calls = []
    monkeypatch.setattr(
        image, "verify_native_reader_role", lambda user, token: calls.append((user, token))
    )
    return calls


def template():
    workflow = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())
    render = next(
        step["run"] for step in workflow["jobs"]["pool"]["steps"] if step.get("id") == "render"
    )
    start = render.index("<<SDL\n") + len("<<SDL\n")
    raw = render[start : render.index("\nSDL", start)]
    return (
        Template(raw).substitute(
            ORG="Borduas-Holdings",
            RUNNER_LABEL=f"dfc-images-{RUN}-{ATTEMPT}",
            EPHEMERAL="true",
            CPU="4",
            MEMORY="16Gi",
            STORAGE="96Gi",
            POOL_SIZE="1",
            PLACEMENT_KEY=f"borduas-dfc-images-run-{RUN}-end",
        )
        + "\n"
    )


def configure(tmp_path, profile="bb-dfc-e71", text=None):
    path = tmp_path / "runner.yaml"
    path.write_text(template() if text is None else text)
    image.configure(
        path,
        image="",
        host="https://index.docker.io/v1/",
        username="jobordu",
        password="public-synthetic-reader",
        reader_from_sops=True,
        private_profile=profile,
    )
    return path


def test_actual_renderer_keeps_e71_complete_document_and_standard_credentials(
    tmp_path, environment
):
    before = yaml.safe_load(template())
    path = configure(tmp_path)
    after = yaml.safe_load(path.read_text())
    expected = before["services"]["runner"]
    expected["image"] = E71
    expected["credentials"] = dict(
        host="https://index.docker.io/v1/", username="jobordu", password="public-synthetic-reader"
    )
    assert after == before
    assert path.stat().st_mode & 0o777 == 0o600
    assert "RUNNER_TOKEN=@@RUNNER_TOKEN@@" in after["services"]["runner"]["env"]
    assert not any(
        "ACCESS_TOKEN" in value or "AGE" in value for value in after["services"]["runner"]["env"]
    )
    assert environment == [("jobordu", "public-synthetic-reader")]


def test_original_legacy_private_dfc_profile_keeps_ce1(tmp_path, environment):
    path = configure(tmp_path, "bb-ce1")
    assert (
        yaml.safe_load(path.read_text())["services"]["runner"]["image"]
        == image.PRIVATE_BB_CE1_IMAGE
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("PRIVATE_PROFILE_LABEL", f"apps-{RUN}-{ATTEMPT}"),
        ("PRIVATE_PROFILE_PLACEMENT", f"borduas-other-run-{RUN}-end"),
        ("PRIVATE_PROFILE_CPU", "8"),
        ("PRIVATE_PROFILE_MEMORY", "8Gi"),
        ("PRIVATE_PROFILE_STORAGE", "48Gi"),
        ("PRIVATE_PROFILE_EPHEMERAL", "false"),
        ("PRIVATE_PROFILE_OWNED_PROVIDERS", "[]"),
        ("GITHUB_REPOSITORY_ID", "1074974924"),
        (
            "GITHUB_WORKFLOW_REF",
            (
                "Borduas-Holdings/Blazing-Back/"
                ".github/workflows/sentry-owned-production.yml@refs/heads/main"
            ),
        ),
        ("GITHUB_REF", "refs/pull/1/merge"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("RUNNER_ENVIRONMENT", "self-hosted"),
        ("RUNNER_IMAGE", E71),
    ],
)
def test_foreign_role_resource_provider_or_source_refuses_before_reader(
    tmp_path, environment, key, value
):
    image.os.environ[key] = value
    with pytest.raises(ValueError):
        configure(tmp_path)
    assert environment == []


def test_substituted_generated_image_refuses_before_reader(tmp_path, environment):
    with pytest.raises(ValueError):
        configure(tmp_path, text=template().replace("e71bf4", "e71bf5"))
    assert environment == []


@pytest.mark.parametrize("profile", ["bb-ce1", "bb-dfc-e71"])
def test_actual_pool_dfc_shell_guard_admits_only_expected_profile_and_resources(
    profile, environment
):
    workflow = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())
    render = next(
        step["run"] for step in workflow["jobs"]["pool"]["steps"] if step.get("id") == "render"
    )
    start = render.index('case "$RUNNER_LABEL/$PLACEMENT_KEY" in')
    guard = render[start : render.index("\nesac", start) + len("\nesac")]
    env = dict(
        RUNNER_PRIVATE_PROFILE=profile,
        RUNNER_PUBLIC_PROFILE="",
        GITHUB_REPOSITORY="Borduas-Holdings/Blazing-Back",
        ORG="Borduas-Holdings",
        RUNNER_LABEL=f"dfc-images-{RUN}-{ATTEMPT}",
        GH_RUN_ID=RUN,
        GITHUB_RUN_ATTEMPT=ATTEMPT,
        PLACEMENT_KEY=f"borduas-dfc-images-run-{RUN}-end",
        POOL_SIZE="1",
        EPHEMERAL="true",
        CPU="4",
        MEMORY="16Gi",
        STORAGE="96Gi",
    )
    result = subprocess.run(
        ["/bin/bash", "-c", "set -euo pipefail\n" + textwrap.dedent(guard)],
        env=env,
        capture_output=True,
    )
    assert result.returncode == 0
    env["CPU"] = "8"
    refused = subprocess.run(
        ["/bin/bash", "-c", "set -euo pipefail\n" + textwrap.dedent(guard)],
        env=env,
        capture_output=True,
    )
    assert refused.returncode == 2
