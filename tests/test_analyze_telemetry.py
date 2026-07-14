"""Unit tests for the telemetry aggregation / percentile logic."""

from __future__ import annotations

import json

import pytest

from just_akash import analyze_telemetry as at


class TestPercentile:
    def test_linear_interpolation(self):
        assert at._percentile([1, 2, 3, 4], 50) == 2.5

    def test_p99_interpolates_near_top(self):
        assert at._percentile([1, 2, 3, 4], 99) == pytest.approx(3.97)

    def test_single_value(self):
        assert at._percentile([5], 95) == 5.0

    def test_empty_is_none(self):
        assert at._percentile([], 50) is None

    def test_unsorted_input_handled(self):
        assert at._percentile([4, 1, 3, 2], 50) == 2.5


class TestMedianMad:
    def test_median_and_mad(self):
        # values 1,2,4,8 -> median 3.0; abs devs 2,1,1,5 -> MAD median 1.5
        med, mad = at._median_and_mad([1, 2, 4, 8])
        assert med == 3.0
        assert mad == 1.5

    def test_empty_is_none(self):
        assert at._median_and_mad([]) is None


class TestAggregate:
    def _rec(self, provider, feature, outcome, latency):
        return {
            "provider": provider,
            "feature": feature,
            "outcome": outcome,
            "latency_ms": latency,
        }

    def test_groups_and_pass_rate(self):
        recs = [
            self._rec("p1", "ingress", "PASS", 100),
            self._rec("p1", "ingress", "PASS", 300),
            self._rec("p1", "ingress", "FAIL", None),
        ]
        g = at.aggregate(recs)[("p1", "ingress")]
        assert g["count"] == 3
        assert g["pass"] == 2
        assert g["fail"] == 1
        assert g["pass_rate"] == 2 / 3
        assert g["p50"] == 200  # median of [100, 300]

    def test_latency_only_from_pass_with_number(self):
        # FAIL latency and a None PASS latency must NOT enter the distribution.
        recs = [
            self._rec("p", "exec", "PASS", 50),
            self._rec("p", "exec", "FAIL", 9999),  # excluded (not PASS)
            self._rec("p", "exec", "PASS", None),  # excluded (no number)
        ]
        g = at.aggregate(recs)[("p", "exec")]
        assert g["p50"] == 50
        assert g["max"] == 50

    def test_no_latency_samples_leaves_percentiles_none(self):
        g = at.aggregate([self._rec("p", "deploy", "NO-BID", None)])[("p", "deploy")]
        assert g["other"] == 1
        assert g["p99"] is None


class TestSloBreaches:
    def _g(self, count, passes):
        return {"count": count, "pass": passes, "pass_rate": passes / count}

    def test_respects_min_samples(self):
        groups = {("p", "ingress"): self._g(3, 1)}  # 33% but only 3 samples
        assert at.slo_breaches(groups, min_samples=20, slo=0.95) == []

    def test_flags_below_slo_with_enough_samples(self):
        groups = {("p", "ingress"): self._g(20, 10)}  # 50% over 20 -> breach
        out = at.slo_breaches(groups, min_samples=20, slo=0.95)
        assert out == [("p", "ingress", 0.5, 20)]

    def test_ok_above_slo(self):
        groups = {("p", "ingress"): self._g(20, 20)}
        assert at.slo_breaches(groups, min_samples=20, slo=0.95) == []


class TestReportAndMain:
    def test_report_flags_low_pass(self):
        recs = [
            {"provider": "p", "feature": "ingress", "outcome": "FAIL", "latency_ms": None}
            for _ in range(3)
        ]
        report = at.format_report(at.aggregate(recs))
        assert "LOW-PASS" in report

    def test_load_records_skips_blank_and_corrupt(self, tmp_path):
        f = tmp_path / "t.jsonl"
        f.write_text(
            '{"provider":"p","feature":"exec","outcome":"PASS","latency_ms":1}\n\nnot json\n'
        )
        recs = at.load_records(str(f))
        assert len(recs) == 1

    def test_main_check_returns_1_on_breach(self, tmp_path, capsys):
        rows = [
            {"provider": "p", "feature": "ingress", "outcome": "FAIL", "latency_ms": None}
            for _ in range(20)
        ]
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        rc = at.main([str(f), "--check", "--min-samples", "20"])
        assert rc == 1
        assert "SLO breach" in capsys.readouterr().out

    def test_main_no_records_is_ok(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        assert at.main([str(f)]) == 0
