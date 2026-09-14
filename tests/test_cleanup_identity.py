"""Exercise final cleanup transport through complete class/run identity evidence."""

import inspect
import textwrap
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


def _identity(workload_class="ci-payload", group=1):
    if workload_class.startswith("ci-"):
        return Identity(PREFIX, REPO, workload_class, group, run=99, attempt=2)
    return Identity(PREFIX, REPO, workload_class, group, release="abc")


def _document(names):
    return {
        "deployment": {"id": {"owner": OWNER, "dseq": DSEQ}, "state": "active"},
        "groups": [
            {"id": {"owner": OWNER, "dseq": DSEQ, "gseq": n}, "group_spec": {"name": name}}
            for n, name in enumerate(names, 1)
        ],
    }


@pytest.fixture
def setup(monkeypatch):
    # This suite isolates authorization; closure has separate real HTTP controls.
    monkeypatch.setattr(cs._lease_verification, "verdict", lambda *a, **k: {"closed": True})
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
    monkeypatch.setattr(
        guard.chain,
        "owner_close_population_evidence",
        lambda owner, dseq, population: {
            "owner": owner,
            "dseq": dseq,
            "groups": population,
        },
    )
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
    client, doc, _ = setup
    client.get_deployment.return_value = {
        "leases": [{"status": {"services": {s: {} for s in services}}}]
    }
    workload_class = "ci-runner" if services == ["runner"] else "ci-payload"
    doc.update(_document([format_identity(_identity(workload_class), REGISTER)]))
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
        [PREFIX + "research-run-99-end"],
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
        other["groups"][0]["id"]["dseq"] = "12"
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
    doc["groups"][0]["id"]["gseq"] = 2
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


def test_recorded_chain_wire_identity_reaches_real_cleanup_gate(setup, monkeypatch):
    import json
    from pathlib import Path

    client, _, _ = setup
    fixture = Path(__file__).with_name("fixtures") / "chain_deployment_info_identity.json"
    recorded = json.loads(fixture.read_text())
    assert len(recorded["groups"]) == 1
    assert recorded["groups"][0]["id"]["gseq"] == 1
    monkeypatch.setattr(guard.chain, "_lcd_get", lambda *a, **k: recorded)
    # The observed legacy class must remain held; only the explicit versioned CI control qualifies.
    assert _run() == 2
    client.close_deployment.assert_not_called()
    recorded["groups"][0]["group_spec"]["name"] = format_identity(_identity(), REGISTER)
    assert _run() == 0
    client.close_deployment.assert_called_once_with(DSEQ)


@pytest.mark.parametrize(
    ("services", "name"),
    [
        (["probe"], PREFIX + "probe"),
        (["probe"], PREFIX + "probe.a1b2c3"),
        (["backtest"], PREFIX + "backtest-run-99-end"),
        (["runner"], PREFIX + "runner-run-99-end"),
    ],
)
def test_exact_legacy_single_group_is_authorized_only_for_its_intent(setup, services, name):
    client, doc, _ = setup
    client.get_deployment.return_value = {
        "leases": [{"status": {"services": {s: {} for s in services}}}]
    }
    doc.update(_document([name]))
    assert _run() == 0
    client.close_deployment.assert_called_once_with(DSEQ)


@pytest.mark.parametrize(
    ("services", "name"),
    [
        (["probe"], PREFIX + "backtest"),
        (["backtest"], PREFIX + "research"),
        (["runner"], PREFIX + "runner-product"),
        (["app"], PREFIX + "app"),
        (["probe"], PREFIX + "idv2-class-ci-payload-g1-op-1-attempt-2-run-99-end"),
        (["backtest"], PREFIX + "idv2-class-prod-payload-g1-op-1-release-r1"),
        (["runner"], PREFIX + "idv2-class-staging-payload-g1-op-1-release-r1"),
        (["probe"], PREFIX + "idv2-class-research-g1-op-1-release-r1"),
    ],
)
def test_arbitrary_legacy_and_every_idv2_population_remain_held(setup, services, name):
    client, doc, _ = setup
    client.get_deployment.return_value = {
        "leases": [{"status": {"services": {s: {} for s in services}}}]
    }
    doc.update(_document([name]))
    assert _run() == 2
    client.close_deployment.assert_not_called()


def test_mixed_legacy_population_cannot_pass_on_one_matching_prefix(setup):
    client, doc, _ = setup
    doc.update(_document([PREFIX + "runner-run-99-end", PREFIX + "research-run-99-end"]))
    client.get_deployment.return_value = {"leases": [{"status": {"services": {"runner": {}}}}]}
    assert _run() == 2
    client.close_deployment.assert_not_called()


@pytest.mark.parametrize(
    "dseq",
    [
        "0",
        "01",
        "+1",
        "１２",
        str(2**64),
        "9" * 40,
    ],
)
def test_noncanonical_or_overflow_dseq_never_reaches_identity_reads(monkeypatch, dseq):
    reads = []
    monkeypatch.setattr(guard.chain, "rest_urls", lambda: reads.append("read") or [])
    allowed, reason = guard.eligible(OWNER, dseq, PREFIX, REGISTER, guard.CleanupIntent.PROBE)
    assert not allowed and "uint64" in reason
    assert reads == []


def test_cleanup_intent_is_an_enum_not_an_open_string(monkeypatch):
    reads = []
    monkeypatch.setattr(guard.chain, "rest_urls", lambda: reads.append("read") or [])
    allowed, reason = guard.eligible(OWNER, DSEQ, PREFIX, REGISTER, "stale-probe")  # type: ignore[arg-type]
    assert not allowed and "typed cleanup intent" in reason
    assert reads == []


def test_exact_deployment_read_with_pagination_is_incomplete(setup):
    client, doc, _ = setup
    doc["pagination"] = {"next_key": "more"}
    assert _run() == 2
    client.close_deployment.assert_not_called()


def test_missing_signed_creation_population_is_held(setup, monkeypatch):
    client, _, _ = setup
    monkeypatch.setattr(guard.chain, "owner_close_population_evidence", lambda *args: None)
    assert _run() == 2
    client.close_deployment.assert_not_called()


def test_all_group_check_effect_mutation_reopens_the_mixed_population():
    """Replacing ALL with ANY recreates the reproduced mixed-group false close."""

    source = textwrap.dedent(inspect.getsource(cs._all_groups_owned))
    target = "return bool(group_names) and all("
    replacement = "return bool(group_names) and any("
    assert source.count(target) == 1, "all-group mutation target must apply exactly once"
    mutated = source.replace(target, replacement, 1)
    assert mutated != source
    namespace = {"__builtins__": __builtins__}
    exec(mutated, namespace)  # noqa: S102 -- executable effect mutation
    groups = [PREFIX + "runner-run-99-end", "foreign-prod-release"]
    assert not cs._all_groups_owned(groups, PREFIX)
    assert namespace["_all_groups_owned"](groups, PREFIX), (
        "mutation applied but did not reopen the mixed-group population"
    )
    real_verdict = cs.classify(
        {"leases": [{"status": {"services": {"runner": {}}}}]},
        DSEQ,
        NOW,
        reap_runners=True,
        group_names=groups,
        placement_prefix=PREFIX,
    )[0]
    mutant_globals = vars(cs).copy()
    mutant_globals["_all_groups_owned"] = namespace["_all_groups_owned"]
    exec(  # noqa: S102 -- executable effect mutation
        textwrap.dedent(inspect.getsource(cs.classify)), mutant_globals
    )
    mutant_verdict = mutant_globals["classify"](
        {"leases": [{"status": {"services": {"runner": {}}}}]},
        DSEQ,
        NOW,
        reap_runners=True,
        group_names=groups,
        placement_prefix=PREFIX,
    )[0]
    assert real_verdict == "LEAVE-not-ours"
    assert mutant_verdict == "STALE-runner", (
        "mutation applied but did not change the close candidate population"
    )


def _mutated_run(target: str, replacement: str):
    source = textwrap.dedent(inspect.getsource(cs.run))
    assert source.count(target) == 1, "call-site mutation target must apply exactly once"
    mutated = source.replace(target, replacement, 1)
    assert mutated != source
    namespace = vars(cs).copy()
    exec(mutated, namespace)  # noqa: S102 -- executable call-site mutation
    return namespace["run"]


def test_removing_the_selection_policy_call_changes_the_close_population(
    setup, monkeypatch, capsys
):
    """The first guard keeps an unauthorized service/age candidate out of the plan."""

    client, _, _ = setup
    monkeypatch.setattr(guard, "eligible", lambda *a, **k: (False, "held fixture"))
    kwargs = {
        "execute": False,
        "now": NOW,
        "placement_prefix": PREFIX,
        "ownership_register": REGISTER,
        "reap_runners": True,
        "reap_owned": True,
    }
    assert cs.run(**kwargs) == 2
    assert "stale (closable): 0" in capsys.readouterr().out
    target = """            allowed, reason = cleanup_identity.eligible(
                address, str(dseq), placement_prefix, ownership_register, intent
            )
            if not allowed:
                identity_held += 1
                print(f\"  {dseq} HELD: {reason}\")
                continue
"""
    replacement = """            allowed, reason = True, \"mutated selection bypass\"
"""
    mutant = _mutated_run(target, replacement)
    assert mutant(**kwargs) == 0
    out = capsys.readouterr().out
    assert "stale (closable): 1" in out, (
        "mutation applied but did not change the selected close population"
    )
    client.close_deployment.assert_not_called()


def test_removing_the_send_boundary_policy_call_reaches_close_transport(setup, monkeypatch):
    """The second guard rechecks changed evidence immediately before DELETE."""

    client, _, _ = setup
    calls = 0

    def eligibility(*args, **kwargs):
        nonlocal calls
        calls += 1
        return (calls == 1, "fresh" if calls == 1 else "changed before send")

    monkeypatch.setattr(guard, "eligible", eligibility)
    assert _run() == 2
    assert calls == 2
    client.close_deployment.assert_not_called()
    calls = 0
    target = """        allowed, reason = cleanup_identity.eligible(
            address, str(dseq), placement_prefix, ownership_register, intent
        )
        if not allowed:
            identity_held += 1
            print(f\"  {dseq} HELD before close: {reason}\")
            continue
"""
    replacement = """        allowed, reason = True, \"mutated send-boundary bypass\"
"""
    mutant = _mutated_run(target, replacement)
    assert (
        mutant(
            execute=True,
            now=NOW,
            placement_prefix=PREFIX,
            ownership_register=REGISTER,
            reap_runners=True,
            reap_owned=True,
        )
        == 0
    )
    assert calls == 1, "mutation must remove exactly the immediate pre-close recheck"
    client.close_deployment.assert_called_once_with(DSEQ)
