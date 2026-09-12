"""Execute publication fragments from the actual pool workflow with offline inputs.

The harness exercises real shell output writes and actual YAML output/caller wiring.
It models GitHub scheduling and stubs the canonical close effect. It does not establish
GitHub's ability to retain outputs or schedule cleanup after abrupt cancellation.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
POOL = ROOT / ".github/workflows/runner-pool.yml"
CONSUMER = Path(__file__).with_name("fixtures") / "runner_handoff_consumer.yml"
CLOSER = "Digital-Frontier-LDA/just-akash/.github/workflows/runner-teardown.yml"


def documents():
    return yaml.safe_load(POOL.read_text()), yaml.safe_load(CONSUMER.read_text())


def provision_step(document):
    steps = [s for s in document["jobs"]["pool"]["steps"] if s.get("id") == "provision"]
    assert len(steps) == 1
    return steps[0]


def publish_from_real_shell(document, tmp_path, outcome):
    lines = provision_step(document)["run"].splitlines()
    # Execute the entire actual parse/publication fragment, including a removed emit.
    starts = [i for i, line in enumerate(lines) if line.strip().startswith("DSEQ=$(awk")]
    ends = [i for i, line in enumerate(lines) if line.strip().startswith("PROVIDER=$(awk")]
    assert len(starts) == len(ends) == 1 and starts[0] < ends[0]
    early = "\n".join(lines[starts[0] : ends[0]])
    assert early.count("/tmp/ja.log") == 1
    log = tmp_path / "deploy.log"
    log.write_text("DSEQ: 1789000000001\nProvider: offline-provider\n")
    early = early.replace("/tmp/ja.log", '"$DEPLOY_LOG"')
    starts = [i for i, line in enumerate(lines) if line.strip() == 'echo "dseq=$DSEQ"']
    assert len(starts) == 1 and lines[starts[0] - 1].strip() == "{"
    end = next(
        i for i in range(starts[0], len(lines)) if lines[i].strip() == '} >> "$GITHUB_OUTPUT"'
    )
    success = "\n".join(lines[starts[0] - 1 : end + 1])
    output = tmp_path / "outputs"
    output.write_text("")
    # Controlled fault at the post-create boundary: no production commands are run.
    ending = {"success": success, "failure": "exit 1", "cancelled": 'kill -TERM "$$"'}[outcome]
    result = subprocess.run(
        ["bash", "-e", "-c", early + "\n" + ending],
        capture_output=True,
        text=True,
        timeout=5,
        env={
            **os.environ,
            "GITHUB_OUTPUT": str(output),
            "DEPLOY_LOG": str(log),
            "PROVIDER": "offline-provider",
            "WALLET": "offline-owner",
            "WALLET_UACT": "",
            "ONLINE": "2",
            "POOL_SIZE": "2",
            "RUNNER_LABEL": "offline-pool",
        },
    )
    assert result.returncode == {"success": 0, "failure": 1, "cancelled": -15}[outcome], (
        result.stderr
    )
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


def resolve(expression, contexts):
    text = str(expression or "")
    if " || " in text and text.strip().startswith("${{"):
        inner = text.strip()[3:-2].strip()
        source, fallback = inner.split(" || ", 1)
        value = resolve("${{ " + source + " }}", contexts)
        if value:
            return value
        try:
            literal = ast.literal_eval(fallback)
        except (ValueError, SyntaxError):
            return ""
        return literal if isinstance(literal, str) else ""
    match = re.fullmatch(
        r"\s*\$\{\{\s*(steps|jobs|needs)\.([\w-]+)\.outputs\.([\w-]+)\s*\}\}\s*",
        str(expression or ""),
    )
    if not match:
        return ""
    return contexts.get(match[1], {}).get(match[2], {}).get(match[3], "")


def should_run(expression, results):
    text = str(expression).removeprefix("${{").removesuffix("}}").strip()
    text = re.sub(r"needs\.([\w-]+)\.result", lambda m: repr(results[m[1]]), text)
    text = text.replace("always()", "True").replace("&&", " and ").replace("||", " or ")
    tree = ast.parse(text, mode="eval")
    allowed = (
        ast.Expression,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.Constant,
    )
    assert all(isinstance(node, allowed) for node in ast.walk(tree))
    return eval(compile(tree, "<workflow predicate>", "eval"), {"__builtins__": {}})


def lifetime(pool, caller, output, outcome, consumer_result="success"):
    render_output = {
        "runner_targets": ('{"unit-1":["self-hosted","linux","akash","offline-pool-unit-1"]}'),
        "slot_labels": '{"unit-1":"offline-pool-unit-1"}',
        "deployment_group": (
            "just-akash-e2epool-idv2-class-ci-runner-g1-op-7-attempt-1-run-99-end"
        ),
        "operation_label": "offline-pool-idv2-g1-op-7-attempt-1-run-99",
    }
    job_output = {
        k: resolve(v, {"steps": {"provision": output, "render": render_output}})
        for k, v in pool["jobs"]["pool"]["outputs"].items()
    }
    exported = {
        k: resolve(v["value"], {"jobs": {"pool": job_output}})
        for k, v in pool[True]["workflow_call"]["outputs"].items()
    }
    alive = True
    events = ["lease-created"]

    def close(job, context, label):
        nonlocal alive
        target, _, pin = job.get("uses", "").partition("@")
        dseq = resolve(job.get("with", {}).get("dseq"), context)
        if target == CLOSER and re.fullmatch("[0-9a-f]{40}", pin) and dseq == "1789000000001":
            alive = False
            events.append(label + "-close")
        else:
            events.append(label + "-no-close")

    rollback = pool["jobs"]["teardown"]
    assert "pool" in rollback["needs"]
    if should_run(rollback["if"], {"pool": outcome}):
        close(rollback, {"needs": {"pool": job_output}}, "rollback")
    events.append("callee-finished")
    cleanup = caller["jobs"]["teardown"]
    caller_context = {"needs": {"pool": exported if outcome == "success" else {}}}
    early_cleanup = set(cleanup["needs"]) <= {"pool"}
    if early_cleanup and should_run(cleanup["if"], {"pool": outcome}):
        close(cleanup, caller_context, "caller")
    if outcome == "success":
        assert alive, events
        assert exported.get("runner-targets") == render_output["runner_targets"], (
            "missing-runner-targets"
        )
        assert output["provision_healthy"] == "true"
        assert "offline-pool" in render_output["runner_targets"]
        assert caller["jobs"]["work"]["needs"] == ["pool"]
        assert "needs.pool.outputs.runner-targets" in caller["jobs"]["work"]["runs-on"]
        events.extend(["consumer-started", "consumer-" + consumer_result])
    if not early_cleanup and should_run(cleanup["if"], {"pool": outcome, "work": consumer_result}):
        assert set(cleanup["needs"]) <= {"pool", "work"}
        close(cleanup, caller_context, "caller")
    assert not alive, events
    return events


@pytest.mark.parametrize("consumer_result", ["success", "failure", "cancelled"])
def test_actual_success_outputs_survive_internal_workflow_until_caller_cleanup(
    tmp_path, consumer_result
):
    pool, caller = documents()
    output = publish_from_real_shell(pool, tmp_path, "success")
    events = lifetime(pool, caller, output, "success", consumer_result)
    assert "rollback-close" not in events
    assert events.index("consumer-" + consumer_result) < events.index("caller-close")


@pytest.mark.parametrize("outcome", ["failure", "cancelled"])
def test_actual_early_output_survives_shell_failure_and_reaches_internal_rollback(
    tmp_path, outcome
):
    pool, caller = documents()
    output = publish_from_real_shell(pool, tmp_path, outcome)
    assert output == {"dseq": "1789000000001"}
    events = lifetime(pool, caller, output, outcome)
    assert events.index("rollback-close") < events.index("callee-finished")


@pytest.mark.parametrize(
    "mutation,outcome,expected",
    [
        ("unconditional", "success", "rollback-close"),
        ("success-gated", "failure", "caller-no-close"),
        ("missing-output", "failure", "rollback-no-close"),
        ("missing-runner-targets", "success", "missing-runner-targets"),
        ("noop-target", "failure", "rollback-no-close"),
        ("missing-early-emit", "failure", "rollback-no-close"),
        ("missing-consumer", "success", "caller-close"),
    ],
)
def test_exact_target_mutations_break_observed_handoff(tmp_path, mutation, outcome, expected):
    pool, caller = documents()
    before = deepcopy(pool)
    caller_before = deepcopy(caller)
    if mutation == "unconditional":
        pool["jobs"]["teardown"]["if"] = "always()"
        before["jobs"]["teardown"]["if"] = "always()"
    elif mutation == "success-gated":
        old = pool["jobs"]["teardown"]["if"]
        assert old.count("!= 'success'") == 1
        pool["jobs"]["teardown"]["if"] = old.replace("!= 'success'", "== 'success'")
        before["jobs"]["teardown"]["if"] = pool["jobs"]["teardown"]["if"]
    elif mutation == "missing-output":
        del pool["jobs"]["pool"]["outputs"]["dseq"]
        del before["jobs"]["pool"]["outputs"]["dseq"]
    elif mutation == "missing-runner-targets":
        del pool["jobs"]["pool"]["outputs"]["runner-targets"]
        del before["jobs"]["pool"]["outputs"]["runner-targets"]
    elif mutation == "noop-target":
        old = pool["jobs"]["teardown"]["uses"]
        assert old.count("/runner-teardown.yml@") == 1
        pool["jobs"]["teardown"]["uses"] = old.replace("/runner-teardown.yml@", "/noop.yml@")
        before["jobs"]["teardown"]["uses"] = pool["jobs"]["teardown"]["uses"]
    elif mutation == "missing-early-emit":
        step = provision_step(pool)
        emit = 'echo "dseq=$DSEQ" >> "$GITHUB_OUTPUT"\n  PROVIDER=$(awk'
        assert step["run"].count(emit) == 1
        step["run"] = step["run"].replace(emit, ":\n  PROVIDER=$(awk")
        provision_step(before)["run"] = step["run"]
    else:
        assert caller["jobs"]["teardown"]["needs"].count("work") == 1
        caller["jobs"]["teardown"]["needs"].remove("work")
        caller_before["jobs"]["teardown"]["needs"].remove("work")
    assert pool == before and caller == caller_before  # Exactly the named target changed.
    output = publish_from_real_shell(pool, tmp_path, outcome)
    with pytest.raises(AssertionError, match=expected):
        lifetime(pool, caller, output, outcome)
