"""Real chain-population readers joined to the pinned single-Sentry schemas."""

import copy
import json
import os
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace

import pytest

from just_akash import allocation_observation as m
from just_akash import deployment_receipt as receipts
from just_akash import sentry_lease_receipt as capture
from tests import test_execution_observation as baseline
from tests.test_sentry_lease_attempt_receipt import GROUP, SDL

NOW, OWNER, SUBJECT = baseline.NOW, baseline.OWNER, baseline.SUBJECT
chain_world = baseline.world


@pytest.fixture
def allocation(chain_world, tmp_path, monkeypatch):
    world = chain_world
    monkeypatch.delenv("AKASH_REST_URL", raising=False)
    provider = sorted(capture.NATIVE_READER_PROVIDERS)[0]
    resource = {
        "id": 1,
        "cpu": {"units": {"val": "2000"}},
        "memory": {"quantity": {"val": str(6 * 1024**3)}},
        "storage": [{"name": "default", "quantity": {"val": str(40 * 1024**3)}}],
        "gpu": {"units": {"val": "0"}},
    }
    spec = {
        "name": GROUP,
        "requirements": {"attributes": [], "signed_by": {}},
        "resources": [
            {"resource": resource, "count": 1, "price": {"denom": "uact", "amount": "100000"}}
        ],
    }
    world.txs[100][0]["body"]["messages"][0]["groups"] = [copy.deepcopy(spec)]
    world.info["deployment"]["state"] = "active"
    world.info["groups"] = [
        {
            "id": {"owner": OWNER, "dseq": SUBJECT.dseq, "gseq": 1},
            "group_spec": copy.deepcopy(spec),
            "state": "open",
        }
    ]
    lease_id = {
        "owner": OWNER,
        "dseq": SUBJECT.dseq,
        "gseq": 1,
        "oseq": 1,
        "bseq": 7,
        "provider": provider,
    }
    world.leases = [{"lease": {"id": lease_id, "state": "active"}}]
    state = SimpleNamespace(
        order={
            "order": {
                "id": {k: v for k, v in lease_id.items() if k not in {"provider", "bseq"}},
                "state": "active",
                "spec": copy.deepcopy(spec),
            }
        },
        bid={
            "bid": {
                "id": copy.deepcopy(lease_id),
                "state": "active",
                "resources_offer": [{"resources": copy.deepcopy(resource), "count": 1}],
            }
        },
        mutate=None,
        calls=[],
        mono=0.0,
    )
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    create, intent = private / "create.json", private / "lease.json"
    path, prepared, raw = receipts.prepare_receipt(
        str(create),
        operation_id="sentry-123-2-build",
        owner=OWNER,
        sdl_content=SDL,
    )
    _, submitting, submitting_raw = receipts.mark_submitting(path, prepared, raw)
    receipts.mark_create_response_received(
        path,
        submitting,
        submitting_raw,
        dseq=SUBJECT.dseq,
        deployment_response={"candidate": True},
    )
    capture.prepare(
        intent,
        create_path=create,
        operation_id="sentry-123-2-build",
        sdl=SDL,
        dseq=SUBJECT.dseq,
        provider=provider,
        lease_group=1,
    )
    original = (intent.read_bytes(), create.read_bytes())

    def reader(path, *, base, height=None):
        state.calls.append((base, path, height))
        if "/orders/info?" in path:
            doc = copy.deepcopy(state.order)
        elif "/bids/info?" in path:
            doc = copy.deepcopy(state.bid)
        else:
            doc = world.reader(path, base=base, height=height)
        return state.mutate(base, path, height, doc) if state.mutate else doc

    def observe(**kwargs):
        return m.observe_sentry_allocation(
            intent,
            create,
            SDL,
            deadline=100.0,
            _reader=reader,
            _clock=lambda: NOW,
            _monotonic=lambda: state.mono,
            **kwargs,
        )

    state.world, state.intent, state.create, state.original = world, intent, create, original
    state.observe, state.reader = observe, reader
    return state


def unchanged(a):
    assert (a.intent.read_bytes(), a.create.read_bytes()) == a.original
    assert json.loads(a.intent.read_bytes())["state"] == "UNKNOWN"
    assert not a.intent.with_suffix(".response.json").exists()


def test_two_real_complete_snapshots_join_signed_groups_order_matched_bid_and_lease(allocation):
    a = allocation
    result = a.observe()
    assert result.observed, result
    assert result.heights == (200, 200)
    assert result.lease_id == (
        OWNER,
        SUBJECT.dseq,
        "1",
        "1",
        "7",
        sorted(capture.NATIVE_READER_PROVIDERS)[0],
    )
    assert not result.intent_bound_bseq
    assert result.group == GROUP and result.resource_id == 1
    assert result.resource_profile == (2000, 6 * 1024**3, 40 * 1024**3, 0, 1)
    assert result.source_ids == tuple(source["source_id"] for source in a.world.sources)
    assert result.registry_digest == m.chain.OWNER_CORROBORATION_REGISTRY_SHA256
    assert result.observed_at < result.expires_at
    assert not any(
        asdict(result)[k]
        for k in (
            "runner_binding_verified",
            "execution_authority",
            "publication_authority",
            "cleanup_authority",
        )
    )
    assert 0 < result.reads == len(a.calls) < m.execution.MAX_READS
    assert sum("/bids/info?" in path for _, path, _ in a.calls) == 4
    assert sum("/orders/info?" in path for _, path, _ in a.calls) == 4
    # The original populations, not a stubbed proof, read both signed block and
    # indexed tx populations and re-read the terminal lease page for each voter.
    assert sum("/leases/list?" in path for _, path, _ in a.calls) == 8
    assert any("/txs/block/100?" in path for _, path, _ in a.calls)
    assert any("/cosmos/tx/v1beta1/txs?" in path for _, path, _ in a.calls)
    unchanged(a)


@pytest.mark.parametrize("bseq", [0, 2**32 - 1])
def test_chain_observed_bid_sequence_boundaries_are_evidence_not_invented_intent(allocation, bseq):
    a = allocation
    a.world.leases[0]["lease"]["id"]["bseq"] = bseq
    a.bid["bid"]["id"]["bseq"] = bseq
    result = a.observe()
    assert result.observed and result.lease_id is not None
    assert result.lease_id[4] == str(bseq)
    assert result.intent_bound_bseq is False
    unchanged(a)


def test_generated_go_size_field_shape_is_explicitly_supported(allocation):
    a = allocation

    def sizes(value):
        if isinstance(value, dict):
            if "quantity" in value:
                value["size"] = value.pop("quantity")
            for item in value.values():
                sizes(item)
        elif isinstance(value, list):
            for item in value:
                sizes(item)

    for value in (a.world.txs, a.world.info, a.order, a.bid):
        sizes(value)
    assert a.observe().observed
    unchanged(a)


@pytest.mark.parametrize("area", ["signed", "group", "order", "bid"])
@pytest.mark.parametrize(
    "change", ["missing", "extra", "cpu", "memory", "storage", "gpu", "count", "id", "alias"]
)
def test_every_resource_join_refuses_missing_extra_or_different_shapes(allocation, area, change):
    a = allocation
    if area == "signed":
        spec = a.world.txs[100][0]["body"]["messages"][0]["groups"][0]
    elif area == "group":
        spec = a.world.info["groups"][0]["group_spec"]
    elif area == "order":
        spec = a.order["order"]["spec"]
    else:
        spec = {}
    rows = a.bid["bid"]["resources_offer"] if area == "bid" else spec["resources"]
    row = rows[0]
    resource = row["resources" if area == "bid" else "resource"]
    if change == "missing":
        rows.clear()
    elif change == "extra":
        rows.append(copy.deepcopy(row))
    elif change == "cpu":
        resource["cpu"]["units"]["val"] = "2001"
    elif change == "memory":
        resource["memory"]["quantity"]["val"] = str(7 * 1024**3)
    elif change == "storage":
        resource["storage"][0]["quantity"]["val"] = str(41 * 1024**3)
    elif change == "gpu":
        resource["gpu"]["units"]["val"] = "1"
    elif change == "count":
        row["count"] = True
    elif change == "id":
        resource["id"] = 2
    else:
        resource["memory"]["size"] = resource["memory"]["quantity"]
    assert not a.observe().observed
    unchanged(a)


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner", "foreign"),
        ("dseq", "12346"),
        ("gseq", 2),
        ("oseq", 2),
        ("bseq", 8),
        ("bseq", True),
        ("bseq", "07"),
        ("bseq", 2**32),
        ("provider", "foreign"),
    ],
)
def test_exact_leased_bid_identity_not_first_or_invented_bseq(allocation, field, value):
    allocation.bid["bid"]["id"][field] = value
    assert not allocation.observe().observed
    unchanged(allocation)


@pytest.mark.parametrize("value", ["02000", "+2000", "-0", 2000, True, str(2**64)])
def test_canonical_resource_quantity_encoding(allocation, value):
    allocation.bid["bid"]["resources_offer"][0]["resources"]["cpu"]["units"]["val"] = value
    assert not allocation.observe().observed
    unchanged(allocation)


def test_multiple_leases_are_ambiguous_even_when_one_matches(allocation):
    a = allocation
    other = copy.deepcopy(a.world.leases[0])
    other["lease"]["id"]["bseq"] = 8
    a.world.leases.append(other)
    assert not a.observe().observed
    unchanged(a)


@pytest.mark.parametrize("state", ["closed", "insufficient_funds"])
def test_closed_lease_is_not_live_allocation_or_retry_authority(allocation, state):
    allocation.world.leases[0]["lease"]["state"] = state
    assert not allocation.observe().observed
    unchanged(allocation)


def test_voter_disagreement_refuses(allocation):
    a = allocation

    def mutate(base, path, height, doc):
        if base == a.world.sources[1]["url"] and "/orders/info?" in path:
            doc["order"]["spec"]["resources"][0]["resource"]["cpu"]["units"]["val"] = "2001"
        return doc

    a.mutate = mutate
    assert not a.observe().observed
    unchanged(a)


def test_second_snapshot_and_renewed_sentinel_drift_refuse(allocation):
    a = allocation
    pages = 0

    def mutate(base, path, height, doc):
        nonlocal pages
        if "/leases/list?" in path:
            pages += 1
            if pages == 3:
                doc["pagination"]["next_key"] = "unread-further-page"
        return doc

    a.mutate = mutate
    assert not a.observe().observed
    assert pages == 3
    unchanged(a)


def test_two_individually_valid_snapshots_with_changed_leased_bid_refuse(allocation):
    a = allocation
    latest = 0

    def mutate(base, path, height, doc):
        nonlocal latest
        if path.endswith("/blocks/latest"):
            latest += 1
            if latest == 3:
                a.world.leases[0]["lease"]["id"]["bseq"] = 8
                a.bid["bid"]["id"]["bseq"] = 8
        return doc

    a.mutate = mutate
    assert not a.observe().observed
    assert sum("/bids/info?" in path for _, path, _ in a.calls) == 4
    unchanged(a)


@pytest.mark.parametrize("change", ["truncated", "cursor", "duplicate", "unknown_state"])
def test_original_complete_lease_population_remains_mandatory(allocation, change):
    a = allocation

    def mutate(base, path, height, doc):
        if "/leases/list?" in path:
            if change == "truncated":
                doc["pagination"]["total"] = "2"
            elif change == "cursor":
                doc["pagination"]["next_key"] = "repeated"
            elif change == "duplicate":
                doc["leases"].append(copy.deepcopy(doc["leases"][0]))
                doc["pagination"]["total"] = "2"
            else:
                doc["leases"][0]["lease"]["state"] = "unknown"
        return doc

    a.mutate = mutate
    assert not a.observe().observed
    unchanged(a)


def test_replacement_of_identical_intent_bytes_refuses(allocation):
    a = allocation
    replaced = False

    def mutate(base, path, height, doc):
        nonlocal replaced
        if "/bids/info?" in path and not replaced:
            new = a.intent.with_name("replacement")
            new.write_bytes(a.intent.read_bytes())
            new.chmod(0o600)
            new.replace(a.intent)
            replaced = True
        return doc

    a.mutate = mutate
    assert not a.observe().observed
    unchanged(a)


def test_absolute_caller_deadline_not_reset_by_reader(allocation):
    a = allocation

    def mutate(base, path, height, doc):
        a.mono = 101.0
        return doc

    a.mutate = mutate
    assert not a.observe().observed
    assert len(a.calls) == 1
    unchanged(a)


def test_get_cap_holds_and_never_retries_mutation(allocation, monkeypatch):
    monkeypatch.setattr(m.execution, "MAX_READS", 1)
    assert not allocation.observe().observed
    assert len(allocation.calls) == 1
    unchanged(allocation)


def test_final_expired_proof_after_io_does_not_qualify(allocation):
    a = allocation
    now = NOW

    def mutate(base, path, height, doc):
        nonlocal now
        if "/bids/info?" in path:
            now = NOW + timedelta(seconds=31)
        return doc

    a.mutate = mutate
    result = m.observe_sentry_allocation(
        a.intent,
        a.create,
        SDL,
        deadline=100,
        _reader=a.reader,
        _clock=lambda: now,
        _monotonic=lambda: 0,
    )
    assert not result.observed
    unchanged(a)


@pytest.mark.parametrize("tamper", ["bool", "unknown", "provider", "hash", "authority"])
def test_local_data_mismatch_refuses_before_any_network(allocation, tamper):
    a = allocation
    value = json.loads(a.intent.read_bytes())
    if tamper == "bool":
        value["gseq"] = True
    elif tamper == "unknown":
        value["extra"] = "not admitted"
    elif tamper == "provider":
        value["provider"] = "foreign"
    elif tamper == "hash":
        value["deployment_receipt_sha256"] = "0" * 64
    else:
        value["publication_authority"] = True
    a.intent.write_bytes(receipts._canonical_bytes(value))
    result = a.observe()
    assert not result.observed and result.reads == 0 and not a.calls


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "fifo"])
def test_nonprivate_or_nonregular_receipt_refuses_before_network(allocation, kind):
    a = allocation
    if kind == "public":
        a.intent.chmod(0o644)
    elif kind == "hardlink":
        os.link(a.intent, a.intent.with_name("other-link"))
    else:
        a.intent.unlink()
        if kind == "symlink":
            a.intent.symlink_to(a.create)
        else:
            os.mkfifo(a.intent, 0o600)
    assert not a.observe().observed
    assert not a.calls


def test_read_errors_are_closed_and_do_not_echo_private_prose(allocation, capsys):
    a = allocation

    def fail(*a, **k):
        raise OSError("fixture-secret-private-response")

    result = m.observe_sentry_allocation(
        a.intent,
        a.create,
        SDL,
        deadline=100,
        _reader=fail,
        _clock=lambda: NOW,
        _monotonic=lambda: 0,
    )
    assert not result.observed
    assert "fixture-secret" not in repr(result) + capsys.readouterr().out + capsys.readouterr().err
    unchanged(a)


@pytest.mark.parametrize(
    "path",
    [
        "/akash/market/v1beta5/bids/info?id.owner=x&id.dseq=12345",
        "/akash/market/v1beta5/leases/list?filters.owner=x&filters.dseq=12345",
        "/akash/market/v1beta5/leases/create",
        "/cosmos/tx/v1beta1/txs/broadcast",
        "https://foreign.invalid/akash/market/v1beta5/leases/list",
        "/unknown",
    ],
)
def test_closed_transport_rejects_unrecognized_target_before_callback(allocation, path):
    a = allocation
    intent, _ = m._intent(a.intent, a.create, SDL)
    budget = m._Budget(
        tuple(m.chain.OWNER_CORROBORATION_SOURCES_V2), 100, a.reader, lambda: 0, intent
    )
    with pytest.raises(m.execution._Held):
        budget.read(path, base=budget.sources[0]["url"], height=200)
    assert not a.calls


def test_same_budget_has_no_environment_origin_or_proxy_inheritance(allocation, monkeypatch):
    monkeypatch.setenv("AKASH_REST_URL", "https://foreign.invalid")
    assert not allocation.observe().observed
    assert not allocation.calls


@pytest.mark.parametrize(
    "failure", [None, "status", "redirect", "height", "oversize", "duplicate", "nonfinite", "late"]
)
def test_actual_default_transport_caps_origin_height_json_and_deadline(
    allocation, monkeypatch, failure
):
    a = allocation
    intent, _ = m._intent(a.intent, a.create, SDL)
    calls, now = [], [0.0]
    base = a.world.sources[0]["url"]
    path = "/akash/market/v1beta5/orders/info?" + m.urlencode(
        {
            "id.owner": OWNER,
            "id.dseq": SUBJECT.dseq,
            "id.gseq": "1",
            "id.oseq": "1",
        }
    )

    class Response:
        status = 500 if failure == "status" else 200
        headers = {"x-cosmos-block-height": "199" if failure == "height" else "200"}

        def geturl(self):
            return "https://foreign.invalid" if failure == "redirect" else base + path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, maximum):
            assert maximum == m.execution.MAX_RESPONSE_BYTES + 1
            if failure == "oversize":
                return b"x" * maximum
            if failure == "duplicate":
                return b'{"order":{},"order":{}}'
            if failure == "nonfinite":
                return b'{"order":NaN}'
            if failure == "late":
                now[0] = 11.0
            return b'{"order":{}}'

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert request.full_url == base + path and request.method == "GET"
            assert request.headers["X-cosmos-block-height"] == "200"
            assert "Authorization" not in request.headers and timeout == 10.0
            return Response()

    def opener(*handlers):
        assert any(
            type(h) is m.urllib.request.ProxyHandler and vars(h)["proxies"] == {} for h in handlers
        )
        assert any(type(h) is m.chain._NoChainRedirect for h in handlers)
        return Opener()

    monkeypatch.setattr(m.urllib.request, "build_opener", opener)
    budget = m._Budget(a.world.sources, 10, None, lambda: now[0], intent)
    if failure:
        with pytest.raises((m.execution._Held, ValueError)):
            budget.read(path, base=base, height=200)
    else:
        assert budget.read(path, base=base, height=200) == {"order": {}}
    assert len(calls) == 1
    unchanged(a)


def test_default_api_does_not_create_wire_or_workflow_activation():
    assert not hasattr(m, "main")
    assert all(
        getattr(m.AllocationObservation(True, "copied", 0), field) is False
        for field in (
            "intent_bound_bseq",
            "runner_binding_verified",
            "execution_authority",
            "publication_authority",
            "cleanup_authority",
        )
    )
