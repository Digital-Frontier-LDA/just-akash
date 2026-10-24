# ruff: noqa: E402 -- sibling PUBLIC fixture imports only
"""Bounded synthetic Console reads and real SDK pre-create hook; no native effects."""

import copy
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from just_akash import scanner_create_observation as observe
from just_akash import scanner_lease_provenance as lease
from just_akash import scanner_provenance as protocol

spec = importlib.util.spec_from_file_location(
    "scanner_core_oracles", SOURCE / "tests/test_scanner_lease_provenance.py"
)
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)
OWNER = old.OWNER
VERSION = "1s4ifNc1TSMdDSxnRkJJeB3jidGjyhdDisURhZLxf8M="
OTHER = "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg="


def row(identifier="100", version=VERSION, state="active", owner=OWNER):
    return {
        "deployment": {"id": {"owner": owner, "dseq": identifier}, "hash": version, "state": state}
    }


def fixture_reader(monkeypatch, active=None, closed=None, mutate=None, events=None):
    active = active if active is not None else [row()]
    closed = closed if closed is not None else []
    calls = []

    def get(self, method, path, data=None):
        calls.append((method, path))
        if events is not None:
            events.append(("GET", "baseline"))
        state = "active" if "state=active" in path else "closed"
        batch = copy.deepcopy(active if state == "active" else closed)
        doc = {
            "data": {
                "deployments": batch,
                "pagination": {"total": len(batch), "skip": 0, "limit": 100, "hasMore": False},
            }
        }
        if mutate:
            mutate(doc, len(calls), state)
        return doc

    monkeypatch.setattr(observe.ConsoleReadOnly, "_request", get)
    return calls


def test_complete_active_and_closed_baseline_is_bounded_and_stable(monkeypatch):
    calls = fixture_reader(monkeypatch, [row("200")], [row("100", state="closed")])
    result = observe.complete_snapshot(SimpleNamespace(api_key="SYNTHETIC_API_KEY"), OWNER)
    assert result["baseline"]["dseqs"] == ["100", "200"]
    assert [v[0] for v in calls] == ["GET"] * 4
    assert all("/v1/deployments?state=" in path and "limit=100" in path for _, path in calls)


@pytest.mark.parametrize(
    "defect",
    [
        "partial",
        "total_bool",
        "wrong_owner",
        "duplicate",
        "wrong_state",
        "missing_version",
        "noncanonical_version",
        "shifted_second_pass",
    ],
)
def test_incomplete_or_substituted_baseline_never_becomes_complete(monkeypatch, defect):
    def change(doc, count, state):
        if state != "active":
            return
        data = doc["data"]
        entry = data["deployments"][0]
        if defect == "partial":
            data["pagination"].update(total=2, hasMore=True)
        if defect == "total_bool":
            data["pagination"]["total"] = True
        if defect == "wrong_owner":
            entry["deployment"]["id"]["owner"] = "foreign"
        if defect == "duplicate":
            data["deployments"] *= 2
            data["pagination"]["total"] = 2
        if defect == "wrong_state":
            entry["deployment"]["state"] = "closed"
        if defect == "missing_version":
            entry["deployment"].pop("hash")
        if defect == "noncanonical_version":
            entry["deployment"]["hash"] = VERSION.rstrip("=")
        if defect == "shifted_second_pass" and count > 2:
            entry["deployment"]["id"]["dseq"] = "999"

    fixture_reader(monkeypatch, mutate=change)
    with pytest.raises(protocol.ScannerProvenanceHeld):
        observe.complete_snapshot(SimpleNamespace(api_key="SYNTHETIC_API_KEY"), OWNER)


@pytest.mark.parametrize(
    "new_rows,expected",
    [
        ([], None),
        ([("300", VERSION)], "300"),
        ([("300", OTHER)], None),
        ([("300", VERSION), ("301", VERSION)], None),
    ],
)
def test_candidate_zero_one_wrongversion_multiple_stays_data(monkeypatch, new_rows, expected):
    fixture_reader(monkeypatch, [row()])
    before = observe.complete_snapshot(SimpleNamespace(api_key="SYNTHETIC_API_KEY"), OWNER)[
        "baseline"
    ]
    fixture_reader(monkeypatch, [row()] + [row(i, v) for i, v in new_rows])
    after = observe.complete_snapshot(SimpleNamespace(api_key="SYNTHETIC_API_KEY"), OWNER)
    assert observe.unique_new_candidate(before, after, OWNER, VERSION) == expected


def reconcilable(monkeypatch, events=None, fail_capture=False):
    initial, r, raw = old.fixture(recorder=old.Recorder(events))
    doc = yaml.safe_load(raw)
    doc["services"]["runner"]["expose"] = [
        {"port": 65535, "as": 65535, "proto": "tcp", "to": [{"global": True}]}
    ]
    raw = yaml.safe_dump(doc, sort_keys=False)

    def version(content, path, digest):
        assert content == raw
        r.events.append(("helper", "official-version"))
        if fail_capture:
            raise protocol.ScannerProvenanceHeld("synthetic unsupported version")
        return VERSION

    monkeypatch.setattr(observe, "official_version_held_helper", version)
    fixture_reader(monkeypatch, events=r.events)
    capture = observe.PrecreateCapture("hosted-verified-helper", "f" * 64)
    jit = r.records[-1]
    p = lease.ScannerLeaseProvenance(
        binding=initial.binding,
        returned_runner_id=191,
        jit_ack_record=jit,
        jit_ack=old.ack(jit),
        encoded_jit_config="SYNTHETIC_JIT_SECRET",
        recorder=r,
        admission=lambda: None,
        reconciliation=capture,
    )
    return p, r, raw


def test_actual_deploy_captures_version_and_baseline_before_durable_intent_and_wire(
    monkeypatch, tmp_path
):
    p, r, raw = reconcilable(monkeypatch)
    client = old.Client(r.events)
    function, Finished = old.actual_deploy(monkeypatch, p, raw, client)
    r.events.clear()
    with pytest.raises(Finished):
        function(
            "synthetic.yaml",
            preferred_providers=sorted(protocol.PROVIDERS),
            backup_providers=[],
            deposit=6,
            receipt_path=str(tmp_path / "local.json"),
            receipt_operation_id=p.operation.operation_id,
            scanner_provenance=p,
        )
    assert r.events == [("helper", "official-version")] + [("GET", "baseline")] * 4 + [
        ("lease_reconcile", "NO_WIRE"),
        ("lease_reconcile", "UNKNOWN"),
        ("wire", "deployment"),
        ("lease_reconcile", "ACK"),
    ]
    details = p.operation.public_record["details"]
    assert (
        details["precreate_baseline"]["dseqs"] == ["100"]
        and details["expected_manifest_version"] == VERSION
    )
    assert details["payload_profile"] == "scanner-private-e71-tcp65535/v1" and client.creates == 1


def test_unsupported_official_version_blocks_real_deploy_before_unknown_or_wire(
    monkeypatch, tmp_path
):
    p, r, raw = reconcilable(monkeypatch, fail_capture=True)
    client = old.Client(r.events)
    function, _ = old.actual_deploy(monkeypatch, p, raw, client)
    with pytest.raises(protocol.ScannerProvenanceHeld):
        function(
            "synthetic.yaml",
            preferred_providers=sorted(protocol.PROVIDERS),
            backup_providers=[],
            deposit=6,
            receipt_path=str(tmp_path / "local.json"),
            receipt_operation_id=p.operation.operation_id,
            scanner_provenance=p,
        )
    assert client.creates == 0 and not any(v["kind"] == "lease_reconcile" for v in r.records)


def test_baseline_omission_and_legacy_unknown_cannot_be_upgraded(monkeypatch, tmp_path):
    p, r, raw = reconcilable(monkeypatch)
    p.prepare(
        old.prepared(p, raw, tmp_path), raw, client=SimpleNamespace(api_key="SYNTHETIC_API_KEY")
    )
    unknown_details = dict(p._details)
    del unknown_details["precreate_baseline"]
    with pytest.raises(protocol.ScannerProvenanceHeld):
        p.operation.before_wire(unknown_details)
    original, _, _ = old.fixture()
    with pytest.raises(protocol.ScannerProvenanceHeld):
        observe.observe_creation_identity_data(
            SimpleNamespace(), original.binding, {"kind": "lease"}, None, {}, "SYNTHETIC_TOKEN"
        )


def test_original_no_endpoint_validator_stays_strict_and_new_validator_exact(monkeypatch):
    p, r, raw = reconcilable(monkeypatch)
    with pytest.raises(protocol.ScannerProvenanceHeld):
        lease.public_sdl_request(raw, p.binding, "SYNTHETIC_JIT_SECRET")
    assert (
        lease.public_sdl_request_reconcilable(raw, p.binding, "SYNTHETIC_JIT_SECRET")["services"][
            "runner"
        ]["expose"][0]["port"]
        == 65535
    )
    with pytest.raises(protocol.ScannerProvenanceHeld):
        lease.public_sdl_request_reconcilable(
            raw.replace("65535", "65534"), p.binding, "SYNTHETIC_JIT_SECRET"
        )


def test_positive_observation_requires_native_and_exact_signed_chain_proof(monkeypatch, tmp_path):
    import types

    from just_akash import chain

    p, r, raw = reconcilable(monkeypatch)
    p.prepare(
        old.prepared(p, raw, tmp_path), raw, client=SimpleNamespace(api_key="SYNTHETIC_API_KEY")
    )
    unknown_ack = p.operation.before_wire(p._details)
    unknown = p.operation.public_record
    before = protocol.record_sha256(unknown)
    fixture_reader(monkeypatch, [row(), row("300")])
    calls = []
    controller = types.ModuleType("just_akash.scanner_v2_controller")
    controller.admit_native = lambda b, e, t: calls.append("native")
    monkeypatch.setitem(sys.modules, controller.__name__, controller)
    proof = {
        "evidence_version": 3,
        "owner": OWNER,
        "dseq": "300",
        "groups": [{"gseq": 1, "name": p.binding.akash_group}],
        "chain_id": "akashnet-2",
        "creation_manifest_version": VERSION,
        "current_manifest_version": VERSION,
        "registry_digest": chain.OWNER_CORROBORATION_REGISTRY_SHA256,
    }

    def corroborate(owner, identifier, population, version):
        assert (owner, identifier, population, version) == (
            OWNER,
            "300",
            [{"gseq": 1, "name": p.binding.akash_group}],
            VERSION,
        )
        calls.append("two-source-signed-proof")
        return proof

    monkeypatch.setattr(chain, "creation_version_population_evidence", corroborate)
    result = observe.observe_creation_identity_data(
        SimpleNamespace(api_key="SYNTHETIC_API_KEY"),
        p.binding,
        unknown,
        unknown_ack,
        {},
        "SYNTHETIC_TOKEN",
    )
    assert result["dseq"] == "300" and result["original_unknown_record_sha256"] == before
    assert calls == ["native", "native", "two-source-signed-proof", "native"]
    assert (
        p.operation.public_record == unknown
        and result["retry_authorized"] is False
        and result["cleanup_qualified"] is False
    )
    proof["owner"] = "foreign"
    with pytest.raises(protocol.ScannerProvenanceHeld):
        observe.observe_creation_identity_data(
            SimpleNamespace(api_key="SYNTHETIC_API_KEY"),
            p.binding,
            unknown,
            unknown_ack,
            {},
            "SYNTHETIC_TOKEN",
        )
    assert protocol.record_sha256(p.operation.public_record) == before


def test_missing_signed_proof_does_not_relabel_unknown(monkeypatch, tmp_path):
    import types

    from just_akash import chain

    p, r, raw = reconcilable(monkeypatch)
    p.prepare(
        old.prepared(p, raw, tmp_path), raw, client=SimpleNamespace(api_key="SYNTHETIC_API_KEY")
    )
    unknown_ack = p.operation.before_wire(p._details)
    unknown = p.operation.public_record
    controller = types.ModuleType("just_akash.scanner_v2_controller")
    controller.admit_native = lambda *a: None
    monkeypatch.setitem(sys.modules, controller.__name__, controller)
    monkeypatch.setattr(chain, "creation_version_population_evidence", lambda *a: None)
    fixture_reader(monkeypatch, [row(), row("300")])
    assert (
        observe.observe_creation_identity_data(
            SimpleNamespace(api_key="SYNTHETIC_API_KEY"),
            p.binding,
            unknown,
            unknown_ack,
            {},
            "SYNTHETIC_TOKEN",
        )
        is None
    )
    assert p.operation.public_record["phase"] == "UNKNOWN"


def test_oversized_baseline_is_unsupported_never_truncated(monkeypatch):
    def oversized(doc, count, state):
        doc["data"]["pagination"]["total"] = 1001

    calls = fixture_reader(monkeypatch, mutate=oversized)
    with pytest.raises(protocol.ScannerProvenanceHeld):
        observe.complete_snapshot(SimpleNamespace(api_key="SYNTHETIC_API_KEY"), OWNER)
    assert len(calls) == 1


@pytest.mark.parametrize("where", ["open", "read"])
def test_actual_readonly_transport_confidentiality_holds_on_http_exception(monkeypatch, where):
    import http.client
    import time

    class Response:
        def read(self, count):
            raise http.client.BadStatusLine("SYNTHETIC_READER_SECRET")

        def __exit__(self, *args):
            return False

    seen = []

    class Opener:
        def open(self, request, timeout):
            seen.append((request.get_method(), timeout))
            if where == "open":
                raise http.client.BadStatusLine("SYNTHETIC_READER_SECRET")
            return Response()

    monkeypatch.setattr(observe.urllib.request, "build_opener", lambda handler: Opener())
    reader = observe.ConsoleReadOnly(
        SimpleNamespace(api_key="SYNTHETIC_API_KEY"), time.monotonic() + 30
    )
    with pytest.raises(protocol.ScannerProvenanceHeld) as error:
        reader._request("GET", "/v1/deployments?state=active&skip=0&limit=100")
    assert seen[0][0] == "GET" and 0 < seen[0][1] <= 5
    assert "SYNTHETIC_READER_SECRET" not in str(error.value) and error.value.__context__ is None
    with pytest.raises(protocol.ScannerProvenanceHeld):
        reader._request("POST", "/v1/deployments", {})
    assert len(seen) == 1


def test_new_contract_cannot_escape_legacy_create_phase_slot(monkeypatch, tmp_path):
    p, r, raw = reconcilable(monkeypatch)
    old_operation = protocol.ScannerOperation(p.binding, "lease", r)
    assert p.operation.operation_id == old_operation.operation_id

    class ExistingPhase(old.Recorder):
        def persist(self, record):
            value = super().persist(record)
            value["created"] = False
            return value

    p.operation = protocol.ScannerOperation(p.binding, "lease_reconcile", ExistingPhase())
    with pytest.raises(protocol.ScannerProvenanceHeld):
        p.prepare(
            old.prepared(p, raw, tmp_path),
            raw,
            client=SimpleNamespace(api_key="SYNTHETIC_API_KEY"),
        )
    assert p.operation._state == "HELD"


def test_drip_read_deadline_discards_late_result_without_starting_another_read(monkeypatch):
    import threading
    import time

    from just_akash.owner_lookup import Deadline

    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    calls = []
    monkeypatch.setattr(
        observe, "Deadline", lambda *a, **k: Deadline(0.02, ceiling_at=time.time() + 0.02)
    )

    def slow(self, method, path, data=None):
        calls.append(path)
        started.set()
        release.wait(1)
        finished.set()
        return {
            "data": {
                "deployments": [],
                "pagination": {"skip": 0, "limit": 100, "total": 0, "hasMore": False},
            }
        }

    monkeypatch.setattr(observe.ConsoleReadOnly, "_request", slow)
    try:
        before = time.monotonic()
        with pytest.raises(protocol.ScannerProvenanceHeld):
            observe.complete_snapshot(SimpleNamespace(api_key="SYNTHETIC_API_KEY"), OWNER)
        assert started.is_set() and time.monotonic() - before < 0.5 and len(calls) == 1
    finally:
        release.set()
    assert finished.wait(1) and len(calls) == 1
