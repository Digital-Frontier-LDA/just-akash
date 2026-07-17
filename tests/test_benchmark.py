"""Unit tests for the provider benchmark probe's parsing + reporting contract."""

from __future__ import annotations

import subprocess

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

    def test_na_is_kept_as_an_explicit_unavailable_marker(self):
        assert bm.parse_results("BENCH-cpu_eps=na\n")["cpu_eps"] == "na"

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

    def test_stays_within_the_lease_limits(self):
        """The probe must never approach the probe SDL's 1Gi/5Gi: a benchmark that
        OOM-kills its own container would self-inflict the lease death this suite
        exists to measure (quorum-flagged)."""
        assert bm._MEM_MB <= 256
        assert bm._DISK_MB <= 256
        assert "--threads=1" in bm.BENCH_SH  # never peg all cores
        assert f"--memory-total-size={bm._MEM_MB}M" in bm.BENCH_SH

    def test_emits_done_last_so_truncation_is_detectable(self):
        assert bm.BENCH_SH.strip().endswith("say done 1")

    def test_cleans_up_its_disk_artifacts(self):
        """A 256M file left behind would eat the lease's storage."""
        assert "rm -f /tmp/.bench" in bm.BENCH_SH
