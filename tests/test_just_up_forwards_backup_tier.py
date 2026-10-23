"""`just up` takes the backup tier ONLY as its `backup` parameter (CodeRabbit, openmix-dd, #475).

deploy no longer reads AKASH_PROVIDERS_BACKUP. The first fix forwarded an env var
(JUST_UP_BACKUP_PROVIDERS), but the Justfile's `set dotenv-load` lets any .env supply it -- the
inherited-backup bug under a new name (openmix-wqz2y). So the tier is a recipe parameter, and a
.env holding the old OR the new name changes nothing. The recipe runs for real here (its
`#!/bin/bash` is macOS bash 3.2 on a laptop), with `uv` stubbed to print what deploy would get.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVIL = "akash1thirdpartyxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def _up(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    if not shutil.which("just"):
        pytest.skip("just is not installed")
    shutil.copy(ROOT / "Justfile", tmp_path / "Justfile")
    (tmp_path / ".env").write_text(
        f"AKASH_PROVIDERS_BACKUP={EVIL}\nJUST_UP_BACKUP_PROVIDERS={EVIL}\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/sh\nprintf "UVARGS:%s\\n" "$*"\n')
    uv.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path)}
    return subprocess.run(
        ["just", "up", *args], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )


def _deploy_args(out: str) -> str:
    line = next(ln for ln in out.splitlines() if ln.startswith("UVARGS:"))
    return line


def test_a_dotenv_holding_either_name_changes_nothing(tmp_path):
    r = _up(tmp_path, "", "")
    assert r.returncode == 0, r.stderr
    args = _deploy_args(r.stdout)
    assert "--backup-provider" not in args and EVIL not in args, args


def test_the_backup_parameter_is_forwarded_as_flags(tmp_path):
    r = _up(tmp_path, "", "akash1aaaa, akash1bbbb")
    assert r.returncode == 0, r.stderr
    args = _deploy_args(r.stdout)
    assert re.search(r"--backup-provider akash1aaaa --backup-provider akash1bbbb", args), args
    assert EVIL not in args


def test_a_backup_value_that_is_not_addresses_is_refused(tmp_path):
    r = _up(tmp_path, "", "akash1a'; touch pwned; '")
    # the recipe sends its stderr into its log/stdout (`exec > >(tee ...) 2>&1`)
    assert r.returncode != 0 and "backup must be" in (r.stdout + r.stderr)
    assert not (tmp_path / "pwned").exists()


def test_the_recipe_reads_no_backup_environment_variable():
    text = (ROOT / "Justfile").read_text()
    up = text[text.index("\nup ") : text.index("\n# Connect")]
    assert 'up tag="" backup="":' in up
    code = "\n".join(ln for ln in up.splitlines() if not ln.lstrip().startswith("#"))
    assert "JUST_UP_BACKUP_PROVIDERS" not in code and "AKASH_PROVIDERS_BACKUP" not in code


def test_both_e2e_callers_pass_their_backup_tier_as_the_parameter():
    for f in ("just_akash/test_secrets_e2e.py", "just_akash/test_shell_e2e.py"):
        src = (ROOT / f).read_text()
        assert 'up_tag, up_backup = "", ",".join(backup)' in src, f
        assert '["just", "up", up_tag, up_backup]' in src, f
        assert "JUST_UP_BACKUP_PROVIDERS" not in src, f
