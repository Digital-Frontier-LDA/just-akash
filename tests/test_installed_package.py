"""A genuine wheel installed in an isolated child, independent of test checkout layout."""

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from just_akash import guardian_legacy_lease_backend as backend

CHILD_CHECK = r"""
import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import sys
from pathlib import Path

import just_akash

manifest = json.loads(Path(sys.argv[1]).read_bytes())
assert len(manifest) == 60
package = Path(just_akash.__file__).resolve().parent
assert package.parent.name == "site-packages"
assert package.is_relative_to(Path(sys.prefix).resolve())
actual = {str(p.relative_to(package.parent)) for p in package.rglob("*") if p.is_file()}
assert actual == set(manifest)
for name, digest in manifest.items():
    installed = package.parent / name
    assert hashlib.sha256(installed.read_bytes()).hexdigest() == digest

distribution = importlib.metadata.distribution("just-akash")
assert distribution.version == "1.50.0"
assert distribution.metadata["Requires-Python"] == ">=3.10"
assert any("akash-lease-core" in requirement and "/v0.18.0/" in requirement and
           "sha256=7d9ccb117b1cf2f30ebea17018371902ed063daa6c087e2c64e109fe00788070" in requirement
           for requirement in distribution.requires or [])
record = distribution.read_text("RECORD")
assert record is not None
records = list(csv.reader(io.StringIO(record)))
assert len({name for name, digest, size in records}) == len(records)
record_names = {name for name, digest, size in records}
assert set(manifest) <= record_names
hashed = 0
for name, encoded, size in records:
    path = Path(distribution.locate_file(name)).resolve()
    assert path.is_relative_to(Path(sys.prefix).resolve())
    if not encoded:
        assert name.endswith(".dist-info/RECORD") and size == ""
        continue
    algorithm, expected = encoded.split("=", 1)
    assert algorithm == "sha256"
    raw = path.read_bytes()
    assert str(len(raw)) == size
    assert base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=") == expected
    hashed += 1
assert hashed >= len(manifest)
print(json.dumps({"package_members":len(manifest), "hashed_RECORD_members":hashed,
                  "wheel_isolated_site_packages":True, "version":distribution.version}))
"""


def _tool(argv, *, cwd, env):
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=90)
    assert result.returncode == 0, (
        "Isolated offline wheel operation failed (no credential logs emitted)"
    )
    return result.stdout


@pytest.fixture(scope="module")
def isolated_wheel(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("actual-sdk-wheel")
    repository = Path(__file__).resolve().parents[1]
    uv = shutil.which("uv")
    assert uv is not None, "Normal CI uses uv; this packaging contract requires the same real tool"
    # No credentials or arbitrary inherited index configuration enter either child.
    env = {
        key: os.environ[key]
        for key in ("PATH", "UV_CACHE_DIR", "UV_FIND_LINKS")
        if key in os.environ
    }
    env.update(
        TMPDIR=str(workspace),
        UV_KEYRING_PROVIDER="disabled",
        UV_AUTH_STORE="plaintext",
        UV_CREDENTIALS_DIR=str(workspace / "empty-credentials"),
        UV_PYTHON_DOWNLOADS="never",
        UV_HTTP_RETRIES="0",
        UV_OFFLINE="1",
        NO_COLOR="1",
        XDG_CONFIG_HOME=str(workspace / "empty-config"),
        XDG_DATA_HOME=str(workspace / "empty-data"),
    )
    empty = workspace / "empty.netrc"
    empty.write_bytes(b"")
    empty.chmod(0o600)
    env["NETRC"] = str(empty)
    # Reuse the ordinary public build cache populated by normal uv sync --dev.
    # Local qualification supplies its own external cache and retained public wheels.
    if "UV_CACHE_DIR" not in env:
        env["UV_CACHE_DIR"] = (
            _tool([uv, "--no-config", "cache", "dir"], cwd=workspace, env=env).decode().strip()
        )
    output = workspace / "wheel"
    _tool(
        [
            uv,
            "--no-config",
            "build",
            "--wheel",
            "--offline",
            "--python",
            sys.executable,
            "--out-dir",
            str(output),
            str(repository),
        ],
        cwd=workspace,
        env=env,
    )
    wheels = list(output.glob("just_akash-*.whl"))
    assert len(wheels) == 1
    child = workspace / "child"
    _tool(
        [uv, "--no-config", "venv", "--offline", "--python", sys.executable, str(child)],
        cwd=workspace,
        env=env,
    )
    python = child / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _tool(
        [
            uv,
            "--no-config",
            "pip",
            "install",
            "--offline",
            "--no-index",
            "--no-deps",
            "--python",
            str(python),
            str(wheels[0]),
        ],
        cwd=workspace,
        env=env,
    )
    result = _tool(
        [
            str(python),
            "-I",
            "-B",
            "-c",
            CHILD_CHECK,
            str(Path(__file__).with_name("package_manifest.json")),
        ],
        cwd=workspace,
        env=env,
    )
    proof = json.loads(result)
    (workspace / "isolated-wheel-proof.json").write_text(json.dumps(proof, sort_keys=True))
    return proof


def test_actual_normal_installed_package_bytes_match_candidate(isolated_wheel):
    assert isolated_wheel["wheel_isolated_site_packages"] is True
    assert isolated_wheel["package_members"] == 60
    assert isolated_wheel["hashed_RECORD_members"] >= 60
    assert importlib.metadata.version("akash-lease-core") == "0.18.0"


def test_actual_backend_fixed_private_helper_is_packaged_and_hash_joined():
    package = Path(backend.__file__).parent
    helper = package / "_hosted" / "mint_owner_status_delegation.py"
    raw = backend._read_source(helper, backend.HOSTED_MINT_SHA256)
    assert len(raw) == 8404
    assert (
        backend.HOSTED_MINT_SHA256
        == "80ceb0c5089a8cf583d24e436fdbb46cdcd51a3d8c7d475541ced4d11994acc9"
    )
