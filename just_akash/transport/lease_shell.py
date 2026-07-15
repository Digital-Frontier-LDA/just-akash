"""
Lease-shell WebSocket transport via Console Provider-Proxy (Phase 7).

Connects to the Akash Console provider-proxy (wss://provider-proxy.akash.network/)
which relays WebSocket frames to the target provider. Uses JWT auth obtained from
the Console API.

Protocol reference: docs/PROTOCOL.md
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import logging
import math
import os
import select
import shlex
import signal
import ssl
import struct
import sys
import termios
import time
import tty
import urllib.parse

from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.sync.client import connect

from just_akash.api import AkashConsoleAPI

from .base import Transport, TransportConfig

MAX_RECONNECT_ATTEMPTS = 3

# Seconds of total silence from the provider-proxy before an exec gives up. A
# command can legitimately be quiet for a long time (a slow build, a sleep), so
# this is generous; it exists to guarantee we fail with a diagnosis rather than
# block forever.
PROXY_RECV_TIMEOUT = 300.0

_FRAME_STDOUT = 100
_FRAME_STDERR = 101
_FRAME_RESULT = 102
_FRAME_FAILURE = 103
_FRAME_STDIN = 104
_FRAME_RESIZE = 105


def _is_auth_expiry_message(msg: str) -> bool:
    lower = msg.lower()
    return (
        "expired" in lower
        or "unauthorized" in lower
        or "jwt expired" in lower
        or "token expired" in lower
    )


def _is_auth_expiry(exc: ConnectionClosedError) -> bool:
    rcvd = getattr(exc, "rcvd", None)
    if rcvd is not None:
        code = getattr(rcvd, "code", None)
        if code in (4001, 4003):
            return True
        reason = getattr(rcvd, "reason", "") or ""
        if _is_auth_expiry_message(reason):
            return True
    return _is_auth_expiry_message(str(exc))


_logger = logging.getLogger(__name__)


class LeaseShellTransport(Transport):
    """WebSocket-based lease-shell transport via Console Provider-Proxy.

    Connects to provider-proxy which relays to the actual provider.
    """

    def __init__(self, config: TransportConfig) -> None:
        self._config = config
        self._provider_host_uri: str | None = None
        self._service: str | None = None
        self._provider_address: str | None = None
        self._api_client: AkashConsoleAPI | None = None
        self._ws = None

    def _get_api_client(self) -> AkashConsoleAPI:
        if self._api_client is None:
            self._api_client = AkashConsoleAPI(
                api_key=self._config.api_key,
                base_url=self._config.console_url,
            )
        return self._api_client

    def _fetch_jwt(self, ttl: int = 3600, scope: list[str] | None = None) -> str:
        if self._provider_address:
            return self._get_api_client().create_jwt_with_provider(
                self._config.dseq, self._provider_address, ttl=ttl, scope=scope
            )
        return self._get_api_client().create_jwt(self._config.dseq, ttl=ttl, scope=scope)

    def _resolve_provider(self) -> str:
        """Resolve the provider address + hostUri from lease data.

        Sets ``self._provider_address`` and ``self._provider_host_uri`` and
        returns the hostUri. Unlike ``_extract_provider_info`` this does NOT
        require a service name — used by streaming endpoints (logs, events)
        that operate at the lease level.
        """
        leases = self._config.deployment.get("leases", [])
        if not leases or not isinstance(leases, list):
            raise RuntimeError(
                f"No leases found for deployment {self._config.dseq}. "
                "The deployment may not have an active lease yet."
            )
        lease = leases[0]
        if not isinstance(lease, dict):
            raise RuntimeError("Unexpected lease entry format in deployment data.")

        lease_id = lease.get("id")
        if lease_id is not None and not isinstance(lease_id, dict):
            raise RuntimeError("Unexpected lease id format in deployment data.")
        provider_addr = lease_id.get("provider", "") if isinstance(lease_id, dict) else ""
        if provider_addr:
            self._provider_address = provider_addr

        return self._resolve_host_uri(lease, provider_addr)

    def _extract_provider_info(self) -> tuple[str, str]:
        host_uri = self._resolve_provider()

        service = self._config.service_name
        if not service:
            # Inference silently returns the FIRST reported service. On a
            # multi-service deployment that is an arbitrary choice the caller never
            # made -- ours has six, and "exec into whichever one happens to be first"
            # is a footgun, not a feature. Keep it (removing it would break every
            # existing single-service caller) but make it VISIBLE, and name the
            # escape hatch. Explicit --service skips this entirely.
            known = self._known_services()
            if len(known) > 1:
                _logger.warning(
                    "Deployment %s reports %d services (%s); none was chosen, so "
                    "falling back to the first one reported. Pass --service <name> "
                    "to choose deliberately.",
                    self._config.dseq,
                    len(known),
                    ", ".join(sorted(known)),
                )
            service = self._infer_service()

        if not service:
            raise RuntimeError(
                f"Deployment {self._config.dseq} has not reported any service in its "
                "lease status yet, so the target container cannot be inferred. This "
                "usually means the deployment is still starting -- but note the "
                "Console API populates lease.status.services LAZILY, so it can stay "
                "empty even after a container is demonstrably running. If you know "
                "which container you want, pass --service <name> (CLI) or "
                "service_name (TransportConfig) to skip inference entirely."
            )
        self._service = service
        return host_uri, service

    def _resolve_host_uri(self, lease: dict, provider_addr: str) -> str:
        provider = lease.get("provider", {})
        if isinstance(provider, dict):
            host_uri = provider.get("hostUri") or provider.get("host_uri")
            if host_uri:
                self._provider_host_uri = host_uri
                return host_uri

        if not provider_addr:
            raise RuntimeError(
                "Cannot resolve provider hostUri: no provider address found in lease data."
            )

        provider_data = self._get_api_client().get_provider(provider_addr)
        if provider_data and isinstance(provider_data, dict):
            host_uri = provider_data.get("hostUri")
            if host_uri:
                self._provider_host_uri = host_uri
                return host_uri

        raise RuntimeError(
            f"Could not resolve provider hostUri for {provider_addr}. "
            "Ensure the provider is registered and the API is accessible."
        )

    def _known_services(self) -> list[str]:
        """Service names the lease currently reports (may be empty).

        Called from two places in _extract_provider_info: to decide whether to warn
        that inference is about to pick arbitrarily among several services, and to
        shape the error when none are reported at all.

        Deliberately tolerant of a malformed payload (returns [] rather than raising),
        for one reason: it must never disagree with _infer_service(), which walks the
        SAME fields (leases -> lease -> status -> services) with the SAME tolerance. If
        this raised where _infer_service() quietly returns None, the two would tell
        different stories about what the lease says -- a worse bug than either. So a
        malformed payload degrades exactly like an empty one: no services are known,
        inference yields nothing, and the caller raises its own precise, actionable
        error ("has not reported any service ... pass --service").

        Strict payload validation is a reasonable thing to want, but it belongs where
        the payload ENTERS (the API client), applied once to both readers -- not
        bolted onto one of two functions that must stay in agreement.
        """
        leases = self._config.deployment.get("leases", [])
        if not leases:
            return []
        lease = leases[0] if isinstance(leases, list) else {}
        status = lease.get("status", {}) if isinstance(lease, dict) else {}
        services = status.get("services", {}) if isinstance(status, dict) else {}
        return list(services) if isinstance(services, dict) else []

    def _infer_service(self) -> str | None:
        leases = self._config.deployment.get("leases", [])
        if not leases:
            return None
        lease = leases[0] if isinstance(leases, list) else {}
        status = lease.get("status", {}) if isinstance(lease, dict) else {}
        services = status.get("services", {}) if isinstance(status, dict) else {}
        if isinstance(services, dict) and services:
            return next(iter(services))
        return None

    def _build_provider_shell_url(
        self, command: str | None = None, tty: bool = False, stdin: bool = False
    ) -> str:
        assert self._provider_host_uri is not None  # noqa: S101 type-narrowing
        dseq = self._config.dseq
        qs_parts = [
            "podIndex=0",
            f"service={urllib.parse.quote(self._service or '', safe='')}",
            # The provider parses these as "1"/"0", NOT "true"/"false": it checks for
            # the literal "1", so "true" reads as OFF. Sending "true" meant tty=1
            # requests never got a PTY (`tty` reports "not a tty") and stdin=1 never
            # opened an input stream -- which is why interactive `connect` couldn't
            # allocate a terminal or receive typed input. Confirmed against both a
            # df provider and an upstream v0.14.2 provider: tty=1 yields /dev/pts/0,
            # tty=true yields "not a tty".
            f"tty={'1' if tty else '0'}",
            f"stdin={'1' if stdin else '0'}",
        ]
        if command is not None:
            # shlex.split, not command.split(" ").
            #
            # The provider shell takes the command as a list of argv parts (cmd0,
            # cmd1, ...). Splitting naively on spaces ignores shell quoting, so any
            # command carrying a quoted argument was silently shredded:
            #
            #   sh -c "df -h / && echo ok"
            #     -> ['sh', '-c', '"df', '-h', '/', '&&', 'echo', 'ok"']
            #
            # and the remote shell got `"df` as one argv and died with
            # `Syntax error: Unterminated quoted string`. That is every non-trivial
            # command -- anything with a `sh -c '...'` wrapper, which is how you run
            # more than one thing. shlex.split honours the quoting and yields the argv
            # the caller actually wrote. It also drops the empty strings that
            # consecutive spaces used to produce (which became empty cmdN params).
            try:
                parts = shlex.split(command)
            except ValueError as exc:  # unbalanced quotes in the caller's command
                raise RuntimeError(
                    f"Could not parse the remote command (unbalanced quotes?): {exc}"
                ) from exc
            for i, part in enumerate(parts):
                qs_parts.append(f"cmd{i}={urllib.parse.quote(part, safe='')}")
        qs = "&".join(qs_parts)
        return f"{self._provider_host_uri}/lease/{dseq}/1/1/shell?{qs}"

    def _build_shell_url_sh_c(
        self, shell_command: str, tty: bool = False, stdin: bool = False
    ) -> str:
        assert self._provider_host_uri is not None  # noqa: S101 type-narrowing
        dseq = self._config.dseq
        qs_parts = [
            "podIndex=0",
            f"service={urllib.parse.quote(self._service or '', safe='')}",
            # "1"/"0", not "true"/"false" -- see _build_provider_shell_url.
            f"tty={'1' if tty else '0'}",
            f"stdin={'1' if stdin else '0'}",
            "cmd0=sh",
            "cmd1=-c",
            f"cmd2={urllib.parse.quote(shell_command, safe='')}",
        ]
        qs = "&".join(qs_parts)
        return f"{self._provider_host_uri}/lease/{dseq}/1/1/shell?{qs}"

    def _build_logs_url(
        self, follow: bool = False, tail: int = 100, service: str | None = None
    ) -> str:
        assert self._provider_host_uri is not None  # noqa: S101 type-narrowing
        dseq = self._config.dseq
        qs_parts = [
            f"follow={'true' if follow else 'false'}",
            f"tail={int(tail)}",
        ]
        if service:
            qs_parts.append(f"service={urllib.parse.quote(service, safe='')}")
        qs = "&".join(qs_parts)
        return f"{self._provider_host_uri}/lease/{dseq}/1/1/logs?{qs}"

    def _build_events_url(self) -> str:
        assert self._provider_host_uri is not None  # noqa: S101 type-narrowing
        dseq = self._config.dseq
        return f"{self._provider_host_uri}/lease/{dseq}/1/1/kubeevents"

    def _build_proxy_connect_msg(
        self, shell_path: str, jwt: str, stdin_data: str | None = None
    ) -> str:
        msg: dict = {
            "type": "websocket",
            "url": shell_path,
            "providerAddress": self._provider_address,
            "auth": {"type": "jwt", "token": jwt},
            "isBase64": True,
        }
        if stdin_data is not None:
            msg["data"] = base64.b64encode(stdin_data.encode("utf-8")).decode("ascii")
        return json.dumps(msg)

    def _get_proxy_ws_url(self) -> str:
        proxy = self._config.provider_proxy_url
        parsed = urllib.parse.urlparse(proxy)
        if parsed.scheme == "https":
            scheme = "wss"
        elif parsed.scheme == "http":
            scheme = "ws"
        else:
            scheme = parsed.scheme
        return urllib.parse.urlunparse(parsed._replace(scheme=scheme))

    @staticmethod
    def _dispatch_frame(frame: bytes) -> int | None:
        if not isinstance(frame, bytes) or len(frame) < 1:
            return None
        code = frame[0]
        payload = frame[1:]
        if code == 100:
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()
        elif code == 101:
            sys.stderr.buffer.write(payload)
            sys.stderr.buffer.flush()
        elif code == 102:
            try:
                parsed = json.loads(payload)
                if isinstance(parsed, dict):
                    exit_code = parsed.get("exit_code", 0)
                    return 0 if exit_code is None else int(exit_code)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            if len(payload) >= 4:
                try:
                    return int.from_bytes(payload[:4], "little")
                except (ValueError, OverflowError):
                    pass
            return 0
        elif code == 103:
            msg = payload.decode("utf-8", errors="replace")
            raise RuntimeError(f"Provider error: {msg}")
        return None

    @staticmethod
    def _is_proxy_error(msg: dict) -> bool:
        """Does this proxy envelope report a failure?

        The proxy does NOT use ``type: "error"`` for errors -- it sends an ordinary
        ``type: "websocket"`` frame carrying an ``error`` key, e.g.

            {"type": "websocket", "message": "Received error from provider websocket",
             "error": "Received error from provider websocket"}

        so the presence of ``error``, not the value of ``type``, is what identifies a
        failure. Both are accepted here: ``type: "error"`` costs nothing to keep and
        guards against the proxy changing its mind.
        """
        return msg.get("type") == "error" or "error" in msg

    @staticmethod
    def _format_proxy_error(msg: dict) -> str:
        """Render a proxy error envelope as one actionable line.

        Schema rejections arrive with a Zod-style ``errors`` list whose entries name
        the offending field (``path``) and the reason -- that detail is the whole
        value of the frame ("auth.token: is not a valid JWT token" tells you what to
        fix; "Invalid message format" does not), so flatten it into the message.
        """
        summary = str(msg.get("message") or msg.get("error") or msg)
        error = str(msg.get("error") or "")
        if error and error != summary:
            summary = f"{summary} ({error})"

        details = []
        for item in msg.get("errors") or []:
            if not isinstance(item, dict):
                details.append(str(item))
                continue
            path = item.get("path")
            where = ".".join(str(p) for p in path) if isinstance(path, list) else ""
            what = str(item.get("message", "")).strip()
            detail = f"{where}: {what}" if where and what else (what or where)
            if detail:
                details.append(detail)
        if details:
            summary = f"{summary} [{'; '.join(details)}]"
        return summary

    @staticmethod
    def _decode_payload(data: str, *, text_fallback: bool = False) -> bytes | None:
        """Strictly base64-decode one relayed payload; None if it isn't decodable.

        ``validate=True`` is load-bearing. The permissive default DISCARDS characters
        outside the base64 alphabet and only then checks the length, so a plain-English
        string decodes to garbage bytes whenever its filtered length happens to be a
        multiple of four -- garbage that would be written straight to stdout as if the
        provider had sent it. Strict mode rejects it instead.

        ``text_fallback`` is set ONLY by the logs/events stream. There the provider
        sends each frame as a JSON ServiceLogMessage / Kubernetes-event object in
        plain text (not base64), so strict decode correctly rejects it -- and we
        must then surface it as raw UTF-8 for the log/event formatter to render,
        not silently drop real output (which blinded `logs`/`events` on providers
        that stream JSON). exec keeps ``text_fallback=False``: its frames are
        genuinely base64 stdout, so a non-base64 frame there is corruption that
        must not be dispatched as text.

        Returning None rather than raising is deliberate: one corrupt frame must not
        tear down a long-running ``logs --follow``. Callers skip it and read on. The
        frame that MUST NOT reach here is a proxy error frame -- _recv_proxy_message
        raises on those first, so silence here can no longer hide a failure.
        """
        try:
            return base64.b64decode(data, validate=True)
        except (ValueError, TypeError):
            if text_fallback:
                # Non-base64 on the logs/events path == the provider streamed the
                # JSON/text content directly; hand it to the formatter verbatim.
                return data.encode("utf-8", "replace")
            # Log only the length, not the payload: an undecodable frame is unexpected
            # data of unknown provenance, and echoing it into logs risks leaking
            # whatever it happens to contain. The size is enough to flag the anomaly.
            _logger.warning(
                "Discarding an undecodable (non-base64) frame from provider-proxy (%d chars)",
                len(data),
            )
            return None

    def _recv_proxy_message(
        self, ws, timeout: float = PROXY_RECV_TIMEOUT, *, text_fallback: bool = False
    ) -> bytes | None:
        raw = ws.recv(timeout=timeout)
        if isinstance(raw, bytes):
            return raw
        if not isinstance(raw, str):
            return None
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return None
        # A valid-JSON text frame need not be an object (could be an array / null /
        # scalar); only objects carry the proxy envelope.
        if not isinstance(msg, dict):
            return None
        if msg.get("type") in ("ping", "pong"):
            return None
        # Check this BEFORE any decoding. An error frame's `message` is human-readable
        # prose, and the decode paths below would try to base64-decode it: the caller
        # would then either spin on a swallowed exception until recv timed out, or
        # dispatch garbage bytes as provider output. Callers rely on this raise --
        # _exec_loop and _stream both catch RuntimeError to drive the auth-expiry
        # reconnect, which is unreachable unless errors actually surface here.
        if self._is_proxy_error(msg):
            raise RuntimeError(f"Proxy error: {self._format_proxy_error(msg)}")

        message = msg.get("message")
        if isinstance(message, dict) and "data" in message:
            data = message["data"]
            if isinstance(data, list):
                return bytes(data)
            if isinstance(data, str):
                return self._decode_payload(data, text_fallback=text_fallback)
        if isinstance(message, str):
            return self._decode_payload(message, text_fallback=text_fallback)
        if isinstance(message, (bytes, bytearray)):
            return bytes(message)
        if isinstance(msg.get("data"), str):
            return self._decode_payload(msg["data"], text_fallback=text_fallback)
        return None

    def _pump_frames(self, ws, exit_code: int) -> int | None:
        """Read frames until the remote command reports its exit code.

        Returns that exit code, or None to tell the caller the JWT expired and the
        session should be re-established. Raises on any proxy or provider error.

        The provider-proxy does not guarantee the result (exit-code) frame is the
        last one on the wire: a stdout frame can still be in flight when the result
        arrives (issue #12, the "cold-stdout race"), which is why an exec that
        actually succeeded can come back with rc=0 and EMPTY stdout. Returning the
        instant the exit code lands would drop that trailing output for *every*
        exec caller, so once the exit code is in hand we keep draining for at most
        ``result_grace_s`` -- returning early the moment the socket closes (the
        normal terminator) so a well-behaved command is not delayed, and treating a
        quiet grace window as "no trailing frame is coming" rather than a hang.
        """
        timeout = self._config.recv_timeout
        # Set once the result frame arrives; from then on a close or a quiet grace
        # window is a normal terminator, not an error.
        pending_exit: int | None = None
        # Bytes of stdout drained AFTER the result frame -- i.e. the cold-stdout race
        # firing and being caught. Reported on exit so the race stays observable even
        # though the user-facing symptom (empty stdout) is now fixed.
        recovered = 0
        while True:
            try:
                frame = self._recv_proxy_message(ws, timeout=timeout)
            except ConnectionClosedOK:
                self._report_race_recovery(recovered)
                return pending_exit if pending_exit is not None else exit_code
            except ConnectionClosedError as exc:
                if _is_auth_expiry(exc):
                    return None
                raise
            except TimeoutError as exc:
                # After the exit code is in hand, a silent ``result_grace_s`` window
                # just means no trailing stdout frame is coming -- return normally.
                if pending_exit is not None:
                    self._report_race_recovery(recovered)
                    return pending_exit
                # Before completion, silence means the command hung: surface it,
                # rather than blocking in recv() until some outer timeout kills us
                # with no output and no diagnosis.
                raise RuntimeError(
                    f"provider-proxy sent nothing for {timeout:g}s. The command may "
                    "still be running on the container, or the provider may have stopped "
                    "responding. Raise recv_timeout in TransportConfig if the command is "
                    "expected to stay silent for longer."
                ) from exc
            if frame is None:
                continue
            # A stdout frame arriving after the exit code IS the issue-#12 race being
            # caught -- count it so the drain that saved us stays visible in the logs.
            if pending_exit is not None and len(frame) >= 1 and frame[0] == _FRAME_STDOUT:
                recovered += len(frame) - 1
            result = self._dispatch_frame(frame)
            if result is not None:
                # Exit code in hand -- but don't return yet. Keep draining any
                # trailing stdout/stderr frame still in flight, bounded by the short
                # ``result_grace_s`` window (subsequent stdout frames dispatch as a
                # side effect and yield None, so the loop keeps reading).
                pending_exit = result
                timeout = self._config.result_grace_s

    @staticmethod
    def _report_race_recovery(recovered: int) -> None:
        """Emit a one-line ``flaky-pass`` marker when the cold-stdout race was caught.

        Fires only when a stdout frame actually arrived after the result frame (the
        race, ~5% of execs on some providers). It goes to stderr so it never pollutes
        the command's stdout, and it crosses the subprocess boundary the smoke test
        runs exec across -- the one place the race rate stays observable now that the
        symptom is fixed. See ``TransportConfig.result_grace_s`` / issue #12.
        """
        if recovered > 0:
            print(
                f"[lease-shell] flaky-pass: drained {recovered} byte(s) of trailing "
                "stdout after the result frame (issue #12 cold-stdout race caught)",
                file=sys.stderr,
            )

    def _exec_with_refresh(self, command: str) -> int:
        return self._exec_loop(self._build_provider_shell_url(command=command))

    def _exec_shell_command(self, shell_command: str) -> int:
        return self._exec_loop(self._build_shell_url_sh_c(shell_command=shell_command))

    def _exec_loop(self, shell_path: str) -> int:
        attempts = 0
        exit_code = 0

        while attempts < MAX_RECONNECT_ATTEMPTS:
            jwt = self._fetch_jwt()
            proxy_url = self._get_proxy_ws_url()
            connect_msg = self._build_proxy_connect_msg(shell_path, jwt)
            ssl_ctx = ssl.create_default_context()

            try:
                with connect(
                    proxy_url,
                    ssl=ssl_ctx,
                    compression=None,
                    open_timeout=30,
                    ping_interval=30,
                    ping_timeout=20,
                ) as ws:
                    ws.send(connect_msg)
                    result = self._pump_frames(ws, exit_code)
                    if result is not None:
                        return result
            except RuntimeError as exc:
                if _is_auth_expiry_message(str(exc)):
                    pass
                else:
                    raise
            attempts += 1

        raise RuntimeError(
            f"Failed to re-authenticate after {MAX_RECONNECT_ATTEMPTS} attempts. "
            "Check that AKASH_API_KEY is valid and the deployment is active."
        )

    def prepare(self) -> None:
        self._extract_provider_info()

    def exec(self, command: str) -> int:
        if self._service is None:
            self.prepare()
        return self._exec_with_refresh(command)

    def inject(self, remote_path: str, content: str) -> None:
        if self._service is None:
            self.prepare()

        parent = os.path.dirname(remote_path)
        if parent:
            rc = self._exec_shell_command(f"mkdir -p {shlex.quote(parent)}")
            if rc != 0:
                raise RuntimeError(f"Failed to create directory for {remote_path}: exit {rc}")

        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        shell_cmd = f"echo {encoded} | base64 -d > {shlex.quote(remote_path)}"
        rc = self._exec_shell_command(shell_cmd)
        if rc != 0:
            raise RuntimeError(f"Failed to write {remote_path}: exit {rc}")

        rc = self._exec_shell_command(f"chmod 600 {shlex.quote(remote_path)}")
        if rc != 0:
            raise RuntimeError(f"Failed to set permissions on {remote_path}: exit {rc}")

    def _exec_with_stdin(self, command: str, stdin_data: bytes) -> int:
        attempts = 0
        exit_code = 0

        while attempts < MAX_RECONNECT_ATTEMPTS:
            jwt = self._fetch_jwt()
            shell_url = self._build_provider_shell_url(command=command, stdin=True)
            proxy_url = self._get_proxy_ws_url()
            ssl_ctx = ssl.create_default_context()

            try:
                with connect(
                    proxy_url,
                    ssl=ssl_ctx,
                    compression=None,
                    open_timeout=30,
                    ping_interval=30,
                    ping_timeout=20,
                ) as ws:
                    connect_msg = self._build_proxy_connect_msg(shell_url, jwt)
                    ws.send(connect_msg)

                    stdin_frame = bytes([_FRAME_STDIN]) + stdin_data
                    ws.send(self._proxy_frame_msg(shell_url, jwt, stdin_frame))

                    result = self._pump_frames(ws, exit_code)
                    if result is not None:
                        return result
            except RuntimeError as exc:
                if _is_auth_expiry_message(str(exc)):
                    pass
                else:
                    raise
            attempts += 1

        raise RuntimeError(f"Failed to re-authenticate after {MAX_RECONNECT_ATTEMPTS} attempts.")

    def _exec_with_stdin_command(self, shell_command: str, stdin_data: bytes) -> int:
        import time

        attempts = 0
        exit_code = 0

        while attempts < MAX_RECONNECT_ATTEMPTS:
            jwt = self._fetch_jwt()
            shell_url = self._build_shell_url_sh_c(shell_command=shell_command, stdin=True)
            proxy_url = self._get_proxy_ws_url()
            ssl_ctx = ssl.create_default_context()

            try:
                with connect(
                    proxy_url,
                    ssl=ssl_ctx,
                    compression=None,
                    open_timeout=30,
                    ping_interval=30,
                    ping_timeout=20,
                ) as ws:
                    connect_msg = self._build_proxy_connect_msg(shell_url, jwt)
                    ws.send(connect_msg)
                    time.sleep(0.5)

                    # Full envelope on every post-connect frame (see _proxy_frame_msg):
                    # the data frame + the EOF/close frame.
                    data_frame = bytes([_FRAME_STDIN]) + stdin_data
                    ws.send(self._proxy_frame_msg(shell_url, jwt, data_frame))
                    ws.send(self._proxy_frame_msg(shell_url, jwt, bytes([_FRAME_STDIN])))

                    result = self._pump_frames(ws, exit_code)
                    if result is not None:
                        return result
            except RuntimeError as exc:
                if _is_auth_expiry_message(str(exc)):
                    pass
                else:
                    raise
            attempts += 1

        raise RuntimeError(f"Failed to re-authenticate after {MAX_RECONNECT_ATTEMPTS} attempts.")

    def connect(self) -> None:
        if sys.platform == "win32":
            raise NotImplementedError(
                "Interactive shell via lease-shell is not supported on Windows. "
                "Use --transport ssh or run under WSL2."
            )
        if not sys.stdin.isatty():
            raise RuntimeError(
                "connect() requires an interactive TTY; stdin is not a terminal. "
                "Cannot run interactive shell with stdin redirected."
            )
        if self._service is None:
            self.prepare()

        fd = sys.stdin.fileno()
        original_settings = termios.tcgetattr(fd)

        try:
            tty.setraw(fd)
            self._run_interactive_session()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, original_settings)

    # Command run for an interactive `connect`. The provider needs an explicit
    # program to exec into the tty -- a shell request with no cmd is rejected
    # outright ("Received error from provider websocket"). `-i` keeps the shell
    # alive across the session instead of exiting on its first read.
    _INTERACTIVE_SHELL = "/bin/sh -i"

    def _run_interactive_session(self) -> None:
        jwt = self._fetch_jwt()
        shell_path = self._build_provider_shell_url(
            command=self._INTERACTIVE_SHELL, tty=True, stdin=True
        )
        proxy_url = self._get_proxy_ws_url()
        connect_msg = self._build_proxy_connect_msg(shell_path, jwt)
        ssl_ctx = ssl.create_default_context()

        with connect(
            proxy_url,
            ssl=ssl_ctx,
            compression=None,
            open_timeout=30,
            ping_interval=30,
            ping_timeout=20,
        ) as ws:
            ws.send(connect_msg)
            self._ws = ws
            # NOTE: the initial terminal-size frame is intentionally NOT sent here.
            # A data frame sent immediately after the connect message -- before the
            # provider has accepted the session -- is rejected by the proxy
            # ("url/providerAddress Required"), which used to kill the whole session.
            # The resize is instead sent from the IO loop once the first provider
            # frame confirms the session is live (see _run_io_loop). Stdin frames are
            # only produced when the user types, which in practice is well after the
            # session is up, so they aren't gated the same way.

            def _sigint_handler(signum, frame):
                # best-effort Ctrl-C forward; logging is unsafe inside a signal handler
                with contextlib.suppress(Exception):
                    ws.send(self._proxy_frame_msg(shell_path, jwt, bytes([_FRAME_STDIN, 0x03])))

            try:
                _initial_size = os.get_terminal_size()
            except OSError:
                _initial_size = None
            _last_size = [_initial_size]

            def _sigwinch_handler(signum, frame):
                try:
                    new_size = os.get_terminal_size()
                except OSError:
                    new_size = _last_size[0]
                if new_size is not None and self._send_resize(ws, shell_path, jwt, new_size):
                    _last_size[0] = new_size

            original_sigint = signal.signal(signal.SIGINT, _sigint_handler)
            original_sigwinch = signal.signal(signal.SIGWINCH, _sigwinch_handler)

            fd_stdin = sys.stdin.fileno()
            orig_flags = fcntl.fcntl(fd_stdin, fcntl.F_GETFL)
            fcntl.fcntl(fd_stdin, fcntl.F_SETFL, orig_flags | os.O_NONBLOCK)

            try:
                self._run_io_loop(ws, shell_path, jwt)
            finally:
                fcntl.fcntl(fd_stdin, fcntl.F_SETFL, orig_flags)
                signal.signal(signal.SIGINT, original_sigint)
                signal.signal(signal.SIGWINCH, original_sigwinch)
                self._ws = None

    def _proxy_frame_msg(self, url: str, jwt: str, frame_bytes: bytes) -> str:
        """Wrap raw frame bytes (stdin/resize/etc) in a proxy message.

        Every message sent to the provider-proxy -- not just the initial connect --
        must carry the full envelope (url + providerAddress + auth). A bare
        ``{type, data, isBase64}`` frame is rejected with "url/providerAddress
        Required", so stdin keystrokes and resize frames sent that way never reached
        the shell. This is what made interactive `connect` unusable even after the
        session was live.
        """
        return json.dumps(
            {
                "type": "websocket",
                "url": url,
                "providerAddress": self._provider_address,
                "auth": {"type": "jwt", "token": jwt},
                "isBase64": True,
                "data": base64.b64encode(frame_bytes).decode("ascii"),
            }
        )

    def _send_resize(self, ws, url: str, jwt: str, size) -> bool:
        """Send one terminal-size frame. Returns False if it couldn't be sent."""
        try:
            frame = bytes([_FRAME_RESIZE]) + struct.pack(">HH", size.lines, size.columns)
            ws.send(self._proxy_frame_msg(url, jwt, frame))
            return True
        except Exception:  # noqa: BLE001,S110 best-effort resize; a failure just leaves the default size
            return False

    def _run_io_loop(self, ws, url: str, jwt: str) -> None:
        fd_stdin = sys.stdin.fileno()
        sized = False  # send the initial resize only once the session is confirmed live

        while True:
            readable, _, _ = select.select([fd_stdin], [], [], 1.0)

            if fd_stdin in readable:
                try:
                    chunk = os.read(fd_stdin, 4096)
                    if chunk:
                        stdin_frame = bytes([_FRAME_STDIN]) + chunk
                        ws.send(self._proxy_frame_msg(url, jwt, stdin_frame))
                except (OSError, BlockingIOError):
                    pass

            try:
                frame = self._recv_proxy_message(ws, timeout=0.05)
                if frame is not None and len(frame) >= 1:
                    if not sized:
                        # First frame back means the proxy has accepted the session
                        # and will now relay data frames; safe to send the size.
                        sized = True
                        try:
                            _sz = os.get_terminal_size()
                        except OSError:
                            _sz = None
                        if _sz is not None:
                            self._send_resize(ws, url, jwt, _sz)
                    code = frame[0]
                    payload = frame[1:]
                    if code == _FRAME_STDOUT:
                        sys.stdout.buffer.write(payload)
                        sys.stdout.buffer.flush()
                    elif code == _FRAME_STDERR:
                        sys.stderr.buffer.write(payload)
                        sys.stderr.buffer.flush()
                    elif code == _FRAME_RESULT:
                        return
                    elif code == _FRAME_FAILURE:
                        raise RuntimeError(
                            f"Provider error: {payload.decode('utf-8', errors='replace')}"
                        )
            except (ConnectionClosedOK, ConnectionClosedError):
                return
            except TimeoutError:
                pass

    @staticmethod
    def _format_log_message(raw: bytes) -> str:
        """Render one streamed log frame as a single output line.

        The provider streams either raw text lines or JSON ServiceLogMessages
        (``{"name": <service>, "message": <line>}``). Handle both: JSON dicts
        become ``[service] message``; everything else passes through verbatim.
        """
        text = raw.decode("utf-8", errors="replace")
        stripped = text.strip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                name = obj.get("name") or obj.get("service") or ""
                msg = obj.get("message")
                if msg is None:
                    msg = obj.get("msg")
                if msg is None:
                    # Structured JSON with no recognizable message field — surface
                    # the raw payload rather than collapsing it to a blank line.
                    return stripped
                line = f"[{name}] {msg}" if name else str(msg)
                return line.rstrip("\n")
        return text.rstrip("\n")

    @staticmethod
    def _format_event_message(raw: bytes) -> str:
        """Render one streamed Kubernetes event frame as a single line."""
        text = raw.decode("utf-8", errors="replace").strip()
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return text
        if not isinstance(obj, dict):
            return text
        involved = obj.get("involvedObject") or obj.get("object") or {}
        if not isinstance(involved, dict):
            involved = {}
        kind = involved.get("kind", "")
        name = involved.get("name", "")
        target = f"{kind}/{name}".strip("/")
        # Resolve message/note by presence, not truthiness — a numeric 0 is a
        # valid message and must not be dropped.
        message = obj.get("message")
        if message is None:
            message = obj.get("note")
        message = "" if message is None else str(message)
        ts = obj.get("lastTimestamp") or obj.get("firstTimestamp") or obj.get("eventTime") or ""
        parts = [str(p) for p in (ts, obj.get("type", ""), obj.get("reason", ""), target) if p]
        if message != "":
            parts.append(message)
        return "  ".join(parts) if parts else text

    def _stream(
        self,
        provider_url: str,
        scope: list[str],
        formatter,
        recv_timeout: float,
        duration: float | None = None,
    ) -> None:
        """Open a provider-proxy WebSocket and print each frame via ``formatter``.

        Runs until the server closes the stream (non-follow / snapshot) or the
        user interrupts (Ctrl-C). For long-lived follow streams the lease JWT can
        expire mid-stream; on an auth-expiry close we refetch the token and
        reconnect (up to MAX_RECONNECT_ATTEMPTS) so the stream resumes instead of
        ending silently. Any other close ends the stream. Read-only: no stdin is
        sent. Callers must have already called ``_resolve_provider`` so the proxy
        URL embeds the host.

        ``duration`` bounds the whole stream client-side: after that many seconds
        (measured on a monotonic clock, across reconnects) the method returns
        cleanly with whatever was captured. Some providers keep a non-follow logs
        /events connection open after replaying the tail instead of closing it,
        so without this bound a "snapshot" blocks on ``recv`` until ``recv_timeout``
        (default 300s). ``duration`` gives a deterministic snapshot window and
        removes the need to wrap the CLI in an external ``timeout`` (which cannot
        flush partial output).
        """
        # A non-finite duration would defeat the whole point of the bound: NaN makes
        # every `>= deadline` comparison false (the stream never cuts off), and inf
        # sets no real deadline at all. Both silently reintroduce the hang this
        # parameter exists to prevent, so reject them at the API boundary. The CLI
        # catches this earlier with a friendlier message; this guards programmatic
        # callers of stream_logs/stream_events.
        if duration is not None and (not math.isfinite(duration) or duration <= 0):
            raise ValueError(f"duration must be a finite number > 0, got {duration!r}")
        deadline = (time.monotonic() + duration) if duration is not None else None
        attempts = 0
        while attempts < MAX_RECONNECT_ATTEMPTS:
            if deadline is not None and time.monotonic() >= deadline:
                return
            jwt = self._fetch_jwt(scope=scope)
            proxy_url = self._get_proxy_ws_url()
            connect_msg = self._build_proxy_connect_msg(provider_url, jwt)
            ssl_ctx = ssl.create_default_context()
            reconnect = False

            try:
                with connect(
                    proxy_url,
                    ssl=ssl_ctx,
                    compression=None,
                    open_timeout=30,
                    ping_interval=30,
                    ping_timeout=20,
                ) as ws:
                    ws.send(connect_msg)
                    while True:
                        # Bound each recv by the remaining snapshot window so we
                        # never overshoot ``duration`` by up to ``recv_timeout``.
                        this_timeout = recv_timeout
                        if deadline is not None:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                return
                            this_timeout = min(recv_timeout, remaining)
                        try:
                            # Logs/events frames arrive as JSON/text, not base64 —
                            # surface them for the formatter instead of discarding.
                            frame = self._recv_proxy_message(
                                ws, timeout=this_timeout, text_fallback=True
                            )
                        except ConnectionClosedOK:
                            return
                        except ConnectionClosedError as exc:
                            # Auth-expiry on a long follow → refetch + reconnect;
                            # any other close means the stream simply ended.
                            if _is_auth_expiry(exc):
                                reconnect = True
                                break
                            return
                        except TimeoutError:
                            if deadline is not None and time.monotonic() >= deadline:
                                return
                            continue
                        if frame is None:
                            continue
                        # Every received data frame maps to one output line. Do
                        # NOT skip empty lines — a blank line is real log output
                        # and dropping it would make the stream an unfaithful
                        # copy. (ping/pong frames already decode to None above.)
                        line = formatter(frame)
                        sys.stdout.write(line + "\n")
                        sys.stdout.flush()
            except RuntimeError as exc:
                # A proxy "error" frame about an expired token is recoverable;
                # any other proxy error propagates to the caller.
                if _is_auth_expiry_message(str(exc)):
                    reconnect = True
                else:
                    raise

            if not reconnect:
                return
            attempts += 1

        # Every attempt reconnected on auth-expiry and we ran out — fail loudly
        # rather than letting `logs --follow` stop silently (mirrors _exec_loop).
        raise RuntimeError(
            f"Failed to re-authenticate stream after {MAX_RECONNECT_ATTEMPTS} attempts. "
            "Check that AKASH_API_KEY is valid."
        )

    def stream_logs(
        self,
        follow: bool = False,
        tail: int = 100,
        service: str | None = None,
        duration: float | None = None,
    ) -> None:
        """Stream container logs for the lease via the provider-proxy.

        With ``follow=True`` the stream stays open until interrupted; otherwise
        it prints the last ``tail`` lines and returns. ``service`` filters to a
        single service (default: all services in the lease). ``duration`` bounds
        the stream to that many seconds and returns cleanly (useful for a
        deterministic snapshot when the provider holds a non-follow connection
        open instead of closing it).
        """
        self._resolve_provider()
        url = self._build_logs_url(follow=follow, tail=tail, service=service)
        # Follow streams indefinitely between lines, so use a long per-recv
        # timeout and just loop on timeout; a snapshot closes on its own (or on
        # the ``duration`` bound if the provider keeps the connection open).
        self._stream(url, ["logs"], self._format_log_message, recv_timeout=300, duration=duration)

    def stream_events(self, duration: float | None = None) -> None:
        """Stream Kubernetes events for the lease via the provider-proxy.

        ``duration`` bounds the stream to that many seconds and returns cleanly,
        giving a deterministic events snapshot when the provider keeps the
        connection open instead of closing it after the initial replay.
        """
        self._resolve_provider()
        url = self._build_events_url()
        self._stream(
            url, ["events"], self._format_event_message, recv_timeout=300, duration=duration
        )

    def validate(self) -> bool:
        leases = self._config.deployment.get("leases", [])
        if not leases or not isinstance(leases, list):
            return False
        lease = leases[0]
        if not isinstance(lease, dict):
            return False
        provider = lease.get("provider", {})
        if not isinstance(provider, dict):
            return False
        return bool(provider.get("hostUri") or provider.get("host_uri"))
