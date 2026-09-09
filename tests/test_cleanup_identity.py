"""Exercise final cleanup transport through complete class/run identity evidence."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from just_akash import cleanup_identity as guard
from just_akash import cleanup_stale as cs
from just_akash.workload_identity import Identity, format_identity

OWNER = "akash1" + "a" * 38
DSEQ = "1780000000000"
PREFIX = "just-akash-"
REPO = "Digital-Frontier-LDA/just-akash"
REGISTER = {PREFIX: REPO}
NOW = 1781000000
REAL_LIST_ACTIVE_DEPLOYMENTS = cs.chain.list_active_deployments


def _identity(workload_class="ci-runner", group=1):
    if workload_class.startswith("ci-"):
        return Identity(PREFIX, REPO, workload_class, group, run=99, attempt=2)
    return Identity(PREFIX, REPO, workload_class, group, release="abc")


def _document(names):
    return {
        "deployment": {"id": {"owner": OWNER, "dseq": DSEQ}, "state": "active"},
        "groups": [
            {"group_id": {"owner": OWNER, "dseq": DSEQ, "gseq": n}, "group_spec": {"name": name}}
            for n, name in enumerate(names, 1)
        ],
    }


@pytest.fixture
def setup(monkeypatch):
    client = MagicMock()
    client.account_address.return_value = OWNER
    client.get_deployment.return_value = {"leases": [{"status": {"services": {"probe": {}}}}]}
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.setattr(cs, "AkashConsoleAPI", lambda key: client)
    monkeypatch.setattr(cs, "_credit_line", lambda *a: "offline")
    monkeypatch.setattr(cs.time, "sleep", lambda *a: None)
    monkeypatch.setattr(cs.chain, "list_active_deployments", lambda owner: [{"dseq": DSEQ}])
    monkeypatch.setattr(cs.chain, "deployment_group_names", lambda *a: [PREFIX + "runner"])
    monkeypatch.setattr(guard.chain, "rest_urls", lambda: ["https://one.test", "https://two.test"])
    state = {"id": 99, "run_attempt": 2, "status": "completed", "repository": {"full_name": REPO}}
    monkeypatch.setattr(guard, "completed_run", lambda *a: state)
    doc = _document([format_identity(_identity(), REGISTER)])
    monkeypatch.setattr(guard.chain, "_lcd_get", lambda *a, **k: doc)
    return client, doc, state


def _run(**kwargs):
    return cs.run(
        execute=True,
        now=NOW,
        placement_prefix=PREFIX,
        ownership_register=REGISTER,
        reap_runners=True,
        reap_owned=True,
        **kwargs,
    )


@pytest.mark.parametrize("services", [["probe"], ["backtest"], ["runner"], ["app"], []])
def test_every_stale_service_class_requires_complete_identity(setup, services):
    client, _, _ = setup
    client.get_deployment.return_value = {
        "leases": [{"status": {"services": {s: {} for s in services}}}]
    }
    assert _run() == 0
    client.close_deployment.assert_called_once_with(DSEQ)


@pytest.mark.parametrize(
    "names",
    [
        [format_identity(_identity("prod-payload"), REGISTER)],
        [format_identity(_identity("staging-payload"), REGISTER)],
        [
            format_identity(_identity(), REGISTER),
            format_identity(_identity("prod-payload", 2), REGISTER),
        ],
        [format_identity(_identity(), REGISTER), None],
        [],
        [PREFIX + "runner-run-99-end"],
    ],
)
@pytest.mark.parametrize("services", [["probe"], ["backtest"], ["runner"], ["app"], []])
def test_protected_or_unclassified_groups_never_reach_transport(setup, names, capsys, services):
    client, doc, _ = setup
    client.get_deployment.return_value = {
        "leases": [{"status": {"services": {s: {} for s in services}}}]
    }
    doc.update(_document(names))
    assert _run() == 2
    client.close_deployment.assert_not_called()
    assert "HELD" in capsys.readouterr().out


@pytest.mark.parametrize(
    "changes",
    [
        {"run_attempt": 3},
        {"status": "in_progress"},
        {"id": 12},
        {"repository": {"full_name": "wrong/repo"}},
        {"run_attempt": None},
    ],
)
def test_stale_attempt_or_live_or_mismatched_run_is_held(setup, changes):
    client, _, state = setup
    state.update(changes)
    assert _run() == 2
    client.close_deployment.assert_not_called()


def test_missing_register_holds_old_probe(setup):
    client, _, _ = setup
    assert cs.run(execute=True, now=NOW) == 2
    client.close_deployment.assert_not_called()


@pytest.mark.parametrize(
    "case",
    [
        "wrong-owner",
        "wrong-dseq",
        "missing-group",
        "duplicate-group",
        "different-name",
        "one-host",
    ],
)
def test_chain_identity_must_be_complete_and_agree(setup, monkeypatch, case):
    client, doc, _ = setup
    other = deepcopy(doc)
    if case == "wrong-owner":
        other["deployment"]["id"]["owner"] = "other"
    elif case == "wrong-dseq":
        other["groups"][0]["group_id"]["dseq"] = "12"
    elif case == "missing-group":
        other["groups"] = []
    elif case == "duplicate-group":
        other["groups"].append(deepcopy(other["groups"][0]))
    elif case == "different-name":
        other["groups"][0]["group_spec"]["name"] = format_identity(
            replace(_identity(), attempt=3), REGISTER
        )
    else:
        monkeypatch.setattr(
            guard.chain, "rest_urls", lambda: ["https://one.test/a", "https://one.test/b"]
        )
    monkeypatch.setattr(
        guard.chain, "_lcd_get", lambda *a, base: doc if "one.test" in base else other
    )
    assert _run() == 2
    client.close_deployment.assert_not_called()


def test_run_is_rechecked_immediately_before_transport(setup, monkeypatch):
    client, _, state = setup
    calls = []

    def lookup(*args):
        calls.append(args)
        return state if len(calls) == 1 else {**state, "run_attempt": 3}

    monkeypatch.setattr(guard, "completed_run", lookup)
    assert _run() == 2
    assert len(calls) == 2
    client.close_deployment.assert_not_called()


def test_missing_chain_and_missing_github_response_are_held(setup, monkeypatch):
    client, _, _ = setup
    monkeypatch.setattr(guard, "completed_run", lambda *a: None)
    assert _run() == 2
    client.close_deployment.assert_not_called()
    monkeypatch.setattr(guard, "agreeing_group_names", lambda *a: None)
    assert _run() == 2
    client.close_deployment.assert_not_called()


def test_authenticated_lookup_uses_exact_owner_run_and_rejects_errors(monkeypatch):
    calls = []
    monkeypatch.setattr(guard.shutil, "which", lambda name: "/usr/bin/gh")

    def query(argv, **kwargs):
        calls.append(argv)
        return MagicMock(returncode=0, stdout='{"id":99,"status":"completed"}')

    monkeypatch.setattr(guard.subprocess, "run", query)
    assert guard.completed_run(REPO, 99) == {"id": 99, "status": "completed"}
    assert calls == [["/usr/bin/gh", "api", f"repos/{REPO}/actions/runs/99"]]
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **k: MagicMock(returncode=1))
    assert guard.completed_run(REPO, 99) is None


def test_cli_forwards_explicit_register(monkeypatch):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return 2

    monkeypatch.setattr(cs, "run_all_wallets", run)
    assert (
        cs.main(["--ownership-register", '{"just-akash-":"Digital-Frontier-LDA/just-akash"}']) == 2
    )
    assert len(calls) == 1
    assert calls[0]["ownership_register"] == REGISTER


def test_embedded_group_id_must_match_actual_chain_group(setup):
    client, doc, _ = setup
    doc["groups"][0]["group_spec"]["name"] = format_identity(_identity(group=2), REGISTER)
    assert _run() == 2
    client.close_deployment.assert_not_called()
    doc["groups"][0]["group_id"]["gseq"] = 2
    assert _run() == 0
    client.close_deployment.assert_called_once_with(DSEQ)


@pytest.mark.parametrize("include_valid", [False, True])
def test_malformed_enumeration_never_becomes_clean_or_partial(setup, monkeypatch, include_valid):
    client, doc, _ = setup
    rows = [None] + ([{"deployment": doc["deployment"]}] if include_valid else [])
    monkeypatch.setattr(cs.chain, "list_active_deployments", REAL_LIST_ACTIVE_DEPLOYMENTS)
    monkeypatch.setattr(
        cs.chain,
        "_lcd_get",
        lambda path, **kwargs: {"deployments": rows} if "/deployments/list?" in path else doc,
    )
    assert _run() == 2
    client.close_deployment.assert_not_called()
    rows.clear()
    assert _run() == 0  # actual empty list is measurable and closes nothing
    client.close_deployment.assert_not_called()
    rows.append({"deployment": doc["deployment"]})
    assert _run() == 0
    client.close_deployment.assert_called_once_with(DSEQ)


@pytest.mark.parametrize(
    "endpoints",
    [
        ["https://one.test", "https://one.test."],
        ["https://ONE.TEST", "https://one.test"],
        ["https://one.test:443", "https://one.test:8443"],
        ["https://ONE.TEST.:443/a", "https://one.test:8443/b"],
    ],
)
def test_endpoint_aliases_cannot_supply_two_independent_observations(
    setup, monkeypatch, endpoints
):
    client, doc, _ = setup
    reads = []

    def read(path, *, base):
        reads.append(base)
        return doc

    monkeypatch.setattr(guard.chain, "rest_urls", lambda: endpoints)
    monkeypatch.setattr(guard.chain, "_lcd_get", read)
    assert _run() == 2
    assert len(reads) == 1
    client.close_deployment.assert_not_called()
    reads.clear()
    monkeypatch.setattr(guard.chain, "rest_urls", lambda: [endpoints[0], "https://two.test"])
    assert _run() == 0
    assert len(reads) == 4  # plan and immediate pre-close evidence each read both hosts
    client.close_deployment.assert_called_once_with(DSEQ)
