"""Configure a credential-backed mirror of the qualified tenant runner image."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse


def read_pull_credentials(path: Path, *, username: str, password: str) -> tuple[str, str]:
    """Decrypt only the caller's pull bundle, without exporting credentials to the job."""
    if password or not username or not os.environ.get("SOPS_AGE_KEY"):
        raise ValueError("SOPS mode requires an age key, explicit username and no direct password")
    if path.is_symlink() or not path.is_file():
        raise ValueError("Pull bundle must be a regular file")
    child_env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SOPS_AGE_KEY")
        if key in os.environ
    }
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, bounded trusted SOPS binary
            ["sops", "decrypt", "--input-type", "dotenv", "--output-type", "dotenv", str(path)],  # noqa: S607
            env=child_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Pull bundle decryption failed") from exc
    if result.returncode or len(result.stdout) > 16384:
        raise ValueError("Pull bundle decryption failed")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if (
            not separator
            or key not in {"DOCKERHUB_PULL_USERNAME", "DOCKERHUB_PULL_TOKEN"}
            or key in values
            or not value
            or "\r" in value
        ):
            raise ValueError("Pull bundle contains invalid credentials")
        values[key] = value
    if set(values) != {"DOCKERHUB_PULL_USERNAME", "DOCKERHUB_PULL_TOKEN"}:
        raise ValueError("Pull bundle must contain exactly the reader credentials")
    if values["DOCKERHUB_PULL_USERNAME"] != username:
        raise ValueError("Pull identity does not match the configured registry username")
    token = values["DOCKERHUB_PULL_TOKEN"]
    # Actions command escaping prevents '%' in a token becoming a command escape.
    print("::add-mask::" + token.replace("%", "%25"))
    return username, token


def configure(path: Path, *, image: str, host: str, username: str, password: str) -> None:
    if not any((image, host, username, password)):
        return
    if path.is_symlink():
        raise ValueError("Runner template must be a regular task-owned file")
    text = path.read_text()
    matches = list(re.finditer(r"^([ \t]*)image:[ \t]+(\S+)[ \t]*$", text, re.MULTILINE))
    if len(matches) != 1:
        raise ValueError("Runner template must contain exactly one image")
    match = matches[0]
    original = match.group(2)
    chosen = image or original
    if image and not all((host, username, password)):
        raise ValueError("A private runner mirror requires complete registry credentials")
    if not re.fullmatch(
        r"[a-z0-9][a-z0-9.:-]*/(?:[a-z0-9][a-z0-9._-]*/)+[a-z0-9][a-z0-9._-]*@sha256:[0-9a-f]{64}",
        chosen,
    ):
        raise ValueError("Runner mirror requires a complete registry reference and digest")
    if chosen.rsplit("@", 1)[-1] != original.rsplit("@", 1)[-1]:
        raise ValueError("Runner mirror must preserve the qualified image digest")
    credential_lines = ""
    if any((host, username, password)):
        if not all((host, username, password)):
            raise ValueError(
                "Private registry credentials must include host, username and password"
            )
        parsed = urlparse(host)
        registry = chosen.split("/", 1)[0]
        allowed_hosts = {registry}
        if registry == "docker.io":
            allowed_hosts.add("index.docker.io")
        if (
            parsed.scheme != "https"
            or parsed.netloc not in allowed_hosts
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Registry credential host must match the runner mirror over HTTPS")
        if any("\n" in value or "\r" in value for value in (host, username, password)):
            raise ValueError(
                "Registry credentials must use single-line token or base64-key values"
            )
        indent = match.group(1)
        credential_lines = f"\n{indent}credentials:"
        for key, value in (("host", host), ("username", username), ("password", password)):
            credential_lines += f"\n{indent}  {key}: {json.dumps(value)}"
    if re.search(r"^\s*credentials:", text, re.MULTILINE):
        raise ValueError("Runner template already contains registry credentials")
    replacement = match.group(1) + "image: " + chosen + credential_lines
    updated = text[: match.start()] + replacement + text[match.end() :]
    # The template begins holding registry credentials here. Replace it atomically
    # with an owner-only file; never print its contents or credentials.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(updated)
            stream.flush()
            os.chmod(temporary, 0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdl", type=Path, required=True)
    parser.add_argument("--sops-env-file", type=Path)
    args = parser.parse_args()
    try:
        username = os.environ.get("RUNNER_REGISTRY_USERNAME", "")
        password = os.environ.get("RUNNER_REGISTRY_PASSWORD", "")
        if args.sops_env_file:
            username, password = read_pull_credentials(
                args.sops_env_file, username=username, password=password
            )
        configure(
            args.sdl,
            image=os.environ.get("RUNNER_IMAGE", ""),
            host=os.environ.get("RUNNER_REGISTRY_HOST", ""),
            username=username,
            password=password,
        )
    except (ValueError, OSError):
        print(
            "Runner image configuration failed; "
            "mirror identity or registry credentials were not verified"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
