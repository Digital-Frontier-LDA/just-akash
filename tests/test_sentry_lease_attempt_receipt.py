"""Actual SDK lease wire keeps a durable candidate, never retry authority."""

import hashlib
import itertools
import json
from unittest.mock import patch

import pytest

from just_akash import deploy as deployment
from just_akash.runner_image import NATIVE_READER_PROVIDERS

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
PROVIDER = sorted(NATIVE_READER_PROVIDERS)[0]
GROUP = "borduas-sentry-idv1-class-ci-runner-g1-attempt-2-run-123-end"
SDL = f"""version: "2.0"
services:
  runner:
    image: example.invalid/runner:fixture
profiles:
  compute:
    runner:
      resources:
        cpu: {{units: 2}}
        memory: {{size: 6Gi}}
        storage: {{size: 40Gi}}
  placement:
    {GROUP}:
      pricing:
        runner: {{denom: uact, amount: 100000}}
deployment:
  runner:
    {GROUP}:
      profile: runner
      count: 1
"""


@pytest.fixture
def lease(tmp_path, monkeypatch):
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.setenv("AKASH_PROVIDERS", PROVIDER)
    monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
    monkeypatch.setattr(deployment, "_check_wallet_credit", lambda *a, **k: None)
    monkeypatch.setattr(deployment, "_RUN_ID", "123")
    source = tmp_path / "runner.yml"
    source.write_text(SDL)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    create, attempt = private / "create.json", private / "lease.json"
    with (
        patch.object(deployment, "AkashConsoleAPI") as factory,
        patch.object(deployment, "time") as clock,
    ):
        clock.time.side_effect = itertools.count(1000)
        clock.sleep.return_value = None
        client = factory.return_value
        client.account_address.return_value = OWNER
        client.create_deployment.return_value = {
            "dseq": "111",
            "manifest": "private-fixture-manifest",
        }
        client.get_bids.return_value = [
            {
                "id": {"provider": PROVIDER, "gseq": 1},
                "price": {"amount": "10", "denom": "uact"},
                "state": "open",
            }
        ]
        client.create_lease.return_value = {"result": "candidate-fixture"}

        def run():
            return deployment.deploy(
                str(source),
                bid_wait=5,
                bid_wait_retry=5,
                receipt_path=str(create),
                receipt_operation_id="sentry-123-2-build",
                lease_receipt_path=str(attempt),
            )

        yield run, client, create, attempt


def test_actual_lease_wire_observes_durable_unknown_with_exact_bound_profile(lease):
    run, client, create, attempt = lease

    def send(**kw):
        value = json.loads(attempt.read_bytes())
        assert value["state"] == "UNKNOWN"
        assert value["operation_id"] == "sentry-123-2-build"
        assert value["owner"] == OWNER and value["dseq"] == "111"
        assert value["provider"] == kw["provider"] == PROVIDER
        assert value["gseq"] == kw["gseq"] == 1 and value["oseq"] == 1
        assert value["group"] == GROUP
        assert value["resource_profile"] == {
            "cpu_millicores": 2000,
            "memory_bytes": 6 * 1024**3,
            "storage_bytes": 40 * 1024**3,
            "gpu_count": 0,
            "replicas": 1,
        }
        assert json.loads(create.read_bytes())["state"] == "create_response_received"
        assert (
            value["deployment_receipt_sha256"] == hashlib.sha256(create.read_bytes()).hexdigest()
        )
        assert value["sdl_sha256"] == json.loads(create.read_bytes())["artifact_digest"]
        assert b"private-fixture-manifest" not in attempt.read_bytes()
        return {"result": "candidate-fixture"}

    client.create_lease.side_effect = send
    assert run()["dseq"] == "111"
    assert client.create_lease.call_count == 1
    candidate = json.loads(attempt.with_suffix(".response.json").read_bytes())
    assert candidate["state"] == "RESPONSE_CANDIDATE"
    assert candidate["runner_binding_verified"] is candidate["publication_authority"] is False
    assert json.loads(attempt.read_bytes())["state"] == "UNKNOWN"


@pytest.mark.parametrize(
    "message", ["JWT has invalid claims", "selected bid is no longer open", "lost ACK"]
)
def test_ambiguous_lease_never_retries_but_preserves_original_cleanup(lease, message):
    run, client, create, attempt = lease
    client.create_lease.side_effect = RuntimeError(message)
    with pytest.raises(RuntimeError, match="NON-RETRYABLE LEASE"):
        run()
    assert client.create_deployment.call_count == client.create_lease.call_count == 1
    client.close_deployment.assert_called_once_with("111")
    assert json.loads(attempt.read_bytes())["state"] == "UNKNOWN"
    assert not attempt.with_suffix(".response.json").exists()
    assert json.loads(create.read_bytes())["state"] == "create_response_received"


def test_unknown_slot_refuses_before_another_create_or_lease(lease):
    run, client, create, attempt = lease
    attempt.write_text('{"state":"UNKNOWN","preserved":"fixture"}')
    before = attempt.read_bytes()
    with pytest.raises(RuntimeError, match="already exists"):
        run()
    client.create_deployment.assert_not_called()
    client.create_lease.assert_not_called()
    client.close_deployment.assert_not_called()
    assert attempt.read_bytes() == before


def test_lost_response_persistence_keeps_unknown_and_original_cleanup(lease, monkeypatch):
    from just_akash import sentry_lease_receipt as capture

    run, client, create, attempt = lease
    original = capture._create_durable

    def persist(path, data):
        if path == attempt.with_suffix(".response.json"):
            raise OSError("private fixture failure must not be logged")
        return original(path, data)

    monkeypatch.setattr(capture, "_create_durable", persist)
    with pytest.raises(RuntimeError, match="NON-RETRYABLE LEASE"):
        run()
    assert client.create_deployment.call_count == client.create_lease.call_count == 1
    client.close_deployment.assert_called_once_with("111")
    assert json.loads(attempt.read_bytes())["state"] == "UNKNOWN"
    assert not attempt.with_suffix(".response.json").exists()


def test_last_intent_fsync_lost_ack_never_sends_lease_and_retains_create(lease, monkeypatch):
    from just_akash import deployment_receipt as receipts

    run, client, create, attempt = lease
    original = receipts._fsync_parent

    def sync(parent):
        original(parent)
        if attempt.exists():
            raise OSError("directory sync ACK lost")

    monkeypatch.setattr(receipts, "_fsync_parent", sync)
    with pytest.raises(RuntimeError, match="NON-RETRYABLE LEASE"):
        run()
    assert client.create_deployment.call_count == 1
    client.create_lease.assert_not_called()
    client.close_deployment.assert_called_once_with("111")
    assert json.loads(attempt.read_bytes())["state"] == "UNKNOWN"
    assert json.loads(create.read_bytes())["dseq"] == "111"


def test_unknown_does_not_hide_independent_close_failure(lease):
    run, client, create, attempt = lease
    client.create_lease.side_effect = RuntimeError("lost lease ACK")
    client.close_deployment.side_effect = RuntimeError("lost close ACK")
    with pytest.raises(RuntimeError, match="NON-RETRYABLE LEASE"):
        run()
    assert client.create_deployment.call_count == client.create_lease.call_count == 1
    client.close_deployment.assert_called_once_with("111")
    assert json.loads(attempt.read_bytes())["state"] == "UNKNOWN"
    assert json.loads(create.read_bytes())["dseq"] == "111"
    assert not attempt.with_suffix(".response.json").exists()


def test_response_lost_ack_after_durable_install_never_replays(lease, monkeypatch):
    from just_akash import sentry_lease_receipt as capture

    run, client, create, attempt = lease
    original = capture._create_durable

    def persist(path, data):
        original(path, data)
        if path == attempt.with_suffix(".response.json"):
            raise OSError("response install ACK lost")

    monkeypatch.setattr(capture, "_create_durable", persist)
    with pytest.raises(RuntimeError, match="NON-RETRYABLE LEASE"):
        run()
    assert client.create_lease.call_count == 1
    client.close_deployment.assert_called_once_with("111")
    assert json.loads(attempt.read_bytes())["state"] == "UNKNOWN"
    assert (
        json.loads(attempt.with_suffix(".response.json").read_bytes())["state"]
        == "RESPONSE_CANDIDATE"
    )
    with pytest.raises(RuntimeError, match="already exists"):
        run()
    assert client.create_deployment.call_count == client.create_lease.call_count == 1


def test_foreign_same_owner_replacement_is_never_overwritten_or_erased(lease):
    run, client, create, attempt = lease

    def send(**kw):
        attempt.write_text("foreign-replacement")
        return {"result": "candidate-fixture"}

    client.create_lease.side_effect = send
    with pytest.raises(RuntimeError, match="NON-RETRYABLE LEASE"):
        run()
    assert attempt.read_text() == "foreign-replacement"
    assert not attempt.with_suffix(".response.json").exists()
    client.close_deployment.assert_called_once_with("111")


@pytest.mark.parametrize(
    "old,new",
    [
        ("units: 2", "units: 3"),
        ("size: 6Gi", "size: 7Gi"),
        ("size: 40Gi", "size: 41Gi"),
        ("count: 1", "count: 2"),
        (GROUP, "foreign-run-123-end"),
    ],
)
def test_wrong_resource_or_placement_refuses_before_create(lease, old, new):
    run, client, create, attempt = lease
    source = create.parent.parent / "runner.yml"
    source.write_text(SDL.replace(old, new))
    with pytest.raises(RuntimeError, match="Sentry lease"):
        run()
    client.create_deployment.assert_not_called()
    client.create_lease.assert_not_called()
    assert not create.exists() and not attempt.exists()


def test_foreign_selected_provider_refuses_lease_but_preserves_original_close(lease, monkeypatch):
    run, client, create, attempt = lease
    monkeypatch.setenv("AKASH_PROVIDERS", "foreign-provider")
    client.get_bids.return_value[0]["id"]["provider"] = "foreign-provider"
    with pytest.raises(RuntimeError, match="NON-RETRYABLE LEASE"):
        run()
    assert client.create_deployment.call_count == 1
    client.create_lease.assert_not_called()
    client.close_deployment.assert_called_once_with("111")
    assert not attempt.exists()


def test_no_lease_capture_without_original_create_receipt(tmp_path):
    with (
        patch.object(deployment, "AkashConsoleAPI") as factory,
        pytest.raises(RuntimeError, match="requires the original create receipt"),
    ):
        deployment.deploy("unused", lease_receipt_path=str(tmp_path / "lease.json"))
    factory.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [("dseq", "112"), ("operation_id", "other-operation"), ("artifact_digest", "0" * 64)],
)
def test_actual_create_candidate_disagreement_refuses_lease_and_closes_exact_deployment(
    lease, field, value
):
    run, client, create, attempt = lease
    bids = client.get_bids.return_value

    def collect(*args, **kwargs):
        candidate = json.loads(create.read_bytes())
        candidate[field] = value
        create.write_text(json.dumps(candidate))
        return bids

    client.get_bids.side_effect = collect
    with pytest.raises(RuntimeError, match="NON-RETRYABLE LEASE"):
        run()
    assert client.create_deployment.call_count == 1
    client.create_lease.assert_not_called()
    client.close_deployment.assert_called_once_with("111")
    assert not attempt.exists()
    assert json.loads(create.read_bytes())[field] == value
