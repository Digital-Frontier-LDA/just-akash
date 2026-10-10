"""Dormant exact completed-job registration cleanup for a trusted controller.

Private files and returned facts do NOT authenticate that controller. It must
already own installation-token acquisition and the protected producer lifecycle.
No CLI or caller activates this primitive. Cancelled/live/unused-slot recovery,
Akash closure and the whole600/cleanup10 route remain separate and unqualified.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .deployment_receipt import _canonical_bytes, _create_durable, _private_parent
from .github_jit import API, JitHandoff, JitHold, JitPolicy, _json, verify_group_policy
from .jit_slot_observation import _runner

MAX_BYTES = 1024 * 1024


def _require(value: object) -> None:
    if not value:
        raise JitHold("exact completed JIT registration is unverified")


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http(method: str, path: str, token: str, *, deadline: float) -> tuple[int, Any]:
    """Private fixed-origin transport; the caller additionally closes routes."""
    remaining = deadline - time.monotonic()
    _require(remaining > 0 and method in {"GET", "DELETE"})
    _require(isinstance(token, str) and token and not re.search(r"[\r\n]", token))
    org = r"(?:Digital-Frontier-LDA|Borduas-Holdings)"
    direct = rf"/orgs/{org}/actions/runners/[1-9][0-9]*"
    groups = (
        rf"/orgs/{org}/actions/runner-groups/[1-9][0-9]*"
        r"(?:/(?:repositories|runners)\?per_page=100&page=[12])?"
    )
    repository = (
        r"(?:Borduas-Holdings/(?:blazing|Blazing-Back)|"
        r"Digital-Frontier-LDA/[A-Za-z0-9_-][A-Za-z0-9_.-]*)"
    )
    runs = (
        rf"/repos/{repository}/actions/(?:jobs/[1-9][0-9]*|"
        r"runs/[1-9][0-9]*(?:/attempts/[1-9][0-9]*)?|workflows/[A-Za-z0-9_-]+\.ya?ml)"
    )
    _require(isinstance(path, str) and ".." not in path)
    _require(re.fullmatch(direct if method == "DELETE" else f"(?:{direct}|{groups}|{runs})", path))
    req = urllib.request.Request(  # noqa: S310 — fixed HTTPS origin and closed paths
        API + path,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "just-akash-exact-jit-terminal",
        },
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        remaining = deadline - time.monotonic()
        _require(remaining > 0)
        with opener.open(req, timeout=min(20, remaining)) as response:
            _require(response.geturl() == API + path)
            raw = response.read(MAX_BYTES + 1)
            _require(len(raw) <= MAX_BYTES and time.monotonic() < deadline)
            status = response.status
            if method == "DELETE":
                _require(status == 204 and not raw)
                return status, None
            _require(status == 200)
            return status, _json(raw)
    except urllib.error.HTTPError as error:
        # Never retain/parse error bodies or echo credentials and signed URLs.
        code = error.code
        error.close()
        _require(time.monotonic() < deadline)
        return code, None
    except Exception:
        raise JitHold("JIT terminal transport unavailable") from None


def _owned(path: Path) -> tuple[bytes, tuple[int, ...]]:
    _private_parent(path)
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:

        def identity(s):
            return (
                s.st_dev,
                s.st_ino,
                s.st_uid,
                s.st_gid,
                s.st_mode,
                s.st_nlink,
                s.st_size,
                s.st_mtime_ns,
                s.st_ctime_ns,
            )

        opened = os.fstat(fd)
        _require(identity(before) == identity(opened))
        _require(opened.st_uid == os.getuid() and stat.S_IMODE(opened.st_mode) == 0o600)
        _require(0 < opened.st_size <= 65536)
        raw = os.read(fd, 65537)
        _require(len(raw) == opened.st_size)
        _require(identity(opened) == identity(os.fstat(fd)) == identity(path.lstat()))
        return raw, identity(opened)
    finally:
        os.close(fd)


def _decode(raw: bytes, keys: set[str]) -> dict:
    value = _json(raw)
    _require(isinstance(value, dict) and set(value) == keys)
    _require(_canonical_bytes(value) == raw)
    return value


@dataclass(frozen=True)
class TerminalObservation:
    """Fresh exact-ID/group observations, not controller or cleanup authority."""

    runner_id: int
    job_id: int
    mint_intent_sha256: str
    delete_acknowledged: bool
    absent_observed: bool
    requests: int
    controller_authenticated: bool = False
    Akash_cleanup_authority: bool = False
    resource_absence_verified: bool = False
    publication_authority: bool = False


class _Terminal:
    def __init__(self, policy, mint_path, job_id, token, terminal_path, deadline, request):
        _require(type(policy) is JitPolicy and policy.non_reusable_workflow is True)
        _require(_positive(job_id))
        now = time.monotonic()
        _require(type(deadline) in (float, int) and math.isfinite(deadline))
        _require(now < deadline <= now + 600)
        _require(isinstance(token, str) and token and not re.search(r"[\r\n]", token))
        self.policy, self.job_id, self.token = policy, job_id, token
        self.deadline, self.request, self.requests = deadline, request, 0
        self.mint_path, self.terminal_path = Path(mint_path), Path(terminal_path)
        _require(self.terminal_path == self.mint_path.with_suffix(".terminal.json"))
        self.response_path = self.mint_path.with_suffix(".response.json")
        _require(".." not in self.terminal_path.parts)
        _private_parent(self.terminal_path)
        _require(self.terminal_path not in {self.mint_path, self.response_path})
        self.mint_raw, self.mint_identity = _owned(self.mint_path)
        self.response_raw, self.response_identity = _owned(self.response_path)
        mint = _decode(
            self.mint_raw,
            {
                "schema",
                "state",
                "operation_id",
                "controller_claim",
                "policy",
                "producer_workflow_revision",
                "request_path",
                "runner_name",
                "labels",
                "work_folder",
                "registration_verified",
                "delivery_verified",
                "chain_allocation_verified",
                "publication_authority",
            },
        )
        candidate = _decode(
            self.response_raw,
            {
                "schema",
                "state",
                "intent_sha256",
                "runner_id",
                "runner_name",
                "registration_verified",
                "delivery_verified",
                "chain_allocation_verified",
                "publication_authority",
            },
        )
        for row in (mint, candidate):
            _require(
                all(
                    row[k] is False
                    for k in (
                        "registration_verified",
                        "delivery_verified",
                        "chain_allocation_verified",
                        "publication_authority",
                    )
                )
            )
        _require(mint["schema"] == "just-akash/jit-mint-attempt/v1" and mint["state"] == "UNKNOWN")
        _require(candidate["schema"] == "just-akash/jit-mint-response/v1")
        _require(candidate["state"] == "RESPONSE_CANDIDATE")
        self.mint_sha = hashlib.sha256(self.mint_raw).hexdigest()
        _require(candidate["intent_sha256"] == self.mint_sha)
        _require(_positive(candidate["runner_id"]))
        _require(candidate["runner_name"] == mint["runner_name"])
        _require(
            isinstance(mint["runner_name"], str)
            and re.fullmatch(r"dfci-[a-z0-9-]{1,90}", mint["runner_name"])
        )
        _require(
            isinstance(mint["operation_id"], str)
            and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", mint["operation_id"])
        )
        claim = mint["controller_claim"]
        _require(
            isinstance(claim, dict)
            and set(claim) == {"run_id", "run_attempt", "source_revision", "authenticated"}
        )
        _require(claim["authenticated"] is False)
        for key in ("run_id", "run_attempt"):
            _require(isinstance(claim[key], str) and re.fullmatch(r"[1-9][0-9]{0,19}", claim[key]))
            _require(int(claim[key]) < 2**64)
        _require(claim["source_revision"] == policy.source_workflow_revision)
        _require(mint["producer_workflow_revision"] == policy.source_workflow_revision)
        expected_policy = {
            "revision": policy.revision,
            "repository_id": policy.repository_id,
            "repository_name": policy.repository_name,
            "group_id": policy.group_id,
            "workflows": list(policy.workflows),
            "non_reusable_workflow": True,
            "source_workflow_revision": policy.source_workflow_revision,
            "source_workflow_branch": policy.source_workflow_branch,
        }
        _require(_canonical_bytes(mint["policy"]) == _canonical_bytes(expected_policy))
        _require(mint["request_path"] == policy.api_root + "/runners/generate-jitconfig")
        _require(mint["work_folder"] == "_work")
        labels = mint["labels"]
        _require(isinstance(labels, list) and 1 <= len(labels) <= 100)
        _require(
            all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", v) for v in labels)
        )
        _require(
            len({v.casefold() for v in labels}) == len(labels) and mint["runner_name"] in labels
        )
        self.handoff = JitHandoff(
            candidate["runner_id"],
            candidate["runner_name"],
            policy.group_id,
            policy.revision,
            tuple(labels),
            "",
        )
        self.run_id, self.attempt = int(claim["run_id"]), int(claim["run_attempt"])
        self.repo = "/repos/" + policy.repository_name
        self.run_path = f"{self.repo}/actions/runs/{self.run_id}"
        self.job_path = f"{self.repo}/actions/jobs/{job_id}"
        self.workflow_path = (
            policy.workflows[0].split("/.github/workflows/", 1)[1].split("@", 1)[0]
        )
        self.workflow_api = f"{self.repo}/actions/workflows/{self.workflow_path}"
        self.group_path = f"{policy.api_root}/runner-groups/{policy.group_id}"
        self.runner_path = f"{policy.api_root}/runners/{self.handoff.runner_id}"
        self.get_paths = {
            self.run_path,
            self.run_path + f"/attempts/{self.attempt}",
            self.job_path,
            self.workflow_api,
            self.group_path,
            self.runner_path,
            *(self.group_path + f"/repositories?per_page=100&page={p}" for p in (1, 2)),
            *(self.group_path + f"/runners?per_page=100&page={p}" for p in (1, 2)),
        }

    def call(self, method, path):
        _require(time.monotonic() < self.deadline)
        _require(
            (method == "GET" and path in self.get_paths)
            or (method == "DELETE" and path == self.runner_path)
        )
        self.requests += 1
        status, body = self.request(method, path, self.token, deadline=self.deadline)
        _require(type(status) is int and time.monotonic() < self.deadline)
        return status, body

    def get(self, method, path, token, body=None) -> dict:
        _require(method == "GET" and token == self.token and body is None)
        status, result = self.call(method, path)
        _require(status == 200 and isinstance(result, dict))
        return cast(dict, result)

    def renew_files(self):
        _require(_owned(self.mint_path) == (self.mint_raw, self.mint_identity))
        _require(_owned(self.response_path) == (self.response_raw, self.response_identity))
        _require(time.monotonic() < self.deadline)

    def completed(self):
        workflow = self.get("GET", self.workflow_api, self.token)
        _require(
            _positive(workflow.get("id"))
            and workflow.get("path") == ".github/workflows/" + self.workflow_path
        )
        _require(workflow.get("state") == "active")
        for path in (self.run_path, self.run_path + f"/attempts/{self.attempt}"):
            run = self.get("GET", path, self.token)
            for field, expected in (
                ("id", self.run_id),
                ("run_attempt", self.attempt),
                ("workflow_id", workflow["id"]),
            ):
                _require(type(run.get(field)) is int and run[field] == expected)
            _require(run.get("head_sha") == self.policy.source_workflow_revision)
            _require(run.get("head_branch") == self.policy.source_workflow_branch)
            _require(run.get("path") == workflow["path"])
            _require(run.get("status") in {"in_progress", "completed"})
            for key in ("repository", "head_repository"):
                repository = cast(dict, run.get(key))
                _require(isinstance(repository, dict))
                _require(
                    type(repository.get("id")) is int
                    and repository["id"] == self.policy.repository_id
                )
                _require(
                    repository.get("full_name") == self.policy.repository_name
                    and repository.get("private") is True
                )
                _require("visibility" not in repository or repository["visibility"] == "private")
        job = self.get("GET", self.job_path, self.token)
        for field, expected in (
            ("id", self.job_id),
            ("run_id", self.run_id),
            ("run_attempt", self.attempt),
            ("runner_id", self.handoff.runner_id),
            ("runner_group_id", self.policy.group_id),
        ):
            _require(type(job.get(field)) is int and job[field] == expected)
        _require(job.get("head_sha") == self.policy.source_workflow_revision)
        _require(job.get("runner_name") == self.handoff.runner_name)
        _require(
            job.get("status") == "completed" and job.get("conclusion") in {"success", "failure"}
        )
        _require(isinstance(job.get("completed_at"), str) and job["completed_at"])

    def runner(self, row: Any):
        _require(isinstance(row, dict) and row.get("status") in {"online", "offline"})
        _require(row.get("ephemeral") is True)
        return row["status"], _runner({**row, "status": "online"}, self.handoff)

    def sample(self):
        self.completed()
        verify_group_policy(self.policy, self.token, request=self.get)
        first = self.get("GET", self.group_path + "/runners?per_page=100&page=1", self.token)
        terminal = self.get("GET", self.group_path + "/runners?per_page=100&page=2", self.token)
        renewed = self.get("GET", self.group_path + "/runners?per_page=100&page=1", self.token)
        _require(_canonical_bytes(first) == _canonical_bytes(renewed))
        count, rows = first.get("total_count"), first.get("runners")
        _require(
            type(count) is int
            and count in {0, 1}
            and isinstance(rows, list)
            and len(rows) == count
        )
        rows = cast(list, rows)
        _require(
            type(terminal.get("total_count")) is int
            and terminal["total_count"] == count
            and terminal.get("runners") == []
        )
        status, direct = self.call("GET", self.runner_path)
        if count == 0:
            _require(status == 404)
            return None
        _require(status == 200 and self.runner(rows[0]) == self.runner(direct))
        return self.runner(direct)

    def stable(self):
        first, second = self.sample(), self.sample()
        _require(first == second)
        self.renew_files()
        return second

    def intent(self):
        return _canonical_bytes(
            {
                "schema": "just-akash/jit-registration-terminal/v1",
                "state": "UNKNOWN",
                "mint_intent_sha256": self.mint_sha,
                "mint_candidate_sha256": hashlib.sha256(self.response_raw).hexdigest(),
                "repository_id": self.policy.repository_id,
                "repository_name": self.policy.repository_name,
                "policy_revision": self.policy.revision,
                "group_id": self.policy.group_id,
                "run_id": self.run_id,
                "run_attempt": self.attempt,
                "job_id": self.job_id,
                "source_revision": self.policy.source_workflow_revision,
                "runner_id": self.handoff.runner_id,
                "runner_name": self.handoff.runner_name,
                "request_path": self.runner_path,
                "method": "DELETE",
                "controller_authenticated": False,
                "Akash_cleanup_authority": False,
                "resource_absence_verified": False,
                "publication_authority": False,
            }
        )


def retire_completed_jit_registration(
    policy: JitPolicy,
    mint_path: Path,
    job_id: int,
    installation_token: str,
    terminal_path: Path,
    *,
    deadline: float,
    _request=_http,
) -> TerminalObservation:
    """Opt-in sole DELETE, only after actual exact completed-job observations.

    Trusted-controller authorization is a prerequisite, not supplied by files,
    constructors or this function. A retained terminal UNKNOWN always blocks
    another DELETE; use the separate read-only reconciliation function instead.
    """
    try:
        _require(not os.path.lexists(terminal_path))
        observer = _Terminal(
            policy, mint_path, job_id, installation_token, terminal_path, deadline, _request
        )
        present = observer.stable()
        acknowledged = False
        if present is not None:
            raw = observer.intent()
            _create_durable(observer.terminal_path, raw)
            identity = _owned(observer.terminal_path)
            _require(identity[0] == raw)
            # Renew actual job, policy and registration after journal fsync.
            _require(observer.sample() == present)
            observer.renew_files()
            _require(_owned(observer.terminal_path) == identity)
            try:
                status, body = observer.call("DELETE", observer.runner_path)
                acknowledged = status == 204 and body is None
            except Exception:
                # Mutation may have happened. Reads may reconcile, never retry.
                acknowledged = False
            absent = observer.stable() is None
        else:
            absent = True
        return TerminalObservation(
            observer.handoff.runner_id,
            job_id,
            observer.mint_sha,
            acknowledged,
            absent,
            observer.requests,
        )
    except Exception:
        raise JitHold(
            "completed JIT terminal observation unavailable; retain exact journals"
        ) from None


def reconcile_completed_jit_registration(
    policy: JitPolicy,
    mint_path: Path,
    job_id: int,
    installation_token: str,
    terminal_path: Path,
    *,
    deadline: float,
    _request=_http,
) -> TerminalObservation:
    """Read-only reconciliation of an existing UNKNOWN, never another DELETE."""
    try:
        observer = _Terminal(
            policy, mint_path, job_id, installation_token, terminal_path, deadline, _request
        )
        raw, identity = _owned(observer.terminal_path)
        _require(raw == observer.intent())
        absent = observer.stable() is None
        _require(_owned(observer.terminal_path) == (raw, identity))
        return TerminalObservation(
            observer.handoff.runner_id, job_id, observer.mint_sha, False, absent, observer.requests
        )
    except Exception:
        raise JitHold(
            "completed JIT terminal reconciliation unavailable; retain exact journals"
        ) from None
