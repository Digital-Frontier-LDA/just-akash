"""Native current cleanup actor and historical producer admission; read-only."""

from __future__ import annotations

import os
import re
import stat
import time
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from .github_jit import _json
from .keydb_parity_cleanup_provenance import (
    JOB_PREFIX,
    ROOT_PATHS,
    STEP,
    slot,
    validate_actor,
    validate_locator,
)
from .keydb_parity_provenance import validate_binding

REPOSITORY = "Borduas-Holdings/blazing"
REPOSITORY_ID = 1074974924
ORGANIZATION_ID = 208689568
ROOT = "/repos/" + REPOSITORY
SDK = "Digital-Frontier-LDA/just-akash/.github/workflows/"
PRODUCER_JOB = (
    "Provision one owned KeyDB module parity runner / "
    "Provision KeyDB parity through acknowledged JIT"
)
PRODUCER_STEP = "Run the acknowledged KeyDB parity controller"
TERMINAL = {"success", "failure", "cancelled", "timed_out"}


class CleanupHeld(RuntimeError):
    pass


def require(value: object) -> None:
    if not value:
        raise CleanupHeld("KeyDB parity cleanup native authority was not verified")


def obj(value: object) -> dict[str, Any]:
    require(type(value) is dict)
    return cast(dict[str, Any], value)


def number(value: object) -> int:
    require(type(value) is int and 0 < cast(int, value) < 2**63)
    return cast(int, value)


def decimal(value: str) -> int:
    require(type(value) is str and re.fullmatch(r"[1-9][0-9]{0,18}", value) is not None)
    return number(int(value))


def digest(value: object) -> str:
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{40}", cast(str, value)) is not None)
    return cast(str, value)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NativeGET:
    """One bounded fixed-origin private metadata observation, never a mutation."""

    def __init__(self, token: str):
        require(
            type(token) is str
            and 0 < len(token) <= 4096
            and all(33 <= ord(c) < 127 for c in token)
        )
        self.token = token
        self.calls = 0
        self.end = time.monotonic() + 30

    def __call__(self, route: str) -> dict[str, Any]:
        require(
            re.fullmatch(
                re.escape(ROOT)
                + (
                    r"(?:|/actions/runs/[1-9][0-9]{0,18}"
                    r"(?:/attempts/[1-9][0-9]{0,18}(?:/jobs\?per_page=100&page=[1-4])?)?"
                    r"|/actions/jobs/[1-9][0-9]{0,18})"
                ),
                route,
            )
            is not None
        )
        self.calls += 1
        remaining = self.end - time.monotonic()
        require(self.calls <= 24 and remaining > 0)
        result = None
        try:
            request = urllib.request.Request(
                "https://api.github.com" + route,
                method="GET",
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(
                request, timeout=min(10, remaining)
            ) as response:
                require(response.status == 200)
                raw = response.read(1048577)
            require(len(raw) <= 1048576 and time.monotonic() < self.end)
            result = obj(_json(raw))
        except Exception:
            result = None
        require(result is not None)
        return cast(dict[str, Any], result)


def jobs(get, run: int, attempt: int) -> list[dict[str, Any]]:
    values = []
    seen = set()
    total = None
    for page in range(1, 5):
        doc = get(ROOT + f"/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100&page={page}")
        count = doc.get("total_count")
        rows = doc.get("jobs")
        require(type(count) is int and 0 <= count <= 400 and (total is None or count == total))
        require(type(rows) is list and len(rows) <= 100)
        total = count
        for row in cast(list, rows):
            row = obj(row)
            identity = number(row.get("id"))
            require(identity not in seen)
            seen.add(identity)
            values.append(row)
        if len(values) == total:
            break
        require(len(cast(list, rows)) == 100 and len(values) < cast(int, total))
    require(len(values) == total)
    return values


def repository(get) -> None:
    value = get(ROOT)
    require(
        number(value.get("id")) == REPOSITORY_ID
        and value.get("full_name") == REPOSITORY
        and value.get("private") is True
    )
    owner = obj(value.get("owner"))
    require(
        number(owner.get("id")) == ORGANIZATION_ID and owner.get("login") == "Borduas-Holdings"
    )


def repositories(value: dict[str, Any]) -> None:
    for key in ("repository", "head_repository"):
        require(number(obj(value.get(key)).get("id")) == REPOSITORY_ID)


def reference(value: dict[str, Any], path: str, sha: str) -> None:
    refs = value.get("referenced_workflows")
    require(type(refs) is list and len(refs) <= 64)
    matches = [
        row
        for row in cast(list, refs)
        if type(row) is dict and row.get("path") == SDK + path + "@" + sha
    ]
    require(len(matches) == 1 and matches[0].get("sha") == sha)


def step(row: dict[str, Any], name: str, *, active: bool) -> None:
    rows = row.get("steps")
    require(type(rows) is list and len(rows) <= 256)
    matches = [s for s in cast(list, rows) if type(s) is dict and s.get("name") == name]
    require(len(matches) == 1)
    value = matches[0]
    if active:
        require(value.get("status") == "in_progress" and value.get("conclusion") is None)
    else:
        require(value.get("status") == "completed" and value.get("conclusion") in TERMINAL)


def locate_environment(environment: Mapping[str, str]) -> dict[str, int]:
    require(
        environment.get("GITHUB_REPOSITORY") == REPOSITORY
        and environment.get("GITHUB_REF") == "refs/heads/main"
        and environment.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and environment.get("GITHUB_JOB") == "cleanup"
    )
    target = validate_locator(
        dict(
            run_id=decimal(environment.get("KEYDB_PARITY_TARGET_RUN_ID", "")),
            run_attempt=decimal(environment.get("KEYDB_PARITY_TARGET_RUN_ATTEMPT", "")),
            sdk_round=decimal(environment.get("KEYDB_PARITY_TARGET_ROUND", "")),
        )
    )
    event = environment.get("GITHUB_EVENT_NAME")
    require(event in ("workflow_dispatch", "workflow_run"))
    if event == "workflow_dispatch":
        require(
            target["run_id"] == decimal(environment.get("GITHUB_RUN_ID", ""))
            and target["run_attempt"] == decimal(environment.get("GITHUB_RUN_ATTEMPT", ""))
        )
    else:
        path = Path(environment.get("GITHUB_EVENT_PATH", ""))
        require(path.is_absolute())
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            require(
                stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= 1048576
            )
            raw = os.read(fd, 1048577)
            require(len(raw) == info.st_size)
        finally:
            os.close(fd)
        doc = obj(_json(raw))
        require(doc.get("action") == "completed")
        run = obj(doc.get("workflow_run"))
        require(
            number(run.get("id")) == target["run_id"]
            and number(run.get("run_attempt")) == target["run_attempt"]
        )
        require(number(obj(doc.get("repository")).get("id")) == REPOSITORY_ID)
        # The webhook only selects the target IDs. Historical native run/job and
        # immutable receipt verification below supply producer authority.
    return target


def resolve_actor_job(environment: Mapping[str, str], get) -> dict[str, str]:
    """Read-only locator before the acknowledged action's active-step admission."""
    locator = locate_environment(environment)
    run = decimal(environment.get("GITHUB_RUN_ID", ""))
    attempt = decimal(environment.get("GITHUB_RUN_ATTEMPT", ""))
    repository(get)
    value = get(ROOT + f"/actions/runs/{run}")
    require(number(value.get("id")) == run and number(value.get("run_attempt")) == attempt)
    require(value.get("status") == "in_progress" and value.get("conclusion") is None)
    repositories(value)
    reference(
        value, "keydb-parity-cleanup.yml", digest(environment.get("KEYDB_PARITY_CLEANUP_SDK_SHA"))
    )
    expected = JOB_PREFIX + slot(locator)
    matches = [row for row in jobs(get, run, attempt) if row.get("name") == expected]
    require(len(matches) == 1)
    row = matches[0]
    require(
        row.get("status") == "in_progress"
        and row.get("conclusion") is None
        and row.get("runner_name") == environment.get("RUNNER_NAME")
        and row.get("labels") == ["ubuntu-latest"]
        and number(row.get("runner_id")) > 0
    )
    return {
        "actor-job-id": str(number(row.get("id"))),
        "native-head-sha": digest(value.get("head_sha")),
    }


def actor(environment: Mapping[str, str], get) -> dict[str, Any]:
    locator = locate_environment(environment)
    repository(get)
    run = decimal(environment.get("GITHUB_RUN_ID", ""))
    attempt = decimal(environment.get("GITHUB_RUN_ATTEMPT", ""))
    value = get(ROOT + f"/actions/runs/{run}")
    require(number(value.get("id")) == run and number(value.get("run_attempt")) == attempt)
    mode = "normal" if environment["GITHUB_EVENT_NAME"] == "workflow_dispatch" else "terminal"
    require(
        value.get("event") == environment["GITHUB_EVENT_NAME"]
        and value.get("path") == ROOT_PATHS[mode]
        and value.get("head_branch") == "main"
        and value.get("status") == "in_progress"
        and value.get("conclusion") is None
    )
    repositories(value)
    source = digest(environment.get("GITHUB_WORKFLOW_SHA"))
    native = digest(value.get("head_sha"))
    sdk = digest(environment.get("KEYDB_PARITY_CLEANUP_SDK_SHA"))
    require(
        environment.get("GITHUB_WORKFLOW_REF")
        == REPOSITORY + "/" + ROOT_PATHS[mode] + "@refs/heads/main"
    )
    if mode == "normal":
        require(source == native == environment.get("GITHUB_SHA"))
    reference(value, "keydb-parity-cleanup.yml", sdk)
    identity = decimal(environment.get("KEYDB_PARITY_ACTOR_JOB_ID", ""))
    name = JOB_PREFIX + slot(locator)
    selected = [j for j in jobs(get, run, attempt) if j.get("id") == identity]
    require(len(selected) == 1)

    def check(row):
        row = obj(row)
        require(
            number(row.get("id")) == identity
            and number(row.get("run_id")) == run
            and number(row.get("run_attempt")) == attempt
            and row.get("head_sha") == native
            and row.get("name") == name
        )
        require(
            row.get("status") == "in_progress"
            and row.get("conclusion") is None
            and row.get("labels") == ["ubuntu-latest"]
            and number(row.get("runner_id")) > 0
            and row.get("runner_name") == environment.get("RUNNER_NAME")
        )
        step(row, STEP, active=True)
        return (row["id"], row["runner_id"], row["runner_name"])

    first = check(selected[0])
    require(check(get(ROOT + f"/actions/jobs/{identity}")) == first)
    return validate_actor(
        dict(
            repository=REPOSITORY,
            repository_id=REPOSITORY_ID,
            mode=mode,
            event=environment["GITHUB_EVENT_NAME"],
            ref="refs/heads/main",
            workflow_path=ROOT_PATHS[mode],
            workflow_sha=source,
            native_head_sha=native,
            sdk_sha=sdk,
            run_id=run,
            run_attempt=attempt,
            job_id=identity,
            job_name=name,
            step_name=STEP,
            target_locator=locator,
        )
    )


def producer(
    actor_value: dict[str, Any], binding: dict[str, Any], get, target: dict[str, Any] | None = None
) -> dict[str, Any]:
    b = validate_binding(binding)
    require(
        {k: b[k] for k in ("run_id", "run_attempt", "sdk_round")} == actor_value["target_locator"]
    )
    repository(get)
    run, attempt = b["run_id"], b["run_attempt"]
    value = get(ROOT + f"/actions/runs/{run}/attempts/{attempt}")
    require(
        number(value.get("id")) == run
        and number(value.get("run_attempt")) == attempt
        and value.get("event") == "workflow_dispatch"
        and value.get("path") == ROOT_PATHS["normal"]
        and value.get("head_branch") == "main"
        and value.get("head_sha") == b["workflow_sha"] == b["native_head_sha"]
    )
    repositories(value)
    reference(value, "runner-pool.yml", b["sdk_sha"])
    if actor_value["mode"] == "normal":
        require(value.get("status") == "in_progress" and value.get("conclusion") is None)
    else:
        require(value.get("status") == "completed" and value.get("conclusion") in TERMINAL)
        latest = get(ROOT + f"/actions/runs/{run}")
        require(number(latest.get("id")) == run and number(latest.get("run_attempt")) >= attempt)
        repositories(latest)
        require(
            latest.get("path") == ROOT_PATHS["normal"]
            and latest.get("event") == "workflow_dispatch"
        )
        # A later failed-jobs rerun might otherwise reuse cached old pool outputs.
        # Caller-side slot admission is a separate protection, not an atomic GET.
        require(latest.get("status") == "completed" and latest.get("conclusion") in TERMINAL)
    population = jobs(get, run, attempt)
    selected = [j for j in population if j.get("id") == b["producer_job_id"]]
    require(len(selected) == 1)

    def check(row):
        row = obj(row)
        require(
            number(row.get("id")) == b["producer_job_id"]
            and number(row.get("run_id")) == run
            and number(row.get("run_attempt")) == attempt
            and row.get("head_sha") == b["workflow_sha"]
            and row.get("name") == PRODUCER_JOB
        )
        require(
            row.get("status") == "completed"
            and row.get("conclusion") in TERMINAL
            and type(row.get("started_at")) is str
            and bool(row["started_at"])
            and number(row.get("runner_id")) > 0
            and row.get("labels") == ["ubuntu-latest"]
        )
        step(row, PRODUCER_STEP, active=False)
        return (row["id"], row["runner_id"], row["runner_name"])

    first = check(selected[0])
    require(check(get(ROOT + f"/actions/jobs/{b['producer_job_id']}")) == first)
    if actor_value["mode"] == "normal":
        require(target is not None)
        target = obj(target)
        name = "Run original KeyDB parity on owned Podman runner " + slot(
            actor_value["target_locator"]
        )
        work = [j for j in population if j.get("name") == name]
        require(len(work) == 1)

        def ended(row):
            row = obj(row)
            identity = number(row.get("id"))
            require(
                number(row.get("run_id")) == run
                and number(row.get("run_attempt")) == attempt
                and row.get("head_sha") == b["workflow_sha"]
                and row.get("name") == name
            )
            require(
                row.get("status") == "completed"
                and row.get("conclusion") in TERMINAL | {"skipped"}
            )
            require("runner_id" in row and "runner_name" in row)
            runner = row["runner_id"]
            if runner is None or (type(runner) is int and runner == 0):
                require(row["runner_name"] in (None, ""))
            else:
                require(
                    number(runner) == target["returned_runner_id"]
                    and type(row["runner_name"]) is str
                    and bool(row["runner_name"])
                )
            return identity, row["status"], row["conclusion"], runner, row["runner_name"]

        finished = ended(work[0])
        require(ended(get(ROOT + f"/actions/jobs/{finished[0]}")) == finished)
    return value
