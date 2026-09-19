"""A nested reusable call that omits a required input kills the CONSUMER's whole run.

`runner-pool.yml` has a `teardown` job that calls `runner-teardown.yml`. That
callee declares `just-akash-ref` required, with no default, deliberately — a
default would mean "whatever main happens to be at", which is an unpinned
supply-chain window that looks pinned from the caller's side.

The nested call did not pass it. GitHub cannot turn a call with a missing
required input into a job, so it abandons the entire graph, and the failure
lands on whoever CALLED runner-pool.yml: a run with ZERO jobs, no logs, no
annotation, and an error visible only in the web UI.

MEASURED, 2026-09-03, from Borduas-Holdings/blazing#798:

  * Both blazing workflows that call runner-pool.yml returned startup_failure
    with 0 jobs, at pin c2cad20a and again at 6d77f6cd.
  * Reverting ONE of them (e2e-tests.yml) to the previous pin 017b9e0b made it
    build 14 jobs, while the other, left at the new pin, still failed. 017b9e0b
    has a single `pool` job and no nested teardown at all, which is why it was
    never hit before.
  * Ruled out first, each by command: the reference resolved (5/5 in
    `referenced_workflows`), every input passed BY blazing was declared, all
    three secrets matched, no job was over a size ceiling, and runner-pool.yml
    at the new pin parses cleanly on its own — dropped into another repo as a
    standalone workflow_call file, GitHub emitted no phantom failure run for it.

⚠ THIS REPO'S CI CANNOT CATCH IT BY RUNNING. Nothing here calls runner-pool.yml
as a reusable workflow, so the broken graph is only ever built in a consumer —
the same blind spot as the `./` defect in #248, one input along. A static check
is therefore the only thing that can see it, which is what this file is.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

WORKFLOWS = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"
OWN_REPO = "Digital-Frontier-LDA/just-akash/.github/workflows/"


def _required_inputs(workflow: pathlib.Path) -> set[str]:
    document = yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}
    triggers = document.get(True) or document.get("on") or {}
    inputs = ((triggers or {}).get("workflow_call") or {}).get("inputs") or {}
    return {
        name for name, spec in inputs.items() if isinstance(spec, dict) and spec.get("required")
    }


def _internal_calls() -> list[tuple[str, str, pathlib.Path, set[str]]]:
    """(caller, job, callee path, inputs passed) for calls into this repo's workflows."""
    calls = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job, body in (document.get("jobs") or {}).items():
            if not isinstance(body, dict):
                continue
            uses = str(body.get("uses") or "")
            if not uses:
                continue
            if uses.startswith(OWN_REPO):
                callee = uses[len(OWN_REPO) :].split("@")[0]
            elif uses.startswith("./.github/workflows/"):
                callee = uses[len("./.github/workflows/") :].split("@")[0]
            else:
                continue
            target = WORKFLOWS / callee
            if not target.is_file():
                continue
            calls.append((path.name, job, target, set((body.get("with") or {}).keys())))
    return calls


def test_there_are_internal_calls_to_check() -> None:
    """This guard is worthless if the discovery silently finds nothing."""
    assert _internal_calls(), "no in-repo reusable calls found — check the discovery"


@pytest.mark.parametrize(
    "caller,job,callee,passed", _internal_calls(), ids=lambda v: v if isinstance(v, str) else None
)
def test_every_required_input_is_passed(
    caller: str, job: str, callee: pathlib.Path, passed: set[str]
) -> None:
    missing = sorted(_required_inputs(callee) - passed)
    assert not missing, (
        f"{caller}:{job} calls {callee.name} without required input(s) {missing}. "
        f"GitHub cannot build a job from that call, so it abandons the WHOLE graph and "
        f"the CONSUMER's run returns startup_failure with zero jobs, no logs and no "
        f"annotation. This repo's own CI cannot see it — nothing here calls "
        f"runner-pool.yml as a reusable."
    )


def test_the_check_fires_on_the_omission_that_broke_blazing() -> None:
    """Known-positive: the exact call as it stood before this fix."""
    passed_before = {"dseq", "tag-prefix", "runner-label", "github-org"}
    required = _required_inputs(WORKFLOWS / "runner-teardown.yml")
    assert "just-akash-ref" in required, "the callee no longer requires it — revisit this test"
    assert sorted(required - passed_before) == ["just-akash-ref"]


def test_the_nested_teardown_call_is_the_one_we_think_it_is() -> None:
    """Guard the guard: if the teardown job is renamed or dropped, fail loudly."""
    callers = [
        (c, j) for c, j, callee, _ in _internal_calls() if callee.name == "runner-teardown.yml"
    ]
    assert ("runner-pool.yml", "teardown") in callers, (
        f"nested teardown call not found; saw {callers}"
    )
