"""Tests for scripts/symbol_presence_check.py.

The check exists because a merge that silently drops content produces no
conflict, no failing test, and no undefined symbol. These tests construct
thirteen scenarios and verify the check distinguishes them:

  A. drop             : branch rebased onto main with conflict resolution
                        that took "ours" and discarded main's new symbols
                        → MUST exit 1 with FAIL POSSIBLE_DROP
  B. intentional      : branch legitimately deletes a symbol, commit
                        message documents the deletion
                        → MUST exit 0 with OK INTENTIONAL_DELETE
  C. stale            : branch never rebased, main is ahead
                        → MUST exit 0 with WARN STALE_BRANCH
  D. deleted_module   : branch deletes an entire module from main
                        → MUST exit 1 with FAIL POSSIBLE_DROP for every
                          symbol main had in that file
  E. syntax_error     : a file in the PR diff does not parse
                        → MUST exit 0 with WARN SYNTAX_PARSE_FAIL (loud,
                          not silent OK)
  F. annotated_unpack : main has `FOO: int = 1` and `a, b = ...` style
                        module-level symbols; PR drops them
                        → MUST exit 1 with FAIL POSSIBLE_DROP for each
  G. substring_guard  : commit-message check must use word boundaries; a
                        symbol named "bar" must NOT auto-downgrade just
                        because it appears as a substring inside "embargo"
                        → MUST exit 1 with FAIL POSSIBLE_DROP, never
                          INTENTIONAL_DELETE
  H. starred_unpack   : main has `a, *rest = range(10)` at module level;
                        dropping `rest` MUST be detected (Starred inside
                        Tuple, not just plain Name targets)
                        → MUST exit 1 with FAIL POSSIBLE_DROP
  I. case_insensitive : commit-message check must match the symbol name
                        case-insensitively (lower('Foo') against the
                        lowercased message), so a commit subject saying
                        "remove foo" mentions a symbol named `Foo`
                        → MUST exit 0 with OK INTENTIONAL_DELETE
  J. type_alias       : main has `type UserId = int` (PEP 695, Py 3.12+);
                        dropping the alias MUST be detected
                        → MUST exit 1 with FAIL POSSIBLE_DROP for
                          `UserId`
  K. git_log_failure  : `git log base..head` returns non-zero
                        → MUST exit 2 (internal error), NOT 1 (FAIL);
                          previously the failure was caught and the
                          symbol reported as "unmentioned" (POSSIBLE_DROP
                          on a real PR).
  L. keyword_boundary : commit subject "undelete foo" must NOT match
                        `delete` (substring overlap on the keyword side);
                        the symbol `foo` is dropped on a fresh branch
                        → MUST exit 1 with FAIL POSSIBLE_DROP, never
                          INTENTIONAL_DELETE
  M. renamed_file     : main has `def bar() in old.py`; branch renames
                        `old.py` -> `new.py` AND drops `bar` in the
                        process. `--no-renames` must be in effect so
                        `old.py` is in the diff and the drop is caught.
                        → MUST exit 1 with FAIL POSSIBLE_DROP for `bar`

Plus one edge case (no Python files modified) which is the early-return
"no work to do" path.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "symbol_presence_check.py"


def _git_env() -> dict[str, str]:
    """Identity env used by every subprocess.run that touches git so
    rebase / cherry-pick / commit don't fail with 'Please tell me who
    you are' on a clean machine.

    Also point GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM at /dev/null so a
    developer / CI machine with `commit.gpgsign=true` set globally
    (a real-world failure mode) cannot break the sandbox commits."""
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "t@x",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=_git_env())
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """Init a sandbox git repo with bar.py holding {foo, helper} at M0."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "bar.py").write_text(
        "def foo():\n    return 1\n\n\ndef helper():\n    return 'h'\n"
    )
    _git(tmp_path, "add", "bar.py")
    _git(tmp_path, "commit", "-q", "-m", "M0: foo() + helper()")
    _git(tmp_path, "tag", "M0")
    return tmp_path


def _make_bar(extra: str) -> str:
    return (
        "def foo():\n    return 1\n\n\n"
        "def helper():\n    return 'h'\n\n\n"
        "def qux():\n    return 'q'\n\n\n"
        "def main_only_added():\n    return 'a'\n\n\n" + extra
    )


def _write(cwd: Path, _name: str, body: str, msg: str) -> None:
    # _name is unused at runtime; it exists at call sites to document
    # which branch / scenario is being constructed.
    (cwd / "bar.py").write_text(body)
    _git(cwd, "add", "bar.py")
    _git(cwd, "commit", "-q", "-m", msg)


def _run_check(cwd: Path, base: str, head: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(cwd), "--base", base, "--head", head],
        capture_output=True,
        text=True,
    )


# --- Scenario A: drop via conflict resolution ---


def test_drop_via_conflict_resolution(sandbox: Path) -> None:
    """branch-A rebases onto main with conflict; taking 'ours' drops
    main's qux() and main_only_added() that lived in the conflict region."""
    _write(sandbox, "main", _make_bar(""), "main: add qux() + main_only_added()")

    _git(sandbox, "checkout", "-q", "M0")
    _git(sandbox, "checkout", "-q", "-b", "branch-A")
    _write(
        sandbox,
        "branch-A",
        "def foo():\n    return 2\n\n\n"
        "def helper():\n    return 'h'\n\n\n"
        "def baz():\n    return 'b'\n\n\n"
        "def a_only():\n    return 'a'\n",
        "A: change foo() body + add baz() + a_only()",
    )

    # Rebase onto main. Auto-merge fails on the trailing block; resolve
    # by keeping ours (which discards main's qux/main_only_added).
    r = subprocess.run(
        ["git", "rebase", "main"],
        cwd=sandbox,
        capture_output=True,
        text=True,
        env={**_git_env(), "GIT_EDITOR": "true"},
    )
    assert r.returncode != 0, "expected rebase conflict"

    (sandbox / "bar.py").write_text(
        "def foo():\n    return 2\n\n\n"
        "def helper():\n    return 'h'\n\n\n"
        "def baz():\n    return 'b'\n\n\n"
        "def a_only():\n    return 'a'\n"
    )
    _git(sandbox, "add", "bar.py")
    r = subprocess.run(
        ["git", "rebase", "--continue"],
        cwd=sandbox,
        capture_output=True,
        text=True,
        env={**_git_env(), "GIT_EDITOR": "true"},
    )
    assert r.returncode == 0, f"rebase continue failed: {r.stderr}"

    result = _run_check(sandbox, "main", "branch-A")
    assert result.returncode == 1, (
        f"drop scenario MUST exit 1; got {result.returncode}\n{result.stdout}"
    )
    assert "POSSIBLE_DROP" in result.stdout, result.stdout
    assert "bar.py::qux" in result.stdout, result.stdout
    assert "bar.py::main_only_added" in result.stdout, result.stdout
    # The PR commit message does NOT mention removing qux or main_only_added,
    # so the check must not auto-downgrade to INTENTIONAL_DELETE.
    assert "INTENTIONAL_DELETE" not in result.stdout, result.stdout


# --- Scenario B: intentional delete (commit-message documented) ---


def test_intentional_delete_downgrades_to_ok(sandbox: Path) -> None:
    """branch-B off main deletes qux() and the commit message says so."""
    _write(sandbox, "main", _make_bar(""), "main: add qux() + main_only_added()")

    _git(sandbox, "checkout", "-q", "-b", "branch-B", "main")
    _write(
        sandbox,
        "branch-B",
        "def foo():\n    return 1\n\n\n"
        "def helper():\n    return 'h'\n\n\n"
        "def main_only_added():\n    return 'a'\n",
        "B: intentionally delete qux() as the point of this PR",
    )

    result = _run_check(sandbox, "main", "branch-B")
    assert result.returncode == 0, (
        f"intentional delete with documented commit message MUST exit 0; "
        f"got {result.returncode}\n{result.stdout}"
    )
    assert "INTENTIONAL_DELETE" in result.stdout, result.stdout
    assert "bar.py::qux" in result.stdout, result.stdout
    assert "POSSIBLE_DROP" not in result.stdout, result.stdout


# --- Scenario C: stale branch (never rebased) ---


def test_stale_branch_warns(sandbox: Path) -> None:
    """branch-C off M0; never rebased. main has new symbols C doesn't."""
    _write(sandbox, "main", _make_bar(""), "main: add qux() + main_only_added()")

    _git(sandbox, "checkout", "-q", "M0")
    _git(sandbox, "checkout", "-q", "-b", "branch-C")
    _write(
        sandbox,
        "branch-C",
        "def foo():\n    return 99\n\n\n"
        "def helper():\n    return 'h'\n\n\n"
        "def c_only():\n    return 'c'\n",
        "C: change foo() + add c_only() (never rebased)",
    )

    result = _run_check(sandbox, "main", "branch-C")
    assert result.returncode == 0, (
        f"stale branch MUST exit 0 with WARN; got {result.returncode}\n{result.stdout}"
    )
    assert "STALE_BRANCH" in result.stdout, result.stdout
    assert "bar.py::qux" in result.stdout, result.stdout
    assert "bar.py::main_only_added" in result.stdout, result.stdout
    assert "branch_stale=True" in result.stdout, result.stdout


# --- Scenario D: whole-file deletion (the maximal drop) ---


def test_deleted_module_drops_all_symbols(sandbox: Path) -> None:
    """branch-D deletes bar.py entirely. Every symbol main had in
    bar.py must be reported as missing -- this is the maximal drop
    and the case the old (repo / f).exists() filter silently swallowed."""
    _write(
        sandbox,
        "main",
        "def foo():\n    return 1\n\n\n"
        "def helper():\n    return 'h'\n\n\n"
        "def qux():\n    return 'q'\n",
        "main: add bar.py with foo, helper, qux",
    )

    _git(sandbox, "checkout", "-q", "-b", "branch-D", "main")
    (sandbox / "bar.py").unlink()
    _git(sandbox, "add", "bar.py")
    _git(sandbox, "commit", "-q", "-m", "D: drop bar.py")

    result = _run_check(sandbox, "main", "branch-D")
    assert result.returncode == 1, (
        f"deleted module MUST exit 1; got {result.returncode}\n{result.stdout}"
    )
    assert "POSSIBLE_DROP" in result.stdout, result.stdout
    assert "bar.py::foo" in result.stdout, result.stdout
    assert "bar.py::helper" in result.stdout, result.stdout
    assert "bar.py::qux" in result.stdout, result.stdout
    # The PR commit message does not name a delete-keyword for any
    # specific symbol, so no auto-downgrade.
    assert "INTENTIONAL_DELETE" not in result.stdout, result.stdout


# --- Scenario E: parse failure is loud, not silent ---


def test_syntax_error_file_warns_loudly(sandbox: Path) -> None:
    """A Python file in the PR diff does not parse. The check must
    NOT silently report OK -- 'I couldn't look' must look different
    from 'nothing was there'."""
    _write(sandbox, "main", "def foo():\n    return 1\n", "main: add foo()")

    _git(sandbox, "checkout", "-q", "-b", "branch-E", "main")
    (sandbox / "bar.py").write_text("def foo(:\n    return 1\n")  # unclosed paren
    _git(sandbox, "add", "bar.py")
    _git(sandbox, "commit", "-q", "-m", "E: introduce syntax error")

    result = _run_check(sandbox, "main", "branch-E")
    # Cannot conclude FAIL because we genuinely can't tell whether any
    # symbol is missing -- but must NOT silently report OK either.
    assert result.returncode == 0, (
        f"parse failure MUST exit 0 with WARN; got {result.returncode}\n{result.stdout}"
    )
    assert "SYNTAX_PARSE_FAIL" in result.stdout, result.stdout
    assert "bar.py" in result.stdout, result.stdout


# --- Scenario F: annotated assignments + tuple unpacking ---


def test_annotated_and_tuple_symbols_detected(sandbox: Path) -> None:
    """Main defines `FOO: int = 1` (annotated) and `first, second = 1, 2`
    (tuple unpacking). Both styles must be counted as top-level symbols;
    the old code missed both."""
    _write(
        sandbox,
        "main",
        "FOO: int = 1\n\n\n"
        "BAR: str = 'x'\n\n\n"
        "first, second = 1, 2\n\n\n"
        "def kept():\n    return 'k'\n",
        "main: add annotated + tuple symbols",
    )

    _git(sandbox, "checkout", "-q", "-b", "branch-F", "main")
    (sandbox / "bar.py").write_text("def kept():\n    return 'k'\n")
    _git(sandbox, "add", "bar.py")
    _git(sandbox, "commit", "-q", "-m", "F: drop constants")

    result = _run_check(sandbox, "main", "branch-F")
    assert result.returncode == 1, (
        f"missing annotated + tuple symbols MUST exit 1; got {result.returncode}\n{result.stdout}"
    )
    assert "FOO" in result.stdout, result.stdout
    assert "BAR" in result.stdout, result.stdout
    assert "first" in result.stdout, result.stdout
    assert "second" in result.stdout, result.stdout


# --- Scenario G: substring false positive guard on commit-message check ---


def test_substring_match_does_not_auto_downgrade(sandbox: Path) -> None:
    """`pr_commits_mention` must use word boundaries -- a symbol named
    'bar' must NOT match inside 'embargo'. Without word boundaries, the
    old substring check would auto-downgrade a real drop to OK just
    because a delete-keyword appeared anywhere on a line that happened
    to contain the symbol's characters as a substring."""
    _write(
        sandbox,
        "main",
        "def bar():\n    return 1\n\n\ndef helper():\n    return 'h'\n",
        "main: add bar() + helper()",
    )

    _git(sandbox, "checkout", "-q", "-b", "branch-G", "main")
    (sandbox / "bar.py").write_text("def helper():\n    return 'h'\n")
    _git(sandbox, "add", "bar.py")
    _git(sandbox, "commit", "-q", "-m", "G: remove embargo logic")

    result = _run_check(sandbox, "main", "branch-G")
    # 'bar' inside 'embargo' must NOT match: auto-downgrade requires
    # the symbol name to appear as a whole token next to a delete keyword.
    assert result.returncode == 1, (
        f"substring false positive MUST NOT auto-downgrade; "
        f"got {result.returncode}\n{result.stdout}"
    )
    assert "POSSIBLE_DROP" in result.stdout, result.stdout
    assert "bar.py::bar" in result.stdout, result.stdout
    assert "INTENTIONAL_DELETE" not in result.stdout, result.stdout


# --- Scenario H: starred unpacking target (`a, *rest = ...`) ---


def test_starred_unpacking_target_detected(sandbox: Path) -> None:
    """Main defines `a, *rest = range(10)` at module level (Starred
    inside Tuple). Both `a` and `rest` are namespace bindings at module
    level; `_names` must recurse into `Starred.value` so `rest` is not
    silently missed as a top-level symbol."""
    _write(
        sandbox,
        "main",
        "def kept():\n    return 'k'\n\n\na, *rest = range(10)\n",
        "main: add starred-unpack `a, *rest = range(10)`",
    )

    _git(sandbox, "checkout", "-q", "-b", "branch-H", "main")
    (sandbox / "bar.py").write_text("def kept():\n    return 'k'\n")
    _git(sandbox, "add", "bar.py")
    _git(sandbox, "commit", "-q", "-m", "H: drop starred unbind target")

    result = _run_check(sandbox, "main", "branch-H")
    assert result.returncode == 1, (
        f"missing starred-unpack symbol MUST exit 1; got {result.returncode}\n{result.stdout}"
    )
    assert "rest" in result.stdout, result.stdout


# --- Scenario I: case-insensitive symbol mention in commit subject ---


def test_case_insensitive_symbol_match(sandbox: Path) -> None:
    """The PR commit-message mention check is case-insensitive: a
    symbol named `Foo` (PascalCase) must match a commit subject that
    says 'foo' next to a delete keyword. Without normalisation, the
    regex was searching for `Foo` against a lowercased message and
    never matched."""
    _write(
        sandbox,
        "main",
        "def Foo():\n    return 1\n\n\ndef helper():\n    return 'h'\n",
        "main: add Foo() + helper()",
    )

    _git(sandbox, "checkout", "-q", "-b", "branch-I", "main")
    (sandbox / "bar.py").write_text("def helper():\n    return 'h'\n")
    _git(sandbox, "add", "bar.py")
    _git(sandbox, "commit", "-q", "-m", "I: remove foo (case-insensitive mention)")

    result = _run_check(sandbox, "main", "branch-I")
    assert result.returncode == 0, (
        f"symbol mentioned case-insensitively in commit subject MUST exit 0 "
        f"with INTENTIONAL_DELETE; got {result.returncode}\n{result.stdout}"
    )
    assert "INTENTIONAL_DELETE" in result.stdout, result.stdout
    assert "Foo" in result.stdout, result.stdout


# --- Edge case: empty diff (no Python files modified) ---


def test_no_python_files_modified_exits_zero(sandbox: Path) -> None:
    """A PR whose diff has no Python files at all must exit 0 via the
    'no Python files modified' early-return path. Earlier versions of
    this test confused 'no Python files in diff' with 'bar.py differs'
    -- the diff for branch-noop is empty because branch-noop is created
    from main after main has 'x = 1', so bar.py is unchanged."""
    _write(sandbox, "main", "x = 1\n", "main: add a non-function module-level change")

    _git(sandbox, "checkout", "-q", "-b", "branch-noop", "main")
    (sandbox / "README.md").write_text("hi\n")
    _git(sandbox, "add", "README.md")
    _git(sandbox, "commit", "-q", "-m", "branch: add README only")

    result = _run_check(sandbox, "main", "branch-noop")
    assert result.returncode == 0, (
        f"PR with no Python-file diff MUST exit 0; got {result.returncode}\n{result.stdout}"
    )
    assert "OK: no Python files modified" in result.stdout, result.stdout


# --- Scenario J: Python 3.12+ type alias (PEP 695) ---


def test_type_alias_drop_detected(sandbox: Path) -> None:
    """Main defines `type UserId = int` at module level. The check uses
    `ast.TypeAlias` (Python 3.12+). On a branch that drops the type
    alias, the missing symbol MUST be reported as POSSIBLE_DROP. The
    AST walker must look up `ast.TypeAlias` via getattr so 3.10/3.11
    interpreters don't AttributeError -- a forward-compat requirement
    because `requires-python` is ">=3.10"."""
    _write(
        sandbox,
        "main",
        "type UserId = int\n\n\n\ndef kept():\n    return 'k'\n",
        "main: add type alias UserId",
    )

    _git(sandbox, "checkout", "-q", "-b", "branch-J", "main")
    (sandbox / "bar.py").write_text("def kept():\n    return 'k'\n")
    _git(sandbox, "add", "bar.py")
    _git(sandbox, "commit", "-q", "-m", "J: drop type alias")

    result = _run_check(sandbox, "main", "branch-J")
    assert result.returncode == 1, (
        f"dropped type alias MUST exit 1; got {result.returncode}\n{result.stdout}"
    )
    assert "POSSIBLE_DROP" in result.stdout, result.stdout
    assert "UserId" in result.stdout, result.stdout


# --- Scenario K: git log failure is loud (not silent OK / false FAIL) ---


def test_git_log_failure_exits_two(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `git log base..head` invocation that fails must surface as
    exit 2 (internal error), NOT exit 1 (POSSIBLE_DROP). Previously
    `pr_commits_mention` caught CalledProcessError and returned False,
    making every missing symbol on an up-to-date branch flag as
    POSSIBLE_DROP -- a false positive caused by the check failing quiet.

    Simulate by pointing the script at refs that don't exist on either
    side: `git log BOGUS_BASE..HEAD` returns "fatal: bad revision"."""
    _write(
        sandbox,
        "main",
        "def kept():\n    return 'k'\n\n\ndef bar():\n    return 'b'\n",
        "main: add bar()",
    )

    _git(sandbox, "checkout", "-q", "-b", "branch-K", "main")
    (sandbox / "bar.py").write_text("def kept():\n    return 'k'\n")
    _git(sandbox, "add", "bar.py")
    _git(sandbox, "commit", "-q", "-m", "K: drop bar()")

    # BOGUS_BASE isn't a real ref on the local repo -- `git log` will
    # exit non-zero with stderr "fatal: bad revision ...", which is the
    # documented internal-error path.
    result = _run_check(sandbox, "BOGUS_BASE", "branch-K")
    assert result.returncode == 2, (
        f"`git log` failure MUST exit 2; got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    # The stderr should mention git (not be silent). Either "git log"
    # or "git merge-base" -- both are documented exit-2 paths when an
    # internal git invocation fails, and the order depends on which
    # call the script reaches first. We just want to assert the check
    # surfaces the failure rather than pretending the symbol is
    # unmentioned.
    assert "git" in (result.stderr + result.stdout).lower(), result.stderr
    assert "POSSIBLE_DROP" not in result.stdout, result.stdout


# --- Scenario L: delete-keyword substring overlap (the other half) ---


def test_keyword_substring_does_not_auto_downgrade(sandbox: Path) -> None:
    """The keyword check is symmetric: `delete` must NOT auto-downgrade
    just because the word `undelete` appears in the commit subject.
    Previously `any(k in lc for k in keywords)` was a substring match,
    so a real drop committed with subject "undelete foo" would have
    been silently classified as INTENTIONAL_DELETE.

    Here the PR commits the file deletion with a subject that says
    "undelete foo from sandbox" — the symbol `foo` is gone from the
    PR head, the subject contains `delete` as a substring inside
    `undelete`, but the subject does NOT mention an intentional
    delete. The check MUST exit 1 with POSSIBLE_DROP."""
    # Sandbox's M0 already has foo() + helper(). Branch main to add
    # `extra()` so the next commit on main has a real diff.
    _write(
        sandbox,
        "main",
        (
            "def foo():\n    return 1\n\n\n"
            "def helper():\n    return 'h'\n\n\n"
            "def extra():\n    return 'e'\n"
        ),
        "main: add extra() alongside foo + helper",
    )

    _git(sandbox, "checkout", "-q", "-b", "branch-L", "main")
    (sandbox / "bar.py").write_text(
        "def helper():\n    return 'h'\n\n\ndef extra():\n    return 'e'\n"
    )
    _git(sandbox, "add", "bar.py")
    _git(sandbox, "commit", "-q", "-m", "L: undelete foo from sandbox")

    result = _run_check(sandbox, "main", "branch-L")
    # `undelete` matches `delete` as a substring but is NOT a delete
    # keyword -- auto-downgrade must require the keyword as a whole
    # token next to the symbol as a whole token.
    assert result.returncode == 1, (
        f"keyword substring overlap MUST NOT auto-downgrade; "
        f"got {result.returncode}\n{result.stdout}"
    )
    assert "POSSIBLE_DROP" in result.stdout, result.stdout
    assert "bar.py::foo" in result.stdout, result.stdout
    assert "INTENTIONAL_DELETE" not in result.stdout, result.stdout


# --- Scenario M: renamed Python file with `git diff` rename detection ---


def test_renamed_python_file_still_flags_dropped_symbols(sandbox: Path) -> None:
    """When a PR renames a Python file, `git diff --name-only` (with
    rename detection, the default) collapses old.py -> new.py into
    one path. The check then asks 'what symbols are at new.py at
    base?' -- but `new.py` did not exist at base, so it's empty,
    and main's symbols in `old.py` are silently missed.

    With `--no-renames`, git reports BOTH paths. The check sees
    `old.py` at HEAD as empty, so every main symbol there is
    reported missing.

    Construct directly without using `_write` (which always writes
    `bar.py`). Bypass the helper and use raw subprocess so the file
    on main is named `old.py`. The sandbox fixture starts the repo
    on `main` with a tag `M0` already pointing at a `bar.py`; we
    add `old.py` here as a second commit on main, then branch for
    the rename+drop. (We don't `git branch -f main HEAD` -- that
    refuses to move the currently-checked-out branch.)"""
    import subprocess as _sp

    # Add old.py holding bar() + kept() on top of the M0 bar.py commit.
    # Sandbox is on branch `main` already (init -b main in the fixture);
    # new commit lands on `main` directly.
    (sandbox / "old.py").write_text("def bar():\n    return 1\n\n\ndef kept():\n    return 'k'\n")
    _git(sandbox, "add", "old.py")
    _git(sandbox, "commit", "-q", "-m", "main: add bar() + kept() in old.py")

    _git(sandbox, "checkout", "-q", "-b", "branch-M", "main")
    # Rename old.py -> new.py via git mv (so git records the rename),
    # then drop `bar` in the new file so the symbols don't survive.
    _sp.run(["git", "mv", "old.py", "new.py"], cwd=sandbox, check=True, env=_git_env())
    (sandbox / "new.py").write_text("def kept():\n    return 'k'\n")
    _sp.run(["git", "add", "-A"], cwd=sandbox, check=True, env=_git_env())
    _git(sandbox, "commit", "-q", "-m", "M: rename old.py to new.py and drop bar()")

    result = _run_check(sandbox, "main", "branch-M")
    assert result.returncode == 1, (
        f"renamed file with dropped symbol MUST exit 1; got {result.returncode}\n{result.stdout}"
    )
    assert "POSSIBLE_DROP" in result.stdout, result.stdout
    assert "bar" in result.stdout, result.stdout
