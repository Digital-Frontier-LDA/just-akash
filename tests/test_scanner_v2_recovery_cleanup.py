"""Public synthetic native/Console/chain fixtures; no credentials or network."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from just_akash import scanner_cleanup_provenance as known_protocol
from just_akash import scanner_v2_cleanup as known
from just_akash import scanner_v2_recovery_cleanup as recovery
from just_akash.scanner_cleanup_native import CleanupHeld
from just_akash.scanner_provenance import ScannerProvenanceHeld, record_sha256
from just_akash.scanner_recovery_provenance import (
    BUNDLE,
    RecoveryOperation,
    compatible,
    validate_bundle,
    validate_cleanup_ack,
    validate_cleanup_record,
    validate_source,
)

FIXTURE = Path(__file__).parent / "fixtures/scanner_recovery_source.json"


def public():
    return json.loads(FIXTURE.read_bytes())


def resolver(data, **kwargs):
    proof = data["witness"]["creation_proof"]
    return recovery.RecoveryResolver(
        data["source"],
        kwargs.pop("renew", lambda: None),
        client=lambda owner: owner,
        snapshot=kwargs.pop("snapshot", lambda _client, _owner: copy.deepcopy(data["observed"])),
        proof=kwargs.pop("proof", lambda *_args: copy.deepcopy(proof)),
        now=kwargs.pop(
            "now", lambda: datetime.fromisoformat(proof["observed_at"]) + timedelta(seconds=1)
        ),
        **kwargs,
    )


def bundle(data, w=None):
    w = w or resolver(data).observe()
    return validate_bundle(
        data["source"]["actor"],
        data["source"]
        | {
            "schema": BUNDLE,
            "target": w["target"],
            "derivation": {
                "record": w,
                "artifact_id": 901,
                "artifact_digest": "sha256:" + "4" * 64,
                "body_sha256": record_sha256(w),
            },
        },
    )


class Recorder:
    def __init__(self, value, fail=None):
        self.bundle = value
        self.rows = []
        self.fail = fail
        self.clear = {"close": True, "remove": True}

    def persist(self, record):
        r = validate_cleanup_record(record)
        if self.fail == (r["kind"], r["phase"]):
            raise ValueError("PUBLIC_TRANSPORT_UNKNOWN")
        digest = record_sha256(r)
        ack = {
            "schema": "just-akash/scanner-cleanup-durable-ack/v1",
            "backend": "github-actions-artifact/v4",
            "operation_id": r["operation_id"],
            "phase": r["phase"],
            "record_sha256": digest,
            "parent_record_sha256": r["parent_record_sha256"],
            "artifact_id": 1000 + len(self.rows),
            "artifact_digest": "sha256:" + "5" * 64,
            "record_readback_sha256": digest,
            "actor": r["actor"],
            "producer": r["producer"],
            "target_sha256": r["target_sha256"],
            "history_sha256": "6" * 64,
            "created": True,
        }
        validate_cleanup_ack(r, ack)
        self.rows.append(
            {
                "record": r,
                "artifact_id": ack["artifact_id"],
                "artifact_digest": ack["artifact_digest"],
                "body_sha256": digest,
            }
        )
        return ack

    def history(self, producer, target):
        return {
            "schema": "just-akash/scanner-cleanup-history/v1",
            "actor": self.bundle["actor"],
            "producer": producer,
            "target_sha256": record_sha256(target),
            "complete": True,
            "wire_clear": self.clear,
            "handlers": [],
            "prior_actor_records": [],
            "current_actor_records": self.rows,
            "missing": [],
            "incomplete": False,
        }


def engine(data, *, fail=None, mutate=None, blocked=None):
    b = bundle(data)
    r = Recorder(b, fail)
    if blocked:
        r.clear[blocked] = False
    calls, state = [], {"closed": False, "absent": False}

    class Runner:
        def not_busy(self, identity, group):
            assert (
                identity == b["target"]["returned_runner_id"]
                and group == b["producer"]["runner_group_id"]
            )

        def absent(self, _identity):
            return state["absent"]

        def remove(self, identity, before):
            before()
            calls.append(("remove", identity))
            state["absent"] = True

    def closer(target, before):
        calls.append(("wallet_gate", target["owner"], target["dseq"]))

        class Closer:
            def close_deployment(self, dseq):
                before()
                calls.append(("close", dseq))
                state["closed"] = True

        return Closer()

    fresh = resolver(data)
    original = fresh.observe

    def observe(target=None):
        calls.append(("derive",))
        if mutate:
            mutate(data, len([x for x in calls if x[0] == "derive"]))
        return original(target)

    fresh.observe = observe
    evidence = b["derivation"]
    e = recovery.RecoveryEngine(
        actor_value=b["actor"],
        producer_binding=b["producer"],
        target=b["target"],
        recorder=r,
        revalidate=lambda: calls.append(("native",)),
        runner_api=Runner(),
        derivation_evidence={
            "artifact_id": evidence["artifact_id"],
            "artifact_digest": evidence["artifact_digest"],
            "record_sha256": evidence["body_sha256"],
        },
        resolver=fresh,
        closed=lambda *_args: state["closed"],
        closer_factory=closer,
        sleep=lambda _: None,
    )
    return e, r, calls


def test_actual_resolver_derives_only_unique_new_signed_identity_without_ack_upgrade():
    data = public()
    original = copy.deepcopy(data["source"]["records"]["deployment"])
    calls = []
    w = resolver(data, renew=lambda: calls.append("native")).observe()
    assert calls == ["native", "native"]
    assert w["authority"] == "READONLY_DERIVATION"
    assert w["target"]["dseq"] == data["observed"]["rows"][0]["dseq"]
    assert "deployment_ack_record_sha256" not in w["target"]
    assert w["target"]["deployment_unknown_record_sha256"] == record_sha256(original)
    assert data["source"]["records"]["deployment"] == original


@pytest.mark.parametrize("fault", ["ambiguous", "none", "old", "wrong-version", "partial"])
def test_complete_console_candidate_refusal(fault):
    data = public()
    if fault == "ambiguous":
        other = data["observed"]["rows"][0] | {"dseq": "1234568"}
        data["observed"]["rows"].append(other)
        data["observed"]["baseline"]["dseqs"].append(other["dseq"])
    elif fault == "none":
        data["observed"]["rows"] = []
        data["observed"]["baseline"]["dseqs"] = []
    elif fault == "old":
        data["source"]["records"]["deployment"]["details"]["precreate_baseline"]["dseqs"] = [
            "1234567"
        ]
    elif fault == "wrong-version":
        data["observed"]["rows"][0]["version"] = "BAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ="
    else:
        data["observed"].pop("baseline")
    expected = KeyError if fault == "partial" else ScannerProvenanceHeld
    with pytest.raises(expected):
        resolver(data).observe()


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner", "foreign"),
        ("dseq", "1234568"),
        ("registry_digest", "0" * 64),
        ("source_ids", ["allnodes", "allnodes"]),
        ("population_count", 2),
        ("groups", [{"gseq": 1, "name": "foreign"}]),
        ("current_manifest_version", "BAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ="),
        ("evidence_version", 2),
    ],
)
def test_actual_signed_proof_join_refuses_wrong_authority(field, value):
    data = public()
    data["witness"]["creation_proof"][field] = value
    with pytest.raises(ScannerProvenanceHeld):
        resolver(data).observe()


def test_native_drift_after_readonly_observation_holds_before_target_and_intents():
    data, calls = public(), []

    def renew():
        calls.append("native")
        if len(calls) == 2:
            raise ValueError("PUBLIC_CHANGED_ATTEMPT")

    with pytest.raises(ValueError, match="PUBLIC_CHANGED_ATTEMPT"):
        resolver(data, renew=renew).observe()
    assert calls == ["native", "native"]


def test_expired_signed_proof_holds_and_caller_cannot_supply_another_dseq():
    data = public()
    with pytest.raises(ScannerProvenanceHeld):
        resolver(
            data,
            now=lambda: datetime.fromisoformat(data["witness"]["creation_proof"]["expires_at"]),
        ).observe()
    r = resolver(data)
    wrong = r.observe()["target"] | {"dseq": "7654321"}
    with pytest.raises(ScannerProvenanceHeld):
        r.observe(wrong)


def test_real_inherited_engine_renews_derivation_at_both_final_effect_boundaries():
    data = public()
    e, r, calls = engine(data)
    result = e.run()
    assert result["cleanup_qualified"] is True
    assert [x[0] for x in calls].count("derive") == 3
    assert [x for x in calls if x[0] in ("close", "remove")] == [
        ("close", "1234567"),
        ("remove", 991),
    ]
    assert [(x["record"]["kind"], x["record"]["phase"]) for x in r.rows] == [
        ("close", "NO_WIRE"),
        ("remove", "NO_WIRE"),
        ("close", "UNKNOWN"),
        ("close", "ACK"),
        ("remove", "UNKNOWN"),
        ("remove", "ACK"),
    ]
    assert r.rows[2]["record"]["details"]["source_record_sha256"] == record_sha256(
        data["source"]["records"]["deployment"]
    )
    assert recovery.RecoveryEngine.guard is known.CleanupEngine.guard
    assert recovery.RecoveryEngine.run is known.CleanupEngine.run
    assert e.closed is not None


def test_create_identity_drift_at_real_close_boundary_is_poisoned_without_wire():
    data = public()

    def drift(current, count):
        if count >= 2:
            current["witness"]["creation_proof"]["creation_txhash"] = "D" * 64

    e, _r, calls = engine(data, mutate=drift)
    with pytest.raises(CleanupHeld):
        e.run()
    assert e.attempted["close"] is True and e.wire_started["close"] is False
    assert not any(x[0] in ("close", "remove") for x in calls)
    with pytest.raises(CleanupHeld):
        e.guard("close")


def test_unknown_durability_failure_never_enters_original_close_transport():
    e, r, calls = engine(public(), fail=("close", "UNKNOWN"))
    with pytest.raises(ValueError, match="PUBLIC_TRANSPORT_UNKNOWN"):
        e.run()
    assert [(x["record"]["kind"], x["record"]["phase"]) for x in r.rows] == [
        ("close", "NO_WIRE"),
        ("remove", "NO_WIRE"),
    ]
    assert not any(x[0] in ("close", "remove") for x in calls)
    assert e.operations["close"]._held is True


def test_prior_same_slot_unknown_is_not_escaped_by_derived_target_schema():
    e, r, calls = engine(public(), blocked="close")
    with pytest.raises(CleanupHeld):
        e.run()
    assert not any(x[0] in ("close", "remove") for x in calls)
    assert all(x["record"]["phase"] == "NO_WIRE" for x in r.rows)


def test_recovery_operation_cannot_use_unknown_as_fake_ack_or_another_slot():
    data = public()
    b = bundle(data)
    with pytest.raises(ScannerProvenanceHeld):
        known_protocol.validate_target(b["producer"], b["target"])
    evidence = b["derivation"]
    op = RecoveryOperation(
        actor=b["actor"],
        producer=b["producer"],
        target=b["target"],
        kind="close",
        recorder=Recorder(b),
        derivation_evidence={
            "artifact_id": evidence["artifact_id"],
            "artifact_digest": evidence["artifact_digest"],
            "record_sha256": evidence["body_sha256"],
        },
    )
    intent = {
        k: b["target"][k] for k in ("owner", "dseq", "group_population", "returned_runner_id")
    }
    with pytest.raises(ScannerProvenanceHeld):
        op.no_wire(intent | {"source_record_sha256": "0" * 64})
    assert op._held is True


def test_legacy_unknown_and_jit_only_absence_are_not_positive_no_wire_fences():
    data = public()
    for records in (
        {"jit": data["source"]["records"]["jit"], "deployment": None, "lease": None},
        {"jit": None, "deployment": data["source"]["records"]["deployment"], "lease": None},
    ):
        with pytest.raises(ScannerProvenanceHeld):
            validate_source(data["source"]["actor"], data["source"] | {"records": records})


def test_late_ack_bridge_requires_the_real_parent_unknown_not_schema_similarity():
    data = public()
    b = bundle(data)
    t = b["target"]
    # This direct predicate test is in addition to the real JS native/artifact history test.
    ack = copy.deepcopy(data["source"]["records"]["deployment"])
    ack.update(
        phase="ACK", phase_index=2, parent_record_sha256=t["deployment_unknown_record_sha256"]
    )
    ack["details"]["dseq"] = t["dseq"]
    known_target = {
        k: t[k]
        for k in (
            "owner",
            "dseq",
            "returned_runner_id",
            "group_population",
            "jit_ack_record_sha256",
        )
    }
    known_target.update(
        deployment_ack_record_sha256=record_sha256(ack), lease_ack_record_sha256=None
    )
    current = b | {"target": known_target, "records": {"deployment": ack}}
    assert compatible(t, current)
    current["records"]["deployment"]["parent_record_sha256"] = "0" * 64
    assert not compatible(t, current)


@pytest.mark.parametrize(
    "result", [("address", "match"), ("address", "foreign"), ("unknown", None)]
)
def test_actual_existing_two_field_owner_lookup_contract_is_used_for_readonly_routing(
    monkeypatch, result
):
    import sys
    import types

    from just_akash import api, owner_lookup

    owner = public()["source"]["records"]["deployment"]["details"]["owner"]
    wallet = types.ModuleType("just_akash.wallet_pool")
    wallet.configured_api_keys = lambda: ["PUBLIC_NONAUTH_KEY"]
    monkeypatch.setitem(sys.modules, "just_akash.wallet_pool", wallet)

    class Client:
        def __init__(self, key):
            self.key = key

        def account_address(self):
            raise AssertionError("ask seam is synthetic; no network permitted")

    monkeypatch.setattr(api, "CIConsoleAPI", Client)
    kind, value = result
    observed = owner if value == "match" else value
    monkeypatch.setattr(
        owner_lookup,
        "ask",
        lambda *_args, **_kwargs: ("answered", observed) if kind == "address" else (kind, None),
    )
    if observed == owner:
        assert recovery.observation_client(owner).key == "PUBLIC_NONAUTH_KEY"
    else:
        with pytest.raises(ScannerProvenanceHeld):
            recovery.observation_client(owner)
