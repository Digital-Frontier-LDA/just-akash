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
_EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.S)
_SECRET_REF = re.compile(r"\bsecrets\.([A-Za-z_][A-Za-z_0-9]*)")
_VERSION_HEADER = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\]", re.M)
_PYPROJECT_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"', re.M)


def secret_refs(text: str) -> set[str]:
    """GitHub secret names referenced from ``${{ }}`` expressions in ``text``."""
    names: set[str] = set()
    for expr in _EXPR.findall(text):
        names.update(_SECRET_REF.findall(expr))
    return names


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
        for name in sorted(secret_refs(text) - ALLOWED_SECRETS):
            # Report the line so the fix is obvious, not just the file.
            for lineno, line in enumerate(text.splitlines(), 1):
                if f"secrets.{name}" in line:
                    problems.append(
                        f"{path.relative_to(root)}:{lineno}: uses ${{{{ secrets.{name} }}}} "
                        f"directly — put it in secrets/ci.sops.env and read it via "
                        f"./.github/actions/sops-env"
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

    problems = check_secrets(root) + check_changelog(root)
    if problems:
        print("Repo invariant check FAILED:\n", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
            # GitHub annotation so the failure lands on the offending line.
            print(f"::error::{p}")
        return 1
    print("Repo invariants OK (SOPS-only secrets; changelog ordered and in sync).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
