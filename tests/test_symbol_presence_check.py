"""Tests for scripts/symbol_presence_check.py.

The check exists because a merge that silently drops content produces no
conflict, no failing test, and no undefined symbol. These tests construct
three scenarios and verify the check distinguishes them:

  - drop            : branch rebased onto main with conflict resolution
                      that took "ours" and discarded main's new symbols
                      → MUST exit 1 with FAIL POSSIBLE_DROP
  - intentional     : branch legitimately deletes a symbol, commit
                      message documents the deletion
                      → MUST exit 0 with OK INTENTIONAL_DELETE
  - stale           : branch never rebased, main is ahead
                      → MUST exit 0 with WARN STALE_BRANCH
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "symbol_presence_check.py"


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "t@x",
    }
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
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


def _write(cwd: Path, name: str, body: str, msg: str) -> None:
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
        env={**os.environ, "GIT_EDITOR": "true"},
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
        env={**os.environ, "GIT_EDITOR": "true"},
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


# --- Edge case: empty diff (no Python files modified) ---


def test_no_python_files_modified_exits_zero(sandbox: Path) -> None:
    _write(sandbox, "main", "x = 1\n", "main: add a non-function module-level change")

    _git(sandbox, "checkout", "-q", "-b", "branch-noop", "main")
    (sandbox / "README.md").write_text("hi\n")
    _git(sandbox, "add", "README.md")
    _git(sandbox, "commit", "-q", "-m", "branch: add README only")

    result = _run_check(sandbox, "main", "branch-noop")
    # Branch added a new function (x = 1 → not a function), so main and
    # branch-noop bar.py differ. But neither is missing anything main has.
    # The exact exit depends on whether branch-noop is detected as having
    # a missing symbol. With "x = 1" only, main has it (added there);
    # branch-noop also has it. So no missing → exit 0.
    assert result.returncode == 0, result.stdout
