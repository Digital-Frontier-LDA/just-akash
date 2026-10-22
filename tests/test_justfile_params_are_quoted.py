"""Every recipe parameter is just-quoted once, never pasted raw into bash (just-akash-ahl).

t1q (#478) fixed `up`'s tag; the same raw `{{param}}` interpolation was in every other recipe, so a
quote in any argument ran as code. Each recipe now binds `_p={{quote(p)}}` and uses "${_p}"; the
variadic `smoke-providers *args` takes `[positional-arguments]` and passes "$@". The structural
test reads the real Justfile; the hostile-value tests run the real recipes with `uv` stubbed to
print its argv one per line.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOSTILE = 'x"; touch pwned; echo "'
HEADER = re.compile(r"^([a-z][a-z0-9_-]*) (.+):\s*$")


def _recipes() -> dict[str, tuple[list[str], str]]:
    lines = (ROOT / "Justfile").read_text().split("\n")
    out = {}
    for i, ln in enumerate(lines):
        m = HEADER.match(ln)
        if not m or ":=" in ln:
            continue
        params = re.findall(
            r"(?:^| )([*+]?\$?[a-z][a-z0-9_-]*)(?:=(?:\"[^\"]*\"|\S+))?", m.group(2)
        )
        body = []
        for b in lines[i + 1 :]:
            if not b.startswith("    "):
                break
            body.append(b)
        out[m.group(1)] = (params, "\n".join(body))
    return out


def test_the_survey_found_the_recipes():
    names = set(_recipes())
    assert {"up", "exec", "add-funds", "destroy", "tag", "inject", "smoke-providers"} <= names


@pytest.mark.parametrize("recipe", sorted(_recipes()))
def test_no_recipe_pastes_a_parameter_raw(recipe):
    params, body = _recipes()[recipe]
    for p in params:
        if p.startswith(("*", "+")):
            assert "{{" + p.lstrip("*+") + "}}" not in body, recipe
            continue
        assert "{{" + p + "}}" not in body, f"{recipe}: raw {{{{{p}}}}}"
        assert "{{quote(" + p + ")}}" in body, f"{recipe}: {p} is not bound via quote()"


def test_the_variadic_recipe_passes_its_arguments_positionally():
    text = (ROOT / "Justfile").read_text()
    assert "[positional-arguments]\nsmoke-providers *args:" in text
    assert 'just_akash.smoke_providers "$@"' in text


def _just(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess:
    if not shutil.which("just"):
        pytest.skip("just is not installed")
    shutil.copy(ROOT / "Justfile", tmp_path / "Justfile")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/sh\nfor a in "$@"; do printf "UVARG:[%s]\\n" "$a"; done\n')
    uv.chmod(0o755)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path)}
    return subprocess.run(
        ["just", *argv], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )


@pytest.mark.parametrize(
    "argv, flag",
    [
        (("exec", "123", HOSTILE), None),
        (("add-funds", HOSTILE, "123"), "--deposit"),
        (("destroy", HOSTILE), None),
        (("tag", "123", HOSTILE), "--name"),
        (("tag", HOSTILE, "n"), None),
        (("inject", "123", HOSTILE), "--env-file"),
        (("status", HOSTILE), None),
        (("update", HOSTILE), "--sdl"),
        (("deploy", "sdl.yaml", HOSTILE), "--image"),
        (("auto-topup", HOSTILE, "on"), "--dseq"),
        (("export-metrics", HOSTILE), None),
    ],
    ids=lambda v: v[0] if isinstance(v, tuple) else str(v),
)
def test_a_hostile_argument_runs_no_code_and_arrives_literally(tmp_path, argv, flag):
    r = _just(tmp_path, *argv)
    out = r.stdout + r.stderr
    assert not (tmp_path / "pwned").exists(), f"{argv[0]}: the argument executed as code\n{out}"
    assert r.returncode == 0, out
    assert HOSTILE in out.replace("UVARG:[--dseq=", "UVARG:["), out
    if flag:
        assert f"UVARG:[{flag}]\nUVARG:[{HOSTILE}]" in r.stdout, r.stdout


def test_smoke_providers_passes_each_argument_intact(tmp_path):
    r = _just(tmp_path, "smoke-providers", "--provider", "a b", HOSTILE)
    assert not (tmp_path / "pwned").exists()
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"UVARG:[--provider]\nUVARG:[a b]\nUVARG:[{HOSTILE}]" in r.stdout, r.stdout
