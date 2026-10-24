"""Native identity admission for the independent BB TruffleHog private runner role."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import suppress
from http.client import HTTPException
from typing import Any, cast

CONTRACT = "bb-native-trufflehog/v1"
REPOSITORY = "Borduas-Holdings/Blazing-Back"
REPOSITORY_ID = 1071436278
ROOT_WORKFLOW = ".github/workflows/secret-scanning.yml"
SDK_REPOSITORY = "Digital-Frontier-LDA/just-akash"
SDK_WORKFLOW = ".github/workflows/runner-pool.yml"
JOB = "Provision one owned native TruffleHog runner / Provision ephemeral runner pool on Akash"
STEP = "Admit the private scanner native caller"
ROOT = "/repos/" + REPOSITORY


class CallerHeld(ValueError):
    """Fixed failure text; remote responses and credentials are never diagnostic text."""


def require(value: object) -> None:
    if not value:
        raise CallerHeld("Optional hosted native caller was not verified")


def positive(value: object) -> int:
    require(type(value) is int and 0 < cast(int, value) < 2**63)
    return cast(int, value)


def object_value(value: object) -> dict[str, Any]:
    require(type(value) is dict)
    return cast(dict[str, Any], value)


def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NativeGET:
    """Fixed-origin, bounded, no-redirect GET transport. Never retries a request."""

    def __init__(self, token: str):
        require(0 < len(token) <= 4096 and all(33 <= ord(c) < 127 for c in token))
        self.token = token
        self.end = time.monotonic() + 120
        self.calls = 0

    def __call__(self, route: str) -> dict[str, Any]:
        require(
            re.fullmatch(
                r"(?:" + re.escape(ROOT) + r"(?:|/actions/runs/[1-9][0-9]{0,18}"
                r"(?:/attempts/[1-9][0-9]{0,18}/jobs\?per_page=100&page=[1-4])?"
                r"|/actions/jobs/[1-9][0-9]{0,18}|/pulls/[1-9][0-9]{0,18}"
                r"|/git/ref/heads/main)|/orgs/Borduas-Holdings/actions/runner-groups"
                r"(?:\?per_page=100&page=[12]|/[1-9][0-9]{0,18}"
                r"(?:/repositories\?per_page=100&page=[12])?))",
                route,
            )
            is not None
        )
        self.calls += 1
        remaining = self.end - time.monotonic()
        require(self.calls <= 26 and remaining > 0)
        document: dict[str, Any] | None = None
        try:
            request = urllib.request.Request(
                "https://api.github.com" + route,
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "just-akash-hosted-caller",
                },
                method="GET",
            )
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(
                request, timeout=min(10, remaining)
            ) as response:
                require(response.status == 200)
                raw = response.read(1024 * 1024 + 1)
            require(len(raw) <= 1024 * 1024 and time.monotonic() < self.end)
            document = object_value(json.loads(raw, object_pairs_hook=unique))
        except (OSError, HTTPException, ValueError, TypeError, RecursionError):
            pass
        # Deliberately outside the handler so raw transport exception context cannot escape.
        require(document is not None)
        return cast(dict[str, Any], document)


def verify_scanner_group(get: Callable[[str], dict[str, Any]]) -> None:
    """Org registration is allowed only in a selected, private Default group containing BB."""
    root = "/orgs/Borduas-Holdings/actions/runner-groups"
    first, second = (get(root + f"?per_page=100&page={page}") for page in (1, 2))
    rows = first.get("runner_groups")
    require(type(rows) is list and 0 < len(rows) <= 100)
    rows = cast(list[Any], rows)
    require(first.get("total_count") == len(rows))
    require(second.get("total_count") == len(rows) and second.get("runner_groups") == [])
    groups = [object_value(row) for row in cast(list[Any], rows)]
    identities = [positive(row.get("id")) for row in groups]
    require(len(identities) == len(set(identities)))
    matches = [row for row in groups if row.get("default") is True]
    require(len(matches) == 1)
    identity = positive(matches[0].get("id"))
    route = root + f"/{identity}"

    def policy(row: dict[str, Any]) -> tuple[object, ...]:
        require(positive(row.get("id")) == identity and row.get("default") is True)
        require(row.get("visibility") == "selected")
        require(row.get("allows_public_repositories") is False)
        require(
            row.get("restricted_to_workflows") is False and row.get("selected_workflows") == []
        )
        return (identity, True, "selected", False, False)

    expected = policy(matches[0])
    require(policy(get(route)) == expected)
    allowed = {1071436278: "Borduas-Holdings/Blazing-Back", 1074974924: "Borduas-Holdings/blazing"}
    first, second = (get(route + f"/repositories?per_page=100&page={page}") for page in (1, 2))
    rows = first.get("repositories")
    require(type(rows) is list and 1 <= len(rows) <= 2)
    rows = cast(list[Any], rows)
    require(first.get("total_count") == len(rows))
    require(second.get("total_count") == len(rows) and second.get("repositories") == [])
    seen: set[int] = set()
    for value in cast(list[Any], rows):
        row = object_value(value)
        repo = positive(row.get("id"))
        require(repo not in seen and row.get("full_name") == allowed.get(repo))
        require(row.get("private") is True)
        seen.add(repo)
    require(REPOSITORY_ID in seen)
    require(policy(get(route)) == expected)


def admit(environment: Mapping[str, str], get: Callable[[str], dict[str, Any]]) -> None:
    require(environment.get("NATIVE_CALLER_CONTRACT") == CONTRACT)
    require(environment.get("GITHUB_REPOSITORY") == REPOSITORY)
    require(environment.get("RUNNER_ENVIRONMENT") == "github-hosted")
    require(environment.get("GITHUB_JOB") == "pool")
    require(environment.get("NATIVE_CALLER_REPOSITORY_SCOPE") == "false")
    require(environment.get("NATIVE_CALLER_PULL_READER") == "false")
    require(environment.get("NATIVE_CALLER_REGISTRY_SOPS") == "true")
    require(environment.get("NATIVE_CALLER_CREDENTIALS_SOPS") == "false")
    require(environment.get("RUNNER_PRIVATE_PROFILE") == "bb-native-trufflehog")
    require(environment.get("ORG") == "Borduas-Holdings")
    runner = environment.get("RUNNER_NAME", "")
    require(bool(runner) and len(runner) <= 256)
    source = environment.get("GITHUB_SHA", "")
    sdk = environment.get("NATIVE_CALLER_SDK_REF", "")
    require(re.fullmatch(r"[0-9a-f]{40}", source) is not None)
    require(re.fullmatch(r"[0-9a-f]{40}", sdk) is not None)
    require(environment.get("NATIVE_CALLER_SDK_REPOSITORY") == SDK_REPOSITORY)
    require(environment.get("GITHUB_WORKFLOW_SHA") == source)
    event, ref = environment.get("GITHUB_EVENT_NAME"), environment.get("GITHUB_REF", "")
    require(event == "workflow_dispatch" and ref == "refs/heads/main")
    require(environment.get("GITHUB_WORKFLOW_REF") == REPOSITORY + "/" + ROOT_WORKFLOW + "@" + ref)
    run_text, attempt_text = (
        environment.get("GITHUB_RUN_ID", ""),
        environment.get("GITHUB_RUN_ATTEMPT", ""),
    )
    require(re.fullmatch(r"[1-9][0-9]{0,18}", run_text) is not None)
    require(re.fullmatch(r"[1-9][0-9]{0,18}", attempt_text) is not None)
    run, attempt = positive(int(run_text)), positive(int(attempt_text))
    fixed = {
        "SCANNER_POOL_SIZE": "1",
        "SCANNER_MIN_POOL_SIZE": "1",
        "SCANNER_CPU": "4",
        "SCANNER_MEMORY": "16Gi",
        "SCANNER_STORAGE": "96Gi",
        "SCANNER_EPHEMERAL": "true",
        "SCANNER_PROVIDER_SELECT": "cheapest",
        "SCANNER_IMAGE": "",
        "SCANNER_REGISTRY_HOST": "https://index.docker.io/v1/",
        "SCANNER_REGISTRY_USERNAME": "jobordu",
        "SCANNER_AGE_PRESENT": "true",
        "SCANNER_PASSWORD_PRESENT": "false",
        "SCANNER_LABEL": f"trufflehog-{run}-{attempt}",
        "SCANNER_PLACEMENT": f"borduas-native-trufflehog-run-{run}-end",
        "SCANNER_TAG_PREFIX": "ci-blazing-back-native-trufflehog",
    }
    require(all(environment.get(key) == value for key, value in fixed.items()))
    encoded_owners = environment.get("SCANNER_OWNED_PROVIDERS", "")
    require(0 < len(encoded_owners) <= 512)
    owners: object = None
    with suppress(ValueError, TypeError, RecursionError):
        owners = json.loads(encoded_owners, object_pairs_hook=unique)
    require(type(owners) is list and len(owners) == 3)
    owners = cast(list[object], owners)
    require(all(type(value) is str for value in owners))
    require(
        set(owners)
        == {
            "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk",
            "akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z",
            "akash1z9nr23cgweu45g2jktfx95v7g2xp8qlsa3ys2x",
        }
    )
    expected_callee = SDK_REPOSITORY + "/" + SDK_WORKFLOW + "@" + sdk
    step = environment.get("SCANNER_ADMISSION_STEP", "")
    require(
        step
        in (
            STEP,
            "Preflight — the runner PAT must still be valid",
            "Provision (retry down candidates, tag before waiting)",
        )
    )

    def context() -> str:
        repo = get(ROOT)
        require(positive(repo.get("id")) == REPOSITORY_ID)
        require(repo.get("full_name") == REPOSITORY and repo.get("private") is True)
        value = get(ROOT + f"/actions/runs/{run}")
        require(positive(value.get("id")) == run and positive(value.get("run_attempt")) == attempt)
        require(value.get("path") == ROOT_WORKFLOW and value.get("event") == event)
        require(value.get("status") == "in_progress" and value.get("conclusion") is None)
        for field in ("repository", "head_repository"):
            require(positive(object_value(value.get(field)).get("id")) == REPOSITORY_ID)
        native_head = value.get("head_sha")
        require(
            type(native_head) is str and re.fullmatch(r"[0-9a-f]{40}", native_head) is not None
        )
        require(native_head == source and value.get("head_branch") == "main")
        require(object_value(get(ROOT + "/git/ref/heads/main").get("object")).get("sha") == source)
        rows = value.get("referenced_workflows")
        require(type(rows) is list and 1 <= len(rows) <= 64)
        rows = cast(list[Any], rows)
        matches = [
            object_value(row) for row in rows if object_value(row).get("path") == expected_callee
        ]
        require(len(matches) == 1 and matches[0].get("sha") == sdk)
        return cast(str, native_head)

    native_head = context()
    verify_scanner_group(get)
    jobs: list[dict[str, Any]] = []
    seen: set[int] = set()
    total: int | None = None
    for page in range(1, 5):
        response = get(
            ROOT + f"/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100&page={page}"
        )
        count = response.get("total_count")
        require(type(count) is int and 0 <= count <= 400 and (total is None or count == total))
        total = cast(int, count)
        batch = response.get("jobs")
        require(type(batch) is list and len(batch) <= 100)
        batch = cast(list[Any], batch)
        for raw in batch:
            row = object_value(raw)
            identity = positive(row.get("id"))
            require(identity not in seen)
            seen.add(identity)
            jobs.append(row)
        if len(jobs) == total:
            break
        require(len(batch) == 100 and len(jobs) < total)
    require(len(jobs) == total)
    matches = [row for row in jobs if row.get("name") == JOB]
    require(len(matches) == 1)
    job = matches[0]
    job_id = positive(job.get("id"))

    def job_identity(row: dict[str, Any]) -> tuple[object, ...]:
        require(positive(row.get("id")) == job_id and row.get("name") == JOB)
        require(positive(row.get("run_id")) == run and positive(row.get("run_attempt")) == attempt)
        require(row.get("head_sha") == native_head)
        require(row.get("status") == "in_progress" and row.get("conclusion") is None)
        require(row.get("runner_name") == runner)
        runner_id = positive(row.get("runner_id"))
        require(row.get("labels") == ["ubuntu-latest"])
        steps = row.get("steps")
        require(type(steps) is list and len(steps) <= 256)
        steps = cast(list[Any], steps)
        active = [object_value(item) for item in steps if object_value(item).get("name") == step]
        require(
            len(active) == 1
            and active[0].get("status") == "in_progress"
            and active[0].get("conclusion") is None
        )
        return (job_id, run, attempt, native_head, JOB, runner_id, runner, "ubuntu-latest", step)

    expected_job = job_identity(job)
    require(job_identity(get(ROOT + f"/actions/jobs/{job_id}")) == expected_job)
    require(context() == native_head)
    verify_scanner_group(get)
    require(job_identity(get(ROOT + f"/actions/jobs/{job_id}")) == expected_job)


def main() -> None:
    token = os.environ.get("GH_TOKEN", "")
    admit(os.environ, NativeGET(token))
