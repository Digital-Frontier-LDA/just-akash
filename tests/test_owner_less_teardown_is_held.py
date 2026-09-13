"""An owner-less lease receipt is HELD by runner-teardown, never resolved by probing (#348).

The pool can publish a DSEQ and deployment group with an empty wallet-address
(LEASE_OWNER_UNREADABLE). The destructive identity is the pair (owner, dseq): a DSEQ
alone can collide across owners, and trying configured accounts until one reads it can
select a foreign lease. Recovering the owner requires an exhaustive owner registry read
through each independent chain observer, which just-akash does not have. So teardown must
not resolve, destroy or verify; it must fail the job and publish outputs that locate the
still-live lease.

These tests run the REAL close step with just-akash replaced by a recorder.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/runner-teardown.yml"
OWNER = "akash1" + "a" * 38
GROUP = "borduas-runner-run-7-end"
JA_LINE = "JA=(uv run --with . just-akash)"

FAKE = """import json, os, sys
from pathlib import Path
with Path(os.environ["TASK_CALLS"]).open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
sub = sys.argv[1]
if sub == "resolve-owner":
    print(json.dumps({"owner": os.environ["FAKE_OWNER"]}))
elif sub == "destroy":
    print("Deployment closed")
elif sub == "verify-closed":
    print(json.dumps({"closed": True, "reason": "terminal"}))
"""


def close_step(doc: dict | None = None) -> dict:
    if doc is None:
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), "runner-teardown.yml did not parse to a mapping"
    steps = [s for s in doc["jobs"]["teardown"]["steps"] if s.get("id") == "close"]
    assert len(steps) == 1, f"expected one close step, found {len(steps)}"
    return steps[0]


def run_close(
    tmp_path: Path, wallet: str, group: str, script: str | None = None
) -> tuple[int, list, dict, str]:
    source = script if script is not None else close_step()["run"]
    assert source.count(JA_LINE) == 1, "just-akash invocation moved; re-derive the harness"
    # Rebase the step's own /tmp/ paths BEFORE inserting the recorder, whose
    # interpreter path can itself live under /tmp.
    assert source.count("/tmp") == source.count("/tmp/") > 0, "a /tmp path would escape"
    source = source.replace("/tmp/", f"{tmp_path}/")
    fake = tmp_path / "fake.py"
    fake.write_text(FAKE, encoding="utf-8")
    source = source.replace(
        JA_LINE, f"JA=({shlex.quote(sys.executable)} {shlex.quote(str(fake))})\nsleep() {{ :; }}"
    )
    calls, output = tmp_path / "calls", tmp_path / "output"
    calls.write_text("", encoding="utf-8")
    output.write_text("", encoding="utf-8")
    proc = subprocess.run(
        ["bash", "-e", "-c", source],
        env={
            **os.environ,
            "DSEQ": "1002",
            "WALLET_ADDRESS": wallet,
            "DEPLOYMENT_GROUP": group,
            "TAG_PREFIX": "pool",
            "FAKE_OWNER": OWNER,
            "GITHUB_OUTPUT": str(output),
            "TASK_CALLS": str(calls),
        },
        text=True,
        capture_output=True,
        timeout=30,
    )
    invoked = [json.loads(line) for line in calls.read_text().splitlines()]
    outputs = {}
    for line in output.read_text().splitlines():
        key, _, value = line.partition("=")
        outputs.setdefault(key, []).append(value)
    return proc.returncode, invoked, outputs, proc.stdout + proc.stderr


def test_an_owner_less_receipt_is_held_and_never_resolved_or_closed(tmp_path: Path) -> None:
    rc, calls, out, log = run_close(tmp_path, wallet="", group=GROUP)
    assert rc != 0, "a HELD lease must fail the job, never read as success"
    assert calls == [], f"an owner-less receipt reached just-akash: {calls}"
    assert out.get("held_reason") == ["OWNER_UNKNOWN"], out
    assert out.get("held_dseq") == ["1002"], out
    assert out.get("held_deployment_group") == [GROUP], out
    assert out.get("closed") == ["unknown"], out
    assert "HELD: lease owner unknown" in log


def test_a_non_canonical_group_cannot_inject_an_output(tmp_path: Path) -> None:
    rc, calls, out, _ = run_close(tmp_path, wallet="", group="g\nclosed=true")
    assert rc != 0 and calls == []
    assert out.get("closed") == ["unknown"], out
    assert out.get("held_deployment_group") == [""], out


def test_opposite_leg_a_bound_owner_closes_the_exact_owner_dseq_pair(tmp_path: Path) -> None:
    """Must stay green: the held path may not swallow the receipts that carry an owner."""
    rc, calls, out, log = run_close(tmp_path, wallet=OWNER, group=GROUP)
    assert rc == 0, log[-2000:]
    by_sub = {c[0]: c for c in calls}
    assert [c[0] for c in calls] == ["resolve-owner", "destroy", "verify-closed"], calls

    def flag(call: list, name: str) -> str | None:
        return call[call.index(name) + 1] if name in call else None

    assert flag(by_sub["resolve-owner"], "--expected-owner") == OWNER
    assert flag(by_sub["resolve-owner"], "--expected-group") == GROUP
    assert flag(by_sub["destroy"], "--expected-owner") == OWNER
    assert flag(by_sub["destroy"], "--expected-group") == GROUP
    assert flag(by_sub["verify-closed"], "--owner") == OWNER
    assert out.get("closed") == ["true"] and "held_reason" not in out, out


@pytest.mark.parametrize("group", [GROUP, ""])
def test_no_call_ever_passes_a_group_without_an_owner(tmp_path: Path, group: str) -> None:
    """The exact shape resolve-owner refuses (--expected-group requires --expected-owner)."""
    _, calls, _, _ = run_close(tmp_path, wallet="", group=group)
    for call in calls:
        assert not ("--expected-group" in call and "--expected-owner" not in call), call


def test_held_is_published_through_the_job_and_workflow_and_cannot_be_skipped() -> None:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = doc["jobs"]["teardown"]
    step = close_step(doc)
    assert "continue-on-error" not in step and "continue-on-error" not in job
    assert "if" not in step, "a conditional close step could be skipped instead of failing"
    outputs = (doc.get("on") or doc.get(True))["workflow_call"]["outputs"]
    for key in ("held_reason", "held_dseq", "held_deployment_group"):
        assert job["outputs"][key] == f"${{{{ steps.close.outputs.{key} }}}}"
        assert outputs[key]["value"] == f"${{{{ jobs.teardown.outputs.{key} }}}}"
