"""Tests for the v1.6 CLI subcommands: update, logs, events, add-funds, auto-topup."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from just_akash.cli import _enrich_deployment_with_provider


def _run_cli(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", args)
    from just_akash.cli import main

    return main()


# ── update ───────────────────────────────────────────────────────────


class TestCliUpdate:
    @patch("just_akash.deploy.update")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_update_success(self, MockAPI, mock_update, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        with pytest.raises(SystemExit) as e:
            _run_cli(
                monkeypatch,
                ["just-akash", "update", "--dseq", "12345", "--sdl", "x.yaml", "--image", "img:2"],
            )
        assert e.value.code == 0
        kwargs = mock_update.call_args.kwargs
        assert kwargs["dseq"] == "12345"
        assert kwargs["sdl_path"] == "x.yaml"
        assert kwargs["image"] == "img:2"

    @patch("just_akash.deploy.update", side_effect=RuntimeError("boom"))
    @patch("just_akash.api.AkashConsoleAPI")
    def test_update_failure_exits_1(self, MockAPI, mock_update, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        with pytest.raises(SystemExit) as e:
            _run_cli(monkeypatch, ["just-akash", "update", "--dseq", "12345", "--sdl", "x.yaml"])
        assert e.value.code == 1
        assert "boom" in capsys.readouterr().err

    def test_update_requires_sdl(self, monkeypatch):
        # --sdl is required; argparse exits 2 without it.
        with pytest.raises(SystemExit) as e:
            _run_cli(monkeypatch, ["just-akash", "update", "--dseq", "12345"])
        assert e.value.code == 2


# ── logs ─────────────────────────────────────────────────────────────


class TestCliLogs:
    @patch("just_akash.cli._make_lease_shell")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_logs_calls_stream_with_args(self, MockAPI, mock_make, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        transport = mock_make.return_value
        _run_cli(
            monkeypatch,
            ["just-akash", "logs", "--dseq", "12345", "-f", "--tail", "20", "--service", "web"],
        )
        transport.stream_logs.assert_called_once_with(follow=True, tail=20, service="web")

    @patch("just_akash.cli._make_lease_shell")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_logs_defaults(self, MockAPI, mock_make, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        transport = mock_make.return_value
        _run_cli(monkeypatch, ["just-akash", "logs", "--dseq", "12345"])
        transport.stream_logs.assert_called_once_with(follow=False, tail=100, service=None)

    @patch("just_akash.cli._make_lease_shell")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_logs_keyboardinterrupt_is_clean(self, MockAPI, mock_make, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        transport = mock_make.return_value
        transport.stream_logs.side_effect = KeyboardInterrupt()
        # Must not propagate KeyboardInterrupt.
        _run_cli(monkeypatch, ["just-akash", "logs", "--dseq", "12345"])

    @patch("just_akash.cli._make_lease_shell")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_logs_stream_runtimeerror_exits_1_not_traceback(
        self, MockAPI, mock_make, monkeypatch, capsys
    ):
        # The logs handler wraps stream_logs in an inner try that only catches
        # KeyboardInterrupt; a RuntimeError raised mid-stream (e.g. provider
        # resolution failing after the transport is built) must fall through to
        # the outer RuntimeError handler -> friendly message + exit 1, never an
        # uncaught traceback.
        monkeypatch.setenv("AKASH_API_KEY", "k")
        transport = mock_make.return_value
        transport.stream_logs.side_effect = RuntimeError("provider resolution failed mid-stream")
        with pytest.raises(SystemExit) as e:
            _run_cli(monkeypatch, ["just-akash", "logs", "--dseq", "12345"])
        assert e.value.code == 1
        assert "provider resolution failed mid-stream" in capsys.readouterr().err

    @patch("just_akash.cli._make_lease_shell")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_logs_negative_tail_exits_1(self, MockAPI, mock_make, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        with pytest.raises(SystemExit) as e:
            _run_cli(monkeypatch, ["just-akash", "logs", "--dseq", "12345", "--tail", "-5"])
        assert e.value.code == 1
        assert "--tail" in capsys.readouterr().err
        mock_make.return_value.stream_logs.assert_not_called()


# ── events ───────────────────────────────────────────────────────────


class TestCliEvents:
    @patch("just_akash.cli._make_lease_shell")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_events_calls_stream(self, MockAPI, mock_make, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        transport = mock_make.return_value
        _run_cli(monkeypatch, ["just-akash", "events", "--dseq", "12345"])
        transport.stream_events.assert_called_once_with()

    @patch("just_akash.cli._make_lease_shell")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_events_keyboardinterrupt_is_clean(self, MockAPI, mock_make, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        transport = mock_make.return_value
        transport.stream_events.side_effect = KeyboardInterrupt()
        _run_cli(monkeypatch, ["just-akash", "events", "--dseq", "12345"])


# ── add-funds ────────────────────────────────────────────────────────


class TestCliAddFunds:
    @patch("just_akash.api.AkashConsoleAPI")
    def test_below_minimum_exits_1(self, MockAPI, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        with pytest.raises(SystemExit) as e:
            _run_cli(
                monkeypatch,
                ["just-akash", "add-funds", "--dseq", "12345", "--deposit", "0.1"],
            )
        assert e.value.code == 1
        assert "minimum" in capsys.readouterr().err.lower()

    @patch("just_akash.api._get_tag", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_nan_deposit_does_not_bypass_minimum_guard(
        self, MockAPI, mock_tag, monkeypatch, capsys
    ):
        # argparse `type=float` accepts "nan" -> float('nan'). The minimum-deposit
        # guard is `if args.deposit < 0.5`, but `nan < 0.5` is False, so NaN slips
        # past the guard and is forwarded to deposit_deployment. From there it is
        # serialized as the bare token `NaN` in the request body
        # ({"deposit": NaN}), which is NOT valid JSON per RFC 8259 and a strict
        # Console API parser rejects. A NaN deposit is a nonsensical amount that
        # the guard exists to catch; it must be rejected (exit 1) and never reach
        # the API.
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        with pytest.raises(SystemExit) as e:
            _run_cli(
                monkeypatch,
                ["just-akash", "add-funds", "--dseq", "12345", "--deposit", "nan", "-y"],
            )
        assert e.value.code == 1
        client.deposit_deployment.assert_not_called()

    @patch("just_akash.api._get_tag", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_confirmed_deposits(self, MockAPI, mock_tag, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        _run_cli(
            monkeypatch,
            ["just-akash", "add-funds", "--dseq", "12345", "--deposit", "1.5", "-y"],
        )
        client.deposit_deployment.assert_called_once_with("12345", 1.5)
        assert "Added 1.5 USD" in capsys.readouterr().out

    @patch("just_akash.api._confirm", return_value=False)
    @patch("just_akash.api._get_tag", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_cancelled_does_not_deposit(
        self, MockAPI, mock_tag, mock_confirm, monkeypatch, capsys
    ):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        _run_cli(monkeypatch, ["just-akash", "add-funds", "--dseq", "12345", "--deposit", "1.0"])
        client.deposit_deployment.assert_not_called()
        assert "Cancelled" in capsys.readouterr().out


# ── auto-topup ───────────────────────────────────────────────────────


class TestCliAutoTopup:
    @patch("just_akash.api._get_tag", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_on(self, MockAPI, mock_tag, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        _run_cli(monkeypatch, ["just-akash", "auto-topup", "--dseq", "12345", "--on"])
        client.set_auto_top_up.assert_called_once_with("12345", True)
        assert "enabled" in capsys.readouterr().out

    @patch("just_akash.api._get_tag", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_off(self, MockAPI, mock_tag, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        _run_cli(monkeypatch, ["just-akash", "auto-topup", "--dseq", "12345", "--off"])
        client.set_auto_top_up.assert_called_once_with("12345", False)
        assert "disabled" in capsys.readouterr().out

    @patch("just_akash.api._get_tag", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_show_unset(self, MockAPI, mock_tag, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        client.get_deployment_settings.return_value = {}
        _run_cli(monkeypatch, ["just-akash", "auto-topup", "--dseq", "12345"])
        client.set_auto_top_up.assert_not_called()
        assert "not configured" in capsys.readouterr().out

    @patch("just_akash.api._get_tag", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_show_enabled_with_details(self, MockAPI, mock_tag, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        client.get_deployment_settings.return_value = {
            "autoTopUpEnabled": True,
            "topUpFrequencyMs": 86400000,
        }
        _run_cli(monkeypatch, ["just-akash", "auto-topup", "--dseq", "12345"])
        out = capsys.readouterr().out
        assert "auto top-up on" in out
        assert "topUpFrequencyMs" in out

    @patch("just_akash.api._get_tag", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_show_does_not_lie_when_enabled_is_string_false(
        self, MockAPI, mock_tag, monkeypatch, capsys
    ):
        # If the API returns autoTopUpEnabled as the *string* "false" (a real
        # JSON-vs-bool mismatch some servers emit), the display computes
        # bool("false") == True and prints "auto top-up on" — the opposite of
        # the truth. The shown state must reflect the disabled value.
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        client.get_deployment_settings.return_value = {"autoTopUpEnabled": "false"}
        _run_cli(monkeypatch, ["just-akash", "auto-topup", "--dseq", "12345"])
        out = capsys.readouterr().out
        assert "auto top-up off" in out
        assert "auto top-up on" not in out

    def test_on_and_off_mutually_exclusive(self, monkeypatch):
        with pytest.raises(SystemExit) as e:
            _run_cli(
                monkeypatch,
                ["just-akash", "auto-topup", "--dseq", "12345", "--on", "--off"],
            )
        assert e.value.code == 2

    @patch("just_akash.api._get_tag", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_set_path_api_error_exits_1_cleanly(self, MockAPI, mock_tag, monkeypatch, capsys):
        # The set-path of auto-topup (--on/--off) calls set_auto_top_up, which can
        # raise RuntimeError on a real server failure (e.g. the upsert's PATCH/POST
        # hits an escrow-service 500). That must surface as a friendly stderr
        # message + exit 1, never an uncaught traceback. The existing tests only
        # exercise the success path; this locks down the failure path.
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        client.set_auto_top_up.side_effect = RuntimeError(
            "API Error (500): escrow service unavailable"
        )
        with pytest.raises(SystemExit) as e:
            _run_cli(monkeypatch, ["just-akash", "auto-topup", "--dseq", "12345", "--on"])
        assert e.value.code == 1
        err = capsys.readouterr().err
        assert "escrow service unavailable" in err
        # The set call was actually attempted (we're testing the set-path, not show).
        client.set_auto_top_up.assert_called_once_with("12345", True)


def _dep_with_lease(provider):
    """Build a deployment dict with one lease (id.provider=akash1p)."""
    lease = {"id": {"provider": "akash1p"}, "provider": provider}
    return {"leases": [lease]}


class TestEnrichProviderHostUri:
    def test_backfills_blank_hosturi_from_registry(self):
        client = MagicMock()
        client.get_provider.return_value = {"hostUri": "https://p.example:8443"}
        out = _enrich_deployment_with_provider(client, _dep_with_lease({"hostUri": ""}))
        host = out["leases"][0]["provider"]["hostUri"]
        assert host == "https://p.example:8443"
        client.get_provider.assert_called_once_with("akash1p")

    def test_keeps_existing_nonblank_hosturi(self):
        client = MagicMock()
        out = _enrich_deployment_with_provider(
            client, _dep_with_lease({"hostUri": "https://keep"})
        )
        assert out["leases"][0]["provider"]["hostUri"] == "https://keep"
        client.get_provider.assert_not_called()

    def test_tolerates_non_list_leases(self):
        client = MagicMock()
        assert _enrich_deployment_with_provider(client, {"leases": None}) == {"leases": None}
        client.get_provider.assert_not_called()

    def test_skips_non_dict_lease_entries(self):
        client = MagicMock()
        client.get_provider.return_value = {"hostUri": "https://p"}
        dep = {"leases": ["weird", {"id": {"provider": "akash1p"}}]}
        out = _enrich_deployment_with_provider(client, dep)
        client.get_provider.assert_called_once_with("akash1p")
        assert out["leases"][1]["provider"]["hostUri"] == "https://p"
