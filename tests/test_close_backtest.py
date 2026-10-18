"""Every test here is a wrong-close vector, not a happy path.

close_backtest closes a LEASED deployment on a SHARED wallet, by a name a person typed. The
bar is the one test_close_orphans.py sets: can it ever close something that is not a leaked
backtest of this wallet? Each refusal test below is one way it could.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from just_akash import chain, close_backtest
from just_akash.cleanup_stale import STALE_E2E_AGE_SECONDS

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
OTHER = "akash1cklqag0e0v794msgqkr6uwf7sr6zyy42gm78f0"
GROUP = "just-akash-backtest.482950fc2cf3"
REPO = Path(__file__).resolve().parents[1]


def _dseq_aged(seconds: float) -> str:
    """dseqs are ms-since-epoch, so age is decodable from them."""
    return str(int((time.time() - seconds) * 1000))


OLD = _dseq_aged(STALE_E2E_AGE_SECONDS + 3600)


def _detail(dseq, *, owner=OWNER, state="active", services=("backtest",)):
    return {
        "deployment": {"id": {"owner": owner, "dseq": dseq}, "state": state},
        "leases": [
            {
                "id": {"owner": owner, "dseq": dseq, "provider": "akash1p"},
                "state": "active",
                "status": {"services": {s: {} for s in services}},
            }
        ],
        "escrow_account": {"state": {"state": "open", "funds": []}},
    }


class _FakeClient:
    """Stands in for AkashConsoleAPI and records every close so a test can assert on none."""

    def __init__(self, detail, after_close_state="closed"):
        self._detail = detail
        self._after = after_close_state
        self.closed: list[str] = []

    def account_address(self):
        return OWNER

    def get_deployment(self, dseq):
        if isinstance(self._detail, Exception):
            raise self._detail
        if self.closed:
            d = dict(self._detail)
            d["deployment"] = {**d["deployment"], "state": self._after}
            return d
        return self._detail

    def close_deployment(self, dseq):
        self.closed.append(dseq)
        return {}


@pytest.fixture
def chain_says(monkeypatch):
    """What the >= 2 LCD sources agree the group population is."""
    box = {"names": [GROUP], "raise": None}

    def fake(owner, dseq, expected_group):
        if box["raise"]:
            raise box["raise"]
        return box["names"]

    monkeypatch.setattr(chain, "corroborated_deployment_group_names", fake)
    return box


def _why(client, dseq=OLD, group=GROUP) -> str:
    """The refusal reason; a test that expects one fails here if the close was allowed."""
    why = close_backtest.refusal(client, OWNER, dseq, group)
    assert why is not None, "expected a refusal, got VERIFIED"
    return why


def test_a_leaked_backtest_of_this_wallet_is_verified(chain_says):
    assert close_backtest.refusal(_FakeClient(_detail(OLD)), OWNER, OLD, GROUP) is None


@pytest.mark.parametrize(
    "group", ["akash", "just-akash-canary.09a75b4f3236", GROUP + "0", "x" + GROUP, GROUP.upper()]
)
def test_a_group_that_is_not_a_backtest_group_is_refused(chain_says, group):
    assert "not just-akash-backtest" in _why(_FakeClient(_detail(OLD)), group=group)


@pytest.mark.parametrize("dseq", ["0", "-1", "12 34", "1;rm", "99999999999999999999"])
def test_a_dseq_that_is_not_one_plain_dseq_is_refused(chain_says, dseq):
    assert "dseq" in _why(_FakeClient(_detail(dseq)), dseq=dseq)


def test_the_chain_naming_a_different_group_is_refused(chain_says):
    """The person typed the wrong group, or the dseq is someone else's backtest."""
    chain_says["names"] = []
    assert "chain sources do not agree" in _why(_FakeClient(_detail(OLD)))


def test_two_groups_on_chain_are_refused(chain_says):
    chain_says["names"] = [GROUP, "just-akash-backtest.000000000000"]
    assert "chain sources do not agree" in _why(_FakeClient(_detail(OLD)))


def test_an_unreachable_chain_is_refused(chain_says):
    chain_says["raise"] = chain.ChainCorroborationUnreachable({"a": "timeout", "b": "timeout"})
    assert "unreadable" in _why(_FakeClient(_detail(OLD)))


def test_another_wallets_deployment_is_refused(chain_says):
    assert "is not this key's wallet" in _why(_FakeClient(_detail(OLD, owner=OTHER)))


@pytest.mark.parametrize("state", ["closed", "insufficient_funds", ""])
def test_a_deployment_that_is_not_active_is_refused(chain_says, state):
    assert "not active" in _why(_FakeClient(_detail(OLD, state=state)))


@pytest.mark.parametrize("services", [("vllm",), ("backtest", "sidecar"), ("canary",), ("probe",)])
def test_a_service_set_other_than_exactly_backtest_is_refused(chain_says, services):
    assert "are not exactly" in _why(_FakeClient(_detail(OLD, services=services)))


def test_no_services_reported_is_unclassifiable_not_a_backtest(chain_says):
    assert "unclassifiable" in _why(_FakeClient(_detail(OLD, services=())))


def test_a_backtest_younger_than_the_e2e_floor_is_refused(chain_says):
    young = _dseq_aged(STALE_E2E_AGE_SECONDS - 3600)
    assert "old; a backtest is stale after" in _why(_FakeClient(_detail(young)), dseq=young)


def test_an_unknown_age_is_refused(chain_says):
    assert "age unknown" in _why(_FakeClient(_detail("12345")), dseq="12345")


def test_an_unreadable_deployment_is_refused(chain_says):
    assert "unreadable" in _why(_FakeClient(RuntimeError("502")))


# ── run(): dry run closes nothing; execute closes exactly that dseq, re-checked ─────────


@pytest.fixture
def wired(monkeypatch, chain_says):
    monkeypatch.setenv("AKASH_API_KEY", "test-key")  # pragma: allowlist secret
    monkeypatch.setattr(close_backtest, "_credit_line", lambda c, a: "n/a")
    monkeypatch.setattr(close_backtest.time, "sleep", lambda s: None)
    ended: list[str] = []
    monkeypatch.setattr(close_backtest.lease_intent, "close_quietly", lambda d, r: ended.append(d))
    holder: dict = {}

    def use(client):
        monkeypatch.setattr(close_backtest, "AkashConsoleAPI", lambda key: client)
        holder["client"] = client
        return client

    return {"use": use, "ended": ended, "chain": chain_says}


def test_dry_run_closes_nothing(wired):
    client = wired["use"](_FakeClient(_detail(OLD)))
    assert close_backtest.run(dseq=OLD, group=GROUP, execute=False) == 0
    assert client.closed == []


def test_a_refusal_exits_nonzero_and_closes_nothing_even_with_execute(wired, capsys):
    client = wired["use"](_FakeClient(_detail(OLD, services=("vllm",))))
    assert close_backtest.run(dseq=OLD, group=GROUP, execute=True) == 1
    assert client.closed == []
    assert "::error::" in capsys.readouterr().out


def test_execute_closes_exactly_that_dseq_and_ends_its_intent(wired):
    client = wired["use"](_FakeClient(_detail(OLD)))
    assert close_backtest.run(dseq=OLD, group=GROUP, execute=True) == 0
    assert client.closed == [OLD]
    assert wired["ended"] == [OLD]


def test_execute_rechecks_the_chain_at_close_time(wired, monkeypatch):
    """The plan job's verdict is minutes old when an approver starts the close job."""
    client = wired["use"](_FakeClient(_detail(OLD)))
    answers = iter([[GROUP], []])
    monkeypatch.setattr(
        chain, "corroborated_deployment_group_names", lambda o, d, g: next(answers)
    )
    assert close_backtest.run(dseq=OLD, group=GROUP, execute=True) == 1
    assert client.closed == []


def test_a_close_that_does_not_read_back_terminal_is_not_counted(wired):
    client = wired["use"](_FakeClient(_detail(OLD), after_close_state="active"))
    assert close_backtest.run(dseq=OLD, group=GROUP, execute=True) == 1
    assert client.closed == [OLD]
    assert wired["ended"] == []


def test_no_api_key_is_a_refusal(monkeypatch):
    monkeypatch.delenv("AKASH_API_KEY", raising=False)
    assert close_backtest.run(dseq=OLD, group=GROUP, execute=True) == 2


# ── the workflow: dispatch-only, gated close, inputs never interpolated into run ────────


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


def test_only_the_gated_job_can_close():
    jobs = _wf()["jobs"]
    assert jobs["close"]["environment"] == "production"
    assert jobs["close"]["needs"] == "plan"
    assert "inputs.execute" in jobs["close"]["if"]
    plan_runs = " ".join(s.get("run", "") for s in jobs["plan"]["steps"])
    close_runs = " ".join(s.get("run", "") for s in jobs["close"]["steps"])
    assert "--execute" not in plan_runs
    assert "--execute" in close_runs


def test_the_plan_refuses_execute_without_a_protected_production_environment():
    """GitHub creates a named environment on first use UNPROTECTED."""
    steps = _wf()["jobs"]["plan"]["steps"]
    gate = [s for s in steps if "environments/production" in s.get("run", "")]
    assert gate and "required_reviewers" in gate[0]["run"]
    assert gate[0]["if"] == "${{ inputs.execute }}"


def test_inputs_never_reach_a_run_block_by_interpolation():
    for job in _wf()["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), step.get("name")


def test_it_shares_the_wallet_concurrency_group():
    assert _wf()["concurrency"] == {"group": "akash-wallet", "cancel-in-progress": False}
