"""
Integration tests: --transport flag wiring through CLI → Transport layer.

Tests the full chain: argparse → transport factory → transport method call,
with all I/O mocked. No network required.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from tests._creds import fake_api_key

# One throwaway key — a call, not a literal, so detect-secrets stays quiet and this
# file leaves .secrets.baseline (issue #38 item 3).
_KEY = fake_api_key()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(monkeypatch, args, env=None):
    """Run CLI with given args; return SystemExit code (or None if no exit)."""
    monkeypatch.setattr(sys, "argv", args)
    env = env or {"AKASH_API_KEY": _KEY}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from just_akash.cli import main

    try:
        main()
        return 0
    except SystemExit as e:
        return e.code


def _mock_client(deployment=None):
    """Return a mock AkashConsoleAPI with a deployment that has port 22."""
    if deployment is None:
        deployment = {
            "leases": [
                {
                    "status": {
                        "forwarded_ports": {
                            "app": [
                                {
                                    "port": 22,
                                    "host": "provider.akash.network",
                                    "externalPort": 32022,
                                }
                            ]
                        }
                    }
                }
            ]
        }
    client = MagicMock()
    client.api_key = _KEY
    client.get_deployment.return_value = deployment
    client.list_deployments.return_value = [{"dseq": "99999"}]
    return client


# ---------------------------------------------------------------------------
# --transport flag: accepted on exec, inject, connect
# ---------------------------------------------------------------------------


class TestTransportFlagParsed:
    """--transport flag is accepted with 'ssh' and 'lease-shell' values."""

    def test_exec_accepts_transport_ssh(self, monkeypatch):
        """exec --transport ssh parses without argparse error."""
        client = _mock_client()
        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch("just_akash.api.AkashConsoleAPI", return_value=client),
            patch(
                "just_akash.cli._require_ssh",
                return_value=({"host": "h", "port": 32022}, ["ssh", "-p", "32022", "root@h"]),
            ),
            patch("just_akash.cli.subprocess.run", return_value=mock_result),
        ):
            rc = _run(
                monkeypatch,
                ["just-akash", "exec", "--dseq", "99999", "--transport", "ssh", "echo hi"],
            )
        assert rc == 0

    def test_exec_accepts_transport_lease_shell(self, monkeypatch, capsys):
        """exec --transport lease-shell routes to LeaseShellTransport (Phase 7+)."""
        lease_shell_deployment = {
            "leases": [
                {
                    "provider": {"hostUri": "https://provider.example.com:8443"},
                    "status": {"services": {"web": {}}},
                }
            ]
        }
        client = _mock_client(deployment=lease_shell_deployment)
        with (
            patch("just_akash.api.AkashConsoleAPI", return_value=client),
            patch(
                "just_akash.transport.lease_shell.LeaseShellTransport.exec", return_value=0
            ) as mock_exec,
        ):
            rc = _run(
                monkeypatch,
                ["just-akash", "exec", "--dseq", "99999", "--transport", "lease-shell", "echo hi"],
            )
        assert mock_exec.called
        assert rc == 0

    def test_inject_accepts_transport_ssh(self, monkeypatch, tmp_path):
        """inject --transport ssh (default) completes normally."""
        env_file = tmp_path / "secrets.env"
        env_file.write_text("KEY=val\n")
        client = _mock_client()

        with (
            patch("just_akash.api.AkashConsoleAPI", return_value=client),
            patch(
                "just_akash.cli._require_ssh",
                return_value=({"host": "h", "port": 32022}, ["ssh", "-p", "32022", "root@h"]),
            ),
            patch("just_akash.cli.subprocess.run", return_value=MagicMock(returncode=0)),
        ):
            rc = _run(
                monkeypatch,
                [
                    "just-akash",
                    "inject",
                    "--dseq",
                    "99999",
                    "--env-file",
                    str(env_file),
                    "--transport",
                    "ssh",
                ],
            )
        assert rc == 0

    def test_inject_accepts_transport_lease_shell(self, monkeypatch, capsys):
        """inject --transport lease-shell succeeds with Phase 8 implementation."""
        lease_shell_deployment = {
            "leases": [
                {
                    "provider": {"hostUri": "https://provider.example.com:8443"},
                    "status": {"services": {"web": {}}},
                }
            ]
        }
        client = _mock_client(deployment=lease_shell_deployment)
        with (
            patch("just_akash.api.AkashConsoleAPI", return_value=client),
            patch(
                "just_akash.transport.lease_shell.LeaseShellTransport._exec_shell_command",
                return_value=0,
            ),
            patch(
                "just_akash.transport.lease_shell.LeaseShellTransport._exec_with_stdin_command",
                return_value=0,
            ),
        ):
            rc = _run(
                monkeypatch,
                [
                    "just-akash",
                    "inject",
                    "--dseq",
                    "99999",
                    "--env",
                    "SECRET=abc",
                    "--transport",
                    "lease-shell",
                ],
            )
        assert rc == 0

    def test_connect_accepts_transport_lease_shell(self, monkeypatch, capsys):
        """Phase 9: connect --transport lease-shell is routed to LeaseShellTransport."""
        deployment = {
            "leases": [
                {
                    "provider": {"hostUri": "https://provider.example.com:8443"},
                    "status": {"services": {"web": {}}},
                }
            ]
        }
        client = _mock_client(deployment=deployment)
        with (
            patch("just_akash.api.AkashConsoleAPI", return_value=client),
            patch("just_akash.transport.lease_shell.LeaseShellTransport.prepare"),
            patch("just_akash.transport.lease_shell.LeaseShellTransport.connect") as mock_connect,
        ):
            rc = _run(
                monkeypatch,
                ["just-akash", "connect", "--dseq", "99999", "--transport", "lease-shell"],
            )
        # connect() was called (not NotImplementedError stub)
        assert mock_connect.called
        assert rc == 0

    def test_exec_rejects_invalid_transport(self, monkeypatch, capsys):
        """exec --transport ftp should fail (invalid choice)."""
        rc = _run(
            monkeypatch, ["just-akash", "exec", "--dseq", "99999", "--transport", "ftp", "echo hi"]
        )
        assert rc != 0


# ---------------------------------------------------------------------------
# Default transport is SSH (zero regression)
# ---------------------------------------------------------------------------


class TestDefaultTransportIsSSH:
    """Omitting --transport behaves identically to --transport ssh."""

    def test_exec_default_uses_ssh(self, monkeypatch):
        client = _mock_client()
        mock_result = MagicMock()
        mock_result.returncode = 0
        ssh_info = {"host": "h", "port": 32022}
        ssh_cmd_base = ["ssh", "-p", "32022", "root@h"]

        with (
            patch("just_akash.api.AkashConsoleAPI", return_value=client),
            patch(
                "just_akash.cli._require_ssh", return_value=(ssh_info, ssh_cmd_base)
            ) as mock_ssh,
            patch("just_akash.cli.subprocess.run", return_value=mock_result),
        ):
            rc = _run(monkeypatch, ["just-akash", "exec", "--dseq", "99999", "echo hi"])
        assert rc == 0
        mock_ssh.assert_called_once()

    def test_exec_explicit_ssh_same_as_default(self, monkeypatch):
        """--transport ssh behaves the same as no --transport flag."""
        client = _mock_client()
        mock_result = MagicMock()
        mock_result.returncode = 5
        ssh_cmd_base = ["ssh", "-p", "32022", "root@h"]

        with (
            patch("just_akash.api.AkashConsoleAPI", return_value=client),
            patch(
                "just_akash.cli._require_ssh",
                return_value=({"host": "h", "port": 32022}, ssh_cmd_base),
            ),
            patch("just_akash.cli.subprocess.run", return_value=mock_result),
        ):
            rc = _run(
                monkeypatch,
                ["just-akash", "exec", "--dseq", "99999", "--transport", "ssh", "exit 5"],
            )
        assert rc == 5  # exit code propagated


# ---------------------------------------------------------------------------
# Transport factory: make_transport routing
# ---------------------------------------------------------------------------


class TestMakeTransportRouting:
    """make_transport correctly routes to SSH or LeaseShell transports."""

    def test_make_transport_ssh_returns_ssh_transport(self):
        from just_akash.transport import SSHTransport, make_transport

        t = make_transport("ssh", dseq="123", api_key=_KEY)
        assert isinstance(t, SSHTransport)

    def test_make_transport_lease_shell_returns_stub(self):
        from just_akash.transport import LeaseShellTransport, make_transport

        t = make_transport("lease-shell", dseq="123", api_key=_KEY)
        assert isinstance(t, LeaseShellTransport)

    def test_make_transport_with_deployment_kwarg(self):
        from just_akash.transport import make_transport

        deployment = {"leases": []}
        t = make_transport("ssh", dseq="123", api_key=_KEY, deployment=deployment)
        assert t._config.deployment == deployment  # type: ignore[attr-defined]

    def test_make_transport_with_service_name(self):
        from just_akash.transport import make_transport

        t = make_transport("lease-shell", dseq="123", api_key=_KEY, service_name="web")
        assert t._config.service_name == "web"  # type: ignore[attr-defined]

    def test_make_transport_with_console_url_override(self):
        from just_akash.transport import make_transport

        t = make_transport(
            "ssh", dseq="123", api_key=_KEY, console_url="https://custom.akash.example.com"
        )
        assert t._config.console_url == "https://custom.akash.example.com"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# LeaseShellTransport stub behaviour
# ---------------------------------------------------------------------------


class TestLeaseShellStubBehaviour:
    """Phase 7+: LeaseShellTransport implements prepare() + exec()."""

    def _t_no_deployment(self):
        """Lease shell transport without deployment data."""
        from just_akash.transport import LeaseShellTransport, TransportConfig

        return LeaseShellTransport(TransportConfig(dseq="1", api_key=_KEY))

    def _t_with_deployment(self):
        """Lease shell transport with valid deployment data."""
        from just_akash.transport import LeaseShellTransport, TransportConfig

        return LeaseShellTransport(
            TransportConfig(
                dseq="1",
                api_key=_KEY,
                deployment={
                    "leases": [
                        {
                            "provider": {"hostUri": "https://provider.example.com"},
                            "status": {"services": {"web": {}}},
                        }
                    ]
                },
            )
        )

    def test_prepare_raises_when_no_deployment(self):
        """Phase 7: prepare() needs deployment data."""
        with pytest.raises(RuntimeError, match="No leases found"):
            self._t_no_deployment().prepare()

    def test_prepare_works_with_deployment(self):
        """Phase 7: prepare() now works with valid deployment."""
        t = self._t_with_deployment()
        t.prepare()
        assert t._provider_host_uri is not None

    def test_exec_raises_when_no_deployment(self):
        """Phase 7: exec() needs deployment data."""
        with pytest.raises(RuntimeError, match="No leases found"):
            self._t_no_deployment().exec("echo hi")

    def test_lease_shell_inject_implemented(self):
        """Phase 8: inject() is implemented — no longer raises NotImplementedError."""
        t = self._t_with_deployment()
        t._provider_host_uri = "https://provider.example.com"
        t._service = "web"
        with (
            patch.object(t, "_exec_shell_command", return_value=0),
            patch.object(t, "_exec_with_stdin_command", return_value=0),
        ):
            t.inject("/tmp/x", "content")

    def test_connect_does_not_raise_not_implemented(self):
        """Phase 9: connect() is implemented — NotImplementedError stub is gone."""
        t = self._t_with_deployment()
        # connect() now requires a real TTY; patch dependencies to avoid TTY errors in CI
        with (
            patch("just_akash.transport.lease_shell.LeaseShellTransport._run_interactive_session"),
            patch("termios.tcgetattr", return_value=[]),
            patch("termios.tcsetattr"),
            patch("tty.setraw"),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            # Should not raise NotImplementedError
            t.connect()

    def test_validate_returns_false_without_hostUri(self):
        """validate() returns False when no hostUri."""
        assert self._t_no_deployment().validate() is False

    def test_validate_returns_true_with_hostUri(self):
        """validate() returns True when hostUri present."""
        assert self._t_with_deployment().validate() is True


# ---------------------------------------------------------------------------
# SSHTransport: zero regression from v1.4 behavior
# ---------------------------------------------------------------------------


class TestSSHTransportRegression:
    """SSHTransport delegates to api.py helpers unchanged from v1.4."""

    def _deployment_with_ssh(self):
        return {
            "leases": [
                {
                    "status": {
                        "forwarded_ports": {
                            "app": [
                                {
                                    "port": 22,
                                    "host": "provider.akash.network",
                                    "externalPort": 32022,
                                }
                            ]
                        }
                    }
                }
            ]
        }

    def test_ssh_transport_validate_with_port_22(self):
        from just_akash.transport import SSHTransport, TransportConfig

        config = TransportConfig(dseq="123", api_key=_KEY, deployment=self._deployment_with_ssh())
        assert SSHTransport(config).validate() is True

    def test_ssh_transport_validate_no_ports(self):
        from just_akash.transport import SSHTransport, TransportConfig

        config = TransportConfig(dseq="123", api_key=_KEY, deployment={})
        assert SSHTransport(config).validate() is False

    def test_ssh_transport_prepare_fails_without_ssh_port(self):
        from just_akash.transport import SSHTransport, TransportConfig

        config = TransportConfig(dseq="123", api_key=_KEY, deployment={})
        t = SSHTransport(config)
        with pytest.raises(RuntimeError):
            t.prepare()

    def test_ssh_transport_exec_calls_subprocess(self):
        from just_akash.transport import SSHTransport, TransportConfig

        config = TransportConfig(dseq="123", api_key=_KEY, deployment=self._deployment_with_ssh())
        t = SSHTransport(config)
        t._ssh_info = {"host": "provider.akash.network", "port": 32022}
        t._key_path = "/home/user/.ssh/id_ed25519"

        mock_result = MagicMock()
        mock_result.returncode = 0
        with (
            patch(
                "just_akash.transport.ssh._build_ssh_cmd", return_value=["ssh", "-p", "32022"]
            ) as mock_build,
            patch("just_akash.transport.ssh.subprocess.run", return_value=mock_result) as mock_run,
        ):
            rc = t.exec("echo hello")

        assert rc == 0
        mock_build.assert_called_once()
        mock_run.assert_called_once()
        # Verify command was appended
        call_args = mock_run.call_args[0][0]
        assert call_args[-1] == "echo hello"

    def test_ssh_transport_exec_propagates_exit_code(self):
        from just_akash.transport import SSHTransport, TransportConfig

        config = TransportConfig(dseq="123", api_key=_KEY)
        t = SSHTransport(config)
        t._ssh_info = {"host": "h", "port": 22}
        t._key_path = "/key"
        mock_result = MagicMock()
        mock_result.returncode = 127

        with (
            patch("just_akash.transport.ssh._build_ssh_cmd", return_value=["ssh"]),
            patch("just_akash.transport.ssh.subprocess.run", return_value=mock_result),
        ):
            assert t.exec("bad-command") == 127

    def test_ssh_transport_inject_writes_then_chmods(self):
        from just_akash.transport import SSHTransport, TransportConfig

        config = TransportConfig(dseq="123", api_key=_KEY)
        t = SSHTransport(config)
        t._ssh_info = {"host": "h", "port": 22}
        t._key_path = "/key"

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            m = MagicMock()
            m.returncode = 0
            return m

        with (
            patch("just_akash.transport.ssh._build_ssh_cmd", return_value=["ssh"]),
            patch("just_akash.transport.ssh.subprocess.run", side_effect=fake_run),
        ):
            t.inject("/home/user/.env", "KEY=val\n")

        # Expect: mkdir, cat >, chmod 600
        assert len(calls) == 3
        assert any("mkdir" in " ".join(c) for c in calls)
        assert any("cat >" in " ".join(c) for c in calls)
        assert any("chmod 600" in " ".join(c) for c in calls)


# ---------------------------------------------------------------------------
# TransportConfig defaults
# ---------------------------------------------------------------------------


class TestTransportConfigDefaults:
    def test_default_console_url(self):
        from just_akash.transport import TransportConfig

        c = TransportConfig(dseq="1", api_key=_KEY)
        assert c.console_url == "https://console-api.akash.network"

    def test_default_service_name_none(self):
        from just_akash.transport import TransportConfig

        assert TransportConfig(dseq="1", api_key=_KEY).service_name is None

    def test_default_ssh_key_path_none(self):
        from just_akash.transport import TransportConfig

        assert TransportConfig(dseq="1", api_key=_KEY).ssh_key_path is None

    def test_default_deployment_empty_dict(self):
        from just_akash.transport import TransportConfig

        c = TransportConfig(dseq="1", api_key=_KEY)
        assert c.deployment == {}


# ---------------------------------------------------------------------------
# Protocol constants (from PROTOCOL.md)
# ---------------------------------------------------------------------------


class TestProtocolConstants:
    """Verify the binary frame constants are documented and match expectations."""

    EXPECTED_CODES = {
        "stdout": 100,
        "stderr": 101,
        "result": 102,
        "failure": 103,
        "stdin": 104,
        "resize": 105,
    }

    def test_protocol_md_exists(self):
        import os

        assert os.path.exists("docs/PROTOCOL.md"), "docs/PROTOCOL.md must exist"

    def test_protocol_md_has_required_sections(self):
        with open("docs/PROTOCOL.md") as f:
            content = f.read()
        required = [
            "## Endpoint",
            "## Authentication",
            "## Message Schema",
            "## Connection Lifecycle",
        ]
        for section in required:
            assert section in content, f"Missing section: {section}"

    def test_protocol_md_documents_binary_codes(self):
        with open("docs/PROTOCOL.md") as f:
            content = f.read()
        for code in [100, 101, 102, 103, 104, 105]:
            assert str(code) in content, f"Frame code {code} not documented in PROTOCOL.md"

    def test_protocol_md_documents_query_params(self):
        with open("docs/PROTOCOL.md") as f:
            content = f.read()
        for param in ["service", "tty", "stdin", "cmd"]:
            assert param in content, f"Query param '{param}' not in PROTOCOL.md"
