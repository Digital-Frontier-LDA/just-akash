#!/usr/bin/env python3
"""Guard the repo invariants that keep silently regressing.

Both checks here exist because the invariant was broken in practice, more than
once, by changes that looked fine in isolation and passed every other gate:

1. **SOPS is the only secret channel.** v1.35.0 moved every CI secret into
   ``secrets/ci.sops.env``, leaving ``SOPS_AGE_KEY`` as the sole GitHub secret.
   Two later PRs re-added a direct ``secrets.AKASH_API_KEY`` to a workflow (once
   in the Prometheus work, once in cleanup-stale, fixed by #92). Nothing
   prevented it: a direct reference is ordinary-looking YAML and it *works*, so
   the migration silently erodes one workflow at a time.

2. **The changelog stays ordered and matches the package version.** Parallel
   sessions each add a section, and a naive merge is happy to leave two sections
   claiming the same version in an order that no longer descends (main briefly
   had two ``## [1.37.0]`` headers, one of them below 1.36.1).

3. **A dispatchable job decrypts wallet secrets only from the default branch.**
   Six workflow_dispatch workflows loaded ``./.github/actions/sops-env`` with
   no ref check, so any repo writer could dispatch a branch whose code closes or
   spends on the shared Console wallet (openmix-17uy). auto-topup-audit.yml and
   reseal-akash-key.yml had the guard; nothing made the others carry it.

Run: ``python3 .github/scripts/check_repo_invariants.py [--root DIR]``
Exit 0 when every invariant holds, 1 otherwise (with the offending lines).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# The one GitHub secret this repo is allowed to consume: the age key that
# decrypts everything else. Adding a name here is a deliberate policy decision —
# it means that value does NOT live in SOPS.
ALLOWED_SECRETS = frozenset(
    {
        "SOPS_AGE_KEY",
        # Auto-provisioned per job by GitHub, never stored by us. SOPS could not
        # hold it even in principle, so forbidding it would buy nothing.
        "GITHUB_TOKEN",
    }
)

# `secrets.NAME`, but only inside a ${{ }} expression — otherwise the detect-
# secrets baseline FILENAME (`.secrets.baseline`, which appears in these
# workflows) reads as a secret reference and the guard cries wolf.
# Every ``vars.NAME`` a workflow reads, and -- the part that matters -- WHAT SILENTLY
# HAPPENS WHEN IT IS UNSET.
#
# An unset repo variable is not an error at runtime. It evaluates to the empty string, and
# `''` is falsy in an Actions expression, so a step gated on one simply does not run and the
# job still reports success. There is nothing in the repo to read that says the variable was
# ever expected to exist: the dependency lives in the GitHub UI, invisible to review, to git
# log, and to anyone reading the workflow.
#
# That is not hypothetical. `provider-canary.yml` gated its deploy step on
# `vars.CANARY_AUTODEPLOY`, which was never set. Every scheduled run skipped the deploy and
# exited GREEN for weeks while no canary existed on any provider -- and the only visible
# symptom was three unrelated-looking provider alerts paging critical for a day (#129).
#
# Declaring the variable here does not set it. It forces the CONSEQUENCE into the diff, where
# a reviewer reads "when unset, no canary is ever deployed" and asks the question nobody asked
# for weeks: is it actually set?
DECLARED_VARS: dict[str, str] = {
    "CANARY_AUTODEPLOY": (
        "gates the canary deploy step in provider-canary.yml on SCHEDULED runs "
        "(`inputs.deploy_missing` covers workflow_dispatch, and input defaults do NOT apply "
        "to a schedule trigger). WHEN UNSET: no canary is ever deployed, every scheduled run "
        "still reports success, and the fleet goes dark -- see akash_canary_active_deployments "
        "and the AkashCanaryNoDeployments alert, which exist because of exactly this."
    ),
}

_EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.S)
_SECRET_REF = re.compile(r"\bsecrets\.([A-Za-z_][A-Za-z_0-9]*)")
_VARS_REF = re.compile(r"\bvars\.([A-Za-z_][A-Za-z_0-9]*)")
_VERSION_HEADER = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\]", re.M)
_PYPROJECT_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"', re.M)


def secret_refs(text: str) -> set[str]:
    """GitHub secret names referenced from ``${{ }}`` expressions in ``text``."""
    names: set[str] = set()
    for expr in _EXPR.findall(text):
        names.update(_SECRET_REF.findall(expr))
    return names


def declared_call_secrets(text: str) -> set[str]:
    """Secret names a REUSABLE workflow declares under ``on.workflow_call.secrets``.

    These are supplied by the CALLING repository and cannot live in this repo's SOPS
    file even in principle: a caller brings its own Akash account and its own runner
    PAT, and this repo must never hold either. `workflow_call` secrets are a typed
    parameter list, not a stored credential — the exemption is the whole reason the
    mechanism exists.

    Deliberately narrow: only names DECLARED in that block are exempt, and only in a
    file that actually has one. A reusable workflow reaching for some other
    ``${{ secrets.X }}`` is still a violation, because that one WOULD have to come
    from this repo.
    """
    try:
        import yaml
    except ImportError:  # keep the guard working without a yaml dependency
        return set()
    try:
        doc = yaml.safe_load(text) or {}
    except Exception:
        return set()
    on = doc.get("on") or doc.get(True) or {}
    if not isinstance(on, dict):
        return set()
    call = on.get("workflow_call") or {}
    if not isinstance(call, dict):
        return set()
    declared = call.get("secrets") or {}
    return set(declared) if isinstance(declared, dict) else set()


def _lines_referencing(text: str, name: str, token: str, extractor) -> list[int]:
    """Line numbers where ``name`` is referenced FROM AN ACTIONS EXPRESSION.

    A plain substring scan also reports PROSE. A `run:` line reading
    ``echo 'set vars.X to enable'`` is documentation, not a reference, and reporting it as a
    violation is how a check trains people to ignore it. Raised by CodeRabbit on #132 and
    reproduced: a workflow with the expression on one line and the same name in a run: line
    below yielded TWO problems, one pointing at the prose.

    Falls back to the substring scan when the precise pass finds nothing, so a reference
    inside a ${{ }} block that SPANS lines is still reported -- imprecisely located, but
    never silently dropped. Losing a real finding is the worse failure of the two.
    """
    lines = text.splitlines()
    hits = [n for n, line in enumerate(lines, 1) if name in extractor(line)]
    return hits or [n for n, line in enumerate(lines, 1) if token in line]


def check_secrets(root: Path) -> list[str]:
    """Every ``${{ secrets.X }}`` in CI config must name an allowed secret."""
    problems: list[str] = []
    targets = sorted(
        p
        for d in (".github/workflows", ".github/actions")
        for p in (root / d).rglob("*.y*ml")
        if p.is_file()
    )
    for path in targets:
        text = path.read_text(encoding="utf-8")
        exempt = ALLOWED_SECRETS | declared_call_secrets(text)
        for name in sorted(secret_refs(text) - exempt):
            # Report the line so the fix is obvious, not just the file.
            for lineno in _lines_referencing(text, name, f"secrets.{name}", secret_refs):
                problems.append(
                    f"{path.relative_to(root)}:{lineno}: uses ${{{{ secrets.{name} }}}} "
                    f"directly — put it in secrets/ci.sops.env and read it via "
                    f"./.github/actions/sops-env"
                )
    return problems


def vars_refs(text: str) -> set[str]:
    """Repo-variable names referenced from ``${{ }}`` expressions in ``text``."""
    names: set[str] = set()
    for expr in _EXPR.findall(text):
        names.update(_VARS_REF.findall(expr))
    return names


def check_workflow_vars(root: Path, *, declarations_must_be_used: bool = False) -> list[str]:
    """Every ``${{ vars.X }}`` is declared, and every declaration is still used.

    Enforced in BOTH directions on purpose. Undeclared references are the bug this exists
    for. Dead declarations matter just as much: a table that still lists a variable nobody
    reads teaches the next reader that it is load-bearing, and a stale declaration is how a
    guard becomes decoration.

    The two arms are NOT the same kind of statement, which is why the second is opt-in.
    "no undeclared reference" is a property of any tree. "every declaration is still used"
    is a property of THIS repo's table -- pointed at a synthetic fixture it would demand
    that the fixture reference our variables, which is how a general checker acquires a
    private dependency on one repo's content. An existing test caught exactly that.
    """
    problems: list[str] = []
    targets = sorted(
        p
        for d in (".github/workflows", ".github/actions")
        for p in (root / d).rglob("*.y*ml")
        if p.is_file()
    )
    # On NAMES vs VALUES, because CodeQL flags the shared problem-printer in main() as
    # clear-text logging of sensitive data (py/clear-text-logging-sensitive-data) once this
    # check feeds it. Dismissed as a false positive on #132, recorded here rather than only
    # in the Security tab: what reaches a problem string is a path, a line number, and the
    # IDENTIFIER captured by _VARS_REF -- never a value. Variable and secret values live in
    # GitHub and never enter this process, which only reads files from disk, and the name is
    # already committed in cleartext in the very file being reported. If a future check ever
    # puts a VALUE into a problem string, that alert is real and this note does not cover it.
    used: set[str] = set()
    for path in targets:
        text = path.read_text(encoding="utf-8")
        refs = vars_refs(text)
        used |= refs
        for name in sorted(refs - set(DECLARED_VARS)):
            for lineno in _lines_referencing(text, name, f"vars.{name}", vars_refs):
                problems.append(
                    f"{path.relative_to(root)}:{lineno}: reads ${{{{ vars.{name} }}}} but "
                    f"it is not declared in DECLARED_VARS. An unset repo variable is the "
                    f"empty string, so this expression is silently FALSE and whatever it "
                    f"gates never runs -- while the job still reports success. Declare it "
                    f"with what breaks when it is missing."
                )
    for name in sorted(set(DECLARED_VARS) - used) if declarations_must_be_used else []:
        problems.append(
            f"DECLARED_VARS lists {name!r} but no workflow reads it any more. Remove the "
            f"entry -- a stale declaration reads as a live dependency and is how this table "
            f"stops describing reality."
        )
    return problems


SOPS_ENV_ACTION = "./.github/actions/sops-env"

# Environments with REQUIRED REVIEWERS, which the bead accepts in place of the ref guard.
# Empty on purpose: the environment's name in a workflow cannot show that it has reviewers
# (only the repo settings can), and today the only environment is `copilot`, with no
# protection rules. Adding a name here is a claim someone checked those settings.
PROTECTED_ENVIRONMENTS: frozenset[str] = frozenset()

# A step condition that runs even after an earlier step FAILED. On a step between the guard
# and sops-env it would load the secrets anyway once the guard has refused the ref.
_STATUS_OVERRIDE = re.compile(r"\b(?:always|failure|cancelled)\s*\(")


def _env_names_for(env: dict, expr: str) -> list[str]:
    """Names in a step's ``env:`` whose value is exactly ``${{ <expr> }}``."""
    want = re.compile(r"\$\{\{\s*" + re.escape(expr) + r"\s*\}\}")
    return [k for k, v in env.items() if isinstance(v, str) and want.fullmatch(v.strip())]


def is_ref_guard(step: object) -> bool:
    """True for a step that fails unless github.ref is refs/heads/<default_branch>.

    It must compare the FULL ref (github.ref, not ref_name, so refs/tags/main fails) with
    the event's default branch and ``exit 1`` on a mismatch. It may not carry an ``if:``
    or ``continue-on-error``, either of which lets the job go on past a refusal. The only
    early pass it may have is for a schedule event, matched on github.event_name: GitHub
    runs a schedule only from the default branch, and that event has no repository payload
    to read default_branch from. Any other ``exit 0`` or ``|| true`` disqualifies it.
    """
    if not isinstance(step, dict) or "uses" in step or "if" in step:
        return False
    if step.get("continue-on-error") not in (None, False):
        return False
    env, run = step.get("env"), step.get("run")
    if not isinstance(env, dict) or not isinstance(run, str):
        return False
    for event in _env_names_for(env, "github.event_name"):
        run = re.sub(
            r'if\s+\[\s*"\$\{?' + re.escape(event) + r'\}?"\s*=\s*"?schedule"?\s*\]\s*;\s*then\s*'
            r"(?:echo[^\n]*\n\s*)?exit\s+0\s*\n\s*fi\b",
            "",
            run,
        )
    if re.search(r"\bexit\s+0\b|\|\|\s*(?:true|:)", run):
        return False
    for ref in _env_names_for(env, "github.ref"):
        for default in _env_names_for(env, "github.event.repository.default_branch"):
            compare = re.compile(
                r'if\s+\[\s*"\$\{?'
                + re.escape(ref)
                + r'\}?"\s*!=\s*"refs/heads/\$\{?'
                + re.escape(default)
                + r'\}?"\s*\]\s*;\s*then\b(?:(?!\bfi\b).)*?\bexit\s+1\b',
                re.S,
            )
            if compare.search(run):
                return True
    return False


def _dispatchable(on: object) -> bool:
    if isinstance(on, str):
        return on == "workflow_dispatch"
    if isinstance(on, (list, dict)):
        return "workflow_dispatch" in on
    return False


def check_dispatch_ref_guard(root: Path) -> list[str]:
    """Every job of a workflow_dispatch workflow that loads sops-env is ref-guarded first.

    workflow_dispatch runs the workflow AND the code of whatever ref the dispatcher picks,
    so a job that decrypts secrets/ci.sops.env must refuse a non-default ref before it
    checks anything out or loads the secrets (openmix-17uy). The guard has to come before
    the first checkout and the first sops-env step, and no step from the guard up to that
    sops-env step may carry an always()/failure()/cancelled() condition that would run it
    after the guard failed. A job whose environment is in PROTECTED_ENVIRONMENTS is exempt.
    """
    try:
        import yaml
    except ImportError:
        # Fail closed: a guard that quietly checks nothing is worse than a red build.
        return ["check_dispatch_ref_guard needs PyYAML (python3 -m pip install pyyaml)"]
    problems: list[str] = []
    for path in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        doc = yaml.safe_load(text) or {}
        if not isinstance(doc, dict):
            continue
        # YAML 1.1 reads a bare `on:` key as the boolean True.
        if not _dispatchable(doc.get("on", doc.get(True))):
            continue
        jobs = doc.get("jobs") or {}
        for job_id, job in jobs.items() if isinstance(jobs, dict) else []:
            if not isinstance(job, dict):
                continue
            steps = job.get("steps") or []
            sops = [
                i
                for i, s in enumerate(steps)
                if isinstance(s, dict) and str(s.get("uses", "")).rstrip("/") == SOPS_ENV_ACTION
            ]
            if not sops:
                continue
            env = job.get("environment")
            env_name = env.get("name") if isinstance(env, dict) else env
            if env_name in PROTECTED_ENVIRONMENTS:
                continue
            m = re.search(rf"^  {re.escape(str(job_id))}:", text, re.M)
            where = f"{path.relative_to(root)}:{text.count(chr(10), 0, m.start()) + 1 if m else 1}"
            first_sops = sops[0]
            checkouts = [
                i
                for i, s in enumerate(steps)
                if isinstance(s, dict) and str(s.get("uses", "")).startswith("actions/checkout@")
            ]
            limit = min([first_sops, *checkouts])
            guards = [i for i, s in enumerate(steps[:limit]) if is_ref_guard(s)]
            sops_name = steps[first_sops].get("name") or SOPS_ENV_ACTION
            if not guards:
                problems.append(
                    f"{where}: job {job_id!r} loads sops-env (step {sops_name!r}) in a "
                    f"workflow_dispatch workflow with no default-ref guard before its first "
                    f"checkout and sops-env step -- any branch or tag could be dispatched to "
                    f"run with the wallet key. Add the 'Refuse to run from a non-default ref' "
                    f"step from auto-topup-audit.yml first."
                )
                continue
            for s in steps[guards[0] + 1 : first_sops + 1]:
                cond = s.get("if") if isinstance(s, dict) else None
                if isinstance(cond, str) and _STATUS_OVERRIDE.search(cond):
                    problems.append(
                        f"{where}: job {job_id!r} step {s.get('name') or s.get('uses')!r} "
                        f"runs with if: {cond} between the ref guard and sops-env, so it "
                        f"still runs after the guard refused the ref."
                    )
    return problems


def check_changelog(root: Path) -> list[str]:
    """Changelog versions descend strictly, are unique, and the newest matches
    the packaged version."""
    problems: list[str] = []
    changelog = root / "CHANGELOG.md"
    pyproject = root / "pyproject.toml"
    if not changelog.is_file() or not pyproject.is_file():
        return ["CHANGELOG.md or pyproject.toml missing"]

    versions = [tuple(map(int, m)) for m in _VERSION_HEADER.findall(changelog.read_text())]
    if not versions:
        return ["CHANGELOG.md has no '## [x.y.z]' section headers"]

    seen: set[tuple[int, ...]] = set()
    for v in versions:
        if v in seen:
            problems.append(
                f"CHANGELOG.md: duplicate section for {'.'.join(map(str, v))} — two "
                "sections claiming one version (usually a merge that re-added a "
                "header instead of folding into the existing one)"
            )
        seen.add(v)
    for newer, older in zip(versions, versions[1:], strict=False):
        if newer <= older:
            problems.append(
                f"CHANGELOG.md: {'.'.join(map(str, newer))} is listed above "
                f"{'.'.join(map(str, older))} but is not newer — sections must "
                "descend strictly"
            )

    m = _PYPROJECT_VERSION.search(pyproject.read_text())
    if not m:
        problems.append("pyproject.toml has no version")
    else:
        packaged = m.group(1)
        newest = ".".join(map(str, versions[0]))
        if packaged != newest:
            problems.append(
                f"version mismatch: pyproject.toml is {packaged} but the newest "
                f"CHANGELOG section is {newest} — a release bumped one and not the other"
            )
    return problems


# This script lives at <repo>/.github/scripts/, so its own repo root is two levels up.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Problems are formatted "<path>:<line>: ...". Pulling that back out lets the GitHub
# annotation carry file=/line=, which is what makes it land ON the offending line instead
# of at the top of the run. The comment below has always claimed that; until now it was not
# true. Raised by CodeRabbit on #132.
_PROBLEM_LOC = re.compile(r"^(?P<file>[^\s:]+):(?P<line>\d+): ")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="Repository root (default: cwd)")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()

    problems = (
        check_secrets(root)
        # Only demand that declarations still be USED when we are checking this repo --
        # see check_workflow_vars' docstring for why the two arms differ.
        + check_workflow_vars(root, declarations_must_be_used=(root == REPO_ROOT))
        + check_changelog(root)
        + check_dispatch_ref_guard(root)
    )
    if problems:
        print("Repo invariant check FAILED:\n", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
            # GitHub annotation so the failure lands on the offending line -- with
            # file=/line= it actually does. Problems with no location still annotate.
            loc = _PROBLEM_LOC.match(p)
            if loc:
                print(f"::error file={loc['file']},line={loc['line']}::{p}")
            else:
                print(f"::error::{p}")
        return 1
    # Say only what was actually checked. Outside this repo the dead-declaration arm does
    # not run, so claiming "all still referenced" would report coverage nobody verified --
    # which is the exact defect this whole check exists to make impossible.
    declared = (
        f"{len(DECLARED_VARS)} declared repo variable(s), all still referenced"
        if root == REPO_ROOT
        else f"{len(DECLARED_VARS)} declared repo variable(s) (usage unchecked: not this repo)"
    )
    print(
        f"Repo invariants OK (SOPS-only secrets; {declared}; changelog ordered and in sync; "
        "dispatchable sops-env jobs ref-guarded)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
