"""Unit tests for the Prometheus textfile-collector exporter.

Covers: the outcome counter (incl. the natural errors no-credit / no-bid /
lease-down / no-room, plus the never-reached "-" row), the latency percentile
summary, the last-run freshness gauge, and the deploy-credit gauge formatting.
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import patch

from just_akash import prometheus_exporter as px


def _rec(provider, feature, outcome, latency=None, ts=None):
    r = {"provider": provider, "feature": feature, "outcome": outcome, "latency_ms": latency}
    if ts is not None:
        r["ts"] = ts
    return r


def _lines(text_or_lines):
    if isinstance(text_or_lines, str):
        return text_or_lines.splitlines()
    return text_or_lines


class TestOutcomeCounters:
    def test_natural_errors_become_first_class_series(self):
        recs = [
            _rec("prov1", "deploy", "NO-CREDIT"),
            _rec("prov1", "deploy", "NO-BID"),
            _rec("prov1", "exec", "LEASE-DOWN"),
            _rec("prov2", "deploy", "NO-ROOM"),
            _rec("prov1", "deploy", "PASS"),
            _rec("prov1", "deploy", "PASS"),
            _rec("prov1", "exec", "FAIL"),
            _rec("prov1", "ssh", "-"),  # never reached
        ]
        lines = px.render_outcome_counters(recs)
        assert f"# TYPE {px.OUTCOME_METRIC} counter" in lines
        # no-credit / no-bid / lease-down / no-room are lowercased first-class labels.
        assert (
            'just_akash_smoke_outcome_total{provider="prov1",feature="deploy",'
            'outcome="no-credit"} 1' in lines
        )
        assert (
            'just_akash_smoke_outcome_total{provider="prov1",feature="deploy",'
            'outcome="no-bid"} 1' in lines
        )
        assert (
            'just_akash_smoke_outcome_total{provider="prov1",feature="exec",'
            'outcome="lease-down"} 1' in lines
        )
        assert (
            'just_akash_smoke_outcome_total{provider="prov2",feature="deploy",'
            'outcome="no-room"} 1' in lines
        )
        # PASS is counted (2 records) and the bare "-" becomes "unreached".
        assert (
            'just_akash_smoke_outcome_total{provider="prov1",feature="deploy",'
            'outcome="pass"} 2' in lines
        )
        assert (
            'just_akash_smoke_outcome_total{provider="prov1",feature="ssh",'
            'outcome="unreached"} 1' in lines
        )

    def test_counter_totals_conserve_every_record(self):
        recs = [_rec("p", "deploy", "PASS") for _ in range(3)] + [_rec("p", "deploy", "FAIL")]
        lines = px.render_outcome_counters(recs)
        sample_totals = sum(
            int(ln.rsplit(" ", 1)[1]) for ln in lines if ln.startswith(px.OUTCOME_METRIC)
        )
        assert sample_totals == len(recs)  # nothing dropped, nothing double-counted

    def test_rows_missing_provider_or_feature_are_skipped(self):
        recs = [_rec(None, "deploy", "PASS"), _rec("p", None, "PASS"), _rec("p", "deploy", "PASS")]
        lines = [ln for ln in px.render_outcome_counters(recs) if ln.startswith(px.OUTCOME_METRIC)]
        assert lines == [
            'just_akash_smoke_outcome_total{provider="p",feature="deploy",outcome="pass"} 1'
        ]


class TestLatencySummary:
    def test_percentiles_over_pass_samples_only(self):
        recs = [
            _rec("p", "ingress", "PASS", 100),
            _rec("p", "ingress", "PASS", 300),
            _rec("p", "ingress", "FAIL", 99999),  # excluded (not PASS)
            _rec("p", "ingress", "PASS", None),  # excluded (no number)
        ]
        lines = px.render_latency_summary(recs)
        assert f"# TYPE {px.LATENCY_METRIC} gauge" in lines
        # p50 of [100, 300] == 200 (matches analyze_telemetry._percentile).
        assert (
            'just_akash_smoke_latency_ms{provider="p",feature="ingress",quantile="0.5"} 200'
            in lines
        )

    def test_bool_latency_is_not_treated_as_a_number(self):
        # True is an int subclass; it must not sneak in as 1ms.
        recs = [_rec("p", "exec", "PASS", True)]
        lines = [ln for ln in px.render_latency_summary(recs) if ln.startswith(px.LATENCY_METRIC)]
        assert lines == []

    def test_no_pass_samples_emits_no_series(self):
        recs = [_rec("p", "deploy", "NO-BID", None)]
        lines = [ln for ln in px.render_latency_summary(recs) if ln.startswith(px.LATENCY_METRIC)]
        assert lines == []


class TestLastRun:
    def test_emits_max_epoch_from_iso_timestamps(self):
        recs = [
            _rec("p", "deploy", "PASS", 1, ts="2026-07-18T07:00:00+00:00"),
            _rec("p", "deploy", "PASS", 1, ts="2026-07-19T07:00:00+00:00"),  # newer
        ]
        lines = px.render_last_run(recs)
        value = float(next(ln for ln in lines if ln.startswith(px.LAST_RUN_METRIC)).split()[-1])
        expected = datetime.fromisoformat("2026-07-19T07:00:00+00:00").timestamp()
        assert value == expected  # the NEWER of the two runs

    def test_tolerates_trailing_z(self):
        recs = [_rec("p", "deploy", "PASS", 1, ts="2026-07-19T07:00:00Z")]
        assert px.render_last_run(recs)  # parsed, not skipped

    def test_no_parseable_ts_emits_nothing(self):
        recs = [_rec("p", "deploy", "PASS", 1, ts="not-a-date"), _rec("p", "deploy", "PASS", 1)]
        assert px.render_last_run(recs) == []


class TestDeployCreditGauge:
    def test_formats_usd_with_account_label(self):
        lines = px.render_deploy_credit_gauge("akash1me", 170.62)
        assert f"# TYPE {px.CREDIT_METRIC} gauge" in lines
        assert 'just_akash_deploy_credit_usd{account="akash1me"} 170.62' in lines

    def test_integral_credit_renders_without_decimal_point(self):
        lines = px.render_deploy_credit_gauge("akash1me", 170.0)
        assert 'just_akash_deploy_credit_usd{account="akash1me"} 170' in lines

    def test_unknown_credit_declares_family_without_a_sample(self):
        lines = px.render_deploy_credit_gauge("akash1me", None)
        assert f"# TYPE {px.CREDIT_METRIC} gauge" in lines
        assert not any(ln.startswith("just_akash_deploy_credit_usd{") for ln in lines)


class TestRenderMetrics:
    def test_full_document_has_all_families(self):
        recs = [_rec("p", "deploy", "PASS", 500, ts="2026-07-19T07:00:00+00:00")]
        text = px.render_metrics(recs, credit=("akash1me", 42.0))
        assert px.OUTCOME_METRIC in text
        assert px.LATENCY_METRIC in text
        assert px.LAST_RUN_METRIC in text
        assert 'just_akash_deploy_credit_usd{account="akash1me"} 42' in text
        assert text.endswith("\n")

    def test_no_credit_when_not_requested(self):
        text = px.render_metrics([_rec("p", "deploy", "PASS", 1)])
        assert px.CREDIT_METRIC not in text


class TestRunAndIO:
    def test_run_reads_jsonl_and_writes_atomic_file(self, tmp_path):
        src = tmp_path / "t.jsonl"
        src.write_text(json.dumps(_rec("p", "deploy", "NO-CREDIT")) + "\n")
        out = tmp_path / "smoke.prom"
        rc = px.run(str(src), output=str(out))
        assert rc == 0
        body = out.read_text()
        assert 'outcome="no-credit"} 1' in body
        # No temp leftovers from the atomic write.
        assert not list(tmp_path.glob(".metrics-*.tmp"))

    def test_run_missing_file_returns_2(self, tmp_path):
        assert px.run(str(tmp_path / "nope.jsonl")) == 2

    def test_main_stdout(self, tmp_path, capsys):
        src = tmp_path / "t.jsonl"
        src.write_text(json.dumps(_rec("p", "deploy", "PASS", 10)) + "\n")
        rc = px.main([str(src)])
        assert rc == 0
        assert px.OUTCOME_METRIC in capsys.readouterr().out


class TestResolveDeployCredit:
    def test_no_api_key_returns_none_and_warns(self, monkeypatch, capsys):
        monkeypatch.delenv("AKASH_API_KEY", raising=False)
        assert px.resolve_deploy_credit() is None
        assert "AKASH_API_KEY" in capsys.readouterr().err

    def test_reads_account_and_usd_from_chain(self, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        with (
            patch("just_akash.api.AkashConsoleAPI") as MockAPI,
            patch("just_akash.chain.deploy_credit", return_value={"uact": 170_000_000}),
        ):
            MockAPI.return_value.account_address.return_value = "akash1me"
            assert px.resolve_deploy_credit() == ("akash1me", 170.0)

    def test_query_failure_returns_none(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        with (
            patch("just_akash.api.AkashConsoleAPI") as MockAPI,
            patch("just_akash.chain.deploy_credit", side_effect=RuntimeError("LCD down")),
        ):
            MockAPI.return_value.account_address.return_value = "akash1me"
            assert px.resolve_deploy_credit() is None
        assert "deploy-credit query failed" in capsys.readouterr().err

    def test_run_with_credit_emits_gauge(self, tmp_path):
        src = tmp_path / "t.jsonl"
        src.write_text(json.dumps(_rec("p", "deploy", "PASS", 10)) + "\n")
        out = tmp_path / "smoke.prom"
        with patch.object(px, "resolve_deploy_credit", return_value=("akash1me", 42.0)):
            rc = px.run(str(src), output=str(out), with_credit=True)
        assert rc == 0
        assert 'just_akash_deploy_credit_usd{account="akash1me"} 42' in out.read_text()


def _bench(provider, ts="2026-07-20T12:32:55+00:00", complete=True, **overrides):
    """A realistic accrued smoke-benchmark.jsonl row (values from a live grade)."""
    r = {
        "ts": ts,
        "provider": provider,
        "complete": complete,
        "cpu_eps": "787.79",
        "cpu_samples": "781.22 793.84 777.73 773.39 786.99",
        "thr_pre": "1",
        "thr_post": "6",
        "thrus_pre": "625",
        "thrus_post": "14396",
        "steal_pre": "0",
        "steal_post": "0",
        "cputot_pre": "43184696563",
        "cputot_post": "43184718002",
        "cpu_psi_load": "avg10=15.63",
        "mem_bw": "7320.98 MiB/sec",
        "dseq": "1784551027087",
    }
    if complete:
        r["done"] = "1"
    r.update(overrides)
    return r


class TestBenchmarkMetrics:
    def test_gauges_from_a_complete_grade(self):
        lines = px.render_benchmark_metrics([_bench("prov1")])
        assert f"# TYPE {px.BENCH_CPU_EPS_METRIC} gauge" in lines
        assert 'just_akash_bench_cpu_events_per_s{provider="prov1"} 787.79' in lines
        # Fidelity deltas: 6-1 throttle events, 14396-625 usec — the honesty signals.
        assert 'just_akash_bench_cpu_throttled_events{provider="prov1"} 5' in lines
        assert 'just_akash_bench_cpu_throttled_usec{provider="prov1"} 13771' in lines
        assert 'just_akash_bench_steal_pct{provider="prov1"} 0' in lines
        assert 'just_akash_bench_cpu_psi_load{provider="prov1"} 15.63' in lines
        # Throttled during a single-threaded run => under-delivering verdict.
        assert 'just_akash_bench_under_delivering{provider="prov1"} 1' in lines
        assert 'just_akash_bench_mem_bandwidth_mib_s{provider="prov1"} 7320.98' in lines
        assert any(
            ln.startswith('just_akash_bench_last_run_timestamp{provider="prov1"}') for ln in lines
        )

    def test_stability_matches_the_benchmark_modules_own_math(self):
        from just_akash import benchmark

        row = _bench("prov1")
        expected_cv = benchmark.stability(row)["cpu_cv_pct"]
        lines = px.render_benchmark_metrics([row])
        got = next(ln for ln in lines if ln.startswith(px.BENCH_CPU_CV_METRIC + "{"))
        assert float(got.split()[-1]) == expected_cv
        # A steady grade (cv ~1%) reads stable.
        assert 'just_akash_bench_cpu_unstable{provider="prov1"} 0' in lines

    def test_incomplete_grade_is_never_exported(self):
        # No BENCH-done — a cut-short partial sample must not become the gauge.
        lines = px.render_benchmark_metrics([_bench("prov1", complete=False)])
        assert lines == []

    def test_latest_complete_grade_wins(self):
        old = _bench("prov1", ts="2026-07-01T00:00:00+00:00", cpu_eps="100")
        new = _bench("prov1", ts="2026-07-19T00:00:00+00:00", cpu_eps="900")
        newer_but_incomplete = _bench(
            "prov1", ts="2026-07-20T00:00:00+00:00", cpu_eps="1", complete=False
        )
        lines = px.render_benchmark_metrics([new, old, newer_but_incomplete])
        assert 'just_akash_bench_cpu_events_per_s{provider="prov1"} 900' in lines

    def test_absent_inputs_stay_absent_not_zero(self):
        # Only cpu_eps was measurable: no honesty inputs, <2 stability samples.
        row = {"ts": "2026-07-20T00:00:00+00:00", "provider": "p", "done": "1", "cpu_eps": "500"}
        lines = px.render_benchmark_metrics([row])
        assert 'just_akash_bench_cpu_events_per_s{provider="p"} 500' in lines
        # under_delivering unmeasured (no throttle/steal/psi input) — absent, not 0.
        assert not any(px.BENCH_UNDER_DELIVERING_METRIC in ln for ln in lines)
        assert not any(px.BENCH_CPU_CV_METRIC in ln for ln in lines)
        assert not any(px.BENCH_CPU_UNSTABLE_METRIC in ln for ln in lines)

    def test_render_metrics_includes_bench_only_when_given(self):
        recs = [_rec("p", "deploy", "PASS", 1)]
        assert px.BENCH_CPU_EPS_METRIC not in px.render_metrics(recs)
        text = px.render_metrics(recs, benchmark_records=[_bench("p")])
        assert px.BENCH_CPU_EPS_METRIC in text

    def test_run_with_benchmark_file(self, tmp_path):
        src = tmp_path / "t.jsonl"
        src.write_text(json.dumps(_rec("p", "deploy", "PASS", 10)) + "\n")
        bench = tmp_path / "b.jsonl"
        bench.write_text(json.dumps(_bench("p")) + "\n")
        out = tmp_path / "smoke.prom"
        assert px.run(str(src), output=str(out), benchmark_path=str(bench)) == 0
        assert px.BENCH_CPU_EPS_METRIC in out.read_text()

    def test_run_with_missing_benchmark_file_still_renders(self, tmp_path, capsys):
        src = tmp_path / "t.jsonl"
        src.write_text(json.dumps(_rec("p", "deploy", "PASS", 10)) + "\n")
        out = tmp_path / "smoke.prom"
        rc = px.run(str(src), output=str(out), benchmark_path=str(tmp_path / "nope.jsonl"))
        assert rc == 0  # optional stream: its absence never sinks the export
        assert px.OUTCOME_METRIC in out.read_text()
        assert "skipping benchmark file" in capsys.readouterr().err


class TestCreditJson:
    def test_loads_balance_check_snapshot(self, tmp_path):
        f = tmp_path / "credit.json"
        f.write_text(
            json.dumps(
                {
                    "check": "deploy_credit",
                    "status": "OK",
                    "account": "akash1me",
                    "deploy_credit_usd": 170.62,
                    "min_usd": 0.0,
                }
            )
        )
        assert px.load_credit_json(str(f)) == ("akash1me", 170.62)

    def test_malformed_snapshot_returns_none_and_warns(self, tmp_path, capsys):
        f = tmp_path / "credit.json"
        f.write_text('{"account": 42}')
        assert px.load_credit_json(str(f)) is None
        assert "unusable credit snapshot" in capsys.readouterr().err

    def test_missing_snapshot_returns_none(self, tmp_path, capsys):
        assert px.load_credit_json(str(tmp_path / "nope.json")) is None
        assert "unusable credit snapshot" in capsys.readouterr().err

    def test_run_with_credit_json_emits_gauge_without_api_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AKASH_API_KEY", raising=False)
        src = tmp_path / "t.jsonl"
        src.write_text(json.dumps(_rec("p", "deploy", "PASS", 10)) + "\n")
        f = tmp_path / "credit.json"
        f.write_text(json.dumps({"account": "akash1me", "deploy_credit_usd": 42.0}))
        out = tmp_path / "smoke.prom"
        assert px.run(str(src), output=str(out), credit_json=str(f)) == 0
        assert 'just_akash_deploy_credit_usd{account="akash1me"} 42' in out.read_text()

    def test_cli_rejects_both_credit_sources(self, tmp_path):
        src = tmp_path / "t.jsonl"
        src.write_text(json.dumps(_rec("p", "deploy", "PASS", 10)) + "\n")
        try:
            px.main([str(src), "--with-credit", "--credit-json", "x.json"])
        except SystemExit as e:
            assert e.code == 2  # argparse mutual-exclusion error
        else:
            raise AssertionError("expected SystemExit")

    def test_main_with_benchmark_and_credit_json(self, tmp_path, capsys):
        src = tmp_path / "t.jsonl"
        src.write_text(json.dumps(_rec("p", "deploy", "PASS", 10)) + "\n")
        bench = tmp_path / "b.jsonl"
        bench.write_text(json.dumps(_bench("p")) + "\n")
        credit = tmp_path / "credit.json"
        credit.write_text(json.dumps({"account": "akash1me", "deploy_credit_usd": 7.5}))
        rc = px.main([str(src), "--benchmark", str(bench), "--credit-json", str(credit)])
        assert rc == 0
        out = capsys.readouterr().out
        assert px.BENCH_CPU_EPS_METRIC in out
        assert 'just_akash_deploy_credit_usd{account="akash1me"} 7.5' in out


class TestBenchmarkPoisonRows:
    """One type-drifted row on the append-only branch must never freeze the export
    (the accrued file is re-read in FULL every run, so a raising row would poison
    every future render, not just one)."""

    def test_numeric_and_list_fields_degrade_to_unmeasured(self, capsys):
        poisoned = _bench(
            "prov1",
            cpu_eps=900.5,  # JSON number, not the writer's string
            cpu_samples=[781.2, 793.8],  # JSON array
            cpu_psi_load={"avg10": 15.63},  # even a dict must not raise
        )
        lines = px.render_benchmark_metrics([poisoned])
        # Stringified number is salvaged; un-stringifiable fields read unmeasured.
        assert 'just_akash_bench_cpu_events_per_s{provider="prov1"} 900.5' in lines
        assert not any(px.BENCH_CPU_CV_METRIC in ln for ln in lines)
        assert not any(px.BENCH_PSI_METRIC in ln for ln in lines)
        # The honest string fields still render.
        assert 'just_akash_bench_steal_pct{provider="prov1"} 0' in lines

    def test_poisoned_provider_never_hides_a_healthy_one(self):
        healthy = _bench("prov-ok")
        poisoned = {"provider": "prov-bad", "done": "1", "ts": 12345, "cpu_eps": ["x"]}
        lines = px.render_benchmark_metrics([poisoned, healthy])
        assert 'just_akash_bench_cpu_events_per_s{provider="prov-ok"} 787.79' in lines

    def test_full_render_survives_a_poisoned_jsonl(self, tmp_path):
        src = tmp_path / "t.jsonl"
        src.write_text(json.dumps(_rec("p", "deploy", "PASS", 10)) + "\n")
        bench = tmp_path / "b.jsonl"
        bench.write_text(
            json.dumps(_bench("p", cpu_eps=900.5, cpu_samples=[1, 2]))
            + "\n"
            + json.dumps({"provider": 42, "done": "1"})  # non-string provider: skipped
            + "\n"
        )
        out = tmp_path / "smoke.prom"
        assert px.run(str(src), output=str(out), benchmark_path=str(bench)) == 0
        body = out.read_text()
        assert px.OUTCOME_METRIC in body  # the primary stream always renders


class TestLatestOutcomes:
    def test_latest_run_wins_per_provider_feature(self):
        recs = [
            _rec("p", "update", "FAIL", 9000, ts="2026-07-21T17:03:00+00:00"),
            _rec("p", "update", "PASS", 30000, ts="2026-07-21T17:16:00+00:00"),
            _rec("p", "deploy", "NO-BID", None, ts="2026-07-21T17:16:00+00:00"),
        ]
        lines = px.render_latest_outcomes(recs)
        assert f"# TYPE {px.LATEST_OUTCOME_METRIC} gauge" in lines
        # The 17:16 PASS shadows the 17:03 FAIL — only ONE series per (p, feature),
        # so a failing-outcome alert auto-resolves on the next passing run.
        assert (
            'just_akash_smoke_latest_outcome_info{provider="p",feature="update",'
            'outcome="pass"} 1' in lines
        )
        assert not any('feature="update",outcome="fail"' in ln for ln in lines)
        assert (
            'just_akash_smoke_latest_outcome_info{provider="p",feature="deploy",'
            'outcome="no-bid"} 1' in lines
        )

    def test_unparseable_ts_falls_back_to_file_order(self):
        recs = [
            _rec("p", "exec", "FAIL", 1),  # no ts at all
            _rec("p", "exec", "PASS", 1),  # later line wins
        ]
        lines = [
            ln for ln in px.render_latest_outcomes(recs) if ln.startswith(px.LATEST_OUTCOME_METRIC)
        ]
        assert lines == [
            'just_akash_smoke_latest_outcome_info{provider="p",feature="exec",outcome="pass"} 1'
        ]

    def test_empty_records_render_nothing(self):
        assert px.render_latest_outcomes([]) == []

    def test_included_in_full_document(self):
        recs = [_rec("p", "deploy", "PASS", 500, ts="2026-07-19T07:00:00+00:00")]
        assert px.LATEST_OUTCOME_METRIC in px.render_metrics(recs)


class TestLatestLatencies:
    def test_latest_pass_wins_and_failures_hold_last_good(self):
        recs = [
            _rec("p", "deploy", "PASS", 41000, ts="2026-07-21T07:00:00+00:00"),
            _rec("p", "deploy", "PASS", 39000, ts="2026-07-22T07:00:00+00:00"),
            # A later FAIL must NOT overwrite the last good timing (time-to-failure
            # is not the feature's cost) — the series holds 39000.
            _rec("p", "deploy", "FAIL", 5000, ts="2026-07-22T09:00:00+00:00"),
        ]
        lines = px.render_latest_latencies(recs)
        assert f"# TYPE {px.LATEST_LATENCY_METRIC} gauge" in lines
        assert 'just_akash_smoke_latest_latency_ms{provider="p",feature="deploy"} 39000' in lines
        assert not any(" 5000" in ln for ln in lines)

    def test_never_passed_pair_is_absent(self):
        recs = [_rec("p", "deploy", "NO-BID", None, ts="2026-07-22T07:00:00+00:00")]
        assert [
            ln
            for ln in px.render_latest_latencies(recs)
            if ln.startswith(px.LATEST_LATENCY_METRIC)
        ] == []

    def test_bool_latency_never_sneaks_in(self):
        recs = [_rec("p", "exec", "PASS", True, ts="2026-07-22T07:00:00+00:00")]
        assert px.render_latest_latencies(recs) == []

    def test_included_in_full_document(self):
        recs = [_rec("p", "deploy", "PASS", 500, ts="2026-07-19T07:00:00+00:00")]
        assert px.LATEST_LATENCY_METRIC in px.render_metrics(recs)
