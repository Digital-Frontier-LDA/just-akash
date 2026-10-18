"""Every test here is a wrong-close vector, not a happy path.

close_backtest closes a LEASED deployment on a SHARED wallet, by a name a person typed. The
bar is the one test_close_orphans.py sets: can it ever close something that is not the named
leaked backtest? Each refusal test below is one way it could. The chain view is exercised for
real against fake LCD answers (no network); only the transport is faked.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from just_akash import close_backtest as cb
from just_akash.cleanup_stale import STALE_E2E_AGE_SECONDS
from just_akash.wallet_pool import SECONDS_PER_BLOCK

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
OTHER = "akash1cklqag0e0v794msgqkr6uwf7sr6zyy42gm78f0"
DSEQ = "1789163231320"
GROUP = "just-akash-backtest.482950fc2cf3"
REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
TIP = 29_200_000
OLD_CREATED = TIP - int(STALE_E2E_AGE_SECONDS / SECONDS_PER_BLOCK) - 1000
SOURCES = (
    {
        "source_id": "a",
        "url": "https://a.example:443",
        "max_age_seconds": 180,
        "max_height_skew": 5,
    },
    {
        "source_id": "b",
        "url": "https://b.example:443/api",
        "max_age_seconds": 180,
        "max_height_skew": 5,
    },
)


# ── a fake LCD: per-host answers for the two paths the chain view reads ─────────────────


def _info(*, state="active", created=OLD_CREATED, groups=(GROUP,), owner=OWNER, dseq=DSEQ):
    return {
        "deployment": {
            "id": {"owner": owner, "dseq": dseq},
            "state": state,
            "created_at": str(created),
        },
        "groups": [
            {"id": {"owner": owner, "dseq": dseq, "gseq": i}, "group_spec": {"name": name}}
            for i, name in enumerate(groups, start=1)
        ],
    }


def _tip(height=TIP, age_s=10):
    t = (NOW - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {"block": {"header": {"height": str(height), "time": t}}}


class _Lcd:
    def __init__(self, **per_host):
        self.answers = {"a.example": {}, "b.example": {}}
        for host, over in per_host.items():
            self.answers[host.replace("_", ".")].update(over)

    def read(self, path, base):
        host = base.split("//")[1].split(":")[0].split("/")[0]
        mine = self.answers[host]
        if "raise" in mine:
            raise mine["raise"]
        if "blocks/latest" in path:
            return mine.get("tip", _tip())
        return mine.get("info", _info())


def _view(lcd=None, resolve=None, dseq=DSEQ, group=GROUP):
    lcd = lcd or _Lcd()
    resolve = resolve or (
        lambda host: frozenset({{"a.example": "1.1.1.1", "b.example": "2.2.2.2"}[host]})
    )
    return cb._chain_view(
        OWNER, dseq, group, sources=SOURCES, reader=lcd.read, resolve=resolve, now=NOW
    )


def _refused(view_result) -> str:
    view, why = view_result
    assert view is None and why is not None, "expected a refusal, got VERIFIED"
    return why


def test_two_independent_fresh_agreeing_sources_verify():
    view, why = _view()
    assert why is None and view is not None
    assert view.created_height == OLD_CREATED
    assert view.age_seconds >= STALE_E2E_AGE_SECONDS


def test_two_urls_on_one_upstream_count_once():
    """Codex OBJECT 5: independence is by resolved address, not by URL."""
    assert "only 1 independent" in _refused(_view(resolve=lambda host: frozenset({"9.9.9.9"})))


def test_a_source_reporting_the_deployment_not_active_is_refused():
    assert "not active" in _refused(_view(_Lcd(b_example={"info": _info(state="closed")})))


def test_sources_disagreeing_on_created_at_are_refused():
    lcd = _Lcd(b_example={"info": _info(created=OLD_CREATED - 7)})
    assert "disagree on created_at" in _refused(_view(lcd))


def test_tips_too_far_apart_are_refused():
    assert "blocks apart" in _refused(_view(_Lcd(b_example={"tip": _tip(height=TIP + 6)})))


def test_a_stale_tip_is_refused():
    assert "not fresh" in _refused(_view(_Lcd(a_example={"tip": _tip(age_s=600)})))


@pytest.mark.parametrize(
    "groups", [("just-akash-backtest.000000000000",), (GROUP, "just-akash-backtest.000000000000")]
)
def test_any_group_population_but_exactly_the_named_group_is_refused(groups):
    assert "only group" in _refused(_view(_Lcd(a_example={"info": _info(groups=groups)})))


def test_another_wallets_deployment_is_refused():
    assert "only group" in _refused(_view(_Lcd(a_example={"info": _info(owner=OTHER)})))


def test_age_comes_from_the_chain_height_not_the_dseq():
    """Codex OBJECT 2a: the deployer picks the dseq. A dseq that 'decodes' to a month ago on
    a deployment the chain created an hour ago is young."""
    recent = TIP - int(3600 / SECONDS_PER_BLOCK)
    lcd = _Lcd(
        a_example={"info": _info(created=recent)}, b_example={"info": _info(created=recent)}
    )
    assert "h old on chain" in _refused(_view(lcd))


def test_an_unreadable_source_is_refused():
    assert "unreadable" in _refused(_view(_Lcd(b_example={"raise": RuntimeError("timeout")})))


def test_an_unexpected_shape_is_refused():
    assert "unexpected shape" in _refused(_view(_Lcd(a_example={"tip": {"block": {}}})))


def test_the_real_reader_does_not_follow_redirects(monkeypatch):
    seen = {}

    def fake_lcd_get(path, *, base, timeout, follow_redirects):
        seen["follow_redirects"] = follow_redirects
        return {}

    monkeypatch.setattr(cb.chain, "_lcd_get", fake_lcd_get)
    cb._read_no_redirect("/x", "https://a.example")
    assert seen == {"follow_redirects": False}


def test_an_rest_override_is_refused_on_the_destructive_read(monkeypatch):
    monkeypatch.setenv("AKASH_REST_URL", "https://evil.example")
    assert "AKASH_REST_URL" in _refused(cb.chain_view(OWNER, DSEQ, GROUP))


@pytest.mark.parametrize(
    "owner,dseq,group",
    [
        ("akash1short", DSEQ, GROUP),
        (OWNER, "0", GROUP),
        (OWNER, "1;rm", GROUP),
        (OWNER, "99999999999999999999", GROUP),
        (OWNER, DSEQ, "akash"),
        (OWNER, DSEQ, "just-akash-canary.09a75b4f3236"),
        (OWNER, DSEQ, GROUP + "0"),
        (OWNER, DSEQ, GROUP.upper()),
    ],
)
def test_malformed_inputs_are_refused(owner, dseq, group):
    assert cb.shape_refusal(owner, dseq, group) is not None


# ── the Console view: exactly one active lease, its status read, exactly {backtest} ──────


def _lease(state="active", services=("backtest",), status: object = "ok"):
    st = {"services": {s: {} for s in services}} if status == "ok" else status
    return {
        "id": {"owner": OWNER, "dseq": DSEQ, "provider": "akash1p"},
        "state": state,
        "status": st,
    }


def _detail(*, leases=None, owner=OWNER, dseq=DSEQ, state="active"):
    return {
        "deployment": {"id": {"owner": owner, "dseq": dseq}, "state": state},
        "leases": [_lease()] if leases is None else leases,
    }


class _Client:
    """Stands in for AkashConsoleAPI. Records every close so a test can assert on none."""

    def __init__(self, detail, *, after=None, close_raises=None, address=OWNER):
        self.detail = detail
        self.after = after if after is not None else _detail(state="closed")
        self.close_raises = close_raises
        self.address = address
        self.closed: list[str] = []
        self.calls: list[str] = []

    def account_address(self):
        return self.address

    def get_deployment(self, dseq):
        self.calls.append("console-read")
        source = self.after if self.closed else self.detail
        if isinstance(source, Exception):
            raise source
        return source

    def close_deployment(self, dseq):
        self.calls.append("delete")
        self.closed.append(dseq)
        if self.close_raises:
            raise self.close_raises
        return {}


def test_one_active_backtest_lease_passes():
    assert cb.console_refusal(_Client(_detail()), OWNER, DSEQ) is None


def test_a_closed_backtest_lease_cannot_vouch_for_an_unreadable_active_one():
    """Codex OBJECT 3: the old helper unioned services across ALL leases."""
    leases = [_lease(state="closed"), _lease(status=None)]
    why = cb.console_refusal(_Client(_detail(leases=leases)), OWNER, DSEQ)
    assert why is not None and "could not be read" in why


@pytest.mark.parametrize(
    "leases,needle",
    [
        ([], "0 active lease"),
        ([_lease(), _lease()], "2 active lease"),
        ([_lease(services=("vllm",))], "not exactly"),
        ([_lease(services=("backtest", "sidecar"))], "not exactly"),
        ([_lease(services=())], "could not be read"),
        ([_lease(state="closed")], "0 active lease"),
    ],
)
def test_anything_but_one_active_backtest_lease_is_refused(leases, needle):
    why = cb.console_refusal(_Client(_detail(leases=leases)), OWNER, DSEQ)
    assert why is not None and needle in why


@pytest.mark.parametrize(
    "detail,needle",
    [
        (_detail(owner=OTHER), "owner"),
        (_detail(dseq="1"), "different dseq"),
        (_detail(state="closed"), "not active"),
        (RuntimeError("502"), "unreadable"),
    ],
)
def test_a_console_record_that_disagrees_is_refused(detail, needle):
    why = cb.console_refusal(_Client(detail), OWNER, DSEQ)
    assert why is not None and needle in why


# ── run(): plan holds no key; execute orders the reads and proves the close ──────────────


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setenv("AKASH_API_KEY", "test-key")  # pragma: allowlist secret
    monkeypatch.setattr(cb, "_credit_line", lambda c, a: "n/a")
    monkeypatch.setattr(cb.time, "sleep", lambda s: None)
    ended: list[str] = []
    monkeypatch.setattr(cb.lease_intent, "close_quietly", lambda d, r: ended.append(d))
    state: dict = {"view": (cb.ChainView(OLD_CREATED, TIP, STALE_E2E_AGE_SECONDS + 1), None)}

    def fake_view(owner, dseq, group):
        state.get("during_view", lambda: None)()
        if "client" in state:
            state["client"].calls.append("chain-view")
        return state["view"]

    monkeypatch.setattr(cb, "chain_view", fake_view)

    def use(client):
        monkeypatch.setattr(cb, "AkashConsoleAPI", lambda key: client)
        state["client"] = client
        return client

    state["use"] = use
    state["ended"] = ended
    return state


def _run(execute=True):
    return cb.run(owner=OWNER, dseq=DSEQ, group=GROUP, execute=execute)


def test_plan_never_builds_a_console_client(wired, monkeypatch):
    def boom(key):
        raise AssertionError("plan must not touch the key")

    monkeypatch.setattr(cb, "AkashConsoleAPI", boom)
    monkeypatch.delenv("AKASH_API_KEY")
    assert _run(execute=False) == cb.EXIT_OK


def test_execute_reads_the_console_last_then_deletes_then_reads_back(wired):
    client = wired["use"](_Client(_detail()))
    assert _run() == cb.EXIT_OK
    assert client.calls == ["chain-view", "console-read", "delete", "console-read"]
    assert client.closed == [DSEQ] and wired["ended"] == [DSEQ]


def test_a_service_change_during_corroboration_is_refused(wired):
    """Codex OBJECT 4: the Console read must come AFTER the chain view."""
    client = wired["use"](_Client(_detail()))
    wired["during_view"] = lambda: setattr(
        client, "detail", _detail(leases=[_lease(services=("vllm",))])
    )
    assert _run() == cb.EXIT_REFUSED
    assert client.closed == []


def test_a_key_for_another_wallet_is_refused(wired):
    client = wired["use"](_Client(_detail(), address=OTHER))
    assert _run() == cb.EXIT_REFUSED and client.closed == []


def test_a_chain_refusal_sends_nothing(wired):
    client = wired["use"](_Client(_detail()))
    wired["view"] = (None, "only 1 independent chain source(s) answered")
    assert _run() == cb.EXIT_REFUSED and client.closed == []


@pytest.mark.parametrize(
    "after",
    [
        _detail(state="insufficient_funds"),
        _detail(state="active"),
        _detail(state="closed", owner=OTHER),
        _detail(state="closed", dseq="1"),
        RuntimeError("502"),
    ],
)
def test_only_this_owner_this_dseq_state_closed_counts_as_closed(wired, after):
    """Codex OBJECT 6."""
    client = wired["use"](_Client(_detail(), after=after))
    assert _run() == cb.EXIT_UNKNOWN
    assert wired["ended"] == []
    assert client.closed == [DSEQ]


def test_a_delete_whose_reply_is_lost_but_committed_is_a_success(wired):
    """Codex OBJECT 7."""
    client = wired["use"](_Client(_detail(), close_raises=TimeoutError("read timed out")))
    assert _run() == cb.EXIT_OK
    assert client.closed == [DSEQ] and wired["ended"] == [DSEQ]


def test_a_delete_that_raises_and_does_not_read_back_closed_is_unknown(wired, capsys):
    client = wired["use"](
        _Client(_detail(), after=_detail(state="active"), close_raises=TimeoutError("timed out"))
    )
    assert _run() == cb.EXIT_UNKNOWN
    assert "outcome UNKNOWN" in capsys.readouterr().out
    assert client.closed == [DSEQ]


def test_malformed_input_is_usage_and_sends_nothing(wired):
    client = wired["use"](_Client(_detail()))
    assert cb.run(owner=OWNER, dseq="1;rm", group=GROUP, execute=True) == cb.EXIT_USAGE
    assert client.closed == []


# ── the workflow, parsed: plan holds no secret and never executes; close is gated ────────


def _wf() -> dict:
    return yaml.safe_load((REPO / ".github" / "workflows" / "close-backtest.yml").read_text())


def test_the_workflow_is_dispatch_only_with_required_named_inputs():
    doc = _wf()
    on = doc.get("on") or doc.get(True) or {}
    assert set(on) == {"workflow_dispatch"}, "a cron or push trigger has no dseq to name"
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["dseq"]["required"] is True
    assert inputs["expected_group"]["required"] is True
    assert inputs["execute"]["default"] is False
    assert inputs["owner"]["type"] == "choice"


def test_plan_loads_no_secret():
    """Codex OBJECT 1: an ungated job runs branch-controlled code."""
    plan = _wf()["jobs"]["plan"]
    assert "environment" not in plan
    for step in plan["steps"]:
        assert "sops-env" not in str(step.get("uses", "")), step.get("name")
    assert "secrets." not in yaml.safe_dump(plan)


def test_plan_never_passes_execute_to_python_in_any_form():
    for step in _wf()["jobs"]["plan"]["steps"]:
        for field in ("run", "env", "with"):
            assert "execute" not in yaml.safe_dump(step.get(field, "")).lower(), step.get("name")


def test_only_the_gated_job_holds_the_key_and_closes():
    close = _wf()["jobs"]["close"]
    assert close["environment"] == "production"
    assert close["needs"] == "plan"
    assert close["if"].replace(" ", "") == "${{inputs.execute}}"
    assert any("sops-env" in str(s.get("uses", "")) for s in close["steps"])
    assert any("--execute" in s.get("run", "") for s in close["steps"])


def test_inputs_never_reach_a_run_block_by_interpolation():
    for job in _wf()["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), step.get("name")


def test_it_shares_the_wallet_concurrency_group():
    assert _wf()["concurrency"] == {"group": "akash-wallet", "cancel-in-progress": False}


def _gate_step() -> dict:
    steps = [
        s for s in _wf()["jobs"]["plan"]["steps"] if "environments/production" in s.get("run", "")
    ]
    assert len(steps) == 1
    return steps[0]


def test_the_gate_check_runs_only_for_execute():
    assert _gate_step()["if"].replace(" ", "") == "${{inputs.execute}}"


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq, as on GitHub runners")
@pytest.mark.parametrize(
    "environment,ok",
    [
        (None, False),  # no `production` environment: gh api fails
        ({"protection_rules": []}, False),
        ({"protection_rules": [{"type": "wait_timer", "wait_timer": 5}]}, False),
        ({"protection_rules": [{"type": "required_reviewers", "reviewers": []}]}, False),
        (
            {
                "protection_rules": [
                    {"type": "required_reviewers", "reviewers": [{"type": "User"}]}
                ]
            },
            True,
        ),
    ],
)
def test_the_gate_check_passes_only_with_a_real_reviewer(tmp_path, environment, ok):
    """Codex OBJECT 8: run the step's own shell against a fake `gh` that applies the real
    --jq program to a fixture, so the jq and the >= 1 comparison are what is tested."""
    fixture = tmp_path / "env.json"
    fixture.write_text(json.dumps(environment) if environment is not None else "")
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'while [ $# -gt 0 ]; do [ "$1" = "--jq" ] && { JQ="$2"; shift; }; shift; done\n'
        f'[ -s "{fixture}" ] || exit 1\n'
        f'jq -r "$JQ" "{fixture}"\n'
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "REPO": "o/r",
        "GH_TOKEN": "x",
    }
    result = subprocess.run(
        ["bash", "-c", _gate_step()["run"]], env=env, capture_output=True, text=True
    )
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
