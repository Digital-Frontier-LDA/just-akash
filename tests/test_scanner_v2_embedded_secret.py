"""Whole-deploy embedded-credential filtering; only synthetic closed fixtures."""

from __future__ import annotations

import base64
import json

import pytest
import test_scanner_v2_receipt_projection as prior


def transaction(secret, position):
    padding = "f" * (64 - len(secret))
    if position == "prefix":
        return secret + padding
    if position == "suffix":
        return padding + secret
    left = len(padding) // 2
    return padding[:left] + secret + padding[left:]


@pytest.mark.parametrize("length", [1, 4, 8, 16, 32, 48])
@pytest.mark.parametrize("position", ["prefix", "middle", "suffix"])
def test_complete_actual_reader_secret_never_survives_inside_hash(
    monkeypatch, tmp_path, length, position
):
    secret = ("0123456789abcdef" * 3)[:length]
    row, raw = prior.create(
        monkeypatch,
        tmp_path,
        dict(txHash=transaction(secret, position), transactionHash="e" * 64),
        password=secret,
    )
    assert row["create_transaction_identifiers"] == {"transactionHash": "e" * 64}
    # A one-character credential can coincide with public DSEQ/time/digest text;
    # the API echo must be absent and optional transaction metadata contains none.
    assert transaction(secret, position).encode() not in raw
    assert secret not in json.dumps(row["create_transaction_identifiers"])


def test_embedded_case_variant_is_withheld(monkeypatch, tmp_path):
    secret = "abcdef0123456789abcdef0123456789"
    echo = transaction(secret.upper(), "middle")
    row, raw = prior.create(
        monkeypatch, tmp_path, dict(txHash=echo, transactionHash="e" * 64), password=secret
    )
    assert row["create_transaction_identifiers"] == {"transactionHash": "e" * 64}
    assert secret.upper().encode() not in raw


def test_embedded_actual_console_key_is_withheld(monkeypatch, tmp_path):
    secret = "0123456789abcdef" * 2
    row, raw = prior.create(
        monkeypatch,
        tmp_path,
        dict(txHash=transaction(secret, "suffix"), transactionHash="e" * 64),
        api_key=secret,
    )
    assert row["create_transaction_identifiers"] == {"transactionHash": "e" * 64}
    assert secret.encode() not in raw


def test_embedded_in_memory_jit_config_is_withheld(monkeypatch, tmp_path):
    secret = "0123456789abcdef" * 2
    row, raw = prior.create(
        monkeypatch,
        tmp_path,
        dict(txHash=transaction(secret, "prefix"), transactionHash="e" * 64),
        config=secret,
    )
    assert row["create_transaction_identifiers"] == {"transactionHash": "e" * 64}
    assert secret.encode() not in raw


def test_embedded_decoded_jit_credential_is_withheld(monkeypatch, tmp_path):
    secret = "0123456789abcdef"
    payload = base64.b64encode(json.dumps(dict(token=secret)).encode()).decode()
    config = base64.b64encode(json.dumps({".credentials": payload}).encode()).decode()
    row, raw = prior.create(
        monkeypatch,
        tmp_path,
        dict(txHash=transaction(secret, "middle"), transactionHash="e" * 64),
        config=config,
    )
    assert row["create_transaction_identifiers"] == {"transactionHash": "e" * 64}
    assert secret.encode() not in raw


@pytest.mark.parametrize("key", ["a", "e", "0"])
def test_public_json_keys_and_empty_strings_do_not_mask_valid_metadata(monkeypatch, tmp_path, key):
    payload = base64.b64encode(
        json.dumps({key: "", "credential": "PUBLIC_NON_HEX_CREDENTIAL"}).encode()
    ).decode()
    config = base64.b64encode(json.dumps({".credentials": payload}).encode()).decode()
    digest = key * 64
    row, raw = prior.create(monkeypatch, tmp_path, dict(txHash=digest), config=config)
    assert row["create_transaction_identifiers"] == {"txHash": digest}
    assert b"PUBLIC_NON_HEX_CREDENTIAL" not in raw
