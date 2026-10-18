"""Every test here is a wrong-close vector, not a happy path.

close_backtest closes a LEASED deployment on a SHARED wallet, by a name a person typed. The
bar is the one test_close_orphans.py sets: can it ever close something that is not the named
leaked backtest? Each refusal test below is one way it could. The chain view is exercised for
real against fake LCD answers (no network); only the transport is faked.
"""

from __future__ import annotations

import json
import os
import re
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
PIN = TIP - cb.FINALITY_DEPTH
FLOOR_BLOCKS = int(STALE_E2E_AGE_SECONDS / SECONDS_PER_BLOCK)
OLD_CREATED = PIN - FLOOR_BLOCKS - 1000


def _source(sid, url, operator):
    return {
        "source_id": sid,
        "url": url,
        "operator": operator,
        "chain_id": "akashnet-2",
        "max_age_seconds": 180,
        "max_height_skew": 5,
    }


SOURCES = (
    _source("a", "https://a.example:443", "op-a"),
    _source("b", "https://b.example:443/api", "op-b"),
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


def _tip(height=TIP, block_time=None, chain_id="akashnet-2"):
    t = (block_time or NOW - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return {"block": {"header": {"height": str(height), "time": t, "chain_id": chain_id}}}


class _Lcd:
    """Answers per host; records every (host, kind, height) read."""

    def __init__(self, **per_host):
        self.answers: dict[str, dict] = {"a.example": {}, "b.example": {}}
        for host, over in per_host.items():
            self.answers[host.replace("_", ".")].update(over)
        self.reads: list[tuple[str, str, int | None]] = []

    def read(self, path, base, height):
        host = base.split("//")[1].split(":")[0].split("/")[0]
        kind = "tip" if "blocks/latest" in path else "info"
        self.reads.append((host, kind, height))
        mine = self.answers[host]
        if "raise" in mine:
            raise mine["raise"]
        return mine.get(kind, _tip() if kind == "tip" else _info())


def _view(lcd=None, sources=SOURCES, clock=None, dseq=DSEQ, group=GROUP):
    lcd = lcd or _Lcd()
    return cb._chain_view(
        OWNER, dseq, group, sources=sources, reader=lcd.read, clock=clock or (lambda: NOW)
    )


def _refused(view_result) -> str:
    view, why = view_result
    assert view is None and why is not None, "expected a refusal, got VERIFIED"
    return why


def test_two_registered_operators_agreeing_at_a_pinned_height_verify():
    lcd = _Lcd()
    view, why = _view(lcd)
    assert why is None and view is not None
    assert view.created_height == OLD_CREATED and view.pinned_height == PIN
    # Tips unpinned; every deployment read pinned to the SAME corroborated height.
    assert [h for _, kind, h in lcd.reads if kind == "tip"] == [None, None]
    assert [h for _, kind, h in lcd.reads if kind == "info"] == [PIN, PIN]


def test_a_deployment_answer_with_a_wrong_echoed_height_is_refused(monkeypatch):
    """Codex round 2, P1-a: the REAL reader sends x-cosmos-block-height and refuses an answer
    that echoes another height (a cached or historical response)."""
    sent = {}

    class _Resp:
        headers = {"x-cosmos-block-height": "1"}

        def read(self):
            return json.dumps(_info()).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout):
            sent["height"] = req.get_header("X-cosmos-block-height")
            return _Resp()

    monkeypatch.setattr(cb.chain.urllib.request, "build_opener", lambda *h: _Opener())
    with pytest.raises(cb.chain.ChainResponseError, match="echo pinned height"):
        cb._read_no_redirect("/akash/x", "https://a.example", PIN)
    assert sent["height"] == str(PIN)


def test_an_unreadable_pinned_read_is_refused():
    lcd = _Lcd()
    real = lcd.read

    def read(path, base, height):
        if height is not None and "b.example" in base:
            raise cb.chain.ChainResponseError("did not echo pinned height")
        return real(path, base, height)

    assert f"unreadable at height {PIN}" in _refused(
        cb._chain_view(OWNER, DSEQ, GROUP, sources=SOURCES, reader=read, clock=lambda: NOW)
    )


def test_two_sources_from_one_registered_operator_are_one_vote():
    """Codex round 2, P1-b: independence is the registry's distinct operators, not DNS."""
    same = (SOURCES[0], _source("b", "https://b.example:443/api", "op-a"))
    assert "share a host or an operator" in _refused(_view(sources=same))


def test_two_sources_on_one_host_are_one_vote():
    same = (SOURCES[0], _source("b", "https://a.example:443/other", "op-b"))
    assert "share a host or an operator" in _refused(_view(sources=same))


def test_one_source_is_not_enough():
    assert "only 1 registered" in _refused(_view(sources=SOURCES[:1]))


def test_a_registered_source_on_another_chain_is_refused():
    other = (SOURCES[0], {**SOURCES[1], "chain_id": "sandbox-01"})
    assert "not on akashnet-2" in _refused(_view(sources=other))


def test_a_tip_from_another_chain_is_refused():
    """Codex round 2, P2-d: a wrong chain id must not verify."""
    lcd = _Lcd(b_example={"tip": _tip(chain_id="sandbox-01")})
    assert "is on chain 'sandbox-01'" in _refused(_view(lcd))


@pytest.mark.parametrize("created", ["0", "-1", "", "abc", "1e9", None, 12345])
def test_a_malformed_created_at_is_refused(created):
    """Codex round 2, P2-d: 0 or -1 must not read as 'very old'."""
    info = _info()
    info["deployment"]["created_at"] = created
    lcd = _Lcd(a_example={"info": info}, b_example={"info": info})
    assert "malformed created_at" in _refused(_view(lcd))


def test_a_created_at_above_the_pinned_height_is_refused():
    info = _info(created=PIN + 1)
    lcd = _Lcd(a_example={"info": info}, b_example={"info": info})
    assert "above the pinned height" in _refused(_view(lcd))


@pytest.mark.parametrize("height", ["0", "-5", "", "12a"])
def test_a_malformed_tip_height_is_refused(height):
    tip = _tip()
    tip["block"]["header"]["height"] = height
    assert "malformed tip height" in _refused(_view(_Lcd(a_example={"tip": tip})))


def test_a_source_reporting_the_deployment_not_active_is_refused():
    assert "not active" in _refused(_view(_Lcd(b_example={"info": _info(state="closed")})))


def test_sources_disagreeing_on_created_at_are_refused():
    lcd = _Lcd(b_example={"info": _info(created=OLD_CREATED - 7)})
    assert "disagree on created_at" in _refused(_view(lcd))


def test_tips_too_far_apart_are_refused():
    assert "blocks apart" in _refused(_view(_Lcd(b_example={"tip": _tip(height=TIP + 6)})))


def test_a_stale_tip_is_refused():
    lcd = _Lcd(a_example={"tip": _tip(block_time=NOW - timedelta(seconds=600))})
    assert "not fresh" in _refused(_view(lcd))


def test_a_block_committed_during_verification_is_fresh():
    """Codex round 2, P2-c: freshness is judged when THAT response arrived. Source b's tip
    was committed 20 s after verification began and arrived 1 s later."""
    start = NOW
    lcd = _Lcd(b_example={"tip": _tip(height=TIP + 3, block_time=start + timedelta(seconds=20))})
    arrivals = iter([start + timedelta(seconds=1), start + timedelta(seconds=21)])
    view, why = _view(lcd, clock=lambda: next(arrivals))
    assert why is None and view is not None


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
    recent = PIN - int(3600 / SECONDS_PER_BLOCK)
    lcd = _Lcd(
        a_example={"info": _info(created=recent)}, b_example={"info": _info(created=recent)}
    )
    assert "h old on chain" in _refused(_view(lcd))


@pytest.mark.parametrize("minutes,ok", [(48 * 60 - 1, False), (48 * 60 + 1, True)])
def test_the_48h_floor_is_exact(minutes, ok):
    """Codex round 2: a mutant lowering the floor to 2 h passed every earlier test."""
    created = PIN - int(minutes * 60 / SECONDS_PER_BLOCK)
    lcd = _Lcd(
        a_example={"info": _info(created=created)}, b_example={"info": _info(created=created)}
    )
    view, why = _view(lcd)
    assert (view is not None) is ok, why


def test_an_unreadable_source_is_refused():
    assert "unreadable" in _refused(_view(_Lcd(b_example={"raise": RuntimeError("timeout")})))


def test_an_unexpected_shape_is_refused():
    assert "unexpected tip shape" in _refused(_view(_Lcd(a_example={"tip": {"block": {}}})))


def test_the_real_reader_does_not_follow_redirects(monkeypatch):
    seen = {}

    def fake_lcd_get(path, *, base, timeout, height, follow_redirects):
        seen.update(follow_redirects=follow_redirects, height=height)
        return {}

    monkeypatch.setattr(cb.chain, "_lcd_get", fake_lcd_get)
    cb._read_no_redirect("/x", "https://a.example", PIN)
    assert seen == {"follow_redirects": False, "height": PIN}


def test_the_registered_sources_satisfy_the_operator_rule():
    """The production registry itself: >= 2 distinct operators and hosts on akashnet-2."""
    assert cb._sources_refusal(cb.chain.OWNER_CORROBORATION_SOURCES_V2) is None


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


@pytest.mark.parametrize(
    "lease,needle",
    [
        (
            {**_lease(), "id": {"owner": OTHER, "dseq": DSEQ, "provider": "akash1p"}},
            "another owner",
        ),
        (
            {**_lease(), "id": {"owner": OWNER, "dseq": "1", "provider": "akash1p"}},
            "another owner",
        ),
        ({k: v for k, v in _lease().items() if k != "id"}, "has no id"),
    ],
)
def test_the_active_lease_must_name_this_owner_and_dseq(lease, needle):
    """Codex round 2, P2-e."""
    why = cb.console_refusal(_Client(_detail(leases=[lease])), OWNER, DSEQ)
    assert why is not None and needle in why


def test_a_vllm_lease_with_no_state_beside_a_backtest_lease_is_refused():
    """Codex round 2, P2-e: a row we cannot classify could be the live one."""
    stateless = {k: v for k, v in _lease(services=("vllm",)).items() if k != "state"}
    why = cb.console_refusal(_Client(_detail(leases=[_lease(), stateless])), OWNER, DSEQ)
    assert why is not None and "unknown state" in why


@pytest.mark.parametrize(
    "state", ["", "ACTIVE", "unknown", "Active", "paused", "reclaiming", None, 1, [], {}]
)
def test_a_lease_row_in_an_unknown_state_beside_a_backtest_lease_is_refused(state):
    """Codex round 3: [active backtest, state="" vllm] used to pass and send a DELETE."""
    odd = {**_lease(services=("vllm",)), "state": state}
    why = cb.console_refusal(_Client(_detail(leases=[_lease(), odd])), OWNER, DSEQ)
    assert why is not None and "unknown state" in why


@pytest.mark.parametrize("state", ["closed", "insufficient_funds"])
def test_a_finished_lease_beside_the_active_backtest_is_fine(state):
    finished = {**_lease(services=("vllm",)), "state": state}
    assert cb.console_refusal(_Client(_detail(leases=[_lease(), finished])), OWNER, DSEQ) is None


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


def test_a_reporting_failure_after_a_proven_close_keeps_exit_0(wired, monkeypatch, capsys):
    """Codex round 2, P2-f: 1 means "refused, nothing sent"; after a proven close it is false."""
    client = wired["use"](_Client(_detail()))
    monkeypatch.setattr(cb, "_credit_line", lambda c, a: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(
        cb.lease_intent, "close_quietly", lambda d, r: (_ for _ in ()).throw(RuntimeError("y"))
    )
    assert _run() == cb.EXIT_OK
    assert client.closed == [DSEQ]
    assert "::warning::" in capsys.readouterr().out


def test_an_abbreviated_execute_flag_is_rejected():
    """Codex round 2: argparse would read `--exec` as --execute."""
    with pytest.raises(SystemExit):
        cb.main(["--owner", OWNER, "--dseq", DSEQ, "--expected-group", GROUP, "--exec"])


@pytest.mark.parametrize("state", ["", "ACTIVE", "unknown", [], {}])
def test_an_unknown_lease_state_sends_no_delete(wired, state):
    odd = {**_lease(services=("vllm",)), "state": state}
    client = wired["use"](_Client(_detail(leases=[_lease(), odd])))
    assert _run() == cb.EXIT_REFUSED
    assert client.closed == []


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
            text = yaml.safe_dump(step.get(field, "")).lower()
            assert "execute" not in text, step.get("name")
            # Any prefix of --execute (`--e`, `--exec`): reject even though the parser now
            # refuses abbreviations, so neither guard alone carries the property.
            for token in re.split(r"[\s'\"=\\]+", text):
                assert not (token.startswith("--e") and "--execute".startswith(token)), (
                    step.get("name"),
                    token,
                )


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
