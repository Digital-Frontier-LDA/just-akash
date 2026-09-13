"""The paid secrets E2E has a durable create identity before its first POST."""

from __future__ import annotations

import inspect
import signal
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from just_akash import paid_create as paid
from just_akash import test_secrets_e2e as target
from just_akash.deployment_receipt import (
    mark_create_response_received,
    mark_submitting,
    prepare_receipt,
)

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
OPERATION_ID = "e2e-secrets-test"
RUN_ID = "abc123def456"
GROUP = f"just-akash-secrets.{RUN_ID}"
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
    just-akash-secrets.abc123def456:
      pricing:
        app: {denom: uakt, amount: 1}
deployment:
  app:
    just-akash-secrets.abc123def456:
      profile: app
      count: 1
"""


def _prepared(tmp_path: Path, *, sdl: str = SDL, operation_id: str = OPERATION_ID):
    tmp_path.chmod(0o700)
    path = tmp_path / "receipt.json"
    return prepare_receipt(
        str(path),
        operation_id=operation_id,
        owner=OWNER,
        sdl_content=sdl,
    )


def test_a_response_receipt_recovers_exact_dseq_without_population_probe(
    monkeypatch, tmp_path: Path
) -> None:
    submitting = mark_submitting(*_prepared(tmp_path))
    mark_create_response_received(*submitting, dseq="1002", deployment_response={"dseq": "1002"})
    monkeypatch.setattr(
        paid.chain,
        "list_active_deployments",
        lambda *_: (_ for _ in ()).throw(AssertionError("response receipt must not probe")),
    )
    assert paid.reconcile_receipt(submitting[0], OPERATION_ID, 123.0, {}) == "1002"


def test_submitting_receipt_requires_exact_two_source_population_and_verified_close(
    monkeypatch, tmp_path: Path
) -> None:
    submitting = mark_submitting(*_prepared(tmp_path))
    dseq = "123000"
    calls = []
    monkeypatch.setattr(paid.chain, "list_active_deployments", lambda owner: [{"dseq": dseq}])
    monkeypatch.setattr(
        paid.chain,
        "corroborated_deployment_group_population",
        lambda owner, got_dseq, population: (
            calls.append((owner, got_dseq, population)) or population
        ),
    )
    monkeypatch.setattr(
        paid, "robust_destroy", lambda dseq, **kw: calls.append((dseq, kw)) or True
    )
    ref = {}
    assert paid.reconcile_receipt(submitting[0], OPERATION_ID, 123.0, ref) is None
    assert calls[0] == (OWNER, dseq, [{"gseq": 1, "name": GROUP}])
    assert calls[1] == (dseq, {"owner": OWNER, "groups": [{"gseq": 1, "name": GROUP}]})
    assert not submitting[0].exists(), "verified cleanup must consume exactly its receipt"


def _sdl_for_groups(groups: list[str]) -> str:
    placements = "\n".join(
        f"    {group}:\n      pricing:\n        app: {{denom: uakt, amount: 1}}"
        for group in groups
    )
    deployment = "\n".join(f"    {group}:\n      profile: app\n      count: 1" for group in groups)
    return SDL.replace(
        "    just-akash-secrets.abc123def456:\n"
        "      pricing:\n"
        "        app: {denom: uakt, amount: 1}",
        placements,
    ).replace(
        "    just-akash-secrets.abc123def456:\n      profile: app\n      count: 1",
        deployment,
    )


@pytest.mark.parametrize("observed", [[], [{"gseq": 1, "name": "partial"}]])
def test_disagreeing_or_partial_population_and_unverified_close_retain_receipt(
    monkeypatch, tmp_path: Path, observed
) -> None:
    submitting = mark_submitting(*_prepared(tmp_path))
    monkeypatch.setattr(paid.chain, "list_active_deployments", lambda owner: [{"dseq": "123000"}])
    monkeypatch.setattr(
        paid.chain, "corroborated_deployment_group_population", lambda *args: observed
    )
    closes = []
    monkeypatch.setattr(
        paid, "robust_destroy", lambda *args, **kw: closes.append((args, kw)) or False
    )
    assert paid.reconcile_receipt(submitting[0], OPERATION_ID, 123.0, {}) is None
    assert closes == [] and submitting[0].exists()


def test_returned_close_without_two_source_terminal_verdict_retains_receipt(
    monkeypatch, tmp_path: Path
) -> None:
    submitting = mark_submitting(*_prepared(tmp_path))
    population = [{"gseq": 1, "name": GROUP}]
    monkeypatch.setattr(paid.chain, "list_active_deployments", lambda owner: [{"dseq": "123000"}])
    monkeypatch.setattr(
        paid.chain, "corroborated_deployment_group_population", lambda *args: population
    )
    monkeypatch.setattr(paid, "robust_destroy", lambda *args, **kwargs: False)
    assert paid.reconcile_receipt(submitting[0], OPERATION_ID, 123.0, {}) is None
    assert submitting[0].exists()


def test_later_multiple_exact_candidates_are_ambiguous_and_remain_held(
    monkeypatch, tmp_path: Path
) -> None:
    submitting = mark_submitting(*_prepared(tmp_path))
    population = [{"gseq": 1, "name": GROUP}]
    monkeypatch.setattr(
        paid.chain,
        "list_active_deployments",
        lambda owner: [{"dseq": "123000"}, {"dseq": "123001"}],
    )
    monkeypatch.setattr(
        paid.chain, "corroborated_deployment_group_population", lambda *args: population
    )
    closes = []
    monkeypatch.setattr(
        paid, "robust_destroy", lambda *args, **kwargs: closes.append((args, kwargs)) or True
    )
    assert paid.reconcile_receipt(submitting[0], OPERATION_ID, 123.0, {}) is None
    assert closes == []
    assert submitting[0].exists()


def test_receipt_operation_id_disagreement_is_held_independently_of_provenance(
    monkeypatch, tmp_path: Path
) -> None:
    submitting = mark_submitting(*_prepared(tmp_path))
    monkeypatch.setattr(
        paid.chain,
        "list_active_deployments",
        lambda *_: (_ for _ in ()).throw(AssertionError("operation mismatch must hold first")),
    )
    assert paid.reconcile_receipt(submitting[0], "another-operation", 123.0, {}) is None


def test_operation_id_must_be_distinct_from_the_group_provenance_stamp(
    monkeypatch, tmp_path: Path
) -> None:
    submitting = mark_submitting(*_prepared(tmp_path, operation_id=RUN_ID))
    monkeypatch.setattr(
        paid.chain,
        "list_active_deployments",
        lambda *_: (_ for _ in ()).throw(AssertionError("non-distinct identity must hold")),
    )
    assert paid.reconcile_receipt(submitting[0], RUN_ID, 123.0, {}) is None
    assert submitting[0].exists()


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


def test_base_exception_terminates_the_complete_just_up_process_group(monkeypatch) -> None:
    class Process:
        pid = 9876
        returncode = -signal.SIGTERM

        def __init__(self) -> None:
            self.calls = 0

        def communicate(self, timeout: float | None = None):
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt
            return "", ""

    process = Process()
    signals = []
    monkeypatch.setattr(target.subprocess, "Popen", lambda *_, **__: process)
    monkeypatch.setattr(target.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    with pytest.raises(KeyboardInterrupt):
        target._run_just_up({"PATH": "/bin"}, timeout=1)
    assert signals == [(9876, signal.SIGTERM)]


def test_stubborn_process_group_escalates_from_sigterm_to_sigkill(monkeypatch) -> None:
    class Process:
        pid = 2468
        returncode = -signal.SIGKILL

        def __init__(self) -> None:
            self.calls = 0

        def communicate(self, timeout: float | None = None):
            self.calls += 1
            if self.calls <= 2:
                assert timeout is not None, "both pre-kill waits must be bounded"
                raise subprocess.TimeoutExpired(["just", "up"], timeout)
            assert timeout is None, "SIGKILL must be followed by complete reaping"
            return "out", "err"

    process = Process()
    signals = []
    monkeypatch.setattr(target.subprocess, "Popen", lambda *_, **__: process)
    monkeypatch.setattr(target.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    result, timed_out = target._run_just_up({"PATH": "/bin"}, timeout=1)
    assert timed_out is True and result.returncode == -signal.SIGKILL
    assert process.calls == 3
    assert signals == [(2468, signal.SIGTERM), (2468, signal.SIGKILL)]


def test_runner_receipt_path_is_deterministic_and_private(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    path, _, env = target._receipt_environment()
    assert path == tmp_path / "just-akash-secrets-receipt" / "create.json"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert set(env) == {"JUST_AKASH_RECEIPT_PATH", "JUST_AKASH_RECEIPT_OPERATION_ID"}


def test_ci_always_uploads_an_unresolved_receipt_for_thirty_days() -> None:
    workflow = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    assert workflow.count("name: Preserve unresolved deployment receipt") == 1
    block = workflow.split("name: Preserve unresolved deployment receipt", 1)[1].split("\n\n", 1)[
        0
    ]
    assert "if: always()" in block
    assert "${{ runner.temp }}/just-akash-secrets-receipt/create.json" in block
    assert "retention-days: 30" in block


def test_unverified_cleanup_preserves_receipt_and_bound_identity(
    monkeypatch, tmp_path: Path
) -> None:
    receipt = tmp_path / "create.json"
    receipt.write_text("recovery seed")
    identity = {
        "dseq": "1002",
        "owner": OWNER,
        "groups": [{"gseq": 1, "name": "group-one"}],
        "receipt_path": receipt,
    }
    calls = []
    monkeypatch.setattr(
        target,
        "robust_destroy",
        lambda dseq, **kwargs: calls.append((dseq, kwargs)) or False,
    )
    assert target._verified_cleanup(identity) is False
    assert receipt.read_text() == "recovery seed"
    assert identity["dseq"] == "1002"
    assert calls == [("1002", {"owner": OWNER, "groups": [{"gseq": 1, "name": "group-one"}]})]


def test_verified_cleanup_removes_receipt_and_finish_call_site_has_an_observable_effect(
    monkeypatch, tmp_path: Path
) -> None:
    receipt = tmp_path / "create.json"
    receipt.write_text("recovery seed")
    identity = {
        "dseq": "1002",
        "owner": OWNER,
        "groups": [{"gseq": 1, "name": "group-one"}],
        "receipt_path": receipt,
    }
    monkeypatch.setattr(target, "robust_destroy", lambda *_args, **_kwargs: True)
    assert target._verified_cleanup(identity) is True
    assert identity["dseq"] is None and not receipt.exists()

    source = textwrap.dedent(inspect.getsource(target._finish))
    call = "_verified_cleanup(dseq_ref)"
    assert source.count(call) == 1, f"cleanup call-site target count changed: {source.count(call)}"
    mutated = source.replace(call, "None")
    assert mutated != source and mutated.count(call) == 0

    call_identity = {
        "dseq": "1002",
        "owner": OWNER,
        "groups": [{"gseq": 1, "name": "group-one"}],
    }
    effects = []
    namespace = {
        "_verified_cleanup": lambda ref: effects.append(ref.copy()),
        "_summary": lambda _: None,
        "sys": target.sys,
    }
    exec(mutated, namespace)
    with pytest.raises(SystemExit):
        namespace["_finish"]([], call_identity)
    assert effects == [], "the bypass mutation unexpectedly retained the cleanup effect"

    monkeypatch.setattr(target, "_verified_cleanup", lambda ref: effects.append(ref.copy()))
    monkeypatch.setattr(target, "_summary", lambda _: None)
    with pytest.raises(SystemExit):
        target._finish([], call_identity)
    assert effects == [call_identity]


def test_receipt_and_reconciliation_surround_every_just_up_exit() -> None:
    source = inspect.getsource(target.main)
    receipt = source.index("_receipt_environment()")
    create = source.index("_run_just_up(")
    reconcile = source.index("_reconcile_receipt(")
    first_exit = source.index("sys.exit", create)
    assert receipt < create < reconcile < first_exit


def test_just_up_passes_the_complete_receipt_identity() -> None:
    source = (Path(__file__).parents[1] / "Justfile").read_text(encoding="utf-8")
    start = source.index('up tag="":')
    end = source.index("\n# Connect to a running instance", start)
    recipe = source[start:end]
    for flag in ("--receipt-path", "--receipt-operation-id"):
        assert recipe.count(flag) == 1
    for predicted_identity in (
        "--receipt-expected-owner",
        "--receipt-expected-group",
        "--receipt-artifact-sha256",
    ):
        assert predicted_identity not in recipe
