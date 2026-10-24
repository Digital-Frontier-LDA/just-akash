"""Offline controls execute actual new signed-version boundaries; no chain calls."""

import base64
import importlib.util
import sys
import types
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "just_akash"
PACKAGE = "scanner_creation_version_fixture"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(SOURCE)]
sys.modules[PACKAGE] = package
lookups = types.ModuleType(PACKAGE + ".owner_lookup")
lookups.Deadline = type("ForbiddenDeadline", (), {})
lookups.bounded_call = lambda *args, **kwargs: (_ for _ in ()).throw(
    AssertionError("unexpected network wrapper")
)
sys.modules[lookups.__name__] = lookups
spec = importlib.util.spec_from_file_location(PACKAGE + ".chain", SOURCE / "chain.py")
chain = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = chain
spec.loader.exec_module(chain)
VERSION = base64.b64encode(b"v" * 32).decode()
OTHER = base64.b64encode(b"x" * 32).decode()
OWNER = "akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z"
GROUP = "borduas-th-run-42-a2-r1-end"


def proof_input(version=VERSION, owner=OWNER):
    message = {
        "@type": chain._CREATE_DEPLOYMENT_TYPE,
        "id": {"owner": owner, "dseq": "300"},
        "groups": [{"name": GROUP}],
        "hash": version,
    }
    tx = {
        "body": {"messages": [message]},
        "signatures": [base64.b64encode(b"s" * 64).decode()],
        "auth_info": {"signer_infos": [{}]},
    }
    response = {"code": 0, "txhash": "A" * 64}
    population = {
        "txs": [tx],
        "responses": [response],
        "fingerprints": ["f"],
        "raw_hashes": ["A" * 64],
        "response_population": ["r"],
    }
    return message, population


def signed(monkeypatch, population, expected=VERSION):
    monkeypatch.setattr(chain, "_indexed_transaction_population", lambda *a, **k: population)
    block = {key: population[key] for key in ["fingerprints", "raw_hashes", "response_population"]}
    return chain._signed_creation_population(
        lambda *a, **k: None, {}, 100, OWNER, "300", block, expected_version=expected
    )


def test_exact_signed_version_positive_and_legacy_default_preserved(monkeypatch):
    _, population = proof_input()
    result = signed(monkeypatch, population)
    assert result["snapshot"] == (("1", GROUP),) and result["txhash"] == "A" * 64
    # Old API intentionally does not claim a version; its old group-only scope remains.
    message, population = proof_input(OTHER)
    assert signed(monkeypatch, population, expected=None)["snapshot"] == (("1", GROUP),)


@pytest.mark.parametrize(
    "kind",
    [
        "different_version",
        "missing_version",
        "noncanonical_version",
        "wrong_owner",
        "duplicate_create",
        "failed_tx",
        "legacy_version_alias",
    ],
)
def test_signed_lookalikes_cannot_prove_expected_creation(monkeypatch, kind):
    message, population = proof_input()
    if kind == "different_version":
        message["hash"] = OTHER
    if kind == "missing_version":
        message.pop("hash")
    if kind == "noncanonical_version":
        message["hash"] = VERSION.rstrip("=")
    if kind == "wrong_owner":
        message["id"]["owner"] = "foreign"
    if kind == "duplicate_create":
        population["txs"][0]["body"]["messages"].append(dict(message))
    if kind == "legacy_version_alias":
        message["version"] = message.pop("hash")
    if kind == "failed_tx":
        population["responses"][0]["code"] = 1
    with pytest.raises(chain.ChainResponseError):
        signed(monkeypatch, population)


def deployment(version=VERSION):
    return {
        "deployment": {
            "id": {"owner": OWNER, "dseq": "300"},
            "created_at": "100",
            "hash": version,
        },
        "groups": [
            {"id": {"owner": OWNER, "dseq": "300", "gseq": "1"}, "group_spec": {"name": GROUP}}
        ],
    }


def test_native_state_version_must_match_signed_expected_version():
    assert chain._deployment_snapshot_at_height(
        deployment(), OWNER, "300", expected_version=VERSION
    ) == ((("1", GROUP),), 100)
    assert (
        chain._deployment_snapshot_at_height(
            deployment(OTHER), OWNER, "300", expected_version=VERSION
        )
        is None
    )
    assert chain._deployment_snapshot_at_height(deployment(OTHER), OWNER, "300") == (
        (("1", GROUP),),
        100,
    )


@pytest.mark.parametrize(
    "value", [None, "", False, "a" * 64, base64.b64encode(b"v" * 31).decode(), VERSION.rstrip("=")]
)
def test_unsupported_manifest_version_refused_before_source_reads(monkeypatch, value):
    calls = []
    monkeypatch.setattr(chain, "_owner_close_evidence", lambda *a, **k: calls.append((a, k)))
    assert (
        chain.creation_version_population_evidence(
            OWNER, "300", [{"gseq": 1, "name": GROUP}], value
        )
        is None
    )
    assert calls == []


def test_public_version_entry_binds_existing_independent_population_proof(monkeypatch):
    calls = []
    monkeypatch.setattr(
        chain,
        "_owner_close_evidence",
        lambda *a, **k: calls.append((a, k)) or {"public": "synthetic"},
    )
    assert chain.creation_version_population_evidence(
        OWNER, "300", [{"gseq": 1, "name": GROUP}], VERSION
    ) == {"public": "synthetic"}
    assert calls[0][0] == (OWNER, "300", GROUP)
    assert calls[0][1]["expected_version"] == VERSION
    assert calls[0][1]["expected_population"] == (("1", GROUP),)
    assert calls[0][1]["sources"] is chain.OWNER_CORROBORATION_SOURCES_V2
    assert calls[0][1]["reader"] is chain._lcd_get


def test_legacy_state_version_alias_cannot_replace_native_v1beta4_hash():
    data = deployment()
    data["deployment"]["version"] = data["deployment"].pop("hash")
    assert (
        chain._deployment_snapshot_at_height(data, OWNER, "300", expected_version=VERSION) is None
    )
