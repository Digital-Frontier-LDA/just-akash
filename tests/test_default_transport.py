"""
Wave 0 TDD stubs for Phase 10: Default Transport Switch and Fallback.

All tests (except test_exec_ssh_transport_flag_bypasses_lease_shell) are written
BEFORE the implementation. They define the required behaviors — each test must be
RED before Plan 10-02 runs. After Plan 10-02, all tests must be GREEN.

Requirements: TRNS-01, TRNS-03
"""

import sys
from unittest.mock import MagicMock, patch

from tests._creds import fake_api_key

# ── Helpers ──────────────────────────────────────────────────────────────────


def _mock_client(dseq="99999"):
    """Return a mock AkashConsoleAPI with lease data (lease-shell available)."""
    client = MagicMock()
    client.api_key = fake_api_key()
    client.list_deployments.return_value = [{"deployment": {"dseq": dseq, "state": "active"}}]
    client.get_deployment.return_value = {
        "deployment": {"dseq": dseq, "state": "active"},
        "leases": [
            {
                "provider": {
                    "hostUri": "https://provider.example.com:8443",
                },
                "status": {"services": {"web": {"available": 1}}},
            }
        ],
    }
    return client


def _mock_client_no_lease(dseq="99999"):
    """Return a mock client whose deployment has NO active lease (fallback trigger)."""
    client = MagicMock()
    client.api_key = fake_api_key()
    client.list_deployments.return_value = [{"deployment": {"dseq": dseq, "state": "active"}}]
    client.get_deployment.return_value = {
        "deployment": {"dseq": dseq, "state": "active"},
        "leases": [],  # No lease → validate() returns False
    }
    return client


def _run(monkeypatch, argv):
    """Run just-akash CLI with given argv; return exit code."""
    from just_akash import cli

    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    try:
        cli.main()
        return 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


# ── Default transport: argparse defaults ─────────────────────────────────────


class TestDefaultTransportArgparse:
    """Verify that argparse defaults are 'lease-shell' (TRNS-01)."""

    def test_exec_defaults_to_lease_shell(self, monkeypatch):
        """exec --transport default must be 'lease-shell', not 'ssh'."""
        client = _mock_client()
        with (
            patch("just_akash.api.AkashConsoleAPI", return_value=client),
            patch("just_akash.transport.lease_shell.LeaseShellTransport.prepare"),
            patch(
                "just_akash.transport.lease_shell.LeaseShellTransport.exec", return_value=0
            ) as mock_exec,
            patch(
                "just_akash.transport.lease_shell.LeaseShellTransport.validate", return_value=True
            ),
        ):
            _run(monkeypatch, ["just-akash", "exec", "--dseq", "99999", "echo hello"])
        assert mock_exec.called, (
            "exec command did not route to LeaseShellTransport.exec(). "
            "Expected default='lease-shell'."
        )

    def test_inject_defaults_to_lease_shell(self, monkeypatch):
        """inject --transport default must be 'lease-shell', not 'ssh'."""
        client = _mock_client()
        with (
            patch("just_akash.api.AkashConsoleAPI", return_value=client),
            patch("just_akash.transport.lease_shell.LeaseShellTransport.prepare"),
            patch("just_akash.transport.lease_shell.LeaseShellTransport.inject") as mock_inject,
            patch(
                "just_akash.transport.lease_shell.LeaseShellTransport.validate", return_value=True
            ),
        ):
            _run(monkeypatch, ["just-akash", "inject", "--dseq", "99999", "--env", "KEY=VALUE"])
        assert mock_inject.called, (
            "inject command did not route to LeaseShellTransport.inject(). "
            "Expected default='lease-shell'."
        )

    def test_connect_defaults_to_lease_shell(self, monkeypatch):
        """connect --transport default must be 'lease-shell', not 'ssh'."""
        client = _mock_client()
        with (
            patch("just_akash.api.AkashConsoleAPI", return_value=client),
            patch("just_akash.transport.lease_shell.LeaseShellTransport.prepare"),
            patch("just_akash.transport.lease_shell.LeaseShellTransport.connect") as mock_connect,
            patch(
                "just_akash.transport.lease_shell.LeaseShellTransport.validate", return_value=True
            ),
        ):
            _run(monkeypatch, ["just-akash", "connect", "--dseq", "99999"])
        assert mock_connect.called, (
            "connect command did not route to LeaseShellTransport.connect(). "
            "Expected default='lease-shell'."
        )


# ── Fallback: lease-shell unavailable → SSH ───────────────────────────────────


class TestFallbackToSSH:
    """Verify auto-fallback to SSH when validate() returns False (TRNS-03)."""

    def test_exec_fallback_to_ssh_when_validate_false(self, monkeypatch, capsys):
        """exec falls back to SSH and logs notice when lease-shell not available."""
        client = _mock_client_no_lease()
        mock_result = MagicMock()
        mock_result.returncode = 0
        with (
            patch("just_akash.api.AkashConsoleAPI", return_value=client),
            patch(
                "just_akash.transport.lease_shell.LeaseShellTransport.validate", return_value=False
            ),
            patch("just_akash.cli._require_ssh") as mock_require_ssh,
            patch("just_akash.cli.subprocess.run", return_value=mock_result) as mock_subprocess,
        ):
            mock_require_ssh.return_value = (
                {"host": "1.2.3.4", "port": "10022"},
                ["ssh", "-p", "10022", "root@1.2.3.4"],
            )
            _run(monkeypatch, ["just-akash", "exec", "--dseq", "99999", "echo hi"])
        captured = capsys.readouterr()
        assert "fallback" in captured.err.lower() or "ssh" in captured.err.lower(), (
            "Expected a fallback notice on stderr when lease-shell is unavailable. "
            f"Got stderr: {captured.err!r}"
        )
        assert mock_subprocess.called, "Expected SSH subprocess to be invoked on fallback"

    def test_inject_fallback_to_ssh_when_validate_false(self, monkeypatch, capsys):
        """inject falls back to SSH and logs notice when lease-shell not available."""
        client = _mock_client_no_lease()
        mock_result = MagicMock()
        mock_result.returncode = 0
        with (
            patch("just_akash.api.AkashConsoleAPI", return_value=client),
            patch(
                "just_akash.transport.lease_shell.LeaseShellTransport.validate", return_value=False
            ),
            patch("just_akash.cli._require_ssh") as mock_require_ssh,
            patch("just_akash.cli.subprocess.run", return_value=mock_result),
        ):
            mock_require_ssh.return_value = (
                {"host": "1.2.3.4", "port": "10022"},
                ["ssh", "-p", "10022", "root@1.2.3.4"],
            )
            _run(monkeypatch, ["just-akash", "inject", "--dseq", "99999", "--env", "KEY=VALUE"])
        captured = capsys.readouterr()
        assert "fallback" in captured.err.lower() or "ssh" in captured.err.lower(), (
            "Expected a fallback notice on stderr when lease-shell is unavailable. "
            f"Got stderr: {captured.err!r}"
        )

    def test_connect_fallback_to_ssh_when_validate_false(self, monkeypatch, capsys):
        """connect falls back to SSH (os.execvp) when lease-shell not available."""
        client = _mock_client_no_lease()
        with (
            patch("just_akash.api.AkashConsoleAPI", return_value=client),
            patch(
                "just_akash.transport.lease_shell.LeaseShellTransport.validate", return_value=False
            ),
            patch("just_akash.cli._require_ssh") as mock_require_ssh,
            patch("just_akash.cli.os.execvp") as mock_execvp,
        ):
            mock_require_ssh.return_value = (
                {"host": "1.2.3.4", "port": "10022"},
                ["ssh", "-p", "10022", "root@1.2.3.4"],
            )
            mock_execvp.side_effect = SystemExit(0)
            _run(monkeypatch, ["just-akash", "connect", "--dseq", "99999"])
        captured = capsys.readouterr()
        assert "fallback" in captured.err.lower() or "ssh" in captured.err.lower(), (
            "Expected a fallback notice on stderr when lease-shell is unavailable. "
            f"Got stderr: {captured.err!r}"
        )
        assert mock_execvp.called, "Expected os.execvp (SSH) to be invoked on fallback"


# ── Content: NO_SSH_MSG must not mention lease-shell as unsupported ────────────


class TestNoSshMsg:
    """NO_SSH_MSG must be accurate — lease-shell IS supported in v1.5 (TRNS-01)."""

    def test_no_ssh_msg_does_not_claim_lease_shell_unsupported(self):
        """NO_SSH_MSG must not contain 'does not support lease-shell'."""
        from just_akash.cli import NO_SSH_MSG

        assert "does not support lease-shell" not in NO_SSH_MSG, (
            "NO_SSH_MSG still contains outdated text claiming lease-shell is unsupported. "
            "Remove the false claim since lease-shell is the v1.5 default transport."
        )


# ── Explicit --transport ssh: bypass lease-shell entirely ────────────────────


class TestExplicitSSHTransport:
    """--transport ssh bypasses lease-shell entirely (ROADMAP SC-3)."""

    def test_exec_ssh_transport_flag_bypasses_lease_shell(self, monkeypatch):
        """--transport ssh routes directly to SSH subprocess, never calls LeaseShellTransport."""
        client = _mock_client()
        mock_result = MagicMock()
        mock_result.returncode = 0
        with (
            patch("just_akash.api.AkashConsoleAPI", return_value=client),
            patch(
                "just_akash.transport.lease_shell.LeaseShellTransport.validate"
            ) as mock_validate,
            patch("just_akash.cli._require_ssh") as mock_require_ssh,
            patch("just_akash.cli.subprocess.run", return_value=mock_result),
        ):
            mock_require_ssh.return_value = (
                {"host": "1.2.3.4", "port": "10022"},
                ["ssh", "-p", "10022", "root@1.2.3.4"],
            )
            _run(
                monkeypatch,
                ["just-akash", "exec", "--dseq", "99999", "--transport", "ssh", "echo hi"],
            )
        # validate() must NOT be called when --transport ssh is explicit
        assert not mock_validate.called, (
            "--transport ssh should bypass LeaseShellTransport entirely (no validate() call)"
        )
        assert mock_require_ssh.called, "--transport ssh should use _require_ssh()"
