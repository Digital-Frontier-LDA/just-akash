"""Unit tests for LeaseShellTransport.inject() and _exec_with_stdin() — Phase 8."""

import base64
import json
import shlex
from unittest.mock import patch

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from just_akash.transport import LeaseShellTransport, TransportConfig
from just_akash.transport.lease_shell import _FRAME_STDIN
from tests._creds import fake_api_key, fake_secret


def _make_transport() -> LeaseShellTransport:
    config = TransportConfig(
        dseq="123",
        api_key=fake_api_key(),
        deployment={
            "leases": [
                {
                    "provider": {"hostUri": "https://provider.example.com:8443"},
                    "status": {"services": {"web": {"ready_replicas": 1, "total": 1}}},
                }
            ]
        },
    )
    t = LeaseShellTransport(config)
    t._provider_host_uri = "https://provider.example.com:8443"
    t._service = "web"
    return t


class FakeWebSocket:
    def __init__(self, frames):
        self._frames = iter(frames)
        self.sent_messages: list = []

    def recv(self, timeout=None):
        try:
            return next(self._frames)
        except StopIteration:
            from websockets.exceptions import ConnectionClosedOK

            raise ConnectionClosedOK(None, None) from None

    def send(self, data):
        self.sent_messages.append(data)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class TestExecWithStdin:
    def test_exec_with_stdin_sends_stdin_frame(self):
        t = _make_transport()
        stdin_data = b"aGVsbG8="
        frames = [bytes([102]) + (0).to_bytes(4, "little")]

        with (
            patch.object(t, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            fake_ws = FakeWebSocket(frames)
            mock_connect.return_value = fake_ws
            exit_code = t._exec_with_stdin("base64 -d > /tmp/f", stdin_data)

        assert exit_code == 0
        assert len(fake_ws.sent_messages) == 2

        connect_msg = json.loads(fake_ws.sent_messages[0])
        assert "stdin=1" in connect_msg["url"]

        stdin_msg = json.loads(fake_ws.sent_messages[1])
        assert stdin_msg["type"] == "websocket"
        decoded_frame = base64.b64decode(stdin_msg["data"])
        assert decoded_frame[0] == _FRAME_STDIN
        assert decoded_frame[1:] == stdin_data

    def test_exec_with_stdin_returns_exit_code(self):
        t = _make_transport()
        frames = [bytes([102]) + (1).to_bytes(4, "little")]

        with (
            patch.object(t, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            mock_connect.return_value = FakeWebSocket(frames)
            exit_code = t._exec_with_stdin("cat", b"data")

        assert exit_code == 1

    def test_exec_with_stdin_reconnects_on_auth_expiry(self):
        t = _make_transport()

        class FakeWSExpired:
            def recv(self, timeout=None):
                raise ConnectionClosedError(rcvd=Close(code=4001, reason=""), sent=None)

            def send(self, data):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        fake_ws_ok = FakeWebSocket([bytes([102]) + (0).to_bytes(4, "little")])

        with (
            patch.object(t, "_fetch_jwt", return_value="jwt") as mock_jwt,
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            mock_connect.side_effect = [FakeWSExpired(), fake_ws_ok]
            exit_code = t._exec_with_stdin("cmd", b"data")

        assert exit_code == 0
        assert mock_jwt.call_count == 2

    def test_exec_with_stdin_raises_after_max_reconnects(self):
        t = _make_transport()

        class FakeWSExpired:
            def recv(self, timeout=None):
                raise ConnectionClosedError(rcvd=Close(code=4001, reason=""), sent=None)

            def send(self, data):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        with (
            patch.object(t, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            mock_connect.side_effect = [FakeWSExpired() for _ in range(10)]
            with pytest.raises(RuntimeError, match="Failed to re-authenticate"):
                t._exec_with_stdin("cmd", b"data")

    def test_exec_with_stdin_empty_data(self):
        t = _make_transport()
        frames = [bytes([102]) + (0).to_bytes(4, "little")]

        with (
            patch.object(t, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            fake_ws = FakeWebSocket(frames)
            mock_connect.return_value = fake_ws
            exit_code = t._exec_with_stdin("cat", b"")

        assert exit_code == 0
        stdin_msg = json.loads(fake_ws.sent_messages[1])
        decoded_frame = base64.b64decode(stdin_msg["data"])
        assert decoded_frame == bytes([_FRAME_STDIN])


class TestLeaseShellTransportInject:
    def test_inject_creates_parent_directory(self):
        t = _make_transport()
        with patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd:
            t.inject("/tmp/secrets.env", "KEY=value")

        mkdir_call = mock_cmd.call_args_list[0][0][0]
        assert "mkdir -p" in mkdir_call
        assert shlex.quote("/tmp") in mkdir_call

    def test_inject_writes_via_exec_shell_command(self):
        t = _make_transport()
        content = "SECRET=abc123"
        expected_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")

        with patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd:
            t.inject("/tmp/secrets.env", content)

        # Second call is the write (first is mkdir, third is chmod)
        cmd = mock_cmd.call_args_list[1][0][0]
        assert expected_b64 in cmd
        assert "base64 -d" in cmd
        assert "/tmp/secrets.env" in cmd
        # The write must umask 077 so the file is created 0600 — no window where the
        # secret is world-/group-readable before chmod. (CodeRabbit, PR #66.)
        assert "umask 077" in cmd
        assert cmd.index("umask 077") < cmd.index("base64 -d")

    def test_inject_sets_file_permissions(self):
        t = _make_transport()
        with patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd:
            t.inject("/tmp/secrets.env", "KEY=value")

        chmod_call = mock_cmd.call_args_list[2][0][0]
        assert "chmod 600" in chmod_call
        assert shlex.quote("/tmp/secrets.env") in chmod_call

    def test_inject_uses_exec_shell_command_for_all_steps(self):
        t = _make_transport()
        with patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd:
            t.inject("/tmp/secrets.env", "KEY=value")

        assert mock_cmd.call_count == 3
        assert "mkdir -p" in mock_cmd.call_args_list[0][0][0]
        assert "base64 -d" in mock_cmd.call_args_list[1][0][0]
        assert "chmod 600" in mock_cmd.call_args_list[2][0][0]

    def test_inject_raises_on_mkdir_failure(self):
        t = _make_transport()
        with (
            patch.object(t, "_exec_shell_command", return_value=1),
            pytest.raises(RuntimeError, match="Failed to create directory"),
        ):
            t.inject("/tmp/secrets.env", "KEY=value")

    def test_inject_raises_on_write_failure(self):
        t = _make_transport()
        with (
            patch.object(t, "_exec_shell_command", side_effect=[0, 1]),
            pytest.raises(RuntimeError, match="Failed to write"),
        ):
            t.inject("/tmp/secrets.env", "KEY=value")

    def test_inject_raises_on_chmod_failure(self):
        t = _make_transport()
        with (
            patch.object(t, "_exec_shell_command", side_effect=[0, 0, 1]),
            pytest.raises(RuntimeError, match="Failed to set permissions"),
        ):
            t.inject("/tmp/secrets.env", "KEY=value")

    def test_inject_escapes_path_with_shell_metacharacters(self):
        t = _make_transport()
        dangerous_path = "/tmp/test'; rm -rf /"
        with patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd:
            t.inject(dangerous_path, "content")

        write_cmd = mock_cmd.call_args_list[1][0][0]
        assert "base64 -d" in write_cmd
        assert shlex.quote(dangerous_path) in write_cmd

    def test_inject_handles_multiline_content(self):
        t = _make_transport()
        content = "LINE1=val1\nLINE2=val2\n"
        expected_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")

        with patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd:
            t.inject("/tmp/multiline.env", content)

        write_cmd = mock_cmd.call_args_list[1][0][0]
        assert expected_b64 in write_cmd

    def test_inject_secret_value_not_in_plaintext_commands(self):
        t = _make_transport()
        secret_value = fake_secret("pw")

        with patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd:
            t.inject("/tmp/secret.env", f"PASSWORD={secret_value}")

        # no command should contain the raw secret value
        for i, call_args in enumerate(mock_cmd.call_args_list):
            cmd = call_args[0][0]
            assert secret_value not in cmd, f"Secret leaked in command[{i}]: {cmd!r}"

    def test_inject_calls_prepare_if_not_configured(self):
        t = _make_transport()
        t._provider_host_uri = None
        t._service = None

        with (
            patch.object(t, "prepare") as mock_prepare,
            patch.object(t, "_exec_shell_command", return_value=0),
        ):
            t.inject("/tmp/test.env", "KEY=val")

        mock_prepare.assert_called_once()

    def test_inject_with_empty_content_produces_valid_command(self):
        t = _make_transport()

        with patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd:
            t.inject("/tmp/empty.env", "")

        write_cmd = mock_cmd.call_args_list[1][0][0]
        assert "base64 -d" in write_cmd

    def test_inject_no_mkdir_for_top_level_path(self):
        t = _make_transport()
        with patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd:
            t.inject("file.txt", "content")

        # Only write + chmod, no mkdir
        assert mock_cmd.call_count == 2
        assert "base64 -d" in mock_cmd.call_args_list[0][0][0]
        assert "chmod 600" in mock_cmd.call_args_list[1][0][0]

    def test_inject_root_level_absolute_path_still_runs_mkdir(self):
        """inject('/file.txt', ...) has dirname='/' which is truthy, so mkdir runs.

        This is a boundary case: os.path.dirname('/file.txt') == '/' which is
        a non-empty string, so the code enters the mkdir branch. Verify the mkdir
        command is actually issued for root-level absolute paths (3 calls total),
        unlike bare relative filenames which skip mkdir (2 calls).
        """
        t = _make_transport()
        with patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd:
            t.inject("/file.txt", "content")

        # dirname('/file.txt') == '/' which is truthy → mkdir IS called
        assert mock_cmd.call_count == 3, (
            f"Expected 3 _exec_shell_command calls (mkdir + write + chmod) for '/file.txt', "
            f"got {mock_cmd.call_count}. The dirname '/' is truthy so mkdir should run."
        )
        assert "mkdir -p" in mock_cmd.call_args_list[0][0][0]

    def test_inject_content_with_equals_and_special_chars_survives_base64_roundtrip(self):
        """Content containing shell-dangerous characters (backticks, $(), newlines)
        must be base64-encoded so they never reach the shell interpreter directly.

        This tests the invariant that the raw content NEVER appears in any
        _exec_shell_command call -- only the base64-encoded form does.
        """
        t = _make_transport()
        dangerous_content = (
            "DB_URL=postgres://u:p@host/db\nSECRET=$(cat /etc/shadow)\nTOKEN=`whoami`"
        )
        encoded = base64.b64encode(dangerous_content.encode("utf-8")).decode("ascii")

        with patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd:
            t.inject("/app/.env", dangerous_content)

        write_cmd = mock_cmd.call_args_list[1][0][0]
        # The base64-encoded string must be present
        assert encoded in write_cmd
        # The raw dangerous substrings must NOT be present in any command
        for call_args in mock_cmd.call_args_list:
            cmd = call_args[0][0]
            assert "$(cat" not in cmd, f"Shell substitution leaked into command: {cmd!r}"
            assert "`whoami`" not in cmd, f"Backtick expansion leaked into command: {cmd!r}"

    def test_inject_write_failure_prevents_chmod_from_running(self):
        """When the write step (step 2) fails, chmod (step 3) must NOT execute.

        This catches a regression where inject() might swallow the write error
        and proceed to chmod, or where the error check is on the wrong return code.
        """
        t = _make_transport()
        call_log = []

        def tracking_exec(cmd):
            call_log.append(cmd)
            if "base64 -d" in cmd:
                return 1  # write fails
            return 0  # mkdir succeeds

        with (
            patch.object(t, "_exec_shell_command", side_effect=tracking_exec),
            pytest.raises(RuntimeError, match="Failed to write"),
        ):
            t.inject("/tmp/secrets.env", "KEY=value")

        # Verify chmod was never called
        chmod_calls = [c for c in call_log if "chmod" in c]
        assert len(chmod_calls) == 0, f"chmod was called despite write failure: {chmod_calls}"
