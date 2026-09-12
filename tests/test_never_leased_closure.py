"""Actual CLI/shared-verifier effects for never-leased deployment closure."""

import inspect
import json
import socket
import sys
import types
import urllib.request

import pytest

from just_akash import _lease_verification as verifier
from just_akash import cli

OWNER = "akash1" + "a" * 38
DSEQ = "42"


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network forbidden in never-leased tests")

    monkeypatch.setattr(urllib.request, "urlopen", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def deployment():
    return {
        "deployment": {"id": {"owner": OWNER, "dseq": DSEQ}, "state": "closed"},
        "escrow_account": {
            "id": {"scope": "deployment", "xid": f"{OWNER}/{DSEQ}"},
            "state": {"owner": OWNER, "state": "closed"},
        },
    }


def lease(state="closed"):
    return {
        "lease": {
            "id": {
                "owner": OWNER,
                "dseq": DSEQ,
                "gseq": 1,
                "oseq": 1,
                "bseq": 0,
                "provider": "provider",
            },
            "state": state,
        }
    }


def read_fixture(case, calls):
    def get(url):
        calls.append(url)
        second = url.startswith(verifier.DEFAULT_ENDPOINTS[1])
        if "/deployments/info?" in url:
            doc = deployment()
            if case == "active_deployment":
                doc["deployment"]["state"] = "active"
            elif case == "open_escrow":
                doc["escrow_account"]["state"]["state"] = "open"
            elif case == "wrong_owner":
                doc["deployment"]["id"]["owner"] = "wrong"
            elif case == "wrong_dseq":
                doc["deployment"]["id"]["dseq"] = "43"
            elif case == "wrong_escrow":
                doc["escrow_account"]["id"]["xid"] = "wrong/42"
            elif case == "second_open" and second:
                doc["escrow_account"]["state"]["state"] = "open"
            elif case == "missing_deployment":
                return None
            return doc
        assert "/leases/list?" in url
        doc: dict = {"leases": [], "pagination": {"next_key": None, "total": "0"}}
        if case == "unreadable":
            raise OSError("offline failure")
        if case == "missing_population":
            return {"pagination": {"next_key": None}}
        if case == "missing_pagination":
            return {"leases": []}
        if case == "missing_cursor":
            doc["pagination"] = {"total": "0"}
        if case == "positive_total":
            doc["pagination"]["total"] = "1"
        if case == "invalid_total":
            doc["pagination"] = {"next_key": None, "total": False}
        if case == "invalid_cursor":
            doc["pagination"] = {"next_key": [], "total": "0"}
        if case == "cursor_loop":
            doc["pagination"]["next_key"] = "again"
        if case == "page_limit":
            doc["pagination"]["next_key"] = str(len(calls))
        if case == "empty_nonempty" and second:
            doc["leases"] = [lease()]
        if case == "active_lease":
            doc["leases"] = [lease("active")]
        if case == "malformed_row":
            doc["leases"] = [None]
        if case == "later_active":
            if "pagination.key=" in url:
                doc["leases"] = [lease("active")]
            else:
                doc["pagination"]["next_key"] = "second"
        return doc

    return get


def actual_cli(monkeypatch, capsys, case):
    calls = []
    get = read_fixture(case, calls)

    class Response:
        def __init__(self, doc):
            self.doc = doc

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self.doc).encode()

    def opener(request, **kwargs):
        assert request.get_method() == "GET"
        assert request.full_url.startswith(verifier.DEFAULT_ENDPOINTS)
        return Response(get(request.full_url))

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "just-akash",
            "verify-closed",
            "--dseq",
            DSEQ,
            "--owner",
            OWNER,
            "--retries",
            "1",
            "--json",
        ],
    )
    try:
        cli.main()
        rc = 0
    except SystemExit as exc:
        rc = exc.code
    return rc, json.loads(capsys.readouterr().out), calls


def test_complete_empty_history_with_closed_deployment_and_escrow_passes_cli(monkeypatch, capsys):
    rc, proof, calls = actual_cli(monkeypatch, capsys, "closed")
    assert rc == 0 and proof["closed"] is True
    assert len(calls) == 4
    assert sum("/deployments/info?" in url for url in calls) == 2


@pytest.mark.parametrize(
    "case",
    [
        "active_deployment",
        "open_escrow",
        "wrong_owner",
        "wrong_dseq",
        "wrong_escrow",
        "second_open",
        "missing_deployment",
        "unreadable",
        "missing_population",
        "missing_pagination",
        "missing_cursor",
        "positive_total",
        "invalid_total",
        "invalid_cursor",
        "cursor_loop",
        "page_limit",
        "empty_nonempty",
        "active_lease",
        "malformed_row",
        "later_active",
    ],
)
def test_empty_history_does_not_bypass_any_proof(monkeypatch, capsys, case):
    rc, proof, calls = actual_cli(monkeypatch, capsys, case)
    assert rc == 1 and proof["closed"] is False
    assert calls


def test_zero_total_with_continuation_is_rejected_before_following_cursor(monkeypatch, capsys):
    rc, proof, calls = actual_cli(monkeypatch, capsys, "cursor_loop")
    assert rc == 1 and proof["closed"] is False
    assert len(calls) == 2  # one contradictory lease read per independent source


def test_exact_deployment_proof_omission_changes_actual_cli_effect(monkeypatch, capsys):
    rc, proof, _ = actual_cli(monkeypatch, capsys, "open_escrow")
    assert rc == 1 and proof["closed"] is False
    source = inspect.getsource(verifier.verdict)
    needle = "deployment_closed(base, dseq, owner, get)"
    assert source.count(needle) == 1
    namespace = dict(vars(verifier))
    exec(source.replace(needle, "True", 1), namespace)
    mutant = namespace["verdict"]
    function = types.FunctionType(mutant.__code__, verifier.__dict__, argdefs=mutant.__defaults__)
    function.__kwdefaults__ = mutant.__kwdefaults__
    monkeypatch.setattr(verifier, "verdict", function)
    rc, proof, calls = actual_cli(monkeypatch, capsys, "open_escrow")
    assert rc == 0 and proof["closed"] is True
    assert len(calls) == 2  # exact guard omission removed both deployment reads


def test_empty_history_still_requires_two_independent_sources():
    calls = []
    proof = verifier.verdict(
        DSEQ,
        OWNER,
        ["https://same.example", "https://same.example:443"],
        read_fixture("closed", calls),
        retries=1,
    )
    assert proof["closed"] is False
    assert len(calls) == 1
