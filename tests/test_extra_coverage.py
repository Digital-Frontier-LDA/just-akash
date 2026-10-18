"""Tests for remaining uncovered paths — api_main, deploy_main."""

import os
import sys
from unittest.mock import patch

import pytest


class TestDeployMain:
    @patch("just_akash.deploy.deploy")
    def test_deploy_main_success(self, mock_deploy, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["deploy", "--sdl", "test.yaml"])
        monkeypatch.delenv("AKASH_DEBUG", raising=False)
        from just_akash.deploy import deploy_main

        with pytest.raises(SystemExit) as exc_info:
            deploy_main()
        assert exc_info.value.code == 0

    @patch("just_akash.deploy.deploy", side_effect=RuntimeError("fail"))
    def test_deploy_main_failure(self, mock_deploy, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["deploy", "--sdl", "test.yaml"])
        monkeypatch.delenv("AKASH_DEBUG", raising=False)
        from just_akash.deploy import deploy_main

        with pytest.raises(SystemExit) as exc_info:
            deploy_main()
        assert exc_info.value.code == 1


class TestDeployMainCharter:
    """Review (fallback, a1eab31): `python -m just_akash.deploy` must honour a charter like
    `just-akash deploy` does, or enforce refuses with advice this entry point cannot follow."""

    @patch("just_akash.deploy.deploy")
    def test_charter_file_is_loaded_and_passed(self, mock_deploy, monkeypatch, tmp_path):
        import json

        from tests import _charter_harness as harness

        path = tmp_path / "c.json"
        path.write_text(json.dumps(harness.charter_doc()))
        monkeypatch.setattr(sys, "argv", ["deploy", "--sdl", "t.yaml", "--charter", str(path)])
        from just_akash.deploy import deploy_main

        with pytest.raises(SystemExit) as exc_info:
            deploy_main()
        assert exc_info.value.code == 0
        charter = mock_deploy.call_args.kwargs["charter"]
        assert charter is not None and charter.bead == "openmix-pvr6.1"

    @patch("just_akash.deploy.deploy")
    def test_charter_env_is_honoured(self, mock_deploy, monkeypatch):
        import json

        from tests import _charter_harness as harness

        monkeypatch.setenv("JUST_AKASH_CHARTER_JSON", json.dumps(harness.charter_doc()))
        monkeypatch.setattr(sys, "argv", ["deploy", "--sdl", "t.yaml"])
        from just_akash.deploy import deploy_main

        with pytest.raises(SystemExit):
            deploy_main()
        assert mock_deploy.call_args.kwargs["charter"].bead == "openmix-pvr6.1"

    @patch("just_akash.deploy.deploy")
    def test_an_invalid_charter_exits_1_before_deploy(self, mock_deploy, monkeypatch, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("{}")
        monkeypatch.setattr(sys, "argv", ["deploy", "--sdl", "t.yaml", "--charter", str(path)])
        from just_akash.deploy import deploy_main

        with pytest.raises(SystemExit) as exc_info:
            deploy_main()
        assert exc_info.value.code == 1
        mock_deploy.assert_not_called()


class TestApiMain:
    def test_no_api_key(self, monkeypatch):
        monkeypatch.delenv("AKASH_API_KEY", raising=False)
        monkeypatch.setattr(sys, "argv", ["api", "list"])
        from just_akash.api import api_main

        with pytest.raises(SystemExit):
            api_main()

    @patch("just_akash.api.format_deployments_table", return_value="table")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_list(self, MockAPI, mock_fmt, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "list"])
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        client = MockAPI.return_value
        client.list_deployments.return_value = []
        from just_akash.api import api_main

        api_main()
        captured = capsys.readouterr()
        assert "table" in captured.out

    @patch("just_akash.api._extract_ssh_info", return_value=None)
    @patch("just_akash.api._extract_lease_provider", return_value=None)
    @patch("just_akash.api.AkashConsoleAPI")
    def test_status_with_dseq_non_tty(self, MockAPI, mock_lease, mock_ssh, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "deployment": {"state": "active"},
            "leases": [],
        }
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        from just_akash.api import api_main

        api_main()
        captured = capsys.readouterr()
        assert '"status": "ready"' in captured.out

    @patch("just_akash.api._extract_ssh_info", return_value=None)
    @patch("just_akash.api._extract_lease_provider", return_value=None)
    @patch("just_akash.api.AkashConsoleAPI")
    def test_status_closed_non_tty(self, MockAPI, mock_lease, mock_ssh, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "deployment": {"state": "closed"},
            "leases": [],
        }
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        from just_akash.api import api_main

        api_main()
        captured = capsys.readouterr()
        assert '"status": "down"' in captured.out

    @patch("just_akash.api._extract_ssh_info", return_value=None)
    @patch("just_akash.api._extract_lease_provider", return_value=None)
    @patch("just_akash.api.AkashConsoleAPI")
    def test_status_unknown_state_non_tty(
        self, MockAPI, mock_lease, mock_ssh, monkeypatch, capsys
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "deployment": {"state": "pending"},
            "leases": [],
        }
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        from just_akash.api import api_main

        api_main()
        captured = capsys.readouterr()
        assert '"status": "unknown"' in captured.out

    @patch("just_akash.api._extract_ssh_info", return_value={"host": "1.2.3.4", "port": 22})
    @patch("just_akash.api._extract_lease_provider", return_value=None)
    @patch("just_akash.api.AkashConsoleAPI")
    def test_status_with_ssh_non_tty(self, MockAPI, mock_lease, mock_ssh, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "deployment": {"state": "active"},
            "leases": [],
        }
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        from just_akash.api import api_main

        api_main()
        captured = capsys.readouterr()
        assert "endpoint" in captured.out

    @patch("just_akash.api._extract_ssh_info", return_value=None)
    @patch("just_akash.api._extract_lease_provider", return_value=None)
    @patch("just_akash.api.AkashConsoleAPI")
    def test_status_no_deployments_non_tty(
        self, MockAPI, mock_lease, mock_ssh, monkeypatch, capsys
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status"])
        client = MockAPI.return_value
        client.list_deployments.return_value = []
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        from just_akash.api import api_main

        with pytest.raises(SystemExit):
            api_main()
        captured = capsys.readouterr()
        assert '"status": "down"' in captured.out

    @patch("just_akash.api._extract_ssh_info", return_value=None)
    @patch("just_akash.api._extract_lease_provider", return_value=None)
    @patch("just_akash.api.AkashConsoleAPI")
    def test_status_no_deployments_tty(self, MockAPI, mock_lease, mock_ssh, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status"])
        client = MockAPI.return_value
        client.list_deployments.return_value = []
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        from just_akash.api import api_main

        with pytest.raises(SystemExit):
            api_main()
        captured = capsys.readouterr()
        assert "No active deployments" in captured.out

    @patch("just_akash.api._extract_dseq", return_value="12345")
    @patch("just_akash.api._extract_ssh_info", return_value=None)
    @patch("just_akash.api._extract_lease_provider", return_value=None)
    @patch("just_akash.api.AkashConsoleAPI")
    def test_status_single_dep_auto_select_tty(
        self, MockAPI, mock_lease, mock_ssh, mock_dseq, monkeypatch, capsys
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status"])
        client = MockAPI.return_value
        client.list_deployments.return_value = [{"dseq": "12345"}]
        client.get_deployment.return_value = {
            "deployment": {"state": "active"},
            "leases": [],
        }
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        from just_akash.api import api_main

        api_main()
        captured = capsys.readouterr()
        assert "Auto-selected" in captured.out

    @patch("just_akash.api._extract_ssh_info", return_value={"host": "1.2.3.4", "port": 22})
    @patch("just_akash.api._extract_lease_provider", return_value="akash1prov")
    @patch("just_akash.api._get_tag", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_status_tty_with_ssh_and_ports(
        self, MockAPI, mock_tag, mock_lease, mock_ssh, monkeypatch, capsys
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "status", "--dseq", "12345"])
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "deployment": {"state": "active"},
            "leases": [
                {
                    "status": {
                        "forwarded_ports": {
                            "web": [{"port": 80, "host": "1.2.3.4", "externalPort": 8080}]
                        },
                        "services": {"web": {"ready_replicas": 1, "total": 1}},
                    }
                }
            ],
            "escrow_account": {"state": {"funds": [{"amount": "5000", "denom": "uakt"}]}},
        }
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        from just_akash.api import api_main

        api_main()
        captured = capsys.readouterr()
        assert "ssh -p" in captured.out
        assert "Port:" in captured.out
        assert "Service:" in captured.out
        assert "Escrow:" in captured.out

    @patch("just_akash.api._extract_ssh_info", return_value=None)
    @patch("just_akash.api._extract_lease_provider", return_value=None)
    @patch("just_akash.api._resolve_dseq", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_connect_no_deployments(
        self, MockAPI, mock_resolve, mock_lease, mock_ssh, monkeypatch, capsys
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "connect"])
        client = MockAPI.return_value
        client.list_deployments.return_value = []
        from just_akash.api import api_main

        with pytest.raises(SystemExit) as exc_info:
            api_main()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No active deployments" in captured.out

    @patch("just_akash.api._extract_ssh_info", return_value=None)
    @patch("just_akash.api._extract_lease_provider", return_value=None)
    @patch("just_akash.api.AkashConsoleAPI")
    def test_connect_no_ssh(self, MockAPI, mock_lease, mock_ssh, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "connect", "--dseq", "12345"])
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "deployment": {"state": "active"},
            "leases": [],
        }
        from just_akash.api import api_main

        with pytest.raises(SystemExit) as exc_info:
            api_main()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No SSH port" in captured.out

    @patch("just_akash.api._extract_ssh_info", return_value={"host": "1.2.3.4", "port": 22})
    @patch("just_akash.api._extract_lease_provider", return_value=None)
    @patch("just_akash.api.AkashConsoleAPI")
    def test_connect_no_key(self, MockAPI, mock_lease, mock_ssh, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "connect", "--dseq", "12345"])
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "deployment": {"state": "active"},
            "leases": [],
        }
        monkeypatch.setattr(os.path, "exists", lambda _: False)
        from just_akash.api import api_main

        with pytest.raises(SystemExit) as exc_info:
            api_main()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "No SSH key found" in captured.out

    @patch("just_akash.api._find_ssh_key", return_value="/fake/key")
    @patch("just_akash.api._extract_ssh_info", return_value={"host": "1.2.3.4", "port": 22})
    @patch("just_akash.api._extract_lease_provider", return_value=None)
    @patch("just_akash.api.AkashConsoleAPI")
    def test_connect_with_key_found(
        self, MockAPI, mock_lease, mock_ssh, mock_find_key, monkeypatch
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(
            sys, "argv", ["api", "connect", "--dseq", "12345", "--key", "/fake/key"]
        )
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "deployment": {"state": "active"},
            "leases": [],
        }
        from just_akash.api import api_main

        with patch("os.execvp") as mock_exec:
            api_main()
            mock_exec.assert_called_once()
            args = mock_exec.call_args[0]
            assert args[0] == "ssh"

    @patch("just_akash.api._extract_dseq", return_value="12345")
    @patch("just_akash.api._resolve_dseq", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_close_single_dep_auto_select(
        self, MockAPI, mock_resolve, mock_dseq, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "close"])
        client = MockAPI.return_value
        client.list_deployments.return_value = [{"dseq": "12345"}]
        client.close_deployment.return_value = {"data": {"closed": True}}
        monkeypatch.setattr("builtins.input", lambda _: "y")
        import just_akash.api as api_mod
        from just_akash.api import api_main

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")
        api_main()
        captured = capsys.readouterr()
        assert "closed" in captured.out

    @patch("just_akash.api._resolve_dseq", return_value="")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_close_no_deployments(self, MockAPI, mock_resolve, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "close"])
        client = MockAPI.return_value
        client.list_deployments.return_value = []
        from just_akash.api import api_main

        with pytest.raises(SystemExit) as exc_info:
            api_main()
        assert exc_info.value.code == 0

    @patch("just_akash.api.format_deployments_table", return_value="table")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_close_all_confirmed(self, MockAPI, mock_fmt, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "close-all"])
        client = MockAPI.return_value
        client.list_deployments.return_value = [{"dseq": "1"}]
        client.close_all_deployments.return_value = {"closed": [True]}
        monkeypatch.setattr("builtins.input", lambda _: "y")
        import just_akash.api as api_mod
        from just_akash.api import api_main

        monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")
        api_main()
        captured = capsys.readouterr()
        assert "All deployments closed" in captured.out

    @patch("just_akash.api.format_deployments_table", return_value="table")
    @patch("just_akash.api.AkashConsoleAPI")
    def test_close_all_cancelled(self, MockAPI, mock_fmt, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "close-all"])
        client = MockAPI.return_value
        client.list_deployments.return_value = [{"dseq": "1"}]
        monkeypatch.setattr("builtins.input", lambda _: "n")
        from just_akash.api import api_main

        api_main()
        captured = capsys.readouterr()
        assert "Cancelled" in captured.out

    @patch("just_akash.api.AkashConsoleAPI")
    def test_close_all_no_deps(self, MockAPI, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "close-all"])
        client = MockAPI.return_value
        client.list_deployments.return_value = []
        from just_akash.api import api_main

        api_main()
        captured = capsys.readouterr()
        assert "No deployments to close" in captured.out

    @patch("just_akash.api.AkashConsoleAPI")
    def test_no_command(self, MockAPI, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api"])
        from just_akash.api import api_main

        with pytest.raises(SystemExit) as exc_info:
            api_main()
        assert exc_info.value.code == 0

    @patch("just_akash.api._save_tags")
    @patch("just_akash.api._load_tags", return_value={})
    @patch("just_akash.api.AkashConsoleAPI")
    def test_tag(self, MockAPI, mock_load, mock_save, monkeypatch, capsys):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "tag", "--dseq", "12345", "--name", "test"])
        from just_akash.api import api_main

        api_main()
        captured = capsys.readouterr()
        assert "Tagged" in captured.out

    @patch("just_akash.api.AkashConsoleAPI")
    def test_unknown_command(self, MockAPI, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "unknown-cmd"])
        from just_akash.api import api_main

        with pytest.raises(SystemExit):
            api_main()

    @patch("just_akash.api.AkashConsoleAPI")
    def test_runtime_error(self, MockAPI, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "test-key")
        monkeypatch.setattr(sys, "argv", ["api", "list"])
        client = MockAPI.return_value
        client.list_deployments.side_effect = RuntimeError("fail")
        from just_akash.api import api_main

        with pytest.raises(SystemExit) as exc_info:
            api_main()
        assert exc_info.value.code == 1
