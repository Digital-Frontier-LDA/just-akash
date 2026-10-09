"""Known poll identities reach the existing scoped exact-ID teardown path."""

from __future__ import annotations

import json
import os
import subprocess
from copy import deepcopy

import pytest

from tests import test_pool_handoff_runtime as handoff
from tests import test_runner_pool_outcome_is_monotonic as provision
from tests import test_runner_teardown_exact_ids as teardown


def runner(identity=701, *, label="pool-label", status="online", version="2.330.0"):
    return {
        "id": identity,
        "name": f"just-akash-pool-{identity}",
        "status": status,
        "busy": True,
        "version": version,
        "labels": [{"name": x} for x in ("self-hosted", "akash", label)],
    }


def collector():
    script = provision.provision_script()
    start = script.index("# Preserve known identities across polls/retry leases")
    end = script.index("# COUNT LINES, NEVER", start)
    return script[start:end]


def collect(tmp_path, polls, *, prior=None, body=None):
    output = tmp_path / "output"
    script = "KNOWN_RUNNER_IDS_JSON=$PRIOR\n"
    env = {
        **os.environ,
        "PRIOR": json.dumps(prior or []),
        "RUNNER_LABEL": "pool-label",
        "GITHUB_OUTPUT": str(output),
    }
    for index, poll in enumerate(polls):
        env[f"POLL_{index}"] = poll
        script += f'RUNNER_PAGES="$POLL_{index}"\n' + (body or collector()) + "\n"
    result = subprocess.run(
        [provision.modern_bash(), "-euo", "pipefail", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    writes = output.read_text().splitlines() if output.exists() else []
    return [json.loads(line.removeprefix("runner_ids=")) for line in writes], result.stdout


def test_all_known_statuses_and_pages_are_retained_without_foreign_ids(tmp_path):
    pages = "\n".join(
        json.dumps({"runners": rows})
        for rows in [
            [runner(701), runner(702, status="offline"), runner(900, label="foreign")],
            [runner(703), {**runner(901), "name": "foreign-runner"}],
        ]
    )
    writes, _ = collect(tmp_path, [pages])
    assert writes == [[701, 702, 703]]


@pytest.mark.parametrize(
    "change",
    [
        {"id": True},
        {"id": 0},
        {"id": -1},
        {"id": 1.5},
        {"id": 9007199254740992},
        {"id": "702"},
        {"name": "unrelated"},
        {"name": None},
        {"labels": None},
        {"labels": [{"name": "akash"}, {"name": "pool-label"}]},
        {"labels": [{"name": "self-hosted"}, {"name": "pool-label"}]},
        {"labels": [{"name": "self-hosted"}, {"name": "akash"}]},
    ],
)
def test_unqualified_identity_cannot_enter_the_exact_id_output(tmp_path, change):
    rows = [runner(701), {**runner(702), **change}]
    writes, _ = collect(tmp_path, [json.dumps({"runners": rows})])
    assert writes == [[701]]


def test_identity_history_survives_empty_poll_and_deduplicates_later_poll(tmp_path):
    polls = [
        json.dumps({"runners": [runner(701)]}),
        json.dumps({"runners": []}),
        json.dumps({"runners": [runner(702), runner(701)]}),
    ]
    writes, _ = collect(tmp_path, polls)
    assert writes == [[701], [701], [701, 702]]


@pytest.mark.parametrize("bad_poll", ["not-json", '{"message":"unavailable"}', "null"])
def test_malformed_poll_preserves_previous_output_without_error_body(tmp_path, bad_poll):
    writes, log = collect(tmp_path, [json.dumps({"runners": [runner(701)]}), bad_poll])
    assert writes == [[701]]
    assert "earlier known IDs are retained" in log
    assert bad_poll not in log


def test_cumulative_identity_bound_preserves_previous_population(tmp_path):
    prior = list(range(1, 10001))
    writes, log = collect(tmp_path, [json.dumps({"runners": [runner(10001)]})], prior=prior)
    assert writes == []
    assert "earlier known IDs are retained" in log


def install_registration_rows(monkeypatch, rounds):
    original = 'print(json.dumps({"runners": runners}))'
    assert provision.GH_STUB.count(original) == 1
    replacement = (
        f"rows = {rounds!r}\n"
        "round_index = int(open(os.path.join(os.environ['STUB_STATE'], 'round')).read()) - 1\n"
        "print(json.dumps({'runners': rows[min(round_index, len(rows)-1)]}))"
    )
    monkeypatch.setattr(provision, "GH_STUB", provision.GH_STUB.replace(original, replacement))


def test_whole_healthy_provision_publishes_ids_without_changing_ready_outputs(
    tmp_path, monkeypatch
):
    install_registration_rows(monkeypatch, [[runner(701)]])
    result = provision.run_step(
        tmp_path,
        {"rounds": [{"dseq": "123", "provider": provision.PROVIDER_A}], "owner": provision.OWNER},
    )
    assert result["rc"] == 0
    assert json.loads(provision.last(result, "runner_ids")) == [701]
    assert provision.last(result, "provision_healthy") == "true"
    assert provision.last(result, "runners_online") == "1/1"
    assert json.loads(provision.last(result, "runner_targets"))[-1] == "pool-label"
    provision.assert_monotonic(result)


def test_retry_retains_both_known_registration_identities(tmp_path, monkeypatch):
    install_registration_rows(monkeypatch, [[runner(701, version=None)], [runner(702)]])
    result = provision.run_step(
        tmp_path,
        {
            "rounds": [
                {"dseq": "123", "provider": provision.PROVIDER_A},
                {"dseq": "124", "provider": provision.PROVIDER_B},
            ],
            "owner": provision.OWNER,
            "destroy": {"123": "ok"},
            "verify": {"123": "closed"},
        },
    )
    assert result["rc"] == 0
    assert json.loads(provision.last(result, "runner_ids")) == [701, 702]
    assert provision.last(result, "dseq") == "124"
    assert "destroy dseq=123" in result["calls"]
    provision.assert_monotonic(result)


def test_failed_close_keeps_known_ids_and_original_failure(tmp_path, monkeypatch):
    install_registration_rows(monkeypatch, [[runner(701, status="offline")]])
    result = provision.run_step(
        tmp_path,
        {"rounds": [{"dseq": "123", "provider": provision.PROVIDER_A}], "owner": provision.OWNER},
    )
    assert result["rc"] == 1
    assert json.loads(provision.last(result, "runner_ids")) == [701]
    assert provision.last(result, "failure_reason") == "LEASE_CLOSE_UNVERIFIED"
    assert provision.last(result, "dseq") == "123"
    provision.assert_monotonic(result)


@pytest.mark.parametrize("missing", [None, "job-output", "public-output", "caller-input"])
def test_actual_output_chain_selects_exact_teardown_instead_of_label_fallback(tmp_path, missing):
    pool, caller = handoff.documents()
    pool, caller = deepcopy(pool), deepcopy(caller)
    if missing == "job-output":
        del pool["jobs"]["pool"]["outputs"]["runner-ids"]
    elif missing == "public-output":
        del pool[True]["workflow_call"]["outputs"]["runner-ids"]
    elif missing == "caller-input":
        del caller["jobs"]["teardown"]["with"]["runner-ids"]
    job = {
        key: handoff.resolve(value, {"steps": {"provision": {"runner_ids": "[701]"}}})
        for key, value in pool["jobs"]["pool"]["outputs"].items()
    }
    public = {
        key: handoff.resolve(value["value"], {"jobs": {"pool": job}})
        for key, value in pool[True]["workflow_call"]["outputs"].items()
    }
    forwarded = (
        handoff.resolve(
            caller["jobs"]["teardown"]["with"].get("runner-ids"), {"needs": {"pool": public}}
        )
        or "[]"
    )
    assert forwarded == ("[701]" if missing is None else "[]")
    result = teardown._run(
        tmp_path,
        runner_ids=forwarded,
        listing={"runners": [runner(701, label="pool-operation")]},
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text()
    assert ("-X DELETE orgs/example/actions/runners/701" in calls) is (missing is None)
    assert ("actions/runners?per_page=100" in calls) is (missing is not None)


def test_failed_pool_rollback_uses_the_same_job_ids_and_scope_inputs():
    pool, _ = handoff.documents()
    inputs = pool["jobs"]["teardown"]["with"]
    forwarded = handoff.resolve(inputs["runner-ids"], {"needs": {"pool": {"runner-ids": "[701]"}}})
    assert forwarded == "[701]"
    assert inputs["github-org"] == "${{ inputs.github-org }}"
    assert (
        inputs["github-repository"]
        == "${{ inputs.runner-native-repository-scope && 'Borduas-Holdings/blazing' || '' }}"
    )
