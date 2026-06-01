"""Tests for just_akash.cli — command dispatch logic."""

import os
import sys
from unittest.mock import patch

import pytest


def _run_cli(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", args)
    from just_akash.cli import main

    return main()


class TestCliNoCommand:
    def test_no_command_prints_help(self, monkeypatch):
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(monkeypatch, ["just-akash"])
        assert exc_info.value.code == 0


class TestCliDeploy:
    @patch("just_akash.deploy.deploy")
    def test_deploy_success(self, mock_deploy, monkeypatch):
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(monkeypatch, ["just-akash", "deploy", "--sdl", "test.yaml"])
        assert exc_info.value.code == 0
        mock_deploy.assert_called_once()

    @patch("just_akash.deploy.deploy", side_effect=RuntimeError("deploy failed"))
    def test_deploy_failure(self, mock_deploy, monkeypatch):
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(monkeypatch, ["just-akash", "deploy", "--sdl", "test.yaml"])
        assert exc_info.value.code == 1


class TestCliListNoApiKey:
    def test_list_no_key(self, monkeypatch, capsys):
        monkeypatch.delenv("AKASH_API_KEY", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(monkeypatch, ["just-akash", "list"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "AKASH_API_KEY not set" in captured.err


class TestCliList:
    @patch("just_akash.api.format_deployments_table", return_value="table output")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_list_tty(self, MockAPI, mock_fmt, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.list_deployments.return_value = []
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        _run_cli(monkeypatch, ["just-akash", "list"])
        captured = capsys.readouterr()
        assert "table output" in captured.out

    @patch("just_akash.api.AkashConsoleAPI")
    def test_list_json(self, MockAPI, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.list_deployments.return_value = []
        _run_cli(monkeypatch, ["just-akash", "list"])
        captured = capsys.readouterr()
        assert "[]" in captured.out


class TestCliStatusWithDseq:
    @patch("just_akash.api._extract_ssh_info", return_value=None)
    @patch("just_akash.api._extract_lease_provider", return_value="akash1prov")
    @patch("just_akash.api._get_tag", return_value="my-job")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_status_with_dseq(
        self, MockAPI, mock_tag, mock_lease_prov, mock_ssh, monkeypatch, capsys
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "deployment": {"state": "active"},
            "leases": [],
        }
        _run_cli(monkeypatch, ["just-akash", "status", "--dseq", "12345"])
        captured = capsys.readouterr()
        assert "active" in captured.out


class TestCliStatusForwardedEndpoints:
    @patch("just_akash.api._get_tag", return_value=None)
    @patch("just_akash.api._extract_lease_provider", return_value="akash1prov")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_status_json_includes_lcd_endpoint(
        self, MockAPI, mock_lease_prov, mock_tag, monkeypatch, capsys
    ):
        """status --json must surface forwarded non-SSH ports (e.g. LCD :1317).

        Regression for the akash-node-lcd recipe: it parses status JSON for the
        forwarded LCD endpoint, which previously was never emitted.
        """
        import json

        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "deployment": {"state": "active"},
            "leases": [
                {
                    "status": {
                        "forwarded_ports": {
                            "node": [
                                {"port": 1317, "host": "5.6.7.8", "externalPort": 31317},
                                {"port": 26657, "host": "5.6.7.8", "externalPort": 32657},
                            ]
                        }
                    }
                }
            ],
        }
        _run_cli(monkeypatch, ["just-akash", "status", "--json", "--dseq", "12345"])
        captured = capsys.readouterr()
        result = json.loads(captured.out)
        endpoints = result["endpoints"]
        lcd = next(e for e in endpoints if e["internal_port"] == 1317)
        assert lcd["host"] == "5.6.7.8"
        assert lcd["port"] == 31317


class TestCliStatusNoDeployments:
    @patch("just_akash.api._resolve_dseq", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_status_no_deployments(self, MockAPI, mock_resolve, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.list_deployments.return_value = []
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(monkeypatch, ["just-akash", "status"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No active deployments" in captured.out


class TestCliDestroyNoDeployments:
    @patch("just_akash.api._resolve_dseq", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_destroy_no_deployments(self, MockAPI, mock_resolve, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.list_deployments.return_value = []
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(monkeypatch, ["just-akash", "destroy"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No active deployments" in captured.out


class TestCliDestroyAllNoDeployments:
    @patch("just_akash.api.AkashConsoleAPI")
    def test_destroy_all_no_deployments(self, MockAPI, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.list_deployments.return_value = []
        _run_cli(monkeypatch, ["just-akash", "destroy-all"])
        captured = capsys.readouterr()
        assert "No deployments to destroy" in captured.out


class TestCliTag:
    @patch("just_akash.api._save_tags")
    @patch("just_akash.api._load_tags", return_value={})
    def test_tag_success(self, mock_load, mock_save, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        _run_cli(monkeypatch, ["just-akash", "tag", "--dseq", "12345", "--name", "my-job"])
        captured = capsys.readouterr()
        assert "Tagged 12345" in captured.out
        mock_save.assert_called_once()

    def test_tag_missing_args(self, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(monkeypatch, ["just-akash", "tag"])
        assert exc_info.value.code == 2


class TestCliRuntimeError:
    @patch("just_akash.api.AkashConsoleAPI")
    def test_list_runtime_error(self, MockAPI, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.list_deployments.side_effect = RuntimeError("API error")
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(monkeypatch, ["just-akash", "list"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "API error" in captured.err


class TestCliTest:
    @patch("just_akash.test_lifecycle.main")
    def test_test_command(self, mock_test_main, monkeypatch):
        _run_cli(monkeypatch, ["just-akash", "test"])
        mock_test_main.assert_called_once()


class TestCliStatusWithSsh:
    @patch("just_akash.api._extract_ssh_info")
    @patch("just_akash.api._extract_lease_provider", return_value="akash1prov")
    @patch("just_akash.api._get_tag", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_status_with_ssh(
        self, MockAPI, mock_tag, mock_lease_prov, mock_ssh, monkeypatch, capsys
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "deployment": {"state": "active"},
            "leases": [],
        }
        mock_ssh.return_value = {"host": "1.2.3.4", "port": 22222}
        _run_cli(monkeypatch, ["just-akash", "status", "--dseq", "12345"])
        captured = capsys.readouterr()
        assert "ssh -p 22222" in captured.out


class TestCliDestroyWithConfirm:
    @patch("just_akash.api._save_tags")
    @patch("just_akash.api._load_tags", return_value={})
    @patch("just_akash.api._resolve_dseq", return_value="12345")
    @patch("just_akash.api._get_tag", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_destroy_confirmed(
        self, MockAPI, mock_tag, mock_resolve, mock_load, mock_save, monkeypatch, capsys
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr("builtins.input", lambda _: "y")
        _run_cli(monkeypatch, ["just-akash", "destroy", "--dseq", "12345"])
        captured = capsys.readouterr()
        assert "destroyed" in captured.out

    @patch("just_akash.api._resolve_dseq", return_value="12345")
    @patch("just_akash.api._get_tag", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_destroy_cancelled(self, MockAPI, mock_tag, mock_resolve, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr("builtins.input", lambda _: "n")
        _run_cli(monkeypatch, ["just-akash", "destroy", "--dseq", "12345"])
        captured = capsys.readouterr()
        assert "Cancelled" in captured.out


class TestCliDestroyAllWithConfirm:
    @patch("just_akash.api._extract_dseq", side_effect=["1", "2"])
    @patch("just_akash.api._save_tags")
    @patch("just_akash.api._load_tags", return_value={"1": "a", "2": "b"})
    @patch("just_akash.api.format_deployments_table", return_value="table")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_destroy_all_confirmed(
        self, MockAPI, mock_fmt, mock_load, mock_save, mock_dseq, monkeypatch, capsys
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.list_deployments.return_value = [{"dseq": "1"}, {"dseq": "2"}]
        monkeypatch.setattr("builtins.input", lambda _: "y")
        _run_cli(monkeypatch, ["just-akash", "destroy-all"])
        captured = capsys.readouterr()
        assert "All deployments destroyed" in captured.out

    @patch("just_akash.api.format_deployments_table", return_value="table")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_destroy_all_cancelled(self, MockAPI, mock_fmt, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.list_deployments.return_value = [{"dseq": "1"}]
        monkeypatch.setattr("builtins.input", lambda _: "n")
        _run_cli(monkeypatch, ["just-akash", "destroy-all"])
        captured = capsys.readouterr()
        assert "Cancelled" in captured.out


class TestCliDeployPassesArgs:
    @patch("just_akash.deploy.deploy")
    def test_deploy_passes_all_args(self, mock_deploy, monkeypatch):
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                monkeypatch,
                [
                    "just-akash",
                    "deploy",
                    "--sdl",
                    "my.yaml",
                    "--gpu",
                    "--image",
                    "ubuntu:22.04",
                    "--bid-wait",
                    "30",
                    "--bid-wait-retry",
                    "60",
                ],
            )
        assert exc_info.value.code == 0
        mock_deploy.assert_called_once_with(
            sdl_path="my.yaml",
            gpu=True,
            image="ubuntu:22.04",
            bid_wait=30,
            bid_wait_retry=60,
            env_vars=[],
            preferred_providers=None,
            backup_providers=None,
        )

    @patch("just_akash.deploy.deploy")
    def test_deploy_passes_provider_tier_args(self, mock_deploy, monkeypatch):
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                monkeypatch,
                [
                    "just-akash",
                    "deploy",
                    "--sdl",
                    "my.yaml",
                    "--provider",
                    "akash1pref1",
                    "--provider",
                    "akash1pref2",
                    "--backup-provider",
                    "akash1back1",
                ],
            )
        assert exc_info.value.code == 0
        kwargs = mock_deploy.call_args.kwargs
        assert kwargs["preferred_providers"] == ["akash1pref1", "akash1pref2"]
        assert kwargs["backup_providers"] == ["akash1back1"]


class TestCliConnect:
    @patch("just_akash.api._find_ssh_key", return_value="/fake/key")
    @patch("just_akash.api._extract_ssh_info", return_value={"host": "1.2.3.4", "port": 22})
    @patch("just_akash.api.AkashConsoleAPI")
    def test_connect_ssh(self, MockAPI, mock_ssh, mock_find_key, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "deployment": {"state": "active"},
            "leases": [],
        }
        with patch("os.execvp") as mock_exec:
            _run_cli(
                monkeypatch,
                ["just-akash", "connect", "--dseq", "12345"],
            )
            mock_exec.assert_called_once()

    @patch("just_akash.api._resolve_dseq", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_connect_no_deployments(self, MockAPI, mock_resolve, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.list_deployments.return_value = []
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(monkeypatch, ["just-akash", "connect"])
        assert exc_info.value.code == 1

    @patch("just_akash.api._extract_ssh_info", return_value=None)
    @patch("just_akash.api.AkashConsoleAPI")
    def test_connect_no_ssh_shows_warning(self, MockAPI, mock_ssh, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "deployment": {"state": "active"},
            "leases": [],
        }
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(monkeypatch, ["just-akash", "connect", "--dseq", "12345"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "SSH-enabled SDL" in captured.err
        assert "lease-shell" in captured.err

    @patch("just_akash.api._extract_ssh_info", return_value={"host": "1.2.3.4", "port": 22})
    @patch("just_akash.api.AkashConsoleAPI")
    def test_connect_no_key_found(self, MockAPI, mock_ssh, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "deployment": {"state": "active"},
            "leases": [],
        }
        monkeypatch.setattr(os.path, "exists", lambda _: False)
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(monkeypatch, ["just-akash", "connect", "--dseq", "12345"])
        assert exc_info.value.code == 1


class TestCliExecNoSsh:
    @patch("just_akash.api._extract_ssh_info", return_value=None)
    @patch("just_akash.api.AkashConsoleAPI")
    def test_exec_no_ssh_shows_warning(self, MockAPI, mock_ssh, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "deployment": {"state": "active"},
            "leases": [],
        }
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(monkeypatch, ["just-akash", "exec", "--dseq", "12345", "echo hello"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "SSH-enabled SDL" in captured.err
        assert "lease-shell" in captured.err


class TestCliInjectNoSsh:
    @patch("just_akash.api._extract_ssh_info", return_value=None)
    @patch("just_akash.api.AkashConsoleAPI")
    def test_inject_no_ssh_shows_warning(self, MockAPI, mock_ssh, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "deployment": {"state": "active"},
            "leases": [],
        }
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                monkeypatch,
                ["just-akash", "inject", "--dseq", "12345", "--env", "SECRET=value"],
            )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "SSH-enabled SDL" in captured.err
        assert "lease-shell" in captured.err
