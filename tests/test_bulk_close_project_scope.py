"""A bulk close never closes a deployment outside the caller's Console project.

An API key not bound to a project reaches EVERY project of its team (Console teams and
projects). After the 2026-11-19 console-2 sweep, Flynn's prod dseqs live in the
`flynn-production` project, but the shared key is still unbound, so the scope has to hold
in code: every listed deployment carries its `projectId`, and a bulk close refuses any
whose `projectId` is not the caller's configured project, or is missing.
"""

from __future__ import annotations

import pytest

from just_akash import bulk_close
from just_akash.api import AkashConsoleAPI

RUN = "a1b2c3d4"
OURS = "11111111-1111-4111-8111-111111111111"
FLYNN = "22222222-2222-4222-8222-222222222222"


def _dep(dseq: str, name: str, project: str | None) -> dict:
    row: dict = {
        "deployment": {"id": {"owner": "akash1x", "dseq": dseq}, "state": "active"},
        "groups": [{"group_spec": {"name": name}}],
    }
    if project is not None:
        row["projectId"] = project
    return row


ROWS = [
    _dep("1", f"just-akash-e2e.{RUN}", OURS),  # ours, our project
    _dep("2", f"just-akash-e2e.{RUN}", FLYNN),  # run-tagged, but another project
    _dep("3", "web", FLYNN),  # another project, not prod-named
    _dep("4", f"just-akash-e2e.{RUN}", None),  # project unknown
]


class _Fake(AkashConsoleAPI):
    def __init__(self, rows):
        self._rows = rows
        self.closed: list[str] = []

    def list_deployments(self, active_only: bool = True, max_pages=None):
        return list(self._rows)

    def close_deployment(self, dseq: str):
        self.closed.append(dseq)
        return {"dseq": dseq}


def _plan(result):
    return {p.dseq: (p.action, p.reason) for p in result["plan"]}


def test_only_dseqs_in_the_callers_project_are_closed():
    c = _Fake(ROWS)
    c.close_all_deployments(run_tag=RUN, project_id=OURS, execute=True)
    assert c.closed == ["1"]


def test_an_allowlisted_dseq_in_another_project_is_refused():
    c = _Fake(ROWS)
    result = c.close_all_deployments(allow_dseqs=["3"], project_id=OURS, execute=True)
    assert c.closed == []
    action, reason = _plan(result)["3"]
    assert action == "refuse"
    assert FLYNN in reason


def test_a_dseq_whose_project_is_unknown_is_refused():
    c = _Fake(ROWS)
    result = c.close_all_deployments(allow_dseqs=["4"], project_id=OURS, execute=True)
    assert c.closed == []
    assert _plan(result)["4"][0] == "refuse"


def test_an_actual_close_requires_a_project(monkeypatch):
    monkeypatch.delenv(bulk_close.PROJECT_ENV, raising=False)
    c = _Fake(ROWS)
    with pytest.raises(ValueError, match="project"):
        c.close_all_deployments(run_tag=RUN, execute=True)
    assert c.closed == []


def test_a_dry_run_without_a_project_still_plans(monkeypatch):
    monkeypatch.delenv(bulk_close.PROJECT_ENV, raising=False)
    result = _Fake(ROWS).close_all_deployments(run_tag=RUN)
    assert result["dry_run"] is True
    assert _plan(result)["1"][0] == "close"


def test_the_project_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv(bulk_close.PROJECT_ENV, OURS)
    c = _Fake(ROWS)
    c.close_all_deployments(run_tag=RUN, execute=True)
    assert c.closed == ["1"]


def test_the_cli_refuses_yes_without_a_project(monkeypatch, capsys, tmp_path):
    import sys

    import just_akash.api as api_mod
    from just_akash import cli

    monkeypatch.delenv(bulk_close.PROJECT_ENV, raising=False)
    monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    c = _Fake(ROWS)
    monkeypatch.setattr(api_mod, "AkashConsoleAPI", lambda key: c)
    monkeypatch.setattr(sys, "argv", ["just-akash", "destroy-all", "--run-tag", RUN, "--yes"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2
    assert c.closed == []
    assert "--project-id" in capsys.readouterr().out


def test_the_cli_closes_within_the_named_project(monkeypatch, tmp_path):
    import sys

    import just_akash.api as api_mod
    from just_akash import cli

    monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    c = _Fake(ROWS)
    monkeypatch.setattr(api_mod, "AkashConsoleAPI", lambda key: c)
    argv = ["just-akash", "destroy-all", "--run-tag", RUN, "--project-id", OURS, "--yes"]
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()
    assert c.closed == ["1"]


def test_the_cli_holds_the_project_line_whatever_client_it_is_handed(monkeypatch, capsys):
    """A client that would not refuse (a mock, a subclass) is never asked to close."""
    import sys
    from unittest.mock import MagicMock

    import just_akash.api as api_mod
    from just_akash import cli

    monkeypatch.delenv(bulk_close.PROJECT_ENV, raising=False)
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    client = MagicMock()
    monkeypatch.setattr(api_mod, "AkashConsoleAPI", lambda key: client)
    monkeypatch.setattr(sys, "argv", ["just-akash", "destroy-all", "--allow-dseq", "3", "--yes"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2
    client.close_all_deployments.assert_not_called()
