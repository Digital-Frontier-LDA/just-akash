"""Actual SDK lease wire keeps a durable candidate, never retry authority."""

import json
from unittest.mock import patch

import pytest

from just_akash import deploy as deployment
from just_akash.runner_image import NATIVE_READER_PROVIDERS

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
PROVIDER = sorted(NATIVE_READER_PROVIDERS)[0]
GROUP = "borduas-sentry-idv1-class-ci-runner-g1-attempt-2-run-123-end"
SDL = f'''version: "2.0"
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
'''


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
    with patch.object(deployment, "AkashConsoleAPI") as factory:
        client = factory.return_value
        client.account_address.return_value = OWNER
        client.create_deployment.return_value = {"dseq": "111", "manifest": "private-fixture-manifest"}
        client.get_bids.return_value = [
            {"id": {"provider": PROVIDER, "gseq": 1}, "price": {"amount": "10", "denom": "uact"}, "state": "open"}
        ]
        client.create_lease.return_value = {"result": "candidate-fixture"}
        def run():
            return deployment.deploy(
                str(source), bid_wait=0, bid_wait_retry=0,
                receipt_path=str(create), receipt_operation_id="sentry-123-2-build",
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
            "cpu_millicores": 2000, "memory_bytes": 6 * 1024**3,
            "storage_bytes": 40 * 1024**3, "gpu_count": 0, "replicas": 1,
        }
        assert json.loads(create.read_bytes())["state"] == "create_response_received"
        return {"result": "candidate-fixture"}
    client.create_lease.side_effect = send
    assert run()["dseq"] == "111"
    assert client.create_lease.call_count == 1
    candidate = json.loads(attempt.with_suffix(".response.json").read_bytes())
    assert candidate["state"] == "RESPONSE_CANDIDATE"
    assert candidate["runner_binding_verified"] is candidate["publication_authority"] is False
    assert json.loads(attempt.read_bytes())["state"] == "UNKNOWN"


@pytest.mark.parametrize("message", ["JWT has invalid claims", "selected bid is no longer open", "lost ACK"])
def test_ambiguous_lease_never_retries_reselects_or_closes(lease, message):
    run, client, create, attempt = lease
    client.create_lease.side_effect = RuntimeError(message)
    with pytest.raises(RuntimeError, match="NON-RETRYABLE LEASE"):
        run()
    assert client.create_deployment.call_count == client.create_lease.call_count == 1
    client.close_deployment.assert_not_called()
    assert json.loads(attempt.read_bytes())["state"] == "UNKNOWN"
    assert not attempt.with_suffix(".response.json").exists()
    assert json.loads(create.read_bytes())["state"] == "create_response_received"
