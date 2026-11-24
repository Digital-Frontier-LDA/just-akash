"""openmix-17uy: the Console wallet key reaches only main, enforced by GitHub, not by the workflow file.

`SOPS_AGE_KEY` opens secrets/ci.sops.env (AKASH_API_KEY, the shared Console wallet). workflow_dispatch on a
branch runs THAT branch's copy of the file, so an in-file ref check (#512) can be deleted by any repo writer.
The fix: the key is an ENVIRONMENT secret of `wallet`, whose deployment-branch policy admits main only, and
every job that reads it declares `environment: wallet`. A job with no environment that runs first
(wallet-environment-gate.yml) refuses unless `wallet` exists with exactly that policy, because a run naming
a missing environment would CREATE it unprotected. No pull_request run gets the key.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"
GATE_FILE = WF / "wallet-environment-gate.yml"
GATE_USES = "./.github/workflows/wallet-environment-gate.yml"
KEY = re.compile(r"secrets\.SOPS_AGE_KEY\b")
# The measured readers on main 1a687b1f (11 jobs in 9 workflows). A NEW reader is held to the same rule
# by the census below; this list only proves the census really sees them (anti-vacuity).
KNOWN = {
    ("auto-topup-audit.yml", "audit"), ("bid-probe.yml", "probe"), ("ci.yml", "e2e-shell"),
    ("ci.yml", "e2e-secrets"), ("cleanup-stale.yml", "cleanup"), ("close-orphans.yml", "close"),
    ("lease-intent-backfill.yml", "backfill"), ("provider-canary.yml", "canary"),
    ("provider-smoke.yml", "smoke"), ("provider-smoke.yml", "report"), ("reseal-akash-key.yml", "reseal"),
}


def _load(p: Path) -> dict:
    return yaml.safe_load(p.read_text()) or {}


def _on(doc: dict) -> dict:
    on = doc.get(True, doc.get("on"))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {k: None for k in on}
    return on or {}


def _readers() -> list[tuple[str, str, dict, dict]]:
    out = []
    for p in sorted(WF.glob("*.y*ml")):
        doc = _load(p)
        for name, job in (doc.get("jobs") or {}).items():
            if KEY.search(yaml.safe_dump(job)):
                out.append((p.name, name, job, doc))
    return out


def _needs(job: dict) -> list[str]:
    n = job.get("needs") or []
    return [n] if isinstance(n, str) else list(n)


def test_the_census_sees_every_known_reader():
    assert {(f, j) for f, j, _, _ in _readers()} >= KNOWN


@pytest.mark.parametrize("wf,name", sorted(KNOWN))
def test_every_reader_is_in_wallet_behind_the_gate(wf, name):
    found = [(j, d) for f, n, j, d in _readers() if (f, n) == (wf, name)]
    assert len(found) == 1
    job, doc = found[0]
    assert job.get("environment") == "wallet", f"{wf}:{name} must declare the literal environment 'wallet'"
    assert "wallet-gate" in _needs(job), f"{wf}:{name} must need wallet-gate"
    gate = doc["jobs"]["wallet-gate"]
    assert gate.get("uses") == GATE_USES and "environment" not in gate
    assert gate.get("permissions") == {"actions": "read"}
    cond = str(job.get("if", ""))
    if "always()" in cond:
        # always() would run the job even when the gate FAILED; it must also require the gate's success.
        assert "needs.wallet-gate.result == 'success'" in cond, f"{wf}:{name}: always() bypasses the gate"


def test_no_reader_runs_without_the_environment():
    # Every reader, known or new: environment wallet + the gate. A reader added later inherits the rule.
    bad = [f"{f}:{n}" for f, n, j, _ in _readers()
           if j.get("environment") != "wallet" or "wallet-gate" not in _needs(j)]
    assert bad == []


def test_no_pull_request_run_gets_the_key():
    for f, n, job, doc in _readers():
        on = _on(doc)
        if not ({"pull_request", "pull_request_target", "merge_group"} & set(on)):
            continue
        cond = str(job.get("if", ""))
        assert "github.event_name == 'push'" in cond and "pull_request" not in cond, (
            f"{f}:{n} runs on a pull_request/merge_group workflow; its if must admit push only")
        assert "||" not in cond.replace("needs.wallet-gate.result == 'success'", ""), f"{f}:{n}: an || reopens it"


def test_every_pr_reports_the_skipped_e2e():
    note = _load(WF / "ci.yml")["jobs"]["dependabot-coverage-note"]
    assert note["if"] == "github.event_name == 'pull_request'"
    assert "openmix-17uy" in yaml.safe_dump(note)


# --- the gate's own logic, run against a stub `gh` -----------------------------------------------------

def _gate_script() -> str:
    doc = _load(GATE_FILE)
    assert set(_on(doc)) == {"workflow_call"}
    assert doc.get("permissions") == {"actions": "read"}
    job = doc["jobs"]["check"]
    assert "environment" not in job, "the gate itself must not name an environment"
    steps = job["steps"]
    assert len(steps) == 1 and steps[0]["env"]["ENVIRONMENT"] == "wallet"
    return steps[0]["run"]


def _run_gate(tmp_path: Path, env_json, policies, *, default="main", fail_repo=False) -> subprocess.CompletedProcess:
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    (data / "repo").write_text("" if fail_repo else json.dumps({"default_branch": default}))
    (data / "env").write_text("" if env_json is None else json.dumps(env_json))
    (data / "pol").write_text("" if policies is None else json.dumps({"branch_policies": policies}))
    gh = bin_ / "gh"
    gh.write_text(f"""#!/usr/bin/env bash
# stub: gh api <path> [--jq <filter>]
path="$2"; jqf="${{4:-.}}"
case "$path" in
  */deployment-branch-policies) f={data}/pol ;;
  */environments/*) f={data}/env ;;
  repos/*) f={data}/repo ;;
esac
[ -s "$f" ] || {{ echo "HTTP 404" >&2; exit 1; }}
jq -r "$jqf" "$f"
""")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "GH_TOKEN": "x", "REPO": "o/r", "ENVIRONMENT": "wallet"}
    return subprocess.run(["bash", "-c", _gate_script()], env=env, capture_output=True, text=True)


MAIN_ONLY = {"deployment_branch_policy": {"custom_branch_policies": True, "protected_branches": False}}


def test_gate_passes_only_a_main_only_wallet(tmp_path):
    r = _run_gate(tmp_path, MAIN_ONLY, [{"type": "branch", "name": "main"}])
    assert r.returncode == 0, r.stdout + r.stderr


MAIN = [{"type": "branch", "name": "main"}]


@pytest.mark.parametrize("case,env_json,policies,default,fail_repo,reason", [
    ("missing-environment", None, None, "main", False, "is missing or unreadable"),
    ("no-branch-policy", {"deployment_branch_policy": None}, [], "main", False, "must use selected branches"),
    # A stale `main` entry left behind after the policy was switched away must not pass:
    ("no-policy-stale-main", {"deployment_branch_policy": None}, MAIN, "main", False, "must use selected branches"),
    ("protected-branches", {"deployment_branch_policy": {"custom_branch_policies": False, "protected_branches": True}}, [], "main", False, "must use selected branches"),
    ("protected-stale-main", {"deployment_branch_policy": {"custom_branch_policies": False, "protected_branches": True}}, MAIN, "main", False, "must use selected branches"),
    ("main-plus-another", MAIN_ONLY, MAIN + [{"type": "branch", "name": "dev"}], "main", False, "must admit exactly branch"),
    ("a-wildcard", MAIN_ONLY, [{"type": "branch", "name": "*"}], "main", False, "must admit exactly branch"),
    ("a-tag-policy", MAIN_ONLY, [{"type": "tag", "name": "main"}], "main", False, "must admit exactly branch"),
    ("not-the-default", MAIN_ONLY, MAIN, "trunk", False, "must admit exactly branch"),
    ("policies-unreadable", MAIN_ONLY, None, "main", False, "cannot read the branch policies"),
    ("repo-unreadable", MAIN_ONLY, MAIN, "main", True, "cannot read the default branch"),
])
def test_gate_fails_closed(tmp_path, case, env_json, policies, default, fail_repo, reason):
    r = _run_gate(tmp_path, env_json, policies, default=default, fail_repo=fail_repo)
    assert r.returncode != 0, f"{case}: the gate passed\n{r.stdout}{r.stderr}"
    assert "::error::wallet gate:" in r.stdout, case
    assert reason in r.stdout, f"{case}: failed for the wrong reason\n{r.stdout}"


# --- GA ruling 2026-11-24: E2E is off pull requests, and NO pull_request-reachable job touches a secret ---
# pull_request / pull_request_target runs execute (or are steered by) the PR branch. A job such a run can
# reach must reference no secret at all (GITHUB_TOKEN aside), inherit none, and name no environment that
# holds one. Parsed from the workflow YAML: triggers, each job's `if:`, its environment, its secret refs.

PR_EVENTS = {"pull_request", "pull_request_target"}
#: Only INSIDE a `${{ ... }}` expression: a file named `.secrets.baseline` is not a secret reference.
EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.S)
SECRET_REF = re.compile(r"\bsecrets\.(?!GITHUB_TOKEN\b)[A-Za-z_]\w*|\bsecrets\s*\[|\(\s*secrets\s*\)")


def _secret_refs(body: str) -> list[str]:
    return sorted({m for e in EXPR.findall(body) for m in SECRET_REF.findall(e)})


def _pr_workflows() -> list[tuple[str, dict]]:
    return [(p.name, _load(p)) for p in sorted(WF.glob("*.y*ml")) if PR_EVENTS & set(_on(_load(p)))]


def _push_only(cond: str) -> bool:
    # Unreachable from a PR only when the condition REQUIRES a push event and nothing ORs it back open.
    c = cond.replace("needs.wallet-gate.result == 'success'", "")
    return "github.event_name == 'push'" in c and "||" not in c and "pull_request" not in c


def _pr_reachable_jobs() -> list[tuple[str, str, dict]]:
    return [(f, n, j) for f, doc in _pr_workflows() for n, j in (doc.get("jobs") or {}).items()
            if not _push_only(str(j.get("if", "")))]


def test_no_workflow_uses_pull_request_target():
    # It runs with the base repo's secrets and a writable token on PR-controlled input; none needs it.
    users = [p.name for p in sorted(WF.glob("*.y*ml")) if "pull_request_target" in _on(_load(p))]
    assert users == [], f"pull_request_target is not allowed (openmix-17uy GA ruling): {users}"


def test_the_secret_matcher_reads_expressions_only():
    assert _secret_refs("run: detect-secrets scan --baseline .secrets.baseline") == []
    assert _secret_refs("${{ secrets.GITHUB_TOKEN }}") == []
    assert _secret_refs("x: ${{ secrets.AKASH_API_KEY }}") == ["secrets.AKASH_API_KEY"]
    assert _secret_refs("x: ${{ toJSON( secrets ) }}") == ["( secrets )"]
    assert _secret_refs("x: ${{ secrets['K'] }}") == ["secrets["]


def test_the_pr_census_is_not_vacuous():
    names = {f for f, _ in _pr_workflows()}
    assert "ci.yml" in names
    reachable = {(f, n) for f, n, _ in _pr_reachable_jobs()}
    assert ("ci.yml", "dependabot-coverage-note") in reachable  # PR jobs really are seen
    assert not {("ci.yml", "e2e-shell"), ("ci.yml", "e2e-secrets"), ("ci.yml", "wallet-gate")} & reachable


def test_no_pr_reachable_job_references_a_secret_or_an_environment():
    bad = []
    for f, n, job in _pr_reachable_jobs():
        body = yaml.safe_dump(job)
        refs = _secret_refs(body)
        if refs:
            bad.append(f"{f}:{n} references {refs}")
        if job.get("secrets") is not None:
            bad.append(f"{f}:{n} passes secrets to a reusable workflow ({job.get('secrets')!r})")
        if job.get("environment") is not None:
            bad.append(f"{f}:{n} names environment {job.get('environment')!r}")
    assert bad == [], "a pull_request run could reach a secret:\n" + "\n".join(bad)


@pytest.mark.parametrize("name", ["e2e-shell", "e2e-secrets"])
def test_e2e_is_push_only(name):
    job = _load(WF / "ci.yml")["jobs"][name]
    assert _push_only(str(job["if"])), f"ci.yml:{name} must run on push to main only: {job['if']!r}"
    assert job.get("environment") == "wallet"
    assert set(_on(_load(WF / "ci.yml"))["push"]["branches"]) == {"main"}
