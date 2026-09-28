"""Every Console GET carries a per-request unique `_cb` parameter.

A cache in front of the Console API answered repeated GETs of the same URL
from an earlier response (`cf-cache-status: HIT`, `cache-control: private,
max-age=30`), so two reads of one URL could return stale data. A request header
(`Cache-Control: no-cache`) was measured not to prevent it; a unique query
parameter was (MISS on every request).
"""

import json
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from just_akash.api import AkashConsoleAPI


def _ok():
    resp = MagicMock()
    resp.read.return_value = json.dumps({"data": {"deployments": []}}).encode()
    resp.status = 200
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _urls(mock_urlopen):
    return [c.args[0].full_url for c in mock_urlopen.call_args_list]


@patch("just_akash.api.urllib.request.urlopen")
def test_each_get_carries_a_unique_cache_buster(mock_urlopen):
    mock_urlopen.side_effect = lambda *a, **k: _ok()
    c = AkashConsoleAPI("key", base_url="https://console.example")
    c._request("GET", "/v1/deployments/1")
    c._request("GET", "/v1/deployments/1")
    a, b = (parse_qs(urlsplit(u).query)["_cb"][0] for u in _urls(mock_urlopen))
    assert a != b, "two reads of one URL must not share a cache key"
    assert all(urlsplit(u).path == "/v1/deployments/1" for u in _urls(mock_urlopen))


@patch("just_akash.api.urllib.request.urlopen")
def test_the_cache_buster_keeps_existing_query_parameters(mock_urlopen):
    mock_urlopen.side_effect = lambda *a, **k: _ok()
    AkashConsoleAPI("key", base_url="https://console.example")._request(
        "GET", "/v1/deployments?limit=100&skip=0"
    )
    q = parse_qs(urlsplit(_urls(mock_urlopen)[0]).query)
    assert q["limit"] == ["100"] and q["skip"] == ["0"] and len(q["_cb"][0]) == 16


@patch("just_akash.api.urllib.request.urlopen")
def test_non_get_requests_are_not_altered(mock_urlopen):
    """THE CONTROL: a write's URL is exactly the endpoint."""
    mock_urlopen.side_effect = lambda *a, **k: _ok()
    c = AkashConsoleAPI("key", base_url="https://console.example")
    c._request("POST", "/v1/deployments", {"x": 1})
    c._request("DELETE", "/v1/deployments/7")
    assert _urls(mock_urlopen) == [
        "https://console.example/v1/deployments",
        "https://console.example/v1/deployments/7",
    ]
