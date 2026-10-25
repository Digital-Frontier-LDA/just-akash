"""Exact public producer-input and group fixtures; no real credentials."""

import json

from just_akash import keydb_qualification_controller as controller
from just_akash import keydb_qualification_jit as jit
from just_akash import keydb_qualification_provenance as protocol


def environment():
    exact = {
        "PUBLIC_PROFILE": "",
        "IMAGE": "",
        "REGISTRY_HOST": "https://index.docker.io/v1/",
        "REGISTRY_USERNAME": "jobordu",
        "REGISTRY_SOPS": "true",
        "NATIVE_PULL_READER": "false",
        "NATIVE_REPOSITORY_SCOPE": "false",
        "CREDENTIALS_SOPS": "false",
        "EPHEMERAL": "true",
        "POOL_SIZE": "1",
        "MIN_POOL_SIZE": "1",
        "CPU": "4",
        "MEMORY": "16Gi",
        "STORAGE": "96Gi",
        "SELECT": "cheapest",
        "ORG": "Borduas-Holdings",
        "DIRECT_PASSWORD_PRESENT": "false",
    }
    env = {"KEYDB_QUALIFICATION_" + k: v for k, v in exact.items()}
    env.update(
        RUNNER_PRIVATE_PROFILE=controller.PROFILE,
        GITHUB_REPOSITORY="Borduas-Holdings/blazing",
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_REF="refs/heads/codex/keydb-parity-owned-d8ab-20261024",
        RUNNER_ENVIRONMENT="github-hosted",
        GITHUB_JOB="keydb_qualification_v1",
        GITHUB_SHA="a" * 40,
        GITHUB_WORKFLOW_SHA="a" * 40,
        GITHUB_WORKFLOW_REF="Borduas-Holdings/blazing/.github/workflows/keydb-modules-parity.yml@refs/heads/codex/keydb-parity-owned-d8ab-20261024",
        GITHUB_RUN_ID="42",
        GITHUB_RUN_ATTEMPT="2",
        KEYDB_QUALIFICATION_SDK_ROUND="1",
        KEYDB_QUALIFICATION_SDK_SHA="b" * 40,
        KEYDB_QUALIFICATION_PRODUCER_JOB_ID="77",
        KEYDB_QUALIFICATION_RUNNER_GROUP_ID="99",
    )
    for k in ("PROVIDERS", "OWNED_PROVIDERS"):
        env["KEYDB_QUALIFICATION_" + k] = json.dumps(sorted(protocol.PROVIDERS))
    return env


def group_fixture(change=None):
    calls = []

    def request(method, path, token, *args):
        calls.append((method, path))
        if "/repositories?" in path:
            rows = (
                []
                if path.endswith("page=2")
                else [dict(id=1074974924, full_name="Borduas-Holdings/blazing", private=True)]
            )
            if change == "BBrepository" and rows:
                rows[0].update(id=1071436278, full_name="Borduas-Holdings/Blazing-Back")
            return dict(total_count=1, repositories=rows)
        row = dict(
            id=99,
            name=jit.RUNNER_GROUP_NAME,
            visibility="selected",
            allows_public_repositories=False,
            restricted_to_workflows=True,
            selected_workflows=[jit.ROOT_WORKFLOW],
        )
        if change == "BBgroup":
            row["name"] = "bb-native-trufflehog-v2"
        if change == "wrongWorkflow":
            row["selected_workflows"] = [
                "Borduas-Holdings/blazing/.github/workflows/owned-keydb-podman.yml@refs/heads/codex/keydb-parity-owned-d8ab-20261024"
            ]
        return row

    return request, calls


def ack(record):
    return dict(
        schema="just-akash/keydb-qualification-durable-ack/v1",
        backend="github-actions-artifact/v4",
        operation_id=record["operation_id"],
        phase=record["phase"],
        parent_record_sha256=record["parent_record_sha256"],
        record_sha256=protocol.record_sha256(record),
        record_readback_sha256=protocol.record_sha256(record),
        artifact_id=881,
        artifact_digest="sha256:" + "e" * 64,
        producer=protocol.producer_identity(record["binding"]),
        exclusive=True,
        created=True,
    )
