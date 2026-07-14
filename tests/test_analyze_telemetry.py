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
        result = at._median_and_mad([1, 2, 4, 8])
        assert result is not None
        med, mad = result
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

    def test_pass_rate_excludes_nobid_and_unreached(self):
        # NO-BID and "-" are not failures -> excluded from the rate denominator.
        recs = [
            self._rec("p", "ingress", "PASS", 100),
            self._rec("p", "ingress", "-", None),  # never reached (deploy no-bid)
            self._rec("p", "ingress", "NO-BID", None),
        ]
        g = at.aggregate(recs)[("p", "ingress")]
        assert g["attempts"] == 1  # only the PASS is a real attempt
        assert g["pass_rate"] == 1.0  # 1/1, not 1/3

    def test_pass_rate_none_with_zero_attempts(self):
        g = at.aggregate([self._rec("p", "ingress", "NO-BID", None)])[("p", "ingress")]
        assert g["attempts"] == 0
        assert g["pass_rate"] is None


class TestSloBreaches:
    def _g(self, attempts, passes):
        fails = attempts - passes
        return {
            "attempts": attempts,
            "pass": passes,
            "fail": fails,
            "pass_rate": (passes / attempts) if attempts else None,
        }

    def test_respects_min_samples(self):
        groups = {("p", "ingress"): self._g(3, 1)}  # 33% but only 3 attempts
        assert at.slo_breaches(groups, min_samples=20, slo=0.95) == []

    def test_flags_below_slo_with_enough_samples(self):
        groups = {("p", "ingress"): self._g(20, 10)}  # 50% over 20 -> breach
        out = at.slo_breaches(groups, min_samples=20, slo=0.95)
        assert out == [("p", "ingress", 0.5, 20)]

    def test_ok_above_slo(self):
        groups = {("p", "ingress"): self._g(20, 20)}
        assert at.slo_breaches(groups, min_samples=20, slo=0.95) == []

    def test_zero_attempts_never_breaches(self):
        # NO-BID-only feature (attempts=0, pass_rate None) must not trip --check.
        groups = {("p", "ingress"): self._g(0, 0)}
        assert at.slo_breaches(groups, min_samples=1, slo=0.95) == []


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
        assert "RELIABILITY breach" in capsys.readouterr().out

    def test_main_check_fails_on_slow_p95(self, tmp_path, capsys):
        # 20 PASS ingress at 50s -> p95 ~50s, over a 10s ceiling -> too slow.
        rows = [
            {"provider": "p", "feature": "ingress", "outcome": "PASS", "latency_ms": 50000}
            for _ in range(20)
        ]
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        rc = at.main([str(f), "--check", "--min-samples", "20", "--max-p95", "ingress=10000"])
        assert rc == 1
        assert "TOO SLOW" in capsys.readouterr().out

    def test_main_check_ok_when_within_ceiling(self, tmp_path):
        rows = [
            {"provider": "p", "feature": "ingress", "outcome": "PASS", "latency_ms": 400}
            for _ in range(20)
        ]
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        assert (
            at.main([str(f), "--check", "--min-samples", "20", "--max-p95", "ingress=10000"]) == 0
        )


class TestLatencySlo:
    def test_parse_thresholds(self):
        assert at.parse_thresholds("ready=45000, ingress=15000") == {
            "ready": 45000.0,
            "ingress": 15000.0,
        }

    def test_parse_thresholds_empty(self):
        assert at.parse_thresholds("") == {}

    def test_parse_thresholds_malformed_raises(self):
        with pytest.raises(ValueError):
            at.parse_thresholds("ready")  # missing '=ms'

    def test_parse_thresholds_rejects_non_finite_or_nonpositive(self):
        # NaN would silently disable the gate (p95 > nan is always False).
        for bad in ("ready=nan", "ready=inf", "ready=0", "ready=-5"):
            with pytest.raises(ValueError):
                at.parse_thresholds(bad)

    def _g(self, p95, n_lat):
        return {"p95": p95, "n_lat": n_lat, "attempts": n_lat}

    def test_flags_slow_p95_with_enough_samples(self):
        groups = {("p", "ready"): self._g(50000, 20)}
        assert at.latency_breaches(groups, {"ready": 30000}, min_samples=20) == [
            ("p", "ready", 50000, 30000)
        ]

    def test_respects_min_samples(self):
        groups = {("p", "ready"): self._g(50000, 5)}  # slow but only 5 samples
        assert at.latency_breaches(groups, {"ready": 30000}, min_samples=20) == []

    def test_feature_without_threshold_never_flagged(self):
        groups = {("p", "exec"): self._g(50000, 20)}
        assert at.latency_breaches(groups, {"ready": 30000}, min_samples=20) == []

    def test_within_ceiling_ok(self):
        groups = {("p", "ready"): self._g(20000, 20)}
        assert at.latency_breaches(groups, {"ready": 30000}, min_samples=20) == []

    def test_main_no_records_is_ok(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        assert at.main([str(f)]) == 0
