"""Unit tests for LeaseShellTransport.exec() and supporting methods."""

import base64
import io
import json
from unittest.mock import MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

from just_akash.transport.base import TransportConfig
from just_akash.transport.lease_shell import (
    LeaseShellTransport,
    _is_auth_expiry,
    _is_auth_expiry_message,
)

# --- FakeWebSocket helper ---


class FakeWebSocket:
    """Minimal WebSocket mock that serves pre-built frames then closes."""

    def __init__(self, frames):
        self._frames = iter(frames)
        self.sent_messages: list = []

    def recv(self, timeout=None):
        try:
            return next(self._frames)
        except StopIteration:
            raise ConnectionClosedOK(None, None) from None

    def send(self, data):
        self.sent_messages.append(data)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def close(self):
        pass


def make_close_error(code: int, reason: str = "") -> ConnectionClosedError:
    """Build a ConnectionClosedError with a specific close code for testing."""
    close_frame = Close(code=code, reason=reason)
    return ConnectionClosedError(rcvd=close_frame, sent=None)


# --- Fixtures ---

DEPLOYMENT_FIXTURE = {
    "leases": [
        {
            "provider": {"hostUri": "https://provider.us-east.akash.pub:8443"},
            "status": {"services": {"web": {"ready_replicas": 1, "total": 1}}},
        }
    ]
}


# --- JWT Fetch Tests ---


class TestJWTFetch:
    """Test _fetch_jwt() and AkashConsoleAPI.create_jwt() integration."""

    def test_fetch_jwt_happy_path(self):
        """Test _fetch_jwt() calls create_jwt and returns token string."""
        config = TransportConfig(
            dseq="123456",
            api_key="test-key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)

        with patch.object(transport, "_get_api_client") as mock_get_api:
            mock_api = MagicMock()
            mock_api.create_jwt.return_value = "jwt-token-abc123"
            mock_get_api.return_value = mock_api

            result = transport._fetch_jwt(ttl=7200)

            assert result == "jwt-token-abc123"
            mock_api.create_jwt.assert_called_once_with("123456", ttl=7200, scope=None)

    def test_fetch_jwt_with_default_ttl(self):
        """Test _fetch_jwt() uses default TTL of 3600."""
        config = TransportConfig(
            dseq="789",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)

        with patch.object(transport, "_get_api_client") as mock_get_api:
            mock_api = MagicMock()
            mock_api.create_jwt.return_value = "token"
            mock_get_api.return_value = mock_api

            transport._fetch_jwt()

            mock_api.create_jwt.assert_called_once_with("789", ttl=3600, scope=None)

    def test_fetch_jwt_error_propagates(self):
        """Test _fetch_jwt() propagates RuntimeError from create_jwt."""
        config = TransportConfig(
            dseq="bad",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)

        with patch.object(transport, "_get_api_client") as mock_get_api:
            mock_api = MagicMock()
            mock_api.create_jwt.side_effect = RuntimeError("API error: 403 Forbidden")
            mock_get_api.return_value = mock_api

            with pytest.raises(RuntimeError, match="API error: 403 Forbidden"):
                transport._fetch_jwt()


# --- Provider URL Extraction Tests ---


class TestProviderInfoExtraction:
    """Test _extract_provider_info() and related methods."""

    def test_extract_provider_info_happy_path(self):
        config = TransportConfig(
            dseq="999",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
            service_name="web",
        )
        transport = LeaseShellTransport(config)

        host_uri, service = transport._extract_provider_info()

        assert host_uri == "https://provider.us-east.akash.pub:8443"
        assert service == "web"

    def test_extract_provider_info_https_preserved(self):
        config = TransportConfig(
            dseq="1",
            api_key="k",
            deployment={
                "leases": [
                    {
                        "provider": {"hostUri": "https://example.com:9000"},
                        "status": {"services": {"api": {}}},
                    }
                ]
            },
        )
        transport = LeaseShellTransport(config)

        host_uri, service = transport._extract_provider_info()

        assert host_uri == "https://example.com:9000"

    def test_extract_provider_info_http_preserved(self):
        config = TransportConfig(
            dseq="2",
            api_key="k",
            deployment={
                "leases": [
                    {
                        "provider": {"hostUri": "http://localhost:8080"},
                        "status": {"services": {"service1": {}}},
                    }
                ]
            },
        )
        transport = LeaseShellTransport(config)

        host_uri, service = transport._extract_provider_info()

        assert host_uri == "http://localhost:8080"

    def test_extract_provider_info_no_leases(self):
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment={"leases": []},
        )
        transport = LeaseShellTransport(config)

        with pytest.raises(RuntimeError, match="No leases found"):
            transport._extract_provider_info()

    def test_extract_provider_info_missing_hostUri(self):
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment={
                "leases": [
                    {
                        "provider": {},
                        "status": {"services": {"web": {}}},
                    }
                ]
            },
        )
        transport = LeaseShellTransport(config)

        with pytest.raises(RuntimeError, match="Cannot resolve provider hostUri"):
            transport._extract_provider_info()

    def test_extract_provider_info_host_uri_snake_case_fallback(self):
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment={
                "leases": [
                    {
                        "provider": {"host_uri": "https://snake-case.example.com"},
                        "status": {"services": {"app": {}}},
                    }
                ]
            },
        )
        transport = LeaseShellTransport(config)

        host_uri, service = transport._extract_provider_info()

        assert host_uri == "https://snake-case.example.com"

    def test_extract_provider_info_missing_service_name(self):
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment={
                "leases": [
                    {
                        "provider": {"hostUri": "https://provider.com"},
                        "status": {"services": {}},
                    }
                ]
            },
            service_name=None,
        )
        transport = LeaseShellTransport(config)

        # The message deliberately changed: "cannot determine service name" conflated
        # "the lease reports no services yet" with "there are several, pick one", and
        # callers could not act on it. It now says which, and names the escape hatch.
        with pytest.raises(RuntimeError, match="has not reported any service"):
            transport._extract_provider_info()

    def test_extract_provider_info_with_string_id_does_not_crash(self):
        """Lease 'id' as non-dict must raise RuntimeError, not AttributeError."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment={
                "leases": [
                    {
                        "id": "plain-string-id",
                        "provider": {"hostUri": "https://provider.com"},
                        "status": {"services": {"web": {}}},
                    }
                ]
            },
        )
        transport = LeaseShellTransport(config)
        with pytest.raises(RuntimeError):
            transport._extract_provider_info()

    def test_extract_provider_info_lease_id_none_does_not_set_provider_address(self):
        """When lease['id'] is None, _provider_address must remain unset.

        lease_id = lease.get('id') returns None, so the dict-based provider
        address extraction is skipped and _provider_address stays None.
        """
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment={
                "leases": [
                    {
                        "id": None,
                        "provider": {"hostUri": "https://provider.com"},
                        "status": {"services": {"web": {}}},
                    }
                ]
            },
        )
        transport = LeaseShellTransport(config)
        host_uri, service = transport._extract_provider_info()
        assert host_uri == "https://provider.com"
        assert service == "web"
        assert transport._provider_address is None


# --- Frame Dispatch Tests ---


class TestFrameDispatch:
    """Test _dispatch_frame() handles all frame codes correctly."""

    def test_dispatch_frame_code_100_stdout(self):
        """Test code 100 writes to sys.stdout.buffer."""
        buf = io.BytesIO()
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.buffer = buf
            result = LeaseShellTransport._dispatch_frame(bytes([100]) + b"hello world")

        assert result is None
        assert buf.getvalue() == b"hello world"

    def test_dispatch_frame_code_101_stderr(self):
        """Test code 101 writes to sys.stderr.buffer."""
        buf = io.BytesIO()
        with patch("sys.stderr") as mock_stderr:
            mock_stderr.buffer = buf
            result = LeaseShellTransport._dispatch_frame(bytes([101]) + b"error message")

        assert result is None
        assert buf.getvalue() == b"error message"

    def test_dispatch_frame_code_102_int32_le(self):
        """Test code 102 with 4-byte LE int32 exit code."""
        # Exit code 42 in little-endian
        frame = bytes([102]) + (42).to_bytes(4, "little")
        result = LeaseShellTransport._dispatch_frame(frame)

        assert result == 42

    def test_dispatch_frame_code_102_json_fallback(self):
        """Test code 102 falls back to JSON parsing when fewer than 4 bytes available."""
        json_payload = json.dumps({"exit_code": 137}).encode("utf-8")
        # Use only 2 bytes of the JSON so int32 parse skips and falls back to JSON
        frame = bytes([102]) + json_payload[:2]
        # Will fail both int32 and JSON parse, so defaults to 0
        result = LeaseShellTransport._dispatch_frame(frame)
        assert result == 0

    def test_dispatch_frame_code_102_zero_default(self):
        """Test code 102 defaults to 0 if both int32 and JSON parsing fail."""
        # Use exactly 3 bytes so int32 skip happens and JSON parse fails
        frame = bytes([102]) + b"bad"
        result = LeaseShellTransport._dispatch_frame(frame)

        assert result == 0

    def test_dispatch_frame_code_102_json_valid_with_less_than_4_bytes(self):
        """Test code 102 with JSON payload when less than 4 bytes available."""
        # Payload with < 4 bytes will skip int32 and try JSON
        json_payload = b'{"exit_code":99}'
        frame = bytes([102]) + json_payload[:2]  # Only 2 bytes, not valid JSON
        result = LeaseShellTransport._dispatch_frame(frame)
        # Should default to 0 since JSON parse will fail
        assert result == 0

    def test_dispatch_frame_code_103_failure(self):
        """Test code 103 raises RuntimeError."""
        frame = bytes([103]) + b"provider error: out of memory"
        with pytest.raises(RuntimeError, match="Provider error: provider error"):
            LeaseShellTransport._dispatch_frame(frame)

    def test_dispatch_frame_code_103_utf8_decode_error(self):
        """Test code 103 handles invalid UTF-8."""
        frame = bytes([103]) + b"\xff\xfe invalid utf8"
        with pytest.raises(RuntimeError, match="Provider error"):
            LeaseShellTransport._dispatch_frame(frame)

    def test_dispatch_frame_unknown_code(self):
        """Test unknown frame code returns None."""
        frame = bytes([255]) + b"unknown"
        result = LeaseShellTransport._dispatch_frame(frame)

        assert result is None

    def test_dispatch_frame_empty_bytes(self):
        """Test empty bytes returns None."""
        result = LeaseShellTransport._dispatch_frame(b"")

        assert result is None

    def test_dispatch_frame_non_bytes_input(self):
        """Test non-bytes input returns None."""
        result = LeaseShellTransport._dispatch_frame(None)  # type: ignore

        assert result is None

    def test_dispatch_frame_code_102_json_non_dict_payload_returns_zero(self):
        """Code 102 with JSON non-dict payload must not crash with AttributeError."""
        frame = bytes([102]) + b"[1]"
        result = LeaseShellTransport._dispatch_frame(frame)
        assert result == 0


class TestInferService:
    def test_infer_service_returns_none_when_services_is_list(self):
        """_infer_service() must return None when services is a list, not a dict."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment={
                "leases": [
                    {
                        "provider": {"hostUri": "https://provider.com"},
                        "status": {"services": ["web", "api"]},
                    }
                ]
            },
        )
        transport = LeaseShellTransport(config)

        assert transport._infer_service() is None

    def test_infer_service_returns_none_when_status_is_string(self):
        """_infer_service() must return None when lease status is a string."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment={
                "leases": [
                    {
                        "provider": {"hostUri": "https://provider.com"},
                        "status": "active",
                    }
                ]
            },
        )
        transport = LeaseShellTransport(config)

        assert transport._infer_service() is None


class TestBuildProxyConnectMsg:
    def test_build_proxy_connect_msg_with_none_provider_address(self):
        """_build_proxy_connect_msg must produce valid JSON even if _provider_address is None."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        transport._provider_address = None

        result = transport._build_proxy_connect_msg("/lease/123/1/1/shell", "jwt-token")
        parsed = json.loads(result)

        assert parsed["providerAddress"] is None
        assert parsed["auth"]["token"] == "jwt-token"

    def test_build_proxy_connect_msg_with_empty_jwt(self):
        """_build_proxy_connect_msg must produce valid JSON with empty string JWT."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        transport._provider_address = None

        result = transport._build_proxy_connect_msg("/path", "")
        parsed = json.loads(result)

        assert parsed["auth"]["token"] == ""

    def test_build_proxy_connect_msg_with_null_bytes_in_stdin(self):
        """_build_proxy_connect_msg must handle stdin_data containing null bytes."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        transport._provider_address = None

        stdin_with_nulls = "hello\x00world\x00"
        result = transport._build_proxy_connect_msg("/path", "jwt", stdin_data=stdin_with_nulls)
        parsed = json.loads(result)

        decoded = base64.b64decode(parsed["data"])
        assert decoded == b"hello\x00world\x00"


class TestRecvProxyMessage:
    def test_recv_proxy_message_returns_none_when_message_data_is_int(self):
        """_recv_proxy_message must return None when message.data is an integer, not crash."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        ws = MagicMock()
        ws.recv.return_value = json.dumps({"type": "data", "message": {"data": 42}})

        result = transport._recv_proxy_message(ws)

        assert result is None

    def test_recv_proxy_message_returns_none_when_msg_data_is_list(self):
        """_recv_proxy_message must return None when top-level msg['data'] is a list."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        ws = MagicMock()
        ws.recv.return_value = json.dumps({"type": "data", "data": [1, 2, 3]})

        result = transport._recv_proxy_message(ws)

        assert result is None


# Captured verbatim from wss://console.akash.network/provider-proxy-mainnet. Note the
# type is "websocket", NOT "error" -- the proxy reports failures on an ordinary frame
# with an `error` key, which is exactly why these used to slip through.
PROXY_PROVIDER_ERROR = json.dumps(
    {
        "type": "websocket",
        "message": "Received error from provider websocket",
        "error": "Received error from provider websocket",
    }
)
PROXY_SCHEMA_ERROR = json.dumps(
    {
        "type": "websocket",
        "message": "Message doesn't match expected schema",
        "error": "Invalid message format",
        "errors": [
            {
                "code": "custom",
                "message": "is not a valid JWT token",
                "path": ["auth", "token"],
            }
        ],
    }
)


def _transport() -> LeaseShellTransport:
    return LeaseShellTransport(
        TransportConfig(dseq="123", api_key="key", deployment=DEPLOYMENT_FIXTURE)
    )


class TestProxyErrorFrames:
    """The proxy's error frames must surface, not be swallowed into a hang.

    Regression: these frames fell through to base64.b64decode() of their prose
    `message`, which either raised (swallowed by a blanket except -> None -> the
    caller re-blocked in recv for the full timeout) or, when the filtered length
    happened to be a multiple of 4, decoded to garbage bytes dispatched as output.
    """

    def test_provider_error_frame_raises(self):
        ws = MagicMock()
        ws.recv.return_value = PROXY_PROVIDER_ERROR

        with pytest.raises(RuntimeError, match="Received error from provider websocket"):
            _transport()._recv_proxy_message(ws)

    def test_schema_error_frame_names_the_offending_field(self):
        """The `errors` detail is the whole value of the frame -- keep it."""
        ws = MagicMock()
        ws.recv.return_value = PROXY_SCHEMA_ERROR

        with pytest.raises(RuntimeError) as exc:
            _transport()._recv_proxy_message(ws)

        assert "auth.token: is not a valid JWT token" in str(exc.value)

    def test_error_frame_does_not_decode_to_garbage_output(self):
        """A prose message whose base64-filtered length is a multiple of 4.

        Under the permissive default b64decode this silently produced bytes, which
        _dispatch_frame would have written to stdout as if the provider sent them.
        """
        ws = MagicMock()
        ws.recv.return_value = json.dumps(
            {
                "type": "websocket",
                "message": "Received error from provider websocketAB",
                "error": "x",
            }
        )

        with pytest.raises(RuntimeError):
            _transport()._recv_proxy_message(ws)

    def test_pump_raises_instead_of_looping_forever(self):
        """The actual CI symptom: exec produced no output and hung until killed."""
        ws = MagicMock()
        ws.recv.return_value = PROXY_PROVIDER_ERROR

        with pytest.raises(RuntimeError, match="Proxy error"):
            _transport()._pump_frames(ws, 0)

    def test_auth_expiry_error_frame_is_recoverable(self):
        """An expired-token frame must reach the reconnect logic that was written for it."""
        ws = MagicMock()
        ws.recv.return_value = json.dumps(
            {"type": "websocket", "message": "jwt expired", "error": "unauthorized"}
        )

        with pytest.raises(RuntimeError) as exc:
            _transport()._pump_frames(ws, 0)

        assert _is_auth_expiry_message(str(exc.value))

    def test_valid_data_frame_still_decodes(self):
        """The error check must not disturb the normal relay path."""
        ws = MagicMock()
        payload = bytes([100]) + b"hello"
        ws.recv.return_value = json.dumps(
            {"type": "websocket", "message": base64.b64encode(payload).decode("ascii")}
        )

        assert _transport()._recv_proxy_message(ws) == payload


class TestExecTimeoutIsBounded:
    def test_silence_raises_a_diagnosis_not_a_bare_timeout(self):
        ws = MagicMock()
        ws.recv.side_effect = TimeoutError

        with pytest.raises(RuntimeError, match="sent nothing for"):
            _transport()._pump_frames(ws, 0)

    def test_recv_timeout_is_configurable(self):
        config = TransportConfig(
            dseq="123", api_key="key", deployment=DEPLOYMENT_FIXTURE, recv_timeout=7
        )
        ws = MagicMock()
        ws.recv.side_effect = TimeoutError

        with pytest.raises(RuntimeError, match="7s"):
            LeaseShellTransport(config)._pump_frames(ws, 0)

        assert ws.recv.call_args.kwargs["timeout"] == 7


class TestBuildShellPath:
    def test_build_shell_path_url_encodes_shell_metacharacters(self):
        """Shell metacharacters (semicolons, pipes) must be URL-encoded, not passed through raw."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        transport._provider_host_uri = "https://provider.com"
        transport._service = "web"
        url = transport._build_provider_shell_url(command="echo hello; cat /etc/passwd")
        assert ";" not in url
        assert "%3B" in url
        assert "cmd1=hello%3B" in url

    def test_build_shell_path_command_none_produces_no_cmd_params(self):
        """_build_provider_shell_url(command=None) must not include any cmd params.

        When command is None, the `if command is not None` branch is skipped entirely.
        This is the path used for interactive shell sessions (connect).
        Verify no cmd0=, cmd1=, etc. appear in the URL.
        """
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        transport._provider_host_uri = "https://provider.com"
        transport._service = "web"
        url = transport._build_provider_shell_url(command=None, tty=True, stdin=True)
        assert "cmd0=" not in url
        assert "cmd1=" not in url
        assert "tty=1" in url
        assert "stdin=1" in url

    def test_build_shell_path_empty_command_sends_no_cmd_params(self):
        """An empty command must send NO cmd params at all.

        This used to assert the opposite -- that command='' produced `cmd0=` with an
        empty value -- and its own docstring conceded the wart: "could cause
        provider-side errors if the provider doesn't expect an empty command
        argument." That came from splitting '' on a space, which yields [''].

        shlex.split('') yields [], so an empty command now means exactly that: no
        command. The characterised bug is gone, so the characterisation goes with it.
        """
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        transport._provider_host_uri = "https://provider.com"
        transport._service = "web"
        url = transport._build_provider_shell_url(command="")

        assert "cmd0=" not in url, f"empty command must not emit an empty cmd param: {url}"

    def test_build_shell_path_whitespace_only_command_sends_no_cmd_params(self):
        """Whitespace-only is an empty command, not a command made of empty strings.

        `"   ".split(" ")` produced ['', '', '', ''] -> four empty cmd params.
        shlex.split("   ") yields [].
        """
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        transport._provider_host_uri = "https://provider.com"
        transport._service = "web"
        url = transport._build_provider_shell_url(command="   ")

        assert "cmd0=" not in url, f"whitespace-only command must not emit cmd params: {url}"


# --- exec() Happy Path Tests ---


class TestExecHappyPath:
    """Test exec() end-to-end with mocked WebSocket."""

    def test_exec_happy_path_stdout_only(self):
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        transport.prepare()

        with patch.object(transport, "_fetch_jwt", return_value="test-jwt"):
            frames = [
                bytes([100]) + b"output\n",
                bytes([102]) + (0).to_bytes(4, "little"),
            ]

            with patch("just_akash.transport.lease_shell.connect") as mock_connect:
                fake_ws = FakeWebSocket(frames)
                mock_connect.return_value = fake_ws

                exit_code = transport.exec("echo hello")

        assert exit_code == 0
        mock_connect.assert_called_once()
        connect_msg = json.loads(fake_ws.sent_messages[0])
        assert "cmd0=" in connect_msg["url"]
        assert "echo" in connect_msg["url"]

    def test_exec_captures_stderr(self):
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        transport.prepare()

        with patch.object(transport, "_fetch_jwt", return_value="jwt"):
            frames = [
                bytes([100]) + b"normal output\n",
                bytes([101]) + b"error output\n",
                bytes([102]) + (0).to_bytes(4, "little"),
            ]

            with patch("just_akash.transport.lease_shell.connect") as mock_connect:
                mock_connect.return_value = FakeWebSocket(frames)

                exit_code = transport.exec("some command")

        assert exit_code == 0

    def test_exec_non_zero_exit_code(self):
        config = TransportConfig(
            dseq="456",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        transport.prepare()

        with patch.object(transport, "_fetch_jwt", return_value="jwt"):
            frames = [
                bytes([100]) + b"command not found\n",
                bytes([102]) + (127).to_bytes(4, "little"),
            ]

            with patch("just_akash.transport.lease_shell.connect") as mock_connect:
                mock_connect.return_value = FakeWebSocket(frames)

                exit_code = transport.exec("nonexistent_cmd")

        assert exit_code == 127

    def test_exec_jwt_sent_in_connect_message(self):
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        transport.prepare()

        with patch.object(transport, "_fetch_jwt", return_value="token-xyz"):
            frames = [
                bytes([102]) + (0).to_bytes(4, "little"),
            ]

            with patch("just_akash.transport.lease_shell.connect") as mock_connect:
                fake_ws = FakeWebSocket(frames)
                mock_connect.return_value = fake_ws

                transport.exec("test")

        connect_msg = json.loads(fake_ws.sent_messages[0])
        assert connect_msg["auth"]["type"] == "jwt"
        assert connect_msg["auth"]["token"] == "token-xyz"

    def test_exec_compression_disabled(self):
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        transport.prepare()

        with (
            patch.object(transport, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            mock_connect.return_value = FakeWebSocket(
                [
                    bytes([102]) + (0).to_bytes(4, "little"),
                ]
            )

            transport.exec("test")

        call_args = mock_connect.call_args
        assert call_args.kwargs.get("compression") is None

    def test_exec_auto_prepare(self):
        config = TransportConfig(
            dseq="999",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)

        with (
            patch.object(transport, "_fetch_jwt", return_value="jwt"),
            patch.object(transport, "prepare") as mock_prepare,
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            mock_connect.return_value = FakeWebSocket(
                [
                    bytes([102]) + (0).to_bytes(4, "little"),
                ]
            )

            mock_prepare.side_effect = lambda: (
                setattr(transport, "_service", "s")
                or setattr(transport, "_provider_host_uri", "https://x")
            )

            transport.exec("cmd")

        mock_prepare.assert_called_once()


# --- validate() Tests ---


class TestValidate:
    """Test validate() method."""

    def test_validate_true_with_hostUri(self):
        """Test validate() returns True when deployment has hostUri."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)

        assert transport.validate() is True

    def test_validate_true_with_host_uri_snake_case(self):
        """Test validate() returns True with host_uri (snake_case)."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment={
                "leases": [
                    {
                        "provider": {"host_uri": "https://example.com"},
                    }
                ]
            },
        )
        transport = LeaseShellTransport(config)

        assert transport.validate() is True

    def test_validate_false_with_empty_leases(self):
        """Test validate() returns False with empty leases."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment={"leases": []},
        )
        transport = LeaseShellTransport(config)

        assert transport.validate() is False

    def test_validate_false_with_missing_hostUri(self):
        """Test validate() returns False when hostUri missing."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment={
                "leases": [
                    {
                        "provider": {},  # No hostUri
                    }
                ]
            },
        )
        transport = LeaseShellTransport(config)

        assert transport.validate() is False

    def test_validate_false_with_non_dict_provider(self):
        """Test validate() returns False when provider is not a dict."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment={
                "leases": [
                    {
                        "provider": "not-a-dict",
                    }
                ]
            },
        )
        transport = LeaseShellTransport(config)

        assert transport.validate() is False

    def test_validate_false_when_leases_is_dict_instead_of_list(self):
        """validate() must return False when leases is a dict, not a list."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment={"leases": {"0": {"provider": {"hostUri": "https://provider.com"}}}},
        )
        transport = LeaseShellTransport(config)

        assert transport.validate() is False


# --- NotImplemented Methods ---


class TestNotImplementedMethods:
    """Test that connect() raises NotImplementedError (inject() is implemented in Phase 8)."""

    def test_inject_implemented_phase_8(self):
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment={
                "leases": [
                    {
                        "provider": {"hostUri": "https://provider.example.com:8443"},
                        "status": {"services": {"web": {}}},
                    }
                ]
            },
        )
        transport = LeaseShellTransport(config)
        transport._provider_host_uri = "https://provider.example.com:8443"
        transport._service = "web"
        from unittest.mock import patch

        with (
            patch.object(transport, "exec", side_effect=[0, 0]),
            patch.object(transport, "_exec_shell_command", return_value=0),
            patch.object(transport, "_exec_with_stdin_command", return_value=0),
        ):
            transport.inject("/tmp/file", "content")

    def test_connect_does_not_raise_not_implemented(self):
        from unittest.mock import patch as _patch

        config = TransportConfig(dseq="123", api_key="key")
        transport = LeaseShellTransport(config)
        transport._provider_host_uri = "https://provider.example.com"
        transport._service = "web"
        with (
            _patch(
                "just_akash.transport.lease_shell.LeaseShellTransport._run_interactive_session"
            ),
            _patch("termios.tcgetattr", return_value=[]),
            _patch("termios.tcsetattr"),
            _patch("tty.setraw"),
            _patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True
            mock_stdin.fileno.return_value = 0
            transport.connect()


# --- Token Refresh Tests ---


class TestTokenRefresh:
    """Test token-expiry reconnect logic in exec()."""

    def test_exec_reconnects_on_token_expiry(self):
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        transport.prepare()

        with (
            patch.object(transport, "_fetch_jwt", return_value="jwt-token") as mock_fetch_jwt,
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):

            class FakeWSAuthExpiry:
                def recv(self, timeout=None):
                    raise make_close_error(4001)

                def send(self, data):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

            fake_ws_ok = FakeWebSocket(
                [
                    bytes([100]) + b"output\n",
                    bytes([102]) + (0).to_bytes(4, "little"),
                ]
            )

            mock_connect.side_effect = [FakeWSAuthExpiry(), fake_ws_ok]

            exit_code = transport.exec("test cmd")

        assert exit_code == 0
        assert mock_fetch_jwt.call_count == 2
        assert mock_connect.call_count == 2

    def test_exec_reconnects_on_expired_message(self):
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        transport.prepare()

        with (
            patch.object(transport, "_fetch_jwt", return_value="jwt") as mock_fetch_jwt,
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):

            class FakeWSExpired:
                def recv(self, timeout=None):
                    raise make_close_error(1000, "token expired")

                def send(self, data):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

            fake_ws_ok = FakeWebSocket(
                [
                    bytes([102]) + (2).to_bytes(4, "little"),
                ]
            )

            mock_connect.side_effect = [FakeWSExpired(), fake_ws_ok]

            exit_code = transport.exec("test")

        assert exit_code == 2
        assert mock_fetch_jwt.call_count == 2

    def test_exec_raises_after_max_reconnect_attempts(self):
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        transport.prepare()

        with (
            patch.object(transport, "_fetch_jwt", return_value="jwt") as mock_fetch_jwt,
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):

            class FakeWSAuthExpiry:
                def recv(self, timeout=None):
                    raise make_close_error(4001)

                def send(self, data):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

            mock_connect.side_effect = [FakeWSAuthExpiry() for _ in range(10)]

            with pytest.raises(RuntimeError, match="Failed to re-authenticate"):
                transport.exec("test")

        from just_akash.transport.lease_shell import MAX_RECONNECT_ATTEMPTS

        assert mock_fetch_jwt.call_count == MAX_RECONNECT_ATTEMPTS

    def test_exec_non_auth_close_propagates(self):
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment=DEPLOYMENT_FIXTURE,
        )
        transport = LeaseShellTransport(config)
        transport.prepare()

        with (
            patch.object(transport, "_fetch_jwt", return_value="jwt") as mock_fetch_jwt,
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):

            class FakeWSAbnormal:
                def recv(self, timeout=None):
                    raise make_close_error(1006)

                def send(self, data):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

            mock_connect.return_value = FakeWSAbnormal()

            with pytest.raises(ConnectionClosedError):
                transport.exec("test")

        assert mock_fetch_jwt.call_count == 1
        assert mock_connect.call_count == 1

    def test_is_auth_expiry_message(self):
        """Test _is_auth_expiry_message() helper function."""
        # True cases
        assert _is_auth_expiry_message("token expired") is True
        assert _is_auth_expiry_message("unauthorized access") is True
        assert _is_auth_expiry_message("Token Expired") is True
        assert _is_auth_expiry_message("UNAUTHORIZED") is True
        assert _is_auth_expiry_message("jwt expired") is True
        assert _is_auth_expiry_message("session expired") is True

        # False cases — bare "token" without expiry context must NOT match
        assert _is_auth_expiry_message("contains token in message") is False
        assert _is_auth_expiry_message("invalid token format") is False
        assert _is_auth_expiry_message("connection reset") is False
        assert _is_auth_expiry_message("timeout occurred") is False
        assert _is_auth_expiry_message("") is False

    def test_is_auth_expiry_with_close_code(self):
        """Test _is_auth_expiry() detects close codes 4001 and 4003."""
        # Code 4001
        exc_4001 = make_close_error(4001)
        assert _is_auth_expiry(exc_4001) is True

        # Code 4003
        exc_4003 = make_close_error(4003)
        assert _is_auth_expiry(exc_4003) is True

        # Code 1000 (normal) — should return False
        exc_normal = make_close_error(1000)
        assert _is_auth_expiry(exc_normal) is False

    def test_is_auth_expiry_with_reason_string(self):
        """Test _is_auth_expiry() detects auth keywords in reason."""
        exc = make_close_error(1000, "token expired")
        assert _is_auth_expiry(exc) is True

        exc = make_close_error(1000, "unauthorized")
        assert _is_auth_expiry(exc) is True

        exc = make_close_error(1000, "connection reset")
        assert _is_auth_expiry(exc) is False

    def test_is_auth_expiry_with_rcvd_reason_none_returns_false(self):
        """Normal close with reason=None must not be detected as auth expiry.
        Exercises the `or ""` fallback in `getattr(rcvd, "reason", "") or ""`."""
        exc = MagicMock(spec=ConnectionClosedError)
        rcvd = MagicMock()
        rcvd.code = 1000
        rcvd.reason = None
        exc.rcvd = rcvd
        assert _is_auth_expiry(exc) is False

    def test_is_auth_expiry_with_no_rcvd_falls_back_to_str_negative(self):
        """When rcvd is None and str(exc) has no auth keywords, returns False."""
        exc = ConnectionClosedError(rcvd=None, sent=None)
        # str(exc) for rcvd=None says "no close frame received" — no auth keywords
        assert _is_auth_expiry(exc) is False

    def test_is_auth_expiry_with_no_rcvd_falls_back_to_str_positive(self):
        """When rcvd is None and str(exc) contains auth keywords, returns True."""
        exc = MagicMock(spec=ConnectionClosedError)
        exc.rcvd = None
        exc.__str__ = MagicMock(return_value="websocket closed: token expired")
        assert _is_auth_expiry(exc) is True

    def test_dispatch_frame_code_102_json_exit_code_null_returns_zero(self):
        """Code 102 with JSON {"exit_code": null} must return 0, not crash.

        json.loads produces {"exit_code": None}. The code does
        int(json.loads(...).get("exit_code", 0)) which means int(None) is called,
        raising TypeError. The except clause should catch this and fall through
        to the int32 path or default to 0. This test verifies the implementation
        handles null exit_code gracefully.
        """
        payload = json.dumps({"exit_code": None}).encode("utf-8")
        frame = bytes([102]) + payload
        result = LeaseShellTransport._dispatch_frame(frame)
        # null exit_code must be treated as 0
        assert result == 0


class TestColdStdoutRace:
    """Issue #12 -- the provider-proxy can deliver a stdout frame AFTER the result
    (exit-code) frame, so returning the instant the exit code lands drops that
    trailing output (rc=0 with EMPTY stdout, ~5% of execs on some providers).

    _pump_frames must keep draining for a short bounded grace window once the exit
    code is in hand, so every exec caller -- not just the smoke test -- gets the
    trailing output, and must surface a flaky-pass marker so the race stays visible.
    """

    def _pump(self, ws):
        buf = io.BytesIO()
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.buffer = buf
            code = _transport()._pump_frames(ws, 0)
        return code, buf.getvalue()

    def test_trailing_stdout_after_result_is_not_dropped(self):
        """The core regression: STDOUT arriving after RESULT must still be emitted."""
        frames = [
            bytes([102]) + json.dumps({"exit_code": 0}).encode(),  # RESULT first
            bytes([100]) + b"MARKER-OUTPUT",  # stdout in flight, arrives late
        ]
        code, out = self._pump(FakeWebSocket(frames))
        assert code == 0
        assert b"MARKER-OUTPUT" in out

    def test_flaky_pass_marker_reports_recovered_bytes(self, capsys):
        """When the race is caught, a one-line flaky-pass marker hits stderr."""
        frames = [
            bytes([102]) + json.dumps({"exit_code": 0}).encode(),
            bytes([100]) + b"twelve-bytes",  # 12 bytes recovered
        ]
        self._pump(FakeWebSocket(frames))
        err = capsys.readouterr().err
        assert "flaky-pass" in err
        assert "12 byte" in err
        assert "issue #12" in err

    def test_no_marker_on_normal_frame_ordering(self, capsys):
        """Normal order (STDOUT then RESULT) must NOT emit a false flaky-pass."""
        frames = [
            bytes([100]) + b"normal-output",  # stdout BEFORE result -- not the race
            bytes([102]) + json.dumps({"exit_code": 0}).encode(),
        ]
        code, out = self._pump(FakeWebSocket(frames))
        assert code == 0
        assert b"normal-output" in out
        assert "flaky-pass" not in capsys.readouterr().err

    def test_frame_trace_emitted_when_env_set(self, capsys, monkeypatch):
        """With JUST_AKASH_TRACE_FRAMES set, a FRAME-TRACE line records the shape."""
        monkeypatch.setenv("JUST_AKASH_TRACE_FRAMES", "1")
        frames = [
            bytes([100]) + b"hello",
            bytes([102]) + json.dumps({"exit_code": 0}).encode(),
        ]
        self._pump(FakeWebSocket(frames))
        err = capsys.readouterr().err
        assert "FRAME-TRACE" in err
        assert "shape=[stdout,result]" in err
        assert "flaky-pass" not in err  # the trace subsumes the flaky-pass line

    def test_frame_trace_shows_drop_shape(self, capsys, monkeypatch):
        """A result frame with NO preceding stdout is the DROP signature (issue #3438)."""
        monkeypatch.setenv("JUST_AKASH_TRACE_FRAMES", "1")
        frames = [bytes([102]) + json.dumps({"exit_code": 0}).encode()]
        self._pump(FakeWebSocket(frames))
        err = capsys.readouterr().err
        assert "shape=[result]" in err
        assert "stdout_bytes=0" in err

    def test_frame_trace_subsumes_flaky_pass_on_recovered(self, capsys, monkeypatch):
        """A drained reorder rides the trace line (recovered=N), not a separate marker."""
        monkeypatch.setenv("JUST_AKASH_TRACE_FRAMES", "1")
        frames = [
            bytes([102]) + json.dumps({"exit_code": 0}).encode(),
            bytes([100]) + b"late",  # 4 bytes recovered after the result
        ]
        self._pump(FakeWebSocket(frames))
        err = capsys.readouterr().err
        assert "FRAME-TRACE" in err
        assert "recovered=4" in err
        assert "flaky-pass" not in err

    def test_no_frame_trace_without_env(self, capsys, monkeypatch):
        """Default (flag unset): no FRAME-TRACE line -- behaviour is unchanged."""
        monkeypatch.delenv("JUST_AKASH_TRACE_FRAMES", raising=False)
        frames = [
            bytes([100]) + b"hi",
            bytes([102]) + json.dumps({"exit_code": 0}).encode(),
        ]
        self._pump(FakeWebSocket(frames))
        assert "FRAME-TRACE" not in capsys.readouterr().err

    def test_grace_timeout_after_result_returns_cleanly(self):
        """A silent grace window after the exit code returns it -- not a hang error."""
        ws = MagicMock()
        ws.recv.side_effect = [
            bytes([102]) + json.dumps({"exit_code": 7}).encode(),
            TimeoutError(),  # no trailing frame, no close within the grace window
        ]
        assert _transport()._pump_frames(ws, 0) == 7

    def test_silence_before_result_still_raises_hang_diagnosis(self):
        """Before any result frame, silence is a hang and must still be surfaced."""
        ws = MagicMock()
        ws.recv.side_effect = TimeoutError()
        with pytest.raises(RuntimeError, match="sent nothing"):
            _transport()._pump_frames(ws, 0)

    def test_grace_window_uses_config_result_grace_s(self):
        """After the result frame, recv switches to the short result_grace_s timeout."""
        seen_timeouts = []

        def _recv(timeout=None):
            seen_timeouts.append(timeout)
            if len(seen_timeouts) == 1:
                return bytes([102]) + json.dumps({"exit_code": 0}).encode()
            raise ConnectionClosedOK(None, None)

        ws = MagicMock()
        ws.recv.side_effect = _recv
        cfg = TransportConfig(
            dseq="123", api_key="key", deployment=DEPLOYMENT_FIXTURE, result_grace_s=0.05
        )
        LeaseShellTransport(cfg)._pump_frames(ws, 0)
        # First recv uses recv_timeout (pre-result); after RESULT it drops to the
        # grace budget (min(result_grace_s, remaining) -> at or just under 0.05).
        assert seen_timeouts[0] == 300.0
        assert 0 < seen_timeouts[1] <= 0.05

    def test_total_drain_budget_bounds_dribbling_proxy(self):
        """A proxy that keeps sending ignored frames after the result must not
        stretch the drain past the total result_grace_s budget (per-recv silence
        alone would never trip). The monotonic deadline cuts it off."""
        result_frame = bytes([102]) + json.dumps({"exit_code": 3}).encode()
        filler = bytes([255]) + b"x"  # unknown code -> _dispatch_frame yields None

        class DribblingWS:
            """Never closes, never goes silent -- only the deadline can stop it."""

            def __init__(self):
                self.calls = 0

            def recv(self, timeout=None):
                self.calls += 1
                return result_frame if self.calls == 1 else filler

        # monotonic: base when the result lands, then advance past deadline (+0.05).
        clock = iter([100.0, 100.0, 100.02, 100.04, 100.06, 100.08, 100.10])
        cfg = TransportConfig(
            dseq="123", api_key="key", deployment=DEPLOYMENT_FIXTURE, result_grace_s=0.05
        )
        with patch("just_akash.transport.lease_shell.time.monotonic", lambda: next(clock)):
            code = LeaseShellTransport(cfg)._pump_frames(DribblingWS(), 0)
        assert code == 3
