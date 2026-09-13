"""The paid secrets E2E has a durable create identity before its first POST."""

from __future__ import annotations

import inspect
import signal
import subprocess
from pathlib import Path

from just_akash import test_secrets_e2e as target
from just_akash.deployment_receipt import (
    mark_create_response_received,
    mark_submitting,
    prepare_receipt,
    sha256_bytes,
)

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
SDL = """---
version: "2.0"
services:
  app:
    image: example.invalid/image:latest
    expose: []
profiles:
  compute:
    app:
      resources:
        cpu: {units: 1}
        memory: {size: 1Gi}
        storage: {size: 1Gi}
  placement:
    group-one:
      pricing:
        app: {denom: uakt, amount: 1}
deployment:
  app:
    group-one:
      profile: app
      count: 1
"""


def _prepared(tmp_path: Path):
    tmp_path.chmod(0o700)
    path = tmp_path / "receipt.json"
    return prepare_receipt(
        str(path),
        operation_id="e2e-secrets-test",
        expected_owner=OWNER,
        expected_groups=["group-one"],
        expected_artifact_digest=sha256_bytes(SDL.encode()),
        sdl_content=SDL,
    )


def test_a_submitting_receipt_runs_reconciliation_once(monkeypatch, tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    mark_submitting(*prepared)
    calls = []
    client = object()
    monkeypatch.setattr(target, "AkashConsoleAPI", lambda _api_key: client)
    monkeypatch.setattr(
        target,
        "_report_suspected_orphans",
        lambda got_client, started, operation: calls.append((got_client, started, operation)),
    )

    assert target._reconcile_receipt(prepared[0], "operation-7", 123.0, "test-key") is None
    assert calls == [(client, 123.0, "operation-7")]


def test_a_response_receipt_recovers_exact_dseq_without_population_probe(
    monkeypatch, tmp_path: Path
) -> None:
    submitting = mark_submitting(*_prepared(tmp_path))
    mark_create_response_received(*submitting, dseq="1002", deployment_response={"dseq": "1002"})
    monkeypatch.setattr(
        target,
        "_report_suspected_orphans",
        lambda *_: (_ for _ in ()).throw(AssertionError("response receipt must not probe")),
    )

    assert target._reconcile_receipt(submitting[0], "operation-7", 123.0, "test-key") == "1002"


def test_timeout_terminates_the_complete_just_up_process_group(monkeypatch) -> None:
    class Process:
        pid = 4321
        returncode = -signal.SIGTERM

        def __init__(self) -> None:
            self.calls = 0

        def communicate(self, timeout: float | None = None):
            self.calls += 1
            if self.calls == 1:
                if timeout is None:
                    raise AssertionError("the first communicate call must be time-bounded")
                raise subprocess.TimeoutExpired(["just", "up"], timeout)
            return "out", "err"

    process = Process()
    popen_calls = []
    signals = []
    monkeypatch.setattr(
        target.subprocess,
        "Popen",
        lambda *args, **kwargs: popen_calls.append((args, kwargs)) or process,
    )
    monkeypatch.setattr(target.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    result, timed_out = target._run_just_up({"PATH": "/bin"}, timeout=1)
    assert timed_out is True and result.returncode == -signal.SIGTERM
    assert signals == [(4321, signal.SIGTERM)]
    assert popen_calls[0][1]["start_new_session"] is True


def test_receipt_and_reconciliation_surround_every_just_up_exit() -> None:
    source = inspect.getsource(target.main)
    receipt = source.index("_receipt_environment(api_key)")
    create = source.index("_run_just_up(")
    reconcile = source.index("_reconcile_receipt(")
    first_exit = source.index("sys.exit", create)
    assert receipt < create < reconcile < first_exit


def test_just_up_passes_the_complete_receipt_identity() -> None:
    source = (Path(__file__).parents[1] / "justfile").read_text(encoding="utf-8")
    start = source.index('up tag="":')
    end = source.index("\n# Connect to a running instance", start)
    recipe = source[start:end]
    for flag in (
        "--receipt-path",
        "--receipt-expected-owner",
        "--receipt-expected-group",
        "--receipt-artifact-sha256",
        "--receipt-operation-id",
    ):
        assert recipe.count(flag) == 1
