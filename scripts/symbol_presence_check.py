#!/usr/bin/env python3
"""
Symbol-presence check: flag Python files on a PR branch that are missing
top-level symbols that main HEAD has. FAILs the check on POSSIBLE_DROP
cases (unless the PR's commit messages document the deletion as
INTENTIONAL_DELETE). WARNS on stale-branch cases and on files that
do not parse — those are not defects but requests to rebase / manual
review.

For each missing symbol we record:
  - in_merge_base : was the symbol in the file at merge-base?
  - branch_stale  : is main NOT an ancestor of HEAD? (i.e. the branch
                    has not caught up via rebase / merge)
  - mentioned     : does any PR commit message name the symbol with a
                    delete/remove/drop/deprecate/retire keyword?

Symbol extraction supports FunctionDef, AsyncFunctionDef, ClassDef,
plain Assign (with tuple / list targets), and annotated assignments
with a value (AnnAssign where node.value is not None). Bare
annotations (x: int with no = ...) are declarations, not symbols;
they are not counted.

Categorisation (three reachable cases):
  - in_merge_base + branch_stale           -> LEGACY_STALE   (WARN)
  - in_merge_base + NOT branch_stale       -> POSSIBLE_DROP  (FAIL)
                                                or INTENTIONAL_DELETE
                                                (OK) if mentioned
  - NOT in_merge_base + branch_stale       -> STALE_BRANCH   (WARN)

Plus a fourth category for files that cannot be parsed (any rev):
  - SYNTAX_PARSE_FAIL                                              (WARN)
    The file exists at one or more of (merge-base, head, main) but
    does not parse. The check cannot conclude anything about this
    file -- "I couldn't look" must not look like "nothing was there".
    Reviewer must manually inspect; the check explicitly abstains.

Plus a deletion path for whole files:
  - When the PR deletes a file (HEAD has no content at path), every
    symbol main has at that path is reported as missing. Each one is
    categorised as POSSIBLE_DROP / INTENTIONAL_DELETE / LEGACY_STALE /
    STALE_BRANCH the same way a per-symbol drop would be, just at
    larger scale. Renames below git's -M threshold may surface as
    delete + add; the FAIL is conservative (reviewer should verify
    the symbols are still present under the new name).

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
  0  - OK, or WARN only (stale branch; parse failure; PR author should
       rebase / fix the file).
  1  - FAIL: at least one POSSIBLE_DROP that wasn't auto-downgraded
       by a commit-message mention. Reviewer must verify each finding
       before merge.
  2  - Internal error (git invocation failed, or merge-base could not
       be computed).
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path


def run(cmd: list[str], cwd: Path) -> str:
    # All git invocations come from this script with arguments we built,
    # not from user-supplied strings; the safety check S603 is not load-
    # bearing here. S607 is the "partial executable path" rule — `git`
    # resolves via PATH at invocation, which is the standard pattern
    # for dev tooling that depends on the user's installed git.
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)  # noqa: S603, S607
    return r.stdout


def _names(target: ast.expr) -> Iterator[str]:
    """Yield Name ids from an assignment target, recursing into Tuple /
    List unpacking. Does not descend into attribute / subscript targets
    (foo.bar = ... and foo[0] = ... are not namespace bindings at module
    level for our purposes)."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _names(elt)


def symbols_from_source(source: str) -> set[str]:
    """Return the set of top-level symbol names defined in `source`.
    Raises SyntaxError on parse failure — the caller decides how to
    surface that. Silently swallowing parse errors would make
    'I couldn't look' indistinguishable from 'nothing was there'."""
    tree = ast.parse(source)
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                out.update(_names(t))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            # Annotated assignment with a value (FOO: int = 1) IS a
            # module-level binding. Bare annotation (x: int, no = ...)
            # is a declaration only and is not counted.
            out.update(_names(node.target))
    return out


def symbols_at(repo: Path, rev: str, path: str) -> set[str]:
    """Return top-level symbols at rev:path. Returns an empty set if
    the file does not exist at that rev (e.g., deleted at HEAD, or
    added between fork and main). Raises SyntaxError on parse failure."""
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

    # Determine which Python files the PR modifies. Includes deleted
    # files: a PR that drops a whole module is the maximal drop and
    # MUST be reported, not silently skipped. (Working-tree .exists()
    # filter would have hidden this — see tests/test_*.py scenario D.)
    try:
        files = [
            f
            for f in run(["git", "diff", "--name-only", f"{base}...{head}"], repo).splitlines()
            if f.endswith(".py")
        ]
    except subprocess.CalledProcessError as e:
        print(f"git diff failed: {e.stderr}", file=sys.stderr)
        return 2
    if not files:
        print("OK: no Python files modified by this PR")
        return 0

    try:
        merge_base = run(["git", "merge-base", base, head], repo).strip()
    except subprocess.CalledProcessError as e:
        print(f"git merge-base failed: {e.stderr}", file=sys.stderr)
        return 2
    try:
        ancestor_proc = subprocess.run(  # noqa: S603
            ["git", "merge-base", "--is-ancestor", base, head],  # noqa: S607
            cwd=repo,
            capture_output=True,
        )
    except FileNotFoundError as e:
        print(f"git not found: {e}", file=sys.stderr)
        return 2
    is_stale = ancestor_proc.returncode != 0

    findings: list[tuple[str, str, str, str, str]] = []
    # (file, symbol, in_base, mentioned, category)
    parse_failures: list[tuple[str, str]] = []
    # (path, error_message)
    for path in files:
        try:
            s_base = symbols_at(repo, merge_base, path)
            s_head = symbols_at(repo, head, path)
            s_main = symbols_at(repo, base, path)
        except SyntaxError as e:
            # "I couldn't look" must be loud -- record and skip per-
            # symbol comparison for this file. Dedupe by path.
            if path not in {p for p, _ in parse_failures}:
                parse_failures.append((path, str(e)))
            continue

        # Deleted file at HEAD: s_head is set(), so missing = s_main --
        # every main symbol is reported missing. Categorisation below
        # applies as for any missing symbol.
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

    if not findings and not parse_failures:
        print(
            f"OK: {len(files)} Python file(s) checked, no missing symbols; branch_stale={is_stale}"
        )
        return 0

    if findings:
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
        print("  (c) Whole-file deletion -- the PR removed an entire module.")
        print("      Every symbol in main's version of that file is listed")
        print("      above. Confirm the module is genuinely no longer needed")
        print("      (no callers remain) before merging.")
        print()
    if oks:
        print("== OK (auto-downgraded by commit-message mention) ==")
        emit("OK", oks)
        print()
    if warns:
        print("== WARN (not a defect; PR author should rebase) ==")
        emit("WARN", warns)
        print()
    if parse_failures:
        print("== WARN (could not parse; check abstains -- manual review) ==")
        for path, err in parse_failures:
            print(f"  WARN SYNTAX_PARSE_FAIL  {path}  ({err})")
        print()

    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
