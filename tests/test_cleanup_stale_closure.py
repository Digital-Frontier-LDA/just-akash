"""Real cleanup entry point, mocked Console DELETE, fresh chain HTTP closure proof."""

from __future__ import annotations

import io
import json
import socket
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from just_akash import cleanup_stale as cs
from just_akash.api import AkashConsoleAPI
from just_akash.workload_identity import Identity, format_identity

OWNER = "akash1" + "a" * 38
PREFIX = "just-akash-"
REPO = "Digital-Frontier-LDA/just-akash"
REGISTER = {PREFIX: REPO}
NOW = 2_000_000_000
DSEQ = str((NOW - 7200) * 1000)
ENDPOINTS = ["https://one.test", "https://two.test"]


@pytest.fixture
def lifecycle(monkeypatch):
    # Unstubbed transports are forbidden: no test can reach a live resource.
    def deny_network(*args, **kwargs):
        raise AssertionError("unexpected network connection")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setenv("AKASH_API_KEY", "offline-key")
    monkeypatch.delenv("AKASH_API_KEYS", raising=False)
    monkeypatch.delenv("AKASH_WALLETS_EXPECTED", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(cs.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cs.time, "time", lambda: NOW)
    monkeypatch.setattr(cs, "_credit_line", lambda *args: "offline")
    monkeypatch.setattr(cs.chain, "rest_urls", lambda: ENDPOINTS)
    monkeypatch.setattr(cs.chain, "list_active_deployments", lambda owner: [{"dseq": DSEQ}])
    name = format_identity(Identity(PREFIX, REPO, "ci-payload", 1, run=99, attempt=2), REGISTER)
    monkeypatch.setattr(cs.chain, "deployment_group_names", lambda *args: [name])
    monkeypatch.setattr(
        cs.cleanup_identity,
        "completed_run",
        lambda *args: {
            "id": 99,
            "run_attempt": 2,
            "status": "completed",
            "repository": {"full_name": REPO},
        },
    )
    before = {
        "deployment": {"id": {"owner": OWNER, "dseq": DSEQ}, "state": "active"},
        "groups": [
            {"id": {"owner": OWNER, "dseq": DSEQ, "gseq": 1}, "group_spec": {"name": name}}
        ],
    }
    samples = json.loads((Path(__file__).parent / "fixtures/closure_wire.json").read_text())
    # Preserve recorded wire fields and states; only adjust the synthetic deployment ID
    # so the real stale-age classifier includes it.
    samples = json.loads(
        json.dumps(samples)
        .replace('"dseq": "7"', f'"dseq": "{DSEQ}"')
        .replace(f"{OWNER}/7", f"{OWNER}/{DSEQ}")
    )
    state: dict = {"deleted": False, "sample": "closed", "events": [], "samples": samples}
    client = AkashConsoleAPI("offline-key")
    monkeypatch.setattr(client, "account_address", lambda: OWNER)

    def console_request(method, path, *args, **kwargs):
        assert path == f"/v1/deployments/{DSEQ}"
        state["events"].append((method, path))
        if method == "GET":
            return {"data": {"leases": [{"status": {"services": {"probe": {}}}}]}}
        assert method == "DELETE"
        state["deleted"] = True
        return {"data": {"accepted": True}}

    monkeypatch.setattr(client, "_request", console_request)
    monkeypatch.setattr(cs, "AkashConsoleAPI", lambda key: client)

    def urlopen(request, timeout=15):
        parsed = urlsplit(request.full_url)
        assert parsed.hostname in {"one.test", "two.test"}
        assert parsed.scheme == "https"
        assert request.get_header("User-agent") == "just-akash-balance/1.0"
        phase = "after" if state["deleted"] else "before"
        state["events"].append((phase, request.full_url))
        query = parse_qs(parsed.query)
        sample = samples[state["sample"]]
        if parsed.path.endswith("/deployments/info"):
            assert query == {"id.owner": [OWNER], "id.dseq": [DSEQ]}
            doc = sample["deployment"] if state["deleted"] else before
        else:
            assert parsed.path.endswith("/leases/list")
            assert state["deleted"], "closure lease read must follow DELETE"
            assert query["filters.owner"] == [OWNER]
            assert query["filters.dseq"] == [DSEQ]
            doc = sample["leases"]
        return io.BytesIO(json.dumps(doc).encode())

    monkeypatch.setattr(cs.chain.urllib.request, "urlopen", urlopen)
    return state


def _main():
    return cs.main(
        [
            "--execute",
            "--placement-prefix",
            PREFIX,
            "--ownership-register",
            json.dumps(REGISTER),
        ]
    )


@pytest.mark.parametrize("sample,expected", [("open_escrow", 1), ("closed", 0)])
def test_main_counts_only_chain_verified_closure(lifecycle, capsys, sample, expected):
    lifecycle["sample"] = sample
    assert _main() == expected
    out = capsys.readouterr().out
    assert f"closed={1 if expected == 0 else 0} failed=0 unverified={expected}" in out
    events = lifecycle["events"]
    deletes = [i for i, (kind, _) in enumerate(events) if kind == "DELETE"]
    assert len(deletes) == 1
    assert len([event for event in events[: deletes[0]] if event[0] == "before"]) == 4
    after = [url for phase, url in events[deletes[0] + 1 :] if phase == "after"]
    assert len(after) >= 3
    assert any("/leases/list?" in url for url in after)
    assert any("/deployments/info?" in url for url in after)
    if expected == 0:
        for endpoint in ENDPOINTS:
            assert any(url.startswith(endpoint) and "/deployments/info?" in url for url in after)


def test_missing_post_delete_evidence_is_unverified(lifecycle, capsys):
    lifecycle["samples"]["closed"]["deployment"] = {}
    assert _main() == 1
    assert "closed=0 failed=0 unverified=1" in capsys.readouterr().out


def test_typed_verdict_is_required(lifecycle, monkeypatch, capsys):
    monkeypatch.setattr(cs._lease_verification, "verdict", lambda *a, **k: {"closed": "true"})
    assert _main() == 1
    assert "closed=0 failed=0 unverified=1" in capsys.readouterr().out
    assert len([event for event in lifecycle["events"] if event[0] == "DELETE"]) == 1
