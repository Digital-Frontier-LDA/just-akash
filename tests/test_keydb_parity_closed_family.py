"""Own-role admission/phase controls; no native request or effect is executed."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from just_akash import keydb_parity_controller as controller  # noqa: E402
from just_akash import keydb_parity_jit as jit  # noqa: E402
from just_akash import keydb_parity_provenance as protocol  # noqa: E402


def environment():
    exact = {
        "PUBLIC_PROFILE": "",
        "IMAGE": "",
        "REGISTRY_HOST": "https://index.docker.io/v1/",
        "REGISTRY_USERNAME": "jobordu",
        "REGISTRY_SOPS": "true",
        "NATIVE_PULL_READER": "false",
        "NATIVE_REPOSITORY_SCOPE": "false",
        "CREDENTIALS_SOPS": "false",
        "EPHEMERAL": "true",
        "POOL_SIZE": "1",
        "MIN_POOL_SIZE": "1",
        "CPU": "4",
        "MEMORY": "16Gi",
        "STORAGE": "96Gi",
        "SELECT": "cheapest",
        "ORG": "Borduas-Holdings",
        "DIRECT_PASSWORD_PRESENT": "false",
    }
    env = {"KEYDB_PARITY_" + k: v for k, v in exact.items()}
    env.update(
        RUNNER_PRIVATE_PROFILE=controller.PROFILE,
        GITHUB_REPOSITORY="Borduas-Holdings/blazing",
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_REF="refs/heads/main",
        RUNNER_ENVIRONMENT="github-hosted",
        GITHUB_JOB="keydb_parity_v1",
        GITHUB_SHA="a" * 40,
        GITHUB_WORKFLOW_SHA="a" * 40,
        GITHUB_WORKFLOW_REF="Borduas-Holdings/blazing/.github/workflows/keydb-modules-parity.yml@refs/heads/main",
        GITHUB_RUN_ID="42",
        GITHUB_RUN_ATTEMPT="2",
        KEYDB_PARITY_SDK_ROUND="1",
        KEYDB_PARITY_SDK_SHA="b" * 40,
        KEYDB_PARITY_PRODUCER_JOB_ID="77",
        KEYDB_PARITY_RUNNER_GROUP_ID="99",
    )
    for k in ("PROVIDERS", "OWNED_PROVIDERS"):
        env["KEYDB_PARITY_" + k] = json.dumps(sorted(protocol.PROVIDERS))
    return env


def test_exact_new_family_binds_Blazing_and_cannot_borrow_previous_roles():
    env = environment()
    binding = protocol.KeyDBBinding(controller.inputs(env))
    row = binding.to_dict()
    assert row["repository_id"] == 1074974924 and row["role"] == "blazing-keydb-modules-parity-v1"
    assert (
        row["runner_name"] == "dfci-bl-keydb-42-a2-r1"
        and row["akash_group"] == "borduas-keydb-parity-run-42-a2-r1-end"
    )
    for key, value in [
        ("RUNNER_PRIVATE_PROFILE", "bb-native-trufflehog-v2"),
        ("RUNNER_PRIVATE_PROFILE", "m06-owned-orchestrator-boot/v1"),
        ("GITHUB_REPOSITORY", "Borduas-Holdings/Blazing-Back"),
        (
            "GITHUB_WORKFLOW_REF",
            "Borduas-Holdings/blazing/.github/workflows/orchestrator-image-boot.yml@refs/heads/main",
        ),
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_JOB", "scanner_v2"),
    ]:
        changed = dict(env)
        changed[key] = value
        with pytest.raises(controller.ControllerHeld):
            controller.inputs(changed)


def group_fixture(change=None):
    calls = []

    def request(method, path, token, *args):
        calls.append((method, path))
        if "/repositories?" in path:
            rows = (
                []
                if path.endswith("page=2")
                else [dict(id=1074974924, full_name="Borduas-Holdings/blazing", private=True)]
            )
            if change == "BBrepository" and rows:
                rows[0].update(id=1071436278, full_name="Borduas-Holdings/Blazing-Back")
            return dict(total_count=1, repositories=rows)
        row = dict(
            id=99,
            name=jit.RUNNER_GROUP_NAME,
            visibility="selected",
            allows_public_repositories=False,
            restricted_to_workflows=True,
            selected_workflows=[jit.ROOT_WORKFLOW],
        )
        if change == "BBgroup":
            row["name"] = "bb-native-trufflehog-v2"
        if change == "wrongWorkflow":
            row["selected_workflows"] = [
                "Borduas-Holdings/blazing/.github/workflows/owned-keydb-podman.yml@refs/heads/main"
            ]
        return row

    return request, calls


def test_exact_private_group_complete_membership_and_two_read_policy():
    request, calls = group_fixture()
    jit.verify_group(99, request, "PUBLIC_SYNTHETIC")
    assert len(calls) == 4 and all(method == "GET" for method, _ in calls)


@pytest.mark.parametrize("change", ["BBrepository", "BBgroup", "wrongWorkflow"])
def test_foreign_closed_groups_never_allow_mint(change):
    request, calls = group_fixture(change)
    with pytest.raises(jit.JitHold):
        jit.verify_group(99, request, "PUBLIC_SYNTHETIC")
    assert all(method == "GET" for method, _ in calls)


def ack(record):
    return dict(
        schema="just-akash/keydb-parity-durable-ack/v1",
        backend="github-actions-artifact/v4",
        operation_id=record["operation_id"],
        phase=record["phase"],
        parent_record_sha256=record["parent_record_sha256"],
        record_sha256=protocol.record_sha256(record),
        record_readback_sha256=protocol.record_sha256(record),
        artifact_id=881,
        artifact_digest="sha256:" + "e" * 64,
        producer=protocol.producer_identity(record["binding"]),
        exclusive=True,
        created=True,
    )


def test_jit_effect_never_reached_without_durable_prewire_and_new_role():
    binding = protocol.KeyDBBinding(controller.inputs(environment()))
    calls = []

    def request(*args):
        calls.append(args)
        pytest.fail("no native access without recorder")

    with pytest.raises(protocol.KeyDBProvenanceHeld):
        jit.mint_keydb_parity_jit(
            binding, "PUBLIC_HOSTED", before_post=lambda: None, request=request
        )
    assert calls == []


def test_new_family_phase_ACK_never_replays_existing_unknown():
    class Recorder:
        def __init__(self):
            self.rows = []

        def persist(self, row):
            protocol.validate_record(row)
            self.rows.append(copy.deepcopy(row))
            return ack(row)

    binding = protocol.KeyDBBinding(controller.inputs(environment()))
    recorder = Recorder()
    operation = protocol.KeyDBOperation(binding, "jit", recorder)
    details = dict(
        runner_name=binding.runner_name,
        runner_group_id=99,
        labels=["self-hosted", "linux", "akash", binding.runner_label],
        request_body_sha256="d" * 64,
    )
    operation.no_wire(details)
    operation.before_wire(details)
    unknown = operation.public_record
    with pytest.raises(protocol.KeyDBProvenanceHeld):
        operation.before_wire(details)
    assert operation.public_record == unknown and [r["phase"] for r in recorder.rows] == [
        "NO_WIRE",
        "UNKNOWN",
    ]


def test_actual_workflow_selects_exactly_one_producer_and_no_legacy_rollback_for_KeyDB():
    import yaml

    source = Path(__file__).resolve().parents[1] / ".github/workflows/runner-pool.yml"
    wf = yaml.safe_load(source.read_bytes())

    def evaluate(raw, profile, outputs=None):
        raw = raw.strip().removeprefix("${{").removesuffix("}}").strip()
        raw = (
            raw.replace("inputs.runner-private-profile", repr(profile))
            .replace("&&", " and ")
            .replace("||", " or ")
            .replace("always()", "True")
        )
        raw = raw.replace("needs.pool.result", repr("skipped")).replace(
            "needs.pool.outputs.scanner-native-admitted", repr("")
        )
        if outputs:
            for key, value in outputs.items():
                raw = raw.replace(key, repr(value))
        return eval(raw, {"__builtins__": {}}, {})

    for profile, expected in [
        ("", "pool"),
        ("m01-current-profile", "pool"),
        ("bb-native-trufflehog", "pool"),
        ("bb-native-trufflehog-v2", "scanner_v2"),
        ("bb-native-trufflehog-v2-reconciled", "scanner_v2"),
        ("blazing-keydb-modules-parity-v1", "keydb_parity_v1"),
    ]:
        selected = [
            j
            for j in ("pool", "scanner_v2", "keydb_parity_v1")
            if evaluate(wf["jobs"][j]["if"], profile)
        ]
        assert selected == [expected]
        values = {
            f"jobs.{j}.outputs.dseq": ("PUBLIC_" + j if j == expected else "")
            for j in ("pool", "scanner_v2", "keydb_parity_v1")
        }
        assert (
            evaluate(wf[True]["workflow_call"]["outputs"]["dseq"]["value"], profile, values)
            == "PUBLIC_" + expected
        )
    assert evaluate(wf["jobs"]["teardown"]["if"], "blazing-keydb-modules-parity-v1") is False
    controller_step = wf["jobs"]["keydb_parity_v1"]["steps"][-1]
    assert controller_step["uses"] == "./.just-akash/.github/actions/keydb-parity-durable-receipts"
    assert (
        controller_step["env"]["KEYDB_PARITY_RUNNER_GROUP_ID"]
        == "${{ inputs.keydb-parity-runner-group-id }}"
    )
    assert controller_step["name"] == controller.NATIVE_STEP


@pytest.mark.parametrize(
    "echo", ["PUBLIC_JIT_MEMORY", "PUBLIC_READER_MEMORY", "PUBLIC_API_MEMORY", "f" * 64]
)
def test_actual_22be_prepare_hook_and_R2_local_receipt_projection_stay_compatible(tmp_path, echo):
    from just_akash import deployment_receipt as local
    from just_akash import keydb_parity_lease as lease

    binding = protocol.KeyDBBinding(controller.inputs(environment()))

    class Recorder:
        def __init__(self):
            self.rows = []

        def persist(self, row):
            protocol.validate_record(row)
            self.rows.append(copy.deepcopy(row))
            return ack(row)

    recorder = Recorder()
    op = protocol.KeyDBOperation(binding, "jit", recorder)
    details = dict(
        runner_name=binding.runner_name,
        runner_group_id=99,
        labels=["self-hosted", "linux", "akash", binding.runner_label],
        request_body_sha256="d" * 64,
    )
    op.no_wire(details)
    op.before_wire(details)
    jit_ack = op.ack(dict(details, runner_id=191))
    p = lease.KeyDBLeaseProvenance(
        binding=binding,
        returned_runner_id=191,
        jit_ack_record=op.public_record,
        jit_ack=jit_ack,
        encoded_jit_config="PUBLIC_JIT_MEMORY",
        recorder=recorder,
        admission=lambda: None,
    )
    path = tmp_path / "private.yaml"
    controller.write_sdl(
        path,
        binding,
        dict(
            RUNNER_JIT_CONFIG="PUBLIC_JIT_MEMORY",
            EPHEMERAL="true",
            RUNNER_EXECUTION_USER="root",
            RUNNER_NAME=binding.runner_name,
        ),
        "PUBLIC_READER_MEMORY",
    )
    raw = path.read_text()
    owner = "akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z"
    receipt = local.prepare_receipt(
        str(tmp_path / "local.json"),
        operation_id=p.operation.operation_id,
        sdl_content=raw,
        owner=owner,
    )

    class Client:
        api_key = "PUBLIC_API_MEMORY"

        def account_address(self):
            return owner

        def create_deployment(self, sdl_content, deposit):
            return dict(dseq="300", txHash=echo)

    client = Client()
    p.prepare(receipt, raw, client=client)
    p.bound_client(client).create_deployment(raw, deposit=6)
    safe = p.local_create_response("300", dict(dseq="300", txHash=echo))
    assert safe["dseq"] == "300"
    if echo == "f" * 64:
        assert safe["txHash"] == echo
    else:
        assert set(safe) == {"dseq"}
    p.created("300")
    assert (
        p.operation.public_record["kind"] == "lease"
        and p.operation.public_record["phase"] == "ACK"
    )
    assert all(
        x not in json.dumps(safe)
        for x in ["PUBLIC_JIT_MEMORY", "PUBLIC_READER_MEMORY", "PUBLIC_API_MEMORY"]
    )


@pytest.mark.parametrize("fail", [None, "UNKNOWN", "ACK"])
def test_returned_ID_ACK_before_tenant_delivery_and_no_effect_replay(fail):
    import base64

    binding = protocol.KeyDBBinding(controller.inputs(environment()))
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
                raise protocol.KeyDBProvenanceHeld("PUBLIC_NO_REPLAY")
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
            and path == jit.ROOT + "/runners/generate-jitconfig"
        )
        posts.append(body)
        return dict(runner=dict(id=789, name=binding.runner_name), encoded_jit_config=config)

    def mint():
        return jit.mint_keydb_parity_jit(
            binding,
            "PUBLIC_HOSTED_TOKEN",
            recorder=recorder,
            request=request,
            before_post=lambda: None,
        )

    if fail:
        with pytest.raises((protocol.KeyDBProvenanceHeld, jit.JitMintUnknown)):
            mint()
    else:
        result = mint()
        assert result.handoff.runner_id == 789
        assert jit.tenant_environment(
            result, binding, image=jit.PRIVATE_IMAGE, entrypoint_sha256=jit.ENTRYPOINT_SHA256
        ) == dict(
            RUNNER_JIT_CONFIG=config,
            EPHEMERAL="true",
            RUNNER_EXECUTION_USER="root",
            RUNNER_NAME=binding.runner_name,
        )
        assert config not in repr(result)
    assert len(posts) == (0 if fail == "UNKNOWN" else 1)
    count = len(posts)
    with pytest.raises((protocol.KeyDBProvenanceHeld, jit.JitMintUnknown)):
        mint()
    assert len(posts) == count and config not in json.dumps(recorder.rows)


@pytest.mark.parametrize(
    "change", [None, "mainmove", "wrongSDK", "skippedJob", "wrongRunner", "wrongOwner"]
)
def test_actual_hosted_native_admission_revalidates_source_and_executing_job(monkeypatch, change):
    from just_akash import keydb_parity_native

    env = environment()
    env["RUNNER_NAME"] = "PUBLIC_HOSTED_RUNNER"
    binding = protocol.KeyDBBinding(controller.inputs(env))
    source = "a" * 40
    sdk = "b" * 40
    job = dict(
        id=77,
        run_id=42,
        run_attempt=2,
        head_sha=source,
        name=controller.NATIVE_JOB,
        status="in_progress",
        conclusion=None,
        runner_name=env["RUNNER_NAME"],
        runner_id=123,
        labels=["ubuntu-latest"],
        steps=[dict(name=controller.NATIVE_STEP, status="in_progress", conclusion=None)],
    )
    run = dict(
        id=42,
        run_attempt=2,
        event="workflow_dispatch",
        path=controller.WORKFLOW,
        head_sha=source,
        head_branch="main",
        status="in_progress",
        conclusion=None,
        repository=dict(id=1074974924),
        head_repository=dict(id=1074974924),
        referenced_workflows=[
            dict(
                path=controller.SDK_REPOSITORY + "/" + controller.SDK_WORKFLOW + "@" + sdk, sha=sdk
            )
        ],
    )
    repo = dict(
        id=1074974924,
        full_name=controller.REPOSITORY,
        private=True,
        owner=dict(id=208689568, login="Borduas-Holdings"),
    )
    main = dict(object=dict(sha=source))
    if change == "mainmove":
        main["object"]["sha"] = "c" * 40
    elif change == "wrongSDK":
        run["referenced_workflows"][0]["sha"] = "c" * 40
    elif change == "skippedJob":
        job["status"] = "completed"
        job["conclusion"] = "skipped"
    elif change == "wrongRunner":
        job["runner_name"] = "PUBLIC_OTHER"
    elif change == "wrongOwner":
        repo["owner"]["id"] = 1

    def get(route):
        if route == controller.ROOT:
            return copy.deepcopy(repo)
        if route.endswith("/git/ref/heads/main"):
            return copy.deepcopy(main)
        if "/jobs?" in route:
            return dict(total_count=1, jobs=[copy.deepcopy(job)])
        if "/actions/jobs/" in route:
            return copy.deepcopy(job)
        return copy.deepcopy(run)

    group = []
    monkeypatch.setattr(keydb_parity_native, "NativeGET", lambda token: get)
    monkeypatch.setattr(controller, "verify_group", lambda *args: group.append(args[0]))
    if change:
        with pytest.raises(controller.ControllerHeld):
            controller.admit_native(binding, env, "PUBLIC_HOSTED_TOKEN")
        assert group == []
    else:
        controller.admit_native(binding, env, "PUBLIC_HOSTED_TOKEN")
        assert group == [99]


def test_all_real_public_phase_records_match_actual_hosted_JS_codec():
    import shutil
    import subprocess

    binding = protocol.KeyDBBinding(controller.inputs(environment()))

    class Recorder:
        def __init__(self):
            self.rows = []

        def persist(self, row):
            self.rows.append(copy.deepcopy(row))
            return ack(row)

    recorder = Recorder()
    common = dict(
        owner=sorted(protocol.PROVIDERS)[0],
        returned_runner_id=191,
        jit_ack_record_sha256="1" * 64,
        public_request_sha256="2" * 64,
        wire_request_sha256="3" * 64,
    )
    for kind in ("jit", "lease", "lease_bind"):
        operation = protocol.KeyDBOperation(binding, kind, recorder)
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
                provider=sorted(protocol.PROVIDERS)[0],
                gseq=1,
                deployment_ack_record_sha256="5" * 64,
            )
            extra = dict(lease_response_received=True)
        operation.no_wire(details)
        operation.before_wire(details)
        operation.ack(dict(details, **extra))
    schema = (
        Path(__file__).resolve().parents[1]
        / ".github/actions/keydb-parity-durable-receipts/schema.cjs"
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
        protocol.record_sha256(r) for r in recorder.rows
    ]
    assert len(recorder.rows) == 9 and {r["kind"] for r in recorder.rows} == {
        "jit",
        "lease",
        "lease_bind",
    }
