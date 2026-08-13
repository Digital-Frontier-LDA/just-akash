"""Reading `group_spec.name` back is what turns provenance from a label into a proof.

just_akash.provenance WRITES the placement key; this is the read half. The key becomes
`group_spec.name` inside MsgCreateDeployment — author-controlled, written atomically,
immutable afterwards — so it is the only thing on chain that says who created a
deployment. just-akash's tags live in a local file and the Console API exposes none.

The version matters and was the whole blocker: every configured endpoint answers
`v1beta3` with HTTP 501 and serves `v1beta4` with a 200. That 501 was recorded here as
"public LCDs don't serve akash-module queries", which closed the door on chain-native
ownership for months. It was a URL version, not a limitation.
"""

from __future__ import annotations

import pytest

from just_akash import chain

OWNER = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
DSEQ = "1786555091232"


def _info(*names: str) -> dict:
    return {"groups": [{"group_spec": {"name": n}} for n in names]}


def test_the_query_targets_the_version_the_chain_actually_serves(monkeypatch):
    """v1beta3 returns 501 on every configured endpoint. Pinning it here would make the
    read path look implemented while never returning anything — the exact shape of the
    belief this replaces."""
    seen: list[str] = []

    def fake(path, timeout=15, base=None):
        seen.append(path)
        return _info("just-akash-backtest")

    monkeypatch.setattr(chain, "_lcd_get", fake)
    chain.deployment_group_names(OWNER, DSEQ)
    assert seen and "/akash/deployment/v1beta4/" in seen[0], seen
    assert "v1beta3" not in seen[0]


def test_it_asks_for_the_specific_deployment():
    """A list query would return the chain's deployments, not this one's — and the caller
    is deciding whether to destroy something."""
    seen: list[str] = []
    import just_akash.chain as c

    orig = c._lcd_get
    try:
        c._lcd_get = lambda path, timeout=15, base=None: (  # type: ignore[assignment]
            seen.append(path),
            _info("x"),
        )[1]
        c.deployment_group_names(OWNER, DSEQ)
    finally:
        c._lcd_get = orig
    assert f"id.owner={OWNER}" in seen[0] and f"id.dseq={DSEQ}" in seen[0]


def test_every_group_name_is_returned(monkeypatch):
    monkeypatch.setattr(chain, "_lcd_get", lambda *a, **k: _info("just-akash-a", "just-akash-b"))
    assert chain.deployment_group_names(OWNER, DSEQ) == ["just-akash-a", "just-akash-b"]


def test_one_dead_endpoint_does_not_answer_for_the_chain(monkeypatch):
    """Failing over matters more here than for a balance read: an empty answer means
    'cannot prove ownership', and a caller that destroys things treats that as a refusal.
    One lagging LCD must not turn a real deployment into an unverifiable one."""
    calls = {"n": 0}

    def flaky(path, timeout=15, base=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("chain query failed: dead endpoint")
        return _info("just-akash-runner")

    monkeypatch.setattr(chain, "_lcd_get", flaky)
    assert chain.deployment_group_names(OWNER, DSEQ) == ["just-akash-runner"]
    assert calls["n"] >= 2, "must try the next endpoint"


def test_all_endpoints_failing_is_UNKNOWN_not_empty_ownership(monkeypatch):
    """[] means 'we could not read it', never 'it is not ours'. cleanup_stale keys a
    LEAVE on this, so conflating the two would destroy on a network failure."""

    def dead(path, timeout=15, base=None):
        raise RuntimeError("chain query failed")

    monkeypatch.setattr(chain, "_lcd_get", dead)
    assert chain.deployment_group_names(OWNER, DSEQ) == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"groups": None},
        {"groups": []},
        {"groups": [{}]},
        {"groups": [{"group_spec": {}}]},
        {"groups": [{"group_spec": {"name": ""}}]},
        {"groups": [{"group_spec": {"name": None}}]},
        {"groups": "not-a-list"},
    ],
)
def test_a_malformed_or_empty_response_is_unknown_not_a_crash(monkeypatch, payload):
    """This runs inside a sweep that decides what to destroy. An unexpected shape must
    degrade to 'cannot prove', never to an exception that aborts the audit or, worse, an
    empty string that compares unequal to our prefix and reads as 'someone else's'."""
    monkeypatch.setattr(chain, "_lcd_get", lambda *a, **k: payload)
    assert chain.deployment_group_names(OWNER, DSEQ) == []


def test_a_partially_readable_response_is_unknown_not_partial_ownership(monkeypatch):
    """Three groups, two readable, is not a weaker proof — it is a different deployment's
    proof. The caller decides whether to DESTROY on this, so an unnamed group makes the
    whole response unreadable rather than yielding the names that did parse."""
    payload = {
        "groups": [
            {"group_spec": {"name": "just-akash-runner"}},
            {"group_spec": {}},
        ]
    }
    monkeypatch.setattr(chain, "_lcd_get", lambda *a, **k: payload)
    assert chain.deployment_group_names(OWNER, DSEQ) == []


def test_a_partial_response_falls_through_to_a_healthier_endpoint(monkeypatch):
    """Rejecting the partial answer must not end the search — the next endpoint may
    simply be less lagged, and giving up would turn a readable deployment into an
    unverifiable one."""
    answers = [
        {"groups": [{"group_spec": {"name": "just-akash-runner"}}, {"group_spec": {}}]},
        {"groups": [{"group_spec": {"name": "just-akash-runner"}}]},
    ]
    calls = {"n": 0}

    def staged(path, timeout=15, base=None):
        calls["n"] += 1
        return answers[min(calls["n"] - 1, len(answers) - 1)]

    monkeypatch.setattr(chain, "_lcd_get", staged)
    assert chain.deployment_group_names(OWNER, DSEQ) == ["just-akash-runner"]
    assert calls["n"] >= 2


@pytest.mark.parametrize("owner,dseq", [("", DSEQ), (OWNER, ""), ("", "")])
def test_a_missing_identifier_never_reaches_the_network(monkeypatch, owner, dseq):
    """A blank owner would query the whole chain and return a stranger's group name as if
    it were this deployment's — the worst possible answer for a caller about to destroy."""

    def boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("queried the chain with an incomplete deployment id")

    monkeypatch.setattr(chain, "_lcd_get", boom)
    assert chain.deployment_group_names(owner, dseq) == []
