"""
Abstract transport interface for just-akash shell commands.

All transports implement: prepare(), exec(), inject(), connect(), validate().
SSHTransport: wraps existing SSH subprocess logic (v1.4 behavior).
LeaseShellTransport: WebSocket-based (stub in Phase 6; implemented in Phase 7).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TransportConfig:
    """Configuration for a transport instance."""

    dseq: str
    api_key: str
    deployment: dict[str, Any] = field(default_factory=dict)
    console_url: str = "https://console-api.akash.network"
    provider_proxy_url: str = "https://console.akash.network/provider-proxy-mainnet"
    service_name: str | None = None
    ssh_key_path: str | None = None
    # Seconds of silence from the provider-proxy before an exec fails with a
    # diagnosis instead of blocking. Raise it for commands expected to stay quiet
    # for a long time.
    recv_timeout: float = 300.0
    # Once the command's exit-code (result) frame arrives, the provider-proxy can
    # still have a stdout frame in flight -- it does not guarantee the result frame
    # is last (issue #12, the "cold-stdout race"). After the exit code is in hand
    # we keep draining for at most this many seconds so that trailing output is not
    # dropped, returning early the instant the socket closes so a well-behaved
    # command is never delayed. The drain returns on close, so this only bounds the
    # pathological slow/no-close case; raise it only if telemetry shows late frames.
    result_grace_s: float = 0.25


class Transport(ABC):
    """
    Abstract base for shell transport mechanisms.

    Phase 6: SSHTransport (full) + LeaseShellTransport (stub).
    Phase 7+: LeaseShellTransport fully implemented.
    """

    @abstractmethod
    def prepare(self) -> None:
        """Validate transport can be used; raise RuntimeError if not."""
        ...

    @abstractmethod
    def exec(self, command: str) -> int:
        """Execute command remotely; return exit code (0 = success)."""
        ...

    @abstractmethod
    def inject(self, remote_path: str, content: str) -> None:
        """Write content to remote_path on the container."""
        ...

    @abstractmethod
    def connect(self) -> None:
        """Open interactive shell session (replaces current process via execvp)."""
        ...

    @abstractmethod
    def validate(self) -> bool:
        """Return True if transport can be used with current deployment."""
        ...
