"""Exercise the real chain readers and core boundary with adversarial responses."""

from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import json
import sys
from datetime import timedelta
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from akash_lease_core.chain_identity import DeploymentKey
from akash_lease_core.create_journal import PreparedGroup

from just_akash import chain, cli
from just_akash import finalized_closure as adapter
from tests import test_owner_close_authority as creation_fixture

# Public provider addresses, used only as checksum-valid synthetic subjects.
OWNER = "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk"  # pragma: allowlist secret
PROVIDER = "akash1z9nr23cgweu45g2jktfx95v7g2xp8qlsa3ys2x"  # pragma: allowlist secret
SUBJECT = DeploymentKey(OWNER, "123")
GROUPS = (PreparedGroup(1, "runner-run-7"), PreparedGroup(2, "sidecar"))
SOURCES = creation_fixture.SOURCES
NOW = creation_fixture.NOW
RAW_CLOSE = b"synthetic-close-block-transaction"
TXHASH = hashlib.sha256(RAW_CLOSE).hexdigest().upper()


@pytest.fixture
def reader_factory(monkeypatch):
    # Reuse the existing complete creation-block fixture, not a mocked identity
    # verdict. Every new test runs the actual creation proof before closure.
    monkeypatch.setattr(creation_fixture, "OWNER", OWNER)

    def factory(*, mutate=None, leases=(), total=None, sources=SOURCES):
        parent, _ = creation_fixture._reader(
            sources=sources,
            current_groups=tuple((str(g.gseq), g.group_name) for g in GROUPS),
            create_groups=tuple(g.group_name for g in GROUPS),
        )
        calls = []
        tx = {
            "body": {
                "messages": [{"@type": adapter._CLOSE_TYPE, "id": {"owner": OWNER, "dseq": "123"}}]
            },
            "auth_info": {"signer_infos": [{}]},
            "signatures": [base64.b64encode(b"synthetic-signature").decode()],
        }

        def read(path, *, base, height=None):
            calls.append((base, path, height))
            doc: dict
            if path == f"/cosmos/tx/v1beta1/txs/{TXHASH}":
                doc = {"tx": tx, "tx_response": {"height": "95", "txhash": TXHASH, "code": 0}}
            elif "/txs/block/95?" in path:
                doc = {
                    "block_id": {"hash": base64.b64encode(b"z" * 32).decode()},
                    "block": {
                        "header": {
                            "height": "95",
                            "chain_id": "akashnet-2",
                            "time": (NOW - timedelta(seconds=20)).isoformat(),
                        },
                        "data": {"txs": [base64.b64encode(RAW_CLOSE).decode()]},
                    },
                    "txs": [tx],
                    "pagination": {"next_key": None, "total": "1"},
                }
            elif "/leases/list?" in path:
                query = parse_qs(urlsplit(path).query)
                assert query["filters.owner"] == [OWNER]
                assert query["filters.dseq"] == ["123"]
                assert query["pagination.count_total"] == ["true"]
                offset = int(query["pagination.offset"][0])
                page = list(leases[offset : offset + adapter._PAGE_SIZE])
                doc = {
                    "leases": page,
                    "pagination": {
                        "next_key": "more" if offset + len(page) < len(leases) else None,
                        "total": str(len(leases) if total is None else total),
                    },
                }
            else:
                doc = parent(path, base=base, height=height)
                if "/deployments/info?" in path:
                    doc["deployment"]["state"] = "closed"
                    for group in doc["groups"]:
                        group["state"] = "closed"
                    doc["escrow_account"] = {
                        "id": {"scope": "deployment", "xid": f"{OWNER}/123"},
                        "state": {"owner": OWNER, "state": "closed"},
                    }
            doc = copy.deepcopy(doc)
            if mutate is not None:
                mutate(path, base, height, doc)
            return doc

        return read, calls

    return factory


@pytest.mark.parametrize("bad_height", [None, "99", "not-a-height"])
def test_actual_transport_requires_echoed_height(reader_factory, monkeypatch, bad_height):
    sources = chain.OWNER_CORROBORATION_SOURCES_V2
    read, _ = reader_factory(sources=sources)
    opener = MagicMock()

    def opened(req, *, timeout):
        parsed = urlsplit(req.full_url)
        source = next(s for s in sources if req.full_url.startswith(s["url"] + "/"))
        path = req.full_url[len(source["url"]) :]
        pinned = req.get_header("X-cosmos-block-height")
        doc = read(path, base=source["url"], height=int(pinned) if pinned else None)
        response = MagicMock()
        response.read.return_value = json.dumps(doc).encode()
        headers = Message()
        if pinned and bad_height is not None:
            headers["x-cosmos-block-height"] = bad_height
        response.headers = headers
        response.__enter__.return_value = response
        assert parsed.scheme == "https" and timeout == 15
        return response

    opener.open.side_effect = opened
    monkeypatch.setattr(chain.urllib.request, "build_opener", lambda *_a: opener)
    monkeypatch.setattr(adapter, "datetime", MagicMock(now=lambda _tz: NOW))
    monkeypatch.delenv("AKASH_REST_URL", raising=False)
    with pytest.raises(adapter.ClosureUnverified):
        adapter.observe_finalized_closure("op-1", SUBJECT, GROUPS, TXHASH)


def test_redirect_handler_refuses_foreign_operator_before_following():
    requests = []

    class RedirectServer(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/unregistered")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectServer)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(chain.ChainResponseError, match="HTTP 302"):
            chain._lcd_get(
                "/registered",
                base=f"http://127.0.0.1:{server.server_port}",
                follow_redirects=False,
            )
        assert requests == ["/registered"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_actual_transport_success_uses_no_redirect_opener(reader_factory, monkeypatch):
    sources = chain.OWNER_CORROBORATION_SOURCES_V2
    read, _ = reader_factory(sources=sources)
    opener = MagicMock()
    policies = []

    def opened(req, *, timeout):
        source = next(s for s in sources if req.full_url.startswith(s["url"] + "/"))
        path = req.full_url[len(source["url"]) :]
        pinned = req.get_header("X-cosmos-block-height")
        doc = read(path, base=source["url"], height=int(pinned) if pinned else None)
        response = MagicMock()
        response.read.return_value = json.dumps(doc).encode()
        response.headers = {"x-cosmos-block-height": pinned} if pinned else {}
        response.__enter__.return_value = response
        assert timeout == 15
        return response

    def build(policy):
        policies.append(policy)
        return opener

    opener.open.side_effect = opened
    monkeypatch.setattr(chain.urllib.request, "build_opener", build)
    monkeypatch.setattr(adapter, "datetime", MagicMock(now=lambda _tz: NOW))
    monkeypatch.delenv("AKASH_REST_URL", raising=False)
    report = adapter.observe_finalized_closure("op-1", SUBJECT, GROUPS, TXHASH)
    assert report["execution_closed"] is True
    assert all(isinstance(policy, chain._NoChainRedirect) for policy in policies)
    assert len(policies) == opener.open.call_count


def observe(reader, *, clock=lambda: NOW, sources=SOURCES, function=adapter._observe):
    return function(
        "run-7-attempt-1-op-1",
        SUBJECT,
        GROUPS,
        TXHASH,
        reader=reader,
        sources=sources,
        clock=clock,
    )


def lease(index=1, **changes):
    value = {
        "lease": {
            "id": {
                "owner": OWNER,
                "dseq": "123",
                "gseq": "1",
                "oseq": str(index),
                "bseq": "1",
                "provider": PROVIDER,
            },
            "state": "closed",
        }
    }
    value["lease"].update(changes)
    return value


def test_empty_lease_history_has_real_creation_and_close_block_proof(reader_factory):
    reader, calls = reader_factory()
    report = observe(reader)
    assert report["execution_closed"] is True
    assert report["settlement_proven"] is False
    closure = report["closure"]
    assert closure["$type"] == "ExecutionClosure"
    assert closure["subject"]["owner"] == OWNER
    assert closure["common_finality_height"] == 100
    assert closure["close_transaction_height"] == 95
    assert closure["operator_identity_a"] != closure["operator_identity_b"]
    assert len([c for c in calls if "/txs/block/90?" in c[1] and c[2] == 90]) == 2
    assert len([c for c in calls if "/txs/block/95?" in c[1] and c[2] == 95]) == 2
    assert len([c for c in calls if "/leases/list?" in c[1] and c[2] == 100]) == 2
    encoded = json.dumps(
        report["evidence"], sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    assert hashlib.sha256(encoded).hexdigest() == closure["evidence_digest"]


def test_every_page_read_at_same_height_with_stable_total(reader_factory):
    reader, calls = reader_factory(leases=tuple(lease(i) for i in range(1, 202)))
    report = observe(reader)
    assert len(report["evidence"]["observations"][0]["leases"]) == 201
    assert len([c for c in calls if "/leases/list?" in c[1] and c[2] == 100]) == 4


def test_measured_chain_bseq_zero_is_a_valid_lease_identity(reader_factory):
    row = lease()
    row["lease"]["id"]["bseq"] = 0
    reader, _ = reader_factory(leases=(row,))
    report = observe(reader)
    assert report["execution_closed"] is True
    assert report["evidence"]["observations"][0]["leases"][0][4] == "0"


def test_recorded_real_chain_evidence_matches_core_envelope_and_digest():
    from akash_lease_core.create_journal import ExecutionClosure, ExecutionClosureProofMode

    report = json.loads(
        (
            Path(__file__).parents[1] / "docs/evidence/finalized-closure-blazing-37014155266.json"
        ).read_text()
    )
    wire = dict(report["closure"])
    del wire["$type"]
    subject = wire["subject"]
    wire["subject"] = DeploymentKey(subject["owner"], subject["dseq"])
    wire["proof_mode"] = ExecutionClosureProofMode(wire["proof_mode"])
    closure = ExecutionClosure(**wire)
    evidence = report["evidence"]
    encoded = json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    assert hashlib.sha256(encoded).hexdigest() == closure.evidence_digest
    assert closure.close_transaction_height == 28886415
    assert closure.common_finality_height == 28888641
    for observation in evidence["observations"]:
        assert observation["leases"][0][4] == "0"
    assert report["execution_closed"] is True and report["settlement_proven"] is False


@pytest.mark.parametrize("escrow_state", ["overdrawn", "open", None])
def test_execution_closure_does_not_depend_on_or_claim_settlement(reader_factory, escrow_state):
    def mutate(path, _base, _height, doc):
        if "/deployments/info?" in path:
            if escrow_state is None:
                del doc["escrow_account"]
            else:
                doc["escrow_account"]["state"]["state"] = escrow_state

    reader, _ = reader_factory(mutate=mutate)
    assert observe(reader)["execution_closed"] is True
    assert observe(reader)["settlement_proven"] is False


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["tx_response"].update(code=1),
        lambda d: d["tx_response"].update(code=False),
        lambda d: d["tx_response"].update(height="101"),
        lambda d: d["tx_response"].update(height="089"),
        lambda d: d["tx_response"].update(height="89"),
        lambda d: d["tx_response"].update(txhash="A" * 64),
        lambda d: d["tx"]["body"]["messages"][0]["id"].update(owner=PROVIDER),
        lambda d: d["tx"]["body"]["messages"][0]["id"].update(dseq="124"),
        lambda d: d["tx"]["body"]["messages"][0].update(
            **{"@type": "/cosmos.bank.v1beta1.MsgSend"}
        ),
        lambda d: d["tx"].update(signatures=[]),
        lambda d: d["tx"].update(signatures=["invalid"]),
        lambda d: d["tx"].update(auth_info={"signer_infos": []}),
        lambda d: d["tx"].update(body=[]),
    ],
)
def test_missing_invalid_or_foreign_close_evidence_holds(reader_factory, change):
    def mutate(path, base, _height, doc):
        if path == f"/cosmos/tx/v1beta1/txs/{TXHASH}" and base == SOURCES[1]["url"]:
            change(doc)

    reader, _ = reader_factory(mutate=mutate)
    with pytest.raises((adapter.ClosureUnverified, AttributeError)):
        observe(reader)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["block"]["data"].update(txs=[base64.b64encode(b"other").decode()]),
        lambda d: d["txs"][0]["body"]["messages"][0]["id"].update(dseq="124"),
        lambda d: d["block"]["header"].update(chain_id="foreign"),
        lambda d: d["block"]["header"].update(height="94"),
        lambda d: d["block"]["header"].update(time=(NOW + timedelta(seconds=1)).isoformat()),
        lambda d: d["block_id"].update(hash=base64.b64encode(b"y" * 32).decode()),
        lambda d: d.update(txs=[]),
        lambda d: d["pagination"].update(total="2"),
    ],
)
def test_close_inclusion_must_agree_on_both_registered_sources(reader_factory, change):
    def mutate(path, base, _height, doc):
        if "/txs/block/95?" in path and base == SOURCES[1]["url"]:
            change(doc)

    reader, _ = reader_factory(mutate=mutate)
    with pytest.raises((adapter.ClosureUnverified, chain.ChainResponseError)):
        observe(reader)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["deployment"].update(state="active"),
        lambda d: d["groups"][1].update(state="paused"),
        lambda d: d["groups"][1]["id"].update(dseq="124"),
        lambda d: d["groups"][1]["group_spec"].update(name="other"),
        lambda d: d.update(groups=d["groups"][:1]),
        lambda d: d["escrow_account"]["id"].update(xid=f"{OWNER}/124"),
        lambda d: d["escrow_account"]["state"].update(owner=PROVIDER),
    ],
)
def test_nonterminal_or_incomplete_deployment_population_holds(reader_factory, change):
    def mutate(path, base, _height, doc):
        if "/deployments/info?" in path and base == SOURCES[1]["url"]:
            change(doc)

    reader, _ = reader_factory(mutate=mutate)
    with pytest.raises(adapter.ClosureUnverified):
        observe(reader)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.pop("pagination"),
        lambda d: d["pagination"].pop("next_key"),
        lambda d: d["pagination"].update(total="2"),
        lambda d: d["pagination"].update(total=False),
        lambda d: d["pagination"].update(next_key="more"),
        lambda d: d["leases"][0]["lease"].update(state="active"),
        lambda d: d["leases"][0]["lease"]["id"].update(owner=PROVIDER),
        lambda d: d["leases"][0]["lease"]["id"].update(dseq="124"),
        lambda d: d["leases"][0]["lease"]["id"].update(gseq="3"),
        lambda d: d["leases"][0]["lease"]["id"].update(provider="akash1" + "a" * 38),
        lambda d: d["leases"][0]["lease"]["id"].update(oseq="01"),
        lambda d: d["leases"][0]["lease"].update(state="insufficient_funds"),
    ],
)
def test_incomplete_active_foreign_or_disagreeing_lease_populations_hold(reader_factory, change):
    def mutate(path, base, _height, doc):
        if "/leases/list?" in path and base == SOURCES[1]["url"]:
            change(doc)

    reader, _ = reader_factory(leases=(lease(),), mutate=mutate)
    with pytest.raises(adapter.ClosureUnverified):
        observe(reader)


def test_duplicate_leases_hold(reader_factory):
    reader, _ = reader_factory(leases=(lease(), lease()))
    with pytest.raises(adapter.ClosureUnverified, match="duplicated"):
        observe(reader)


def test_changed_total_on_second_page_holds(reader_factory):
    def mutate(path, _base, _height, doc):
        if "pagination.offset=200" in path:
            doc["pagination"]["total"] = "202"

    reader, _ = reader_factory(leases=tuple(lease(i) for i in range(1, 202)), mutate=mutate)
    with pytest.raises(adapter.ClosureUnverified, match="changed"):
        observe(reader)


@pytest.mark.parametrize("field", ["operator", "gateway_ancestry", "cache_ancestry", "source_id"])
def test_aliased_trust_paths_never_vote(reader_factory, field):
    sources = copy.deepcopy(SOURCES)
    sources[1][field] = sources[0][field]
    reader, _ = reader_factory()
    with pytest.raises(adapter.ClosureUnverified):
        observe(reader, sources=sources)


def test_expired_collection_and_reversed_clock_hold(reader_factory):
    for finish in (NOW + timedelta(seconds=30), NOW - timedelta(seconds=1)):
        times = iter((NOW, finish))
        reader, _ = reader_factory()
        with pytest.raises(adapter.ClosureUnverified, match="expired"):
            observe(reader, clock=lambda _times=times: next(_times))


def test_unavailable_second_source_holds(reader_factory):
    reader, _ = reader_factory()

    def failing(path, *, base, height=None):
        if base == SOURCES[1]["url"]:
            raise TimeoutError("untrusted response text")
        return reader(path, base=base, height=height)

    with pytest.raises(adapter.ClosureUnverified):
        observe(failing)


def test_public_boundary_rejects_endpoint_override_without_queries(monkeypatch):
    monkeypatch.setenv("AKASH_REST_URL", "https://foreign.example")
    reads = []
    monkeypatch.setattr(chain, "_lcd_get", lambda *a, **k: reads.append((a, k)))
    with pytest.raises(adapter.ClosureUnverified):
        adapter.observe_finalized_closure("op-1", SUBJECT, GROUPS, TXHASH)
    assert reads == []


@pytest.mark.parametrize("operation", ["", "x" * 129, " x", "x ", "é"])
def test_invalid_operation_rejected_before_network(reader_factory, operation):
    reader, calls = reader_factory()
    with pytest.raises(ValueError):
        adapter._observe(
            operation, SUBJECT, GROUPS, TXHASH, reader=reader, sources=SOURCES, clock=lambda: NOW
        )
    assert calls == []


def test_effect_mutation_ignoring_active_lease_falsely_proves_closure(reader_factory, monkeypatch):
    source = inspect.getsource(adapter._lease_population)
    target = 'lease.get("state") not in {"closed", "insufficient_funds"}'
    assert source.count(target) == 1
    namespace = vars(adapter).copy()
    exec(source.replace(target, "False", 1), namespace)
    reader, _ = reader_factory(leases=(lease(state="active"),))
    with pytest.raises(adapter.ClosureUnverified):
        observe(reader)
    monkeypatch.setattr(adapter, "_lease_population", namespace["_lease_population"])
    assert observe(reader)["execution_closed"] is True


def test_cli_invokes_finalized_adapter_and_emits_failed_verdict(monkeypatch, capsys):
    calls = []

    def failed(*args):
        calls.append(args)
        raise adapter.ClosureUnverified("caller-controlled text must not be logged")

    monkeypatch.setattr(adapter, "observe_finalized_closure", failed)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "just-akash",
            "verify-finalized-closed",
            "--owner",
            OWNER,
            "--dseq",
            "123",
            "--operation-id",
            "op-1",
            "--close-tx-hash",
            TXHASH,
            "--groups-json",
            '[{"gseq":1,"name":"runner-run-7"}]',
        ],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert calls == [("op-1", SUBJECT, GROUPS[:1], TXHASH)]
    output = json.loads(capsys.readouterr().out)
    assert output["execution_closed"] is False
    assert "caller-controlled" not in output["reason"]


def test_effect_mutation_removing_close_subject_check_false_allows(reader_factory, monkeypatch):
    def mutate(path, _base, _height, doc):
        if path == f"/cosmos/tx/v1beta1/txs/{TXHASH}":
            doc["tx"]["body"]["messages"][0]["id"]["owner"] = PROVIDER
        elif "/txs/block/95?" in path:
            doc["txs"][0]["body"]["messages"][0]["id"]["owner"] = PROVIDER

    reader, _ = reader_factory(mutate=mutate)
    with pytest.raises(adapter.ClosureUnverified, match="exact deployment"):
        observe(reader)
    source = inspect.getsource(adapter._close_transaction)
    target = 'and message.get("id") == {"owner": subject.owner, "dseq": subject.dseq}'
    assert source.count(target) == 1
    namespace = vars(adapter).copy()
    exec(source.replace(target, "and True", 1), namespace)
    monkeypatch.setattr(adapter, "_close_transaction", namespace["_close_transaction"])
    assert observe(reader)["execution_closed"] is True
