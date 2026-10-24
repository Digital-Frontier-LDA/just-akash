"""Actual cleanup engine/phase controls using closed transports and public fixtures."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from just_akash import keydb_parity_cleanup as subject  # noqa: E402
from just_akash.keydb_parity_cleanup_native import CleanupHeld  # noqa: E402
from just_akash.keydb_parity_cleanup_provenance import (  # noqa: E402
    JOB_PREFIX,
    STEP,
    slot,
    validate_cleanup_record,
)
from just_akash.keydb_parity_provenance import (
    IMAGE,
    PROVIDERS,
    ROLE,
    KeyDBProvenanceHeld,
    record_sha256,
)  # noqa: E402


def binding():
    return dict(
        repository="Borduas-Holdings/blazing",
        repository_id=1074974924,
        workflow_path=".github/workflows/keydb-modules-parity.yml",
        workflow_sha="a" * 40,
        native_head_sha="a" * 40,
        sdk_sha="b" * 40,
        event="workflow_dispatch",
        ref="refs/heads/main",
        run_id=42,
        run_attempt=2,
        sdk_round=1,
        producer_job_id=91,
        role=ROLE,
        image=IMAGE,
        providers=sorted(PROVIDERS),
        resources=dict(cpu="4", memory="16Gi", storage="96Gi", count=1),
        runner_name="dfci-bl-keydb-42-a2-r1",
        runner_label="dfci-bl-keydb-42-a2-r1",
        akash_group="borduas-keydb-parity-run-42-a2-r1-end",
        runner_group_id=7,
    )


def actor(mode="normal"):
    locator = dict(run_id=42, run_attempt=2, sdk_round=1)
    return dict(
        repository="Borduas-Holdings/blazing",
        repository_id=1074974924,
        mode=mode,
        event="workflow_dispatch" if mode == "normal" else "workflow_run",
        ref="refs/heads/main",
        workflow_path=".github/workflows/keydb-modules-parity.yml"
        if mode == "normal"
        else ".github/workflows/keydb-parity-finished.yml",
        workflow_sha="a" * 40,
        native_head_sha="a" * 40 if mode == "normal" else "9" * 40,
        sdk_sha="c" * 40,
        run_id=42 if mode == "normal" else 500,
        run_attempt=2 if mode == "normal" else 1,
        job_id=77,
        job_name=JOB_PREFIX + slot(locator),
        step_name=STEP,
        target_locator=locator,
    )


def target():
    return dict(
        owner=sorted(PROVIDERS)[0],
        dseq="300",
        returned_runner_id=191,
        group_population=[dict(gseq=1, name=binding()["akash_group"])],
        jit_ack_record_sha256="d" * 64,
        deployment_ack_record_sha256="e" * 64,
        lease_ack_record_sha256="f" * 64,
    )


class Recorder:
    def __init__(self, fixture):
        self.f = fixture
        self.records = []
        self.accepted = []
        self.fail = None

    def persist(self, record):
        record = validate_cleanup_record(record)
        if record["phase"] == "UNKNOWN":
            assert {
                r["record"]["kind"] for r in self.accepted if r["record"]["phase"] == "NO_WIRE"
            } == {"close", "remove"}
        self.f.events.append((record["kind"], record["phase"]))
        desc = dict(
            record=copy.deepcopy(record),
            artifact_id=100 + len(self.records),
            artifact_digest="sha256:" + "a" * 64,
            body_sha256=record_sha256(record),
        )
        self.records.append(desc)
        if self.fail == (record["kind"], record["phase"]):
            raise OSError("PUBLIC_ACK_LOSS")
        self.accepted.append(desc)
        return dict(
            schema="just-akash/keydb-parity-cleanup-durable-ack/v1",
            backend="github-actions-artifact/v4",
            operation_id=record["operation_id"],
            phase=record["phase"],
            record_sha256=record_sha256(record),
            parent_record_sha256=record["parent_record_sha256"],
            artifact_id=desc["artifact_id"],
            artifact_digest=desc["artifact_digest"],
            record_readback_sha256=record_sha256(record),
            actor=record["actor"],
            producer=record["producer"],
            target_sha256=record["target_sha256"],
            history_sha256="b" * 64,
            created=True,
        )

    def history(self, producer, t):
        self.f.histories += 1
        rows = copy.deepcopy(self.accepted)
        if self.f.corrupt_current:
            for r in rows:
                if r["record"]["phase"] == "UNKNOWN":
                    r["body_sha256"] = "0" * 64
        return dict(
            schema="just-akash/keydb-parity-cleanup-history/v1",
            actor=self.f.actor,
            producer=producer,
            target_sha256=record_sha256(t),
            complete=not self.f.incomplete,
            missing=[] if not self.f.incomplete else ["PUBLIC_MISSING"],
            incomplete=self.f.incomplete,
            wire_clear=self.f.clear.copy(),
            handlers=[],
            prior_actor_records=[],
            current_actor_records=rows,
        )


class Runner:
    def __init__(self, f):
        self.f = f

    def not_busy(self, identity, group):
        assert identity == 191 and group == 7
        subject.require(not self.f.busy and not self.f.foreign_group)

    def absent(self, identity):
        assert identity == 191
        self.f.absence_reads += 1
        subject.require(not self.f.partial)
        return self.f.absent

    def remove(self, identity, before):
        assert identity == 191
        before()
        self.f.events.append(("wire", "remove"))
        self.f.removes += 1
        self.f.absent = not self.f.remove_unknown_present
        if self.f.remove_unknown_present:
            raise OSError("PUBLIC_REMOVE_UNKNOWN")


class Fixture:
    def __init__(self, mode="normal"):
        self.actor = actor(mode)
        self.target = target()
        self.events = []
        self.clear = dict(close=True, remove=True)
        self.closed = False
        self.absent = False
        self.busy = False
        self.foreign_group = False
        self.partial = False
        self.incomplete = False
        self.corrupt_current = False
        self.bad_owner = False
        self.native_bad = False
        self.remove_unknown_present = False
        self.close_unknown_active = False
        self.closes = 0
        self.removes = 0
        self.renewals = 0
        self.histories = 0
        self.absence_reads = 0
        self.recorder = Recorder(self)

    def renew(self):
        self.renewals += 1
        subject.require(not self.native_bad)

    def authority(self, b, t):
        assert b == binding() and t == self.target
        subject.require(not self.bad_owner)
        return dict(owner=t["owner"], dseq=t["dseq"])

    def factory(self, t, before):
        f = self

        class Closer:
            def close_deployment(self, dseq):
                assert dseq == "300"
                before()
                f.events.append(("wire", "close"))
                f.closes += 1
                f.closed = not f.close_unknown_active
                if f.close_unknown_active:
                    raise OSError("PUBLIC_CLOSE_UNKNOWN")

        return Closer()

    def engine(self):
        return subject.CleanupEngine(
            actor_value=self.actor,
            producer_binding=binding(),
            target=self.target,
            recorder=self.recorder,
            revalidate=self.renew,
            runner_api=Runner(self),
            authority=self.authority,
            closed=lambda b, t: self.closed,
            closer_factory=self.factory,
            clock=lambda: 0,
            sleep=lambda _: None,
        )


@pytest.mark.parametrize("mode", ["normal", "terminal"])
def test_real_engine_orders_both_intents_unknown_wire_ack_and_complete_measurement(mode):
    f = Fixture(mode)
    result = f.engine().run()
    assert (
        result["cleanup_qualified"] is True
        and result["lease_closed"] is True
        and result["registration_absent"] is True
    )
    assert f.events == [
        ("close", "NO_WIRE"),
        ("remove", "NO_WIRE"),
        ("close", "UNKNOWN"),
        ("wire", "close"),
        ("close", "ACK"),
        ("remove", "UNKNOWN"),
        ("wire", "remove"),
        ("remove", "ACK"),
    ]
    assert f.closes == f.removes == 1 and f.renewals >= 4


@pytest.mark.parametrize(
    "field", ["busy", "foreign_group", "partial", "incomplete", "bad_owner", "native_bad"]
)
def test_bad_scope_busy_partial_or_owner_proof_holds_before_effect(field):
    f = Fixture()
    setattr(f, field, True)
    with pytest.raises(CleanupHeld):
        f.engine().run()
    assert f.closes == f.removes == 0


def test_missing_native_returned_id_is_not_replaced_by_name_or_label():
    f = Fixture()
    f.target["returned_runner_id"] = None
    with pytest.raises(KeyDBProvenanceHeld):
        f.engine()
    assert f.closes == f.removes == 0


def test_prior_unknown_close_is_never_rewired_and_active_state_holds():
    f = Fixture()
    f.clear["close"] = False
    with pytest.raises(CleanupHeld):
        f.engine().run()
    assert f.closes == f.removes == 0


def test_prior_unknown_close_reconciles_only_positive_closed_then_pristine_remove():
    f = Fixture("terminal")
    f.clear["close"] = False
    f.closed = True
    result = f.engine().run()
    assert result["cleanup_qualified"] is True and f.closes == 0 and f.removes == 1


def test_prior_unknown_remove_is_not_rewired_when_registration_still_exists():
    f = Fixture("terminal")
    f.closed = True
    f.clear["remove"] = False
    with pytest.raises(CleanupHeld):
        f.engine().run()
    assert f.closes == f.removes == 0


def test_prior_unknowns_can_only_qualify_already_measured_end_state_without_wire():
    f = Fixture("terminal")
    f.clear = dict(close=False, remove=False)
    f.closed = f.absent = True
    result = f.engine().run()
    assert result["cleanup_qualified"] is True and f.closes == f.removes == 0 and f.events == []


def test_mismatched_current_unknown_session_cannot_reach_wire():
    f = Fixture()
    f.corrupt_current = True
    with pytest.raises(CleanupHeld):
        f.engine().run()
    assert f.closes == f.removes == 0


def test_close_unknown_active_never_retries_or_removes():
    f = Fixture()
    f.close_unknown_active = True
    with pytest.raises(CleanupHeld):
        f.engine().run()
    assert f.closes == 1 and f.removes == 0


def test_close_ack_loss_with_live_runner_defers_further_effect_to_new_actor():
    f = Fixture()
    f.recorder.fail = ("close", "ACK")
    with pytest.raises(CleanupHeld):
        f.engine().run()
    assert f.closes == 1 and f.removes == 0 and f.closed


def test_remove_ack_loss_qualifies_only_independent_absent_and_closed():
    f = Fixture()
    f.recorder.fail = ("remove", "ACK")
    result = f.engine().run()
    assert result["cleanup_qualified"] is True and result["remove_ack_received"] is False
    assert f.closes == f.removes == 1 and f.absence_reads >= 3


def test_remove_unknown_still_present_never_retries():
    f = Fixture()
    f.remove_unknown_present = True
    with pytest.raises(CleanupHeld):
        f.engine().run()
    assert f.closes == f.removes == 1


def test_second_handler_after_first_complete_performs_no_extra_effect():
    f = Fixture()
    f.engine().run()
    f.actor = actor("terminal")
    f.recorder = Recorder(f)
    f.clear = dict(close=False, remove=False)
    result = f.engine().run()
    assert result["cleanup_qualified"] is True and f.closes == f.removes == 1
