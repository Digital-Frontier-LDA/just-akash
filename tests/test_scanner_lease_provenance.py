# ruff: noqa: E402 -- load only sibling PUBLIC source, portable without author cache
"""Offline public synthetic controls at the actual SDK function/effect boundaries."""

import ast
import copy
import hashlib
import json
import logging
import math
import sys
import time
import types
from pathlib import Path

import pytest
import yaml

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from just_akash import deployment_receipt as local  # noqa: E402 -- portable public fixture import
from just_akash import (
    scanner_lease_provenance as lease,  # noqa: E402 -- portable public fixture import
)
from just_akash import (
    scanner_provenance as protocol,  # noqa: E402 -- portable public fixture import
)

OWNER = sorted(protocol.PROVIDERS)[0]


def binding():
    return protocol.ScannerBinding(
        dict(
            repository="Borduas-Holdings/Blazing-Back",
            repository_id=1071436278,
            workflow_path=".github/workflows/secret-scanning.yml",
            workflow_sha="a" * 40,
            native_head_sha="a" * 40,
            sdk_sha="b" * 40,
            event="workflow_dispatch",
            ref="refs/heads/main",
            run_id=42,
            run_attempt=2,
            sdk_round=1,
            producer_job_id=91,
            runner_group_id=7,
            role=protocol.ROLE,
            image=protocol.IMAGE,
            providers=sorted(protocol.PROVIDERS),
            resources=dict(cpu="4", memory="16Gi", storage="96Gi", count=1),
            runner_name=protocol.runner_name(42, 2, 1),
            runner_label=protocol.runner_name(42, 2, 1),
            akash_group=protocol.placement(42, 2, 1),
        )
    )


def ack(record):
    sha = protocol.record_sha256(record)
    return dict(
        schema="just-akash/scanner-durable-ack/v1",
        backend="github-actions-artifact/v4",
        operation_id=record["operation_id"],
        phase=record["phase"],
        record_sha256=sha,
        parent_record_sha256=record["parent_record_sha256"],
        artifact_id=100,
        artifact_digest="sha256:" + "c" * 64,
        record_readback_sha256=sha,
        producer=protocol.producer_identity(record["binding"]),
        exclusive=True,
        created=True,
    )


class Recorder:
    def __init__(self, events=None, fail=None):
        self.events = events if events is not None else []
        self.records = []
        self.fail = fail

    def persist(self, record):
        protocol.validate_record(record)
        self.events.append((record["kind"], record["phase"]))
        self.records.append(copy.deepcopy(record))
        if (record["kind"], record["phase"]) == self.fail:
            raise OSError("SYNTHETIC_SECRET_SENTINEL")
        return ack(record)


def fixture(recorder=None, admission=lambda: None):
    b = binding()
    r = recorder if recorder is not None else Recorder()
    op = protocol.ScannerOperation(b, "jit", r)
    details = dict(
        runner_name=b.runner_name,
        runner_group_id=b.runner_group_id,
        labels=["self-hosted", "linux", "akash", b.runner_label],
        request_body_sha256="d" * 64,
    )
    op.no_wire(details)
    op.before_wire(details)
    a = op.ack(dict(details, runner_id=191))
    p = lease.ScannerLeaseProvenance(
        binding=b,
        returned_runner_id=191,
        jit_ack_record=op.public_record,
        jit_ack=a,
        encoded_jit_config="SYNTHETIC_JIT_SECRET",
        recorder=r,
        admission=admission,
    )
    doc = dict(
        version="2.0",
        services=dict(
            runner=dict(
                image=protocol.IMAGE,
                credentials=dict(
                    host="https://index.docker.io/v1/",
                    username="jobordu",
                    password="SYNTHETIC_PULL_SECRET",
                ),
                env=[
                    "RUNNER_JIT_CONFIG=SYNTHETIC_JIT_SECRET",
                    "EPHEMERAL=true",
                    "RUNNER_EXECUTION_USER=root",
                    "RUNNER_NAME=" + b.runner_name,
                ],
            )
        ),
        profiles=dict(
            compute=dict(
                runner=dict(
                    resources=dict(
                        cpu=dict(units="4"), memory=dict(size="16Gi"), storage=[dict(size="96Gi")]
                    )
                )
            ),
            placement={
                b.akash_group: dict(pricing=dict(runner=dict(denom="uact", amount=100000)))
            },
        ),
        deployment=dict(runner={b.akash_group: dict(profile="runner", count=1)}),
    )
    return p, r, yaml.safe_dump(doc, sort_keys=False)


class Client:
    api_key = "SYNTHETIC_API_KEY"

    def __init__(self, events, fail=None):
        self.events = events
        self.fail = fail
        self.creates = 0
        self.leases = 0
        self.closes = 0

    def account_address(self):
        return OWNER

    def create_deployment(self, content, deposit):
        self.creates += 1
        self.events.append(("wire", "deployment"))
        if self.fail == "create":
            raise OSError("SYNTHETIC_SECRET_SENTINEL")
        return dict(dseq="300", manifest="SYNTHETIC_PRIVATE_MANIFEST")

    def create_lease(self, **kwargs):
        self.leases += 1
        self.events.append(("wire", "lease"))
        if self.fail == "lease":
            raise RuntimeError("JWT has invalid claims SYNTHETIC_SECRET_SENTINEL")
        return dict(data=dict(lease=True))

    def close_deployment(self, dseq):
        self.closes += 1


def prepared(p, content, tmp_path):
    return local.prepare_receipt(
        str(tmp_path / "local.json"),
        operation_id=p.operation.operation_id,
        owner=OWNER,
        sdl_content=content,
    )


def actual_deploy(monkeypatch, p, content, client):
    """Whole actual deploy function, public observation helpers substituted offline.

    Stops immediately after the real local-create transition and public deployment ACK;
    the separate real bound-client lease controls cover its actual provider wire.
    """
    tree = ast.parse((SOURCE / "just_akash/deploy.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "deploy")
    node.decorator_list = []
    wallet = types.SimpleNamespace(
        client=client, configured_keys=1, account=OWNER, contention_aware=False
    )
    api = types.ModuleType("just_akash.api")
    api.CIConsoleAPI = type("SyntheticFixedOriginClient", (), {})
    monkeypatch.setitem(sys.modules, "just_akash.api", api)
    pool = types.ModuleType("just_akash.wallet_pool")

    def select_wallet(*a, **k):
        assert k["client_factory"] is api.CIConsoleAPI
        return wallet

    pool.select_client_for_create = select_wallet
    pool.configured_api_keys = lambda: ["SYNTHETIC_API_KEY"]
    monkeypatch.setitem(sys.modules, "just_akash.wallet_pool", pool)

    class Finished(Exception):
        pass

    def log(level, message):
        if message.startswith("Deployment created "):
            raise Finished()

    ns = dict(
        __package__="just_akash",
        math=math,
        logging=logging,
        os=types.SimpleNamespace(environ={}),
        time=time,
        json=json,
        AkashConsoleAPI=object,
        AkashAPIError=type("SyntheticAPIError", (RuntimeError,), {}),
        _resolve_selection=lambda value: value,
        AuctionPolicy=lambda **kw: None,
        charter_mod=types.SimpleNamespace(
            mode=lambda: "off", is_ours_placement=lambda k: False, gate=lambda *a: False
        ),
        _sdl_placement_keys=lambda *a: ([p.binding.akash_group], []),
        uncharted_key=lambda v: v,
        OURS_PREFIXES=("flynn-",),
        _resolve_tier=lambda value, name: value,
        _log=log,
        display=lambda value, kind: value,
        _resolve_sdl_path=lambda path, gpu: path,
        _prepare_sdl_content=lambda *a, **k: content,
        protect_content=lambda _: None,
        _check_wallet_credit=lambda *a: None,
        prepare_receipt=local.prepare_receipt,
        credential_binding_for=local.credential_binding_for,
        mark_submitting=local.mark_submitting,
        mark_create_response_received=local.mark_create_response_received,
        _private_create_identity=lambda d: None,
        active=lambda: True,
        emit=lambda *a, **k: None,
        error_text=lambda e: str(e),
        _report_suspected_orphans=lambda *a: None,
        _RUN_ID="synthetic",
        Code=types.SimpleNamespace(DEPLOY_CREATE_FAILED="synthetic"),
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            node,
        ],
        type_ignores=[],
    )
    exec(
        compile(ast.fix_missing_locations(module), str(SOURCE / "just_akash/deploy.py"), "exec"),
        ns,
    )
    return ns["deploy"], Finished


def test_actual_deploy_orders_durable_ack_before_wire_and_candidate_before_auction(
    monkeypatch, tmp_path
):
    p, r, content = fixture()
    c = Client(r.events)
    f, Finished = actual_deploy(monkeypatch, p, content, c)
    r.events.clear()
    with pytest.raises(Finished):
        f(
            "synthetic.yaml",
            preferred_providers=sorted(protocol.PROVIDERS),
            backup_providers=[],
            deposit=6,
            receipt_path=str(tmp_path / "local.json"),
            receipt_operation_id=p.operation.operation_id,
            scanner_provenance=p,
        )
    assert r.events == [
        ("lease", "NO_WIRE"),
        ("lease", "UNKNOWN"),
        ("wire", "deployment"),
        ("lease", "ACK"),
    ]
    assert c.creates == 1 and c.closes == 0
    assert json.loads((tmp_path / "local.json").read_text())["state"] == "create_response_received"
    assert p.operation.public_record["details"]["dseq"] == "300"
    assert (
        p.operation.public_record["details"]["wire_request_sha256"]
        == hashlib.sha256(content.encode()).hexdigest()
    )


@pytest.mark.parametrize("phase", ["NO_WIRE", "UNKNOWN"])
def test_actual_deploy_missing_durable_ack_never_sends(monkeypatch, tmp_path, phase):
    p, r, content = fixture()
    r.fail = ("lease", phase)
    c = Client(r.events)
    f, _ = actual_deploy(monkeypatch, p, content, c)
    with pytest.raises(RuntimeError):
        f(
            "synthetic.yaml",
            preferred_providers=sorted(protocol.PROVIDERS),
            backup_providers=[],
            deposit=6,
            receipt_path=str(tmp_path / "local.json"),
            receipt_operation_id=p.operation.operation_id,
            scanner_provenance=p,
        )
    assert c.creates == 0 and c.closes == 0
    with pytest.raises(protocol.ScannerProvenanceHeld):
        p.operation.before_wire(p._details)


def setup_created(tmp_path, fail=None):
    p, r, content = fixture()
    p.prepare(prepared(p, content, tmp_path), content)
    c = Client(r.events, fail)
    bound = p.bound_client(c)
    bound.create_deployment(content, deposit=6)
    p.created("300")
    return p, r, c, bound


def test_actual_bound_lease_orders_phases_and_returns_public_only(tmp_path):
    p, r, c, bound = setup_created(tmp_path)
    r.events.clear()
    bound.create_lease(dseq="300", provider=OWNER, manifest="SYNTHETIC_PRIVATE_MANIFEST")
    assert r.events == [
        ("lease_bind", "NO_WIRE"),
        ("lease_bind", "UNKNOWN"),
        ("wire", "lease"),
        ("lease_bind", "ACK"),
    ]
    result = p.public_result(dict(dseq="300", provider=OWNER, manifest="must-not-escape"))
    text = json.dumps(result)
    assert result["cleanup_qualified"] is False and result["returned_runner_id"] == 191
    assert all(
        v not in text
        for v in [
            "SYNTHETIC_JIT_SECRET",
            "SYNTHETIC_PULL_SECRET",
            "SYNTHETIC_PRIVATE_MANIFEST",
            "must-not-escape",
        ]
    )
    assert result["lease_ack_record"]["details"][
        "deployment_ack_record_sha256"
    ] == protocol.record_sha256(p.operation.public_record)


@pytest.mark.parametrize("phase", ["NO_WIRE", "UNKNOWN"])
def test_lease_receipt_failure_prevents_provider_wire(tmp_path, phase):
    p, r, c, bound = setup_created(tmp_path)
    r.fail = ("lease_bind", phase)
    with pytest.raises(protocol.ScannerProvenanceHeld):
        bound.create_lease(dseq="300", provider=OWNER, manifest="SYNTHETIC_PRIVATE_MANIFEST")
    assert c.leases == 0 and c.closes == 0
    with pytest.raises(protocol.ScannerProvenanceHeld):
        bound.create_lease(dseq="300", provider=OWNER, manifest="SYNTHETIC_PRIVATE_MANIFEST")
    assert c.leases == 0


def test_ambiguous_provider_mutation_retains_unknown_never_retries_or_closes(tmp_path):
    p, r, c, bound = setup_created(tmp_path, "lease")
    with pytest.raises(protocol.ScannerProvenanceHeld) as error:
        bound.create_lease(dseq="300", provider=OWNER, manifest="SYNTHETIC_PRIVATE_MANIFEST")
    assert "SYNTHETIC_SECRET_SENTINEL" not in str(error.value) and error.value.__context__ is None
    assert p.lease_operation.public_record["phase"] == "UNKNOWN"
    with pytest.raises(protocol.ScannerProvenanceHeld):
        bound.create_lease(dseq="300", provider=OWNER, manifest="SYNTHETIC_PRIVATE_MANIFEST")
    with pytest.raises(protocol.ScannerProvenanceHeld):
        bound.close_deployment("300")
    assert c.leases == 1 and c.closes == 0


def test_changed_native_admission_after_intent_prevents_first_wire(tmp_path):
    allowed = [True]

    def admission():
        protocol.require(allowed[0])

    p, r, content = fixture(admission=admission)
    p.prepare(prepared(p, content, tmp_path), content)
    c = Client(r.events)
    bound = p.bound_client(c)
    allowed[0] = False
    with pytest.raises(protocol.ScannerProvenanceHeld):
        bound.create_deployment(content, deposit=6)
    assert c.creates == 0 and p.operation.public_record["phase"] == "NO_WIRE"


@pytest.mark.parametrize(
    "change", ["foreign_provider", "dseq", "group", "secret_env", "extra_port"]
)
def test_profile_and_lease_drift_refused_without_new_effect(tmp_path, change):
    p, r, content = fixture()
    doc = yaml.safe_load(content)
    if change == "group":
        doc["deployment"]["runner"] = {"foreign": dict(profile="runner", count=1)}
        doc["profiles"]["placement"] = {
            "foreign": dict(pricing=dict(runner=dict(denom="uact", amount=100000)))
        }
    if change == "secret_env":
        doc["services"]["runner"]["env"].append("GH_RUNNER_PAT=SYNTHETIC")
    if change == "extra_port":
        doc["services"]["runner"]["expose"] = [dict(port=80)]
    if change in ["group", "secret_env", "extra_port"]:
        content = yaml.safe_dump(doc)
        with pytest.raises(protocol.ScannerProvenanceHeld):
            p.prepare(prepared(p, content, tmp_path), content)
        assert not any(x["kind"] == "lease" for x in r.records)
    else:
        p, r, c, bound = setup_created(tmp_path)
        with pytest.raises(protocol.ScannerProvenanceHeld):
            bound.create_lease(
                dseq="301" if change == "dseq" else "300",
                provider="foreign" if change == "foreign_provider" else OWNER,
                manifest="SYNTHETIC_PRIVATE_MANIFEST",
            )
        assert c.leases == 0


def test_missing_recorder_and_forged_native_ack_hold_and_block_replay():
    b = binding()
    op = protocol.ScannerOperation(b, "jit")
    details = dict(
        runner_name=b.runner_name,
        runner_group_id=b.runner_group_id,
        labels=["self-hosted", "linux", "akash", b.runner_label],
        request_body_sha256="d" * 64,
    )
    with pytest.raises(protocol.ScannerProvenanceHeld):
        op.no_wire(details)
    with pytest.raises(protocol.ScannerProvenanceHeld):
        op.no_wire(details)

    class Wrong(Recorder):
        def persist(self, record):
            value = super().persist(record)
            value["producer"]["run_attempt"] = 3
            return value

    op = protocol.ScannerOperation(b, "jit", Wrong())
    with pytest.raises(protocol.ScannerProvenanceHeld):
        op.no_wire(details)


def test_public_request_hash_omits_secret_fingerprints_but_preserves_profile():
    p, r, content = fixture()
    a = lease.public_sdl_request(content, p.binding, "SYNTHETIC_JIT_SECRET")
    altered = content.replace("SYNTHETIC_PULL_SECRET", "OTHER_PULL").replace(
        "SYNTHETIC_JIT_SECRET", "OTHER_JIT"
    )
    b = lease.public_sdl_request(altered, p.binding, "OTHER_JIT")
    assert protocol.record_sha256(a) == protocol.record_sha256(b)
    view = p.binding["resources"]
    view["cpu"] = "999"
    assert p.binding.resources["cpu"] == "4"


def test_actual_provider_lease_except_refuses_old_jwt_retry_and_implicit_close(tmp_path):
    p, r, c, bound = setup_created(tmp_path, "lease")
    tree = ast.parse((SOURCE / "just_akash/deploy.py").read_text())
    node = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Try)
        and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "create_lease"
            for statement in n.body
            for call in ast.walk(statement)
        )
    )
    loop = ast.While(test=ast.Constant(value=True), body=[node], orelse=[])
    ns = dict(
        client=bound,
        scanner_provenance=p,
        dseq="300",
        provider=OWNER,
        manifest="SYNTHETIC_PRIVATE_MANIFEST",
        lease_gseq=1,
    )
    with pytest.raises(protocol.ScannerProvenanceHeld):
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[])),
                "actual-provider-lease-try",
                "exec",
            ),
            ns,
        )
    assert (
        c.leases == 1 and c.closes == 0 and p.lease_operation.public_record["phase"] == "UNKNOWN"
    )


def test_native_authority_changes_during_durable_upload_blocks_actual_create(tmp_path):
    allowed = [True]

    class DriftRecorder(Recorder):
        def persist(self, record):
            value = super().persist(record)
            if record["kind"] == "lease" and record["phase"] == "UNKNOWN":
                allowed[0] = False
            return value

    p, r, content = fixture(
        recorder=DriftRecorder(), admission=lambda: protocol.require(allowed[0])
    )
    p.prepare(prepared(p, content, tmp_path), content)
    c = Client(r.events)
    bound = p.bound_client(c)
    with pytest.raises(protocol.ScannerProvenanceHeld):
        bound.create_deployment(content, deposit=6)
    assert c.creates == 0 and p.operation.public_record["phase"] == "UNKNOWN"


def test_owner_or_returned_manifest_substitution_cannot_reach_lease_wire(tmp_path):
    p, r, c, bound = setup_created(tmp_path)
    with pytest.raises(protocol.ScannerProvenanceHeld):
        bound.create_lease(dseq="300", provider=OWNER, manifest="REPLACEMENT_MANIFEST")
    c.account_address = lambda: sorted(protocol.PROVIDERS)[1]
    with pytest.raises(protocol.ScannerProvenanceHeld):
        bound.create_lease(dseq="300", provider=OWNER, manifest="SYNTHETIC_PRIVATE_MANIFEST")
    assert c.leases == 0 and c.closes == 0


def test_native_producer_attempt_boolean_cannot_impersonate_integer_one():
    data = binding().to_dict()
    data["run_attempt"] = 1
    data["runner_name"] = data["runner_label"] = protocol.runner_name(42, 1, 1)
    data["akash_group"] = protocol.placement(42, 1, 1)
    b = protocol.ScannerBinding(data)

    class BoolAck(Recorder):
        def persist(self, record):
            value = super().persist(record)
            value["producer"]["run_attempt"] = True
            return value

    operation = protocol.ScannerOperation(b, "jit", BoolAck())
    details = dict(
        runner_name=b.runner_name,
        runner_group_id=b.runner_group_id,
        labels=["self-hosted", "linux", "akash", b.runner_label],
        request_body_sha256="d" * 64,
    )
    with pytest.raises(protocol.ScannerProvenanceHeld):
        operation.no_wire(details)
