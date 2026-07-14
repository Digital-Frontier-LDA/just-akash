"""Tests for LeaseShellTransport streaming: logs and kube events.

Covers message formatters, URL builders, and stream_logs/stream_events
end-to-end against a fake WebSocket (with proxy-envelope unwrapping).
"""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

from just_akash.transport.base import TransportConfig
from just_akash.transport.lease_shell import LeaseShellTransport


def _auth_expiry_close() -> ConnectionClosedError:
    """A close that _is_auth_expiry recognizes (provider auth code 4001)."""
    return ConnectionClosedError(rcvd=Close(code=4001, reason="token expired"), sent=None)


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


def _proxy_envelope_text(text: str) -> str:
    """Envelope whose ``message`` is a plain (non-base64) JSON/text string — the
    shape Digital Frontier providers use to stream logs/events."""
    return json.dumps({"type": "data", "message": text})


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


class TestJsonTextFrameFallback:
    """DF providers stream logs/events as JSON *text*, not base64. Those frames
    must be surfaced (text_fallback), not discarded as 'undecodable' — while exec
    (base64 binary stdout) must still reject a non-base64 frame."""

    def test_decode_payload_text_fallback_returns_raw(self):
        assert LeaseShellTransport._decode_payload('{"a":1}') is None
        assert LeaseShellTransport._decode_payload('{"a":1}', text_fallback=True) == b'{"a":1}'

    def test_decode_payload_base64_wins_regardless_of_flag(self):
        enc = base64.b64encode(b"hello").decode()
        assert LeaseShellTransport._decode_payload(enc) == b"hello"
        assert LeaseShellTransport._decode_payload(enc, text_fallback=True) == b"hello"

    def test_stream_logs_surfaces_json_text_frame(self, capsys):
        # The exact shape captured live from a DF provider.
        t = _make_transport()
        frames = [_proxy_envelope_text('{"name":"diag","message":"diag-http-up"}')]
        with (
            patch.object(t, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            mock_connect.return_value = FakeWebSocket(frames)
            t.stream_logs()
        assert "[diag] diag-http-up" in capsys.readouterr().out

    def test_stream_events_surfaces_json_text_frame(self, capsys):
        t = _make_transport()
        ev = (
            '{"type":"Normal","reason":"Started","note":"Started container diag",'
            '"object":{"kind":"Pod","name":"diag-x"}}'
        )
        with (
            patch.object(t, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            mock_connect.return_value = FakeWebSocket([_proxy_envelope_text(ev)])
            t.stream_events()
        out = capsys.readouterr().out
        assert "Started" in out and "diag" in out

    def test_exec_recv_still_discards_non_base64(self):
        # exec keeps text_fallback=False: a non-base64 frame must not be dispatched
        # as text (it would corrupt binary stdout) — _recv_proxy_message drops it.
        t = _make_transport()
        ws = MagicMock()
        ws.recv.return_value = _proxy_envelope_text("this is not base64")
        assert t._recv_proxy_message(ws) is None


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


# ── recv-loop robustness (uncaught-crash regression guard) ────────────


class TestRecvProxyMessageRobustness:
    def test_non_object_json_frames_return_none(self):
        t = _make_transport()
        for frame in ["[1, 2, 3]", "null", "42", '"a string"', "true"]:
            ws = FakeWebSocket([frame])
            # Must not raise AttributeError — non-object JSON has no envelope.
            assert t._recv_proxy_message(ws, timeout=1) is None

    def test_malformed_base64_payload_returns_none(self):
        t = _make_transport()
        # "abcde" is not valid base64 (length 5 → padding error / binascii.Error).
        ws = FakeWebSocket([json.dumps({"type": "data", "message": "abcde"})])
        assert t._recv_proxy_message(ws, timeout=1) is None


# ── follow-stream auth-expiry reconnect ───────────────────────────────


class TestStreamReconnect:
    def test_logs_reconnects_on_auth_expiry(self, capsys):
        t = _make_transport()
        # First connection serves a line then closes with an auth-expiry code;
        # the stream should refetch the JWT and reconnect rather than end.
        ws1 = FakeWebSocket([b"line-before-expiry"], close_exc=_auth_expiry_close())
        ws2 = FakeWebSocket([b"line-after-reconnect"])  # ends with a clean close
        with (
            patch.object(t, "_fetch_jwt", return_value="jwt") as mock_jwt,
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            mock_connect.side_effect = [ws1, ws2]
            t.stream_logs(follow=True)
        out = capsys.readouterr().out
        assert "line-before-expiry" in out
        assert "line-after-reconnect" in out
        assert mock_jwt.call_count == 2  # reconnected with a fresh token

    def test_non_auth_close_ends_stream_without_reconnect(self, capsys):
        t = _make_transport()
        ws = FakeWebSocket([b"only-line"], close_exc=ConnectionClosedError(rcvd=None, sent=None))
        with (
            patch.object(t, "_fetch_jwt", return_value="jwt") as mock_jwt,
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            mock_connect.return_value = ws
            t.stream_logs(follow=True)
        assert "only-line" in capsys.readouterr().out
        assert mock_jwt.call_count == 1  # no reconnect on a non-auth close

    def test_raises_when_auth_expiry_exhausts_reconnects(self, capsys):
        # Every connection closes on auth-expiry; after MAX_RECONNECT_ATTEMPTS
        # the stream must fail loudly rather than return silently.
        from just_akash.transport.lease_shell import MAX_RECONNECT_ATTEMPTS

        t = _make_transport()
        wss = [
            FakeWebSocket([f"line-{i}".encode()], close_exc=_auth_expiry_close())
            for i in range(MAX_RECONNECT_ATTEMPTS)
        ]
        with (
            patch.object(t, "_fetch_jwt", return_value="jwt") as mock_jwt,
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            mock_connect.side_effect = wss
            with pytest.raises(RuntimeError, match="Failed to re-authenticate stream"):
                t.stream_logs(follow=True)
        assert mock_jwt.call_count == MAX_RECONNECT_ATTEMPTS


# ── bounded snapshot (--duration) ────────────────────────────────────


class TestSnapshotDuration:
    """``duration`` bounds a stream client-side so a provider that keeps a
    non-follow logs/events connection open (instead of closing it after the
    tail replay) can no longer hang the client until the 300s recv timeout.
    """

    def test_duration_returns_on_a_silent_stream(self):
        import time as _time

        t = _make_transport()

        class SilentWebSocket:
            """recv() waits out its timeout then raises TimeoutError — models a
            provider that never sends a frame and never closes the socket."""

            def __init__(self):
                self.timeouts: list = []
                self.sent_messages: list = []

            def recv(self, timeout=None):
                self.timeouts.append(timeout)
                if timeout:
                    _time.sleep(min(timeout, 0.5))
                raise TimeoutError

            def send(self, data):
                self.sent_messages.append(data)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def close(self):
                pass

        ws = SilentWebSocket()
        with (
            patch.object(t, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect", return_value=ws),
        ):
            start = _time.monotonic()
            t.stream_events(duration=0.2)  # must RETURN, not hang
            elapsed = _time.monotonic() - start

        assert elapsed < 5.0, f"duration bound failed — stream ran {elapsed:.2f}s"
        # Every recv was bounded by the remaining window, never the 300s default.
        assert ws.timeouts, "recv was never called"
        assert max(ws.timeouts) <= 0.2 + 1e-6, ws.timeouts

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), 0.0, -1.0])
    def test_non_finite_or_nonpositive_duration_is_rejected_before_connecting(self, bad):
        """A non-finite duration would silently defeat the bound and reintroduce the
        hang: NaN makes every ``>= deadline`` comparison false, inf sets no real
        deadline. The guard must reject it fast, at the API boundary, before any
        socket work — so a programmatic caller can't disable the cutoff by accident.
        """
        t = _make_transport()
        with (
            patch.object(t, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect") as mock_connect,
        ):
            with pytest.raises(ValueError, match="finite number > 0"):
                t.stream_logs(duration=bad)
            with pytest.raises(ValueError, match="finite number > 0"):
                t.stream_events(duration=bad)
            mock_connect.assert_not_called()  # rejected before touching the network

    def test_no_duration_uses_full_recv_timeout(self):
        """Without ``duration`` the per-recv timeout stays at the 300s default
        and the loop relies on the server closing — legacy behavior unchanged."""
        t = _make_transport()
        seen: list = []
        fake_ws = FakeWebSocket([b"one", b"two"])
        orig_recv = fake_ws.recv

        def recording_recv(timeout=None):
            seen.append(timeout)
            return orig_recv(timeout=timeout)

        fake_ws.recv = recording_recv
        with (
            patch.object(t, "_fetch_jwt", return_value="jwt"),
            patch("just_akash.transport.lease_shell.connect", return_value=fake_ws),
        ):
            t.stream_logs(follow=True)  # closes cleanly once frames are exhausted
        assert seen and seen[0] == 300
