"""A DSEQ survives failures after create through an opt-in durable receipt."""

from __future__ import annotations

import inspect
import json
import multiprocessing
import os
import stat
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from just_akash import deploy as deploy_module
from just_akash.deployment_receipt import (
    artifact_identity,
    decode_receipt,
    mark_create_response_received,
    mark_submitting,
    prepare_receipt,
    sha256_bytes,
)

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
RUN_ID = "abc123def456"
SDL = """version: "2.0"
services:
  app:
    image: example.invalid/app@sha256:deadbeef
profiles:
  compute:
    app:
      resources:
        cpu:
          units: 1
        memory:
          size: 1Gi
        storage:
          - size: 1Gi
  placement:
    receipt-primary:
      pricing:
        app:
          denom: uact
          amount: 10000
    receipt-secondary:
      pricing:
        app:
          denom: uact
          amount: 10000
deployment:
  app:
    receipt-primary:
      profile: app
      count: 1
    receipt-secondary:
      profile: app
      count: 1
"""
GROUPS = ["receipt-primary", "receipt-secondary"]


@pytest.fixture(autouse=True)
def _no_wallet_credit_chain_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy_module, "_check_wallet_credit", lambda *_args, **_kwargs: None)


def _private_dir(path: Path) -> Path:
    path.mkdir()
    path.chmod(0o700)
    return path


def _prepare(path: Path, *, sdl: str = SDL):
    return prepare_receipt(
        str(path),
        operation_id=RUN_ID,
        expected_owner=OWNER,
        expected_groups=GROUPS,
        expected_artifact_digest=sha256_bytes(sdl.encode()),
        sdl_content=sdl,
    )


def test_receipt_derives_both_independent_digests_from_exact_submitted_bytes() -> None:
    population, population_digest, artifact_digest = artifact_identity(SDL)
    assert population == [
        {"gseq": 1, "name": "receipt-primary"},
        {"gseq": 2, "name": "receipt-secondary"},
    ]
    assert len(population_digest) == 64
    assert artifact_digest == sha256_bytes(SDL.encode())

    changed = SDL.replace("example.invalid/app@sha256:deadbeef", "example.invalid/app@sha256:cafe")
    changed_population, changed_population_digest, changed_artifact_digest = artifact_identity(
        changed
    )
    assert changed_population == population
    assert changed_population_digest == population_digest
    assert changed_artifact_digest != artifact_digest


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.replace("    receipt-secondary:\n      profile: app\n      count: 1\n", ""),
        lambda s: s.replace(
            "    receipt-secondary:\n      profile: app", "    unknown:\n      profile: app"
        ),
        lambda s: s.replace(
            "    receipt-primary:\n      profile: app\n      count: 1\n"
            "    receipt-secondary:\n      profile: app\n      count: 1\n",
            "    receipt-secondary:\n      profile: app\n      count: 1\n"
            "    receipt-primary:\n      profile: app\n      count: 1\n",
        ),
    ],
    ids=["unused-profile-placement", "unknown-deployment-placement", "order-mismatch"],
)
def test_artifact_identity_rejects_unreconciled_deployment_population(mutation) -> None:
    changed = mutation(SDL)
    assert changed != SDL, "population mutation did not apply"
    with pytest.raises(RuntimeError, match="same complete group population"):
        artifact_identity(changed)


def test_population_reconciliation_effect_mutation_accepts_omitted_group() -> None:
    source = inspect.getsource(artifact_identity)
    target = "    if deployment_order != names:\n"
    assert source.count(target) == 1, (
        f"reconciliation target count changed: {source.count(target)}"
    )
    mutated = source.replace(target, "    if False:\n", 1)
    assert mutated != source
    namespace = vars(__import__("just_akash.deployment_receipt", fromlist=["*"])).copy()
    exec(mutated, namespace)  # noqa: S102 - executable effect mutation
    omitted = SDL.replace("    receipt-secondary:\n      profile: app\n      count: 1\n", "")
    assert omitted != SDL
    population, _, _ = namespace["artifact_identity"](omitted)
    assert len(population) == 2, "mutant did not accept the omitted deployment group"


def test_prepared_to_response_transition_is_private_typed_and_transaction_bound(
    tmp_path: Path,
) -> None:
    parent = _private_dir(tmp_path / "private")
    path, prepared, prepared_bytes = _prepare(parent / "receipt.json")
    assert json.loads(path.read_text())["state"] == "prepared"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    response = {"dseq": "42", "manifest": "rendered", "txhash": "A" * 64}
    response_receipt = mark_create_response_received(
        *mark_submitting(path, prepared, prepared_bytes),
        dseq="42",
        deployment_response=response,
    )
    durable = json.loads(path.read_text())
    assert durable == response_receipt
    assert durable["receipt_type"] == "just-akash/deployment-create/v1"
    assert durable["operation_id"] == RUN_ID
    assert durable["signer_owner_candidate"] == OWNER
    assert durable["owner_evidence_source"] == "console_account_address"
    assert "owner" not in durable
    assert durable["dseq"] == "42"
    assert durable["create_transaction_identifiers"] == {"txhash": "A" * 64}
    assert durable["create_response_digest"] == sha256_bytes(
        (json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    subject = {
        "signer_owner_candidate": durable["signer_owner_candidate"],
        "dseq": durable["dseq"],
        "group_population": durable["group_population"],
        "group_population_digest": durable["group_population_digest"],
        "artifact_digest": durable["artifact_digest"],
    }
    assert subject == {
        "signer_owner_candidate": OWNER,
        "dseq": "42",
        "group_population": [
            {"gseq": 1, "name": "receipt-primary"},
            {"gseq": 2, "name": "receipt-secondary"},
        ],
        "group_population_digest": prepared["group_population_digest"],
        "artifact_digest": sha256_bytes(SDL.encode()),
    }
    assert decode_receipt(path.read_bytes())["state"] == "create_response_received"

    tampered_documents = []
    for key, value in (
        ("dseq", "01"),
        ("signer_owner_candidate", OWNER[:-1] + "q"),
        ("group_population_digest", "0" * 64),
        ("artifact_digest", "not-a-digest"),
        ("authority", "created"),
        ("response_received_at", "2000-01-01T00:00:00+00:00"),
    ):
        tampered = dict(durable)
        tampered[key] = value
        tampered_documents.append(tampered)
    extra = dict(durable)
    extra["owner"] = OWNER
    tampered_documents.append(extra)
    for tampered in tampered_documents:
        with pytest.raises(RuntimeError):
            decode_receipt((json.dumps(tampered) + "\n").encode())


def test_decoder_refuses_forged_created_authority(tmp_path: Path) -> None:
    parent = _private_dir(tmp_path / "private")
    path, _, _ = _prepare(parent / "receipt.json")
    assert decode_receipt(path.read_bytes())["state"] == "prepared"
    forged = json.loads(path.read_text())
    forged["state"] = "created"
    with pytest.raises(RuntimeError, match="unsupported authority state"):
        decode_receipt((json.dumps(forged) + "\n").encode())


def test_existing_malformed_symlink_and_nonprivate_paths_are_refused(tmp_path: Path) -> None:
    private = _private_dir(tmp_path / "private")
    existing = private / "existing.json"
    existing.write_text("live receipt")
    with pytest.raises(RuntimeError, match="already exists"):
        _prepare(existing)
    assert existing.read_text() == "live receipt"

    target = private / "target"
    target.write_text("do not replace")
    symlink = private / "symlink.json"
    symlink.symlink_to(target)
    with pytest.raises(RuntimeError, match="already exists"):
        _prepare(symlink)
    assert target.read_text() == "do not replace"

    public = tmp_path / "public"
    public.mkdir()
    public.chmod(0o755)
    with pytest.raises(RuntimeError, match="exact private mode 0700"):
        _prepare(public / "receipt.json")

    real_parent = _private_dir(tmp_path / "real-parent")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(RuntimeError, match="real directory"):
        _prepare(linked_parent / "receipt.json")


def test_changed_or_malformed_prepared_receipt_is_never_overwritten(tmp_path: Path) -> None:
    parent = _private_dir(tmp_path / "private")
    path, prepared, prepared_bytes = _prepare(parent / "receipt.json")
    path.write_text("malformed")
    path.chmod(0o600)
    with pytest.raises(RuntimeError, match="changed before create completed"):
        mark_create_response_received(
            *mark_submitting(path, prepared, prepared_bytes),
            dseq="42",
            deployment_response={"dseq": "42"},
        )
    assert path.read_text() == "malformed"


@pytest.mark.parametrize("dseq", [0, "0", "01", -1, True, 2**64])
def test_noncanonical_or_out_of_range_dseq_retains_prepared_receipt(
    tmp_path: Path, dseq: object
) -> None:
    parent = _private_dir(tmp_path / "private")
    path, prepared, prepared_bytes = _prepare(parent / "receipt.json")
    submitting = mark_submitting(path, prepared, prepared_bytes)
    with pytest.raises(RuntimeError, match="DSEQ"):
        mark_create_response_received(
            *submitting,
            dseq=dseq,
            deployment_response={"dseq": dseq},
        )
    durable = json.loads(path.read_text())
    assert durable["state"] == "submitting"
    assert "dseq" not in durable


def test_caller_expectations_are_checked_before_a_create_can_spend(tmp_path: Path) -> None:
    parent = _private_dir(tmp_path / "private")
    path = parent / "receipt.json"
    with pytest.raises(RuntimeError, match="complete submitted SDL population"):
        prepare_receipt(
            str(path),
            operation_id=RUN_ID,
            expected_owner=OWNER,
            expected_groups=["receipt-primary"],
            expected_artifact_digest=sha256_bytes(SDL.encode()),
            sdl_content=SDL,
        )
    with pytest.raises(RuntimeError, match="artifact digest mismatch"):
        prepare_receipt(
            str(path),
            operation_id=RUN_ID,
            expected_owner=OWNER,
            expected_groups=GROUPS,
            expected_artifact_digest="0" * 64,
            sdl_content=SDL,
        )
    with pytest.raises(RuntimeError, match="exactly 64 hexadecimal"):
        prepare_receipt(
            str(path),
            operation_id=RUN_ID,
            expected_owner=OWNER,
            expected_groups=GROUPS,
            expected_artifact_digest="not-a-digest",
            sdl_content=SDL,
        )
    assert not path.exists()


@pytest.mark.parametrize(
    "owner",
    [
        "akash1" + "b" * 38,
        "akash1" + "i" * 38,
        "akash1" + "o" * 38,
        "akash1" + "1" * 38,
        "akash1" + "q" * 37,
        "akash1" + "q" * 39,
        OWNER[:-1] + ("q" if OWNER[-1] != "q" else "p"),
    ],
    ids=[
        "forbidden-b",
        "forbidden-i",
        "forbidden-o",
        "forbidden-1",
        "short",
        "long",
        "bad-checksum",
    ],
)
def test_noncanonical_expected_owner_is_refused(owner: str, tmp_path: Path) -> None:
    parent = _private_dir(tmp_path / "private")
    path = parent / "receipt.json"
    with pytest.raises(RuntimeError, match="canonical akash1 address"):
        prepare_receipt(
            str(path),
            operation_id=RUN_ID,
            expected_owner=owner,
            expected_groups=GROUPS,
            expected_artifact_digest=sha256_bytes(SDL.encode()),
            sdl_content=SDL,
        )
    assert not path.exists()


def _crash_after_emit(path: str) -> None:
    receipt_path, prepared, prepared_bytes = prepare_receipt(
        path,
        operation_id=RUN_ID,
        expected_owner=OWNER,
        expected_groups=GROUPS,
        expected_artifact_digest=sha256_bytes(SDL.encode()),
        sdl_content=SDL,
    )
    mark_create_response_received(
        *mark_submitting(receipt_path, prepared, prepared_bytes),
        dseq="99",
        deployment_response={"dseq": "99", "txHash": "B" * 64},
    )
    os._exit(23)


def test_process_kill_after_emit_cannot_lose_the_response_receipt(tmp_path: Path) -> None:
    parent = _private_dir(tmp_path / "private")
    path = parent / "receipt.json"
    process = multiprocessing.Process(target=_crash_after_emit, args=(str(path),))
    process.start()
    process.join(10)
    assert process.exitcode == 23
    assert json.loads(path.read_text())["state"] == "create_response_received"
    assert json.loads(path.read_text())["dseq"] == "99"


def _receipt_arguments(sdl: str, receipt: Path) -> dict[str, Any]:
    population, _, digest = artifact_identity(sdl)
    return {
        "receipt_path": str(receipt),
        "expected_owner": OWNER,
        "expected_groups": [str(group["name"]) for group in population],
        "expected_artifact_digest": digest,
        "receipt_operation_id": "github-run-123-attempt-2",
    }


@patch("just_akash.deploy.AkashConsoleAPI")
def test_early_post_create_failure_leaves_recovery_capable_dseq(
    mock_api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
    monkeypatch.setattr(deploy_module, "_RUN_ID", RUN_ID)
    sdl_path = tmp_path / "sdl.yaml"
    sdl_path.write_text(SDL)
    rendered = deploy_module._prepare_sdl_content(str(sdl_path))
    parent = _private_dir(tmp_path / "private")
    receipt = parent / "receipt.json"

    client = mock_api.return_value
    client.account_address.return_value = OWNER
    client.create_deployment.return_value = {
        "dseq": "12345",
        "manifest": "abc",
        "transactionHash": "C" * 64,
    }

    with pytest.raises(RuntimeError, match="No bids received"):
        deploy_module.deploy(
            sdl_path=str(sdl_path),
            bid_wait=0,
            bid_wait_retry=0,
            **_receipt_arguments(rendered, receipt),
        )

    durable = json.loads(receipt.read_text())
    assert durable["state"] == "create_response_received"
    assert durable["dseq"] == "12345"
    assert durable["create_transaction_identifiers"] == {"transactionHash": "C" * 64}
    client.create_lease.assert_not_called()


@patch("just_akash.deploy.AkashConsoleAPI")
def test_prepared_receipt_exists_before_the_create_send(
    mock_api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
    monkeypatch.setattr(deploy_module, "_RUN_ID", RUN_ID)
    sdl_path = tmp_path / "sdl.yaml"
    sdl_path.write_text(SDL)
    rendered = deploy_module._prepare_sdl_content(str(sdl_path))
    parent = _private_dir(tmp_path / "private")
    receipt = parent / "receipt.json"

    client = mock_api.return_value
    client.account_address.return_value = OWNER

    def fail_create(*_args, **_kwargs):
        durable = json.loads(receipt.read_text())
        assert durable["state"] == "submitting"
        raise RuntimeError("create stopped")

    client.create_deployment.side_effect = fail_create
    with pytest.raises(RuntimeError, match="Failed to create deployment"):
        deploy_module.deploy(sdl_path=str(sdl_path), **_receipt_arguments(rendered, receipt))
    assert json.loads(receipt.read_text())["state"] == "submitting"


@patch("just_akash.deploy.AkashConsoleAPI")
def test_mark_submitting_call_site_mutation_exposes_prepared_at_create(
    mock_api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = inspect.getsource(deploy_module.deploy)
    target = "        prepared_receipt = mark_submitting(*prepared_receipt)\n"
    assert source.count(target) == 1, (
        f"submitting call target count changed: {source.count(target)}"
    )
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
    sdl_path = tmp_path / "sdl.yaml"
    sdl_path.write_text(SDL)
    rendered = deploy_module._prepare_sdl_content(str(sdl_path))
    receipt = _private_dir(tmp_path / "private") / "receipt.json"
    client = mock_api.return_value
    client.account_address.return_value = OWNER

    def observe_create(*_args, **_kwargs):
        assert json.loads(receipt.read_text())["state"] == "submitting"

    client.create_deployment.side_effect = observe_create
    with (
        patch.object(deploy_module, "mark_submitting", side_effect=lambda *args: args),
        pytest.raises(AssertionError),
    ):
        deploy_module.deploy(sdl_path=str(sdl_path), **_receipt_arguments(rendered, receipt))
    assert json.loads(receipt.read_text())["state"] == "prepared"


@patch("just_akash.deploy.AkashConsoleAPI")
def test_create_response_without_dseq_retains_the_prepared_receipt(
    mock_api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
    monkeypatch.setattr(deploy_module, "_RUN_ID", RUN_ID)
    sdl_path = tmp_path / "sdl.yaml"
    sdl_path.write_text(SDL)
    rendered = deploy_module._prepare_sdl_content(str(sdl_path))
    parent = _private_dir(tmp_path / "private")
    receipt = parent / "receipt.json"

    client = mock_api.return_value
    client.account_address.return_value = OWNER
    client.create_deployment.return_value = {"manifest": "abc"}
    with pytest.raises(RuntimeError, match="No DSEQ returned"):
        deploy_module.deploy(sdl_path=str(sdl_path), **_receipt_arguments(rendered, receipt))
    durable = json.loads(receipt.read_text())
    assert durable["state"] == "submitting"
    assert "dseq" not in durable


def test_all_receipt_arguments_are_required_together() -> None:
    with pytest.raises(ValueError, match="all-or-none"):
        deploy_module.deploy("unused.yaml", receipt_path="/private/unused")


def test_write_location_effect_mutation_loses_the_dseq_on_early_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = inspect.getsource(deploy_module.deploy)
    target = "            mark_create_response_received(\n"
    assert source.count(target) == 1, (
        f"transition call target count changed: {source.count(target)}"
    )
    calls = 0

    def bypass_transition(*_args, **_kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
    monkeypatch.setattr(deploy_module, "_RUN_ID", RUN_ID)
    sdl_path = tmp_path / "sdl.yaml"
    sdl_path.write_text(SDL)
    rendered = deploy_module._prepare_sdl_content(str(sdl_path))
    parent = _private_dir(tmp_path / "private")
    receipt = parent / "receipt.json"

    with (
        patch("just_akash.deploy.AkashConsoleAPI") as mock_api,
        patch.object(deploy_module, "mark_create_response_received", bypass_transition),
    ):
        client = mock_api.return_value
        client.account_address.return_value = OWNER
        client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}
        with pytest.raises(RuntimeError, match="No bids received"):
            deploy_module.deploy(
                sdl_path=str(sdl_path),
                bid_wait=0,
                bid_wait_retry=0,
                **_receipt_arguments(rendered, receipt),
            )

    assert calls == 1, "call-site bypass mutation did not apply"
    durable = json.loads(receipt.read_text())
    assert durable["state"] == "submitting"
    assert "dseq" not in durable


@pytest.mark.parametrize("fault", ["temp", "replace", "parent-fsync"])
@patch("just_akash.deploy.AkashConsoleAPI")
def test_post_response_transition_failure_is_nonretryable_and_blocks_reinvocation(
    mock_api, fault: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import just_akash.deployment_receipt as receipt_module

    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
    sdl_path = tmp_path / "sdl.yaml"
    sdl_path.write_text(SDL)
    rendered = deploy_module._prepare_sdl_content(str(sdl_path))
    parent = _private_dir(tmp_path / "private")
    receipt = parent / "receipt.json"
    client = mock_api.return_value
    client.account_address.return_value = OWNER
    client.create_deployment.return_value = {"dseq": "12345", "manifest": "abc"}

    if fault == "temp":
        original_write = receipt_module._write_temp
        count = 0

        def fail_second_write(parent_path, receipt_path, payload):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("temp failed")
            return original_write(parent_path, receipt_path, payload)

        context = patch.object(receipt_module, "_write_temp", fail_second_write)
    elif fault == "replace":
        original_replace = receipt_module.os.replace
        count = 0

        def fail_second_replace(source, destination):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("replace failed")
            return original_replace(source, destination)

        context = patch.object(receipt_module.os, "replace", fail_second_replace)
    else:
        original = receipt_module._fsync_parent
        count = 0

        def fail_second(parent_path):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError("parent fsync failed")
            return original(parent_path)

        context = patch.object(receipt_module, "_fsync_parent", fail_second)

    with context, pytest.raises(RuntimeError, match="NON-RETRYABLE CREATE OUTCOME AMBIGUOUS"):
        deploy_module.deploy(sdl_path=str(sdl_path), **_receipt_arguments(rendered, receipt))
    assert receipt.exists(), "transition failure lost the immutable recovery seed"
    assert json.loads(receipt.read_text())["state"] == "submitting"
    assert client.create_deployment.call_count == 1

    with pytest.raises(RuntimeError, match="already exists"):
        deploy_module.deploy(sdl_path=str(sdl_path), **_receipt_arguments(rendered, receipt))
    assert client.create_deployment.call_count == 1, "re-invocation sent a blind second create"


@patch("just_akash.deploy.AkashConsoleAPI")
def test_prepared_install_failure_never_sends_create(
    mock_api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import just_akash.deployment_receipt as receipt_module

    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
    sdl_path = tmp_path / "sdl.yaml"
    sdl_path.write_text(SDL)
    rendered = deploy_module._prepare_sdl_content(str(sdl_path))
    parent = _private_dir(tmp_path / "private")
    receipt = parent / "receipt.json"
    client = mock_api.return_value
    client.account_address.return_value = OWNER
    with (
        patch.object(receipt_module.os, "link", side_effect=OSError("link failed")),
        pytest.raises(OSError, match="link failed"),
    ):
        deploy_module.deploy(sdl_path=str(sdl_path), **_receipt_arguments(rendered, receipt))
    client.create_deployment.assert_not_called()


@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_receipt_mode_refuses_internal_second_create(
    mock_api, mock_time, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.setenv("AKASH_PROVIDERS", "akash1provider")
    monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
    mock_time.time.side_effect = range(1000)
    mock_time.sleep.return_value = None
    sdl_path = tmp_path / "sdl.yaml"
    sdl_path.write_text(SDL)
    rendered = deploy_module._prepare_sdl_content(str(sdl_path))
    receipt = _private_dir(tmp_path / "private") / "receipt.json"
    client = mock_api.return_value
    client.account_address.return_value = OWNER
    client.create_deployment.return_value = {"dseq": "111", "manifest": "m1"}
    client.get_bids.return_value = [
        {
            "id": {"provider": "akash1provider"},
            "price": {"amount": "10", "denom": "uakt"},
            "state": "open",
        }
    ]
    client.create_lease.side_effect = RuntimeError("selected bid is no longer open")

    with pytest.raises(RuntimeError, match="receipt mode refuses internal re-deploy"):
        deploy_module.deploy(
            sdl_path=str(sdl_path),
            bid_wait=5,
            bid_wait_retry=5,
            **_receipt_arguments(rendered, receipt),
        )
    assert client.create_deployment.call_count == 1
    client.close_deployment.assert_not_called()
    assert json.loads(receipt.read_text())["dseq"] == "111"
