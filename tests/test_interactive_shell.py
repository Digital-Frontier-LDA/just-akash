"""Wave 0 test stubs for Phase 9: Interactive Shell (SHLL-01 through SHLL-04).

Tests exercise the proxy-based interactive shell call path where connect()
routes through the Console provider-proxy relay.
"""

import base64
import json
import os
import signal
import struct
from unittest.mock import MagicMock, patch

import pytest

from just_akash.transport.lease_shell import LeaseShellTransport


def _make_transport():
    t = LeaseShellTransport.__new__(LeaseShellTransport)
    t._provider_host_uri = "https://provider.us-east.akash.pub:8443"
    t._service = "web"
    t._provider_address = None
    t._ws = None
    mock_api = MagicMock()
    mock_api.create_jwt.return_value = "test-jwt-token"
    mock_api.create_jwt_with_provider.return_value = "test-jwt-token"
    t._api_client = mock_api
    mock_config = MagicMock()
    mock_config.dseq = "1"
    mock_config.api_key = "test-key"
    mock_config.provider_proxy_url = "https://provider-proxy.akash.network"
    t._config = mock_config
    return t


def _decode_proxy_send(send_args_list):
    """Decode base64-encoded JSON proxy messages sent via ws.send()."""
    frames = []
    for c in send_args_list:
        try:
            data = json.loads(c.args[0])
            if data.get("isBase64") and "data" in data:
                frames.append(base64.b64decode(data["data"]))
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
    return frames


class TestLeaseShellConnect:
    def test_connect_raises_on_windows(self):
        t = _make_transport()
        with patch("sys.platform", "win32"), patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            with pytest.raises(NotImplementedError, match="Windows"):
                t.connect()

    def test_connect_raises_on_non_tty_stdin(self):
        t = _make_transport()
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            with pytest.raises((RuntimeError, NotImplementedError)):
                t.connect()

    def test_connect_opens_websocket_with_tty_true_stdin_true(self):
        t = _make_transport()
        with (
            patch("just_akash.transport.lease_shell.connect") as mock_ws,
            patch("just_akash.transport.lease_shell.ssl.create_default_context"),
            patch("termios.tcgetattr", return_value=[]),
            patch("termios.tcsetattr"),
            patch("tty.setraw"),
            patch("sys.stdin") as mock_stdin,
            patch("signal.signal"),
            patch("fcntl.fcntl"),
            patch("os.get_terminal_size", return_value=os.terminal_size((80, 24))),
        ):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            ws_instance = MagicMock()
            ws_instance.recv.side_effect = RuntimeError("stop")
            mock_ws.return_value.__enter__.return_value = ws_instance
            mock_ws.return_value.__exit__.return_value = False
            with pytest.raises(RuntimeError):
                t.connect()
            first_send = ws_instance.send.call_args_list[0].args[0]
            connect_msg = json.loads(first_send)
            provider_url = connect_msg["url"]
            # "1"/"0", not "true"/"false" — the provider only honors the literal "1".
            assert "tty=1" in provider_url
            assert "stdin=1" in provider_url
            # The interactive request must carry a shell command — a shell request
            # with no cmd is rejected by the provider ("Received error from provider
            # websocket"). We exec an interactive /bin/sh.
            assert "cmd0=%2Fbin%2Fsh" in provider_url  # url-encoded /bin/sh
            assert "cmd1=-i" in provider_url

    def test_connect_sends_terminal_size_after_session_is_live(self):
        # The resize frame must NOT be sent before the provider has accepted the
        # session — a data frame sent that early is rejected by the proxy
        # ("url/providerAddress Required") and kills the whole connect. It is sent
        # once the first provider frame confirms the session is live.
        t = _make_transport()
        with (
            patch("just_akash.transport.lease_shell.connect") as mock_ws,
            patch("just_akash.transport.lease_shell.ssl.create_default_context"),
            patch("termios.tcgetattr", return_value=[]),
            patch("termios.tcsetattr"),
            patch("tty.setraw"),
            patch("sys.stdin") as mock_stdin,
            patch("signal.signal"),
            patch("fcntl.fcntl"),
            patch("os.get_terminal_size", return_value=os.terminal_size((80, 24))),
            patch("sys.stdout"),  # first frame is dispatched to stdout — don't pollute test output
        ):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            ws_instance = MagicMock()
            # First a provider stdout frame (code 100), then stop the loop.
            ws_instance.recv.side_effect = [
                json.dumps({"type": "data", "message": {"data": [100, 120]}}),
                RuntimeError("stop"),
            ]
            mock_ws.return_value.__enter__.return_value = ws_instance
            mock_ws.return_value.__exit__.return_value = False
            with pytest.raises(RuntimeError):
                t.connect()

            sends = ws_instance.send.call_args_list
            # The connect message (index 0) must not be a resize frame.
            connect_msg = json.loads(sends[0].args[0])
            assert "url" in connect_msg and "providerAddress" in connect_msg
            sent_frames = _decode_proxy_send(sends)
            resize_frames = [f for f in sent_frames if f[0] == 105]
            assert resize_frames, "Expected a resize frame (code 105) once the session was live"
            rows, cols = struct.unpack(">HH", resize_frames[0][1:5])
            assert rows == 24
            assert cols == 80

    def test_connect_forwards_stdin_to_frame_104(self):
        t = _make_transport()
        stdin_data = b"hello"
        with (
            patch("just_akash.transport.lease_shell.connect") as mock_ws,
            patch("just_akash.transport.lease_shell.ssl.create_default_context"),
            patch("termios.tcgetattr", return_value=[]),
            patch("termios.tcsetattr"),
            patch("tty.setraw"),
            patch("sys.stdin") as mock_stdin,
            patch("signal.signal"),
            patch("fcntl.fcntl"),
            patch("os.get_terminal_size", return_value=os.terminal_size((80, 24))),
            patch("select.select") as mock_select,
            patch("os.read") as mock_os_read,
        ):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            mock_select.side_effect = [([0], [], []), Exception("stop")]
            mock_os_read.return_value = stdin_data
            ws_instance = MagicMock()
            ws_instance.recv.side_effect = RuntimeError("no recv")
            mock_ws.return_value.__enter__.return_value = ws_instance
            mock_ws.return_value.__exit__.return_value = False
            with pytest.raises(RuntimeError):
                t.connect()
            sends = ws_instance.send.call_args_list
            sent_frames = _decode_proxy_send(sends)
            stdin_frames = [f for f in sent_frames if f[0] == 104]
            assert any(f[1:] == stdin_data for f in stdin_frames), (
                f"Expected frame 104 + b'hello'; got: {[f.hex() for f in stdin_frames]}"
            )
            # The stdin message must carry the full connect envelope, not a data-only
            # frame — the proxy rejects bare {type,data,isBase64} with
            # "url/providerAddress Required", so keystrokes never reach the shell.
            stdin_msg = next(
                json.loads(c.args[0])
                for c in sends
                if json.loads(c.args[0]).get("data")
                and base64.b64decode(json.loads(c.args[0])["data"])[:1] == bytes([104])
            )
            assert "url" in stdin_msg
            assert "providerAddress" in stdin_msg
            assert stdin_msg.get("auth", {}).get("type") == "jwt"

    def test_connect_dispatches_frame_100_to_stdout(self):
        t = _make_transport()
        with (
            patch("just_akash.transport.lease_shell.connect") as mock_ws,
            patch("just_akash.transport.lease_shell.ssl.create_default_context"),
            patch("termios.tcgetattr", return_value=[]),
            patch("termios.tcsetattr"),
            patch("tty.setraw"),
            patch("sys.stdin") as mock_stdin,
            patch("signal.signal"),
            patch("fcntl.fcntl"),
            patch("os.get_terminal_size", return_value=os.terminal_size((80, 24))),
            patch("select.select", return_value=([], [], [])),
            patch("sys.stdout") as mock_stdout,
        ):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            ws_instance = MagicMock()
            ws_instance.recv.side_effect = [bytes([100]) + b"output", RuntimeError("stop")]
            mock_ws.return_value.__enter__.return_value = ws_instance
            mock_ws.return_value.__exit__.return_value = False
            with pytest.raises(RuntimeError):
                t.connect()
            mock_stdout.buffer.write.assert_any_call(b"output")

    def test_connect_dispatches_frame_101_to_stderr(self):
        t = _make_transport()
        with (
            patch("just_akash.transport.lease_shell.connect") as mock_ws,
            patch("just_akash.transport.lease_shell.ssl.create_default_context"),
            patch("termios.tcgetattr", return_value=[]),
            patch("termios.tcsetattr"),
            patch("tty.setraw"),
            patch("sys.stdin") as mock_stdin,
            patch("signal.signal"),
            patch("fcntl.fcntl"),
            patch("os.get_terminal_size", return_value=os.terminal_size((80, 24))),
            patch("select.select", return_value=([], [], [])),
            patch("sys.stderr") as mock_stderr,
        ):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            ws_instance = MagicMock()
            ws_instance.recv.side_effect = [bytes([101]) + b"err", RuntimeError("stop")]
            mock_ws.return_value.__enter__.return_value = ws_instance
            mock_ws.return_value.__exit__.return_value = False
            with pytest.raises(RuntimeError):
                t.connect()
            mock_stderr.buffer.write.assert_any_call(b"err")

    def test_connect_exits_on_frame_102(self):
        t = _make_transport()
        with (
            patch("just_akash.transport.lease_shell.connect") as mock_ws,
            patch("just_akash.transport.lease_shell.ssl.create_default_context"),
            patch("termios.tcgetattr", return_value=[]),
            patch("termios.tcsetattr"),
            patch("tty.setraw"),
            patch("sys.stdin") as mock_stdin,
            patch("signal.signal"),
            patch("fcntl.fcntl"),
            patch("os.get_terminal_size", return_value=os.terminal_size((80, 24))),
            patch("select.select", return_value=([], [], [])),
        ):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            ws_instance = MagicMock()
            ws_instance.recv.return_value = bytes([102])
            mock_ws.return_value.__enter__.return_value = ws_instance
            mock_ws.return_value.__exit__.return_value = False
            t.connect()

    def test_sigint_sends_frame_104_with_0x03(self):
        t = _make_transport()
        captured_handler: list[object] = [None]

        def capture_signal(sig, handler):
            if sig == signal.SIGINT and callable(handler):
                captured_handler[0] = handler
            return signal.SIG_DFL

        with (
            patch("just_akash.transport.lease_shell.connect") as mock_ws,
            patch("just_akash.transport.lease_shell.ssl.create_default_context"),
            patch("termios.tcgetattr", return_value=[]),
            patch("termios.tcsetattr"),
            patch("tty.setraw"),
            patch("sys.stdin") as mock_stdin,
            patch("signal.signal", side_effect=capture_signal),
            patch("fcntl.fcntl"),
            patch("os.get_terminal_size", return_value=os.terminal_size((80, 24))),
            patch("select.select", return_value=([], [], [])),
        ):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            ws_instance = MagicMock()
            ws_instance.recv.return_value = bytes([102])
            mock_ws.return_value.__enter__.return_value = ws_instance
            mock_ws.return_value.__exit__.return_value = False
            t.connect()

        assert captured_handler[0] is not None, "SIGINT handler was never registered"
        ws_instance.send.reset_mock()
        captured_handler[0](signal.SIGINT, None)  # type: ignore[call-arg]
        sent_frames = _decode_proxy_send(ws_instance.send.call_args_list)
        assert bytes([104, 0x03]) in sent_frames

    def test_sigint_does_not_raise_keyboardinterrupt(self):
        t = _make_transport()
        with (
            patch("just_akash.transport.lease_shell.connect") as mock_ws,
            patch("just_akash.transport.lease_shell.ssl.create_default_context"),
            patch("termios.tcgetattr", return_value=[]),
            patch("termios.tcsetattr"),
            patch("tty.setraw"),
            patch("sys.stdin") as mock_stdin,
            patch("signal.signal"),
            patch("fcntl.fcntl"),
            patch("os.get_terminal_size", return_value=os.terminal_size((80, 24))),
            patch("select.select", return_value=([], [], [])),
        ):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            ws_instance = MagicMock()
            ws_instance.recv.return_value = bytes([102])
            mock_ws.return_value.__enter__.return_value = ws_instance
            mock_ws.return_value.__exit__.return_value = False
            t.connect()

    def test_sigwinch_sends_frame_105_with_new_size(self):
        t = _make_transport()
        captured_handler: list[object] = [None]

        def capture_signal(sig, handler):
            if sig == signal.SIGWINCH and callable(handler):
                captured_handler[0] = handler
            return signal.SIG_DFL

        with (
            patch("just_akash.transport.lease_shell.connect") as mock_ws,
            patch("just_akash.transport.lease_shell.ssl.create_default_context"),
            patch("termios.tcgetattr", return_value=[]),
            patch("termios.tcsetattr"),
            patch("tty.setraw"),
            patch("sys.stdin") as mock_stdin,
            patch("signal.signal", side_effect=capture_signal),
            patch("fcntl.fcntl"),
            patch("os.get_terminal_size", return_value=os.terminal_size((100, 40))),
            patch("select.select", return_value=([], [], [])),
        ):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            ws_instance = MagicMock()
            ws_instance.recv.return_value = bytes([102])
            mock_ws.return_value.__enter__.return_value = ws_instance
            mock_ws.return_value.__exit__.return_value = False
            t.connect()

        assert captured_handler[0] is not None, "SIGWINCH handler was never registered"
        ws_instance.send.reset_mock()
        captured_handler[0](signal.SIGWINCH, None)  # type: ignore[call-arg]
        sent_frames = _decode_proxy_send(ws_instance.send.call_args_list)
        resize_frames = [f for f in sent_frames if f[0] == 105]
        assert resize_frames, "SIGWINCH handler did not send a resize frame (code 105)"
        rows, cols = struct.unpack(">HH", resize_frames[0][1:5])
        assert rows == 40
        assert cols == 100

    def test_terminal_restored_on_normal_exit(self):
        t = _make_transport()
        with (
            patch("just_akash.transport.lease_shell.connect") as mock_ws,
            patch("just_akash.transport.lease_shell.ssl.create_default_context"),
            patch("termios.tcgetattr", return_value=[]),
            patch("termios.tcsetattr") as mock_tcsetattr,
            patch("tty.setraw"),
            patch("sys.stdin") as mock_stdin,
            patch("signal.signal"),
            patch("fcntl.fcntl"),
            patch("os.get_terminal_size", return_value=os.terminal_size((80, 24))),
            patch("select.select", return_value=([], [], [])),
        ):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            ws_instance = MagicMock()
            ws_instance.recv.return_value = bytes([102])
            mock_ws.return_value.__enter__.return_value = ws_instance
            mock_ws.return_value.__exit__.return_value = False
            t.connect()
        assert mock_tcsetattr.called, "termios.tcsetattr() was not called — terminal not restored"

    def test_terminal_restored_on_exception(self):
        t = _make_transport()
        with (
            patch("just_akash.transport.lease_shell.connect") as mock_ws,
            patch("just_akash.transport.lease_shell.ssl.create_default_context"),
            patch("termios.tcgetattr", return_value=[]),
            patch("termios.tcsetattr") as mock_tcsetattr,
            patch("tty.setraw"),
            patch("sys.stdin") as mock_stdin,
            patch("signal.signal"),
            patch("fcntl.fcntl"),
            patch("os.get_terminal_size", return_value=os.terminal_size((80, 24))),
        ):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            ws_instance = MagicMock()
            ws_instance.recv.side_effect = RuntimeError("crash")
            mock_ws.return_value.__enter__.return_value = ws_instance
            mock_ws.return_value.__exit__.return_value = False
            with pytest.raises((RuntimeError, NotImplementedError)):
                t.connect()
        assert mock_tcsetattr.called, (
            "termios.tcsetattr() was not called after exception — finally block missing"
        )

    def test_terminal_restored_on_connection_close(self):
        from websockets.exceptions import ConnectionClosedOK

        t = _make_transport()
        with (
            patch("just_akash.transport.lease_shell.connect") as mock_ws,
            patch("just_akash.transport.lease_shell.ssl.create_default_context"),
            patch("termios.tcgetattr", return_value=[]),
            patch("termios.tcsetattr") as mock_tcsetattr,
            patch("tty.setraw"),
            patch("sys.stdin") as mock_stdin,
            patch("signal.signal"),
            patch("fcntl.fcntl"),
            patch("os.get_terminal_size", return_value=os.terminal_size((80, 24))),
        ):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            ws_instance = MagicMock()
            rcvd = MagicMock()
            rcvd.code = 1000
            rcvd.reason = ""
            ws_instance.recv.side_effect = ConnectionClosedOK(rcvd, None)
            mock_ws.return_value.__enter__.return_value = ws_instance
            mock_ws.return_value.__exit__.return_value = False
            t.connect()
        assert mock_tcsetattr.called, "termios.tcsetattr() was not called after ConnectionClosedOK"

    def test_run_io_loop_continues_on_stdin_eof(self):
        """When os.read returns b"" (EOF on stdin), _run_io_loop must not crash or hang."""
        t = _make_transport()
        ws = MagicMock()
        ws.recv.return_value = bytes([102])

        with (
            patch("sys.stdin") as mock_stdin,
            patch("select.select", return_value=([0], [], [])),
            patch("os.read", return_value=b""),
        ):
            mock_stdin.fileno.return_value = 0
            t._run_io_loop(ws, "https://p/lease/1/1/1/shell?tty=1", "jwt")
