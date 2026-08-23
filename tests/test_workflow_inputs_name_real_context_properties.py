"""Every `${{ }}` expression in every workflow must name a REAL context property.

★ THE MEASURED CLASS (five live sites, 2026-08-23, three of them filed as #184):
GitHub's expression evaluator resolves a nonexistent property to EMPTY STRING —
never an error ("If you attempt to dereference a nonexistent property, it will
evaluate to an empty string", Accessing contextual information about workflow
runs, `## job context`). All of this shipped green under six required checks:

  1. `github.organization` (#182's teardown wiring — caught only by review threads;
     fixed by reading the pool's own input)
  2/3. `job.workflow_repository` (runner-pool.yml:262 + runner-teardown.yml:119 —
     masked by the literal fallback, so it works by accident)
  4/5. `job.workflow_sha` (:263 + :120 — resolves empty, `ref:` falls back to the
     default branch: every runner is provisioned and torn down from
     just-akash@main's TIP at deploy-second, not the caller's pin)

#184 was filed from runner-pool.yml alone; the sibling runner-teardown.yml sites
were found by THIS TEST's first run — the guard found what the issue missed.

★ GROUND TRUTH, AND HOW NOT TO READ IT: the documented `job` context is exactly
`container` / `services` / `status` — no `workflow_*` properties exist on any
context. runner-pool.yml's why-comment (added in #148) asserts "the job context
is documented as referring to the REUSABLE workflow file" — that documentation
does not exist; the names look like the runner's ENVIRONMENT variables
(GITHUB_WORKFLOW_SHA / GITHUB_WORKFLOW_REF), which are a different namespace.
actionlint independently agrees: `property "workflow_sha" is not defined in
object type {check_run_id; container; services; status}`.

⚠ A summarized fetch of the docs page FABRICATED a full `job.workflow_*` property
table (with verbatim-looking definitions and a GHES note) that inverted the
verdict 180°. The allowlists below are transcribed from the RAW page — do not
"correct" them from a summary.

★ THE ALLOWLISTS ARE NOT GUESSED: roots and closed-vocabulary leaves are
transcribed from the documented context tables (github/job/strategy/runner
leaves; steps.<id> is validated against ids declared in the same job;
needs.<job>.outputs.<key> against the producing job's declared outputs). A new
legitimate leaf gets added in the same commit that introduces it — that is the
moment a human vets the name; this test exists to force that moment to exist.

KNOWN_INVALID carries the #184 sites so this guard can land GREEN before the
fix (the pin-targets question — what teardown should check out — is a decision
owned by #184, not by this guard). The exception is SELF-CLEANING: when #184's
fix removes a site, the stale entry fails this file until it is dropped, so the
list cannot outlive the bug it names.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))

EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
STR_LIT = re.compile(r"'[^']*'|\"[^\"]*\"")
# Dotted reference: 2+ segments, hyphens allowed (matrix.python-version, env.X-Y)
CHAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z_][A-Za-z0-9_\-]*)+")

# (workflow file, chain) -> issue. Sites still present are exempt from failure;
# entries whose sites have ALL been fixed fail the test until removed.
KNOWN_INVALID = {
    ("runner-pool.yml", "job.workflow_repository"): "#184",
    ("runner-pool.yml", "job.workflow_sha"): "#184",
    ("runner-teardown.yml", "job.workflow_repository"): "#184",
    ("runner-teardown.yml", "job.workflow_sha"): "#184",
}

# Root context objects, per the documented context table.
VALID_ROOTS = {
    "github",
    "env",
    "vars",
    "job",
    "jobs",
    "steps",
    "runner",
    "secrets",
    "strategy",
    "matrix",
    "needs",
    "inputs",
}

# Leaves transcribed from the RAW docs tables (see module docstring). github.* list
# is the documented table in full; do not prune or extend from memory.
VALID_GITHUB_LEAVES = {
    "action",
    "action_path",
    "action_ref",
    "action_repository",
    "action_status",
    "actor",
    "actor_id",
    "api_url",
    "base_ref",
    "env",
    "event",
    "event_name",
    "event_path",
    "graphql_url",
    "head_ref",
    "job",
    "path",
    "ref",
    "ref_name",
    "ref_protected",
    "ref_type",
    "repository",
    "repository_id",
    "repository_owner",
    "repository_owner_id",
    "repositoryUrl",
    "retention_days",
    "run_id",
    "run_number",
    "run_attempt",
    "secret_source",
    "server_url",
    "sha",
    "token",
    "triggering_actor",
    "workflow",
    "workflow_ref",
    "workflow_sha",
    "workspace",
}
# job.*: documented table = container/services/status. actionlint's runtime-derived
# grammar additionally carries check_run_id, so it is accepted here too.
VALID_JOB_FIELDS = {"container", "services", "status", "check_run_id"}
VALID_STRATEGY_LEAVES = {"fail-fast", "job-index", "job-total", "max-parallel"}
VALID_RUNNER_LEAVES = {"name", "os", "arch", "temp", "tool_cache", "debug", "environment"}
# needs.<job> / jobs.<job>: second segment is outputs or result, no others.
VALID_RESULT_LEAVES = {"outputs", "result"}


def _iter_jobs(doc: dict):
    return {k: v for k, v in (doc.get("jobs") or {}).items() if isinstance(v, dict)}


def _exprs(node, path=""):
    """Yield (path, expression-string) for every expression anywhere in the doc.

    Bare `if:` conditions (no `${{ }}` wrapper) are legal and common — treat the
    whole value as one expression so they are covered too.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _exprs(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _exprs(v, f"{path}[{i}]")
    elif isinstance(node, str):
        for m in EXPR.finditer(node):
            yield path, m.group(1)
        if path.endswith(".if") and "${{" not in node and node.strip():
            yield path, node


def _chains(expression: str) -> list[str]:
    """Dotted references in one expression.

    String literals are blanked first (a literal's right side of `||` is not a
    context lookup — the #184 bug hid behind exactly one of those); names
    immediately followed by `(` are function calls (fromJSON, format, ...), not
    context references.
    """
    blanked = STR_LIT.sub("''", expression)
    found = []
    for m in CHAIN.finditer(blanked):
        if blanked[m.end() : m.end() + 1] == "(":
            continue
        found.append(m.group(0))
    return found


def _check_chain(chain: str, job: dict | None, jobs: dict) -> str | None:
    """Return a failure reason for one dotted reference, or None if it is real."""
    segs = chain.split(".")
    root, leaves = segs[0], segs[1:]
    if root not in VALID_ROOTS:
        return f"{root!r} is not a context root GitHub defines"
    if root == "github" and leaves and leaves[0] not in VALID_GITHUB_LEAVES:
        return (
            f"github.{leaves[0]} is not a documented property — it resolves to EMPTY "
            f"STRING at runtime (the #182/#184 class)"
        )
    if root == "job" and leaves and leaves[0] not in VALID_JOB_FIELDS:
        return (
            f"job.{leaves[0]} is not a documented property (job exposes only "
            f"{sorted(VALID_JOB_FIELDS)}) — this exact class shipped #182 green"
        )
    if root == "strategy" and leaves and leaves[0] not in VALID_STRATEGY_LEAVES:
        return f"strategy.{leaves[0]} is not a documented property"
    if root == "runner" and leaves and leaves[0] not in VALID_RUNNER_LEAVES:
        return f"runner.{leaves[0]} is not a documented property"
    if root in {"needs", "jobs"} and len(leaves) >= 1:
        dep = leaves[0]
        if dep not in jobs:
            return f"{root}.{dep} names no job in this workflow"
        if len(leaves) >= 2 and leaves[1] not in VALID_RESULT_LEAVES:
            return f"{root}.{dep}.{leaves[1]} is not defined (outputs/result only)"
        if len(leaves) >= 3 and leaves[1] == "outputs":
            declared = set(jobs[dep].get("outputs") or {})
            if declared and leaves[2] not in declared:
                return (
                    f"{root}.{dep}.outputs.{leaves[2]} is not a declared output of "
                    f"job {dep!r} — a typo here resolves to EMPTY STRING"
                )
    if root == "steps" and leaves and job is not None:
        declared = {
            st.get("id") for st in job.get("steps", []) if isinstance(st, dict) and st.get("id")
        }
        if leaves[0] not in declared:
            return f"steps.{leaves[0]} is not a declared step id in this job"
    return None


def test_every_expression_in_every_workflow_names_a_real_context_property():
    """★★ The workflow-wide extension of #182's pin. Five sites, one class, one guard."""
    failures: list[str] = []
    exempt_seen: set[tuple[str, str]] = set()
    for wf in WORKFLOWS:
        doc = yaml.safe_load(wf.read_text())
        jobs = _iter_jobs(doc)
        for jname, job in jobs.items():
            # needs: must reference jobs that exist in this workflow
            needs = job.get("needs")
            for n in ({needs} if isinstance(needs, str) else set(needs or [])) - set(jobs):
                failures.append(f"{wf.name}:{jname}: needs {n!r} — no such job in this workflow")
            for path, expr in _exprs(job, jname):
                for chain in _chains(expr):
                    reason = _check_chain(chain, job, jobs)
                    if reason is None:
                        continue
                    key = (wf.name, chain)
                    if key in KNOWN_INVALID:
                        exempt_seen.add(key)
                        continue
                    failures.append(f"{wf.name}:{path}: `{chain}` -> {reason}")
        # workflow_call output mappings use the jobs context above the jobs: block
        wf_call = (doc.get("on") or {}).get("workflow_call") or {}
        for path, expr in _exprs(wf_call, "on.workflow_call"):
            for chain in _chains(expr):
                reason = _check_chain(chain, None, jobs)
                if reason is None:
                    continue
                key = (wf.name, chain)
                if key in KNOWN_INVALID:
                    exempt_seen.add(key)
                    continue
                failures.append(f"{wf.name}:{path}: `{chain}` -> {reason}")
    assert not failures, "\n".join(["expressions naming non-existent context:", *failures])
    # Self-cleaning: an exception whose sites are all fixed must be dropped, or a
    # future reader cannot tell "known debt" from "stale list".
    stale = set(KNOWN_INVALID) - exempt_seen
    assert not stale, (
        "KNOWN_INVALID entries whose sites no longer exist — the fix landed, drop "
        "them so the list cannot outlive the bug:\n"
        + "\n".join(f"{wf}: {chain} ({KNOWN_INVALID[(wf, chain)]})" for wf, chain in sorted(stale))
    )
