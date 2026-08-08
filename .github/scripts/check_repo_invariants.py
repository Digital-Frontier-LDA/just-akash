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
            for lineno, line in enumerate(text.splitlines(), 1):
                if f"secrets.{name}" in line:
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


def check_workflow_vars(root: Path) -> list[str]:
    """Every ``${{ vars.X }}`` is declared, and every declaration is still used.

    Enforced in BOTH directions on purpose. Undeclared references are the bug this exists
    for. Dead declarations matter just as much: a table that still lists a variable nobody
    reads teaches the next reader that it is load-bearing, and a stale declaration is how a
    guard becomes decoration.
    """
    problems: list[str] = []
    targets = sorted(
        p
        for d in (".github/workflows", ".github/actions")
        for p in (root / d).rglob("*.y*ml")
        if p.is_file()
    )
    used: set[str] = set()
    for path in targets:
        text = path.read_text(encoding="utf-8")
        refs = vars_refs(text)
        used |= refs
        for name in sorted(refs - set(DECLARED_VARS)):
            for lineno, line in enumerate(text.splitlines(), 1):
                if f"vars.{name}" in line:
                    problems.append(
                        f"{path.relative_to(root)}:{lineno}: reads ${{{{ vars.{name} }}}} but "
                        f"it is not declared in DECLARED_VARS. An unset repo variable is the "
                        f"empty string, so this expression is silently FALSE and whatever it "
                        f"gates never runs -- while the job still reports success. Declare it "
                        f"with what breaks when it is missing."
                    )
    for name in sorted(set(DECLARED_VARS) - used):
        problems.append(
            f"DECLARED_VARS lists {name!r} but no workflow reads it any more. Remove the "
            f"entry -- a stale declaration reads as a live dependency and is how this table "
            f"stops describing reality."
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="Repository root (default: cwd)")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()

    problems = check_secrets(root) + check_workflow_vars(root) + check_changelog(root)
    if problems:
        print("Repo invariant check FAILED:\n", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
            # GitHub annotation so the failure lands on the offending line.
            print(f"::error::{p}")
        return 1
    print(f"Repo invariants OK (SOPS-only secrets; {len(DECLARED_VARS)} declared repo "
        f"variable(s), all still referenced; changelog ordered and in sync).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
