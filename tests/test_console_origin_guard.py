"""The legacy Console client never hands the SPENDING key to a non-Console host.

#375, bead just-akash-ra3.

Two leaks, both closed here:
  1. base_url came from AKASH_CONSOLE_URL with no host check, so any value (plain http,
     a look-alike host, userinfo) received x-api-key on the first request;
  2. urllib follows a 30x to ANY origin and re-sends every ordinary header, x-api-key
     included.

Only the socket boundary is replaced (HTTPSHandler.https_open / HTTPHandler.http_open):
the real OpenerDirector and redirect handlers run, and no network traffic occurs.
"""

import io
import urllib.response
from email.message import Message

import pytest

from just_akash.api import AkashConsoleAPI


class _Response(urllib.response.addinfourl):
    msg = "OK"


KEY = "spending-console-key-375"  # pragma: allowlist secret


@pytest.mark.parametrize(
    "url",
    [
        "https://console-api.akash.network",
        "https://console-api.akash.network/",
        "http://127.0.0.1:8080",
        "https://127.0.0.1:8443",
        "http://localhost:9",
        "http://[::1]:9",
    ],
)
def test_the_console_and_loopback_are_accepted(url):
    assert AkashConsoleAPI(KEY, base_url=url).base_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://console-api.akash.network",  # plain http to the real host
        "https://evil.akash.network",  # sibling host
        "https://console-api.akash.network.evil.com",  # suffix trick
        "https://console-api.akash.network:8443",  # other port
        "https://user:pw@console-api.akash.network",  # userinfo  # pragma: allowlist secret
        "https://console-api.akash.network@evil.com",  # userinfo trick
        "https://another.invalid",
        "ftp://console-api.akash.network",
        "https://console-api.akash.network/v1",  # a path prefix
        "https://console-api.akash.network?x=1",
        "console-api.akash.network",  # no scheme
    ],
)
def test_any_other_console_url_is_refused_before_a_request(url):
    with pytest.raises(ValueError, match="Console"):
        AkashConsoleAPI(KEY, base_url=url)


def test_an_ambient_console_url_is_refused_too(monkeypatch):
    monkeypatch.setenv("AKASH_CONSOLE_URL", "https://another.invalid")
    with pytest.raises(ValueError, match="Console"):
        AkashConsoleAPI(KEY)


def _socket(monkeypatch, location):
    calls = []

    def open_(_handler, request):
        calls.append(request)
        headers = Message()
        status = 200 if len(calls) > 1 else 302
        if status == 302:
            headers["Location"] = location
        response = _Response(
            io.BytesIO(b'{"data":{"dseq":"123"}}'), headers, request.full_url, status
        )
        return response

    monkeypatch.setattr("urllib.request.HTTPSHandler.https_open", open_)
    monkeypatch.setattr("urllib.request.HTTPHandler.http_open", open_)
    return calls


@pytest.mark.parametrize(
    "location",
    [
        "https://another.invalid/v1/deployments/1",
        "http://console-api.akash.network/v1/deployments/1",
    ],
)
def test_a_redirect_off_the_console_never_carries_the_key(monkeypatch, location):
    calls = _socket(monkeypatch, location)
    AkashConsoleAPI(KEY).get_deployment("1")
    assert len(calls) == 2
    assert calls[0].get_header("X-api-key") == KEY
    assert calls[1].get_header("X-api-key") is None
    assert KEY not in repr(calls[1].header_items())


def test_the_first_request_still_carries_the_key(monkeypatch):
    """THE CONTROL: the guard must not strip the key from the Console request itself."""
    calls = _socket(monkeypatch, "https://console-api.akash.network/v1/deployments/1")
    AkashConsoleAPI(KEY).get_deployment("1")
    assert calls[0].full_url.startswith("https://console-api.akash.network/v1/deployments/1")
    assert calls[0].get_header("X-api-key") == KEY
