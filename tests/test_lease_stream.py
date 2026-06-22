"""Tests for LeaseShellTransport streaming: logs and kube events.

Covers message formatters, URL builders, and stream_logs/stream_events
end-to-end against a fake WebSocket (with proxy-envelope unwrapping).
"""

import base64
import json
from unittest.mock import MagicMock, patch

from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from just_akash.transport.base import TransportConfig
from just_akash.transport.lease_shell import LeaseShellTransport

DEPLOYMENT_FIXTURE = {
    "leases": [
        {
            "provider": {"hostUri": "https://provider.us-east.akash.pub:8443"},
            "status": {"services": {"web": {"ready_replicas": 1, "total": 1}}},
        }
    ]
}


class FakeWebSocket:
    """Serves pre-built frames then closes (ConnectionClosedOK)."""

    def __init__(self, frames, close_exc=None):
        self._frames = iter(frames)
        self.sent_messages: list = []
        self._close_exc = close_exc or ConnectionClosedOK(None, None)

    def recv(self, timeout=None):
        try:
            return next(self._frames)
        except StopIteration:
            raise self._close_exc from None

    def send(self, data):
        self.sent_messages.append(data)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def close(self):
        pass


def _make_transport():
    config = TransportConfig(dseq="123", api_key="key", deployment=DEPLOYMENT_FIXTURE)
    return LeaseShellTransport(config)


def _proxy_envelope(payload: bytes) -> str:
    """Wrap raw provider bytes in the proxy JSON envelope (base64 message)."""
    return json.dumps(
        {"type": "data", "message": {"data": base64.b64encode(payload).decode("ascii")}}
    )


# ── log message formatter ────────────────────────────────────────────


class TestFormatLogMessage:
    def test_json_with_name_and_message(self):
        raw = json.dumps({"name": "web", "message": "ready to serve"}).encode()
        assert LeaseShellTransport._format_log_message(raw) == "[web] ready to serve"

    def test_json_message_without_name(self):
        raw = json.dumps({"message": "no service"}).encode()
        assert LeaseShellTransport._format_log_message(raw) == "no service"

    def test_json_service_key_alias(self):
        raw = json.dumps({"service": "db", "msg": "up"}).encode()
        assert LeaseShellTransport._format_log_message(raw) == "[db] up"

    def test_raw_text_passthrough(self):
        assert LeaseShellTransport._format_log_message(b"plain log line\n") == "plain log line"

    def test_brace_prefixed_but_invalid_json_passthrough(self):
        # Looks like JSON but isn't — must not crash, returns verbatim.
        assert LeaseShellTransport._format_log_message(b"{not json") == "{not json"

    def test_trailing_newline_stripped(self):
        raw = json.dumps({"name": "web", "message": "line"}).encode() + b"\n"
        assert LeaseShellTransport._format_log_message(raw) == "[web] line"

    def test_json_dict_without_known_keys_is_not_swallowed(self):
        # A structured JSON log line with none of name/service/message/msg
        # (e.g. a JSON-logger emitting {"level":"info","ts":...}) is real log
        # output. The formatter computes name="" and msg="" then renders
        # str("") == "", so the entire entry is replaced by a blank line —
        # silent data loss. A faithful copy must surface the original payload.
        raw = json.dumps({"level": "info", "ts": 1716393600}).encode()
        out = LeaseShellTransport._format_log_message(raw)
        assert out != ""
        assert "info" in out


# ── event message formatter ──────────────────────────────────────────


class TestFormatEventMessage:
    def test_full_event(self):
        raw = json.dumps(
            {
                "type": "Normal",
                "reason": "Scheduled",
                "message": "assigned to node-3",
                "involvedObject": {"kind": "Pod", "name": "web-1"},
                "lastTimestamp": "2026-05-22T16:12:01Z",
            }
        ).encode()
        out = LeaseShellTransport._format_event_message(raw)
        assert "Normal" in out
        assert "Scheduled" in out
        assert "Pod/web-1" in out
        assert "assigned to node-3" in out
        assert "2026-05-22T16:12:01Z" in out

    def test_object_alias_and_note(self):
        raw = json.dumps(
            {
                "type": "Warning",
                "reason": "Failed",
                "note": "img pull",
                "object": {"kind": "Pod", "name": "p"},
            }
        ).encode()
        out = LeaseShellTransport._format_event_message(raw)
        assert "Warning" in out and "Failed" in out and "Pod/p" in out and "img pull" in out

    def test_invalid_json_passthrough(self):
        assert LeaseShellTransport._format_event_message(b"not json") == "not json"

    def test_non_dict_json_passthrough(self):
        assert LeaseShellTransport._format_event_message(b"[1, 2]") == "[1, 2]"

    def test_partial_event_no_object(self):
        raw = json.dumps({"type": "Normal", "reason": "Pulled"}).encode()
        out = LeaseShellTransport._format_event_message(raw)
        assert out == "Normal  Pulled"

    def test_numeric_message_zero_is_not_silently_dropped(self):
        # An event whose `message` is the number 0 (a valid JSON value) must
        # still appear in the rendered line. The formatter uses
        # `obj.get("message") or obj.get("note") or ""`, so a falsy-but-present
        # numeric message (0) is discarded — silent data loss.
        raw = json.dumps(
            {
                "type": "Normal",
                "reason": "Probe",
                "involvedObject": {"kind": "Pod", "name": "p"},
                "message": 0,
            }
        ).encode()
        out = LeaseShellTransport._format_event_message(raw)
        assert "0" in out


# ── URL builders ─────────────────────────────────────────────────────


class TestBuildLogsUrl:
    def test_defaults(self):
        t = _make_transport()
        t._provider_host_uri = "https://p.com"
        url = t._build_logs_url()
        assert url == "https://p.com/lease/123/1/1/logs?follow=false&tail=100"

    def test_follow_and_tail(self):
        t = _make_transport()
        t._provider_host_uri = "https://p.com"
        url = t._build_logs_url(follow=True, tail=50)
        assert "follow=true" in url and "tail=50" in url

    def test_service_url_encoded(self):
        t = _make_transport()
        t._provider_host_uri = "https://p.com"
        url = t._build_logs_url(service="my svc")
        assert "service=my%20svc" in url

    def test_no_service_param_when_absent(self):
        t = _make_transport()
        t._provider_host_uri = "https://p.com"
        assert "service=" not in t._build_logs_url()


class TestBuildEventsUrl:
    def test_path(self):
        t = _make_transport()
        t._provider_host_uri = "https://p.com"
        assert t._build_events_url() == "https://p.com/lease/123/1/1/kubeevents"


# ── stream_logs end-to-end ───────────────────────────────────────────


class TestStreamLogs:
    def test_prints_raw_byte_frames(self, capsys):
        t = _make_transport()
        frames = [b'{"name":"web","message":"hello"}', b"plain line"]
        with (
            patch.object(t, "_fetch_jwt", return_value="jwt") as mock_jwt,
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            fake_ws = FakeWebSocket(frames)
            mock_connect.return_value = fake_ws
            t.stream_logs(follow=False, tail=10)
        out = capsys.readouterr().out
        assert "[web] hello" in out
        assert "plain line" in out
        # JWT requested with the logs scope.
        assert mock_jwt.call_args.kwargs["scope"] == ["logs"]
        # The provider URL embedded in the proxy connect message targets /logs.
        connect_msg = json.loads(fake_ws.sent_messages[0])
        assert "/lease/123/1/1/logs" in connect_msg["url"]
        assert "tail=10" in connect_msg["url"]

    def test_unwraps_proxy_envelope(self, capsys):
        t = _make_transport()
        frames = [_proxy_envelope(b'{"name":"api","message":"served"}')]
        with (
            patch.object(t, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            mock_connect.return_value = FakeWebSocket(frames)
            t.stream_logs()
        assert "[api] served" in capsys.readouterr().out

    def test_follow_passes_through_to_url(self, capsys):
        t = _make_transport()
        with (
            patch.object(t, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            fake_ws = FakeWebSocket([b"x"])
            mock_connect.return_value = fake_ws
            t.stream_logs(follow=True, tail=5)
        connect_msg = json.loads(fake_ws.sent_messages[0])
        assert "follow=true" in connect_msg["url"]


# ── stream_events end-to-end ─────────────────────────────────────────


class TestStreamEvents:
    def test_prints_events_and_uses_events_scope(self, capsys):
        t = _make_transport()
        frames = [
            json.dumps(
                {
                    "type": "Normal",
                    "reason": "Started",
                    "involvedObject": {"kind": "Pod", "name": "p"},
                }
            ).encode()
        ]
        with (
            patch.object(t, "_fetch_jwt", return_value="jwt") as mock_jwt,
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            fake_ws = FakeWebSocket(frames)
            mock_connect.return_value = fake_ws
            t.stream_events()
        out = capsys.readouterr().out
        assert "Started" in out and "Pod/p" in out
        assert mock_jwt.call_args.kwargs["scope"] == ["events"]
        connect_msg = json.loads(fake_ws.sent_messages[0])
        assert "/lease/123/1/1/kubeevents" in connect_msg["url"]


# ── provider-scoped JWT carries the iteration's scope ────────────────


class TestStreamProviderScopedJwt:
    def test_logs_provider_scoped_jwt_uses_logs_scope_not_shell(self, capsys):
        """When the lease exposes only a provider ADDRESS (id.provider) and no
        embedded hostUri, _resolve_provider sets _provider_address and resolves
        the hostUri via get_provider. _fetch_jwt must then route through
        create_jwt_with_provider carrying scope=["logs"] (the streaming scope),
        NOT the default ["shell"]. This whole branch is unexercised by the other
        tests, which mock _fetch_jwt out entirely.
        """
        deployment = {
            "leases": [
                {
                    "id": {"provider": "akash1prov"},
                    "status": {"services": {"web": {"ready_replicas": 1, "total": 1}}},
                }
            ]
        }
        cfg = TransportConfig(dseq="123", api_key="key", deployment=deployment)
        t = LeaseShellTransport(cfg)

        fake_client = MagicMock()
        fake_client.get_provider.return_value = {"hostUri": "https://p.example:8443"}
        fake_client.create_jwt_with_provider.return_value = "jwt-prov"

        with (
            patch.object(t, "_get_api_client", return_value=fake_client),
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            mock_connect.return_value = FakeWebSocket([b"line"])
            t.stream_logs(follow=False, tail=10)

        # Provider-scoped JWT path was taken (address present, no embedded hostUri).
        assert fake_client.create_jwt_with_provider.called
        assert not fake_client.create_jwt.called
        _, kwargs = fake_client.create_jwt_with_provider.call_args
        assert kwargs["scope"] == ["logs"], (
            f"provider-scoped JWT must carry the logs scope, got {kwargs.get('scope')!r}"
        )
        # And the resolved hostUri (from get_provider) is embedded in the URL.
        connect_msg = json.loads(mock_connect.return_value.sent_messages[0])
        assert connect_msg["url"].startswith("https://p.example:8443/lease/123/1/1/logs")
        assert connect_msg["providerAddress"] == "akash1prov"


# ── _resolve_provider host-uri fallback ──────────────────────────────


class TestResolveProviderHostUriFallback:
    def test_blank_embedded_hosturi_falls_back_to_provider_registry(self):
        """An empty embedded ``provider.hostUri`` must NOT be returned as a usable
        URL; with a valid ``id.provider`` address present, _resolve_provider must
        fall through to the provider-registry lookup (get_provider) and use that
        hostUri instead. A blank hostUri short-circuiting here would yield a
        proxy URL like ``/lease/...`` with no host, silently breaking the stream.
        """
        deployment = {"leases": [{"id": {"provider": "akash1x"}, "provider": {"hostUri": ""}}]}
        cfg = TransportConfig(dseq="123", api_key="key", deployment=deployment)
        t = LeaseShellTransport(cfg)

        fake_client = MagicMock()
        fake_client.get_provider.return_value = {"hostUri": "https://resolved.example:8443"}

        with patch.object(t, "_get_api_client", return_value=fake_client):
            host = t._resolve_provider()

        # The blank embedded hostUri is rejected; the registry value wins.
        assert host == "https://resolved.example:8443"
        assert t._provider_host_uri == "https://resolved.example:8443"
        # The lease's id.provider address was captured and used for the lookup.
        assert t._provider_address == "akash1x"
        fake_client.get_provider.assert_called_once_with("akash1x")


# ── _stream resilience ───────────────────────────────────────────────


class TestStreamResilience:
    def test_timeout_then_data_then_close(self, capsys):
        """A TimeoutError mid-stream must not end the stream (follow semantics)."""
        t = _make_transport()
        t._provider_host_uri = "https://p.com"

        class TimeoutThenData(FakeWebSocket):
            def __init__(self):
                super().__init__([])
                self._step = 0

            def recv(self, timeout=None):
                self._step += 1
                if self._step == 1:
                    raise TimeoutError()
                if self._step == 2:
                    return b"after timeout"
                raise ConnectionClosedOK(None, None)

        with (
            patch.object(t, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            mock_connect.return_value = TimeoutThenData()
            t._stream("https://p.com/lease/123/1/1/logs?", ["logs"], t._format_log_message, 1)
        assert "after timeout" in capsys.readouterr().out

    def test_connection_closed_error_ends_cleanly(self, capsys):
        t = _make_transport()
        t._provider_host_uri = "https://p.com"
        fake_ws = FakeWebSocket([b"one"], close_exc=ConnectionClosedError(rcvd=None, sent=None))
        with (
            patch.object(t, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            mock_connect.return_value = fake_ws
            t._stream("https://p.com/lease/123/1/1/logs?", ["logs"], t._format_log_message, 1)
        assert "one" in capsys.readouterr().out

    def test_none_frames_skipped(self, capsys):
        """ping/pong frames decode to None and must be skipped, not printed."""
        t = _make_transport()
        t._provider_host_uri = "https://p.com"
        frames = [json.dumps({"type": "ping"}), b"real"]
        with (
            patch.object(t, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            mock_connect.return_value = FakeWebSocket(frames)
            t._stream("https://p.com/lease/123/1/1/logs?", ["logs"], t._format_log_message, 1)
        out = capsys.readouterr().out
        assert "real" in out
        assert "ping" not in out

    def test_blank_log_line_between_two_lines_is_preserved(self, capsys):
        """A genuinely empty log line (container prints a blank line) must be
        emitted so the stream is a faithful copy. `_stream` guards with
        `if line:`, so a frame that formats to "" is silently dropped, and the
        two surrounding lines collapse together — losing the blank separator.
        """
        t = _make_transport()
        t._provider_host_uri = "https://p.com"
        # Middle frame is a bare newline -> formats to "" (a real blank line).
        frames = [b"before", b"\n", b"after"]
        with (
            patch.object(t, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            mock_connect.return_value = FakeWebSocket(frames)
            t._stream("https://p.com/lease/123/1/1/logs?", ["logs"], t._format_log_message, 1)
        out = capsys.readouterr().out
        # Faithful output keeps the blank line between the two log lines.
        assert out == "before\n\nafter\n"
