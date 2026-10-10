"""Held allocation diagnostics and protocol-row arity never stand in for quorum."""

import copy
import logging

import pytest

from just_akash import allocation_observation as m
from tests import test_allocation_observation as baseline

NOW, SDL = baseline.NOW, baseline.SDL
allocation, chain_world = baseline.allocation, baseline.chain_world
unchanged = baseline.unchanged


def held_warning(caplog):
    records = [record for record in caplog.records if record.name == m.__name__]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "allocation observation held: allocation.unverified"
    assert record.args == () and record.exc_info is None and record.stack_info is None


def test_unexpected_reader_failure_emits_fixed_diagnostic_without_private_error(
    allocation, caplog
):
    a = allocation

    def fail(*args, **kwargs):
        raise OSError("private-fixture-response-must-never-be-logged")

    with caplog.at_level(logging.WARNING, logger=m.__name__):
        result = m.observe_sentry_allocation(
            a.intent,
            a.create,
            SDL,
            deadline=100,
            _reader=fail,
            _clock=lambda: NOW,
            _monotonic=lambda: 0,
        )
    assert not result.observed and result.reason == "allocation.unverified"
    held_warning(caplog)
    assert "private-fixture" not in caplog.text + repr(result)
    unchanged(a)


def test_local_refusal_emits_fixed_diagnostic_before_any_network(allocation, caplog):
    a = allocation
    a.intent.chmod(0o644)
    with caplog.at_level(logging.WARNING, logger=m.__name__):
        result = a.observe()
    assert not result.observed and result.reads == 0 and not a.calls
    held_warning(caplog)


def test_success_has_no_held_warning_and_still_reads_both_sources_twice(allocation, caplog):
    a = allocation
    with caplog.at_level(logging.WARNING, logger=m.__name__):
        result = a.observe()
    assert result.observed and result.source_ids is not None and len(result.source_ids) == 2
    assert sum("/orders/info?" in path for _, path, _ in a.calls) == 4
    assert not [record for record in caplog.records if record.name == m.__name__]
    unchanged(a)


@pytest.mark.parametrize("count", [0, 2, 3])
def test_complete_protocol_row_population_is_exactly_one_not_an_observer_threshold(
    allocation, count
):
    a = allocation
    spec = a.order["order"]["spec"]
    row = spec["resources"][0]
    spec["resources"] = [copy.deepcopy(row) for _ in range(count)]
    assert not a.observe().observed
    unchanged(a)


def test_one_valid_source_cannot_replace_unavailable_second_source(allocation, caplog):
    a = allocation

    def fail_second(base, path, height, doc):
        if base == a.world.sources[1]["url"]:
            raise OSError("private-second-source-response")
        return doc

    a.mutate = fail_second
    with caplog.at_level(logging.WARNING, logger=m.__name__):
        result = a.observe()
    assert not result.observed
    assert any(base == a.world.sources[0]["url"] for base, _, _ in a.calls)
    assert any(base == a.world.sources[1]["url"] for base, _, _ in a.calls)
    held_warning(caplog)
    assert "private-second-source" not in caplog.text + repr(result)
    unchanged(a)
