"""Tests for canary/orphans.py — publishing orphaned escrow as a metric.

WHAT THESE GUARD. Two onidc deployments held ~$4.00 for five days because the only place an
orphan was ever reported was a workflow log, and df-grafana scrapes a `.prom` file, not logs.
So the property under test is not "the numbers are right" but "a scan that could not see the
whole fleet never publishes a number that reads as all-clear".
"""

from __future__ import annotations

import io
import json

import pytest

from canary.orphans import main, render


def _rep(**kw) -> dict:
    base = {"degraded": False, "deployments": []}
    base.update(kw)
    return base


def test_orphans_are_counted_and_labelled_by_dseq():
    out = render(
        _rep(
            orphaned_escrow_uact=4000000,
            deployments=[
                {"dseq": "111", "classification": "ORPHANED", "escrow_uact": 2000000},
                {"dseq": "222", "classification": "ORPHANED", "escrow_uact": 2000000},
                {"dseq": "333", "classification": "LEASED", "escrow_uact": 2000000},
            ],
        )
    )
    assert "akash_canary_orphans_total 2" in out
    assert 'akash_canary_orphan_escrow_uact{dseq="111"} 2000000' in out
    assert 'akash_canary_orphan_escrow_uact{dseq="222"} 2000000' in out
    assert "akash_canary_orphan_escrow_uact_total 4000000" in out
    # A LEASED deployment is not an orphan and must never appear as one.
    assert '"333"' not in out


def test_a_degraded_scan_publishes_no_count_at_all():
    """THE case this module exists for.

    A half-read fleet yielding `orphans_total 0` is a false all-clear -- a green number
    standing in for a question that was never asked. The series is withheld so df-grafana
    sees absence, which is loud, rather than a zero, which is quiet and wrong.
    """
    out = render(
        _rep(
            degraded=True,
            degraded_reasons=["endpoint refused", "page truncated"],
            deployments=[{"dseq": "111", "classification": "ORPHANED", "escrow_uact": 1}],
        )
    )
    assert "akash_canary_orphan_scan_degraded 1" in out
    body = out.replace("# NOTE: akash_canary_orphans_total withheld", "")
    assert "akash_canary_orphans_total" not in body
    # No per-orphan series either -- publishing them would imply the set is complete.
    assert "akash_canary_orphan_escrow_uact{" not in out
    assert "endpoint refused" in out


def test_an_empty_fleet_publishes_a_real_zero():
    """Only the DEGRADED zero lies. A complete scan finding nothing is a measurement."""
    out = render(_rep())
    assert "akash_canary_orphans_total 0" in out
    assert "akash_canary_orphan_scan_degraded 0" in out


def test_a_verdict_without_a_dseq_never_becomes_an_unlabelled_series():
    """An unlabelled series would silently aggregate with the next one."""
    out = render(_rep(deployments=[{"classification": "ORPHANED", "escrow_uact": 5}]))
    assert "akash_canary_orphan_escrow_uact{" not in out
    # ...but it still COUNTS: it is an orphan we cannot act on, not one that does not exist.
    assert "akash_canary_orphans_total 1" in out


def test_label_values_are_escaped():
    """A malformed dseq must not break the exposition format for every other series."""
    out = render(
        _rep(deployments=[{"dseq": 'a"b\\c', "classification": "ORPHANED", "escrow_uact": 7}])
    )
    assert 'dseq="a\\"b\\\\c"' in out


@pytest.mark.parametrize("bad", ["not-a-number", None, {}, [], "12.5.6"])
def test_a_malformed_escrow_costs_one_number_not_the_whole_file(bad):
    """The first draft guarded the per-orphan parse but not the total, and its own self-test
    crashed the renderer. In production that would have taken out the entire exposition and
    with it every orphan signal."""
    out = render(
        _rep(deployments=[{"dseq": "9", "classification": "ORPHANED", "escrow_uact": bad}])
    )
    assert 'akash_canary_orphan_escrow_uact{dseq="9"} 0' in out
    assert "akash_canary_orphans_total 1" in out
    assert "akash_canary_orphan_escrow_uact_total 0" in out


def test_classification_matching_is_case_insensitive():
    """The CLI emits the enum's lowercase value; the fixtures use upper. Both must work."""
    out = render(_rep(deployments=[{"dseq": "5", "classification": "orphaned", "escrow_uact": 3}]))
    assert "akash_canary_orphans_total 1" in out


def test_the_total_is_recomputed_when_the_report_omits_it():
    out = render(
        _rep(
            deployments=[
                {"dseq": "1", "classification": "ORPHANED", "escrow_uact": 10},
                {"dseq": "2", "classification": "ORPHANED", "escrow_uact": 5},
            ]
        )
    )
    assert "akash_canary_orphan_escrow_uact_total 15" in out


def test_unparseable_stdin_fails_loudly_rather_than_emitting_nothing(monkeypatch, capsys):
    """An empty exposition reads downstream as 'no orphans'. Exit non-zero instead."""
    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
    assert main([]) == 1
    err = capsys.readouterr().err
    assert "orphan-scan --json" in err
    assert "not json at all" in err


def test_a_json_array_is_refused(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([1, 2, 3])))
    assert main([]) == 1
    assert "expected a JSON object" in capsys.readouterr().err


def test_main_renders_to_stdout(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                _rep(deployments=[{"dseq": "7", "classification": "ORPHANED", "escrow_uact": 42}])
            )
        ),
    )
    assert main([]) == 0
    out = capsys.readouterr().out
    assert 'akash_canary_orphan_escrow_uact{dseq="7"} 42' in out
    assert "akash_canary_orphans_total 1" in out


def test_a_boolean_escrow_is_not_worth_one_uact():
    """bool IS an int in Python, so `escrow_uact: true` would publish as 1 — a plausible
    number invented from a type error. canary/collect.py excludes bool from _is_number for
    exactly this reason. Caught by pyright, not by the original try/except."""
    out = render(
        _rep(deployments=[{"dseq": "1", "classification": "ORPHANED", "escrow_uact": True}])
    )
    assert 'akash_canary_orphan_escrow_uact{dseq="1"} 0' in out
    assert "akash_canary_orphan_escrow_uact_total 0" in out


def test_a_numeric_string_escrow_is_accepted():
    """The LCD has been seen returning amounts as strings."""
    out = render(
        _rep(deployments=[{"dseq": "1", "classification": "ORPHANED", "escrow_uact": " 2000000 "}])
    )
    assert 'akash_canary_orphan_escrow_uact{dseq="1"} 2000000' in out


def test_a_float_escrow_truncates_rather_than_crashing():
    out = render(
        _rep(deployments=[{"dseq": "1", "classification": "ORPHANED", "escrow_uact": 2000000.9}])
    )
    assert 'akash_canary_orphan_escrow_uact{dseq="1"} 2000000' in out
