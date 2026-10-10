"""Offline actual journal, metadata and sole-wire boundaries; no GitHub calls."""

import copy
import io
import json
import time
import urllib.error
from dataclasses import replace
from email.message import Message
from types import SimpleNamespace
from typing import Any, cast

import pytest

from just_akash import jit_mint_receipt as mint
from just_akash import jit_registration_terminal as terminal
from just_akash.github_jit import JitHold
from tests.test_jit_mint_receipt import LABELS, NAME, POLICY


@pytest.fixture
def target(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path, end = private / "mint.json", private / "mint.terminal.json"
    pending = mint.prepare(
        path,
        policy=POLICY,
        operation_id="sentry-123-2-build",
        run_id="123",
        run_attempt="2",
        source_revision="a" * 40,
        runner_name=NAME,
        labels=LABELS,
        producer_revision="a" * 40,
    )
    mint.response_received(pending, 789, NAME)
    repo = {"id": POLICY.repository_id, "full_name": POLICY.repository_name, "private": True}
    run = {
        "id": 123,
        "run_attempt": 2,
        "workflow_id": 456,
        "head_sha": "a" * 40,
        "head_branch": "main",
        "path": ".github/workflows/sentry-owned-producer.yml",
        "status": "in_progress",
        "repository": repo,
        "head_repository": repo,
    }
    job = {
        "id": 555,
        "run_id": 123,
        "run_attempt": 2,
        "head_sha": "a" * 40,
        "runner_id": 789,
        "runner_name": NAME,
        "runner_group_id": POLICY.group_id,
        "status": "completed",
        "conclusion": "success",
        "completed_at": "2026-10-10T00:01:00Z",
    }
    runner = {
        "id": 789,
        "name": NAME,
        "os": "linux",
        "status": "offline",
        "busy": False,
        "ephemeral": True,
        "labels": [{"id": i, "name": name, "type": "custom"} for i, name in enumerate(LABELS, 1)],
    }
    group = {
        "id": POLICY.group_id,
        "visibility": "selected",
        "allows_public_repositories": False,
        "restricted_to_workflows": True,
        "selected_workflows": list(POLICY.workflows),
    }
    trace = []

    class GitHub:
        present = True
        delete_status = 204
        direct_status = None
        group_count = None
        hook = None

        def __call__(self, method, route, token, *, deadline):
            assert token == "synthetic-installation-token" and deadline > 0
            trace.append((method, route))
            if self.hook:
                self.hook(method, route)
            if method == "DELETE":
                assert route == POLICY.api_root + "/runners/789"
                intent = json.loads(end.read_bytes())
                assert intent["state"] == "UNKNOWN" and intent["runner_id"] == 789
                assert end.stat().st_mode & 0o777 == 0o600
                self.present = False
                return self.delete_status, None
            assert method == "GET"
            if "/actions/workflows/" in route:
                value = {"id": 456, "path": run["path"], "state": "active"}
            elif "/actions/jobs/" in route:
                value = job
            elif "/actions/runs/" in route:
                value = run
            elif route.endswith("/repositories?per_page=100&page=1"):
                value = {"total_count": 1, "repositories": [repo]}
            elif route.endswith("/repositories?per_page=100&page=2"):
                value = {"total_count": 1, "repositories": []}
            elif "/runners?per_page=100&page=" in route:
                count = int(self.present) if self.group_count is None else self.group_count
                value = {
                    "total_count": count,
                    "runners": [runner] if self.present and route.endswith("=1") else [],
                }
            elif route.endswith("/runners/789"):
                if self.direct_status is not None:
                    return self.direct_status, None
                return (200, copy.deepcopy(runner)) if self.present else (404, None)
            else:
                assert route == f"{POLICY.api_root}/runner-groups/{POLICY.group_id}"
                value = group
            return 200, copy.deepcopy(value)

    github = GitHub()

    def invoke(*, reconcile=False, **overrides):
        options: dict[str, Any] = dict(
            policy=POLICY,
            mint_path=path,
            job_id=555,
            installation_token="synthetic-installation-token",
            terminal_path=end,
            deadline=time.monotonic() + 60,
            _request=github,
        )
        options.update(overrides)
        function = (
            terminal.reconcile_completed_jit_registration
            if reconcile
            else terminal.retire_completed_jit_registration
        )
        return function(**options)

    return SimpleNamespace(
        invoke=invoke,
        github=github,
        path=path,
        end=end,
        run=run,
        job=job,
        runner=runner,
        group=group,
        repo=repo,
        trace=trace,
    )


def test_actual_wire_durable_before_delete_and_twice_absence_without_authority(target):
    result = target.invoke()
    assert result.runner_id == 789 and result.job_id == 555
    assert result.delete_acknowledged and result.absent_observed
    assert not any(
        (
            result.controller_authenticated,
            result.Akash_cleanup_authority,
            result.resource_absence_verified,
            result.publication_authority,
        )
    )
    assert sum(m == "DELETE" for m, _ in target.trace) == 1
    assert json.loads(target.end.read_bytes())["state"] == "UNKNOWN"
    before = list(target.trace)
    with pytest.raises(JitHold):
        target.invoke()
    assert target.trace == before
    reconciled = target.invoke(reconcile=True)
    assert reconciled.absent_observed and not reconciled.delete_acknowledged
    assert sum(m == "DELETE" for m, _ in target.trace) == 1


def test_ephemeral_already_absent_requires_full_context_and_never_delete(target):
    target.github.present = False
    result = target.invoke()
    assert result.absent_observed and not result.delete_acknowledged
    assert not target.end.exists() and not any(m == "DELETE" for m, _ in target.trace)
    assert sum(p.endswith("/runners/789") for _, p in target.trace) == 2


@pytest.mark.parametrize("status", [403, 429, 500, 200])
def test_zero_population_without_numeric404_is_not_absence(target, status):
    target.github.present = False
    target.github.direct_status = status
    with pytest.raises(JitHold):
        target.invoke()
    assert not target.end.exists() and not any(m == "DELETE" for m, _ in target.trace)


@pytest.mark.parametrize(
    "where,key,value",
    [
        ("run", "run_attempt", 3),
        ("run", "id", True),
        ("run", "head_sha", "b" * 40),
        ("run", "head_branch", "foreign"),
        ("run", "workflow_id", 999),
        ("run", "path", ".github/workflows/foreign.yml"),
        ("run", "status", "queued"),
        ("repo", "private", False),
        ("repo", "id", 1),
        ("repo", "full_name", "foreign/repo"),
        ("job", "runner_id", 790),
        ("job", "run_attempt", 1),
        ("job", "run_id", 999),
        ("job", "runner_group_id", 7),
        ("job", "runner_name", "dfci-foreign"),
        ("job", "head_sha", "c" * 40),
        ("job", "status", "in_progress"),
        ("job", "conclusion", "cancelled"),
        ("job", "conclusion", "timed_out"),
        ("job", "completed_at", None),
        ("runner", "busy", True),
        ("runner", "id", 999),
        ("runner", "ephemeral", False),
        ("runner", "os", "windows"),
        ("group", "allows_public_repositories", True),
    ],
)
def test_real_metadata_mismatch_or_busy_never_installs_terminal_intent(target, where, key, value):
    getattr(target, where)[key] = value
    with pytest.raises(JitHold):
        target.invoke()
    assert not target.end.exists() and not any(m == "DELETE" for m, _ in target.trace)


@pytest.mark.parametrize("count", [True, -1, 2, 100])
def test_foreign_or_incomplete_group_population_holds(target, count):
    target.github.group_count = count
    with pytest.raises(JitHold):
        target.invoke()
    assert not target.end.exists() and not any(m == "DELETE" for m, _ in target.trace)


def test_lost_delete_ack_retained_and_readonly_reconciles_without_replay(target):
    original = target.github

    def request(method, path, token, *, deadline):
        result = original(method, path, token, deadline=deadline)
        if method == "DELETE":
            raise TimeoutError("synthetic-private-error")
        return result

    result = target.invoke(_request=request)
    assert result.absent_observed and not result.delete_acknowledged
    assert target.end.exists()
    with pytest.raises(JitHold):
        target.invoke()
    assert target.invoke(reconcile=True).absent_observed
    assert sum(m == "DELETE" for m, _ in target.trace) == 1


def test_lost_delete_ack_without_removal_preserves_unknown_and_never_retries(target):
    original = target.github

    def request(method, path, token, *, deadline):
        if method == "DELETE":
            assert target.end.exists()
            target.trace.append((method, path))
            raise TimeoutError("secret")
        return original(method, path, token, deadline=deadline)

    result = target.invoke(_request=request)
    assert not result.absent_observed and not result.delete_acknowledged
    with pytest.raises(JitHold):
        target.invoke()
    assert not target.invoke(reconcile=True).absent_observed
    assert sum(m == "DELETE" for m, _ in target.trace) == 1


def test_lost_delete_ack_logs_only_fixed_warning_and_keeps_single_wire(target, caplog):
    original = target.github
    sensitive = "synthetic-installation-token private-body signed-url exception-detail"

    def request(method, path, token, *, deadline):
        result = original(method, path, token, deadline=deadline)
        if method == "DELETE":
            raise TimeoutError(sensitive)
        return result

    result = target.invoke(_request=request)
    assert result.absent_observed and not result.delete_acknowledged
    assert json.loads(target.end.read_bytes())["state"] == "UNKNOWN"
    assert caplog.record_tuples == [
        (
            "just_akash.jit_registration_terminal",
            30,
            "JIT registration DELETE acknowledgement unverified; "
            "retain UNKNOWN and reconcile read-only",
        )
    ]
    assert all(record.exc_info is None and record.stack_info is None for record in caplog.records)
    assert sensitive not in caplog.text
    with pytest.raises(JitHold):
        target.invoke()
    assert target.invoke(reconcile=True).absent_observed
    assert sum(method == "DELETE" for method, _ in target.trace) == 1


def test_intent_fsync_lost_ack_never_sends_delete(target, monkeypatch):
    original = terminal._create_durable

    def persist(path, raw):
        original(path, raw)
        raise OSError("private-fsync-error")

    monkeypatch.setattr(terminal, "_create_durable", persist)
    with pytest.raises(JitHold) as held:
        target.invoke()
    assert "private-fsync" not in str(held.value)
    assert target.end.exists() and not any(m == "DELETE" for m, _ in target.trace)


def test_final_deadline_expiry_after_durable_intent_never_sends_delete(target, monkeypatch):
    original = terminal._create_durable
    clock = [100.0]
    monkeypatch.setattr(terminal, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def persist(path, raw):
        original(path, raw)
        clock[0] = 161

    monkeypatch.setattr(terminal, "_create_durable", persist)
    with pytest.raises(JitHold):
        target.invoke(deadline=160)
    assert target.end.exists() and not any(m == "DELETE" for m, _ in target.trace)


def test_rerun_or_busy_transition_after_intent_holds_before_wire(target):
    def change(method, path):
        if target.end.exists():
            target.run["run_attempt"] = 3

    target.github.hook = change
    with pytest.raises(JitHold):
        target.invoke()
    assert target.end.exists() and not any(m == "DELETE" for m, _ in target.trace)


def test_identical_byte_candidate_replacement_during_reads_holds_before_wire(target):
    def replace_candidate(method, path):
        candidate = target.path.with_suffix(".response.json")
        data = candidate.read_bytes()
        candidate.unlink()
        candidate.write_bytes(data)
        candidate.chmod(0o600)
        target.github.hook = None

    target.github.hook = replace_candidate
    with pytest.raises(JitHold):
        target.invoke()
    assert not target.end.exists() and not any(m == "DELETE" for m, _ in target.trace)


@pytest.mark.parametrize(
    "bad", ["candidate", "source", "policy", "falsefact", "public", "symlink"]
)
def test_bad_journal_has_no_metadata_or_mutation_requests(target, bad):
    candidate = target.path.with_suffix(".response.json")
    if bad == "candidate":
        value = json.loads(candidate.read_bytes())
        value["intent_sha256"] = "0" * 64
        candidate.write_bytes(terminal._canonical_bytes(value))
    elif bad == "public":
        candidate.chmod(0o644)
    elif bad == "symlink":
        real = candidate.with_name("other")
        candidate.rename(real)
        candidate.symlink_to(real)
    else:
        value = json.loads(target.path.read_bytes())
        if bad == "source":
            value["controller_claim"]["source_revision"] = "c" * 40
        elif bad == "policy":
            value["policy"]["group_id"] = True
        else:
            value["publication_authority"] = True
        target.path.write_bytes(terminal._canonical_bytes(value))
    with pytest.raises(JitHold):
        target.invoke()
    assert target.trace == [] and not target.end.exists()


def test_reusable_policy_cannot_be_promoted_to_completed_source_authority(target):
    reusable = replace(
        POLICY,
        non_reusable_workflow=False,
        source_workflow_revision=None,
        workflows=(POLICY.repository_name + "/.github/workflows/sentry.yml@" + "a" * 40,),
    )
    with pytest.raises(JitHold):
        target.invoke(policy=reusable)
    assert target.trace == []


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/orgs/Borduas-Holdings/actions/runners/789"),
        ("DELETE", "/orgs/Borduas-Holdings/actions/runners/790"),
        ("GET", "/repos/foreign/unknown/actions/runs/123"),
    ],
)
def test_private_route_allowlist_refuses_before_transport(target, method, path):
    observer = terminal._Terminal(
        POLICY,
        target.path,
        555,
        "synthetic-installation-token",
        target.end,
        time.monotonic() + 60,
        target.github,
    )
    with pytest.raises(JitHold):
        observer.call(method, path)
    assert target.trace == []


def test_real_transport_delete204_is_empty_fixed_origin_no_proxy_no_redirect(monkeypatch):
    seen = []

    class Response(io.BytesIO):
        status = 204

        def geturl(self):
            return "https://api.github.com/orgs/Borduas-Holdings/actions/runners/789"

    class Opener:
        def open(self, req, timeout):
            seen.append((req, timeout))
            return Response(b"")

    def build(*handlers):
        assert isinstance(handlers[0], terminal.urllib.request.ProxyHandler)
        assert cast(Any, handlers[0]).proxies == {}
        assert isinstance(handlers[1], terminal._NoRedirect)
        return Opener()

    monkeypatch.setattr(terminal.urllib.request, "build_opener", build)
    assert terminal._http(
        "DELETE",
        "/orgs/Borduas-Holdings/actions/runners/789",
        "secret",
        deadline=time.monotonic() + 5,
    ) == (204, None)
    assert seen[0][0].data is None and seen[0][0].method == "DELETE"
    assert 0 < seen[0][1] <= 5


@pytest.mark.parametrize("code", [403, 404, 422, 429, 500])
def test_transport_error_bodies_are_never_read_or_echoed(monkeypatch, code):
    class Body(io.BytesIO):
        def read(self, *args):
            raise AssertionError("error body must not be read")

    class Opener:
        def open(self, req, timeout):
            raise urllib.error.HTTPError(
                req.full_url, code, "private-error", Message(), Body(b"secret")
            )

    monkeypatch.setattr(terminal.urllib.request, "build_opener", lambda *args: Opener())
    assert terminal._http(
        "GET",
        "/orgs/Borduas-Holdings/actions/runners/789",
        "secret",
        deadline=time.monotonic() + 5,
    ) == (code, None)


@pytest.mark.parametrize(
    "method,route",
    [
        ("POST", "/orgs/Borduas-Holdings/actions/runners/789"),
        ("DELETE", "/orgs/foreign/actions/runners/789"),
        ("DELETE", "/orgs/Borduas-Holdings/actions/runners/0"),
        ("DELETE", "/orgs/Borduas-Holdings/actions/runners/789?force=1"),
        ("GET", "/repos/Borduas-Holdings/foreign/actions/jobs/555"),
        ("GET", "/orgs/Borduas-Holdings/actions/../runners/789"),
    ],
)
def test_transport_route_rejection_precedes_any_opener(monkeypatch, method, route):
    def forbidden(*args):
        pytest.fail("closed route must not construct transport")

    monkeypatch.setattr(terminal.urllib.request, "build_opener", forbidden)
    with pytest.raises(JitHold):
        terminal._http(method, route, "secret", deadline=time.monotonic() + 5)


@pytest.mark.parametrize("bad", ["redirect", "oversized", "duplicate", "body204", "read-deadline"])
def test_actual_transport_body_identity_and_inherited_deadline_hold(monkeypatch, bad):
    clock = [100.0]
    route = "/orgs/Borduas-Holdings/actions/runners/789"
    monkeypatch.setattr(terminal, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    class Response(io.BytesIO):
        status = 204 if bad == "body204" else 200

        def geturl(self):
            return "https://foreign.invalid" if bad == "redirect" else terminal.API + route

        def read(self, count=-1):
            if bad == "read-deadline":
                clock[0] = 106
            return super().read(count)

    raw = (
        b"x" * (terminal.MAX_BYTES + 1)
        if bad == "oversized"
        else b'{"id":1,"id":2}'
        if bad == "duplicate"
        else b'{"id":789}'
    )

    class Opener:
        def open(self, req, timeout):
            return Response(raw)

    monkeypatch.setattr(terminal.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(JitHold) as held:
        terminal._http(
            "DELETE" if bad == "body204" else "GET", route, "synthetic-secret", deadline=105
        )
    assert "synthetic-secret" not in str(held.value)


def test_opener_construction_delay_cannot_start_request_after_inherited_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(terminal, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    class Opener:
        def open(self, *args, **kwargs):
            pytest.fail("expired request must not start")

    def build(*args):
        clock[0] = 106
        return Opener()

    monkeypatch.setattr(terminal.urllib.request, "build_opener", build)
    with pytest.raises(JitHold):
        terminal._http(
            "DELETE", "/orgs/Borduas-Holdings/actions/runners/789", "secret", deadline=105
        )


def test_group_page_one_renewal_and_numeric404_cannot_be_used_independently(target):
    target.github.direct_status = 404
    with pytest.raises(JitHold):
        target.invoke()
    assert not target.end.exists() and not any(m == "DELETE" for m, _ in target.trace)


def test_page_one_changes_during_terminal_pagination_holds(target):
    seen = [0]

    def change(method, route):
        if route.endswith("/runners?per_page=100&page=1"):
            seen[0] += 1
            if seen[0] == 2:
                target.github.present = False

    target.github.hook = change
    with pytest.raises(JitHold):
        target.invoke()
    assert not target.end.exists() and not any(m == "DELETE" for m, _ in target.trace)


def test_busy_change_after_durable_intent_does_not_delete(target):
    def change(method, route):
        if target.end.exists():
            target.runner["busy"] = True

    target.github.hook = change
    with pytest.raises(JitHold):
        target.invoke()
    assert target.end.exists() and not any(m == "DELETE" for m, _ in target.trace)


def test_readonly_reconciliation_refuses_tampered_terminal_identity(target):
    target.invoke()
    before = target.end.read_bytes()

    def change(method, route):
        target.end.unlink()
        target.end.write_bytes(before)
        target.end.chmod(0o600)
        target.github.hook = None

    target.github.hook = change
    with pytest.raises(JitHold):
        target.invoke(reconcile=True)
    assert target.end.read_bytes() == before
    assert sum(m == "DELETE" for m, _ in target.trace) == 1


def test_existing_terminal_even_dangling_symlink_blocks_before_metadata(target):
    target.end.symlink_to(target.end.with_name("missing"))
    with pytest.raises(JitHold):
        target.invoke()
    assert target.trace == [] and target.end.is_symlink()


def test_candidate_fifo_refuses_without_open_or_metadata(target):
    import os

    candidate = target.path.with_suffix(".response.json")
    candidate.unlink()
    os.mkfifo(candidate, mode=0o600)
    with pytest.raises(JitHold):
        target.invoke()
    assert target.trace == []


def test_alternate_terminal_filename_cannot_bypass_same_mint_slot(target):
    with pytest.raises(JitHold):
        target.invoke(terminal_path=target.end.with_name("alternate.json"))
    assert target.trace == [] and not target.end.exists()
