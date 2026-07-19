"""End-to-end integration tests against a local fake Akash (HTTP + WebSocket).

These drive the REAL CLI through ``cli.main`` against the in-process
``FakeAkash`` (tests/_fake_akash.py): real urllib HTTP to a localhost Console
stub, real websockets lease-shell frames to a localhost provider-proxy stub, no
credentials, no network. The frame protocol, JWT fetch, API request shaping, and
CLI dispatch all run unmodified — see _fake_akash.py for the three TLS-bypass
seams (the only things patched).
"""

import sys
from unittest.mock import patch

import pytest


def _run_cli(args: list[str]):
    # Patch sys.argv (not bare assignment) so it is always restored, even when
    # cli.main raises SystemExit — a leak would make later tests order-dependent.
    with patch.object(sys, "argv", args):
        from just_akash.cli import main

        return main()


@pytest.fixture
def fake_akash(monkeypatch):
    import just_akash.transport as transport
    import just_akash.transport.lease_shell as lease_shell
    from tests._fake_akash import FakeAkash

    fa = FakeAkash()

    # Seam 1 — the transport's internal API client defaults to the REAL Console
    # URL (hardcoded in TransportConfig.console_url, not env-overridable). Inject
    # the stub URL when the CLI builds a transport.
    real_make = transport.make_transport

    def _make(name, **kw):
        kw.setdefault("console_url", fa.console_url)
        return real_make(name, **kw)

    monkeypatch.setattr(transport, "make_transport", _make)

    # Seam 2 — redirect the lease-shell socket to the local ws:// stub (the real
    # _get_proxy_ws_url rejects plaintext by design).
    monkeypatch.setattr(
        lease_shell.LeaseShellTransport, "_get_proxy_ws_url", lambda self: fa.proxy_url
    )

    # Seam 3 — the production connect() always passes ssl=, which ws:// rejects.
    real_connect = lease_shell.connect

    def _connect(url, **kw):
        if str(url).startswith("ws://"):
            kw.pop("ssl", None)
        return real_connect(url, **kw)

    monkeypatch.setattr(lease_shell, "connect", _connect)

    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.setenv("AKASH_CONSOLE_URL", fa.console_url)
    try:
        yield fa
    finally:
        fa.stop()


class TestExecEndToEnd:
    def test_echo_round_trip(self, fake_akash, capsys):
        with pytest.raises(SystemExit) as exc:
            _run_cli(["just-akash", "exec", "--dseq", "1234", "echo hello world"])
        assert exc.value.code == 0
        assert "hello world" in capsys.readouterr().out

    def test_propagates_remote_exit_code(self, fake_akash):
        with pytest.raises(SystemExit) as exc:
            _run_cli(["just-akash", "exec", "--dseq", "1234", "false"])
        assert exc.value.code == 1

    def test_failure_frame_exits_one(self, fake_akash, capsys):
        fake_akash.shell.failure_message = "container out of memory"
        with pytest.raises(SystemExit) as exc:
            _run_cli(["just-akash", "exec", "--dseq", "1234", "echo hi"])
        assert exc.value.code == 1

    def test_closed_without_result_now_errors_not_exit_zero(self, fake_akash):
        """Regression for the silent false-success fix (ed7a26a): a proxy that
        closes cleanly WITHOUT a result frame must surface exit 1, not exit 0."""
        fake_akash.shell.close_without_result = True
        with pytest.raises(SystemExit) as exc:
            _run_cli(["just-akash", "exec", "--dseq", "1234", "echo hi"])
        assert exc.value.code == 1


class TestInjectEndToEnd:
    def test_inject_then_cat_round_trip(self, fake_akash, capsys):
        """The lease-shell secrets round-trip that regressed silently in v1.29.0
        (the head -c path wrote EMPTY files): inject writes the secret over the
        proxy, and a subsequent ``cat`` reads it back through the real transport."""
        rc = _run_cli(["just-akash", "inject", "--dseq", "1234", "--env", "SECRET=s3cret"])
        assert rc is None  # success path does not sys.exit
        assert "Injected 1 secret" in capsys.readouterr().out

        with pytest.raises(SystemExit) as exc:
            _run_cli(["just-akash", "exec", "--dseq", "1234", "cat /run/secrets/.env"])
        assert exc.value.code == 0
        assert "s3cret" in capsys.readouterr().out
