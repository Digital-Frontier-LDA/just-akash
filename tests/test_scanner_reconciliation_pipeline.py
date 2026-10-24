"""Actual new producer/SDK boundary controls; public synthetic transport only."""

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_scanner_create_observation import (  # noqa: E402
    OWNER,
    VERSION,
    fixture_reader,
    old,
    reconcilable,
    row,
)
from test_scanner_v2_controller import environment  # noqa: E402

from just_akash import chain  # noqa: E402
from just_akash import scanner_create_observation as observe  # noqa: E402
from just_akash import scanner_lease_provenance as lease  # noqa: E402
from just_akash import scanner_provenance as protocol  # noqa: E402
from just_akash import scanner_v2_controller as controller


def test_new_renderer_and_input_leave_old_no_port_default_unchanged(tmp_path):
    env = environment()
    old_binding = controller.inputs(env)
    env["RUNNER_PRIVATE_PROFILE"] = controller.RECONCILED_PROFILE
    assert controller.inputs(env) == old_binding
    p, _, raw = old.fixture()
    guest = dict(v.split("=", 1) for v in yaml.safe_load(raw)["services"]["runner"]["env"])
    for enabled in (False, True):
        path = tmp_path / str(enabled)
        controller.write_sdl(path, p.binding, guest, "SYNTHETIC_PULL_SECRET", reconciled=enabled)
        value = path.read_text()
        if enabled:
            public = lease.public_sdl_request_reconcilable(
                value, p.binding, guest["RUNNER_JIT_CONFIG"]
            )
            assert public["services"]["runner"]["expose"] == [
                {"port": 65535, "as": 65535, "proto": "tcp", "to": [{"global": True}]}
            ]
            with pytest.raises(protocol.ScannerProvenanceHeld):
                lease.public_sdl_request(value, p.binding, guest["RUNNER_JIT_CONFIG"])
        else:
            assert (
                "expose"
                not in lease.public_sdl_request(value, p.binding, guest["RUNNER_JIT_CONFIG"])[
                    "services"
                ]["runner"]
            )


@pytest.mark.parametrize(
    "candidate", ["matching", "zero", "multiple", "wrong_hash", "wrong_owner"]
)
def test_actual_failed_create_routes_only_readonly_reconciliation_and_never_upgrades_unknown(
    monkeypatch, tmp_path, candidate
):
    p, recorder, raw = reconcilable(monkeypatch)
    capture = p._reconciliation
    capture._environment = {}
    capture._token = "SYNTHETIC_HOSTED_TOKEN"
    output = tmp_path / "observation-DATA.json"
    capture._observation_path = str(output)
    client = old.Client(recorder.events, fail="create")
    p.prepare(old.prepared(p, raw, tmp_path), raw, client=client)
    rows = [row()]
    if candidate != "zero":
        rows.append(row("300"))
    if candidate == "multiple":
        rows.append(row("301"))
    fixture_reader(monkeypatch, rows)
    admissions = []
    monkeypatch.setattr(controller, "admit_native", lambda *args: admissions.append("native"))
    proof = dict(
        evidence_version=3,
        owner=OWNER,
        dseq="300",
        groups=[dict(gseq=1, name=p.binding.akash_group)],
        chain_id="akashnet-2",
        creation_manifest_version=VERSION,
        current_manifest_version=VERSION,
        registry_digest=chain.OWNER_CORROBORATION_REGISTRY_SHA256,
    )
    if candidate == "wrong_hash":
        proof["creation_manifest_version"] = "wrong"
    if candidate == "wrong_owner":
        proof["owner"] = "foreign"
    monkeypatch.setattr(
        chain, "creation_version_population_evidence", lambda *args: copy.deepcopy(proof)
    )
    bound = p.bound_client(client)
    with pytest.raises(protocol.ScannerProvenanceHeld):
        bound.create_deployment(raw, deposit=6)
    unknown = p.operation.public_record
    assert (
        unknown["phase"] == "UNKNOWN"
        and client.creates == 1
        and client.leases == client.closes == 0
    )
    assert [r["phase"] for r in recorder.records if r["kind"] == "lease_reconcile"] == [
        "NO_WIRE",
        "UNKNOWN",
    ]
    if candidate == "matching":
        value = json.loads(output.read_bytes())
        assert value["original_unknown_record_sha256"] == protocol.record_sha256(unknown)
        assert value["binding"] == p.binding.to_dict() and value["returned_runner_id"] == 191
        assert value["provider"] is None and value["group_population"] == proof["groups"]
        assert (
            value["original_UNKNOWN_unchanged"] is True
            and value["retry_authorized"] is value["cleanup_qualified"] is False
        )
        assert len(admissions) == 3
        assert all(
            v not in output.read_text()
            for v in ["SYNTHETIC_JIT_SECRET", "SYNTHETIC_PULL_SECRET", "SYNTHETIC_API_KEY"]
        )
    else:
        assert not output.exists()
    with pytest.raises(protocol.ScannerProvenanceHeld):
        bound.create_deployment(raw, deposit=6)
    assert client.creates == 1 and p.operation.public_record == unknown


def test_ack_failure_has_no_create_or_observation(monkeypatch, tmp_path):
    p, r, raw = reconcilable(monkeypatch)
    c = old.Client(r.events, fail="create")
    p.prepare(old.prepared(p, raw, tmp_path), raw, client=c)
    r.fail = ("lease_reconcile", "UNKNOWN")
    monkeypatch.setattr(
        p._reconciliation,
        "observe_unknown",
        lambda *args: pytest.fail("unacknowledged UNKNOWN cannot be observed"),
    )
    with pytest.raises(protocol.ScannerProvenanceHeld):
        p.bound_client(c).create_deployment(raw, deposit=6)
    assert c.creates == 0


def test_installed_reconciled_wrapper_calls_actual_deploy_with_closed_capture(
    monkeypatch, tmp_path
):
    captured = {}

    def delegate(**kwargs):
        captured.update(kwargs)
        return {"test": "stopped-before-real-deploy"}

    monkeypatch.setattr(lease, "deploy_scanner_v2", delegate)
    result = lease.deploy_scanner_v2_reconcilable(
        helper_path="/public-helper",
        expected_binary_sha256="f" * 64,
        environment={},
        token="PUBLIC_SYNTHETIC",
        receipt_path=str(tmp_path / "receipt"),
    )
    assert result == {"test": "stopped-before-real-deploy"}
    assert type(captured["reconciliation"]) is observe.PrecreateCapture
    assert captured["reconciliation"]._observation_path == str(
        tmp_path / "receipt.reconciliation-DATA.json"
    )


def test_controller_selects_new_real_wrapper_and_preserves_private_sdl_cleanup(
    monkeypatch, tmp_path
):
    from just_akash import runner_image, scanner_durable_pipe

    env = environment()
    env.update(
        RUNNER_PRIVATE_PROFILE=controller.RECONCILED_PROFILE,
        GH_TOKEN="SYNTHETIC_HOSTED_TOKEN",
        AKASH_API_KEY="SYNTHETIC_API_KEY",
        SOPS_AGE_KEY="SYNTHETIC_AGE",
        RUNNER_TEMP=str(tmp_path),
    )
    source = tmp_path / "opaque-encrypted-path-DATA"
    source.write_text("PUBLIC_NOT_ENCRYPTED_FIXTURE")
    helper = tmp_path / "helper"
    helper.write_text("PUBLIC_NONEXECUTED")
    env.update(
        SCANNER_REGISTRY_SOPS_PATH=str(source),
        SCANNER_SDL_VERSION_HELPER=str(helper),
        SCANNER_SDL_VERSION_HELPER_SHA256="f" * 64,
    )
    events = []
    monkeypatch.setattr(controller, "admit_native", lambda *args: events.append("native"))
    monkeypatch.setattr(scanner_durable_pipe, "PipeDurableRecorder", lambda: None)
    minted = SimpleNamespace(
        handoff=SimpleNamespace(runner_id=191, encoded_config="SYNTHETIC_JIT_SECRET"),
        public_record={},
        ack={},
    )
    monkeypatch.setattr(controller, "mint_scanner_jit", lambda *args, **kwargs: minted)
    monkeypatch.setattr(
        runner_image,
        "read_pull_credentials",
        lambda *args, **kwargs: ("jobordu", "SYNTHETIC_PULL_SECRET"),
    )
    monkeypatch.setattr(runner_image, "verify_native_reader_role", lambda *args: None)
    monkeypatch.setattr(
        controller,
        "tenant_environment",
        lambda *args, **kwargs: {
            "RUNNER_JIT_CONFIG": "SYNTHETIC_JIT_SECRET",
            "EPHEMERAL": "true",
            "RUNNER_EXECUTION_USER": "root",
            "RUNNER_NAME": controller.inputs(env)["runner_name"],
        },
    )
    paths = []

    def real_seam(**kwargs):
        path = Path(kwargs["sdl_path"])
        paths.append(path)
        assert kwargs["helper_path"] == str(helper) and kwargs["environment"] is env
        assert yaml.safe_load(path.read_text())["services"]["runner"]["expose"][0]["port"] == 65535
        raise protocol.ScannerProvenanceHeld("synthetic stop at real wrapper boundary")

    monkeypatch.setattr(lease, "deploy_scanner_v2_reconcilable", real_seam)
    monkeypatch.setattr(
        lease,
        "deploy_scanner_v2",
        lambda **kwargs: pytest.fail("old deploy path must not receive new renderer"),
    )
    with pytest.raises(protocol.ScannerProvenanceHeld):
        controller.provision(env)
    assert len(paths) == 1 and not paths[0].exists() and events == ["native"]


def test_real_reconciled_wrapper_observes_post_response_pre_ack_error_without_replay(
    monkeypatch, tmp_path
):
    import types

    p0, recorder, raw = reconcilable(monkeypatch)
    events = []
    produced = []
    real_client = old.Client(events)
    original = recorder.records[-1]
    jit_ack = old.ack(original)
    monkeypatch.setattr(controller, "admit_native", lambda *args: events.append("native"))

    def snapshot(client, owner):
        rows = [row()] if not produced else [row(), row("300")]
        return {
            "baseline": {
                "schema": "just-akash/scanner-precreate-baseline/v1",
                "owner": owner,
                "states": ["active", "closed"],
                "dseqs": [r["deployment"]["id"]["dseq"] for r in rows],
            },
            "rows": [
                {
                    "owner": owner,
                    "dseq": r["deployment"]["id"]["dseq"],
                    "state": "active",
                    "version": VERSION,
                }
                for r in rows
            ],
        }

    monkeypatch.setattr(observe, "complete_snapshot", snapshot)
    proof = dict(
        evidence_version=3,
        owner=OWNER,
        dseq="300",
        groups=[dict(gseq=1, name=p0.binding.akash_group)],
        chain_id="akashnet-2",
        creation_manifest_version=VERSION,
        current_manifest_version=VERSION,
        registry_digest=chain.OWNER_CORROBORATION_REGISTRY_SHA256,
    )
    monkeypatch.setattr(chain, "creation_version_population_evidence", lambda *args: proof)

    def after_response_before_ack(sdl_path, **kwargs):
        p = kwargs["scanner_provenance"]
        p.prepare(old.prepared(p, raw, tmp_path), raw, client=real_client)
        result = p.bound_client(real_client).create_deployment(raw, deposit=6)
        assert result["dseq"] == "300"
        produced.append(p)
        raise ValueError("SYNTHETIC_PRIVATE_RESPONSE")

    module = types.ModuleType("just_akash.deploy")
    module.deploy = after_response_before_ack
    monkeypatch.setitem(sys.modules, module.__name__, module)
    receipt = tmp_path / "candidate.json"
    with pytest.raises(protocol.ScannerProvenanceHeld) as error:
        lease.deploy_scanner_v2_reconcilable(
            helper_path="captured-helper",
            expected_binary_sha256="f" * 64,
            environment={},
            token="SYNTHETIC_HOSTED",
            sdl_path="public-fixture",
            binding=p0.binding,
            returned_runner_id=191,
            jit_ack_record=original,
            jit_ack=jit_ack,
            encoded_jit_config="SYNTHETIC_JIT_SECRET",
            recorder=recorder,
            receipt_path=str(receipt),
            admission=lambda: None,
        )
    value = json.loads(Path(str(receipt) + ".reconciliation-DATA.json").read_bytes())
    assert real_client.creates == 1 and real_client.leases == real_client.closes == 0
    assert produced[0].operation.public_record["phase"] == "UNKNOWN" and value["dseq"] == "300"
    assert error.value.__context__ is None and "SYNTHETIC_PRIVATE_RESPONSE" not in str(error.value)
