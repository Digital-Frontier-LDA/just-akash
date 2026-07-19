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
