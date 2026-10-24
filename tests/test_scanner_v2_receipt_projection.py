"""Actual scanner create/local-writer confidentiality with no API effects."""

from __future__ import annotations

import base64
import copy
import json
import sys

import pytest
import test_scanner_lease_provenance as original
import yaml

ALIASES = (
    "txhash",
    "txHash",
    "tx_hash",
    "transactionHash",
    "transaction_hash",
    "transactionId",
    "transaction_id",
)


def create(
    monkeypatch,
    tmp_path,
    transaction,
    *,
    password="SYNTHETIC_PULL_SECRET",
    config="SYNTHETIC_JIT_SECRET",
    api_key="SYNTHETIC_API_KEY",
):
    old, recorder, content = original.fixture()
    provenance = original.lease.ScannerLeaseProvenance(
        binding=original.binding(),
        returned_runner_id=191,
        jit_ack_record=recorder.records[-1],
        jit_ack=original.ack(recorder.records[-1]),
        encoded_jit_config=config,
        recorder=recorder,
        admission=lambda: None,
    )
    doc = yaml.safe_load(content)
    doc["services"]["runner"]["credentials"]["password"] = password
    doc["services"]["runner"]["env"][0] = "RUNNER_JIT_CONFIG=" + config
    content = yaml.safe_dump(doc, sort_keys=False)

    class Client(original.Client):
        def create_deployment(self, sdl_content, deposit):
            self.response = super().create_deployment(sdl_content, deposit)
            self.response.update(copy.deepcopy(transaction))
            self.expected = copy.deepcopy(self.response)
            return self.response

    client = Client(recorder.events)
    client.api_key = api_key
    deploy, Finished = original.actual_deploy(monkeypatch, provenance, content, client)
    monkeypatch.setattr(
        sys.modules["just_akash.wallet_pool"], "configured_api_keys", lambda: [api_key]
    )
    recorder.events.clear()
    with pytest.raises(Finished):
        deploy(
            "synthetic.yaml",
            preferred_providers=sorted(original.protocol.PROVIDERS),
            backup_providers=[],
            deposit=6,
            receipt_path=str(tmp_path / "local.json"),
            receipt_operation_id=provenance.operation.operation_id,
            scanner_provenance=provenance,
        )
    raw = (tmp_path / "local.json").read_bytes()
    row = json.loads(raw)
    assert row["dseq"] == "300" and row["state"] == "create_response_received"
    assert (
        client.response == client.expected and provenance._manifest == client.expected["manifest"]
    )
    assert client.creates == 1 and client.closes == 0
    assert recorder.events == [
        ("lease", "NO_WIRE"),
        ("lease", "UNKNOWN"),
        ("wire", "deployment"),
        ("lease", "ACK"),
    ]
    return row, raw


@pytest.mark.parametrize("alias", ALIASES)
def test_all_legacy_transaction_aliases_keep_valid_public_hash(monkeypatch, tmp_path, alias):
    value = "A1" * 32
    row, raw = create(monkeypatch, tmp_path, {alias: value})
    assert row["create_transaction_identifiers"] == {alias: value}
    assert b"SYNTHETIC_JIT_SECRET" not in raw and b"SYNTHETIC_PULL_SECRET" not in raw


@pytest.mark.parametrize("alias", ALIASES)
def test_all_legacy_aliases_drop_credential_echo_without_losing_dseq(monkeypatch, tmp_path, alias):
    row, raw = create(
        monkeypatch, tmp_path, {alias: "SYNTHETIC_JIT_SECRET / SYNTHETIC_PULL_SECRET"}
    )
    assert row["create_transaction_identifiers"] == {}
    assert b"SYNTHETIC_JIT_SECRET" not in raw and b"SYNTHETIC_PULL_SECRET" not in raw


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        64,
        {},
        [],
        {"nested": "SYNTHETIC_PULL_SECRET"},
        ["SYNTHETIC_JIT_SECRET"],
        "0x" + "a" * 64,
        "a" * 63,
        "a" * 65,
    ],
)
def test_bad_optional_transaction_metadata_never_erases_returned_candidate(
    monkeypatch, tmp_path, value
):
    row, raw = create(
        monkeypatch,
        tmp_path,
        dict(
            txHash=value,
            data=dict(transaction_id="SYNTHETIC_PULL_SECRET"),
            transaction=dict(hash="SYNTHETIC_JIT_SECRET"),
        ),
    )
    assert row["create_transaction_identifiers"] == {}
    assert b"SYNTHETIC_JIT_SECRET" not in raw and b"SYNTHETIC_PULL_SECRET" not in raw


@pytest.mark.parametrize(
    "case",
    [
        "reader",
        "reader-case-variant",
        "encoded-config",
        "decoded-config-file",
        "escaped-config-token",
    ],
)
def test_hex_shaped_actual_credentials_cannot_pass_hash_filter(monkeypatch, tmp_path, case):
    secret = "b" * 64
    password = secret if case.startswith("reader") else "SYNTHETIC_PULL_SECRET"
    config = secret if case == "encoded-config" else "SYNTHETIC_JIT_SECRET"
    if case in ("decoded-config-file", "escaped-config-token"):
        payload = json.dumps(dict(token=secret)).encode()
        if case == "escaped-config-token":
            payload = b'{"token":"' + b"\\u0062" * 64 + b'"}'
        inner = base64.b64encode(payload).decode()
        config = base64.b64encode(json.dumps({".credentials": inner}).encode()).decode()
    echo = secret.upper() if case == "reader-case-variant" else secret
    row, raw = create(
        monkeypatch,
        tmp_path,
        dict(txHash=echo, transactionHash="a" * 64),
        password=password,
        config=config,
    )
    assert row["create_transaction_identifiers"] == {"transactionHash": "a" * 64}
    assert echo.encode() not in raw and config.encode() not in raw and password.encode() not in raw


def test_hex_shaped_selected_console_key_is_not_local_transaction_metadata(monkeypatch, tmp_path):
    secret = "c" * 64
    row, raw = create(
        monkeypatch, tmp_path, dict(txHash=secret, transactionHash="a" * 64), api_key=secret
    )
    assert row["create_transaction_identifiers"] == {"transactionHash": "a" * 64}
    assert secret.encode() not in raw
