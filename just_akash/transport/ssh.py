"""SSH transport — exact v1.4 behavior wrapped in Transport interface."""

import os
import subprocess
from typing import Any

from just_akash.api import _build_ssh_cmd, _extract_ssh_info, _find_ssh_key

from .base import Transport, TransportConfig


class SSHTransport(Transport):
    """
    SSH-based shell transport.

    Wraps the SSH subprocess helpers from just_akash.api.
    Behavior is byte-for-byte identical to v1.4 CLI SSH commands.
    """

    def __init__(self, config: TransportConfig) -> None:
        self._config = config
        self._ssh_info: dict[str, Any] | None = None
        self._key_path: str | None = None

    def prepare(self) -> None:
        """Extract SSH info from deployment and locate SSH key."""
        self._ssh_info = _extract_ssh_info(self._config.deployment)
        if not self._ssh_info:
            from just_akash.cli import NO_SSH_MSG

            raise RuntimeError(NO_SSH_MSG)
        self._key_path = _find_ssh_key(self._config.ssh_key_path or "")
        if not self._key_path:
            raise RuntimeError("No SSH key found. Specify with --key")

    def exec(self, command: str) -> int:
        """Run command via SSH subprocess; return exit code."""
        assert self._ssh_info is not None, "Call prepare() first"  # noqa: S101 type-narrowing
        assert self._key_path is not None, "Call prepare() first"  # noqa: S101 type-narrowing
        ssh_cmd = _build_ssh_cmd(self._ssh_info, self._key_path)
        ssh_cmd.append(command)
        result = subprocess.run(ssh_cmd, text=True)
        return result.returncode

    def inject(self, remote_path: str, content: str) -> None:
        """Inject secrets via SSH (mkdir, cat, chmod)."""
        assert self._ssh_info is not None, "Call prepare() first"  # noqa: S101 type-narrowing
        assert self._key_path is not None, "Call prepare() first"  # noqa: S101 type-narrowing
        ssh_cmd = _build_ssh_cmd(self._ssh_info, self._key_path)
        # mkdir -p
        subprocess.run(
            ssh_cmd + [f"mkdir -p $(dirname {remote_path})"],
            capture_output=True,
            text=True,
            check=True,
        )
        # write content
        result = subprocess.run(
            ssh_cmd + [f"cat > {remote_path}"], input=content, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to write secrets: {result.stderr.strip()}")
        # chmod 600
        subprocess.run(ssh_cmd + [f"chmod 600 {remote_path}"], capture_output=True, text=True)

    def connect(self) -> None:
        """Interactive SSH shell (replaces process via os.execvp — never returns)."""
        assert self._ssh_info is not None, "Call prepare() first"  # noqa: S101 type-narrowing
        assert self._key_path is not None, "Call prepare() first"  # noqa: S101 type-narrowing
        ssh_cmd = _build_ssh_cmd(self._ssh_info, self._key_path)
        os.execvp("ssh", ssh_cmd)

    def validate(self) -> bool:
        """Return True if deployment has SSH port 22 configured."""
        return _extract_ssh_info(self._config.deployment) is not None
