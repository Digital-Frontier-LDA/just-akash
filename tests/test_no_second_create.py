"""just-akash#332 PR 1: the plain-create gate, and no second create on any deploy path.

Each plain create site is tested in BOTH polarities, separately:

* gate on  -> refused before ANY network I/O (a stub transport that fails the test on
  first use, and a client factory that must never be called);
* gate off -> the existing create still happens, and the gate state is logged.

The gate parse is strict: unset or "0" is off, "1" is on, anything else refuses.
"""

from __future__ import annotations

import http.client
import socket
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from just_akash import capacity
from just_akash import deploy as deploy_module
from just_akash.api import AkashConsoleAPI, CIConsoleAPI
from just_akash.create_gate import GATE_ENV, PlainCreateRefused, require_plain_create_allowed

SDL_YAML = """
version: "2.0"
services:
  web:
    image: python:3.13-slim
    expose:
      - port: 22
        as: 22
        to:
          - global: true
"""

STALE_ERR = RuntimeError(
    "API Error (400): Failed to create lease: Cannot create lease: "
    "The selected bid is no longer open. Please refresh and select an available bid."
)
NO_ORDER_ERR = RuntimeError("API Error (404): no lease for deployment\n")

INVALID_VALUES = ["true", "yes", "", "2", " 1", "1 ", "on", "TRUE", "01"]


def _gate_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if "authorized-create gate:" in line]


@pytest.fixture
def no_network(monkeypatch):
    """A stub transport: any socket, HTTP connection or urllib open fails the test."""
    trips: list[str] = []

    def trip(name):
        def _fail(*_args, **_kwargs):
            trips.append(name)
            raise AssertionError(f"network I/O before the gate: {name}")

        return _fail

    monkeypatch.setattr(socket, "create_connection", trip("socket.create_connection"))
    monkeypatch.setattr(socket.socket, "connect", trip("socket.connect"))
    monkeypatch.setattr(http.client.HTTPConnection, "connect", trip("HTTPConnection.connect"))
    monkeypatch.setattr(urllib.request, "urlopen", trip("urlopen"))
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", trip("OpenerDirector.open"))
    return trips


# --- the parse ----------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "0"])
def test_gate_off_allows_and_says_off(monkeypatch, capsys, value):
    if value is None:
        monkeypatch.delenv(GATE_ENV, raising=False)
    else:
        monkeypatch.setenv(GATE_ENV, value)
    require_plain_create_allowed("site-x")
    lines = _gate_lines(capsys.readouterr().out)
    assert len(lines) == 1 and "gate: off" in lines[0] and "site-x" in lines[0], lines
    assert ("unset" in lines[0]) is (value is None)


def test_gate_on_refuses_and_says_on(monkeypatch, capsys):
    monkeypatch.setenv(GATE_ENV, "1")
    with pytest.raises(PlainCreateRefused, match="No create was sent"):
        require_plain_create_allowed("site-x")
    lines = _gate_lines(capsys.readouterr().out)
    assert len(lines) == 1 and "gate: on" in lines[0], lines


@pytest.mark.parametrize("value", INVALID_VALUES)
def test_any_other_value_refuses_and_never_reads_as_off(monkeypatch, capsys, value):
    monkeypatch.setenv(GATE_ENV, value)
    with pytest.raises(PlainCreateRefused, match="must be unset, '0' or '1'"):
        require_plain_create_allowed("site-x")
    lines = _gate_lines(capsys.readouterr().out)
    assert len(lines) == 1 and "gate: on" in lines[0] and "off" not in lines[0], lines


def test_the_backstop_is_silent_so_each_site_logs_one_line(monkeypatch, capsys):
    monkeypatch.delenv(GATE_ENV, raising=False)
    require_plain_create_allowed("backstop", announce=False)
    assert _gate_lines(capsys.readouterr().out) == []
    monkeypatch.setenv(GATE_ENV, "1")
    with pytest.raises(PlainCreateRefused):
        require_plain_create_allowed("backstop", announce=False)
    assert _gate_lines(capsys.readouterr().out) == []


# --- site 1: deploy() ---------------------------------------------------------------


def _deploy_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.setenv("AKASH_PROVIDERS", "akash1a")
    monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
    sdl = tmp_path / "sdl.yaml"
    sdl.write_text(SDL_YAML)
    return str(sdl)


@pytest.mark.parametrize("value", ["1", *INVALID_VALUES])
def test_deploy_gate_on_refuses_before_any_network_io(
    monkeypatch, tmp_path, no_network, capsys, value
):
    sdl = _deploy_setup(monkeypatch, tmp_path)
    monkeypatch.setenv(GATE_ENV, value)
    factory = MagicMock(name="AkashConsoleAPI")
    monkeypatch.setattr(deploy_module, "AkashConsoleAPI", factory)
    with pytest.raises(PlainCreateRefused):
        deploy_module.deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
    factory.assert_not_called()
    assert factory.mock_calls == []
    assert no_network == []
    lines = _gate_lines(capsys.readouterr().out)
    assert len(lines) == 1 and "gate: on" in lines[0] and "at deploy" in lines[0], lines


def _bids(client):
    client.get_bids.return_value = [
        {"id": {"provider": "akash1a"}, "price": {"amount": 10, "denom": "uakt"}, "state": "open"}
    ]


@pytest.mark.parametrize("value", [None, "0"])
@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_deploy_gate_off_still_creates_once_and_says_off(
    MockAPI, mock_time, monkeypatch, tmp_path, capsys, value
):
    sdl = _deploy_setup(monkeypatch, tmp_path)
    if value is None:
        monkeypatch.delenv(GATE_ENV, raising=False)
    else:
        monkeypatch.setenv(GATE_ENV, value)
    counter = [0.0]

    def advance():
        counter[0] += 1
        return counter[0]

    mock_time.time.side_effect = advance
    client = MockAPI.return_value
    client.create_deployment.return_value = {"dseq": "111", "manifest": "m1"}
    client.create_lease.return_value = {"lease": "ok"}
    _bids(client)
    result = deploy_module.deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
    assert result["dseq"] == "111"
    assert client.create_deployment.call_count == 1
    lines = _gate_lines(capsys.readouterr().out)
    assert len(lines) == 1 and "gate: off" in lines[0] and "at deploy" in lines[0], lines


# --- site 2: the old "already exists" retry -> hold -----------------------------------


@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_already_exists_holds_with_no_close_and_no_second_create(
    MockAPI, mock_time, monkeypatch, tmp_path
):
    sdl = _deploy_setup(monkeypatch, tmp_path)
    monkeypatch.delenv(GATE_ENV, raising=False)
    mock_time.time.return_value = 1000.0
    client = MockAPI.return_value
    client.create_deployment.side_effect = [
        RuntimeError("API Error (400): Deployment already exists"),
        {"dseq": "222", "manifest": "m2"},
    ]
    with pytest.raises(RuntimeError) as caught:
        deploy_module.deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
    assert str(caught.value) == deploy_module.ALREADY_EXISTS_HOLD
    assert "just-akash destroy --dseq" in str(caught.value)  # an actionable next step
    assert client.create_deployment.call_count == 1
    client.close_deployment.assert_not_called()
    client.list_deployments.assert_not_called()
    client.create_lease.assert_not_called()


# --- site 3: the old stale-order re-create -> hold ------------------------------------


@pytest.mark.parametrize(
    "error,reason", [(STALE_ERR, "all bids stale"), (NO_ORDER_ERR, "order un-leaseable")]
)
@patch("just_akash.deploy.time")
@patch("just_akash.deploy.AkashConsoleAPI")
def test_a_stale_or_gone_order_closes_its_own_order_and_never_creates_another(
    MockAPI, mock_time, monkeypatch, tmp_path, capsys, error, reason
):
    sdl = _deploy_setup(monkeypatch, tmp_path)
    monkeypatch.delenv(GATE_ENV, raising=False)
    counter = [0.0]

    def advance():
        counter[0] += 1
        return counter[0]

    mock_time.time.side_effect = advance
    client = MockAPI.return_value
    client.create_deployment.side_effect = [
        {"dseq": "111", "manifest": "m1"},
        {"dseq": "222", "manifest": "m2"},
    ]
    _bids(client)
    client.create_lease.side_effect = error  # every lease attempt fails the same way
    with pytest.raises(RuntimeError, match="Deploy HELD") as caught:
        deploy_module.deploy(sdl_path=sdl, bid_wait=5, bid_wait_retry=5)
    assert reason in str(caught.value)
    assert "re-run the deploy" in str(caught.value)  # an actionable next step
    # No second POST, ever; only THIS deploy's own order is closed, exactly once.
    assert client.create_deployment.call_count == 1
    client.close_deployment.assert_called_once_with("111")
    assert all(call.kwargs.get("dseq") == "111" for call in client.create_lease.call_args_list)


# --- site 4: the capacity probe -------------------------------------------------------


@pytest.mark.parametrize("value", ["1", *INVALID_VALUES])
def test_capacity_probe_gate_on_refuses_before_any_client_call(
    monkeypatch, no_network, capsys, value
):
    monkeypatch.setenv(GATE_ENV, value)
    client = MagicMock(name="client")
    with pytest.raises(PlainCreateRefused):
        capacity.probe_order_sdl(client, SDL_YAML, wait_s=0, poll_s=1)
    assert client.mock_calls == []
    assert no_network == []
    lines = _gate_lines(capsys.readouterr().out)
    assert len(lines) == 1 and "at capacity_probe" in lines[0], lines


@pytest.mark.parametrize("value", [None, "0"])
def test_capacity_probe_gate_off_still_creates_and_closes(monkeypatch, capsys, value):
    if value is None:
        monkeypatch.delenv(GATE_ENV, raising=False)
    else:
        monkeypatch.setenv(GATE_ENV, value)
    client = MagicMock(name="client")
    client.create_deployment.return_value = {"dseq": "333"}
    client.get_bids.return_value = []
    capacity.probe_order_sdl(client, SDL_YAML, wait_s=0, poll_s=1)
    assert client.create_deployment.call_count == 1
    client.close_deployment.assert_called_once_with("333")
    lines = _gate_lines(capsys.readouterr().out)
    assert len(lines) == 1 and "gate: off" in lines[0], lines


# --- backstop: the base client's own create ------------------------------------------


@pytest.mark.parametrize("cls", [AkashConsoleAPI, CIConsoleAPI])
@pytest.mark.parametrize("value", ["1", *INVALID_VALUES])
def test_client_create_gate_on_refuses_before_the_request(
    monkeypatch, no_network, capsys, cls, value
):
    monkeypatch.setenv(GATE_ENV, value)
    api = cls("test-key")
    request = MagicMock(name="_request")
    monkeypatch.setattr(api, "_request", request)
    with pytest.raises(PlainCreateRefused):
        api.create_deployment(SDL_YAML, deposit=0.5)
    request.assert_not_called()
    assert no_network == []
    assert _gate_lines(capsys.readouterr().out) == []  # silent: the site logs its own line


@pytest.mark.parametrize("cls", [AkashConsoleAPI, CIConsoleAPI])
@pytest.mark.parametrize("value", [None, "0"])
def test_client_create_gate_off_still_posts(monkeypatch, cls, value):
    if value is None:
        monkeypatch.delenv(GATE_ENV, raising=False)
    else:
        monkeypatch.setenv(GATE_ENV, value)
    api = cls("test-key")
    request = MagicMock(name="_request", return_value={"data": {"dseq": "444"}})
    monkeypatch.setattr(api, "_request", request)
    assert api.create_deployment(SDL_YAML, deposit=0.5) == {"dseq": "444"}
    request.assert_called_once()
    assert request.call_args.args[:2] == ("POST", "/v1/deployments")
