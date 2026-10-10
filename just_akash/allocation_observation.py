"""Read-only Sentry allocation observations, never execution or runner authority.

The immutable lease UNKNOWN and original create candidate are DATA. Two fresh
registered-source snapshots can corroborate their chain allocation; they cannot
authenticate a controller, deliver a JIT registration, authorize publication or
make a failed/unknown mutation replayable. The caller retains its original hard
supervisor and absolute deadline. No workflow or shipping backend invokes this API.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit

from akash_lease_core import DeploymentKey, PreparedGroup

from . import chain
from . import execution_observation as execution
from . import sentry_lease_receipt as capture
from .deployment_receipt import _canonical_bytes, _private_parent, decode_receipt


def _require(value):
    if not value:
        raise execution._Held


def _object(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise execution._Held
    return cast(dict[str, Any], value)


def _array(value: object) -> list[Any]:
    if type(value) is not list:
        raise execution._Held
    return cast(list[Any], value)


def _number(value, *, bits=64, zero=False):
    _require(type(value) in (str, int))
    text = str(value)
    _require(re.fullmatch(r"0|[1-9][0-9]*", text) is not None)
    result = int(text)
    _require((0 if zero else 1) <= result < 2**bits)
    return result


def _identity(info):
    return tuple(
        getattr(info, field)
        for field in (
            "st_dev",
            "st_ino",
            "st_uid",
            "st_gid",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    )


def _owned(path):
    _require(isinstance(path, Path))
    _private_parent(path)
    expected = path.lstat()
    _require(stat.S_ISREG(expected.st_mode))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        _require(
            stat.S_ISREG(before.st_mode)
            and stat.S_IMODE(before.st_mode) == 0o600
            and before.st_uid == os.getuid()
            and before.st_nlink == 1
            and 0 < before.st_size <= 65536
            and _identity(expected) == _identity(before)
        )
        raw = os.read(fd, 65537)
        _require(
            len(raw) == before.st_size
            and _identity(before) == _identity(os.fstat(fd)) == _identity(path.lstat())
        )
        return raw, _identity(before)
    finally:
        os.close(fd)


def _intent(intent_path, create_path, sdl):
    raw, identity = _owned(intent_path)
    create_raw, create_identity = _owned(create_path)
    create: dict[str, Any] = dict(decode_receipt(create_raw))
    value = json.loads(
        raw,
        object_pairs_hook=execution._json_object,
        parse_constant=execution._invalid_json_constant,
    )
    group, digest = capture.payload_profile(sdl)
    expected = {
        "schema": "just-akash/sentry-lease-attempt/v1",
        "state": "UNKNOWN",
        "operation_id": create["operation_id"],
        "owner": create["expected_owner"],
        "dseq": create["dseq"],
        "gseq": 1,
        "oseq": 1,
        "provider": value.get("provider") if type(value) is dict else None,
        "group": group,
        "resource_profile": capture.PROFILE,
        "sdl_sha256": digest,
        "deployment_receipt_sha256": hashlib.sha256(create_raw).hexdigest(),
        "credential_binding": create.get("credential_binding"),
        "chain_allocation_verified": False,
        "runner_binding_verified": False,
        "publication_authority": False,
    }
    _require(
        type(value) is dict
        and raw == _canonical_bytes(expected)
        and expected["provider"] in capture.NATIVE_READER_PROVIDERS
        and create["state"] == "create_response_received"
        and create["authority"] == "candidate"
        and create["artifact_digest"] == digest
        and create["group_population"] == [{"gseq": 1, "name": group}]
    )
    _number(value["dseq"])
    return value, ((intent_path, raw, identity), (create_path, create_raw, create_identity))


def _attributes(value: Any):
    value = _array(value)
    _require(len(value) <= 100)
    keys = set()
    for item in value:
        row = _object(item)
        _require(set(row) == {"key", "value"})
        _require(
            type(row["key"]) is str
            and 0 < len(row["key"]) <= 256
            and row["key"] not in keys
            and type(row["value"]) is str
            and len(row["value"]) <= 256
        )
        keys.add(row["key"])


def _quantity(part: Any, field: str, *, zero=False):
    # Generated Go JSON tags use size; protobuf JSON uses quantity. Admit one
    # explicitly supported spelling, never silently choose between aliases.
    fields = {"size", "quantity"} if field == "size" else {field}
    part = _object(part)
    _require(set(part) <= fields | {"attributes"})
    supplied = set(part) & fields
    _require(len(supplied) == 1)
    _attributes(part.get("attributes", []))
    value = _object(part[next(iter(supplied))])
    _require(set(value) == {"val"} and type(value["val"]) is str)
    return _number(value["val"], zero=zero)


def _resource(value: Any, count: Any):
    """The closed single-Sentry resource shape from the pinned v1beta4 schema."""
    value = _object(value)
    _require(
        set(value)
        <= {
            "id",
            "cpu",
            "memory",
            "storage",
            "gpu",
            "endpoints",
        }
    )
    resource_id = _number(value.get("id"), bits=32)
    _require(_number(count, bits=32) == 1)
    cpu = _quantity(value.get("cpu"), "units")
    memory = _quantity(value.get("memory"), "size")
    volumes = _array(value.get("storage"))
    _require(len(volumes) == 1)
    volume = _object(volumes[0])
    _require(set(volume) <= {"name", "size", "quantity", "attributes"})
    _require(type(volume.get("name")) is str and 0 < len(volume["name"]) <= 128)
    storage = _quantity({k: v for k, v in volume.items() if k != "name"}, "size")
    gpu = 0 if value.get("gpu") is None else _quantity(value["gpu"], "units", zero=True)
    endpoints = _array(value.get("endpoints", []))
    _require(len(endpoints) <= 32)
    seen = set()
    for item in endpoints:
        endpoint = _object(item)
        _require(set(endpoint) == {"kind", "sequence_number"})
        _require(
            type(endpoint["kind"]) is str
            and endpoint["kind"]
            in {
                "SHARED_HTTP",
                "RANDOM_PORT",
                "LEASED_IP",
            }
        )
        key = (endpoint["kind"], _number(endpoint["sequence_number"], bits=32, zero=True))
        _require(key not in seen)
        seen.add(key)
    _require((cpu, memory, storage, gpu) == (2000, 6 * 1024**3, 40 * 1024**3, 0))
    # Include volume/endpoint/attribute facts; equal totals alone are insufficient.
    return resource_id, _canonical_bytes(value).decode(), 1


def _spec(value: Any, group: str):
    value = _object(value)
    _require(set(value) <= {"name", "requirements", "resources"})
    _require(value.get("name") == group)
    resources = _array(value.get("resources"))
    _require(len(resources) == 1)
    row = _object(resources[0])
    _require(set(row) <= {"resource", "count", "price"})
    shape = _resource(row.get("resource"), row.get("count"))
    return shape, _canonical_bytes(value)


def _market_id(value: Any, *, bid=False):
    fields = {"owner", "dseq", "gseq", "oseq"} | ({"bseq", "provider"} if bid else set())
    _require(type(value) is dict and set(value) == fields and type(value["owner"]) is str)
    identity = (
        value["owner"],
        str(_number(value["dseq"])),
        str(_number(value["gseq"], bits=32)),
        str(_number(value["oseq"], bits=32)),
    )
    if bid:
        _require(type(value["provider"]) is str)
        identity += (str(_number(value["bseq"], bits=32, zero=True)), value["provider"])
    return identity


class _Budget(execution._Budget):
    def __init__(self, sources, deadline, reader, monotonic, intent):
        super().__init__(sources, self._wire, monotonic)
        self.deadline = min(self.deadline, deadline)
        self.test_reader = reader
        self.intent = intent
        self.signed_groups = set()

    def _wire(self, path, *, base, height=None):
        # This closed surface contains only the existing read-only chain routes.
        parsed = urlsplit(path)
        _require(not parsed.scheme and not parsed.netloc and not parsed.fragment)
        query = parse_qs(parsed.query, strict_parsing=True)
        _require(all(len(values) == 1 for values in query.values()))
        route = parsed.path
        _require(
            route
            in {
                "/akash/deployment/v1beta4/deployments/info",
                "/akash/market/v1beta5/leases/list",
                "/akash/market/v1beta5/orders/info",
                "/akash/market/v1beta5/bids/info",
                "/cosmos/tx/v1beta1/txs",
            }
            or re.fullmatch(r"/cosmos/base/tendermint/v1beta1/blocks/(latest|[1-9][0-9]*)", route)
            or re.fullmatch(r"/cosmos/tx/v1beta1/txs/block/[1-9][0-9]*", route)
        )
        if route.startswith("/akash/"):
            prefix = "filters" if route.endswith("/list") else "id"
            _require(query.get(prefix + ".owner") == [self.intent["owner"]])
            _require(query.get(prefix + ".dseq") == [self.intent["dseq"]])
            keys = {prefix + ".owner", prefix + ".dseq"}
            if route.endswith("/leases/list"):
                keys |= {"pagination.limit", "pagination.offset", "pagination.count_total"}
                _require(query.get("pagination.limit") == ["200"])
                _require(query.get("pagination.count_total") == ["true"])
                _number(query.get("pagination.offset", [None])[0], zero=True)
            elif route.endswith(("/orders/info", "/bids/info")):
                keys |= {"id.gseq", "id.oseq"}
                _require(query.get("id.gseq") == ["1"] and query.get("id.oseq") == ["1"])
                if route.endswith("/bids/info"):
                    keys |= {"id.provider", "id.bseq"}
                    _require(query.get("id.provider") == [self.intent["provider"]])
                    _number(query.get("id.bseq", [None])[0], bits=32, zero=True)
            _require(set(query) == keys)
        elif route == "/cosmos/tx/v1beta1/txs":
            _require(set(query) == {"query", "page", "limit", "order_by"})
            _require(re.fullmatch(r"tx.height=[1-9][0-9]*", query["query"][0]))
            _require(query["order_by"] == ["ORDER_BY_ASC"])
            _require(query["limit"] == [str(chain._CREATION_PAGE_SIZE)])
            _number(query["page"][0])
        elif route.startswith("/cosmos/tx/v1beta1/txs/block/"):
            _require(
                set(query) == {"pagination.offset", "pagination.limit", "pagination.count_total"}
            )
            _require(query["pagination.limit"] == [str(chain._CREATION_PAGE_SIZE)])
            _require(query["pagination.count_total"] == ["true"])
            _number(query["pagination.offset"][0], zero=True)
        else:
            _require(not query)
        if self.test_reader is not None:
            result = self.test_reader(path, base=base, height=height)
        else:
            headers = {"Accept": "application/json"}
            if height is not None:
                headers["x-cosmos-block-height"] = str(height)
            remaining = self.deadline - self.monotonic()
            _require(remaining > 0)
            request = urllib.request.Request(base + path, headers=headers, method="GET")  # noqa: S310
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), chain._NoChainRedirect()
            )
            with opener.open(request, timeout=min(15, remaining)) as response:
                _require(response.status == 200 and response.geturl() == base + path)
                if height is not None:
                    _require(response.headers.get("x-cosmos-block-height") == str(height))
                raw = response.read(execution.MAX_RESPONSE_BYTES + 1)
                _require(0 < len(raw) <= execution.MAX_RESPONSE_BYTES)
            result = json.loads(
                raw,
                object_pairs_hook=execution._json_object,
                parse_constant=execution._invalid_json_constant,
            )
        _require(type(result) is dict)
        if route.startswith("/cosmos/tx/v1beta1/txs"):
            txs = result.get("txs", [])
            _require(type(txs) is list)
            for tx in txs:
                messages = tx.get("body", {}).get("messages", []) if type(tx) is dict else []
                for message in messages:
                    if (
                        type(message) is dict
                        and message.get("@type") == "/akash.deployment.v1beta4.MsgCreateDeployment"
                        and message.get("id")
                        == {"owner": self.intent["owner"], "dseq": self.intent["dseq"]}
                    ):
                        self.signed_groups.add(_canonical_bytes(message.get("groups")))
        return result


def _allocation(intent, budget, clock):
    budget.signed_groups.clear()
    subject = DeploymentKey(intent["owner"], intent["dseq"])
    groups = (PreparedGroup(1, intent["group"]),)
    snapshot = execution._sample(subject, groups, budget.sources, budget, clock)
    _require(len(budget.signed_groups) == 1)
    signed = json.loads(next(iter(budget.signed_groups)))
    _require(type(signed) is list and len(signed) == 1)
    signed_shape, signed_spec = _spec(signed[0], intent["group"])
    active = [key for key, state in snapshot.leases if state == "active"]
    _require(len(active) == 1 and len(snapshot.leases) == 1)
    key = active[0]
    _require(key[:4] == (intent["owner"], intent["dseq"], "1", "1"))
    _require(key[5] == intent["provider"])
    height = snapshot.evidence["height"]
    identity = {"id.owner": key[0], "id.dseq": key[1], "id.gseq": key[2], "id.oseq": key[3]}
    facts = []
    for source in budget.sources:

        def read(route, fields, selected_base=source["url"]):
            return budget.read(route + "?" + urlencode(fields), base=selected_base, height=height)

        info = read(
            "/akash/deployment/v1beta4/deployments/info",
            {
                "id.owner": key[0],
                "id.dseq": key[1],
            },
        )
        _require(
            _canonical_bytes(info["deployment"]["id"])
            == _canonical_bytes(
                {
                    "owner": key[0],
                    "dseq": key[1],
                }
            )
        )
        _require(info["deployment"]["state"] == "active")
        _require(_number(info["deployment"]["created_at"]) == snapshot.evidence["creation_height"])
        _require(len(info["groups"]) == 1 and info["groups"][0]["state"] == "open")
        group_id = info["groups"][0]["id"]
        _require(type(group_id) is dict and set(group_id) == {"owner", "dseq", "gseq"})
        _require(group_id["owner"] == key[0] and str(_number(group_id["dseq"])) == key[1])
        _require(str(_number(group_id["gseq"], bits=32)) == key[2])
        group_shape, group_spec = _spec(info["groups"][0]["group_spec"], intent["group"])
        order = read("/akash/market/v1beta5/orders/info", identity)["order"]
        _require(_market_id(order.get("id")) == key[:4])
        _require(order.get("state") == "active")
        order_shape, order_spec = _spec(order.get("spec"), intent["group"])
        bid_fields = dict(identity, **{"id.provider": key[5], "id.bseq": key[4]})
        bid = read("/akash/market/v1beta5/bids/info", bid_fields)["bid"]
        _require(_market_id(bid.get("id"), bid=True) == key)
        _require(bid.get("state") == "active")
        offers = bid.get("resources_offer")
        _require(type(offers) is list and len(offers) == 1)
        offer: Any = offers[0]
        _require(type(offer) is dict and set(offer) <= {"resources", "count", "prices"})
        bid_shape = _resource(offer.get("resources"), offer.get("count"))
        _require(signed_shape == group_shape == order_shape == bid_shape)
        _require(signed_spec == group_spec == order_spec)
        facts.append((key, signed_shape, hashlib.sha256(signed_spec).hexdigest()))
        # The exact single-allocation profile has one row. Renew that complete
        # page and terminal sentinel at the same height after order/bid reads.
        renewed = read(
            "/akash/market/v1beta5/leases/list",
            {
                "filters.owner": key[0],
                "filters.dseq": key[1],
                "pagination.limit": "200",
                "pagination.offset": "0",
                "pagination.count_total": "true",
            },
        )
        _require(type(renewed.get("leases")) is list and len(renewed["leases"]) == 1)
        _require(
            renewed.get("pagination")
            in (
                {"next_key": None, "total": "1"},
                {"next_key": "", "total": "1"},
            )
        )
        lease = renewed["leases"][0]["lease"]
        _require(lease.get("state") == "active" and _market_id(lease.get("id"), bid=True) == key)
    _require(facts[0] == facts[1])
    _require(clock() < execution._time(snapshot.evidence["expires_at"]))
    return facts[0], height, snapshot.evidence


@dataclass(frozen=True)
class AllocationObservation:
    """Public DATA only; construction cannot authenticate its producer."""

    observed: bool
    reason: str
    reads: int
    intent_sha256: str | None = None
    allocation_digest: str | None = None
    lease_id: tuple[str, ...] | None = None
    heights: tuple[int, int] | None = None
    group: str | None = None
    resource_id: int | None = None
    resource_profile: tuple[int, int, int, int, int] | None = None
    source_ids: tuple[str, ...] | None = None
    registry_digest: str | None = None
    creation_txhash: str | None = None
    observed_at: int | None = None
    expires_at: int | None = None
    intent_bound_bseq: bool = False
    runner_binding_verified: bool = False
    execution_authority: bool = False
    publication_authority: bool = False
    cleanup_authority: bool = False


def observe_sentry_allocation(
    intent_path: Path,
    create_path: Path,
    sdl: str,
    *,
    deadline: float,
    _reader=None,
    _clock=lambda: datetime.now(timezone.utc),
    _monotonic=time.monotonic,
) -> AllocationObservation:
    """Two complete stable chain observations inside the caller's existing deadline.

    Private dependencies are fault-test seams, never payload/config inputs. This
    method performs no writes, accepts no signer and never changes UNKNOWN files.
    It does not establish runner placement or a full-route 600-second success.
    """
    budget = None
    try:
        _require(type(deadline) in (float, int) and math.isfinite(deadline))
        limit = min(float(deadline), _monotonic() + execution.MAX_SECONDS)
        _require(_monotonic() < limit and type(sdl) is str)
        intent, files = _intent(intent_path, create_path, sdl)
        sources = execution._registry()
        budget = _Budget(sources, limit, _reader, _monotonic, intent)
        first, h1, _ = _allocation(intent, budget, _clock)
        second, h2, evidence = _allocation(intent, budget, _clock)
        _require(first == second and h2 >= h1)
        for path, raw, identity in files:
            _require(_owned(path) == (raw, identity))
        finished = _clock()
        _require(_monotonic() < budget.deadline)
        _require(finished < execution._time(evidence["expires_at"]))
        return AllocationObservation(
            observed=True,
            reason="allocation.observed",
            reads=budget.reads,
            intent_sha256=hashlib.sha256(files[0][1]).hexdigest(),
            allocation_digest=execution._digest(first),
            lease_id=first[0],
            heights=(h1, h2),
            group=intent["group"],
            resource_id=first[1][0],
            resource_profile=(2000, 6 * 1024**3, 40 * 1024**3, 0, 1),
            source_ids=tuple(evidence["source_ids"]),
            registry_digest=evidence["registry_digest"],
            creation_txhash=evidence["creation_txhash"],
            observed_at=int(finished.timestamp()),
            expires_at=int(execution._time(evidence["expires_at"]).timestamp()),
        )
    except Exception:  # noqa: BLE001 — errors/credentials/bodies never escape into reports
        return AllocationObservation(False, "allocation.unverified", budget.reads if budget else 0)
