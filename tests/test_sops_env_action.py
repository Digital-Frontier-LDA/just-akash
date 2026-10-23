"""The shared sops-env action keeps multi-line values whole and masks every line.

sops' dotenv output escapes a newline as a literal backslash-n, so the action decrypts to
JSON instead: a newline-separated AKASH_API_KEYS wallet list must reach
wallet_pool.configured_api_keys() as separate keys, not as one broken key. Every user of
the action (ci.yml, runner-teardown.yml's credentials-sops, the probes) depends on it.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _script():
    action = yaml.safe_load((ROOT / ".github/actions/sops-env/action.yml").read_text())
    return next(s for s in action["runs"]["steps"] if s["name"] == "Decrypt and export")["run"]


def _run(tmp_path, plain, *, age_key="AGE-SECRET-KEY-1STUB", raw=None):
    """Execute the real step with a stub sops that prints `plain` as its JSON output."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.txt"
    (tmp_path / "plain.json").write_text(json.dumps(plain) if raw is None else raw)
    (bindir / "sops").write_text(
        f'#!/bin/bash\necho "sops $*" >> {log}\ncat {tmp_path}/plain.json\n'
    )
    (bindir / "sops").chmod(0o755)
    (bindir / "jq").symlink_to(shutil.which("jq"))
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "SOPS_AGE_KEY": age_key,
        "SOPS_FILE": "secrets/ci.sops.env",
        "GITHUB_ENV": str(tmp_path / "github_env"),
    }
    proc = subprocess.run(["bash", "-c", _script()], env=env, capture_output=True, text=True)
    exported = (tmp_path / "github_env").read_text() if (tmp_path / "github_env").exists() else ""
    calls = log.read_text() if log.exists() else ""
    return proc, exported, calls


def _exported(exported):
    """Parse GITHUB_ENV heredoc entries the way the runner does."""
    values = {}
    lines = iter(exported.split("\n"))
    for line in lines:
        if "<<" not in line:
            continue
        key, delim = line.split("<<", 1)
        body = []
        for inner in lines:
            if inner == delim:
                break
            body.append(inner)
        values[key] = "\n".join(body)
    return values


def test_decrypts_dotenv_to_json_and_exports_every_value_unchanged(tmp_path):
    plain = {
        "AKASH_API_KEY": "ak-single",
        "SSH_PUBKEY": "ssh-ed25519 AAAAC3Nza fixture@ci",
        "AKASH_PROVIDERS": "akash1aaa,akash1bbb",
    }
    proc, exported, calls = _run(tmp_path, plain)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--decrypt --input-type dotenv --output-type json secrets/ci.sops.env" in calls
    assert _exported(exported) == plain
    assert "Loaded 3 secret(s) from secrets/ci.sops.env" in proc.stdout


@pytest.mark.parametrize("sep", ["\n", ",", ";", "\n\n", " , "])
def test_a_multi_key_wallet_pool_stays_separate_keys_each_masked(tmp_path, sep):
    keys = ["wallet-key-one", "wallet-key-two", "wallet-key-three"]
    proc, exported, _ = _run(tmp_path, {"AKASH_API_KEYS": sep.join(keys)})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    value = _exported(exported)["AKASH_API_KEYS"]
    assert "\\n" not in value
    # wallet_pool.configured_api_keys() splits exactly this way.
    assert [p.strip() for p in re.split(r"[\n,;]", value) if p.strip()] == keys
    for key in keys:
        assert f"::add-mask::{key}\n" in proc.stdout
        assert proc.stdout.count(key) == 1


def test_a_multi_line_value_is_masked_line_by_line_never_whole(tmp_path):
    proc, exported, _ = _run(tmp_path, {"DEPLOY_KEY": "line-one-secret\nline-two-secret"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _exported(exported)["DEPLOY_KEY"] == "line-one-secret\nline-two-secret"
    # One command per line; a single ::add-mask:: would print line two unmasked.
    assert "::add-mask::line-one-secret\n" in proc.stdout
    assert "::add-mask::line-two-secret\n" in proc.stdout
    assert proc.stdout.count("line-two-secret") == 1


def test_only_wallet_lists_are_split_on_commas(tmp_path):
    proc, _, _ = _run(tmp_path, {"AKASH_PROVIDERS": "akash1aaa,akash1bbb"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "::add-mask::akash1aaa,akash1bbb\n" in proc.stdout
    assert "::add-mask::akash1aaa\n" not in proc.stdout


def test_a_value_cannot_close_its_own_heredoc(tmp_path):
    # The old fixed delimiter let a value line equal to it inject further variables.
    plain = {"NOTE": "a\n__SOPS_EOF__\nINJECTED=1\nSOPS_EOF_x"}
    proc, exported, _ = _run(tmp_path, plain)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _exported(exported) == plain


def test_an_empty_age_key_refuses_before_decrypting(tmp_path):
    proc, exported, calls = _run(tmp_path, {"A": "b"}, age_key="")
    assert proc.returncode == 1
    assert "SOPS_AGE_KEY is empty" in proc.stdout
    assert calls == "" and exported == ""


def test_an_undecodable_decrypt_fails_the_step(tmp_path):
    proc, exported, _ = _run(tmp_path, {}, raw="not json\n")
    assert proc.returncode != 0
    assert exported == ""
