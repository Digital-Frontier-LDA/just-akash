"""In-process fake of the Akash Console HTTP API + provider-proxy WebSocket.

Lets the full just-akash CLI run end-to-end in pytest WITHOUT credentials or
network: real ``urllib`` HTTP hits a localhost Console stub, and the real
``websockets`` lease-shell client exchanges real proxy-envelope frames with a
localhost WS stub that interprets a tiny shell command set.

This is an INTEGRATION harness, not a mock farm: ``api._request``, the JWT fetch,
the binary frame protocol (codes 100/102/103), ``_decode_payload``, and
``_pump_frames`` all run for real. The only seams bypass production TLS guards
(both of which are themselves unit-tested):

  * ``LeaseShellTransport._get_proxy_ws_url`` is redirected to the local ``ws://``
    stub (the real method rejects plaintext — a security guard, not logic).
  * ``lease_shell.connect`` strips its mandatory ``ssl=`` for ``ws://`` (the real
    client always passes a TLS context).

Nothing about the frame protocol, the API request/response shaping, or the CLI
dispatch is mocked.
"""

from __future__ import annotations

import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from websockets.sync.server import serve

# Frame codes — mirror just_akash/transport/lease_shell.
_STDOUT = 100
_RESULT = 102
_FAILURE = 103

_PROVIDER = "akash1fakeprovider"
_HOST_URI = "https://provider.fake:8443"


class FakeShell:
    """A tiny command interpreter backing the fake provider-proxy.

    Enough to exercise exec / inject / readback round-trips: argv
    ``echo``/``cat``/``mkdir``/``chmod``/``true``/``false``, and ``sh -c`` scripts
    covering the inject write pattern (``echo <b64> | base64 -d > path``) and a
    bare ``cat <path>``. ``script_overrides`` returns canned stdout for any script
    containing a known substring.
    """

    def __init__(self) -> None:
        self.fs: dict[str, bytes] = {}
        self.script_overrides: dict[str, bytes] = {}
        # Regression knobs (off by default):
        # close_without_result: proxy closes with NO result frame (the closed-lease
        #   silent-success path — must surface as an error, not exit 0).
        # failure_message: proxy sends a failure(103) frame with this text.
        self.close_without_result = False
        self.failure_message: str | None = None

    def run_argv(self, argv: list[str]) -> tuple[bytes, int]:
        if not argv:
            return b"", 0
        cmd, args = argv[0], argv[1:]
        if cmd == "echo":
            return (" ".join(args) + "\n").encode(), 0
        if cmd == "cat":
            if not args:
                return b"", 1
            return self.fs.get(args[0], b""), 0
        if cmd in ("mkdir", "chmod", "touch", "ls", "test"):
            return b"", 0
        if cmd == "true":
            return b"", 0
        if cmd == "false":
            return b"", 1
        return b"", 0  # permissive stub: unknown argv succeeds silently

    _B64_WRITE = re.compile(r"echo\s+([A-Za-z0-9+/=]+)\s*\|\s*base64\s+-d\s*>\s*(\S+)")

    def run_script(self, script: str) -> tuple[bytes, int]:
        for needle, stdout in self.script_overrides.items():
            if needle in script:
                return stdout, 0
        m = self._B64_WRITE.search(script)  # inject write
        if m:
            try:
                self.fs[m.group(2)] = base64.b64decode(m.group(1))
            except Exception:
                return b"", 1
            return b"", 0
        m2 = re.match(r"\s*cat\s+(\S+)\s*$", script)  # bare cat via sh -c
        if m2:
            return self.fs.get(m2.group(1), b""), 0
        return b"", 0  # mkdir/chmod/noop scripts succeed silently


class FakeProviderProxy:
    """WebSocket stub speaking the Console provider-proxy envelope protocol."""

    def __init__(self, shell: FakeShell) -> None:
        self.shell = shell
        self._server = None
        self.port = 0

    def _handle(self, conn) -> None:  # noqa: ANN001 (websockets calls this)
        try:
            raw = conn.recv(timeout=5)
        except Exception:
            return
        try:
            url = json.loads(raw).get("url", "") if isinstance(raw, str) else ""
        except Exception:
            url = ""
        argv, script = _parse_connect_url(url)

        if self.shell.failure_message is not None:
            conn.send(_envelope(bytes([_FAILURE]) + self.shell.failure_message.encode()))
            return
        if self.shell.close_without_result:
            return  # close immediately, no result frame
        stdout, rc = (
            self.shell.run_script(script) if script is not None else self.shell.run_argv(argv)
        )
        if stdout:
            conn.send(_envelope(bytes([_STDOUT]) + stdout))
        conn.send(_envelope(bytes([_RESULT]) + json.dumps({"exit_code": rc}).encode()))
        # Returning closes the connection — the normal terminator.

    def start(self) -> None:
        self._server = serve(self._handle, "127.0.0.1", 0, close_timeout=2)
        self.port = self._server.socket.getsockname()[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()


def _envelope(frame: bytes) -> str:
    return json.dumps({"type": "websocket", "message": base64.b64encode(frame).decode()})


def _parse_connect_url(url: str) -> tuple[list[str], str | None]:
    """Turn the proxy connect message's ``url`` query string back into an argv
    (cmd0, cmd1, …) or, for the ``sh -c`` path, the whole script."""
    q = parse_qs(urlparse(url).query)
    toks: list[str] = []
    i = 0
    while f"cmd{i}" in q:
        toks.append(q[f"cmd{i}"][0])
        i += 1
    if len(toks) >= 3 and toks[0] == "sh" and toks[1] == "-c":
        return [], toks[2]
    return toks, None


class _ConsoleApp:
    def __init__(self) -> None:
        self._next = 1000
        self.deployments: dict[str, dict] = {}
        self.bids = [_bid()]
        self.provider_record = {"owner": _PROVIDER, "hostUri": _HOST_URI, "isOnline": True}

    def create_deployment(self, body: dict) -> dict:
        dseq = str(self._next)
        self._next += 1
        sdl = (body.get("data") or {}).get("sdl", "")
        self.deployments[dseq] = {"dseq": dseq, "manifest": sdl}
        return {"dseq": dseq, "manifest": sdl}

    def get_deployment(self, dseq: str) -> dict:
        return {
            "deployment": {"state": "active", "dseq": dseq},
            "leases": [
                {
                    "id": {"provider": _PROVIDER},
                    "provider": {"hostUri": _HOST_URI},
                    "status": {"services": {"web": {"ready_replicas": 1, "total": 1}}},
                }
            ],
        }

    def list_deployments(self) -> list:
        return [
            {"deployment": {"state": "active", "dseq": d}, "dseq": d, "leases": []}
            for d in self.deployments
        ]

    def close_deployment(self, dseq: str) -> None:
        self.deployments.pop(dseq, None)


def _bid() -> dict:
    return {
        "bid": {"id": {"provider": _PROVIDER}},
        "id": {"provider": _PROVIDER},
        "price": {"amount": 10, "denom": "uact"},
        "state": "open",
    }


class _ConsoleHandler(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:  # silence the default request logging
        pass

    def _send(self, code: int, payload: dict | list) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def do_GET(self) -> None:
        app: _ConsoleApp = self.server.app  # type: ignore[attr-defined]
        path = urlparse(self.path).path
        if path == "/v1/deployments":
            self._send(200, {"data": app.list_deployments()})
        elif path.startswith("/v1/deployments/"):
            self._send(200, {"data": app.get_deployment(path.rsplit("/", 1)[-1])})
        elif path == "/v1/bids":
            self._send(200, {"data": app.bids})
        elif path == "/v1/providers":
            self._send(200, {"data": [app.provider_record]})
        else:
            self._send(404, {"message": "not found"})

    def do_POST(self) -> None:
        app: _ConsoleApp = self.server.app  # type: ignore[attr-defined]
        path = urlparse(self.path).path
        body = self._json_body()
        if path == "/v1/deployments":
            self._send(200, {"data": app.create_deployment(body)})
        elif path == "/v1/leases":
            self._send(200, {"data": {"lease": "created"}})
        elif path == "/v1/create-jwt-token":
            self._send(200, {"data": {"token": "fake.jwt.token"}})
        else:
            self._send(404, {"message": "not found"})

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/v1/deployments/"):
            self.server.app.close_deployment(path.rsplit("/", 1)[-1])  # type: ignore[attr-defined]
            self._send(200, {"data": {}})
        else:
            self._send(404, {"message": "not found"})


class FakeConsole:
    """A localhost stand-in for console-api.akash.network."""

    def __init__(self) -> None:
        self.app = _ConsoleApp()
        self._http = ThreadingHTTPServer(("127.0.0.1", 0), _ConsoleHandler)
        self._http.app = self.app  # type: ignore[attr-defined]
        threading.Thread(target=self._http.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self._http.server_address[1]}"

    def stop(self) -> None:
        self._http.shutdown()
        self._http.server_close()


class FakeAkash:
    """A running pair of (Console HTTP stub, provider-proxy WS stub).

    Use via the ``fake_akash`` pytest fixture in test_integration_fake.py, which
    wires the three TLS-bypass seams and the env vars.
    """

    def __init__(self) -> None:
        self.shell = FakeShell()
        self.console = FakeConsole()
        self.proxy = FakeProviderProxy(self.shell)
        self.proxy.start()

    @property
    def console_url(self) -> str:
        return self.console.base_url

    @property
    def proxy_url(self) -> str:
        return f"ws://127.0.0.1:{self.proxy.port}"

    def stop(self) -> None:
        self.proxy.stop()
        self.console.stop()
