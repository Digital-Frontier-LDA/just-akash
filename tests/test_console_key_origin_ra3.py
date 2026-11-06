"""just-akash-ra3: the legacy Console client's SPENDING key never leaves the Console origin.

Found in the review of inference-benchmark#136: AkashConsoleAPI took its base from
AKASH_CONSOLE_URL with no host check, and used plain urlopen, whose redirect handler
follows a 30x to ANY host (https -> http included) and re-sends x-api-key. The base is now
checked in __init__ (https on console-api.akash.network, or loopback for the local fakes,
never userinfo), every redirect must stay on the request's own origin, and an endpoint must
be an absolute path. The end-to-end test uses two local servers and no external network.
"""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from just_akash import api
from just_akash.api import AkashAPIError, AkashConsoleAPI, console_base_ok

KEY = "ra3-fake-spending-key"


LOOPBACK = [
    "http://127.0.0.1:8080",
    "http://localhost:9",
    "https://[::1]:8443",
    "http://127.0.0.1:1",
]


@pytest.mark.parametrize(
    "base", ["https://console-api.akash.network", "https://console-api.akash.network:443"]
)
def test_the_console_origin_is_accepted(base):
    assert console_base_ok(base)
    assert AkashConsoleAPI(KEY, base_url=base).base_url == base


@pytest.mark.parametrize("base", LOOPBACK)
def test_loopback_is_refused_in_production_by_default(base, monkeypatch):
    """Review of #488 (blocking): AKASH_CONSOLE_URL=http://127.0.0.1:1 was accepted by the
    production constructor, so any local listener (or an HTTP_PROXY) read the key in
    cleartext. Without the test-only opt-in, loopback is refused from every source."""
    assert api._ALLOW_LOOPBACK_CONSOLE_FOR_TESTS is False
    monkeypatch.setattr(api, "_console_open", lambda *a, **k: pytest.fail("a request was made"))
    assert not console_base_ok(base)
    with pytest.raises(ValueError, match="refusing a Console base URL"):
        AkashConsoleAPI(KEY, base_url=base)
    monkeypatch.setenv("AKASH_CONSOLE_URL", base)
    with pytest.raises(ValueError, match="refusing a Console base URL"):
        AkashConsoleAPI(KEY)


def test_loopback_from_transport_config_is_refused_by_default():
    from just_akash.transport.base import TransportConfig
    from just_akash.transport.lease_shell import LeaseShellTransport

    cfg = TransportConfig(dseq="1", api_key=KEY, console_url="http://127.0.0.1:1")
    with pytest.raises(ValueError, match="refusing a Console base URL"):
        LeaseShellTransport(cfg)._get_api_client()


@pytest.mark.parametrize("base", LOOPBACK)
def test_loopback_is_accepted_only_with_the_test_opt_in(base, loopback_console):
    assert console_base_ok(base)
    assert AkashConsoleAPI(KEY, base_url=base).base_url == base


@pytest.mark.parametrize(
    "base",
    [
        "http://console-api.akash.network",  # downgrade
        "https://evil.akash.network",  # exact host match, not a suffix
        "https://console-api.akash.network.evil.example",
        "https://console.example",
        "https://@console-api.akash.network",  # empty userinfo
        "https://u:p@console-api.akash.network",
        "https://127.0.0.1@evil.example",
        "ftp://127.0.0.1",
        "https://console-api.akash.network:notaport",
        "https://console-api.akash.network:8443",  # the Console origin is port 443 only
    ],
)
def test_any_other_base_is_refused_before_a_request(base, monkeypatch):
    monkeypatch.setattr(api, "_console_open", lambda *a, **k: pytest.fail("a request was made"))
    assert not console_base_ok(base)
    with pytest.raises(ValueError, match="refusing a Console base URL"):
        AkashConsoleAPI(KEY, base_url=base)


def test_the_env_override_is_checked_too(monkeypatch):
    monkeypatch.setenv("AKASH_CONSOLE_URL", "https://evil.example")
    with pytest.raises(ValueError, match="refusing a Console base URL"):
        AkashConsoleAPI(KEY)


@pytest.mark.parametrize("endpoint", ["@evil.example/v1/x", "v1/deployments", ".evil.example/x"])
def test_a_relative_endpoint_is_refused(endpoint, monkeypatch):
    monkeypatch.setattr(api, "_console_open", lambda *a, **k: pytest.fail("a request was made"))
    with pytest.raises(ValueError, match="absolute path"):
        AkashConsoleAPI(KEY)._request("GET", endpoint)


# ── end to end, two local servers ─────────────────────────────────────────────────────


def _serve(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _quiet(cls):
    cls.log_message = lambda *a: None
    return cls


def test_a_302_to_another_host_never_delivers_the_key(loopback_console):
    seen = []

    @_quiet
    class B(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("x-api-key"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    b = _serve(B)

    @_quiet
    class A(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            # "localhost" is a different host than A's 127.0.0.1, though both are loopback:
            # the rule is SAME ORIGIN, not "any allowed base".
            self.send_header("Location", f"http://localhost:{b.server_port}/collect")
            self.end_headers()

    a = _serve(A)
    try:
        client = AkashConsoleAPI(KEY, base_url=f"http://127.0.0.1:{a.server_port}")
        with pytest.raises(AkashAPIError) as caught:
            client._request("GET", "/v1/deployments")
        assert caught.value.status == 302
        assert seen == [], f"the key reached the redirect target: {seen}"
    finally:
        a.shutdown()
        b.shutdown()


def test_a_302_within_the_origin_still_works(loopback_console):
    @_quiet
    class A(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/v1/old"):
                self.send_response(302)
                self.send_header("Location", "/v1/new")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            body = {"path": self.path.split("?")[0], "key": self.headers.get("x-api-key")}
            self.wfile.write(json.dumps(body).encode())

    a = _serve(A)
    try:
        client = AkashConsoleAPI(KEY, base_url=f"http://127.0.0.1:{a.server_port}")
        assert client._request("GET", "/v1/old") == {"path": "/v1/new", "key": KEY}
    finally:
        a.shutdown()


# ── the two survivors of #488's first review ──────────────────────────────────────────


def _redirect(base, newurl):
    req = urllib.request.Request(base + "/v1/x", headers={"x-api-key": KEY})
    return api._SameOriginRedirect().redirect_request(
        req, io.BytesIO(), 302, "Found", HTTPMessage(), newurl
    )


def test_a_same_origin_redirect_carrying_userinfo_is_refused():
    with pytest.raises(urllib.error.HTTPError, match="redirect leaves the Console origin"):
        _redirect("https://console-api.akash.network", "https://u@console-api.akash.network/v1/y")


def test_a_loopback_https_to_http_redirect_is_refused(loopback_console):
    with pytest.raises(urllib.error.HTTPError, match="redirect leaves the Console origin"):
        _redirect("https://127.0.0.1:8443", "http://127.0.0.1:8443/v1/y")
