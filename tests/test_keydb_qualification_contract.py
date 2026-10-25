"""Versioned branch-family protocol controls; all transports/effects synthetic."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
import yaml
from _qualification_fixtures import Fixture, actor, binding, target
from _qualification_native_fixtures import Native
from _qualification_native_fixtures import environment as actor_environment
from _qualification_producer_fixtures import ack, environment, group_fixture

from just_akash import keydb_parity_provenance as OLD
from just_akash import keydb_qualification_cleanup_native as CN
from just_akash import keydb_qualification_cleanup_provenance as CP
from just_akash import keydb_qualification_controller as C
from just_akash import keydb_qualification_jit as J
from just_akash import keydb_qualification_native as N
from just_akash import keydb_qualification_provenance as P

SOURCE = Path(__file__).resolve().parents[1]

BRANCH = "codex/keydb-parity-owned-d8ab-20261024"
REF = "refs/heads/" + BRANCH


def test_fixed_branch_new_family_and_main_v1_are_disjoint():
    row = P.KeyDBQualificationBinding(C.inputs(environment())).to_dict()
    assert row["ref"] == REF and row["role"] == "blazing-keydb-parity-qualification-v1"
    assert row["runner_name"] == "dfci-bl-keydb-qualification-42-a2-r1"
    assert row["akash_group"] == "borduas-keydb-qualification-run-42-a2-r1-end"
    with pytest.raises(OLD.KeyDBProvenanceHeld):
        OLD.validate_binding(row)
    legacy = dict(
        row,
        role="blazing-keydb-modules-parity-v1",
        ref="refs/heads/main",
        runner_name="dfci-bl-keydb-42-a2-r1",
        runner_label="dfci-bl-keydb-42-a2-r1",
        akash_group="borduas-keydb-parity-run-42-a2-r1-end",
    )
    OLD.validate_binding(legacy)
    with pytest.raises(P.KeyDBQualificationProvenanceHeld):
        P.validate_binding(legacy)


@pytest.mark.parametrize(
    "key,value",
    [
        ("GITHUB_REF", "refs/heads/main"),
        ("GITHUB_REF", "refs/heads/other"),
        ("GITHUB_EVENT_NAME", "workflow_run"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_JOB", "keydb_parity_v1"),
        ("RUNNER_PRIVATE_PROFILE", "blazing-keydb-modules-parity-v1"),
        ("KEYDB_QUALIFICATION_ORG", "PUBLIC_FOREIGN_ORG"),
    ],
)
def test_closed_input_refusals_before_management_or_mint(key, value):
    changed = environment()
    changed[key] = value
    with pytest.raises(C.ControllerHeld):
        C.inputs(changed)


def test_private_qualification_group_and_actual_mint_target_not_main_group():
    request, calls = group_fixture()
    J.verify_group(99, request, "PUBLIC_TEST_ONLY")
    assert calls and all(method == "GET" for method, _ in calls)
    assert J.ROOT_WORKFLOW.endswith("/keydb-modules-parity.yml@" + REF)
    assert J.RUNNER_GROUP_NAME == "blazing-keydb-parity-qualification-v1"
    for change in ["BBrepository", "BBgroup", "wrongWorkflow"]:
        request, calls = group_fixture(change)
        with pytest.raises(J.JitHold):
            J.verify_group(99, request, "PUBLIC_TEST_ONLY")
        assert all(method == "GET" for method, _ in calls)


@pytest.mark.parametrize(
    "change", [None, "refMove", "wrongSDK", "wrongWorkflowID", "skippedProducer", "wrongRunner"]
)
def test_actual_producer_admission_reads_real_branch_and_current_job(monkeypatch, change):
    env = environment()
    env["RUNNER_NAME"] = "PUBLIC_HOSTED_RUNNER"
    b = P.KeyDBQualificationBinding(C.inputs(env))
    job = dict(
        id=77,
        run_id=42,
        run_attempt=2,
        head_sha="a" * 40,
        name=C.NATIVE_JOB,
        status="in_progress",
        conclusion=None,
        runner_name=env["RUNNER_NAME"],
        runner_id=123,
        labels=["ubuntu-latest"],
        steps=[dict(name=C.NATIVE_STEP, status="in_progress", conclusion=None)],
    )
    run = dict(
        id=42,
        workflow_id=308813680,
        run_attempt=2,
        event="workflow_dispatch",
        path=C.WORKFLOW,
        head_sha="a" * 40,
        head_branch=BRANCH,
        status="in_progress",
        conclusion=None,
        repository=dict(id=1074974924),
        head_repository=dict(id=1074974924),
        referenced_workflows=[
            dict(path=C.SDK_REPOSITORY + "/" + C.SDK_WORKFLOW + "@" + "b" * 40, sha="b" * 40)
        ],
    )
    repo = dict(
        id=1074974924,
        full_name=C.REPOSITORY,
        private=True,
        owner=dict(id=208689568, login="Borduas-Holdings"),
    )
    branch = dict(object=dict(sha="c" * 40 if change == "refMove" else "a" * 40))
    if change == "wrongSDK":
        run["referenced_workflows"][0]["sha"] = "c" * 40
    if change == "wrongWorkflowID":
        run["workflow_id"] = 1
    if change == "skippedProducer":
        job.update(status="completed", conclusion="skipped")
    if change == "wrongRunner":
        job["runner_name"] = "PUBLIC_FOREIGN"
    routes = []

    def get(route):
        routes.append(route)
        if route == C.ROOT:
            return copy.deepcopy(repo)
        if "/git/ref/heads/" in route:
            assert route == C.ROOT + "/git/ref/heads/" + BRANCH
            return copy.deepcopy(branch)
        if "/jobs?" in route:
            return dict(total_count=1, jobs=[copy.deepcopy(job)])
        if "/actions/jobs/" in route:
            return copy.deepcopy(job)
        return copy.deepcopy(run)

    groups = []
    monkeypatch.setattr(N, "NativeGET", lambda token: get)
    monkeypatch.setattr(C, "verify_group", lambda *a: groups.append(a[0]))
    if change:
        with pytest.raises(C.ControllerHeld):
            C.admit_native(b, env, "PUBLIC_HOSTED_TEST_ONLY")
        assert groups == []
    else:
        C.admit_native(b, env, "PUBLIC_HOSTED_TEST_ONLY")
        assert groups == [99] and sum("/git/ref/heads/" + BRANCH in route for route in routes) == 2


@pytest.mark.parametrize("mode", ["normal", "recover"])
def test_genuine_manual_actor_and_historical_producer_native_route(mode, tmp_path):
    native = Native(mode)
    env = actor_environment(native, tmp_path)
    before = env.copy()
    env["GITHUB_EVENT_PATH"] = str(tmp_path / "ABSENT_EVENT_FILE_NOT_READ")
    current = CN.actor(env, native)
    CN.producer(current, binding(), native, target())
    assert current == native.actor and current["event"] == "workflow_dispatch"
    assert current["ref"] == REF and current["workflow_path"] == C.WORKFLOW
    if mode == "recover":
        assert current["run_id"] != binding()["run_id"]
    assert all(env[k] == v for k, v in before.items())


@pytest.mark.parametrize("mode", ["normal", "recover"])
def test_new_engine_known_ID_lifecycle_keeps_both_intents_before_wires(mode):
    f = Fixture(mode)
    result = f.engine().run()
    assert result["cleanup_qualified"] is True and f.closes == f.removes == 1
    close = f.events.index(("wire", "close"))
    remove = f.events.index(("wire", "remove"))
    assert (
        f.events.index(("close", "NO_WIRE")) < close
        and f.events.index(("remove", "NO_WIRE")) < close
    )
    assert f.events.index(("close", "UNKNOWN")) < close < f.events.index(("close", "ACK")) < remove
    assert f.events.index(("remove", "UNKNOWN")) < remove < f.events.index(("remove", "ACK"))


@pytest.mark.parametrize("mode", ["normal", "recover"])
def test_prior_unknown_is_not_replayed_and_unassigned_gap_not_faked(mode):
    f = Fixture(mode)
    f.clear["close"] = False
    with pytest.raises(CN.CleanupHeld):
        f.engine().run()
    assert f.closes == f.removes == 0
    value = actor(mode)
    value["target_locator"]["run_id"] = value["run_id"] if mode == "recover" else 999
    with pytest.raises(P.KeyDBQualificationProvenanceHeld):
        CP.validate_actor(value)
    bad = target()
    bad.pop("dseq")
    with pytest.raises(P.KeyDBQualificationProvenanceHeld):
        CP.validate_target(binding(), bad)


def evaluate(raw, profile, outputs=None):
    raw = (
        raw.strip()
        .removeprefix("${{")
        .removesuffix("}}")
        .strip()
        .replace("inputs.runner-private-profile", repr(profile))
        .replace("&&", " and ")
        .replace("||", " or ")
        .replace("always()", "True")
        .replace("needs.pool.result", repr("skipped"))
        .replace("needs.pool.outputs.scanner-native-admitted", repr(""))
    )
    for key, value in (outputs or {}).items():
        raw = raw.replace(key, repr(value))
    return eval(raw, {"__builtins__": {}}, {})


def test_parsed_workflow_exactly_one_producer_and_typed_output_route_for_every_profile():
    wf = yaml.safe_load((SOURCE / ".github/workflows/runner-pool.yml").read_bytes())
    keys = ["pool", "scanner_v2", "keydb_parity_v1", "keydb_qualification_v1"]
    for profile, expected in [
        ("", "pool"),
        ("m01-private", "pool"),
        ("bb-native-trufflehog", "pool"),
        ("bb-native-trufflehog-v2", "scanner_v2"),
        ("bb-native-trufflehog-v2-reconciled", "scanner_v2"),
        ("blazing-keydb-modules-parity-v1", "keydb_parity_v1"),
        ("blazing-keydb-parity-qualification-v1", "keydb_qualification_v1"),
    ]:
        assert [key for key in keys if evaluate(wf["jobs"][key]["if"], profile)] == [expected]
        values = {
            f"jobs.{key}.outputs.dseq": ("PUBLIC_" + key if key == expected else "")
            for key in keys
        }
        assert (
            evaluate(wf[True]["workflow_call"]["outputs"]["dseq"]["value"], profile, values)
            == "PUBLIC_" + expected
        )
    qualification_outputs = wf["jobs"]["keydb_qualification_v1"]["outputs"]
    assert len(qualification_outputs) == 13
    for field in qualification_outputs:
        raw = wf[True]["workflow_call"]["outputs"][field]["value"]
        refs = re.findall(r"jobs\.[A-Za-z0-9_-]+\.outputs\.[A-Za-z0-9_-]+", raw)
        values = {
            key: (
                "PUBLIC_QUALIFICATION_" + field
                if key == "jobs.keydb_qualification_v1.outputs." + field
                else ""
            )
            for key in refs
        }
        assert (
            evaluate(raw, "blazing-keydb-parity-qualification-v1", values)
            == "PUBLIC_QUALIFICATION_" + field
        )
    assert not evaluate(wf["jobs"]["teardown"]["if"], "blazing-keydb-parity-qualification-v1")
    step = wf["jobs"]["keydb_qualification_v1"]["steps"][-1]
    assert (
        step["name"] == C.NATIVE_STEP
        and step["env"]["KEYDB_QUALIFICATION_SDK_SHA"] == "${{ inputs.just-akash-ref }}"
    )
    assert (
        step["env"]["KEYDB_QUALIFICATION_RUNNER_GROUP_ID"]
        == "${{ inputs.keydb-qualification-runner-group-id }}"
    )


def test_cleanup_workflow_exposes_only_real_manual_normal_or_recover():
    wf = yaml.safe_load(
        (SOURCE / ".github/workflows/keydb-qualification-cleanup.yml").read_bytes()
    )
    steps = wf["jobs"]["cleanup"]["steps"]
    gate = steps[0]["run"]
    assert "workflow_run" not in gate and "recover)" in gate and "normal)" in gate and REF in gate
    assert steps[-1]["name"] == CP.STEP
    assert steps[-1]["env"]["KEYDB_QUALIFICATION_CLEANUP_MODE"] == "${{ inputs.mode }}"


@pytest.mark.parametrize("fail", [None, "UNKNOWN", "ACK"])
def test_returned_ID_ACK_before_tenant_delivery_and_no_effect_replay(fail):
    import base64

    binding = P.KeyDBQualificationBinding(C.inputs(environment()))
    config = base64.b64encode(
        json.dumps(
            {
                ".runner": base64.b64encode(b'{"agentId":789}').decode(),
                ".credentials": base64.b64encode(b"PUBLIC_EPHEMERAL_JIT_CREDENTIAL").decode(),
            }
        ).encode()
    ).decode()

    class Recorder:
        def __init__(self):
            self.rows = []
            self.phase = None

        def persist(self, row):
            expected = {"NO_WIRE": None, "UNKNOWN": "NO_WIRE", "ACK": "UNKNOWN"}[row["phase"]]
            if self.phase != expected:
                raise P.KeyDBQualificationProvenanceHeld("PUBLIC_NO_REPLAY")
            self.phase = row["phase"]
            self.rows.append(copy.deepcopy(row))
            value = ack(row)
            if row["phase"] == fail:
                value["record_readback_sha256"] = "0" * 64
            return value

    recorder = Recorder()
    group, calls = group_fixture()
    posts = []

    def request(method, path, token, body=None):
        if method == "GET":
            return group(method, path, token)
        assert (
            recorder.rows[-1]["phase"] == "UNKNOWN"
            and path == J.ROOT + "/runners/generate-jitconfig"
        )
        posts.append(body)
        return dict(runner=dict(id=789, name=binding.runner_name), encoded_jit_config=config)

    def mint():
        return J.mint_keydb_qualification_jit(
            binding,
            "PUBLIC_HOSTED_TOKEN",
            recorder=recorder,
            request=request,
            before_post=lambda: None,
        )

    if fail:
        with pytest.raises((P.KeyDBQualificationProvenanceHeld, J.JitMintUnknown)):
            mint()
    else:
        result = mint()
        assert result.handoff.runner_id == 789
        assert J.tenant_environment(
            result, binding, image=J.PRIVATE_IMAGE, entrypoint_sha256=J.ENTRYPOINT_SHA256
        ) == dict(
            RUNNER_JIT_CONFIG=config,
            EPHEMERAL="true",
            RUNNER_EXECUTION_USER="root",
            RUNNER_NAME=binding.runner_name,
        )
        assert config not in repr(result)
    assert len(posts) == (0 if fail == "UNKNOWN" else 1)
    count = len(posts)
    with pytest.raises((P.KeyDBQualificationProvenanceHeld, J.JitMintUnknown)):
        mint()
    assert len(posts) == count and config not in json.dumps(recorder.rows)


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_known_DSEQ_failed_producer_with_explicit_unassigned_consumer_still_cleans(
    conclusion, tmp_path
):
    native = Native("normal")
    native.producer.update(conclusion=conclusion)
    native.producer["steps"][0]["conclusion"] = conclusion
    native.work.update(conclusion="skipped", runner_id=0, runner_name="")
    current = CN.actor(actor_environment(native, tmp_path), native)
    CN.producer(current, binding(), native, target())
    assert native.producer["conclusion"] == conclusion


@pytest.mark.parametrize(
    "change", [None, "partial", "nonterminal", "laterAttempt", "otherAssignment", "normal"]
)
def test_recover_absent_consumer_requires_complete_terminal_exact_latest_attempt(change, tmp_path):
    mode = "normal" if change == "normal" else "recover"
    native = Native(mode)
    env = actor_environment(native, tmp_path)
    current = CN.actor(env, native)

    def adjust(route, value):
        if "/jobs?" in route and "/runs/42/" in route:
            value["jobs"] = [row for row in value["jobs"] if row["id"] != 92]
            if change == "otherAssignment":
                value["jobs"].append(
                    dict(
                        id=99,
                        run_id=42,
                        run_attempt=2,
                        head_sha="a" * 40,
                        name="PUBLIC_UNEXPECTED_ASSIGNED_JOB",
                        status="completed",
                        conclusion="success",
                        runner_id=191,
                        runner_name="PUBLIC_ASSIGNED_191",
                    )
                )
            value["total_count"] = len(value["jobs"]) + (1 if change == "partial" else 0)
        if route.endswith("/actions/runs/42"):
            if change == "nonterminal":
                value.update(status="in_progress", conclusion=None)
            if change == "laterAttempt":
                value["run_attempt"] = 3
        return value

    native.change = adjust
    if change:
        with pytest.raises(CN.CleanupHeld):
            CN.producer(current, binding(), native, target())
    else:
        CN.producer(current, binding(), native, target())
        # The same real guard re-observes fresh complete terminal state on demand.
        native.change = lambda route, value: (
            adjust(route, dict(value, status="in_progress", conclusion=None))
            if route.endswith("/actions/runs/42")
            else adjust(route, value)
        )
        with pytest.raises(CN.CleanupHeld):
            CN.producer(current, binding(), native, target())


def test_all_real_public_phase_records_match_actual_hosted_JS_codec():
    import shutil
    import subprocess

    binding = P.KeyDBQualificationBinding(C.inputs(environment()))

    class Recorder:
        def __init__(self):
            self.rows = []

        def persist(self, row):
            self.rows.append(copy.deepcopy(row))
            return ack(row)

    recorder = Recorder()
    common = dict(
        owner=sorted(P.PROVIDERS)[0],
        returned_runner_id=191,
        jit_ack_record_sha256="1" * 64,
        public_request_sha256="2" * 64,
        wire_request_sha256="3" * 64,
    )
    for kind in ("jit", "lease", "lease_bind"):
        operation = P.KeyDBOperation(binding, kind, recorder)
        if kind == "jit":
            details = dict(
                runner_name=binding.runner_name,
                runner_group_id=99,
                labels=["self-hosted", "linux", "akash", binding.runner_label],
                request_body_sha256="4" * 64,
            )
            extra = dict(runner_id=191)
        elif kind == "lease":
            details = dict(
                **common,
                group_population=[dict(gseq=1, name=binding.akash_group)],
                deposit_usd="6",
            )
            extra = dict(dseq="300")
        else:
            details = dict(
                **common,
                dseq="300",
                provider=sorted(P.PROVIDERS)[0],
                gseq=1,
                deployment_ack_record_sha256="5" * 64,
            )
            extra = dict(lease_response_received=True)
        operation.no_wire(details)
        operation.before_wire(details)
        operation.ack(dict(details, **extra))
    schema = (
        Path(__file__).resolve().parents[1]
        / ".github/actions/keydb-qualification-durable-receipts/schema.cjs"
    )
    code = (
        "const fs=require('node:fs'),s=require(process.argv[1]);"
        "const a=JSON.parse(fs.readFileSync(0,'utf8'));"
        "process.stdout.write(JSON.stringify(a.map(r=>s.validate(r).sha256)));"
    )
    result = subprocess.run(
        [shutil.which("node"), "--no-warnings", "-e", code, str(schema)],
        input=json.dumps(recorder.rows),
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0 and json.loads(result.stdout) == [
        P.record_sha256(r) for r in recorder.rows
    ]
    assert len(recorder.rows) == 9 and {r["kind"] for r in recorder.rows} == {
        "jit",
        "lease",
        "lease_bind",
    }
