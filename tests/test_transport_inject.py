"""Unit tests for LeaseShellTransport.inject() and _exec_with_stdin() — Phase 8."""

import base64
import json
import shlex
import urllib.parse
from unittest.mock import patch

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from just_akash.transport import LeaseShellTransport, TransportConfig
from just_akash.transport.lease_shell import _FRAME_STDIN


def _make_transport() -> LeaseShellTransport:
    config = TransportConfig(
        dseq="123",
        api_key="test-key",
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
    """inject() writes the payload by streaming it over a stdin (104) frame via
    `head -c <n> > path`, NOT by embedding a base64 blob in the shell command. The command
    that lands in the provider URL (`cmd2=`) is provider-proxy-logged, so the secret
    must never appear there. mkdir/chmod (no secret) still go via _exec_shell_command;
    only the write goes via _exec_with_stdin_command.
    """

    def test_inject_creates_parent_directory(self):
        t = _make_transport()
        with (
            patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd,
            patch.object(t, "_exec_with_stdin_command", return_value=0),
        ):
            t.inject("/tmp/secrets.env", "KEY=value")

        mkdir_call = mock_cmd.call_args_list[0][0][0]
        assert "mkdir -p" in mkdir_call
        assert shlex.quote("/tmp") in mkdir_call

    def test_inject_writes_content_via_stdin_command(self):
        """The write step pipes the raw content over a stdin frame via
        `head -c <n> > path`, so the shell command carries only the byte count and
        path — never the content. head reads exactly n bytes and exits, so it does
        not hang waiting for a stdin EOF that provider-proxy never delivers."""
        t = _make_transport()
        content = "SECRET=abc123"

        with (
            patch.object(t, "_exec_shell_command", return_value=0),
            patch.object(t, "_exec_with_stdin_command", return_value=0) as mock_stdin,
        ):
            t.inject("/tmp/secrets.env", content)

        assert mock_stdin.call_count == 1
        shell_command, stdin_data = mock_stdin.call_args_list[0][0]
        n = len(content.encode("utf-8"))
        assert shell_command.startswith(f"head -c {n} > ")
        assert shlex.quote("/tmp/secrets.env") in shell_command
        # The content rides the stdin frame, not the command string.
        assert stdin_data == content.encode("utf-8")
        assert content not in shell_command

    def test_inject_sets_file_permissions(self):
        t = _make_transport()
        with (
            patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd,
            patch.object(t, "_exec_with_stdin_command", return_value=0),
        ):
            t.inject("/tmp/secrets.env", "KEY=value")

        # mkdir + chmod go via _exec_shell_command (write goes via stdin), so
        # chmod is the second (and last) _exec_shell_command call.
        chmod_call = mock_cmd.call_args_list[-1][0][0]
        assert "chmod 600" in chmod_call
        assert shlex.quote("/tmp/secrets.env") in chmod_call

    def test_inject_write_uses_stdin_command_not_shell_command(self):
        """mkdir + chmod go through _exec_shell_command (2 calls); the secret write
        goes through _exec_with_stdin_command (1 call) — never a base64 blob in the URL."""
        t = _make_transport()
        with (
            patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd,
            patch.object(t, "_exec_with_stdin_command", return_value=0) as mock_stdin,
        ):
            t.inject("/tmp/secrets.env", "KEY=value")

        assert mock_cmd.call_count == 2
        assert "mkdir -p" in mock_cmd.call_args_list[0][0][0]
        assert "chmod 600" in mock_cmd.call_args_list[1][0][0]
        assert mock_stdin.call_count == 1
        n = len(b"KEY=value")
        assert mock_stdin.call_args_list[0][0][0].startswith(f"head -c {n} > ")
        # No _exec_shell_command call carries the legacy base64-decode pipeline.
        for call_args in mock_cmd.call_args_list:
            assert "base64 -d" not in call_args[0][0]

    def test_inject_raises_on_mkdir_failure(self):
        t = _make_transport()
        with (
            patch.object(t, "_exec_shell_command", return_value=1),
            patch.object(t, "_exec_with_stdin_command", return_value=0),
            pytest.raises(RuntimeError, match="Failed to create directory"),
        ):
            t.inject("/tmp/secrets.env", "KEY=value")

    def test_inject_raises_on_write_failure(self):
        t = _make_transport()
        with (
            patch.object(t, "_exec_shell_command", return_value=0),
            patch.object(t, "_exec_with_stdin_command", return_value=1),
            pytest.raises(RuntimeError, match="Failed to write"),
        ):
            t.inject("/tmp/secrets.env", "KEY=value")

    def test_inject_raises_on_chmod_failure(self):
        t = _make_transport()
        # mkdir ok, chmod fails (the two _exec_shell_command calls); write ok.
        with (
            patch.object(t, "_exec_shell_command", side_effect=[0, 1]),
            patch.object(t, "_exec_with_stdin_command", return_value=0),
            pytest.raises(RuntimeError, match="Failed to set permissions"),
        ):
            t.inject("/tmp/secrets.env", "KEY=value")

    def test_inject_escapes_path_with_shell_metacharacters(self):
        t = _make_transport()
        dangerous_path = "/tmp/test'; rm -rf /"
        with (
            patch.object(t, "_exec_shell_command", return_value=0),
            patch.object(t, "_exec_with_stdin_command", return_value=0) as mock_stdin,
        ):
            t.inject(dangerous_path, "content")

        write_cmd = mock_stdin.call_args_list[0][0][0]
        assert write_cmd.startswith("head -c ")
        assert shlex.quote(dangerous_path) in write_cmd

    def test_inject_handles_multiline_content(self):
        t = _make_transport()
        content = "LINE1=val1\nLINE2=val2\n"

        with (
            patch.object(t, "_exec_shell_command", return_value=0),
            patch.object(t, "_exec_with_stdin_command", return_value=0) as mock_stdin,
        ):
            t.inject("/tmp/multiline.env", content)

        stdin_data = mock_stdin.call_args_list[0][0][1]
        assert stdin_data == content.encode("utf-8")

    def test_inject_secret_value_not_in_plaintext_commands(self):
        t = _make_transport()
        secret_value = "SUPER_SECRET_PASSWORD_12345"

        with (
            patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd,
            patch.object(t, "_exec_with_stdin_command", return_value=0) as mock_stdin,
        ):
            t.inject("/tmp/secret.env", f"PASSWORD={secret_value}")

        # No _exec_shell_command (URL-carried) command may contain the raw secret.
        for i, call_args in enumerate(mock_cmd.call_args_list):
            cmd = call_args[0][0]
            assert secret_value not in cmd, f"Secret leaked in command[{i}]: {cmd!r}"
        # The stdin-command's shell string (which also goes in the URL) must be
        # secret-free; the secret must ride the stdin bytes instead.
        shell_command, stdin_data = mock_stdin.call_args_list[0][0]
        assert secret_value not in shell_command, (
            f"Secret leaked into stdin shell command: {shell_command!r}"
        )
        assert secret_value.encode("utf-8") in stdin_data

    def test_inject_calls_prepare_if_not_configured(self):
        t = _make_transport()
        t._provider_host_uri = None
        t._service = None

        with (
            patch.object(t, "prepare") as mock_prepare,
            patch.object(t, "_exec_shell_command", return_value=0),
            patch.object(t, "_exec_with_stdin_command", return_value=0),
        ):
            t.inject("/tmp/test.env", "KEY=val")

        mock_prepare.assert_called_once()

    def test_inject_with_empty_content_produces_valid_command(self):
        t = _make_transport()

        with (
            patch.object(t, "_exec_shell_command", return_value=0),
            patch.object(t, "_exec_with_stdin_command", return_value=0) as mock_stdin,
        ):
            t.inject("/tmp/empty.env", "")

        shell_command, stdin_data = mock_stdin.call_args_list[0][0]
        assert shell_command.startswith("head -c 0 > ")  # empty payload → 0 bytes
        assert stdin_data == b""

    def test_inject_no_mkdir_for_top_level_path(self):
        t = _make_transport()
        with (
            patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd,
            patch.object(t, "_exec_with_stdin_command", return_value=0) as mock_stdin,
        ):
            t.inject("file.txt", "content")

        # No mkdir (no parent dir) → chmod is the only _exec_shell_command call;
        # the write goes via the stdin command.
        assert mock_cmd.call_count == 1
        assert "chmod 600" in mock_cmd.call_args_list[0][0][0]
        assert mock_stdin.call_count == 1
        assert mock_stdin.call_args_list[0][0][0].startswith("head -c ")

    def test_inject_root_level_absolute_path_still_runs_mkdir(self):
        """inject('/file.txt', ...) has dirname='/' which is truthy, so mkdir runs.

        This is a boundary case: os.path.dirname('/file.txt') == '/' which is
        a non-empty string, so the code enters the mkdir branch. Verify the mkdir
        command is actually issued for root-level absolute paths (mkdir + chmod =
        2 _exec_shell_command calls + 1 stdin write), unlike bare relative
        filenames which skip mkdir (1 _exec_shell_command call).
        """
        t = _make_transport()
        with (
            patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd,
            patch.object(t, "_exec_with_stdin_command", return_value=0) as mock_stdin,
        ):
            t.inject("/file.txt", "content")

        # dirname('/file.txt') == '/' which is truthy → mkdir IS called
        assert mock_cmd.call_count == 2, (
            f"Expected 2 _exec_shell_command calls (mkdir + chmod) for '/file.txt', "
            f"got {mock_cmd.call_count}. The dirname '/' is truthy so mkdir should run."
        )
        assert "mkdir -p" in mock_cmd.call_args_list[0][0][0]
        assert "chmod 600" in mock_cmd.call_args_list[1][0][0]
        assert mock_stdin.call_count == 1

    def test_inject_content_with_special_chars_streamed_via_stdin(self):
        """Content containing shell-dangerous characters (backticks, $(), newlines)
        must ride the stdin frame verbatim and NEVER reach any shell command string.

        This tests the invariant that the raw content NEVER appears in any
        _exec_shell_command call nor the stdin-command's shell string -- only in
        the stdin bytes.
        """
        t = _make_transport()
        dangerous_content = (
            "DB_URL=postgres://u:p@host/db\nSECRET=$(cat /etc/shadow)\nTOKEN=`whoami`"
        )

        with (
            patch.object(t, "_exec_shell_command", return_value=0) as mock_cmd,
            patch.object(t, "_exec_with_stdin_command", return_value=0) as mock_stdin,
        ):
            t.inject("/app/.env", dangerous_content)

        shell_command, stdin_data = mock_stdin.call_args_list[0][0]
        # The content rides the stdin bytes verbatim.
        assert stdin_data == dangerous_content.encode("utf-8")
        # The raw dangerous substrings must NOT be present in any command string.
        commands = [c[0][0] for c in mock_cmd.call_args_list] + [shell_command]
        for cmd in commands:
            assert "$(cat" not in cmd, f"Shell substitution leaked into command: {cmd!r}"
            assert "`whoami`" not in cmd, f"Backtick expansion leaked into command: {cmd!r}"

    def test_inject_write_failure_prevents_chmod_from_running(self):
        """When the write step fails, chmod must NOT execute.

        This catches a regression where inject() might swallow the write error
        and proceed to chmod, or where the error check is on the wrong return code.
        """
        t = _make_transport()
        shell_calls = []

        with (
            patch.object(
                t, "_exec_shell_command", side_effect=lambda cmd: shell_calls.append(cmd) or 0
            ),
            patch.object(t, "_exec_with_stdin_command", return_value=1),  # write fails
            pytest.raises(RuntimeError, match="Failed to write"),
        ):
            t.inject("/tmp/secrets.env", "KEY=value")

        # Verify chmod was never called (only mkdir ran before the write failed).
        chmod_calls = [c for c in shell_calls if "chmod" in c]
        assert len(chmod_calls) == 0, f"chmod was called despite write failure: {chmod_calls}"

    def test_inject_secret_not_in_provider_url_and_sent_via_stdin_frame(self):
        """End-to-end frame-level guard: drive inject() through the real
        _exec_with_stdin_command (only mkdir/chmod are stubbed) and inspect the
        messages actually sent to provider-proxy.

        The secret content must NOT appear in any frame's `url` (the URL is what
        provider-proxy logs), and MUST be carried as the payload of a 104 stdin
        `data` frame.
        """
        t = _make_transport()
        secret = "TOPSECRET_VALUE_9f8e7d6c"
        content = f"API_KEY={secret}"
        # A 102 result frame (exit 0) so _exec_with_stdin_command returns cleanly.
        frames = [bytes([102]) + (0).to_bytes(4, "little")]

        with (
            patch.object(t, "_exec_shell_command", return_value=0),  # mkdir + chmod
            patch.object(t, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
            patch("just_akash.transport.lease_shell.time.sleep"),
        ):
            fake_ws = FakeWebSocket(frames)
            mock_connect.return_value = fake_ws
            t.inject("/tmp/secret.env", content)

        assert fake_ws.sent_messages, "expected messages sent to provider-proxy"

        secret_seen_in_stdin_frame = False
        for raw in fake_ws.sent_messages:
            msg = json.loads(raw)
            # The URL (logged by provider-proxy) must never carry the secret,
            # raw or percent-encoded.
            url = msg["url"]
            assert secret not in url, f"Secret leaked into provider URL: {url!r}"
            assert secret not in urllib.parse.unquote(url), (
                f"Secret leaked into provider URL (decoded): {url!r}"
            )
            # The command in the URL is just `head -c <n> > path`.
            assert "head" in urllib.parse.unquote(url)
            data = msg.get("data")
            if data:
                frame = base64.b64decode(data)
                if frame and frame[0] == _FRAME_STDIN and secret.encode("utf-8") in frame[1:]:
                    assert frame[1:] == content.encode("utf-8")
                    secret_seen_in_stdin_frame = True

        assert secret_seen_in_stdin_frame, (
            "secret content was not delivered via a 104 stdin data frame"
        )
