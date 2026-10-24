"""Actual new producer latch and exact-ID cleanup, public synthetic boundaries."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from just_akash import scanner_v2_fenced_controller as controller
from just_akash.scanner_prelease_fence import (
    PROFILE,
    Interrupted,
    PreleaseFence,
    PreparationFenced,
    fence_record,
    runtime_reservation,
)
from just_akash.scanner_provenance import (
    ScannerBinding,
    ScannerProvenanceHeld,
    profile_sha256,
    record_sha256,
)
from just_akash.scanner_unused_cleanup_provenance import (
    validate_bundle,
    validate_cleanup_ack,
    validate_cleanup_record,
)
from just_akash.scanner_v2_unused_cleanup import UnusedRemoval

FIXTURE = Path(__file__).parent / "fixtures/unused_jit_positive_fence.json"


def data():
    return json.loads(FIXTURE.read_bytes())


def durable(record):
    return {
        "schema": "just-akash/scanner-durable-ack/v1",
        "backend": "github-actions-artifact/v4",
        "operation_id": record["operation_id"],
        "phase": record["phase"],
        "record_sha256": record_sha256(record),
        "parent_record_sha256": record["parent_record_sha256"],
        "artifact_id": 900,
        "artifact_digest": "sha256:" + "3" * 64,
        "record_readback_sha256": record_sha256(record),
        "producer": {
            k: record["binding"][k]
            for k in (
                "repository_id",
                "run_id",
                "run_attempt",
                "workflow_sha",
                "native_head_sha",
                "producer_job_id",
            )
        },
        "exclusive": True,
        "created": True,
    }


def minted():
    value = data()
    j = value["records"]["jit"]
    b = j["binding"]
    b["providers"] = sorted(b["providers"])
    j["profile_sha256"] = profile_sha256(b)
    return SimpleNamespace(
        public_record=j,
        ack=durable(j),
        handoff=SimpleNamespace(
            runner_id=j["details"]["runner_id"],
            runner_name=b["runner_name"],
            labels=tuple(j["details"]["labels"]),
            group_id=b["runner_group_id"],
            policy_revision=b["sdk_sha"],
            encoded_config="PUBLIC_NONAUTH_CONFIG",
        ),
    )


class Recorder:
    def __init__(self, fail=False):
        self.records = []
        self.fail = fail

    def persist(self, record):
        row = fence_record(record)
        if self.fail:
            raise ValueError("PUBLIC_UNKNOWN_ACK")
        self.records.append(copy.deepcopy(row))
        return durable(row)

    def reserve_runtime(self, request):
        row = runtime_reservation(request)
        return {
            "schema": "just-akash/scanner-may-create-ack/v1",
            "operation_id": row["operation_id"],
            "request_sha256": record_sha256(row),
            "binding_sha256": record_sha256(row["binding"]),
            "state": "MAY_CREATE",
            "acknowledged": True,
            "scope": "CURRENT_PARENT_SESSION_ONLY",
        }


def test_actual_fence_poison_before_durable_request_and_after_ack():
    m, recorder = minted(), Recorder()
    f = PreleaseFence(ScannerBinding(m.public_record["binding"]), m, recorder, lambda: None)
    with pytest.raises(PreparationFenced):
        f.failure("private_scratch")
    assert f.state == "FENCED" and len(recorder.records) == 1
    assert recorder.records[0]["parent_record_sha256"] == record_sha256(m.public_record)
    assert (
        "owner" not in recorder.records[0]["details"]
        and "dseq" not in recorder.records[0]["details"]
    )
    with pytest.raises(ScannerProvenanceHeld):
        f.may_create()
    with pytest.raises(ScannerProvenanceHeld):
        f.failure("private_scratch")


def test_fence_ack_failure_and_signal_never_restore_create_or_publish_positive_terminal():
    m, r = minted(), Recorder(True)
    f = PreleaseFence(ScannerBinding(m.public_record["binding"]), m, r, lambda: None)
    with pytest.raises(ValueError, match="PUBLIC_UNKNOWN_ACK"):
        f.failure("guest_contract")
    assert f.state == "FENCED"
    with pytest.raises(ScannerProvenanceHeld):
        f.may_create()
    g = PreleaseFence(ScannerBinding(m.public_record["binding"]), m, Recorder(), lambda: None)
    with pytest.raises(Interrupted):
        g.interrupt()
    with pytest.raises(ScannerProvenanceHeld):
        g.failure("guest_contract")


def environment(tmp_path):
    m = minted()
    b = m.public_record["binding"]
    root = tmp_path / "private-scratch"
    root.mkdir()
    source, helper = root / "PUBLIC-reader-metadata-only", root / "PUBLIC-helper-metadata-only"
    source.write_text("")
    helper.write_text("")
    e = {
        "RUNNER_PRIVATE_PROFILE": PROFILE,
        "SCANNER_PUBLIC_PROFILE": "",
        "SCANNER_IMAGE": "",
        "SCANNER_REGISTRY_HOST": "https://index.docker.io/v1/",
        "SCANNER_REGISTRY_USERNAME": "jobordu",
        "SCANNER_REGISTRY_SOPS": "true",
        "SCANNER_NATIVE_PULL_READER": "false",
        "SCANNER_NATIVE_REPOSITORY_SCOPE": "false",
        "SCANNER_CREDENTIALS_SOPS": "false",
        "SCANNER_EPHEMERAL": "true",
        "SCANNER_POOL_SIZE": "1",
        "SCANNER_MIN_POOL_SIZE": "1",
        "SCANNER_CPU": "4",
        "SCANNER_MEMORY": "16Gi",
        "SCANNER_STORAGE": "96Gi",
        "SCANNER_SELECT": "cheapest",
        "SCANNER_ORG": "Borduas-Holdings",
        "SCANNER_DIRECT_PASSWORD_PRESENT": "false",
        "SCANNER_PROVIDERS": json.dumps(b["providers"]),
        "SCANNER_OWNED_PROVIDERS": json.dumps(b["providers"]),
        "GITHUB_REPOSITORY": b["repository"],
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": b["repository"] + "/" + b["workflow_path"] + "@refs/heads/main",
        "GITHUB_WORKFLOW_SHA": b["workflow_sha"],
        "GITHUB_SHA": b["native_head_sha"],
        "GITHUB_RUN_ID": str(b["run_id"]),
        "GITHUB_RUN_ATTEMPT": str(b["run_attempt"]),
        "SCANNER_SDK_SHA": b["sdk_sha"],
        "SCANNER_PRODUCER_JOB_ID": str(b["producer_job_id"]),
        "SCANNER_RUNNER_GROUP_ID": str(b["runner_group_id"]),
        "SCANNER_SDK_ROUND": str(b["sdk_round"]),
        "GITHUB_JOB": "scanner_v2",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GH_TOKEN": "PUBLIC_NONAUTH",
        "AKASH_API_KEY": "PUBLIC_NONAUTH",
        "SOPS_AGE_KEY": "PUBLIC_NONAUTH",
        "RUNNER_TEMP": str(root),
        "SCANNER_REGISTRY_SOPS_PATH": str(source),
        "SCANNER_SDL_VERSION_HELPER": str(helper),
        "SCANNER_SDL_VERSION_HELPER_SHA256": "4" * 64,
    }
    return e, m


@pytest.mark.parametrize("failure", ["guest", "scratch", "reader"])
def test_actual_provision_pure_failure_is_fenced_without_helper_import_or_runtime_entry(
    monkeypatch, tmp_path, failure
):
    from just_akash import scanner_fenced_pipe

    env, m = environment(tmp_path)
    r = Recorder()
    monkeypatch.setattr(scanner_fenced_pipe, "PipeFencedRecorder", lambda: r)
    monkeypatch.setattr(controller, "admit_native", lambda *_args: None)

    def actual_returned_id(*_args, **_kwargs):
        if failure == "reader":
            Path(env["SCANNER_REGISTRY_SOPS_PATH"]).unlink()
        return m

    monkeypatch.setattr(controller, "mint_scanner_jit", actual_returned_id)
    runtime = []
    monkeypatch.setattr(controller, "enable_runtime", lambda: runtime.append("entered"))
    # Use the real inputs/guest contract unless that exact preparation seam fails.
    if failure == "guest":

        def fail(*_args, **_kwargs):
            raise ValueError("PUBLIC_PURE_GUEST_FAILURE")

        monkeypatch.setattr(controller, "tenant_environment", fail)
    elif failure == "scratch":

        def fail(*_args, **_kwargs):
            raise OSError("PUBLIC_PURE_SCRATCH_FAILURE")

        monkeypatch.setattr(controller.tempfile, "mkdtemp", fail)
    with pytest.raises(PreparationFenced):
        controller.provision(env)
    assert runtime == [] and len(r.records) == 1
    assert r.records[0]["phase"] == "FENCE"
    assert (
        r.records[0]["details"]["stage"]
        == {
            "guest": "guest_contract",
            "scratch": "private_scratch",
            "reader": "reader_path_preflight",
        }[failure]
    )


def test_post_helper_boundary_stays_open_cleanup_gap_and_never_forges_no_create(
    monkeypatch, tmp_path
):
    from just_akash import scanner_fenced_pipe

    env, m = environment(tmp_path)
    r = Recorder()
    monkeypatch.setattr(scanner_fenced_pipe, "PipeFencedRecorder", lambda: r)
    monkeypatch.setattr(controller, "admit_native", lambda *_args: None)
    monkeypatch.setattr(controller, "mint_scanner_jit", lambda *_args, **_kwargs: m)

    def helper_entry():
        raise RuntimeError("PUBLIC_POST_HELPER_BOUNDARY")

    monkeypatch.setattr(controller, "enable_runtime", helper_entry)
    with pytest.raises(RuntimeError, match="PUBLIC_POST_HELPER_BOUNDARY"):
        controller.provision(env)
    assert r.records == []


class RemovalRecorder:
    def __init__(self, bundle, fail=None):
        self.bundle, self.fail, self.rows = bundle, fail, []
        self.clear = True

    def history(self, producer, target):
        return {"wire_clear": {"remove": self.clear}, "current_actor_records": self.rows}

    def persist(self, record):
        r = validate_cleanup_record(record)
        if r["phase"] == self.fail:
            raise ValueError("PUBLIC_REMOVAL_ACK_LOSS")
        digest = record_sha256(r)
        a = {
            "schema": "just-akash/scanner-cleanup-durable-ack/v1",
            "backend": "github-actions-artifact/v4",
            "operation_id": r["operation_id"],
            "phase": r["phase"],
            "parent_record_sha256": r["parent_record_sha256"],
            "record_sha256": digest,
            "record_readback_sha256": digest,
            "artifact_id": 1000 + len(self.rows),
            "artifact_digest": "sha256:" + "5" * 64,
            "actor": r["actor"],
            "producer": r["producer"],
            "target_sha256": r["target_sha256"],
            "history_sha256": "6" * 64,
            "created": True,
        }
        validate_cleanup_ack(r, a)
        self.rows.append(
            {
                "record": r,
                "artifact_id": a["artifact_id"],
                "artifact_digest": a["artifact_digest"],
                "body_sha256": digest,
            }
        )
        return a


def remover(*, fail=None, online=False, blocked=False):
    b = data()
    validate_bundle(b["actor"], b)
    recorder = RemovalRecorder(b, fail)
    recorder.clear = not blocked
    state, calls = {"absent": False}, []

    class Runner:
        def not_busy(self, identity, group):
            assert identity == 991 and group == b["producer"]["runner_group_id"]

        def runner(self, identity):
            return (
                None
                if state["absent"]
                else {"id": identity, "busy": False, "status": "online" if online else "offline"}
            )

        def absent(self, identity):
            assert identity == 991
            return state["absent"]

        def remove(self, identity, before):
            before()
            calls.append(identity)
            state["absent"] = True

    e = UnusedRemoval(b["actor"], b, recorder, lambda: None, Runner())
    return e, recorder, calls


def test_actual_unused_removal_uses_only_acknowledged_id_after_durable_unknown():
    e, recorder, calls = remover()
    result = e.run()
    assert calls == [991] and result["unused_jit_qualified"] is True
    assert [(r["record"]["phase"]) for r in recorder.rows] == ["NO_WIRE", "UNKNOWN", "ACK"]
    assert e.attempted and e.entered
    assert not any(k in result for k in ("owner", "dseq", "lease_closed", "cleanup_qualified"))
    with pytest.raises(ScannerProvenanceHeld):
        e.guard()


@pytest.mark.parametrize("failure", ["NO_WIRE", "UNKNOWN"])
def test_failed_immutable_phase_before_removal_never_enters_wire(failure):
    e, _r, calls = remover(fail=failure)
    with pytest.raises(ValueError, match="PUBLIC_REMOVAL_ACK_LOSS"):
        e.run()
    assert calls == [] and not e.entered


def test_unexpected_online_runner_and_prior_unknown_never_authorize_id_removal():
    for kwargs in ({"online": True}, {"blocked": True}):
        e, _r, calls = remover(**kwargs)
        with pytest.raises(ScannerProvenanceHeld):
            e.run()
        assert calls == []


def test_lost_remove_ack_is_not_replayed_and_only_positive_absence_can_finish():
    e, r, calls = remover(fail="ACK")
    result = e.run()
    assert calls == [991] and result["registration_absent"] is True
    assert result["remove_ack_received"] is False
    assert [x["record"]["phase"] for x in r.rows] == ["NO_WIRE", "UNKNOWN"]


def test_actual_unused_cleanup_entry_runs_real_native_route_actor_producer_guards(
    monkeypatch, tmp_path
):
    from just_akash import scanner_unused_cleanup_native as native
    from just_akash import scanner_v2_unused_cleanup as entry

    b = data()
    a = b["actor"]
    metadata = json.loads((FIXTURE.parent / "unused_jit_native_metadata.json").read_bytes())
    event = tmp_path / "public-native-event.json"
    event.write_text(
        json.dumps(
            {
                "action": "completed",
                "workflow_run": {
                    "id": a["target_locator"]["run_id"],
                    "run_attempt": a["target_locator"]["run_attempt"],
                },
                "repository": {"id": a["repository_id"]},
            }
        )
    )
    env = {
        "GITHUB_REPOSITORY": a["repository"],
        "GITHUB_REF": a["ref"],
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_JOB": "cleanup",
        "SCANNER_TARGET_RUN_ID": str(a["target_locator"]["run_id"]),
        "SCANNER_TARGET_RUN_ATTEMPT": str(a["target_locator"]["run_attempt"]),
        "SCANNER_TARGET_ROUND": str(a["target_locator"]["sdk_round"]),
        "GITHUB_EVENT_NAME": a["event"],
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_RUN_ID": str(a["run_id"]),
        "GITHUB_RUN_ATTEMPT": str(a["run_attempt"]),
        "GITHUB_WORKFLOW_SHA": a["workflow_sha"],
        "GITHUB_SHA": a["workflow_sha"],
        "GITHUB_WORKFLOW_REF": a["repository"] + "/" + a["workflow_path"] + "@refs/heads/main",
        "SCANNER_CLEANUP_SDK_SHA": a["sdk_sha"],
        "SCANNER_ACTOR_JOB_ID": str(a["job_id"]),
        "SCANNER_CLEANUP_NATIVE_HEAD_SHA": a["native_head_sha"],
        "RUNNER_NAME": "Hosted cleanup public fixture",
        "GH_TOKEN": "PUBLIC_NONAUTH",
        "SCANNER_CLEANUP_ACTOR_JSON": json.dumps(a),
    }
    calls = []

    class Response:
        status = 200

        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _maximum):
            return json.dumps(self.value).encode()

    class Opener:
        def open(self, request, timeout):
            assert request.method == "GET" and 0 < timeout <= 10
            route = request.full_url.removeprefix("https://api.github.com")
            assert route in metadata
            calls.append(route)
            return Response(metadata[route])

    monkeypatch.setattr(native.urllib.request, "build_opener", lambda *_args: Opener())
    r = RemovalRecorder(b)
    monkeypatch.setattr(r, "read_target", lambda: copy.deepcopy(b), raising=False)
    monkeypatch.setattr(entry, "PipeUnusedRecorder", lambda _actor: r)

    class AbsentRunner:
        def not_busy(self, identity, group):
            assert identity == 991 and group == b["producer"]["runner_group_id"]

        def runner(self, _identity):
            return None

        def absent(self, identity):
            assert identity == 991
            return True

        def remove(self, *_args):
            raise AssertionError("already absent positive inverse cannot wire")

    monkeypatch.setattr(entry, "RunnerAPI", lambda _token: AbsentRunner())
    result = entry.cleanup(env)
    assert result["unused_jit_qualified"] is True and result["remove_wire_attempted"] is False
    assert calls.count("/repos/Borduas-Holdings/Blazing-Back/actions/runs/12345") >= 3
    assert all(route.startswith("/repos/") for route in calls)


@pytest.mark.parametrize(
    "change",
    [
        {"GITHUB_REF": "refs/heads/feature"},
        {"GITHUB_EVENT_NAME": "pull_request"},
        {"SCANNER_RUNNER_GROUP_ID": ""},
        {"SCANNER_STORAGE": "30Gi"},
        {"RUNNER_PRIVATE_PROFILE": "bb-native-trufflehog-v2-reconciled"},
    ],
)
def test_new_fenced_profile_never_borrows_other_source_group_resource_or_profile(tmp_path, change):
    env, _m = environment(tmp_path)
    env.update(change)
    with pytest.raises(controller.ControllerHeld):
        controller.inputs(env)


def test_original_static_reader_refusal_precedes_mint_and_any_fence(monkeypatch, tmp_path):
    env, _ = environment(tmp_path)
    Path(env["SCANNER_REGISTRY_SOPS_PATH"]).unlink()
    entered = []
    monkeypatch.setattr(controller, "mint_scanner_jit", lambda *_a, **_k: entered.append("mint"))
    with pytest.raises(controller.ControllerHeld):
        controller.provision(env)
    assert entered == []


def test_unused_positive_fence_is_bound_to_same_exact_producer_and_cleanup_sdk():
    b = data()
    b["actor"]["sdk_sha"] = "c" * 40
    with pytest.raises(ScannerProvenanceHeld):
        validate_bundle(b["actor"], b)
