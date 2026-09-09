"""Actual automatic recovery/operator entrypoints with transport-denied chain proof."""

from __future__ import annotations

import inspect
import io
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from just_akash import cleanup_identity as reader
from just_akash import close_orphans as co
from just_akash import deploy as dp
from just_akash import wallet_pool
from just_akash.orphan_detect import Classification, DeploymentVerdict
from just_akash.workload_identity import Identity, format_identity

OWNER = "akash1" + "a" * 38
PREFIX = "just-akash-"
REPO = "Digital-Frontier-LDA/just-akash"
REGISTER = {PREFIX: REPO}
NOW = 2_000_000_000
DSEQ = str((NOW - 7200) * 1000)


def name(kind="ci-runner", group=1):
    value = (
        Identity(PREFIX, REPO, kind, group, run=99, attempt=2)
        if kind.startswith("ci-")
        else Identity(PREFIX, REPO, kind, group, release="r1")
    )
    return format_identity(value, REGISTER)


@pytest.fixture
def lifecycle(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("unexpected external transport")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setenv("AKASH_API_KEY", "offline")
    monkeypatch.delenv("AKASH_API_KEYS", raising=False)
    monkeypatch.setattr(dp.time, "time", lambda: NOW)
    monkeypatch.setattr(dp.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(co, "_credit_line", lambda *args: "offline")
    before = {
        "deployment": {"id": {"owner": OWNER, "dseq": DSEQ}, "state": "active"},
        "groups": [
            {"id": {"owner": OWNER, "dseq": DSEQ, "gseq": 1}, "group_spec": {"name": name()}}
        ],
    }
    fixture = json.loads((Path(__file__).parent / "fixtures/closure_wire.json").read_text())
    after = json.loads(
        json.dumps(fixture["closed"]["deployment"])
        .replace('"dseq": "7"', f'"dseq": "{DSEQ}"')
        .replace(f"{OWNER}/7", f"{OWNER}/{DSEQ}")
    )
    state = {
        "deleted": [],
        "created": [],
        "reads": [],
        "before": before,
        "after": after,
        "run": {
            "id": 99,
            "run_attempt": 2,
            "status": "completed",
            "repository": {"full_name": REPO},
        },
        "orphan": True,
        "github_reads": 0,
    }

    def close(dseq):
        assert dseq == DSEQ
        state["deleted"].append(dseq)

    def create(*args, **kwargs):
        state["created"].append(True)
        raise RuntimeError(
            "already exists" if len(state["created"]) == 1 else "replacement requested"
        )

    client = SimpleNamespace(
        account_address=lambda: OWNER,
        get_deployment=lambda d: {"leases": []},
        close_deployment=close,
        create_deployment=create,
    )
    state["client"] = client
    monkeypatch.setattr(co, "AkashConsoleAPI", lambda key: client)
    monkeypatch.setattr(
        co,
        "lease_status",
        lambda *a, **k: [{"dseq": DSEQ, "deployment_state": "active", "active_lease_count": 0}],
    )
    monkeypatch.setattr(
        co,
        "classify_deployment",
        lambda dseq, owner, **kwargs: DeploymentVerdict(
            dseq,
            Classification.ORPHANED if state["orphan"] else Classification.UNKNOWN,
            confirmations=2,
        ),
    )
    monkeypatch.setattr(dp.chain, "list_active_deployments", lambda owner: [{"dseq": DSEQ}])
    monkeypatch.setattr(dp.chain, "rest_urls", lambda: ["https://one.test", "https://two.test"])
    monkeypatch.setattr(reader.shutil, "which", lambda binary: "/offline/gh")

    def github(argv, **kwargs):
        assert argv == ["/offline/gh", "api", f"repos/{REPO}/actions/runs/99"]
        state["github_reads"] += 1
        response = SimpleNamespace(returncode=0, stdout=json.dumps(state["run"]))
        if state.get("change_after_plan") and state["github_reads"] == 1:
            state["run"]["run_attempt"] = 3
        return response

    monkeypatch.setattr(reader.subprocess, "run", github)

    def urlopen(request, timeout=15):
        parsed = urlsplit(request.full_url)
        assert parsed.hostname in {"one.test", "two.test"}
        query = parse_qs(parsed.query)
        state["reads"].append((bool(state["deleted"]), request.full_url))
        if parsed.path.endswith("/deployments/info"):
            assert query == {"id.owner": [OWNER], "id.dseq": [DSEQ]}
            document = after if state["deleted"] else before
        else:
            assert parsed.path.endswith("/leases/list")
            assert state["deleted"]
            assert query["filters.owner"] == [OWNER]
            assert query["filters.dseq"] == [DSEQ]
            document = {"leases": [], "pagination": {"next_key": None, "total": "0"}}
        return io.BytesIO(json.dumps(document).encode())

    monkeypatch.setattr(dp.chain.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        wallet_pool,
        "select_client_for_create",
        lambda *a, **k: SimpleNamespace(client=client, configured_keys=1),
    )
    monkeypatch.setattr(dp, "_resolve_sdl_path", lambda path, gpu: path)
    monkeypatch.setattr(dp, "_prepare_sdl_content", lambda *a, **k: "offline SDL")
    monkeypatch.setattr(dp, "_check_wallet_credit", lambda *a: None)
    monkeypatch.setattr(dp, "_report_suspected_orphans", lambda *a: None)
    monkeypatch.setattr(dp, "emit", lambda *a, **k: None)
    return state


def operator_main(register=REGISTER):
    return co.main(["--dseq", DSEQ, "--execute", "--ownership-register", json.dumps(register)])


def recovery(state, register=REGISTER):
    return dp._close_stale_for_retry(state["client"], now=NOW, ownership_register=register)


@pytest.mark.parametrize("entry", ["operator", "recovery"])
def test_never_leased_completed_ci_closes_with_shared_proof(lifecycle, entry):
    if entry == "operator":
        assert operator_main() == 0
    else:
        result = recovery(lifecycle)
        assert result.closed == [DSEQ]
        assert result.safe_to_retry
    assert lifecycle["deleted"] == [DSEQ]
    assert lifecycle["github_reads"] == 2
    assert sum(not after for after, _ in lifecycle["reads"]) == 4
    assert any(after and "/leases/list?" in url for after, url in lifecycle["reads"])


@pytest.mark.parametrize("entry", ["operator", "recovery"])
@pytest.mark.parametrize(
    "kind", ["prod-payload", "staging-payload", "legacy", "unreadable", "wrong_group"]
)
def test_protected_sibling_group_holds_both_paths(lifecycle, entry, kind):
    sibling = (
        name(kind, 2) if kind in {"prod-payload", "staging-payload"} else "just-akash-runner-old"
    )
    if kind == "unreadable":
        sibling = None
    if kind == "wrong_group":
        sibling = name(group=3)
    lifecycle["before"]["groups"].append(
        {"id": {"owner": OWNER, "dseq": DSEQ, "gseq": 2}, "group_spec": {"name": sibling}}
    )
    if entry == "operator":
        assert operator_main() != 0
    else:
        result = recovery(lifecycle)
        assert result.held == [DSEQ]
        assert not result.safe_to_retry
    assert not lifecycle["deleted"]


@pytest.mark.parametrize("entry", ["operator", "recovery"])
@pytest.mark.parametrize("gap", ["register", "attempt", "live", "repo", "open_escrow"])
def test_missing_or_conflicting_evidence_never_counts_success(lifecycle, entry, gap):
    register = REGISTER
    if gap == "register":
        register = None
    elif gap == "attempt":
        lifecycle["run"]["run_attempt"] = 3
    elif gap == "live":
        lifecycle["run"]["status"] = "in_progress"
    elif gap == "repo":
        lifecycle["run"]["repository"]["full_name"] = "wrong/repository"
    else:
        lifecycle["after"]["escrow_account"]["state"]["state"] = "open"
    if entry == "operator":
        assert operator_main(register) != 0
    else:
        result = recovery(lifecycle, register)
        assert not result.closed
        assert not result.safe_to_retry
    assert len(lifecycle["deleted"]) == int(gap == "open_escrow")


@pytest.mark.parametrize("verified", [True, False])
def test_actual_create_collision_only_retries_after_verified_cleanup(lifecycle, verified):
    if not verified:
        lifecycle["after"]["escrow_account"]["state"]["state"] = "open"
    with pytest.raises(
        RuntimeError, match="replacement requested" if verified else "refusing replacement"
    ):
        dp.deploy("offline.yml", cleanup_ownership_register=REGISTER)
    assert len(lifecycle["created"]) == (2 if verified else 1)
    assert lifecycle["deleted"] == [DSEQ]


def test_actual_create_collision_without_register_never_sweeps(lifecycle):
    with pytest.raises(RuntimeError, match="refusing replacement"):
        dp.deploy("offline.yml")
    assert len(lifecycle["created"]) == 1
    assert not lifecycle["deleted"]


@pytest.mark.parametrize("entry", ["operator", "recovery"])
def test_final_authorization_callsite_mutation_exposes_new_attempt(lifecycle, monkeypatch, entry):
    lifecycle["change_after_plan"] = True
    if entry == "operator":
        assert operator_main() != 0
        module, function = co, co.run
        target = """        # Final identity authorization immediately precedes DELETE.
        allowed, reason = cleanup_identity.eligible(
            address, dseq, placement_prefix, ownership_register
        )"""
        replacement = (
            "        # Final identity authorization immediately precedes DELETE.\n"
            '        allowed, reason = True, "mutated"'
        )
    else:
        assert not recovery(lifecycle).safe_to_retry
        module, function = dp, dp._close_stale_for_retry
        target = """    for _age, dseq in candidates[:STALE_RETRY_MAX_CLOSE]:
        allowed, reason = cleanup_identity.eligible(
            owner, dseq, placement_prefix, ownership_register
        )"""
        replacement = (
            "    for _age, dseq in candidates[:STALE_RETRY_MAX_CLOSE]:\n"
            '        allowed, reason = True, "mutated"'
        )
    assert not lifecycle["deleted"]
    source = inspect.getsource(function)
    assert source.count(target) == 1
    # Keep actual module globals so fixture transport bindings remain authoritative.
    namespace = vars(module)
    assert isinstance(namespace, dict)
    exec(source.replace(target, replacement), namespace)
    mutant = getattr(module, function.__name__)
    setattr(module, function.__name__, function)
    monkeypatch.setattr(module, function.__name__, mutant)
    lifecycle["run"]["run_attempt"] = 2
    lifecycle["github_reads"] = 0
    if entry == "operator":
        assert operator_main() == 0
    else:
        assert recovery(lifecycle).safe_to_retry
    assert lifecycle["run"]["run_attempt"] == 3
    assert lifecycle["deleted"] == [DSEQ]


def test_actual_replacement_callsite_mutation_exposes_open_escrow(lifecycle, monkeypatch):
    lifecycle["after"]["escrow_account"]["state"]["state"] = "open"
    with pytest.raises(RuntimeError, match="refusing replacement"):
        dp.deploy("offline.yml", cleanup_ownership_register=REGISTER)
    assert len(lifecycle["created"]) == 1
    source = inspect.getsource(dp.deploy)
    target = "if not recovery.safe_to_retry:"
    assert source.count(target) == 1
    original = dp.deploy
    namespace = vars(dp)
    assert isinstance(namespace, dict)
    exec(source.replace(target, "if False:"), namespace)
    mutant = dp.deploy
    dp.deploy = original
    monkeypatch.setattr(dp, "deploy", mutant)
    lifecycle["created"].clear()
    lifecycle["deleted"].clear()
    with pytest.raises(RuntimeError, match="replacement requested"):
        dp.deploy("offline.yml", cleanup_ownership_register=REGISTER)
    assert len(lifecycle["created"]) == 2


@pytest.mark.parametrize("standalone", [False, True])
def test_both_actual_deploy_clis_forward_explicit_registry(lifecycle, monkeypatch, standalone):
    from just_akash import cli

    calls = []

    def deploy(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("offline forwarding boundary")

    monkeypatch.setattr(dp, "deploy", deploy)
    argv = ["just-akash"] + ([] if standalone else ["deploy"])
    argv += [
        "--cleanup-ownership-register",
        json.dumps(REGISTER),
        "--cleanup-placement-prefix",
        PREFIX,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as stopped:
        (dp.deploy_main if standalone else cli.main)()
    assert stopped.value.code == 1
    assert len(calls) == 1
    assert calls[0]["cleanup_ownership_register"] == REGISTER
    assert calls[0]["cleanup_placement_prefix"] == PREFIX


def test_operator_workflow_registry_reaches_real_module_main(lifecycle):
    import contextlib
    import shlex

    import yaml

    workflow = yaml.safe_load(
        (Path(__file__).parents[1] / ".github/workflows/close-orphans.yml").read_text()
    )
    assert workflow["permissions"]["actions"] == "read"
    steps = workflow["jobs"]["close"]["steps"]
    callers = [
        step for step in steps if "python -m just_akash.close_orphans" in step.get("run", "")
    ]
    assert len(callers) == 1
    step = callers[0]
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    values = {"DSEQS": DSEQ}
    lines = [line.strip() for line in step["run"].splitlines()]
    # Execute the workflow's actual Python declarations, not a duplicated registry.
    for variable in ("PREFIX", "REGISTER"):
        commands = [line for line in lines if line.startswith(variable + "=$(")]
        assert len(commands) == 1
        argv = shlex.split(commands[0][len(variable) + 3 : -1])
        assert argv[:4] == ["uv", "run", "python", "-c"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(argv[4], {})
        values[variable] = output.getvalue().strip()
    arguments = [line for line in lines if line.startswith("ARGS=(")]
    assert len(arguments) == 1
    argv = shlex.split(arguments[0][6:-1])
    argv = [values[arg[1:]] if arg.startswith("$") else arg for arg in argv]
    assert json.loads(values["REGISTER"]) == REGISTER
    assert co.main([*argv, "--execute"]) == 0
    assert lifecycle["deleted"] == [DSEQ]


def test_active_identity_missing_from_console_population_is_held(lifecycle, monkeypatch):
    monkeypatch.setattr(co, "lease_status", lambda *a, **k: [])
    assert operator_main() == 2
    assert not lifecycle["deleted"]


def test_orphan_condition_is_rechecked_before_delete(lifecycle, monkeypatch):
    calls = []

    def classify(dseq, owner, **kwargs):
        calls.append(dseq)
        return DeploymentVerdict(
            dseq,
            Classification.ORPHANED if len(calls) == 1 else Classification.UNKNOWN,
            confirmations=2,
        )

    monkeypatch.setattr(co, "classify_deployment", classify)
    assert operator_main() == 2
    assert calls == [DSEQ, DSEQ]
    assert not lifecycle["deleted"]
