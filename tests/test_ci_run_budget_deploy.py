"""Deployment budget wiring; extracted tests are secondary, not import proof.

The actual-import cases require the real declared akash-lease-core dependency.
No compatibility shim or replacement SDK is used by the secondary AST checks.
"""

import ast
import importlib
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from just_akash.api import AkashConsoleAPI, CIConsoleAPI
from just_akash.ci_run_budget import CIRunBudgetError, ci_required

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("AKASH_CI_BUDGET_REQUIRED", raising=False)
    monkeypatch.setattr(
        "urllib.request.HTTPSHandler.https_open", Mock(side_effect=AssertionError("network"))
    )


def parsed():
    tree = ast.parse((ROOT / "just_akash/deploy.py").read_text())
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_create_with_budget_intent"
    )
    deploy = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "deploy"
    )
    return helper, deploy


def extracted_namespace(client):
    helper, _ = parsed()
    namespace = {
        "ci_required": ci_required,
        "CIRunBudgetError": CIRunBudgetError,
        "client": client,
        "sdl_content": "services: {}",
        "deposit": 1,
        "receipt_operation_id": None,
        "prepared_receipt": None,
        "uuid": SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="fresh-intent")),
        "_create_started": 0,
        "_RUN_ID": "run",
        "time": SimpleNamespace(time=lambda: 0, sleep=Mock()),
        "logging": logging,
        "_log": Mock(),
        "error_text": str,
        "display": lambda value, _kind: value,
        "_close_stale_for_retry": Mock(),
        "_report_suspected_orphans": Mock(),
    }
    exec(
        compile(ast.Module(body=[helper], type_ignores=[]), "<extracted-helper>", "exec"),
        namespace,
    )
    return namespace


def initial_section():
    _, deploy = parsed()
    start = next(
        index
        for index, node in enumerate(deploy.body)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "create_operation_id"
            for target in node.targets
        )
    )
    assert isinstance(deploy.body[start + 1], ast.If)
    assert isinstance(deploy.body[start + 2], ast.Try)
    return ast.Module(body=deploy.body[start : start + 3], type_ignores=[])


@pytest.mark.parametrize(
    "code", ["CI_BUDGET_EXHAUSTED", "CI_BUDGET_OPERATION_REPLAY", "CI_BUDGET_OUTCOME_UNKNOWN"]
)
def test_secondary_initial_terminal_never_cleanup_retry_or_rewrap(code):
    client = SimpleNamespace(
        _ci_run_budget_required=True, create_deployment=Mock(side_effect=CIRunBudgetError(code))
    )
    namespace = extracted_namespace(client)
    with pytest.raises(CIRunBudgetError, match=code):
        exec(compile(initial_section(), "<extracted-initial-create>", "exec"), namespace)
    client.create_deployment.assert_called_once()
    namespace["_close_stale_for_retry"].assert_not_called()
    namespace["_report_suspected_orphans"].assert_not_called()


def test_secondary_stale_retry_retains_same_intent():
    client = SimpleNamespace(
        _ci_run_budget_required=True,
        create_deployment=Mock(
            side_effect=[
                RuntimeError("already exists"),
                CIRunBudgetError("CI_BUDGET_OPERATION_REPLAY"),
            ]
        ),
    )
    namespace = extracted_namespace(client)
    with pytest.raises(CIRunBudgetError, match="CI_BUDGET_OPERATION_REPLAY"):
        exec(compile(initial_section(), "<extracted-initial-create>", "exec"), namespace)
    assert [
        call.kwargs["ci_operation_id"] for call in client.create_deployment.call_args_list
    ] == ["fresh-intent", "fresh-intent"]
    namespace["_close_stale_for_retry"].assert_called_once()
    namespace["_report_suspected_orphans"].assert_not_called()


def replacement_namespace(client):
    namespace = extracted_namespace(client)
    _, deploy = parsed()
    replacement = next(
        node
        for node in ast.walk(deploy)
        if isinstance(node, ast.FunctionDef) and node.name == "_redeploy_and_reselect"
    )
    namespace.update(dseq="123", _RUN_ID="run")
    exec(
        compile(
            ast.Module(body=[replacement], type_ignores=[]), "<extracted-replacement>", "exec"
        ),
        namespace,
    )
    return namespace


def test_secondary_replacement_new_intent_follows_successful_close():
    order = []
    client = SimpleNamespace(
        _ci_run_budget_required=True,
        close_deployment=Mock(side_effect=lambda _: order.append("close")),
        create_deployment=Mock(
            side_effect=lambda *args, **kwargs: (
                order.append(kwargs["ci_operation_id"])
                or (_ for _ in ()).throw(CIRunBudgetError("CI_BUDGET_OPERATION_REPLAY"))
            )
        ),
    )
    namespace = replacement_namespace(client)
    with pytest.raises(CIRunBudgetError):
        namespace["_redeploy_and_reselect"]()
    assert order == ["close", "fresh-intent"]
    namespace["_report_suspected_orphans"].assert_not_called()


def test_secondary_replacement_failed_close_prevents_new_intent():
    client = SimpleNamespace(
        _ci_run_budget_required=True,
        close_deployment=Mock(side_effect=RuntimeError("close held")),
        create_deployment=Mock(),
    )
    namespace = replacement_namespace(client)
    with pytest.raises(RuntimeError, match="could not close stale order"):
        namespace["_redeploy_and_reselect"]()
    assert client.close_deployment.call_count == 3
    client.create_deployment.assert_not_called()


def test_secondary_local_helper_preserves_legacy_signature():
    class Local:
        def create_deployment(self, sdl, *, deposit):
            return sdl, deposit

    client = Local()
    namespace = extracted_namespace(client)
    assert namespace["_create_with_budget_intent"](client, "sdl", 1, "unused") == ("sdl", 1)


def test_all_three_create_calls_source_bound_and_privacy_not_mode():
    _, deploy = parsed()
    calls = sorted(
        (
            node
            for node in ast.walk(deploy)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_create_with_budget_intent"
        ),
        key=lambda node: node.lineno,
    )
    assert len(calls) == 3
    assert (
        ast.dump(calls[0].args[3])
        == ast.dump(calls[1].args[3])
        == "Name(id='create_operation_id', ctx=Load())"
    )
    assert ast.unparse(calls[2].args[3]) == "uuid.uuid4().hex"
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_deployment"
        for node in ast.walk(deploy)
    )
    assert ci_required(CIConsoleAPI("synthetic-key"))
    assert not ci_required(SimpleNamespace(_protect_runtime_payloads=True))
    assert not ci_required(Mock())  # Undeclared mock attributes are not budget-mode authority.
    assert not ci_required(AkashConsoleAPI("synthetic-key"))


@pytest.mark.parametrize("stale", [False, True])
def test_actual_import_deploy_terminal_and_stale_intent(monkeypatch, tmp_path, stale):
    dp = importlib.import_module("just_akash.deploy")
    wallet = importlib.import_module("just_akash.wallet_pool")
    error = CIRunBudgetError("CI_BUDGET_OPERATION_REPLAY")
    client = SimpleNamespace(
        _ci_run_budget_required=True,
        create_deployment=Mock(
            side_effect=[RuntimeError("already exists"), error] if stale else error
        ),
    )
    monkeypatch.setattr(
        wallet,
        "select_client_for_create",
        lambda *args, **kwargs: SimpleNamespace(client=client, configured_keys=1),
    )
    monkeypatch.setattr(dp, "_prepare_sdl_content", lambda *args, **kwargs: "services: {}")
    monkeypatch.setattr(dp, "_check_wallet_credit", Mock())
    monkeypatch.setattr(dp, "_close_stale_for_retry", Mock())
    monkeypatch.setattr(dp, "_report_suspected_orphans", Mock())
    monkeypatch.setattr(dp, "_resolve_tier", lambda *args: [])
    with pytest.raises(CIRunBudgetError) as caught:
        dp.deploy(str(tmp_path / "missing-synthetic.yml"))
    assert caught.value is error
    assert client.create_deployment.call_count == (2 if stale else 1)
    ids = [call.kwargs["ci_operation_id"] for call in client.create_deployment.call_args_list]
    assert len(set(ids)) == 1
    assert dp._close_stale_for_retry.call_count == int(stale)
    dp._report_suspected_orphans.assert_not_called()


@pytest.mark.parametrize("mode", ["local", "actions", "process"])
def test_actual_import_authorized_transport_contract(monkeypatch, mode):
    # Reuse the existing real public reserve/redeem fixture, not replacement SDK objects.
    spec = importlib.util.spec_from_file_location(
        "authorized_contract_fixture", ROOT / "tests/test_authorized_console.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    client = fixture.client.__wrapped__(monkeypatch)
    if mode != "local":
        monkeypatch.setenv(
            "GITHUB_ACTIONS" if mode == "actions" else "AKASH_CI_BUDGET_REQUIRED", "true"
        )
        opener = fixture.transport(monkeypatch)
        with pytest.raises(fixture.CreateHeld):
            client.submit(**fixture.authority())
        opener.assert_not_called()
    else:
        fixture.test_shared_account_authority_reaches_exact_post_once(client, monkeypatch)
