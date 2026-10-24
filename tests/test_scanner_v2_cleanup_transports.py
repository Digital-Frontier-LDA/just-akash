"""Closed urllib transports exercise the actual Console and native runner sends."""

from __future__ import annotations

import base64
import copy
import io
import json
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_scanner_v2_cleanup_engine import binding, target  # noqa: E402

from just_akash import scanner_cleanup_transport as runners  # noqa: E402
from just_akash import scanner_v2_cleanup as subject  # noqa: E402
from just_akash.scanner_cleanup_native import CleanupHeld  # noqa: E402


class Response:
    def __init__(self, status, value):
        self.status = status
        self.value = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.value[:limit] if limit >= 0 else self.value


class NativeWire:
    def __init__(self):
        self.present = True
        self.busy = False
        self.partial = False
        self.events = []
        self.unknown = False

    def open(self, request, timeout):
        assert timeout == 10
        path = request.full_url.removeprefix("https://api.github.com")
        self.events.append((request.method, path))
        prefix = runners.ORG
        row = dict(id=191, name="legitimately-renamed-native-ID", busy=self.busy, status="offline")
        if request.method == "DELETE":
            assert path == prefix + "/actions/runners/191"
            self.present = False
            if self.unknown:
                raise OSError("PUBLIC_NETWORK_LOST")
            return Response(204, {})
        assert request.method == "GET"
        if path == prefix:
            return Response(200, dict(id=208689568, login="Borduas-Holdings"))
        if path == prefix + "/actions/runners/191":
            if not self.present:
                raise urllib.error.HTTPError(request.full_url, 404, "not found", {}, io.BytesIO())
            return Response(200, row)
        if path == prefix + "/actions/runner-groups/7":
            return Response(
                200,
                dict(
                    id=7,
                    name="bb-native-trufflehog-v2",
                    visibility="selected",
                    allows_public_repositories=False,
                    restricted_to_workflows=True,
                    selected_workflows=[
                        "Borduas-Holdings/Blazing-Back/.github/workflows/secret-scanning.yml@refs/heads/main"
                    ],
                ),
            )
        if "/repositories?" in path:
            rows = (
                [dict(id=1071436278, full_name="Borduas-Holdings/Blazing-Back", private=True)]
                if path.endswith("page=1")
                else []
            )
            return Response(200, dict(total_count=1, repositories=rows))
        if "/runners?" in path:
            rows = [row] if self.present else []
            return Response(
                200, dict(total_count=len(rows) + (1 if self.partial else 0), runners=rows)
            )
        raise AssertionError("unapproved route")


def test_real_native_transport_allows_renamed_id_and_proves_two_complete_absences(monkeypatch):
    wire = NativeWire()
    monkeypatch.setattr(runners.urllib.request, "build_opener", lambda *args: wire)
    api = runners.RunnerAPI("PUBLIC_SYNTHETIC_TOKEN")
    api.not_busy(191, 7)
    ordering = []

    def before():
        assert not any(method == "DELETE" for method, _ in wire.events)
        ordering.append("durable UNKNOWN and final guard")

    api.remove(191, before)
    assert ordering == ["durable UNKNOWN and final guard"] and api.absent(191)
    tail = wire.events[-5:]
    assert tail == [
        ("GET", runners.ORG),
        ("GET", runners.ORG + "/actions/runners?per_page=100&page=1"),
        ("GET", runners.ORG + "/actions/runners/191"),
        ("GET", runners.ORG + "/actions/runners?per_page=100&page=1"),
        ("GET", runners.ORG + "/actions/runners/191"),
    ]
    with pytest.raises(CleanupHeld):
        api.remove(191, before)
    assert sum(m == "DELETE" for m, _ in wire.events) == 1


@pytest.mark.parametrize("bad", ["busy", "partial"])
def test_real_native_transport_never_confuses_busy_or_partial_with_absence(monkeypatch, bad):
    wire = NativeWire()
    setattr(wire, bad, True)
    monkeypatch.setattr(runners.urllib.request, "build_opener", lambda *args: wire)
    api = runners.RunnerAPI("PUBLIC_SYNTHETIC_TOKEN")
    with pytest.raises(CleanupHeld):
        api.not_busy(191, 7) if bad == "busy" else api.absent(191)
    assert not any(m == "DELETE" for m, _ in wire.events)


def test_real_native_delete_lost_response_is_never_repeated(monkeypatch):
    wire = NativeWire()
    wire.unknown = True
    monkeypatch.setattr(runners.urllib.request, "build_opener", lambda *args: wire)
    api = runners.RunnerAPI("PUBLIC_SYNTHETIC_TOKEN")
    with pytest.raises(CleanupHeld):
        api.remove(191, lambda: None)
    assert api.absent(191)
    with pytest.raises(CleanupHeld):
        api.remove(191, lambda: None)
    assert sum(m == "DELETE" for m, _ in wire.events) == 1


def test_real_wallet_client_close_runs_final_guard_at_actual_delete_open(monkeypatch):
    from just_akash import chain, wallet_pool

    events = []
    t = target()
    owner = t["owner"]
    group = t["group_population"][0]["name"]
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    monkeypatch.setattr(
        wallet_pool, "configured_api_keys", lambda: ["PUBLIC_SYNTHETIC_CONSOLE_KEY"]
    )
    monkeypatch.setattr(chain, "corroborated_deployment_group_names", lambda *args: [group])
    monkeypatch.setattr(
        chain, "owner_close_evidence", lambda *args: dict(dseq="300", expires_at=expiry)
    )

    class Console:
        def open(self, request, timeout):
            events.append((request.method, request.full_url))
            assert request.full_url.startswith("https://console-api.akash.network/v1/")
            if request.method == "POST":
                assert json.loads(request.data) == {
                    "data": {"ttl": 300, "leases": {"access": "scoped", "scope": ["shell"]}}
                }
                token = (
                    "public."
                    + base64.urlsafe_b64encode(json.dumps({"iss": owner}).encode())
                    .decode()
                    .rstrip("=")
                    + ".public"
                )
                return Response(200, {"data": {"token": token}})
            assert events[-2] == ("guard", "durable UNKNOWN acknowledged")
            assert request.method == "DELETE" and request.full_url.endswith("/v1/deployments/300")
            return Response(200, {"data": {"closed": True}})

    monkeypatch.setattr(subject.urllib.request, "build_opener", lambda *args: Console())
    closer = subject.closer_for(
        t, lambda: events.append(("guard", "durable UNKNOWN acknowledged"))
    )
    assert events == [("POST", "https://console-api.akash.network/v1/create-jwt-token")]
    assert closer.close_deployment("300") == {"closed": True}
    assert [m for m, _ in events] == ["POST", "guard", "DELETE"]
    with pytest.raises(RuntimeError):
        closer.close_deployment("301")
    assert sum(m == "DELETE" for m, _ in events) == 1


def test_actual_chain_closure_needs_both_complete_lease_maps_deployment_and_escrow(monkeypatch):
    from just_akash import chain

    t = target()
    now = datetime.now(timezone.utc)
    proof = dict(
        owner=t["owner"],
        dseq="300",
        groups=t["group_population"],
        population_count=1,
        registry_digest=chain.OWNER_CORROBORATION_REGISTRY_SHA256,
        source_ids=["allnodes", "polkachu"],
        height=123,
        observed_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=29)).isoformat(),
    )
    monkeypatch.setattr(
        chain, "owner_close_population_evidence", lambda *args: copy.deepcopy(proof)
    )
    calls = []
    bad = {"enabled": False}

    def read(route, *, base, height):
        assert height == 123
        calls.append((base, route))
        if "/leases/list?" in route:
            return {"leases": [], "pagination": {"next_key": None, "total": "0"}}
        return {
            "deployment": {"id": {"owner": t["owner"], "dseq": "300"}, "state": "closed"},
            "escrow_account": {
                "id": {"scope": "deployment", "xid": t["owner"] + "/300"},
                "state": {"owner": t["owner"], "state": "active" if bad["enabled"] else "closed"},
            },
        }

    monkeypatch.setattr(chain, "_lcd_get", read)
    assert subject.chain_closed(binding(), t)
    assert len({base for base, _ in calls}) == 2
    bad["enabled"] = True
    assert not subject.chain_closed(binding(), t)


def test_actual_engine_to_console_and_runner_transport_joins_both_unknown_before_send(monkeypatch):
    from test_scanner_v2_cleanup_engine import Fixture

    from just_akash import chain, wallet_pool

    f = Fixture("terminal")
    wire = NativeWire()
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    monkeypatch.setattr(wallet_pool, "configured_api_keys", lambda: ["PUBLIC_SYNTHETIC_KEY"])
    monkeypatch.setattr(
        chain, "corroborated_deployment_group_names", lambda *args: [binding()["akash_group"]]
    )
    monkeypatch.setattr(
        chain, "owner_close_evidence", lambda *args: dict(dseq="300", expires_at=expiry)
    )

    class BothOrigins:
        def open(self, request, timeout):
            if request.full_url.startswith("https://api.github.com"):
                if request.method == "DELETE":
                    assert ("remove", "UNKNOWN") in f.events
                    assert (
                        f.closed
                        and sum(r["record"]["phase"] == "NO_WIRE" for r in f.recorder.accepted)
                        == 2
                    )
                return wire.open(request, timeout)
            assert request.full_url.startswith("https://console-api.akash.network/v1/")
            if request.method == "POST":
                payload = (
                    base64.urlsafe_b64encode(json.dumps({"iss": target()["owner"]}).encode())
                    .decode()
                    .rstrip("=")
                )
                return Response(200, {"data": {"token": "public." + payload + ".public"}})
            assert request.method == "DELETE" and request.full_url.endswith("/v1/deployments/300")
            assert ("close", "UNKNOWN") in f.events
            assert sum(r["record"]["phase"] == "NO_WIRE" for r in f.recorder.accepted) == 2
            f.closed = True
            f.closes += 1
            return Response(200, {"data": {"closed": True}})

    monkeypatch.setattr(subject.urllib.request, "build_opener", lambda *args: BothOrigins())
    engine = f.engine()
    engine.io = runners.RunnerAPI("PUBLIC_SYNTHETIC_TOKEN")
    engine.closer_factory = subject.closer_for
    result = engine.run()
    assert (
        result["cleanup_qualified"] is True
        and result["close_ack_received"] is True
        and result["remove_ack_received"] is True
    )
    assert f.closes == 1 and sum(m == "DELETE" for m, _ in wire.events) == 1
