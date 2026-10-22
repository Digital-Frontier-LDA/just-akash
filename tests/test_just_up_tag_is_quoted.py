"""`just up`'s `tag` is just-quoted, never raw-interpolated into the recipe's bash (just-akash-t1q).

`{{tag}}` was pasted into the script text in three places, so a tag holding a quote ran as code:
`just up 'x"; touch pwned; "'` executed `touch`. The recipe now binds `_tag={{quote(tag)}}` once
and uses "$_tag". The recipe runs for real here, with `uv` stubbed: `deploy` prints a DSEQ so the
tag step runs, and every call prints its argv.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOSTILE = 'x"; touch pwned; echo "'


def _up(tmp_path: Path, tag: str) -> subprocess.CompletedProcess:
    if not shutil.which("just"):
        pytest.skip("just is not installed")
    shutil.copy(ROOT / "Justfile", tmp_path / "Justfile")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        '#!/bin/sh\nfor a in "$@"; do printf "UVARG:[%s]\\n" "$a"; done\n'
        'case " $* " in *" deploy "*) echo "DSEQ: 4242" ;; esac\n'
    )
    uv.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path)}
    return subprocess.run(
        ["just", "up", tag, ""], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )


def test_a_hostile_tag_runs_no_code_and_is_passed_literally(tmp_path):
    r = _up(tmp_path, HOSTILE)
    assert not (tmp_path / "pwned").exists(), "the tag executed as shell code"
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"UVARG:[{HOSTILE}]" in r.stdout, r.stdout


def test_a_plain_tag_still_tags_the_lease(tmp_path):
    r = _up(tmp_path, "nightly-1")
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "UVARG:[tag]" in out and "UVARG:[4242]" in out and "UVARG:[nightly-1]" in out, out


def test_no_tag_skips_the_tag_step(tmp_path):
    r = _up(tmp_path, "")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "UVARG:[tag]" not in r.stdout


def test_the_recipe_never_interpolates_tag_raw():
    text = (ROOT / "Justfile").read_text()
    up = text[text.index("\nup ") : text.index("\n# Connect")]
    assert "{{tag}}" not in up
    assert "{{quote(tag)}}" in up
