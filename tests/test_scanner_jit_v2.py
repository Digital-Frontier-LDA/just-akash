"""Native returned-ID/ACK-before-delivery controls; only synthetic hosted endpoints."""

from __future__ import annotations

import base64
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from just_akash import scanner_jit_v2 as subject  # noqa: E402 -- isolated public-source harness
from just_akash.scanner_provenance import (  # noqa: E402 -- isolated public-source harness
    ScannerBinding,
    ScannerProvenanceHeld,
    producer_identity,
    record_sha256,
)

CONFIG = base64.b64encode(
    json.dumps(
        {
            ".runner": base64.b64encode(b'{"agentId":789}').decode(),
            ".credentials": base64.b64encode(b"PUBLIC_ONE_JOB_CREDENTIAL").decode(),
        }
    ).encode()
).decode()


def binding(**changes):
    value = dict(
        repository="Borduas-Holdings/Blazing-Back",
        repository_id=1071436278,
        workflow_path=".github/workflows/secret-scanning.yml",
        workflow_sha="a" * 40,
        native_head_sha="a" * 40,
        sdk_sha="b" * 40,
        event="workflow_dispatch",
        ref="refs/heads/main",
        run_id=77,
        run_attempt=2,
        sdk_round=1,
        producer_job_id=88,
        role="bb-native-trufflehog-v2",
        image=subject.PRIVATE_IMAGE,
        providers=[
            "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk",
            "akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z",
            "akash1z9nr23cgweu45g2jktfx95v7g2xp8qlsa3ys2x",
        ],
        resources=dict(cpu="4", memory="16Gi", storage="96Gi", count=1),
        runner_name="dfci-bb-th-77-a2-r1",
        runner_label="dfci-bb-th-77-a2-r1",
        akash_group="borduas-th-run-77-a2-r1-end",
        runner_group_id=37,
    )
    value.update(changes)
    return ScannerBinding(value)


class Recorder:
    """Synthetic exclusive immutable backend, explicitly not physical durability."""

    def __init__(self, fail=None):
        self.records = []
        self.phases = {}
        self.fail = fail

    def persist(self, record):
        op = record["operation_id"]
        phase = record["phase"]
        expected = {"NO_WIRE": None, "UNKNOWN": "NO_WIRE", "ACK": "UNKNOWN"}[phase]
        if self.phases.get(op) != expected:
            raise RuntimeError("PUBLIC_DUPLICATE_NO_REPLAY")
        self.phases[op] = phase
        self.records.append(copy.deepcopy(record))
        value = dict(
            schema="just-akash/scanner-durable-ack/v1",
            backend="github-actions-artifact/v4",
            operation_id=op,
            phase=phase,
            record_sha256=record_sha256(record),
            parent_record_sha256=record["parent_record_sha256"],
            artifact_id=100 + len(self.records),
            artifact_digest="sha256:" + "c" * 64,
            record_readback_sha256=record_sha256(record),
            producer=producer_identity(record["binding"]),
            exclusive=True,
            created=True,
        )
        if self.fail == phase:
            value["record_readback_sha256"] = "0" * 64
        return value


class API:
    def __init__(self, recorder):
        self.calls = []
        self.recorder = recorder
        self.result = dict(
            runner=dict(id=789, name="dfci-bb-th-77-a2-r1"), encoded_jit_config=CONFIG
        )
        self.group_change = None

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "POST":
            assert self.recorder.records[-1]["phase"] == "UNKNOWN"
            assert (
                path == subject.ROOT + "/runners/generate-jitconfig"
                and "/registration-token" not in path
            )
            return copy.deepcopy(self.result)
        if "/repositories?" in path:
            return dict(
                total_count=1,
                repositories=[
                    dict(id=1071436278, full_name="Borduas-Holdings/Blazing-Back", private=True)
                ]
                if path.endswith("page=1")
                else [],
            )
        row = dict(
            id=37,
            name=subject.RUNNER_GROUP_NAME,
            visibility="selected",
            allows_public_repositories=False,
            restricted_to_workflows=True,
            selected_workflows=[subject.ROOT_WORKFLOW],
        )
        if self.group_change:
            row.update(self.group_change)
        return row


def mint(store, api, **kwargs):
    return subject.mint_scanner_jit(
        binding(),
        "PUBLIC_HOSTED_ONLY_TOKEN",
        recorder=store,
        request=api,
        before_post=lambda: None,
        **kwargs,
    )


def test_actual_jit_returns_native_id_then_ack_before_exact_private_tenant_delivery():
    store = Recorder()
    api = API(store)
    result = mint(store, api)
    assert result.handoff.runner_id == 789
    assert [r["phase"] for r in store.records] == ["NO_WIRE", "UNKNOWN", "ACK"]
    assert CONFIG not in repr(result) and CONFIG not in json.dumps(store.records)
    assert all("PUBLIC_HOSTED_ONLY_TOKEN" not in json.dumps(r) for r in store.records)
    env = subject.tenant_environment(
        result, binding(), image=subject.PRIVATE_IMAGE, entrypoint_sha256=subject.ENTRYPOINT_SHA256
    )
    assert env == dict(
        RUNNER_JIT_CONFIG=CONFIG,
        EPHEMERAL="true",
        RUNNER_EXECUTION_USER="root",
        RUNNER_NAME="dfci-bb-th-77-a2-r1",
    )
    assert sum(x[0] == "POST" for x in api.calls) == 1


@pytest.mark.parametrize("phase", ["NO_WIRE", "UNKNOWN", "ACK"])
def test_missing_or_mismatched_storage_ack_cannot_deliver_or_replay(phase):
    store = Recorder(fail=phase)
    api = API(store)
    with pytest.raises((ScannerProvenanceHeld, subject.JitMintUnknown)):
        mint(store, api)
    count = sum(x[0] == "POST" for x in api.calls)
    assert count == (1 if phase == "ACK" else 0)
    with pytest.raises((ScannerProvenanceHeld, subject.JitMintUnknown)):
        mint(store, api)
    assert sum(x[0] == "POST" for x in api.calls) == count


def test_default_missing_backend_prevents_any_group_or_jit_request():
    calls = []
    with pytest.raises(ScannerProvenanceHeld):
        subject.mint_scanner_jit(
            binding(),
            "PUBLIC_HOSTED_ONLY_TOKEN",
            request=lambda *a: calls.append(a),
            before_post=lambda: None,
        )
    assert calls == []


@pytest.mark.parametrize("native_id", [None, False, 0, -1, "789"])
def test_required_server_returned_id_ambiguity_stays_unknown_without_delivery(native_id):
    store = Recorder()
    api = API(store)
    api.result["runner"]["id"] = native_id
    with pytest.raises(subject.JitMintUnknown):
        mint(store, api)
    assert [r["phase"] for r in store.records] == ["NO_WIRE", "UNKNOWN"]
    with pytest.raises((ScannerProvenanceHeld, subject.JitMintUnknown)):
        mint(store, api)
    assert sum(x[0] == "POST" for x in api.calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"visibility": "all"},
        {"name": "Default"},
        {"restricted_to_workflows": False},
        {"selected_workflows": ["PUBLIC_FOREIGN/REPO/.github/workflows/x.yml@refs/heads/main"]},
    ],
)
def test_actual_wrong_group_scope_cannot_reach_jit(change):
    store = Recorder()
    api = API(store)
    api.group_change = change
    with pytest.raises(subject.JitHold):
        mint(store, api)
    assert not any(x[0] == "POST" for x in api.calls)


@pytest.mark.parametrize(
    "image,entry",
    [
        (subject.PRIVATE_IMAGE.replace("e71bf4", "ce1b12"), subject.ENTRYPOINT_SHA256),
        (subject.PRIVATE_IMAGE, "0" * 64),
    ],
)
def test_private_e71_or_unsupported_entrypoint_never_gets_a_delivery(image, entry):
    store = Recorder()
    api = API(store)
    result = mint(store, api)
    with pytest.raises(subject.JitHold):
        subject.tenant_environment(result, binding(), image=image, entrypoint_sha256=entry)


def test_corrupt_encoded_jit_is_unknown_and_never_published_as_public_detail():
    store = Recorder()
    api = API(store)
    api.result["encoded_jit_config"] = "PUBLIC_NOT_BASE64"
    with pytest.raises(subject.JitMintUnknown):
        mint(store, api)
    assert [r["phase"] for r in store.records] == ["NO_WIRE", "UNKNOWN"]
    assert "PUBLIC_NOT_BASE64" not in json.dumps(store.records)
