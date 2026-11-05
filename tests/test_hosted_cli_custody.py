"""New closed custody controls: real installed SDK, signatures, FDs and HTTPX mocks."""

import base64
import dataclasses
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_hosted_cli import scene as scene

from just_akash import guardian_legacy_lease_backend as backend
from just_akash import hosted_cli_custody as custody
from just_akash import legacy_capacity_cleanup as cleanup
from just_akash.hosted_legacy_cli import HostedCallResult

REAL_CLIENT = httpx.Client


@pytest.fixture
def world(tmp_path, monkeypatch):
    clock = SimpleNamespace(mono=100.0, utc=1800000001)
    monkeypatch.setattr(custody.time, "monotonic", lambda: clock.mono)
    monkeypatch.setattr(custody.time, "time", lambda: clock.utc)
    key = Ed25519PrivateKey.from_private_bytes(bytes([17]) * 32)  # Public synthetic fixture ONLY.
    policy = backend.ReceiptPolicy(
        "9" * 40, "closed-broker", "closed-key", key.public_key().public_bytes_raw(), 60
    )
    reg = custody.CustodyRegistration(
        policy,
        "deploy",
        "Borduas-Holdings/blazing",
        123,
        2,
        "a" * 40,
        ".github/workflows/owned-private-podman-images.yml",
        "b" * 40,
        "c" * 40,
        next(iter(sorted(backend.SDK_PARENTS))),
        "owned-private-images-123-2",
        "signed-consume-123-2",
        tmp_path / "private-custody",
        900.0,
        lambda: "PUBLIC_SYNTHETIC_READ",
    )
    state = SimpleNamespace(
        reg=reg,
        key=key,
        clock=clock,
        requests=[],
        options=[],
        closes=0,
        body_mutation=lambda kind, value: value,
        header_mutation=lambda headers: headers,
        response_origin=None,
        failure=None,
        late_close=False,
    )
    monkeypatch.setattr(custody, "_reviewed_hosted_custody", lambda: state.reg)

    class Stream(httpx.SyncByteStream):
        def __init__(self, raw):
            self.raw = raw

        def __iter__(self):
            yield self.raw

        def close(self):
            state.closes += 1
            if state.late_close:
                state.clock.mono = 1000.0

    def handler(request):
        state.requests.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer PUBLIC_SYNTHETIC_READ"
        assert request.url.host == "api.github.com"
        assert request.headers["accept-encoding"] == "identity"
        if state.failure:
            raise httpx.ReadError("SECRET_TRANSPORT_SENTINEL")
        if request.url.path.endswith("/actions/artifacts/7"):
            kind = "artifact"
            value = dict(
                id=7,
                name=reg.artifact_name,
                digest="sha256:" + "d" * 64,
                expired=False,
                size_in_bytes=1000,
                workflow_run=dict(
                    id=123,
                    repository_id=1074974924,
                    head_repository_id=1074974924,
                    head_sha="a" * 40,
                ),
            )
        else:
            assert request.url.path.endswith("/actions/runs/123/attempts/2")
            kind = "attempt"
            value = dict(
                id=123,
                run_attempt=2,
                head_sha="a" * 40,
                event="workflow_dispatch",
                path=reg.caller_workflow,
                status="in_progress",
                repository=dict(
                    id=1074974924,
                    full_name=reg.repository,
                    private=True,
                    owner=dict(id=208689568, login="Borduas-Holdings", type="Organization"),
                ),
            )
        value = state.body_mutation(kind, value)
        raw = value if type(value) is bytes else backend.canonical(value)
        headers = [("content-type", "application/json"), ("content-length", str(len(raw)))]
        headers = state.header_mutation(headers)
        response_request = (
            httpx.Request("GET", state.response_origin) if state.response_origin else request
        )

        class ForeignOriginResponse(httpx.Response):
            @property
            def url(self):
                return httpx.URL(state.response_origin)

        response_type = ForeignOriginResponse if state.response_origin else httpx.Response
        return response_type(200, headers=headers, stream=Stream(raw), request=response_request)

    def client(**kwargs):
        state.options.append(kwargs)
        return REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(custody.httpx, "Client", client)
    identity = dict(
        chain_id="akashnet-2",
        owner="akash14n4rkmz64rn0tey0r5g07l8q5x0fh2h4hu44kt",
        dseq="12345",
        provider="akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk",
        gseq=1,
        oseq=1,
        body_sha256="b" * 64,
        manifest_sha256="c" * 64,
        source_revision="a" * 40,
        sdk_parent_ref=reg.sdk_parent_ref,
        api_sha256=backend.API_SHA256,
        hook_sha256=backend.HOOK_SHA256,
    )
    claims = dict(
        schema=backend.SCHEMA,
        audience="legacy-lease-boundary-observation",
        broker_identity=policy.broker_identity,
        key_id=policy.key_id,
        policy_revision=policy.revision,
        phase="consume",
        parent_digest="a" * 64,
        parent_revision=2,
        parent_revision_digest="b" * 64,
        provider_revision=11,
        provider_digest="c" * 64,
        operation_id=reg.operation_id,
        attempt=1,
        attempt_id="a" * 64 + ".1",
        binding_digest="d" * 64,
        capacity_evidence_digest="e" * 64,
        placement_state="unknown",
        predecessor_sha256="f" * 64,
        identity=identity,
        original_deadline_at=1800000900,
        observed_at=clock.utc,
        valid_until=clock.utc + 30,
    )

    def sign(data, domain=backend.DOMAIN):
        body = backend.canonical(data)
        return backend.canonical(
            dict(
                payload=base64.b64encode(body).decode(),
                signature=base64.b64encode(key.sign(domain + body)).decode(),
            )
        )

    state.claims, state.sign = claims, sign
    state.wire = sign(claims)
    # Actual decoder must accept the signature; a constructor is not the fixture's proof.
    assert cleanup.historical_consume(state.wire, policy)["phase"] == "consume"
    state.completion = SimpleNamespace(kind="system-exit", exit_status=0, locator_unknown=False)

    def finish(unknown=False):
        custody.prepare()
        result = HostedCallResult(
            "deploy", entered=True, consume_locators=(state.wire,), locator_custody_unknown=unknown
        )
        custody.capture(result, state.completion, 900.0)
        state.reg = dataclasses.replace(state.reg, purpose="destroy", consume_wire=state.wire)

    state.finish = finish
    return state


def test_real_signed_capture_native_exactattempt_and_historical_expiry(world):
    world.finish()
    world.clock.utc += 1000  # Old consume is a locator, never renewed send authority.
    assert custody.read_consumes(7, "sha256:" + "d" * 64) == (world.wire,)
    assert len(world.requests) == 2 and world.closes == 2
    assert world.requests[1].url.path.endswith("/runs/123/attempts/2")
    assert all(
        not x["trust_env"] and not x["follow_redirects"] and x["verify"] for x in world.options
    )
    assert all(x["timeout"].read == 5 for x in world.options)
    raw = (world.reg.directory / custody.BUNDLE).read_bytes()
    assert b"PUBLIC_SYNTHETIC_READ" not in raw and b"SECRET" not in raw


def test_default_none_has_no_client_or_credential_access(world, monkeypatch, capsys):
    monkeypatch.setattr(custody, "_reviewed_hosted_custody", lambda: None)
    with pytest.raises(custody.CustodyHeld):
        custody.prepare()
    with pytest.raises(custody.CustodyHeld):
        custody.read_consumes(7, "sha256:" + "d" * 64)
    assert not world.requests and not world.options
    monkeypatch.setattr("sys.argv", ["custody", "prepare"])
    assert custody.main() == 78
    assert "UNKNOWN" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["begun", "no-result", "lost-ack", "duplicate", "reenter"])
def test_missing_partial_or_replayed_capture_never_means_zero(world, kind):
    custody.prepare()
    if kind == "begun":
        pass
    elif kind == "no-result":
        custody.capture(None, world.completion, 900.0)
    else:
        values = (world.wire, world.wire) if kind == "duplicate" else (world.wire,)
        result = HostedCallResult(
            "deploy",
            entered=True,
            consume_locators=values,
            locator_custody_unknown=kind == "lost-ack",
        )
        if kind == "duplicate":
            with pytest.raises(custody.CustodyHeld):
                custody.capture(result, world.completion, 900.0)
        else:
            custody.capture(result, world.completion, 900.0)
            if kind == "reenter":
                with pytest.raises(custody.CustodyHeld):
                    custody.capture(result, world.completion, 900.0)
                return
    world.reg = dataclasses.replace(world.reg, purpose="destroy", consume_wire=world.wire)
    with pytest.raises(custody.CustodyHeld):
        custody.read_consumes(7, "sha256:" + "d" * 64)
    assert len(world.requests) == 2


@pytest.mark.parametrize(
    "field",
    [
        "source_revision",
        "api_sha256",
        "hook_sha256",
        "sdk_parent_ref",
        "operation_id",
        "signature",
    ],
)
def test_wrong_signed_identity_or_signature_refuses_before_publish(world, field):
    custody.prepare()
    claims = json.loads(json.dumps(world.claims))
    if field in claims["identity"]:
        claims["identity"][field] = "0" * (
            40 if field in {"source_revision", "sdk_parent_ref"} else 64
        )
    elif field == "operation_id":
        claims[field] = "other-operation"
    wire = world.sign(claims)
    if field == "signature":
        wire = wire.replace(b'"signature":"', b'"signature":"A', 1)
    result = HostedCallResult("deploy", entered=True, consume_locators=(wire,))
    with pytest.raises((custody.CustodyHeld, cleanup.CleanupUnknown)):
        custody.capture(result, world.completion, 900.0)
    assert json.loads((world.reg.directory / custody.BUNDLE).read_bytes())["state"] == "begun"


@pytest.mark.parametrize(
    "field", ["id", "name", "digest", "expired", "size", "run", "repo", "head-repo", "head"]
)
def test_wrong_native_artifact_refuses_without_attempt_fallback(world, field):
    world.finish()

    def mutate(kind, value):
        if kind == "artifact":
            if field in {"id", "name", "digest", "expired"}:
                value[field] = {
                    "id": True,
                    "name": "other",
                    "digest": "sha256:" + "0" * 64,
                    "expired": True,
                }[field]
            elif field == "size":
                value["size_in_bytes"] = custody.MAX_BYTES + 1
            else:
                key = {
                    "run": "id",
                    "repo": "repository_id",
                    "head-repo": "head_repository_id",
                    "head": "head_sha",
                }[field]
                value["workflow_run"][key] = 1 if field != "head" else "0" * 40
        return value

    world.body_mutation = mutate
    with pytest.raises(custody.CustodyHeld):
        custody.read_consumes(7, "sha256:" + "d" * 64)
    assert len(world.requests) == 1


@pytest.mark.parametrize(
    "field", ["id", "attempt", "head", "event", "path", "repo", "private", "owner", "status"]
)
def test_native_exactattempt_wrong_fields_no_latest_or_PRshortcut(world, field):
    world.finish()

    def mutate(kind, value):
        if kind == "attempt":
            if field == "attempt":
                value["run_attempt"] = 3
            elif field in {"id", "head", "event", "path", "status"}:
                key = {
                    "id": "id",
                    "head": "head_sha",
                    "event": "event",
                    "path": "path",
                    "status": "status",
                }[field]
                value[key] = {
                    "id": True,
                    "head": "0" * 40,
                    "event": "pull_request",
                    "path": "other.yml",
                    "status": "queued",
                }[field]
            elif field == "repo":
                value["repository"]["id"] = 1
            elif field == "private":
                value["repository"]["private"] = False
            else:
                value["repository"]["owner"]["id"] = True
            value["pull_requests"] = []  # Never used for source/repository identity.
        return value

    world.body_mutation = mutate
    with pytest.raises(custody.CustodyHeld):
        custody.read_consumes(7, "sha256:" + "d" * 64)
    assert len(world.requests) == 2


@pytest.mark.parametrize(
    "kind",
    [
        "link",
        "duplicate-link",
        "duplicate-length",
        "length",
        "mixed-framing",
        "encoding",
        "age",
        "header-control",
        "header-bound",
        "duplicate-json",
        "overflow-json",
        "wrong-origin",
        "transport",
        "late-close",
    ],
)
def test_same_response_transport_and_retirement_refusals(world, kind):
    world.finish()
    if kind == "duplicate-json":
        world.body_mutation = lambda name, value: b'{"id":7,"id":7}'
    elif kind == "overflow-json":
        world.body_mutation = lambda name, value: b'{"unused":1e999}'
    elif kind == "wrong-origin":
        world.response_origin = "https://example.invalid/"
    elif kind == "transport":
        world.failure = True
    elif kind == "late-close":
        world.late_close = True
    else:

        def headers(values):
            if kind == "link":
                return values + [("Link", "<https://api.github.com/next>")]
            if kind == "duplicate-link":
                return values + [("Link", ""), ("link", "")]
            if kind == "duplicate-length":
                return values + [values[1]]
            if kind == "length":
                return [values[0], ("content-length", "1")]
            if kind == "mixed-framing":
                return values + [("Transfer-Encoding", "chunked")]
            if kind == "encoding":
                return values + [("content-encoding", "gzip")]
            if kind == "age":
                return values + [("Age", "1")]
            if kind == "header-control":
                return values + [("X-Test", "line\rbreak")]
            return values + [("X-Test", "x" * custody.MAX_HEADERS)]

        world.header_mutation = headers
    with pytest.raises(custody.CustodyHeld, match="^HOSTED_CUSTODY_UNKNOWN_NO_RETRY$"):
        custody.read_consumes(7, "sha256:" + "d" * 64)
    assert len(world.requests) == 1


@pytest.mark.parametrize(
    "kind",
    [
        "extra",
        "symlink-file",
        "symlink-dir",
        "hardlink",
        "duplicate-json",
        "noncanonical",
        "changed-context",
        "truncated",
        "unfinished",
        "mode",
    ],
)
def test_private_extraction_exact_regular_bundle_population(world, tmp_path, kind):
    world.finish()
    path = world.reg.directory / custody.BUNDLE
    if kind == "extra":
        (world.reg.directory / "extra").write_text("public")
    elif kind == "symlink-file":
        copy = tmp_path / "copy"
        copy.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(copy)
    elif kind == "symlink-dir":
        alias = tmp_path / "alias"
        alias.symlink_to(world.reg.directory, target_is_directory=True)
        world.reg = dataclasses.replace(world.reg, directory=alias)
    elif kind == "hardlink":
        os.link(path, tmp_path / "hardlink")
    elif kind == "mode":
        world.reg.directory.chmod(0o755)
    elif kind == "duplicate-json":
        path.write_bytes(b'{"schema":"x","schema":"y"}')
    elif kind == "truncated":
        path.write_bytes(path.read_bytes()[:-1])
    else:
        data = json.loads(path.read_bytes())
        if kind == "changed-context":
            data["context"]["callee_sha"] = "0" * 40
        if kind == "unfinished":
            data["state"] = "begun"
        path.write_bytes(
            json.dumps(data).encode() if kind == "noncanonical" else backend.canonical(data)
        )
    with pytest.raises(custody.CustodyHeld):
        custody.read_consumes(7, "sha256:" + "d" * 64)
    assert len(world.requests) == 2


def test_deadline_source_rejoin_and_finite_error_sanitization(world, monkeypatch, capsys):
    world.finish()
    with pytest.raises(custody.CustodyHeld):
        custody.read_consumes(7, "sha256:" + "d" * 64, end=99)
    assert not world.requests
    world.failure = True
    with pytest.raises(custody.CustodyHeld) as failure:
        custody.read_consumes(7, "sha256:" + "d" * 64)
    assert "SECRET" not in str(failure.value)
    monkeypatch.setattr("sys.argv", ["custody", "assert-closed"])
    assert custody.main() == 78
    assert "SECRET" not in capsys.readouterr().out


def test_capture_cancellation_is_not_rewritten_and_directory_lock_is_once(world, monkeypatch):
    custody.prepare()
    result = HostedCallResult("deploy", entered=True, consume_locators=(world.wire,))
    held = custody._directory(world.reg)
    try:
        with pytest.raises(BlockingIOError):
            custody.capture(result, world.completion, 900.0)
    finally:
        os.close(held)
    original = custody.os.fsync

    def cancelled(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(custody.os, "fsync", cancelled)
    with pytest.raises(KeyboardInterrupt):
        custody.capture(result, world.completion, 900.0)
    monkeypatch.setattr(custody.os, "fsync", original)


def test_only_actual_signed_cleanup_observation_can_publish_shared_closed(world):
    world.finish()
    consume = world.claims
    claims = dict(
        schema=cleanup.SCHEMA,
        audience="akash-legacy-capacity-closure-observation",
        key_id=world.reg.receipt_policy.key_id,
        verifier_policy_revision=world.reg.receipt_policy.revision,
        broker_identity=world.reg.receipt_policy.broker_identity,
        phase="close",
        identity=consume["identity"],
        operation_id=consume["operation_id"],
        attempt=consume["attempt"],
        attempt_id=consume["attempt_id"],
        parent_digest=consume["parent_digest"],
        parent_revision=3,
        parent_revision_digest="b" * 64,
        provider_revision=11,
        provider_revision_digest="c" * 64,
        binding_digest=consume["binding_digest"],
        predecessor_sha256=backend.sha(world.wire),
        close_tx_hash="A" * 64,
        closure_evidence_digest="d" * 64,
        placement_state="closed",
        execution_closed=True,
        settlement_proven=False,
        original_deadline_at=consume["original_deadline_at"],
        observed_at=world.clock.utc,
        valid_until=world.clock.utc + 30,
    )
    wire = world.sign(claims, cleanup.DOMAIN)
    observation = cleanup.decode_closed(
        wire, world.reg.receipt_policy, world.wire, consume, "A" * 64, now=world.clock.utc
    )
    result = HostedCallResult("destroy", entered=True, cleanup_observation=observation)
    custody.capture(result, world.completion, 900.0)
    custody.assert_closed()
    world.clock.utc += 31
    with pytest.raises(cleanup.CleanupUnknown):
        custody.assert_closed()


def test_no_signed_cleanup_or_forged_flags_never_yields_success(world):
    world.finish()
    with pytest.raises((custody.CustodyHeld, FileNotFoundError)):
        custody.assert_closed()
    with pytest.raises(custody.CustodyHeld):
        custody.capture(HostedCallResult("destroy", entered=True), world.completion, 900.0)
    assert not (world.reg.directory / custody.CLOSED).exists()


def _workflows():
    import yaml

    base = Path(__file__).parents[1] / ".github/workflows"
    return {
        name: yaml.load((base / name).read_text(), Loader=yaml.BaseLoader)
        for name in ("runner-pool.yml", "runner-teardown.yml")
    }


def test_new_workflow_permissions_private_artifact_and_sole_caller_cleanup_contract():
    workflows = _workflows()
    pool, teardown = workflows["runner-pool.yml"], workflows["runner-teardown.yml"]
    for wf, job in ((pool, "pool"), (teardown, "teardown")):
        assert wf["on"]["workflow_call"]["inputs"]["shared-admission"]["default"] == "false"
        assert wf["jobs"][job]["permissions"] == {
            "contents": "read",
            "actions": "read",
            "id-token": "write",
        }
    assert "inputs.shared-admission != true" in pool["jobs"]["teardown"]["if"]
    steps = pool["jobs"]["pool"]["steps"]
    upload = next(s for s in steps if s.get("id") == "shared_custody")
    assert upload["if"] == "always() && inputs.shared-admission == true"
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["path"].endswith("/export/consume-custody.json")
    assert "prepared" not in upload["with"]["path"]
    download = next(
        s
        for s in teardown["jobs"]["teardown"]["steps"]
        if "actions/download-artifact@" in s.get("uses", "")
    )
    assert set(download["with"]) == {"artifact-ids", "path", "merge-multiple"}
    assert download["with"]["artifact-ids"] == "${{ inputs.shared-admission-artifact-id }}"
    assert download["with"]["path"].endswith("/shared-admission/received")


def test_all_five_legacy_internal_destroys_guarded_and_no_global_JA_replacement():
    pool = _workflows()["runner-pool.yml"]
    body = next(s["run"] for s in pool["jobs"]["pool"]["steps"] if s.get("id") == "provision")
    lines = body.splitlines()
    calls = [i for i, line in enumerate(lines) if '"${JA[@]}" destroy ' in line]
    assert len(calls) == 5
    assert all(lines[i - 1].strip() == "shared_hold_before_internal_destroy" for i in calls)
    assert "JA=(uv run --with . just-akash)" in body
    assert '"${JA[@]}" verify-closed' in body
    assert '"${DEPLOY_JA[@]}" deploy "${SHARED_RECEIPT_ARGS[@]}"' in body
    assert "MAX_ATTEMPTS=1" in body and 'for attempt in $(seq 1 "$MAX_ATTEMPTS")' in body


@pytest.mark.parametrize(
    "enabled,code,expected", [("true", 78, 78), ("true", 1, 78), ("false", 78, 0)]
)
def test_actual_pool_terminal_branch_stops_opted_replay_only(tmp_path, enabled, code, expected):
    import subprocess

    pool = _workflows()["runner-pool.yml"]
    body = next(s["run"] for s in pool["jobs"]["pool"]["steps"] if s.get("id") == "provision")
    start = body.index(
        'if [ "${SHARED_ADMISSION:-false}" = true ] && [ "$DEPLOY_RC" -ne 0 ]; then'
    )
    end = body.index("fi", start) + 2
    snippet = body[start:end]
    output = tmp_path / "output"
    argv = ["bash", "-c", snippet + '\nprintf "continued\\n"', "closed-control"]
    result = subprocess.run(
        argv,
        env={
            "PATH": "/usr/bin:/bin",
            "SHARED_ADMISSION": enabled,
            "DEPLOY_RC": str(code),
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == expected
    if enabled == "true":
        assert b"continued" not in result.stdout and "no-deployment" not in output.read_text()
        assert "deployment_outcome=unknown" in output.read_text()


@pytest.mark.parametrize(
    "enabled,code,proof,expected,calls",
    [
        ("true", 78, 0, 78, 0),
        ("true", 1, 0, 78, 0),
        ("true", 0, 78, 78, 1),
        ("true", 0, 0, 0, 1),
        ("false", 1, 78, 0, 0),
    ],
)
def test_actual_teardown_single_call_sharedclosed_requires_signed_service_gate(
    tmp_path, enabled, code, proof, expected, calls
):
    import subprocess

    teardown = _workflows()["runner-teardown.yml"]
    body = next(s["run"] for s in teardown["jobs"]["teardown"]["steps"] if s.get("id") == "close")
    start = body.index(
        'if [ "${SHARED_ADMISSION:-false}" = true ]; then',
        body.index('"${DESTROY_JA[@]}" destroy'),
    )
    end = body.index("# ⛔ 75 = OWNER_LOOKUP_UNREACHABLE", start)
    snippet = body[start:end]
    probe, output = tmp_path / "calls", tmp_path / "output"
    prefix = 'uv() { printf "proof\\n" >> "$PROBE"; return "$PROOF"; };\n'
    result = subprocess.run(
        ["bash", "-c", prefix + snippet + '\nprintf "continued\\n"'],
        env={
            "PATH": "/usr/bin:/bin",
            "SHARED_ADMISSION": enabled,
            "DESTROY_RC": str(code),
            "PROOF": str(proof),
            "PROBE": str(probe),
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        timeout=3,
    )
    assert result.returncode == expected
    assert (len(probe.read_text().splitlines()) if probe.exists() else 0) == calls
    if enabled == "true" and code == proof == 0:
        assert "shared_closed=true" in output.read_text()
    else:
        assert not output.exists() or "shared_closed=true" not in output.read_text()


def test_real_currentMain_bootstrap_CLI_signed_capture_and_cleanup_same_process(
    scene, tmp_path, monkeypatch
):
    from importlib.metadata import version

    from just_akash import hosted_cli_bootstrap as bootstrap
    from just_akash import hosted_legacy_cli as hosted

    assert version("akash-lease-core") == "0.18.0"
    expected = json.loads(Path(__file__).with_name("package_manifest.json").read_bytes())
    assert (
        hashlib.sha256(Path(hosted.__file__).read_bytes()).hexdigest()
        == expected["just_akash/hosted_legacy_cli.py"]
    )

    # Existing mock server/Console/native fixtures, REAL installed current SDK and
    # Core018, real Ed25519 decoder, managers/selected wallet/final opener/FDs.
    operation = "owned-private-images-123-2"
    provision = dataclasses.replace(scene.registration(), operation_id=operation)
    reg = custody.CustodyRegistration(
        scene.broker.policy,
        "deploy",
        "Borduas-Holdings/blazing",
        123,
        2,
        provision.source_revision,
        ".github/workflows/owned-private-podman-images.yml",
        "b" * 40,
        "c" * 40,
        provision.sdk_parent_ref,
        operation,
        "signed-consume-123-2",
        tmp_path / "real-capture",
        900.0,
        lambda: "PUBLIC_SYNTHETIC_READ",
    )
    configured = bootstrap.HostedCLIBootstrap(provision, custody.capture)
    selected = SimpleNamespace(reg=reg, bootstrap=configured)
    monkeypatch.setattr(custody, "_reviewed_hosted_custody", lambda: selected.reg)
    monkeypatch.setattr(bootstrap, "_reviewed_hosted_cli_bootstrap", lambda: selected.bootstrap)
    custody.prepare()
    monkeypatch.setattr(
        "sys.argv",
        [
            "hosted-bootstrap",
            "deploy",
            "--sdl",
            str(scene.sdl_path),
            "--bid-wait",
            "1",
            "--bid-wait-retry",
            "1",
            "--provider",
            "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk",
            "--receipt-path",
            str(scene.receipt_path),
            "--receipt-operation-id",
            operation,
        ],
    )
    with pytest.raises(SystemExit) as exit_status:
        bootstrap.main()
    assert exit_status.value.code == 0
    assert (
        sum(method == "POST" and path == "/v1/leases" for method, path, body in scene.console) == 1
    )
    assert backend.hook._reviewed_hook() is None and hosted._reviewed_hosted_cli() is None
    data, wires = custody._bundle((reg.directory / custody.BUNDLE).read_bytes(), reg)
    assert data["state"] == "finished" and len(wires) == 1
    assert custody.object_map(data["completion"])["locator_unknown"] is False
    # Native GitHub metadata acquisition is covered separately in this module;
    # these historical signed bytes cannot themselves authenticate the GHA job.
    selected.reg = dataclasses.replace(reg, purpose="destroy", consume_wire=wires[0])
    cleanup_registration = hosted.CleanupCLIRegistration(
        scene.broker.policy, wires[0], 900.0, scene.clock.utc + 300, scene.principals
    )
    selected.bootstrap = bootstrap.HostedCLIBootstrap(cleanup_registration, custody.capture)
    monkeypatch.setattr(
        "sys.argv",
        [
            "hosted-bootstrap",
            "destroy",
            "--dseq",
            "123",
            "--expected-owner",
            "akash14n4rkmz64rn0tey0r5g07l8q5x0fh2h4hu44kt",
            "--expected-group",
            "dcloud",
            "--yes",
        ],
    )
    assert bootstrap.main() == 0
    custody.assert_closed()
    assert sum(method == "DELETE" for method, path, body in scene.console) == 1
    assert scene.broker.phases == ["reserve", "consume", "outcome", "close"]
    assert hosted._reviewed_hosted_cli() is cleanup._reviewed_cleanup() is None
