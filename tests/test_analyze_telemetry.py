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

    def test_main_check_reliability_breach_is_informational_not_gating(self, tmp_path, capsys):
        """Reliability must PRINT but never gate: this accrued view can't tell a
        tooling regression on a healthy lease (the smoke run gates that, with the
        diag context) from provider infra the project demoted (LEASE-DOWN /
        quarantined). Gating here would red CI on exactly the latter."""
        rows = [
            {"provider": "p", "feature": "ingress", "outcome": "FAIL", "latency_ms": None}
            for _ in range(20)
        ]
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        rc = at.main([str(f), "--check", "--min-samples", "20"])
        assert rc == 0  # informational — NOT a gate
        out = capsys.readouterr().out
        assert "informational, NOT gating" in out
        assert "0% over 20 attempts" in out  # still fully visible

    def test_lease_down_never_gates_and_is_not_a_fail(self, tmp_path, capsys):
        """v1.22.0: LEASE-DOWN is provider infra, non-gating FLEET-WIDE. It must not
        count as a fail (it deflated the reported rate) nor affect the exit code."""
        rows = [
            {"provider": "p", "feature": "exec", "outcome": "LEASE-DOWN", "latency_ms": None}
            for _ in range(10)
        ] + [
            {"provider": "p", "feature": "exec", "outcome": "PASS", "latency_ms": 1000}
            for _ in range(20)
        ]
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        rc = at.main([str(f), "--check", "--min-samples", "20", "--max-p95", "exec=10000"])
        assert rc == 0
        g = at.aggregate(rows)[("p", "exec")]
        assert g["lease_down"] == 10
        assert g["fail"] == 0
        assert g["attempts"] == 20  # lease-downs stay OUT of the denominator
        assert g["pass_rate"] == 1.0  # 20/20, not 20/30

    def test_lease_down_is_visible_in_the_report_despite_100pct_pass(self):
        """LEASE-DOWN is out of the pass/fail rate, but it IS provider-health signal
        and must not vanish from the report — otherwise a provider that lease-downed
        most of its runs looks pristine at 100% pass over a tiny denominator. (Caught
        in review by Copilot, PR #60.)"""
        rows = [
            {"provider": "p", "feature": "exec", "outcome": "LEASE-DOWN", "latency_ms": None}
            for _ in range(10)
        ] + [{"provider": "p", "feature": "exec", "outcome": "PASS", "latency_ms": 1000}]
        report = at.format_report(at.aggregate(rows))
        assert "100%" in report  # the misleading-on-its-own headline...
        assert "LEASE-DOWN×10" in report  # ...now carries the health signal beside it

    def test_no_lease_down_flag_when_there_are_none(self):
        rows = [{"provider": "p", "feature": "exec", "outcome": "PASS", "latency_ms": 1000}]
        assert "LEASE-DOWN" not in at.format_report(at.aggregate(rows))

    def test_quarantined_provider_latency_breach_does_not_gate(self, tmp_path, capsys):
        """A quarantined provider being slow is the same known infra we already
        decided not to gate on — measured and printed, never failing."""
        rows = [
            {"provider": "bad", "feature": "ingress", "outcome": "PASS", "latency_ms": 50000}
            for _ in range(20)
        ]
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        rc = at.main(
            [
                str(f),
                "--check",
                "--min-samples",
                "20",
                "--max-p95",
                "ingress=10000",
                "--quarantine",
                "bad",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "TOO SLOW" in out and "quarantined" in out  # visible, just not gating

    def test_unquarantined_provider_latency_breach_still_gates(self, tmp_path):
        rows = [
            {"provider": "good", "feature": "ingress", "outcome": "PASS", "latency_ms": 50000}
            for _ in range(20)
        ]
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        rc = at.main(
            [
                str(f),
                "--check",
                "--min-samples",
                "20",
                "--max-p95",
                "ingress=10000",
                "--quarantine",
                "other",
            ]
        )
        assert rc == 1  # the latency gate is real for a non-quarantined provider

    def test_min_version_drops_old_and_unversioned_records(self, tmp_path, capsys):
        """A fixed bug's old failures can't recur, so they must not dilute the rate."""
        rows = (
            [
                {
                    "provider": "p",
                    "feature": "exec",
                    "outcome": "FAIL",
                    "latency_ms": None,
                    "version": "1.13.0",
                }
                for _ in range(10)
            ]
            + [{"provider": "p", "feature": "exec", "outcome": "FAIL", "latency_ms": None}]
            + [
                {
                    "provider": "p",
                    "feature": "exec",
                    "outcome": "PASS",
                    "latency_ms": 1000,
                    "version": "1.17.0",
                }
                for _ in range(20)
            ]
        )
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        rc = at.main([str(f), "--check", "--min-samples", "20", "--min-version", "1.17.0"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "kept 20 of 31" in out  # 10 old + 1 unversioned dropped

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

    def test_check_with_empty_ceilings_warns_that_the_gate_is_disabled(self, tmp_path, capsys):
        """--check with an empty --max-p95 gates on nothing. That is a valid
        calibration state, but it looks identical to a live gate silently disabled by
        an empty SMOKE_LATENCY_SLO_P95 env var — the exact class of failure this
        telemetry effort exists to catch. It must say so loudly, not print a bare
        green CHECK OK. (Caught in review by Copilot, PR #60.)"""
        rows = [
            {"provider": "p", "feature": "ingress", "outcome": "PASS", "latency_ms": 400}
            for _ in range(20)
        ]
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        rc = at.main([str(f), "--check", "--min-samples", "20", "--max-p95", ""])
        out = capsys.readouterr()
        # Still exits 0 (nothing to gate on), but the disabled state is unmissable.
        assert rc == 0
        assert "GATE DISABLED" in out.out
        assert "DISABLED" in out.err and "SMOKE_LATENCY_SLO_P95" in out.err

    def test_check_with_ceilings_does_not_warn_about_a_disabled_gate(self, tmp_path, capsys):
        """The warning must fire ONLY when ceilings are absent — a configured gate
        must stay quiet so the warning keeps its signal."""
        rows = [
            {"provider": "p", "feature": "ingress", "outcome": "PASS", "latency_ms": 400}
            for _ in range(20)
        ]
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        at.main([str(f), "--check", "--min-samples", "20", "--max-p95", "ingress=10000"])
        out = capsys.readouterr()
        assert "GATE DISABLED" not in out.out
        assert "DISABLED" not in out.err


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


class TestShimSurvey:
    """The issue-#85 removal condition, made evaluable.

    The condition is "zero null/missing exit_code occurrences across all active
    providers over 30 consecutive days". Before this existed the shim logged a
    human warning that nothing counted, so the condition could never be checked
    and the 'temporary' shim had no path to removal.
    """

    NEW = at.SHIM_SURVEY_MIN_VERSION

    def _rec(self, day, *, version=None, provider="akash1a", shapes=None):
        rec = {
            "ts": f"2026-08-{day:02d}T07:00:00+00:00",
            "version": version or self.NEW,
            "provider": provider,
            "feature": "exec",
            "outcome": "PASS",
        }
        if shapes:
            rec["exit_code_shapes"] = shapes
        return rec

    def test_pre_instrumentation_records_are_not_evidence(self):
        """The crux: an old record has no exit_code_shapes field because the field
        did not EXIST, not because the shim stayed quiet. Counting that silence
        would start the 30-day clock in the past and retire the shim on evidence
        that was never collected."""
        old = [self._rec(d, version="1.36.0") for d in range(1, 29)]
        survey = at.shim_survey(old)
        assert survey["eligible"] == 0
        assert survey["clean"] is False
        assert "KEEP THE SHIM" in at.format_shim_survey(survey)

    def test_clean_streak_shorter_than_required_is_not_removable(self):
        survey = at.shim_survey([self._rec(d) for d in range(1, 11)])  # ~9 days
        assert survey["occurrences"] == 0
        assert survey["clean"] is False
        assert "NOT YET" in at.format_shim_survey(survey)

    def test_thirty_clean_days_is_removable(self):
        survey = at.shim_survey([self._rec(d) for d in range(1, 32)])  # 30 days span
        assert survey["occurrences"] == 0
        assert survey["clean"] is True
        assert "REMOVABLE" in at.format_shim_survey(survey)

    def test_an_occurrence_keeps_the_shim_however_long_the_span(self):
        """A single real occurrence is decisive — it is proof the shim is
        load-bearing, and no amount of surrounding clean time overrides it."""
        records = [self._rec(d) for d in range(1, 32)]
        records.append(self._rec(2, shapes=["a null exit_code"]))
        survey = at.shim_survey(records)
        assert survey["occurrences"] == 1
        assert survey["clean"] is False
        report = at.format_shim_survey(survey)
        assert "LOAD-BEARING" in report and "KEEP THE SHIM" in report

    def test_streak_restarts_from_the_last_occurrence(self):
        """Clean days count from the last hit, not from the first record."""
        records = [self._rec(d) for d in range(1, 32)]
        records.append(self._rec(29, shapes=["no exit_code key"]))
        survey = at.shim_survey(records)
        assert survey["clean_days"] < 5  # not the ~30-day span of the file
        assert survey["clean"] is False

    def test_occurrences_are_attributed_per_provider(self):
        """The condition is 'across all active providers', so a hit has to name
        the provider it came from — a fleet-wide total can't answer it."""
        records = [
            self._rec(1, provider="akash1a", shapes=["a null exit_code"]),
            self._rec(2, provider="akash1a", shapes=["a null exit_code"]),
            self._rec(3, provider="akash1b", shapes=["no exit_code key"]),
        ]
        survey = at.shim_survey(records)
        assert survey["provider_hits"] == {"akash1a": 2, "akash1b": 1}
        assert survey["providers"] == ["akash1a", "akash1b"]

    def test_empty_shapes_list_is_not_an_occurrence(self):
        survey = at.shim_survey([self._rec(1, shapes=[])])
        assert survey["occurrences"] == 0

    def test_main_shim_survey_flag_runs(self, tmp_path, capsys):
        f = tmp_path / "t.jsonl"
        f.write_text("\n".join(json.dumps(self._rec(d)) for d in range(1, 32)))
        assert at.main([str(f), "--shim-survey"]) == 0
        assert "SHIM SURVEY (issue #85)" in capsys.readouterr().out
