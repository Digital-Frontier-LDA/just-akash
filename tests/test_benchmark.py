"""Unit tests for the provider benchmark probe's parsing + reporting contract."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from just_akash import benchmark as bm


class TestParseResults:
    def test_parses_key_values(self):
        out = "BENCH-cpu_eps=1234.5\nBENCH-mem_bw=900 MiB/sec\nBENCH-done=1\n"
        r = bm.parse_results(out)
        assert r["cpu_eps"] == "1234.5"
        assert r["mem_bw"] == "900 MiB/sec"
        assert r["done"] == "1"

    def test_ignores_noise_lines(self):
        """apk/sysbench chatter must not become metrics."""
        out = "fetch https://dl-cdn.alpinelinux.org/...\nOK: 12 MiB\nBENCH-ncpu=4\nrandom text\n"
        assert bm.parse_results(out) == {"ncpu": "4"}

    def test_tolerates_a_line_prefix(self):
        """The exec/logs path can prefix lines; the marker is found anywhere."""
        assert bm.parse_results("[probe-abc] BENCH-ncpu=2\n") == {"ncpu": "2"}

    def test_empty_value_is_absent_not_empty_string(self):
        """An unavailable metric must read as UNMEASURED, never as a value.
        (`|| echo na` can't fire on a pipeline, so these arrive empty.)"""
        r = bm.parse_results("BENCH-cpu_model=\nBENCH-ncpu=1\n")
        assert "cpu_model" not in r
        assert r["ncpu"] == "1"

    def test_na_is_dropped_like_an_empty_value(self):
        """The contract (module docstring) is that an unavailable metric is ABSENT,
        however the probe spelled it. `na` must not leak into results/JSON as if it
        were a measurement. (Contract clarified in review — CodeRabbit, PR #61.)"""
        r = bm.parse_results("BENCH-cpu_eps=na\nBENCH-ncpu=4\n")
        assert "cpu_eps" not in r
        assert r["ncpu"] == "4"

    def test_empty_input(self):
        assert bm.parse_results("") == {}


class TestIsComplete:
    def test_done_marker_means_complete(self):
        assert bm.is_complete({"done": "1"}) is True

    def test_missing_done_means_partial(self):
        """No BENCH-done => the exec was cut short; the sample must not be graded."""
        assert bm.is_complete({"cpu_eps": "1000"}) is False
        assert bm.is_complete({}) is False


class TestFormatResults:
    def test_groups_by_dimension(self):
        text = bm.format_results(
            "prov1", {"cpu_eps": "900", "disk_write": "500 MB/s", "done": "1"}
        )
        assert "prov1" in text
        assert "cpu" in text and "cpu_eps=900" in text
        assert "disk" in text and "disk_write=500 MB/s" in text

    def test_flags_a_partial_sample(self):
        text = bm.format_results("prov1", {"cpu_eps": "900"})  # no done
        assert "incomplete" in text.lower()

    def test_no_output(self):
        assert "no benchmark output" in bm.format_results("prov1", {})


class TestBenchScript:
    def test_is_valid_posix_sh(self, tmp_path):
        """The probe runs under busybox ash inside the container — it must parse."""
        f = tmp_path / "b.sh"
        f.write_text(bm.BENCH_SH)
        assert subprocess.run(["sh", "-n", str(f)], capture_output=True).returncode == 0

    def test_is_valid_under_dash_when_available(self, tmp_path):
        """dash is the strict POSIX yardstick — closer to busybox ash than bash is.
        Guarded by which() so the suite still runs where dash isn't installed."""
        dash = shutil.which("dash")
        if not dash:
            pytest.skip("dash not installed")
        f = tmp_path / "b.sh"
        f.write_text(bm.BENCH_SH)
        assert subprocess.run([dash, "-n", str(f)], capture_output=True).returncode == 0

    def test_rtt_parse_takes_avg_not_max(self):
        """The RTT summary is min/avg/max[/mdev]; the parse must select AVG. A
        positional cut on the whole line grabs max/mdev and skews grading — regressed
        once (Copilot, PR #61), so pin the numeric-triple + field-2 approach."""
        assert "grep -oE '[0-9.]+/[0-9.]+/[0-9.]+'" in bm.BENCH_SH
        assert "cut -d/ -f2" in bm.BENCH_SH

    def test_disk_read_bypasses_the_page_cache(self):
        """conv=fdatasync flushes the write but leaves the pages cached, so the read
        must use iflag=direct or it measures RAM, not the disk."""
        assert "iflag=direct" in bm.BENCH_SH

    def test_stays_within_the_lease_limits(self):
        """The probe must never approach the probe SDL's 1Gi/5Gi: a benchmark that
        OOM-kills its own container would self-inflict the lease death this suite
        exists to measure (quorum-flagged)."""
        assert bm._MEM_MB <= 256
        assert bm._DISK_MB <= 256
        assert "--threads=1" in bm.BENCH_SH  # never peg all cores
        assert f"--memory-total-size={bm._MEM_MB}M" in bm.BENCH_SH
        # Bind the caps to the actual script: the bounds above still pass if BENCH_SH
        # later hardcodes a larger dd count than the constant. (CodeRabbit, PR #61.)
        assert f"count={bm._DISK_MB}" in bm.BENCH_SH

    def test_emits_done_last_so_truncation_is_detectable(self):
        assert bm.BENCH_SH.strip().endswith("say done 1")

    def test_cleans_up_its_disk_artifacts(self):
        """A 256M file left behind would eat the lease's storage."""
        assert 'rm -f "$BENCH_TMP"' in bm.BENCH_SH

    def test_a_killed_probe_still_cleans_up_via_trap(self):
        """The explicit rm only runs on the happy path; a trap covers a mid-run kill
        so no 256M artifact survives an interrupted probe. Check every signal, not
        just EXIT, so an INT/TERM regression can't hide. (CodeRabbit, PR #61.)"""
        assert "trap " in bm.BENCH_SH
        assert all(sig in bm.BENCH_SH for sig in ("EXIT", "INT", "TERM"))

    def test_disk_paths_are_pid_unique(self):
        """PID-scoped paths so nothing collides even if the assumption of one probe
        per container ever breaks."""
        assert "/tmp/.bench.$$" in bm.BENCH_SH


class TestBenchmarkJsonTrustsLocalMetadata:
    """build_json_record merges REMOTE probe output (results) with LOCAL, trusted
    metadata (dseq/provider/complete). A hostile or buggy probe emitting
    `BENCH-provider=` / `BENCH-dseq=` / `BENCH-complete=` must not be able to shadow
    the values we actually know. (CodeRabbit + Copilot, PR #61.)
    """

    def test_hostile_probe_keys_cannot_shadow_trusted_metadata(self):
        # All-string values: parse_results only ever yields strings, so a hostile
        # BENCH-complete= arrives as the string "true", not a bool.
        results = {"provider": "EVIL", "dseq": "0", "complete": "true", "cpu_eps": "900"}
        rec = bm.build_json_record("1784", "akash1real", results)
        assert rec["provider"] == "akash1real"  # not EVIL
        assert rec["dseq"] == "1784"  # not 0
        assert rec["complete"] is False  # our is_complete(), not the probe's string
        assert rec["cpu_eps"] == "900"  # genuine metrics still pass through

    def test_complete_reflects_the_done_marker_not_a_probe_claim(self):
        assert bm.build_json_record("1", "p", {"done": "1"})["complete"] is True
        assert bm.build_json_record("1", "p", {"cpu_eps": "900"})["complete"] is False


class TestResourceFidelity:
    """Delivered-vs-promised CPU: the "is it good?", not just "is it responsive?"
    signal (Step 1 of the provider-quality build)."""

    def _clean(self):
        # one thread ran, no throttling, no steal, no pressure
        return {
            "thr_pre": "5",
            "thr_post": "5",
            "thrus_pre": "0",
            "thrus_post": "0",
            "steal_pre": "1000",
            "steal_post": "1001",
            "cputot_pre": "100000",
            "cputot_post": "200000",
            "cpu_psi_load": "avg10=0.00",
        }

    def test_clean_provider_is_delivering(self):
        f = bm.resource_fidelity(self._clean())
        assert f["under_delivering"] is False
        assert f["reasons"] == []
        assert f["throttled_during"] == 0
        assert f["steal_pct"] == 0.0  # 1/100000

    def test_throttled_during_single_thread_is_under_delivering(self):
        r = self._clean() | {"thr_pre": "2", "thr_post": "9"}
        f = bm.resource_fidelity(r)
        assert f["under_delivering"] is True
        assert f["throttled_during"] == 7
        assert any("throttled" in x.lower() for x in f["reasons"])

    def test_high_steal_flags_a_vm_host(self):
        # 30000 stolen out of 100000 ticks = 30% steal
        r = self._clean() | {"steal_pre": "0", "steal_post": "30000"}
        f = bm.resource_fidelity(r)
        assert f["steal_pct"] == 30.0
        assert f["under_delivering"] is True
        assert any("steal" in x.lower() for x in f["reasons"])

    def test_high_psi_under_load_flags_contention(self):
        r = self._clean() | {"cpu_psi_load": "avg10=42.50"}
        f = bm.resource_fidelity(r)
        assert f["cpu_psi_load"] == 42.5
        assert f["under_delivering"] is True
        assert any("pressure" in x.lower() for x in f["reasons"])

    def test_small_steal_is_scheduler_noise_not_a_flag(self):
        r = self._clean() | {"steal_pre": "0", "steal_post": "2000"}  # 2% < 5% floor
        f = bm.resource_fidelity(r)
        assert f["steal_pct"] == 2.0
        assert f["under_delivering"] is False

    def test_absent_snapshots_are_omitted_never_guessed(self):
        f = bm.resource_fidelity({"cpu_eps": "900"})  # no fidelity keys
        assert "throttled_during" not in f
        assert "steal_pct" not in f
        assert f["under_delivering"] is False  # nothing measured = no accusation

    def test_zero_cpu_window_does_not_divide_by_zero(self):
        r = self._clean() | {"cputot_pre": "5000", "cputot_post": "5000"}
        f = bm.resource_fidelity(r)  # must not raise
        assert "steal_pct" not in f  # can't compute a rate over a zero window


class TestFidelityInReport:
    def test_report_flags_an_under_delivering_provider(self):
        r = {
            "cpu_eps": "900",
            "done": "1",
            "thr_pre": "0",
            "thr_post": "12",
            "cputot_pre": "1",
            "cputot_post": "2",
        }
        text = bm.format_results("prov", r)
        assert "fidelity" in text
        assert "UNDER-DELIVERING" in text
        assert "throttled_during=12" in text

    def test_report_confirms_a_healthy_provider(self):
        r = {
            "cpu_eps": "900",
            "done": "1",
            "thr_pre": "3",
            "thr_post": "3",
            "steal_pre": "0",
            "steal_post": "0",
            "cputot_pre": "100",
            "cputot_post": "200",
            "cpu_psi_load": "avg10=1.00",
        }
        text = bm.format_results("prov", r)
        assert "fidelity" in text and "OK (delivering" in text

    def test_report_omits_fidelity_when_unmeasured(self):
        text = bm.format_results("prov", {"cpu_eps": "900", "done": "1"})
        assert "fidelity" not in text


class TestStability:
    """Consistently-good vs fast-once: the sustained-load signal (Step 2)."""

    def test_steady_host_is_not_unstable(self):
        s = bm.stability({"cpu_samples": "1000 1010 990 1005 995"})
        assert s["unstable"] is False
        assert s["cpu_cv_pct"] < 5
        assert s["cpu_mean"] == 1000.0
        assert s["cpu_min"] == 990.0 and s["cpu_max"] == 1010.0

    def test_noisy_host_is_flagged_unstable(self):
        s = bm.stability({"cpu_samples": "1500 600 1400 500 900"})
        assert s["unstable"] is True
        assert s["cpu_cv_pct"] > bm._CV_LIMIT

    def test_fewer_than_two_samples_is_nothing_to_compare(self):
        assert bm.stability({"cpu_samples": "1000"}) == {}
        assert bm.stability({"cpu_samples": ""}) == {}
        assert bm.stability({}) == {}

    def test_non_numeric_samples_are_skipped(self):
        # a run that failed emits an empty token; it must not crash the parse
        s = bm.stability({"cpu_samples": "1000  na 1020 1010"})
        assert len(s["cpu_samples"]) == 3  # na dropped
        assert s["unstable"] is False

    def test_report_flags_an_unstable_provider(self):
        text = bm.format_results("p", {"cpu_samples": "1500 600 1400 500 900", "done": "1"})
        assert "stability" in text and "UNSTABLE" in text and "cv=" in text

    def test_report_calls_a_steady_provider_steady(self):
        text = bm.format_results("p", {"cpu_samples": "1000 1005 998 1002 1000", "done": "1"})
        assert "stability" in text and "steady" in text

    def test_report_omits_stability_without_samples(self):
        assert "stability" not in bm.format_results("p", {"cpu_eps": "900", "done": "1"})


class TestStabilityProbe:
    def test_probe_loops_the_configured_sample_count(self):
        # the stability loop bound must be the constant, not a hardcoded number
        assert f"-lt {bm._STABILITY_SAMPLES}" in bm.BENCH_SH
        assert "say cpu_samples" in bm.BENCH_SH

    def test_stability_runs_are_short_so_the_probe_stays_bounded(self):
        # each extra sample is --time=1 (not the 3s main run) to keep total CPU low
        assert "sysbench cpu --time=1 --threads=1" in bm.BENCH_SH
