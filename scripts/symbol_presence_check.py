#!/usr/bin/env python3
"""
Symbol-presence check: flag Python files on a PR branch that are missing
top-level symbols that main HEAD has. FAILs the check on POSSIBLE_DROP
cases (unless the PR's commit messages document the deletion as
INTENTIONAL_DELETE). WARNS on stale-branch cases — those are not
defects but requests to rebase.

For each missing symbol we record:
  - in_merge_base : was the symbol in the file at merge-base?
  - branch_stale  : is main NOT an ancestor of HEAD? (i.e. the branch
                    has not caught up via rebase / merge)
  - mentioned     : does any PR commit message name the symbol with a
                    delete/remove/drop/deprecate/retire keyword?

Categorisation (three reachable cases):
  - in_merge_base + branch_stale           -> LEGACY_STALE   (WARN)
  - in_merge_base + NOT branch_stale       -> POSSIBLE_DROP  (FAIL)
                                                or INTENTIONAL_DELETE
                                                (OK) if mentioned
  - NOT in_merge_base + branch_stale       -> STALE_BRANCH   (WARN)

The fourth combination (NOT in_merge_base + NOT branch_stale) is
unreachable by construction: if main is an ancestor of HEAD, then
merge-base(branch, main) == main, so every symbol main has IS in the
merge-base. A guard that never fires would produce no evidence of not
firing — so it is removed rather than left as documented coverage
that never runs.

The check exists because a merge that silently drops content produces
no conflict, no failing test, and no undefined symbol — every existing
check evaluates the file as it now is and never asks "is this version
missing things main had that the PR didn't intend to remove?". The
case the check was DESIGNED to surface:

  - POSSIBLE_DROP: a symbol main has is missing from the PR head.
    Two distinct causes produce the same PR diff shape:

      (a) Intentional delete — the PR author removed the symbol as
          part of the work. Auto-downgraded to INTENTIONAL_DELETE (OK)
          if a PR commit message names the symbol with a delete /
          remove / drop / deprecate / retire keyword.

      (b) Conflict-resolution drop — the branch rebased onto newer
          main, the rebase produced a conflict, and resolution took
          "ours" across the region containing main's symbol. Symbol
          is in the merge-base (it was on main when the branch forked
          — that's why in_merge_base=True), branch is up to date
          (rebased), and the symbol is silently gone. The PR diff
          shows the symbol as removed — same shape as a legitimate
          delete, indistinguishable from the diff alone. Fix: redo
          the conflict resolution keeping both sides; the symbol
          should reappear.

Note: a symbol "in PR's diff as a deletion" is the same condition as
"in merge_base" for missing symbols, so the diff check is redundant.
The commit-message check is a heuristic — it down-weights cases the
PR author documented, but it does not prove intent. Reviewer triage is
still required for every flag.

Exit codes:
  0  - OK, or WARN only (stale branch; PR author should rebase)
  1  - FAIL: at least one POSSIBLE_DROP that wasn't auto-downgraded
       by a commit-message mention. Reviewer must verify each finding
       before merge.
  2  - Internal error (git invocation failed).
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], cwd: Path) -> str:
    # All git invocations come from this script with arguments we built,
    # not from user-supplied strings; the safety check S603 is not load-
    # bearing here.
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)  # noqa: S603
    return r.stdout


def symbols_from_source(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
    return out


def symbols_at(repo: Path, rev: str, path: str) -> set[str]:
    try:
        src = run(["git", "show", f"{rev}:{path}"], repo)
    except subprocess.CalledProcessError:
        return set()
    return symbols_from_source(src)


def pr_commits_mention(repo: Path, base: str, head: str, name: str) -> bool:
    """Return True if any commit on the PR branch (reachable from head
    but not from base) mentions `name` together with a delete/remove/drop
    keyword. Heuristic only — used to down-weight POSSIBLE_DROP cases
    where the PR author explicitly documented the deletion."""
    try:
        log = run(["git", "log", "--format=%s", f"{base}..{head}"], repo)
    except subprocess.CalledProcessError:
        return False
    keywords = ("delete", "remove", "drop", "deprecate", "retire")
    name_lower = name.lower()
    for line in log.splitlines():
        lc = line.lower()
        if name_lower in lc and any(k in lc for k in keywords):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--base", required=True, help="base ref (e.g. origin/main)")
    ap.add_argument("--head", required=True, help="PR head ref")
    args = ap.parse_args()

    repo = args.repo.resolve()
    base = args.base
    head = args.head

    # Determine which Python files the PR modifies
    try:
        files = [
            f
            for f in run(["git", "diff", "--name-only", f"{base}...{head}"], repo).splitlines()
            if f.endswith(".py") and (repo / f).exists()
        ]
    except subprocess.CalledProcessError as e:
        print(f"git diff failed: {e.stderr}", file=sys.stderr)
        return 2
    if not files:
        print("OK: no Python files modified by this PR")
        return 0

    merge_base = run(["git", "merge-base", base, head], repo).strip()
    is_stale = (
        subprocess.run(  # noqa: S603
            ["git", "merge-base", "--is-ancestor", base, head],  # noqa: S607
            cwd=repo,
            capture_output=True,
        ).returncode
        != 0
    )

    findings: list[tuple[str, str, str, str, str]] = []
    # (file, symbol, in_base, mentioned, category)
    for path in files:
        s_base = symbols_at(repo, merge_base, path)
        s_head = symbols_at(repo, head, path)
        s_main = symbols_at(repo, base, path)

        missing = s_main - s_head
        for sym in sorted(missing):
            in_base = sym in s_base
            mentioned = pr_commits_mention(repo, base, head, sym)
            # The fourth combination (not in_base and not is_stale) is
            # structurally unreachable: if main is an ancestor of HEAD
            # then merge-base(branch, main) == main, so every symbol
            # main has IS in the merge-base. No finding is produced for
            # that case; if it ever holds, it's a bug in merge-base
            # itself, not a CI signal we can produce here.
            if in_base and is_stale:
                category = "LEGACY_STALE"
            elif in_base and not is_stale:
                category = "INTENTIONAL_DELETE" if mentioned else "POSSIBLE_DROP"
            else:
                category = "STALE_BRANCH"
            findings.append((path, sym, str(in_base), str(mentioned), category))

    fail_categories = {"POSSIBLE_DROP"}
    warn_categories = {"STALE_BRANCH", "LEGACY_STALE"}

    fails = [f for f in findings if f[4] in fail_categories]
    warns = [f for f in findings if f[4] in warn_categories]
    oks = [f for f in findings if f[4] == "INTENTIONAL_DELETE"]

    if not findings:
        print(
            f"OK: {len(files)} Python file(s) checked, no missing symbols; branch_stale={is_stale}"
        )
        return 0

    print(
        f"FLAG: {len(findings)} missing symbol(s) across {len(files)} Python file(s); "
        f"branch_stale={is_stale}, merge-base={merge_base[:12]}"
    )
    print()

    def emit(label: str, items: list[tuple[str, str, str, str, str]]) -> None:
        for path, sym, in_base, mentioned, cat in items:
            print(
                f"  {label} {cat:30s}  {path}::{sym}  "
                f"(in_merge_base={in_base}, pr_commit_mentions={mentioned})"
            )

    if fails:
        print("== FAIL (reviewer must verify each finding before merge) ==")
        emit("FAIL", fails)
        print()
        print("Each FAIL means: a symbol main has is missing from this PR head.")
        print("Two possible causes, both producing the same PR diff shape:")
        print("  (a) Intentional delete by the PR author -- confirm a commit")
        print("      message on the PR branch names the symbol with delete /")
        print("      remove / drop / deprecate / retire. If unmentioned, ask")
        print("      the author to either re-introduce the symbol or amend a")
        print("      commit message to document the removal.")
        print("  (b) Lost in conflict resolution -- the branch rebased onto")
        print("      newer main, a conflict was resolved by taking 'ours',")
        print("      and main's symbol was discarded. Fix: redo the rebase")
        print("      or merge and KEEP BOTH SIDES in the conflict region;")
        print("      the symbol should reappear.")
        print()
    if oks:
        print("== OK (auto-downgraded by commit-message mention) ==")
        emit("OK", oks)
        print()
    if warns:
        print("== WARN (not a defect; PR author should rebase) ==")
        emit("WARN", warns)
        print()

    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
