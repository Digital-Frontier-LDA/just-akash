"""Real successful controller entry output mapping; synthetic runtime result only."""

import json

from test_keydb_qualification_positive_fence import data

from just_akash import keydb_qualification_fenced_controller as controller
from just_akash.keydb_qualification_provenance import KeyDBQualificationBinding


def test_successful_main_outputs_match_real_qualification_binding_and_known_ack_fields(
    monkeypatch, tmp_path
):
    binding = KeyDBQualificationBinding(data()["producer"])
    output = tmp_path / "PUBLIC_GITHUB_OUTPUT"
    for key, value in {
        "GITHUB_RUN_ID": str(binding.run_id),
        "GITHUB_RUN_ATTEMPT": str(binding.run_attempt),
        "KEYDB_QUALIFICATION_SDK_ROUND": str(binding.sdk_round),
        "GITHUB_OUTPUT": str(output),
    }.items():
        monkeypatch.setenv(key, value)
    result = {
        "dseq": "1234567",
        "owner": binding.providers[0],
        "returned_runner_id": 991,
        "runner_targets": ["self-hosted", "linux", "akash", binding.runner_label],
        "jit_ack_record_sha256": "1" * 64,
        "deployment_ack_record_sha256": "2" * 64,
        "lease_ack_record_sha256": "3" * 64,
    }
    monkeypatch.setattr(controller, "provision", lambda _env: result)
    monkeypatch.setattr(controller.sys, "argv", ["keydb-qualification-controller"])
    controller.main()
    emitted = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert emitted["deployment_group"] == binding.akash_group
    assert json.loads(emitted["runner-targets"]) == result["runner_targets"]
    for key in (
        "dseq",
        "jit_ack_record_sha256",
        "deployment_ack_record_sha256",
        "lease_ack_record_sha256",
    ):
        assert emitted[key] == result[key]
    assert emitted["wallet_address"] == result["owner"]
    assert emitted["returned_runner_id"] == "991"
    assert emitted["provision_healthy"] == "true"
    assert emitted["cleanup_qualified"] == "false"
