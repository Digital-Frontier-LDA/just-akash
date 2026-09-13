"""Mutation guards for every command that can create paid Akash escrow."""

from __future__ import annotations

import ast
import signal
import subprocess
from pathlib import Path

import pytest

from just_akash import _e2e, api, chain, paid_create, wallet_pool

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"

ROOT = Path(__file__).parents[1]
PATHS = {
    "secrets": ROOT / "just_akash/test_secrets_e2e.py",
    "shell": ROOT / "just_akash/test_shell_e2e.py",
    "lifecycle": ROOT / "just_akash/test_lifecycle.py",
    "provider": ROOT / "just_akash/smoke_providers.py",
}
TARGETS = {
    "secrets": "_run_just_up",  # pragma: allowlist secret -- module category, no credential
    "shell": "run_process_group",
    "lifecycle": "run_process_group",
    "provider": "_run",
}
RECONCILE_NAMES = {
    "secrets": "_reconcile_receipt",  # pragma: allowlist secret
    "shell": "reconcile_receipt",
    "lifecycle": "reconcile_receipt",
    "provider": "reconcile_receipt",
}


def _paid_calls(source: str, target: str) -> list[ast.Call]:
    calls = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != target:
            continue
        rendered = ast.unparse(node)
        if "receipt_env" in rendered and (
            target == "_run_just_up" or "'just', 'up'" in rendered or "command" in rendered
        ):
            calls.append(node)
    return calls


def _reconcile_calls(source: str, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def _assigned_reconciliation_calls(source: str, name: str) -> list[ast.Assign]:
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(
            isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == name
            for call in ast.walk(node.value)
        )
    ]


@pytest.mark.parametrize("path_name", PATHS)
def test_reconciliation_never_overwrites_the_dseq_carrier_and_mutation_is_red(
    path_name: str,
) -> None:
    source = PATHS[path_name].read_text(encoding="utf-8")
    name = RECONCILE_NAMES[path_name]
    calls = _reconcile_calls(source, name)
    assert len(calls) == 2, f"{path_name} must reconcile normal and interruption paths"
    assert _assigned_reconciliation_calls(source, name) == []

    call = calls[0]
    lines = source.splitlines(keepends=True)
    absolute_start = sum(len(line) for line in lines[: call.lineno - 1]) + call.col_offset
    mutant = source[:absolute_start] + 'dseq_ref["dseq"] = ' + source[absolute_start:]
    assigned = _assigned_reconciliation_calls(mutant, name)
    assert len(assigned) == 1, "assigning the None cleanup result must trip the carrier guard"


def test_secrets_normal_and_interruption_reconciliation_share_the_live_carrier() -> None:
    source = PATHS["secrets"].read_text(encoding="utf-8")
    calls = _reconcile_calls(source, "_reconcile_receipt")
    assert len(calls) == 2
    assert all(
        call.args and isinstance(call.args[-1], ast.Name) and call.args[-1].id == "dseq_ref"
        for call in calls
    )

    target = "                    dseq_ref,\n"
    assert source.count(target) == 1, "interruption call mutation target must apply exactly once"
    mutant = source.replace(target, "                    {},\n", 1)
    mutant_calls = _reconcile_calls(mutant, "_reconcile_receipt")
    assert (
        sum(
            isinstance(call.args[-1], ast.Name) and call.args[-1].id == "dseq_ref"
            for call in mutant_calls
        )
        == 1
    )


@pytest.mark.parametrize("path_name", PATHS)
def test_each_paid_create_has_exactly_one_receipt_bound_contained_call_and_mutation(
    path_name: str,
) -> None:
    source = PATHS[path_name].read_text(encoding="utf-8")
    target = TARGETS[path_name]
    calls = _paid_calls(source, target)
    assert len(calls) == 1, f"{path_name} has {len(calls)} paid create effects"

    call = calls[0]
    segment = ast.get_source_segment(source, call)
    assert segment is not None and segment.count(target) == 1
    assert call.end_lineno is not None and call.end_col_offset is not None
    lines = source.splitlines(keepends=True)
    absolute_start = sum(len(line) for line in lines[: call.lineno - 1]) + call.col_offset
    absolute_end = sum(len(line) for line in lines[: call.end_lineno - 1]) + call.end_col_offset
    mutated = (
        source[:absolute_start]
        + segment.replace(target, "uncontained_paid_create", 1)
        + source[absolute_end:]
    )
    assert _paid_calls(mutated, target) == [], "the exact-one call mutation must kill the guard"
    assert segment.count("receipt_env") == 1, "receipt effect mutation must apply exactly once"
    effect_mutant = (
        source[:absolute_start]
        + segment.replace("receipt_env", "unbound_env", 1)
        + source[absolute_end:]
    )
    assert _paid_calls(effect_mutant, target) == [], "removing receipt binding must kill the guard"


def test_shared_paid_runner_terminates_and_reaps_a_stubborn_process_group(monkeypatch) -> None:
    class Process:
        pid = 2468
        returncode = -signal.SIGKILL

        def __init__(self) -> None:
            self.calls = 0

        def communicate(self, timeout: float | None = None):
            self.calls += 1
            if self.calls <= 2:
                assert timeout is not None
                raise subprocess.TimeoutExpired(["just", "up"], timeout)
            assert timeout is None
            return "out", "err"

    process = Process()
    signals = []
    monkeypatch.setattr(paid_create.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(paid_create.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    result, timed_out = paid_create.run_process_group(
        ["just", "up"], env={"PATH": "/bin"}, timeout=1
    )
    assert timed_out and result.returncode == -signal.SIGKILL
    assert process.calls == 3
    assert signals == [(2468, signal.SIGTERM), (2468, signal.SIGKILL)]


def test_shared_paid_runner_settles_group_before_propagating_base_exception(monkeypatch) -> None:
    class Process:
        pid = 9753
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
    monkeypatch.setattr(paid_create.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(paid_create.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    with pytest.raises(KeyboardInterrupt):
        paid_create.run_process_group(["just", "up"], env={"PATH": "/bin"}, timeout=1)
    assert process.calls == 2
    assert signals == [(9753, signal.SIGTERM)]


def test_complete_population_helper_requires_exact_two_source_result(monkeypatch) -> None:
    population = [{"gseq": 1, "name": "first"}, {"gseq": 2, "name": "second"}]
    calls = []
    monkeypatch.delenv("AKASH_REST_URL", raising=False)
    monkeypatch.setattr(
        chain,
        "_source_registry_digest",
        lambda sources: chain.OWNER_CORROBORATION_REGISTRY_SHA256,
    )
    monkeypatch.setattr(
        chain,
        "_corroborated_deployment_group_names",
        lambda *args, **kwargs: calls.append(kwargs) or ["first", "second"],
    )
    assert chain.corroborated_deployment_group_population(OWNER, "123", population) == population
    assert calls[0]["expected_population"] == (("1", "first"), ("2", "second"))

    monkeypatch.setattr(
        chain, "_corroborated_deployment_group_names", lambda *args, **kwargs: ["first"]
    )
    assert chain.corroborated_deployment_group_population(OWNER, "123", population) == []


def test_robust_destroy_selects_receipt_owner_and_all_groups_then_requires_closure(
    monkeypatch,
) -> None:
    population = [{"gseq": 1, "name": "first"}, {"gseq": 2, "name": "second"}]
    closed = []

    class Client:
        def __init__(self, key: str) -> None:
            self.key = key

        def account_address(self) -> str:
            return (
                OWNER
                if self.key == "owner-key"
                else "akash1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqmcn030"
            )

        def close_deployment(self, dseq: str) -> None:
            closed.append((self.key, dseq))

    monkeypatch.setattr(
        chain, "corroborated_deployment_group_population", lambda *args: population
    )
    monkeypatch.setattr(wallet_pool, "configured_api_keys", lambda: ["other-key", "owner-key"])
    monkeypatch.setattr(api, "AkashConsoleAPI", Client)
    monkeypatch.setattr(_e2e, "_confirm_settled", lambda dseq, owner: True)
    monkeypatch.setattr(_e2e.time, "sleep", lambda seconds: None)

    assert _e2e.robust_destroy("123", owner=OWNER, groups=population, retries=0)
    assert closed == [("owner-key", "123")]
