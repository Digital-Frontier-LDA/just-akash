"""Trusted-controller JIT primitive; never an Akash create authorization.

The caller owns durable mint intent, narrow installation-token acquisition,
credential delivery, unknown-outcome reconciliation and independent cleanup.
Nothing here enables a pool or changes legacy runner-pool consumers.
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

API = "https://api.github.com"
ORGANIZATION = "Digital-Frontier-LDA"
_ROOT = f"/orgs/{ORGANIZATION}/actions"
_MAX_BYTES = 1024 * 1024


#: The bare failure classes a hold may carry (df-cicd-hz4.1.5). Constants only.
JIT_FAILURE_KINDS = frozenset(
    {"http", "timeout", "network", "invalid", "rate_limited", "name_exists"}
)
#: GitHub's x-github-request-id: hex groups joined by colons. Anything else is dropped.
_REQUEST_ID = re.compile(r"[A-Za-z0-9:]{1,80}")
#: A read-only GET gets exactly one retry after this wait, or after the wait GitHub
#: names (Retry-After / x-ratelimit-reset), never longer than the cap: a longer named
#: wait holds instead. A rate limit (403/429) and the POST are never retried.
_RETRY_DEFAULT_SECONDS = 2.0
_RETRY_CAP_SECONDS = 10.0
_sleep = time.sleep
_now = time.time


class JitHold(RuntimeError):
    """Admission or observation is incomplete; no successful handoff.

    Carries only bare, validated evidence (df-cicd-hz4.1.5): ``http_status`` (an int
    100-599 or None), ``kind`` (one of JIT_FAILURE_KINDS or None) and ``request_id``
    (GitHub's x-github-request-id or None). Never response text.
    """

    def __init__(
        self,
        message: str = "GitHub JIT step held",
        *,
        http_status: int | None = None,
        kind: str | None = None,
        request_id: str | None = None,
    ):
        super().__init__(message)
        self.http_status = (
            http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        )
        self.kind = kind if isinstance(kind, str) and kind in JIT_FAILURE_KINDS else None
        self.request_id = (
            request_id
            if isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id)
            else None
        )


class JitMintUnknown(JitHold):
    """POST may have created a registration; reconcile before another mint.

    The durable caller already knows the operation/slot name. No exception text
    contains response bodies, bearer tokens or encoded JIT credentials. Before a
    later mint of the same name, look it up (find_runner_by_name, or mint_jit with
    require_name_absent=True) so an unknown outcome never leaves an orphan.
    """


class _Failure(Exception):
    """One attempt's bare outcome; never leaves this module."""

    def __init__(self, kind, status=None, request_id=None, *, retryable=False, wait=None):
        super().__init__(kind)
        self.kind, self.status, self.request_id = kind, status, request_id
        self.retryable, self.wait = retryable, wait


def _header(headers, name: str) -> str | None:
    # An HTTPMessage (case-insensitive) or None; any non-string value is absent.
    get = getattr(headers, "get", None)
    value = get(name) if callable(get) else None
    return value if isinstance(value, str) else None


def _named_wait(headers) -> float | None:
    """The longer of Retry-After (seconds) and x-ratelimit-reset (epoch), else None."""
    waits = []
    retry_after = _header(headers, "retry-after")
    if retry_after is not None and re.fullmatch(r"[0-9]{1,6}", retry_after.strip()):
        waits.append(float(int(retry_after.strip())))
    reset = _header(headers, "x-ratelimit-reset")
    if reset is not None and re.fullmatch(r"[0-9]{1,12}", reset.strip()):
        remaining = int(reset.strip()) - _now()
        if remaining > 0:
            waits.append(float(remaining))
    return max(waits) if waits else None


def _rate_limited(status: int, headers) -> bool:
    if status == 429:
        return True
    return status == 403 and (
        _header(headers, "x-ratelimit-remaining") == "0"
        or _header(headers, "retry-after") is not None
    )


def _send(opener, req, method: str) -> dict:
    expected = 201 if method == "POST" else 200
    try:
        with opener.open(req, timeout=20) as response:
            headers = getattr(response, "headers", None)
            request_id = _header(headers, "x-github-request-id")
            status = getattr(response, "status", None)
            if status != expected:
                raise _Failure("invalid", status, request_id)
            raw = response.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise _Failure("invalid", status, request_id)
            try:
                result = _json(raw)
            except (ValueError, RecursionError):
                raise _Failure("invalid", status, request_id) from None
            if not isinstance(result, dict):
                raise _Failure("invalid", status, request_id)
            return result
    except _Failure:
        raise
    except urllib.error.HTTPError as error:
        status, headers = error.code, error.headers
        request_id = _header(headers, "x-github-request-id")
        with contextlib.suppress(Exception):
            error.close()  # never read the body: it is response text
        if type(status) is int and _rate_limited(status, headers):
            raise _Failure("rate_limited", status, request_id) from None
        raise _Failure(
            "http",
            status,
            request_id,
            retryable=type(status) is int and 500 <= status <= 599,
            wait=_named_wait(headers),
        ) from None
    except TimeoutError:
        raise _Failure("timeout", retryable=True) from None
    except urllib.error.URLError as error:
        kind = "timeout" if isinstance(error.reason, TimeoutError) else "network"
        raise _Failure(kind, retryable=True) from None
    except OSError:
        raise _Failure("network", retryable=True) from None
    except (ValueError, RecursionError):
        raise _Failure("invalid") from None


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
    """Fixed-origin, no-redirect, bounded request with no automatic POST retry.

    A read-only GET that fails on a 5xx, a timeout or the network gets exactly one
    retry, after GitHub's named wait capped at _RETRY_CAP_SECONDS (a longer wait
    holds). A rate limit, any other status, an invalid response and the POST are
    never retried. The hold carries the last attempt's bare evidence only.
    """
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
        raise JitHold("invalid controller request", kind="invalid")
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
    attempts = 1 if method == "POST" else 2
    failure = _Failure("invalid")
    for attempt in range(attempts):
        try:
            # noqa S310: path is constrained to a fixed HTTPS GitHub org API origin.
            return _send(urllib.request.build_opener(_NoRedirect()), req, method)
        except _Failure as caught:
            failure = caught
        if attempt + 1 >= attempts or not failure.retryable:
            break
        wait = _RETRY_DEFAULT_SECONDS if failure.wait is None else failure.wait
        if wait > _RETRY_CAP_SECONDS:
            break
        _sleep(wait)
    error = JitMintUnknown if method == "POST" else JitHold
    raise error(
        "GitHub mutation outcome unknown"
        if method == "POST"
        else "GitHub policy observation unavailable",
        http_status=failure.status,
        kind=failure.kind,
        request_id=failure.request_id,
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
    source_workflow_branch: str = "main"

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
            or self.source_workflow_branch
            not in (
                "main",
                "master",
                "deploy-env",
                "ci-akash-qualification",
                "ci-akash-qualification-v2",
                "ci-akash-qualification-v3",
            )
        ):
            raise JitHold("invalid immutable JIT policy")
        if self.non_reusable_workflow:
            # GitHub group restrictions pin non-reusable workflows to a branch.
            # The trusted policy fixes the default branch separately from the
            # approved source SHA. Existing main callers keep the same default.
            if (
                len(self.workflows) != 1
                or not isinstance(self.source_workflow_revision, str)
                or re.fullmatch(r"[0-9a-f]{40}", self.source_workflow_revision) is None
            ):
                raise JitHold("non-reusable pilot needs one workflow and its source SHA")
            suffix = re.escape("refs/heads/" + self.source_workflow_branch)
        else:
            if self.source_workflow_revision is not None or self.source_workflow_branch != "main":
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
class RunnerRegistration:
    """One org runner registration found by its exact name; public facts only."""

    runner_id: int
    runner_name: str
    busy: bool | None


_RUNNER_NAME = re.compile(r"dfci-[a-z0-9-]{1,90}")


def find_runner_by_name(
    runner_name: str,
    installation_token: str,
    *,
    request: Callable[..., dict] = github_request,
) -> RunnerRegistration | None:
    """Read-only exact-name lookup of an org runner registration (df-cicd-hz4.1.5).

    A caller runs it before any new mint of a name whose earlier mint ended in
    JitMintUnknown, so an unknown outcome never leaves an orphan beside a second
    registration. One GET of the org runner list filtered by name; a population
    that cannot be verified, or two registrations with the name, holds.
    """
    if (
        not isinstance(runner_name, str)
        or _RUNNER_NAME.fullmatch(runner_name) is None
        or not isinstance(installation_token, str)
        or not installation_token
        or re.search(r"[\r\n]", installation_token)
    ):
        raise JitHold("invalid runner lookup", kind="invalid")
    doc = request("GET", f"{_ROOT}/runners?name={runner_name}&per_page=100", installation_token)
    count, rows = doc.get("total_count"), doc.get("runners")
    if (
        type(count) is not int
        or not isinstance(rows, list)
        or len(rows) > 100
        or count != len(rows)
        or any(
            not isinstance(row, dict)
            or not _positive(row.get("id"))
            or not isinstance(row.get("name"), str)
            for row in rows
        )
    ):
        raise JitHold("runner name population unverified", kind="invalid")
    exact = [row for row in rows if row["name"] == runner_name]
    if len(exact) > 1:
        raise JitHold("runner name is ambiguous", kind="invalid")
    if not exact:
        return None
    busy = exact[0].get("busy")
    return RunnerRegistration(exact[0]["id"], runner_name, busy if type(busy) is bool else None)


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
    require_name_absent: bool = False,
) -> JitHandoff:
    """Verify current exact policy and mint one slot, never retrying a mutation.

    Caller must persist unique slot intent first and call only after admission.
    A JitMintUnknown blocks replacement and requires exact-name reconciliation.
    Handoff stays in memory and must never enter a journal/log/output/artifact.
    For a non-reusable workflow the trusted caller supplies the source revision
    from its authenticated producer. Equality here binds that revision to policy;
    it does not authenticate caller-supplied strings or replace admission.
    With require_name_absent (any mint after a JitMintUnknown for this name), an
    exact-name lookup runs after the policy check and an existing registration
    holds (kind "name_exists") before any POST.
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
    if require_name_absent is not False and (
        require_name_absent is not True
        or find_runner_by_name(runner_name, installation_token, request=request) is not None
    ):
        raise JitHold("runner name already registered; reconcile it", kind="name_exists")
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
    except JitHold as error:
        raise JitMintUnknown(
            "GitHub mutation outcome unknown; reconcile the exact slot",
            http_status=error.http_status,
            kind=error.kind,
            request_id=error.request_id,
        ) from None
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise JitMintUnknown(
            "GitHub mutation outcome unknown; reconcile the exact slot", kind="invalid"
        ) from None
