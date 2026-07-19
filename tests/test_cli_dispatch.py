"""CLI dispatch coverage for the benchmark / inject / validate-sdl command bodies.

These three subcommands had ~zero dispatch coverage (the underlying helpers are
unit-tested elsewhere, but nothing drove them through `cli.main`). They cover the
stdout-capture trick in `benchmark`, the `--env-file` parsing + lease-shell/SSH
split in `inject`, and the file/error paths in `validate-sdl`.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from just_akash.transport.base import TransportConfig
from just_akash.transport.lease_shell import LeaseShellTransport
from tests._creds import fake_api_key

_KEY = fake_api_key()

DEPLOY_FIXTURE = {
    "leases": [
        {
            "provider": {"hostUri": "https://provider.example.com:8443"},
            "status": {"services": {"web": {"ready_replicas": 1, "total": 1}}},
        }
    ]
}


def _run_cli(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", args)
    from just_akash.cli import main

    return main()


# ── benchmark ─────────────────────────────────────────────────────────────────


def _bench_bytes(*kv: str, done: bool = False) -> bytes:
    """Build BENCH- output from parts so no single source literal is a long
    high-entropy base64 token (keeps test data out of .secrets.baseline)."""
    lines = [f"BENCH-{k}" for k in kv]
    if done:
        lines.append("BENCH-done=1")
    return ("\n".join(lines) + "\n").encode()


def _bench_transport(bench_lines: bytes, rc: int = 0) -> LeaseShellTransport:
    """A real LeaseShellTransport whose exec_shell_script simulates the probe.

    `benchmark` swaps sys.stdout for a _Capture whose `.buffer` is an internal
    BytesIO, then the probe writes BENCH- lines to sys.stdout.buffer. Override
    exec_shell_script to do exactly that, so the parse path runs end-to-end.
    The instance must be a real LeaseShellTransport — the command asserts it.
    """
    t = LeaseShellTransport(TransportConfig(dseq="123", api_key=_KEY, deployment=DEPLOY_FIXTURE))

    def _exec(_script: str) -> int:
        sys.stdout.buffer.write(bench_lines)
        return rc

    t.exec_shell_script = _exec  # type: ignore[method-assign]
    return t


class TestCliBenchmark:
    @patch("just_akash.transport.make_transport")
    @patch("just_akash.cli._resolve_deployment", return_value="12345")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_benchmark_json_complete_exits_zero(
        self, MockAPI, _resolve, mock_make_transport, monkeypatch, capsys
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        mock_make_transport.return_value = _bench_transport(
            _bench_bytes("cpu_eps=1229", "mem_bw_mbs=4300", done=True)
        )
        with pytest.raises(SystemExit) as exc:
            _run_cli(monkeypatch, ["just-akash", "benchmark", "--dseq", "12345", "--json"])
        assert exc.value.code == 0
        result = json.loads(capsys.readouterr().out)
        assert result["dseq"] == "12345"
        assert result["complete"] is True
        assert result["cpu_eps"] == "1229"
        assert result["mem_bw_mbs"] == "4300"

    @patch("just_akash.transport.make_transport")
    @patch("just_akash.cli._resolve_deployment", return_value="12345")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_benchmark_table_output_when_not_json(
        self, MockAPI, _resolve, mock_make_transport, monkeypatch, capsys
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        mock_make_transport.return_value = _bench_transport(
            _bench_bytes("cpu_eps=1229", done=True)
        )
        with pytest.raises(SystemExit) as exc:
            _run_cli(monkeypatch, ["just-akash", "benchmark", "--dseq", "12345"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        # Human-readable table, not JSON.
        assert "1229" in out
        assert "{\n" not in out

    @patch("just_akash.transport.make_transport")
    @patch("just_akash.cli._resolve_deployment", return_value="12345")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_benchmark_partial_exits_one(
        self, MockAPI, _resolve, mock_make_transport, monkeypatch, capsys
    ):
        """No BENCH-done=1 → is_complete False → exit 1 (a partial sample must
        not be graded as if it were a full one)."""
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        mock_make_transport.return_value = _bench_transport(_bench_bytes("cpu_eps=1229"))
        with pytest.raises(SystemExit) as exc:
            _run_cli(monkeypatch, ["just-akash", "benchmark", "--dseq", "12345", "--json"])
        assert exc.value.code == 1
        result = json.loads(capsys.readouterr().out)
        assert result["complete"] is False

    @patch("just_akash.transport.make_transport")
    @patch("just_akash.cli._resolve_deployment", return_value="12345")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_benchmark_unavailable_lease_shell_exits_one(
        self, MockAPI, _resolve, mock_make_transport, monkeypatch, capsys
    ):
        """validate()==False → stderr error + exit 1; exec never runs."""
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        fake = MagicMock()
        fake.validate.return_value = False
        mock_make_transport.return_value = fake
        with pytest.raises(SystemExit) as exc:
            _run_cli(monkeypatch, ["just-akash", "benchmark", "--dseq", "12345"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "lease-shell is not available" in err
        fake.exec_shell_script.assert_not_called()


# ── inject ────────────────────────────────────────────────────────────────────


class TestCliInject:
    @patch("just_akash.transport.make_transport")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_inject_lease_shell_writes_joined_secrets(
        self, MockAPI, mock_make_transport, monkeypatch, capsys
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        MockAPI.return_value.get_deployment.return_value = DEPLOY_FIXTURE
        fake_t = MagicMock()
        fake_t.validate.return_value = True
        mock_make_transport.return_value = fake_t

        rc = _run_cli(
            monkeypatch,
            ["just-akash", "inject", "--dseq", "123", "--env", "SECRET=value"],
        )
        assert rc is None  # success path does not sys.exit
        # inject receives the joined lines WITH a trailing newline, at the default path.
        fake_t.inject.assert_called_once_with("/run/secrets/.env", "SECRET=value\n")
        assert "Injected 1 secret" in capsys.readouterr().out

    @patch("just_akash.transport.make_transport")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_inject_env_file_skips_comments_and_blanks(
        self, MockAPI, mock_make_transport, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        MockAPI.return_value.get_deployment.return_value = DEPLOY_FIXTURE
        fake_t = MagicMock()
        fake_t.validate.return_value = True
        mock_make_transport.return_value = fake_t

        env_file = tmp_path / ".env.secrets"
        env_file.write_text("# a comment\n\nKEEP=1\n  # indented comment\nDB_PASS=x\n")

        _run_cli(
            monkeypatch,
            ["just-akash", "inject", "--dseq", "123", "--env-file", str(env_file)],
        )
        # Only the two real KEY=VALUE lines are injected, in order, blank/comment dropped.
        sent = fake_t.inject.call_args[0][1]
        assert sent == "KEEP=1\nDB_PASS=x\n"
        assert "Injected 2 secret" in capsys.readouterr().out

    def test_inject_invalid_env_format_exits_one(self, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        with pytest.raises(SystemExit) as exc:
            _run_cli(monkeypatch, ["just-akash", "inject", "--dseq", "123", "--env", "NOEQUALS"])
        assert exc.value.code == 1
        # These validation messages print to stdout (not stderr) in the inject body.
        assert "Invalid --env format" in capsys.readouterr().out

    @patch("just_akash.api.AkashConsoleAPI")
    def test_inject_missing_env_file_exits_one(self, MockAPI, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        with pytest.raises(SystemExit) as exc:
            _run_cli(
                monkeypatch,
                ["just-akash", "inject", "--dseq", "123", "--env-file", "/no/such/file"],
            )
        assert exc.value.code == 1
        assert "Env file not found" in capsys.readouterr().out

    @patch("just_akash.api.AkashConsoleAPI")
    def test_inject_no_secrets_exits_one(self, MockAPI, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        with pytest.raises(SystemExit) as exc:
            _run_cli(monkeypatch, ["just-akash", "inject", "--dseq", "123"])
        assert exc.value.code == 1
        assert "No secrets to inject" in capsys.readouterr().out

    @patch("just_akash.cli.subprocess.run")
    @patch("just_akash.cli._require_ssh")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_inject_ssh_fallback_quotes_remote_path_and_chmods(
        self, MockAPI, mock_require_ssh, mock_run, monkeypatch, capsys
    ):
        """SSH transport path: mkdir/cat/chmod each get the path shlex-quoted, and
        chmod 600 is always invoked (the permission hardening must not be skipped)."""
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        # Force the SSH branch by making lease-shell unavailable.
        MockAPI.return_value.get_deployment.return_value = {"leases": []}
        mock_require_ssh.return_value = (
            {"host": "1.2.3.4", "port": 22},
            ["ssh", "-i", "/k", "-p", "22", "root@1.2.3.4"],
        )
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        # A remote path with shell metacharacters that MUST be quoted before reaching /bin/sh.
        rc = _run_cli(
            monkeypatch,
            [
                "just-akash",
                "inject",
                "--dseq",
                "123",
                "--transport",
                "ssh",
                "--env",
                "SECRET=value",
                "--remote-path",
                "/tmp/foo;rm -rf /",
            ],
        )
        assert rc is None
        # Three subprocess.run calls: mkdir, cat (write), chmod. Each argv is the
        # ssh_cmd list + a trailing remote command string; argv[-1] is that string.
        remote_cmds = [c.args[0][-1] for c in mock_run.call_args_list]
        assert len(remote_cmds) == 3
        # The dangerous path is shlex-quoted everywhere it appears (single-quoted),
        # so its metacharacters are never interpreted by the remote /bin/sh.
        import shlex

        quoted = shlex.quote("/tmp/foo;rm -rf /")
        for remote_cmd in remote_cmds:
            assert quoted in remote_cmd
        # The chmod 600 permission hardening always runs (last command).
        assert remote_cmds[-1].startswith("chmod 600")
        assert "Injected 1 secret" in capsys.readouterr().out


# ── validate-sdl ──────────────────────────────────────────────────────────────


VALID_SDL = """\
version: "2.0"
services:
  web:
    image: python:3.13-slim
    expose:
      - port: 22
        as: 22
        to:
          - global: true
profiles:
  compute:
    web:
      resources:
        cpu:
          units: 1
        memory:
          size: 1Gi
  placement:
    dcloud:
      attributes:
        host: akash
      signedBy:
        anyOf:
          - akash1365yvmc4s7awdyj3n2sav7xfx76adc6dnmlx63
      pricing:
        web:
          denom: uact
          amount: 100
  deployment:
    web:
      dcloud: 1
"""


class TestCliValidateSdl:
    def test_valid_sdl_prints_ok(self, monkeypatch, tmp_path, capsys):
        sdl = tmp_path / "app.yaml"
        sdl.write_text(VALID_SDL)
        rc = _run_cli(monkeypatch, ["just-akash", "validate-sdl", str(sdl)])
        assert rc is None  # success returns normally (no explicit exit)
        assert f"OK: {sdl}" in capsys.readouterr().out

    def test_missing_file_exits_one(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["just-akash", "validate-sdl", "/no/such.yaml"])
        from just_akash.cli import main

        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        assert "SDL file not found" in capsys.readouterr().err

    def test_invalid_sdl_exits_one(self, monkeypatch, tmp_path, capsys):
        """A signedBy.anyOf with a non-authority address silently disables the
        audit constraint on-chain — validate_sdl rejects it."""
        bad = VALID_SDL.replace(
            "akash1365yvmc4s7awdyj3n2sav7xfx76adc6dnmlx63", "akash1nottheauditauthority"
        )
        sdl = tmp_path / "bad.yaml"
        sdl.write_text(bad)
        with pytest.raises(SystemExit) as exc:
            _run_cli(monkeypatch, ["just-akash", "validate-sdl", str(sdl)])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "signedBy" in err or "audit" in err.lower()
