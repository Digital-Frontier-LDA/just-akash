"""A bulk close may only close what this run created, or an explicit allowlist (just-akash-73o).

2026-11-19 13:53-13:54Z: one client closed EVERY deployment on the shared console-2 account
in ascending dseq order, including Flynn's 4 prod leases. That is the shape of
`destroy-all` -> `close_all_deployments()`: list once, DELETE each, no scope. Fleet rule
(GA, 2026-11-19): no agent or agent-launched job runs an unscoped bulk close.

Every test here uses a fake Console client; no endpoint is reached.
"""

from __future__ import annotations

import pytest

from just_akash import bulk_close
from just_akash.api import AkashConsoleAPI

RUN = "a1b2c3d4"
PROJECT = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def _callers_project(monkeypatch):
    """Every row below is filed in the caller's project (see test_bulk_close_project_scope)."""
    monkeypatch.setenv(bulk_close.PROJECT_ENV, PROJECT)


def _dep(dseq: str, *group_names: str) -> dict:
    return {
        "deployment": {"id": {"owner": "akash1x", "dseq": dseq}, "state": "active"},
        "groups": [{"group_spec": {"name": n}} for n in group_names],
        "leases": [],
        "projectId": PROJECT,
    }


FLEET = [
    _dep("1789116940502", "flynn-coder"),  # Flynn prod (as on 11-19)
    _dep("1789155323521", "fleet-gemma.c0ffee"),  # Flynn prod, charter-stamped
    _dep("1789000000001", "dcloud"),  # exact protected name
    _dep("1795091557151", "carlos-work"),  # someone else's, unattributed
    _dep("1796000000001", f"just-akash-e2e.{RUN}"),  # THIS run's
    _dep("1796000000002", "just-akash-e2e.ffffffff"),  # another run of this repo
    _dep("1796000000003", "just-akash-e2e"),  # this repo, no run stamp
]


class _FakeClient(AkashConsoleAPI):
    def __init__(self, rows):  # no network, no key
        self._rows = rows
        self.closed: list[str] = []

    def list_deployments(self, active_only: bool = True, max_pages=None):
        return list(self._rows)

    def close_deployment(self, dseq: str):
        self.closed.append(dseq)
        return {"dseq": dseq}


def _actions(plan):
    return {p.dseq: p.action for p in plan}


# -- the API: no unscoped mode ------------------------------------------------------------
def test_an_unscoped_bulk_close_is_refused_before_anything_is_listed():
    client = _FakeClient(FLEET)
    client.list_deployments = None  # type: ignore[assignment]  # must not even be called
    with pytest.raises(ValueError, match="unscoped"):
        client.close_all_deployments(execute=True)
    assert client.closed == []


def test_the_default_is_a_dry_run_even_with_a_scope():
    client = _FakeClient(FLEET)
    result = client.close_all_deployments(run_tag=RUN)
    assert client.closed == []
    assert result["dry_run"] is True
    assert _actions(result["plan"])["1796000000001"] == "close"


def test_only_this_runs_dseqs_are_closed():
    client = _FakeClient(FLEET)
    client.close_all_deployments(run_tag=RUN, execute=True)
    assert client.closed == ["1796000000001"]


def test_a_foreign_dseq_is_skipped_with_a_reason():
    client = _FakeClient(FLEET)
    result = client.close_all_deployments(run_tag=RUN)
    by = {p.dseq: p for p in result["plan"]}
    for foreign in ("1795091557151", "1796000000002", "1796000000003"):
        assert by[foreign].action == "skip", foreign
        assert by[foreign].reason


def test_an_allowlisted_dseq_is_closed():
    client = _FakeClient(FLEET)
    client.close_all_deployments(allow_dseqs=["1795091557151"], execute=True)
    assert client.closed == ["1795091557151"]


@pytest.mark.parametrize("prod", ["1789116940502", "1789155323521", "1789000000001"])
def test_a_prod_placement_is_refused_even_when_allowlisted(prod):
    client = _FakeClient(FLEET)
    result = client.close_all_deployments(allow_dseqs=[prod], execute=True)
    assert client.closed == []
    assert _actions(result["plan"])[prod] == "refuse"


def test_an_explicitly_protected_dseq_is_refused_even_when_allowlisted(monkeypatch):
    monkeypatch.setenv(bulk_close.PROTECTED_DSEQS_ENV, "1795091557151, 42")
    client = _FakeClient(FLEET)
    client.close_all_deployments(allow_dseqs=["1795091557151"], execute=True)
    assert client.closed == []


def test_an_allowlisted_dseq_whose_groups_cannot_be_read_is_refused():
    """Protection cannot be confirmed without the group names, so it is not assumed."""
    client = _FakeClient([{"deployment": {"id": {"dseq": "77"}, "state": "active"}}])
    client.close_all_deployments(allow_dseqs=["77"], execute=True)
    assert client.closed == []


def test_a_run_tag_that_is_not_a_run_id_is_refused():
    with pytest.raises(ValueError, match="run"):
        _FakeClient(FLEET).close_all_deployments(run_tag="", allow_dseqs=[], execute=True)
    with pytest.raises(ValueError, match="run"):
        _FakeClient(FLEET).close_all_deployments(run_tag="e2e", execute=True)


# -- the human-only override -------------------------------------------------------------
@pytest.mark.parametrize("env", [{"CI": "true"}, {"GITHUB_ACTIONS": "true"}, {"CLAUDECODE": "1"}])
def test_the_protection_override_is_refused_in_ci_and_from_an_agent(monkeypatch, env):
    for k in bulk_close.NON_HUMAN_ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(bulk_close, "_stdin_is_tty", lambda: True)
    with pytest.raises(PermissionError):
        _FakeClient(FLEET).close_all_deployments(
            allow_dseqs=["1789116940502"], execute=True, override_protected=True
        )


def test_the_protection_override_is_refused_without_a_terminal(monkeypatch):
    for k in bulk_close.NON_HUMAN_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(bulk_close, "_stdin_is_tty", lambda: False)
    with pytest.raises(PermissionError):
        _FakeClient(FLEET).close_all_deployments(
            allow_dseqs=["1789116940502"], execute=True, override_protected=True
        )


def test_a_human_at_a_terminal_can_override_for_an_allowlisted_dseq_only(monkeypatch):
    """THE CONTROL: the override exists, and still never widens past the allowlist."""
    for k in bulk_close.NON_HUMAN_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(bulk_close, "_stdin_is_tty", lambda: True)
    client = _FakeClient(FLEET)
    client.close_all_deployments(
        allow_dseqs=["1789116940502"], execute=True, override_protected=True
    )
    assert client.closed == ["1789116940502"]


def test_the_override_never_reaches_a_protected_dseq_that_is_not_allowlisted(monkeypatch):
    """Own-run is not enough under the override: only the allowlist can name a protected dseq."""
    for k in bulk_close.NON_HUMAN_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(bulk_close, "_stdin_is_tty", lambda: True)
    monkeypatch.setenv(bulk_close.PROTECTED_DSEQS_ENV, "1796000000001")
    client = _FakeClient(FLEET)
    result = client.close_all_deployments(
        run_tag=RUN, allow_dseqs=["1789116940502"], execute=True, override_protected=True
    )
    assert client.closed == ["1789116940502"]
    assert _actions(result["plan"])["1796000000001"] == "refuse"


# -- the CLI entry points -----------------------------------------------------------------
def _cli(monkeypatch, argv, client):
    import sys

    from just_akash import cli

    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.setattr("just_akash.api.AkashConsoleAPI", lambda key: client)
    monkeypatch.setattr(sys, "argv", argv)
    try:
        cli.main()
    except SystemExit as e:
        return e.code
    return 0


def test_destroy_all_with_no_scope_closes_nothing_and_says_why(monkeypatch, capsys, tmp_path):
    import just_akash.api as api_mod

    monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")
    client = _FakeClient(FLEET)
    _cli(monkeypatch, ["just-akash", "destroy-all", "--yes"], client)
    assert client.closed == []
    assert "just-akash-73o" in capsys.readouterr().out


def test_destroy_all_with_a_scope_but_no_yes_is_a_dry_run(monkeypatch, tmp_path):
    import just_akash.api as api_mod

    monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")
    client = _FakeClient(FLEET)
    _cli(monkeypatch, ["just-akash", "destroy-all", "--run-tag", RUN], client)
    assert client.closed == []


def test_destroy_all_with_scope_and_yes_closes_only_the_scope(monkeypatch, tmp_path):
    import just_akash.api as api_mod

    monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")
    client = _FakeClient(FLEET)
    _cli(
        monkeypatch,
        [
            "just-akash",
            "destroy-all",
            "--run-tag",
            RUN,
            "--allow-dseq",
            "1789116940502",
            "--project-id",
            PROJECT,
            "--yes",
        ],
        client,
    )
    assert client.closed == ["1796000000001"]


def test_the_legacy_close_all_command_is_scoped_too(monkeypatch, capsys, tmp_path):
    import sys

    import just_akash.api as api_mod

    monkeypatch.setattr(api_mod, "TAGS_FILE", tmp_path / ".tags.json")
    client = _FakeClient(FLEET)
    monkeypatch.setenv("AKASH_API_KEY", "test-key")
    monkeypatch.setattr(api_mod, "AkashConsoleAPI", lambda key: client)
    monkeypatch.setattr(sys, "argv", ["api", "--yes", "close-all"])
    with pytest.raises(SystemExit):
        api_mod.api_main()
    assert client.closed == []
    assert "just-akash-73o" in capsys.readouterr().out


# -- AC5: e2e cleanup stays own-dseq only -------------------------------------------------
def test_no_e2e_harness_or_workflow_reaches_a_bulk_close():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    sources = [
        *(root / "just_akash").glob("*e2e*.py"),
        root / "just_akash" / "test_lifecycle.py",
        *(root / ".github" / "workflows").glob("*.yml"),
        *(root / ".github" / "actions").rglob("*.yml"),
    ]
    assert len(sources) > 5
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for needle in ("close_all_deployments", "destroy-all", "close-all"):
            assert needle not in text, f"{path.relative_to(root)} reaches {needle}"
