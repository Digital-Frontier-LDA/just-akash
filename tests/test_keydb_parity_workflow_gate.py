"""Actual new first-step shell must admit only its own root before credentials."""

import os
import subprocess
from pathlib import Path

import yaml

SOURCE = Path(
    os.environ.get(
        "KEYDB_TEST_GATE_SOURCE",
        str(Path(__file__).resolve().parents[1] / ".github/workflows/runner-pool.yml"),
    )
)


def gate(root):
    workflow = yaml.safe_load(SOURCE.read_bytes())
    script = workflow["jobs"]["keydb_parity_v1"]["steps"][0]["run"]
    env = dict(
        PATH=os.environ["PATH"],
        SDK_REPOSITORY="Digital-Frontier-LDA/just-akash",
        SDK_SHA="a" * 40,
        GITHUB_REPOSITORY="Borduas-Holdings/blazing",
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_REF="refs/heads/main",
        GITHUB_WORKFLOW_REF=root,
        GITHUB_WORKFLOW_SHA="b" * 40,
        GITHUB_SHA="b" * 40,
        GITHUB_JOB="keydb_parity_v1",
        RUNNER_ENVIRONMENT="github-hosted",
    )
    return subprocess.run(
        [os.environ.get("KEYDB_TEST_BASH", "bash"), "-c", script],
        env=env,
        capture_output=True,
        timeout=3,
        check=False,
    ).returncode


def test_own_KeyDB_root_reaches_admitted_job():
    assert (
        gate("Borduas-Holdings/blazing/.github/workflows/keydb-modules-parity.yml@refs/heads/main")
        == 0
    )


def test_foreign_scanner_root_is_refused_before_credentials():
    assert (
        gate("Borduas-Holdings/blazing/.github/workflows/secret-scanning.yml@refs/heads/main") != 0
    )


def test_all_required_returned_id_and_ack_outputs_route_new_job():
    workflow = yaml.safe_load(SOURCE.read_bytes())
    call = workflow.get("on", workflow.get(True))["workflow_call"]
    for name in [
        "returned_runner_id",
        "jit_ack_record_sha256",
        "deployment_ack_record_sha256",
        "lease_ack_record_sha256",
        "cleanup_qualified",
    ]:
        assert (
            "inputs.runner-private-profile == 'blazing-keydb-modules-parity-v1' && "
            "jobs.keydb_parity_v1.outputs."
        ) + name in call["outputs"][name]["value"]
