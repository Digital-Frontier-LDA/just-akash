"""Trusted-controller JIT primitive; never an Akash create authorization.

The caller owns durable mint intent, narrow installation-token acquisition,
credential delivery, unknown-outcome reconciliation and independent cleanup.
Nothing here enables a pool or changes legacy runner-pool consumers.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

API = "https://api.github.com"
ORGANIZATION = "Digital-Frontier-LDA"
_ROOT = f"/orgs/{ORGANIZATION}/actions"
_MAX_BYTES = 1024 * 1024


class JitHold(RuntimeError):
    """Admission or observation is incomplete; no successful handoff."""


class JitMintUnknown(JitHold):
    """POST may have created a registration; reconcile before another mint.

    The durable caller already knows the operation/slot name. No exception text
    contains response bodies, bearer tokens or encoded JIT credentials.
    """


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _json(raw: bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("non-finite JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def github_request(method: str, path: str, token: str, body: dict | None = None) -> dict:
    """Fixed-origin, no-redirect, bounded request with no automatic POST retry."""
    if (
        method not in {"GET", "POST"}
        or not path.startswith(_ROOT + "/")
        or re.search(r"[\s#\\]", path)
        or ".." in path
        or "%" in path
        or not isinstance(token, str)
        or not token
        or re.search(r"[\r\n]", token)
    ):
        raise JitHold("invalid controller request")
    req = urllib.request.Request(  # noqa: S310 — fixed HTTPS org origin, constrained path
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "just-akash-jit-controller",
        },
    )
    try:
        # noqa S310: path is constrained to a fixed HTTPS GitHub org API origin.
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=20) as response:
            if response.status != (201 if method == "POST" else 200):
                raise JitHold("unexpected GitHub status")
            raw = response.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise JitHold("GitHub response exceeds controller bound")
            result = _json(raw)
            if not isinstance(result, dict):
                raise JitHold("GitHub response is not an object")
            return result
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, RecursionError, JitHold):
        error = JitMintUnknown if method == "POST" else JitHold
        raise error(
            "GitHub mutation outcome unknown"
            if method == "POST"
            else "GitHub policy observation unavailable"
        ) from None


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


@dataclass(frozen=True)
class JitPolicy:
    """Single private-repository pilot policy loaded from trusted immutable config.

    Policy construction is data validation, not producer authentication. Public
    or fork pilots need a separately reviewed zero-secret policy and are held.
    """

    revision: str
    group_id: int
    repository_id: int
    repository_name: str
    workflows: tuple[str, ...]
    non_reusable_workflow: bool = False
    source_workflow_revision: str | None = None

    def __post_init__(self):
        if (
            not isinstance(self.revision, str)
            or re.fullmatch(r"[0-9a-f]{40}", self.revision) is None
            or not _positive(self.group_id)
            or not _positive(self.repository_id)
            or not isinstance(self.repository_name, str)
            or re.fullmatch(ORGANIZATION + r"/[A-Za-z0-9_.-]+", self.repository_name) is None
            or type(self.workflows) is not tuple
            or not self.workflows
            or len(self.workflows) > 100
            or type(self.non_reusable_workflow) is not bool
        ):
            raise JitHold("invalid immutable JIT policy")
        if self.non_reusable_workflow:
            # GitHub group restrictions pin non-reusable workflows to a branch.
            # Bind the pilot to main and retain its separately approved source SHA.
            if (
                len(self.workflows) != 1
                or not isinstance(self.source_workflow_revision, str)
                or re.fullmatch(r"[0-9a-f]{40}", self.source_workflow_revision) is None
            ):
                raise JitHold("non-reusable pilot needs one workflow and its source SHA")
            suffix = r"refs/heads/main"
        else:
            if self.source_workflow_revision is not None:
                raise JitHold("branch source binding requires non-reusable workflow policy")
            suffix = r"[0-9a-f]{40}"
        prefix = re.escape(self.repository_name) + r"/\.github/workflows/"
        for workflow in self.workflows:
            if (
                not isinstance(workflow, str)
                or re.fullmatch(prefix + r"[A-Za-z0-9_-]+\.ya?ml@" + suffix, workflow) is None
            ):
                raise JitHold("workflow restriction differs from the pilot reference policy")
        if len(set(self.workflows)) != len(self.workflows):
            raise JitHold("duplicate workflow policy")


def _verify_group(policy: JitPolicy, call: Callable[..., dict]) -> None:
    path = f"{_ROOT}/runner-groups/{policy.group_id}"
    group = call("GET", path)
    workflows = group.get("selected_workflows")
    if (
        type(group.get("id")) is not int
        or group["id"] != policy.group_id
        or group.get("visibility") != "selected"
        or group.get("allows_public_repositories") is not False
        or group.get("restricted_to_workflows") is not True
        or not isinstance(workflows, list)
        or not all(isinstance(item, str) for item in workflows)
        or len(workflows) != len(policy.workflows)
        or set(workflows) != set(policy.workflows)
    ):
        raise JitHold("runner group differs from immutable policy")
    # Fetch an explicit terminal page, with stable totals and duplicate guards.
    # Ignore server-supplied URLs: they must never redirect a bearer credential.
    repositories: dict[int, str] = {}
    # A single-repository pilot needs one populated page and one terminal page.
    # Refuse a broader population immediately instead of scanning it with a
    # controller credential that was intended for one repository.
    for page in (1, 2):
        doc = call("GET", path + f"/repositories?per_page=100&page={page}")
        count, rows = doc.get("total_count"), doc.get("repositories")
        if type(count) is not int or count != 1 or not isinstance(rows, list) or len(rows) > 100:
            raise JitHold("incomplete runner-group repository population")
        for row in rows:
            if (
                not isinstance(row, dict)
                or not _positive(row.get("id"))
                or row["id"] in repositories
                or not isinstance(row.get("full_name"), str)
                or row.get("private") is not True
            ):
                raise JitHold("invalid or duplicate selected repository")
            repositories[row["id"]] = row["full_name"]
        if len(repositories) > count:
            raise JitHold("repository population exceeds total")
        if not rows:
            if len(repositories) != count:
                raise JitHold("missing selected repositories")
            break
    if repositories != {policy.repository_id: policy.repository_name}:
        raise JitHold("selected repository population differs from immutable policy")
    # Detect policy changes during pagination. GitHub does not offer an atomic
    # policy-read/mint transaction; operational policy writers must be controlled.
    if call("GET", path) != group:
        raise JitHold("runner-group policy changed during observation")


def verify_group_policy(
    policy: JitPolicy,
    installation_token: str,
    *,
    request: Callable[..., dict] = github_request,
) -> None:
    """Read-only readiness probe; never a reusable create-admission receipt."""
    if (
        not isinstance(policy, JitPolicy)
        or not isinstance(installation_token, str)
        or not installation_token
        or re.search(r"[\r\n]", installation_token)
    ):
        raise JitHold("invalid JIT policy observation input")
    _verify_group(policy, lambda method, path: request(method, path, installation_token))


@dataclass(frozen=True)
class JitHandoff:
    runner_id: int
    runner_name: str
    group_id: int
    policy_revision: str
    labels: tuple[str, ...]
    encoded_config: str = field(repr=False, compare=False)


def mint_jit(
    policy: JitPolicy,
    runner_name: str,
    labels: tuple[str, ...],
    installation_token: str,
    *,
    request: Callable[..., dict] = github_request,
    producer_workflow_revision: str | None = None,
) -> JitHandoff:
    """Verify current exact policy and mint one slot, never retrying a mutation.

    Caller must persist unique slot intent first and call only after admission.
    A JitMintUnknown blocks replacement and requires exact-name reconciliation.
    Handoff stays in memory and must never enter a journal/log/output/artifact.
    For a non-reusable workflow the trusted caller supplies the source revision
    from its authenticated producer. Equality here binds that revision to policy;
    it does not authenticate caller-supplied strings or replace admission.
    """
    if (
        not isinstance(policy, JitPolicy)
        or not isinstance(runner_name, str)
        or re.fullmatch(r"dfci-[a-z0-9-]{1,90}", runner_name) is None
        or type(labels) is not tuple
        or not 1 <= len(labels) <= 100
        or any(
            not isinstance(label, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", label) is None
            for label in labels
        )
        or len(set(labels)) != len(labels)
        or runner_name not in labels
        or not isinstance(installation_token, str)
        or not installation_token
        or re.search(r"[\r\n]", installation_token)
    ):
        raise JitHold("invalid JIT delivery slot")
    if policy.non_reusable_workflow:
        if producer_workflow_revision != policy.source_workflow_revision:
            raise JitHold("producer workflow source differs from the approved revision")
    elif producer_workflow_revision is not None:
        raise JitHold("unexpected branch source binding for reusable workflow policy")

    def call(method: str, path: str, body: dict | None = None) -> dict:
        return request(method, path, installation_token, body)

    verify_group_policy(policy, installation_token, request=request)
    try:
        doc = call(
            "POST",
            _ROOT + "/runners/generate-jitconfig",
            {
                "name": runner_name,
                "runner_group_id": policy.group_id,
                "labels": list(labels),
                "work_folder": "_work",
            },
        )
        runner, config = doc.get("runner"), doc.get("encoded_jit_config")
        if (
            not isinstance(runner, dict)
            or not _positive(runner.get("id"))
            or runner.get("name") != runner_name
            or not isinstance(config, str)
            or not config
            or len(config) > _MAX_BYTES
        ):
            raise ValueError("invalid JIT response")
        raw = base64.b64decode(config, validate=True)
        if base64.b64encode(raw).decode() != config:
            raise ValueError("noncanonical JIT encoding")
        decoded = _json(raw)
        if (
            not isinstance(decoded, dict)
            or not decoded
            or any(not isinstance(value, str) or not value for value in decoded.values())
        ):
            raise ValueError("invalid JIT configuration")
        # Runner.Listener decodes each map value into a file under its root.
        # Validate both encoding layers and keep each path within that root.
        for filename, content in decoded.items():
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", filename) is None or filename in {".", ".."}:
                raise ValueError("invalid JIT configuration path")
            file_bytes = base64.b64decode(content, validate=True)
            if not file_bytes or base64.b64encode(file_bytes).decode() != content:
                raise ValueError("invalid JIT configuration file encoding")
        return JitHandoff(
            runner["id"], runner_name, policy.group_id, policy.revision, labels, config
        )
    except (JitHold, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise JitMintUnknown("GitHub mutation outcome unknown; reconcile the exact slot") from None
