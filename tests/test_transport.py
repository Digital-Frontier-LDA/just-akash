"""Unit tests for just_akash.transport package."""

from unittest.mock import MagicMock, patch

import pytest

from just_akash.transport import (
    LeaseShellTransport,
    SSHTransport,
    Transport,
    TransportConfig,
    make_transport,
)

# --- Transport ABC ---


class TestTransportABC:
    def test_transport_cannot_be_instantiated_directly(self):
        """Transport is abstract — direct instantiation must raise TypeError."""
        with pytest.raises(TypeError):
            Transport()  # type: ignore[abstract]

    def test_transport_config_dataclass(self):
        config = TransportConfig(dseq="123456", api_key="key-abc")
        assert config.dseq == "123456"
        assert config.api_key == "key-abc"
        assert config.console_url == "https://console-api.akash.network"
        assert config.service_name is None
        assert config.ssh_key_path is None


# --- SSHTransport ---


class TestSSHTransport:
    def _make_deployment_with_ssh(self):
        """Minimal deployment dict that _extract_ssh_info can parse."""
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

    def test_validate_returns_true_when_ssh_port_present(self):
        dep = self._make_deployment_with_ssh()
        config = TransportConfig(dseq="123", api_key="key", deployment=dep)
        t = SSHTransport(config)
        assert t.validate() is True

    def test_validate_returns_false_when_no_ssh(self):
        config = TransportConfig(dseq="123", api_key="key", deployment={})
        t = SSHTransport(config)
        assert t.validate() is False

    def test_prepare_raises_when_no_ssh_port(self):
        config = TransportConfig(dseq="123", api_key="key", deployment={})
        t = SSHTransport(config)
        with pytest.raises(RuntimeError):
            t.prepare()

    def test_prepare_raises_when_no_ssh_key(self):
        dep = self._make_deployment_with_ssh()
        config = TransportConfig(dseq="123", api_key="key", deployment=dep)
        t = SSHTransport(config)
        with (
            patch("just_akash.transport.ssh._find_ssh_key", return_value=None),
            pytest.raises(RuntimeError, match="No SSH key"),
        ):
            t.prepare()

    def test_exec_runs_command_and_returns_exit_code(self):
        dep = self._make_deployment_with_ssh()
        config = TransportConfig(dseq="123", api_key="key", deployment=dep)
        t = SSHTransport(config)
        mock_ssh_info = {"host": "provider.akash.network", "port": 32022}
        t._ssh_info = mock_ssh_info
        t._key_path = "/home/user/.ssh/id_ed25519"

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch("just_akash.transport.ssh._build_ssh_cmd", return_value=["ssh", "-p", "32022"]),
            patch("just_akash.transport.ssh.subprocess.run", return_value=mock_result),
        ):
            rc = t.exec("echo hello")
        assert rc == 0

    def test_exec_propagates_nonzero_exit_code(self):
        config = TransportConfig(dseq="123", api_key="key")
        t = SSHTransport(config)
        t._ssh_info = {"host": "h", "port": 22}
        t._key_path = "/key"
        mock_result = MagicMock()
        mock_result.returncode = 42

        with (
            patch("just_akash.transport.ssh._build_ssh_cmd", return_value=["ssh"]),
            patch("just_akash.transport.ssh.subprocess.run", return_value=mock_result),
        ):
            rc = t.exec("exit 42")
        assert rc == 42

    def test_inject_shlex_quotes_remote_path(self):
        """A remote path containing shell metacharacters must be shlex-quoted in
        every command sent over SSH (mkdir/cat/chmod) — never interpolated raw."""
        import shlex

        config = TransportConfig(dseq="123", api_key="key")
        t = SSHTransport(config)
        t._ssh_info = {"host": "h", "port": 22}
        t._key_path = "/key"
        dangerous = "/tmp/x; rm -rf ~"
        quoted = shlex.quote(dangerous)

        mock_result = MagicMock()
        mock_result.returncode = 0
        with (
            patch("just_akash.transport.ssh._build_ssh_cmd", return_value=["ssh"]),
            patch("just_akash.transport.ssh.subprocess.run", return_value=mock_result) as mock_run,
        ):
            t.inject(dangerous, "secret-content")

        # Every invocation's final argument is the remote command string; the
        # raw path must never appear unquoted in any of them.
        sent_cmds = [call.args[0][-1] for call in mock_run.call_args_list]
        assert sent_cmds, "expected at least one SSH command"
        for cmd in sent_cmds:
            assert quoted in cmd
            assert dangerous not in cmd.replace(quoted, "")
        # The mkdir command must also quote the $(dirname ...) substitution, so a
        # directory path containing spaces stays a single argument to mkdir.
        mkdir_cmd = sent_cmds[0]
        assert f'"$(dirname {quoted})"' in mkdir_cmd

    def test_inject_fails_closed_when_chmod_fails(self):
        """If `chmod 600` fails, inject() must raise — never report success with
        secret material left at weaker-than-intended permissions."""
        config = TransportConfig(dseq="123", api_key="key")
        t = SSHTransport(config)
        t._ssh_info = {"host": "h", "port": 22}
        t._key_path = "/key"

        ok = MagicMock(returncode=0, stderr="")
        chmod_fail = MagicMock(returncode=1, stderr="chmod: Operation not permitted")
        # mkdir, cat, chmod — in order.
        with (
            patch("just_akash.transport.ssh._build_ssh_cmd", return_value=["ssh"]),
            patch(
                "just_akash.transport.ssh.subprocess.run",
                side_effect=[ok, ok, chmod_fail],
            ),
            pytest.raises(RuntimeError, match="chmod 600"),
        ):
            t.inject("/tmp/secret", "content")


# --- LeaseShellTransport (Phase 7+) ---


class TestLeaseShellTransportStub:
    def _stub_no_deployment(self):
        """Lease shell transport without deployment data."""
        config = TransportConfig(dseq="123", api_key="key")
        return LeaseShellTransport(config)

    def _stub_with_deployment(self):
        """Lease shell transport with minimal valid deployment data."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment={
                "leases": [
                    {
                        "provider": {"hostUri": "https://provider.example.com"},
                        "status": {"services": {"web": {}}},
                    }
                ]
            },
        )
        return LeaseShellTransport(config)

    def test_prepare_raises_when_no_deployment_data(self):
        """Phase 7: prepare() needs deployment data to work."""
        with pytest.raises(RuntimeError, match="No leases found"):
            self._stub_no_deployment().prepare()

    def test_prepare_succeeds_with_deployment(self):
        """Phase 7: prepare() works with valid deployment data."""
        t = self._stub_with_deployment()
        t.prepare()
        assert t._provider_host_uri is not None
        assert t._service is not None

    def test_exec_raises_when_no_deployment(self):
        """Phase 7: exec() needs deployment data."""
        with pytest.raises(RuntimeError, match="No leases found"):
            self._stub_no_deployment().exec("echo hi")

    def test_lease_shell_inject_not_a_stub(self):
        """Phase 8: inject() is implemented (no longer NotImplementedError)."""
        t = self._stub_with_deployment()
        t._provider_host_uri = "https://p:8443"
        t._service = "web"
        with (
            patch.object(t, "exec", side_effect=[0, 0]),
            patch.object(t, "_exec_shell_command", return_value=0),
            patch.object(t, "_exec_with_stdin_command", return_value=0),
        ):
            t.inject("/tmp/x", "content")

    def test_lease_shell_connect_opens_session(self):
        """Phase 9: connect() is implemented — no longer raises NotImplementedError."""
        t = self._stub_with_deployment()
        t._provider_host_uri = "https://provider.example.com"
        t._service = "web"

        with (
            patch("just_akash.transport.lease_shell.connect"),
            patch("termios.tcgetattr", return_value=[]),
            patch("termios.tcsetattr"),
            patch("tty.setraw"),
            patch("sys.stdin") as mock_stdin,
            patch.object(t, "_run_interactive_session"),
        ):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            # Should not raise NotImplementedError
            t.connect()
        # If we get here without NotImplementedError, connect() is real

    def test_validate_returns_false_without_deployment(self):
        """validate() returns False when no hostUri."""
        assert self._stub_no_deployment().validate() is False

    def test_validate_returns_true_with_deployment(self):
        """validate() returns True when hostUri present."""
        assert self._stub_with_deployment().validate() is True

    def test_get_proxy_ws_url_preserves_http_in_path(self):
        """_get_proxy_ws_url must only replace scheme, not 'http://' in path."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            provider_proxy_url="https://proxy.example.com/relay/http://backend",
        )
        t = LeaseShellTransport(config)
        url = t._get_proxy_ws_url()
        assert url.startswith("wss://")
        assert "http://backend" in url

    def test_get_proxy_ws_url_accepts_a_wss_override(self):
        """A wss:// override is a TLS scheme and must be preserved as-is."""
        config = TransportConfig(
            dseq="123", api_key="key", provider_proxy_url="wss://proxy.example.com/relay"
        )
        assert LeaseShellTransport(config)._get_proxy_ws_url().startswith("wss://")

    def test_get_proxy_ws_url_rejects_a_plaintext_endpoint(self):
        """connect() always supplies a TLS context, so a plaintext proxy endpoint
        can't work — reject it with a clear error rather than fail opaquely deep in
        the websockets client. The secret-bearing exec/inject paths must never fall
        back to an unencrypted socket. (Hardening from review, PR #64.)"""
        for scheme in ("http", "ws"):
            config = TransportConfig(
                dseq="123", api_key="key", provider_proxy_url=f"{scheme}://proxy.example.com/relay"
            )
            with pytest.raises(RuntimeError, match="TLS scheme"):
                LeaseShellTransport(config)._get_proxy_ws_url()


# --- make_transport factory ---


class TestMakeTransport:
    def test_makes_ssh_transport(self):
        t = make_transport("ssh", dseq="123", api_key="key")
        assert isinstance(t, SSHTransport)

    def test_makes_lease_shell_transport(self):
        t = make_transport("lease-shell", dseq="123", api_key="key")
        assert isinstance(t, LeaseShellTransport)

    def test_raises_for_unknown_transport(self):
        with pytest.raises(ValueError, match="Unknown transport"):
            make_transport("ftp", dseq="123", api_key="key")
