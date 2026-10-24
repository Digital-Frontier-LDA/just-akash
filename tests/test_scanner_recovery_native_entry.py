"""Actual cleanup entry -> unchanged NativeGET routing; public fixture bindings only."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from just_akash import scanner_cleanup_native as native
from just_akash import scanner_cleanup_provenance as known
from just_akash import scanner_v2_recovery_cleanup as subject
from just_akash.scanner_provenance import record_sha256, validate_record

SOURCE = Path(__file__).resolve().parents[1]


class ReachedReadonlyDerivation(RuntimeError):
    pass


def data():
    return json.loads((SOURCE / "tests/fixtures/scanner_recovery_source.json").read_bytes())


def known_bundle(value):
    source = copy.deepcopy(value["source"])
    u = source["records"]["deployment"]
    j = source["records"]["jit"]
    seq = value["witness"]["target"]["dseq"]
    ack = validate_record(
        dict(
            u,
            phase="ACK",
            phase_index=2,
            parent_record_sha256=record_sha256(u),
            details=dict(u["details"], dseq=seq),
        )
    )
    source["schema"] = "just-akash/scanner-cleanup-target/v1"
    source["records"]["deployment"] = ack
    source["phase_inventory"]["deployment"] = ["NO_WIRE", "UNKNOWN", "ACK"]
    source["target"] = dict(
        owner=u["details"]["owner"],
        dseq=seq,
        returned_runner_id=j["details"]["runner_id"],
        group_population=u["details"]["group_population"],
        jit_ack_record_sha256=record_sha256(j),
        deployment_ack_record_sha256=record_sha256(ack),
        lease_ack_record_sha256=None,
    )
    b = source["producer"]
    source["evidence"].append(
        dict(
            id=99001,
            name=f"scanner-v2-{b['repository_id']}-{b['run_id']}-a{b['run_attempt']}-r{b['sdk_round']}-lease-j{b['producer_job_id']}-ack",
            digest="sha256:" + "7" * 64,
            size_in_bytes=1234,
            record_sha256=record_sha256(ack),
            kind="lease",
            phase="ACK",
        )
    )
    return known.validate_bundle(source["actor"], source)


def bind_entry(monkeypatch, bundle):
    actor = known.validate_actor(bundle["actor"])
    env = dict(GH_TOKEN="PUBLIC_SYNTHETIC_NONAUTH", SCANNER_CLEANUP_ACTOR_JSON=json.dumps(actor))
    # Native authority is an already-admitted public fixture for this one route
    # control. The production NativeGET class and its actual route guard stay real.
    monkeypatch.setattr(subject, "locate_environment", lambda environment: None)
    monkeypatch.setattr(subject, "actor", lambda environment, get: actor)
    monkeypatch.setattr(subject, "producer", lambda *args: None)

    class Recorder:
        def __init__(self, current):
            assert current == actor

        def read_target(self):
            return copy.deepcopy(bundle)

    monkeypatch.setattr(subject, "PipeRecoveryRecorder", Recorder)
    opened = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, maximum):
            return json.dumps(dict(run_attempt=bundle["producer"]["run_attempt"])).encode()

    class Opener:
        def open(self, request, timeout):
            opened.append(request.full_url)
            return Response()

    monkeypatch.setattr(native.urllib.request, "build_opener", lambda *args: Opener())
    return env, opened


def test_existing_known_ACK_entry_and_actual_fixed_route_remain_compatible(monkeypatch):
    bundle = known_bundle(data())
    env, opened = bind_entry(monkeypatch, bundle)
    route = f"/repos/Borduas-Holdings/Blazing-Back/actions/runs/{bundle['producer']['run_id']}"
    assert (
        native.NativeGET("PUBLIC_SYNTHETIC_NONAUTH")(route)["run_attempt"]
        == bundle["producer"]["run_attempt"]
    )
    calls = []

    class Engine:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def run(self):
            return dict(known_ACK_entry_reached=True)

    monkeypatch.setattr(subject.known, "CleanupEngine", Engine)
    assert subject.cleanup(env) == dict(known_ACK_entry_reached=True)
    assert len(calls) == 1 and opened == ["https://api.github.com" + route]


def test_actual_terminal_UNKNOWN_entry_reaches_readonly_derivation_after_fresh_native_read(
    monkeypatch,
):
    bundle = data()["source"]
    env, opened = bind_entry(monkeypatch, bundle)

    class Resolver:
        def __init__(self, *args):
            pass

        def observe(self):
            raise ReachedReadonlyDerivation("PUBLIC_READONLY_GATE_REACHED")

    monkeypatch.setattr(subject, "RecoveryResolver", Resolver)
    with pytest.raises(ReachedReadonlyDerivation):
        subject.cleanup(env)
    assert opened == [
        f"https://api.github.com/repos/Borduas-Holdings/Blazing-Back/actions/runs/{bundle['producer']['run_id']}"
    ]
