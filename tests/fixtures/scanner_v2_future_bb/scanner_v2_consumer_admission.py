"""Read-only native consumer admission; use only this job's ephemeral read token."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

REPO = "Borduas-Holdings/Blazing-Back"
ROOT = "/repos/" + REPO
WORKFLOW = ".github/workflows/secret-scanning.yml"
PRODUCER = "Provision one owned native TruffleHog runner v2 / Provision scanner-v2 through acknowledged JIT"
PRODUCER_STEP = "Run the acknowledged scanner-v2 controller"
STEP = "Admit the current acknowledged scanner-v2 slot before task"


def require(value):
    if not value:
        raise ValueError("Native scanner-v2 consumer admission HOLD")


def positive(value):
    require(type(value) is int and 0 < value < 2**63)
    return value


def decimal(value):
    require(type(value) is str and re.fullmatch(r"[1-9][0-9]{0,18}", value) is not None)
    return positive(int(value))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NativeGET:
    def __init__(self, token):
        require(
            type(token) is str
            and 0 < len(token) <= 4096
            and all(33 <= ord(c) < 127 for c in token)
        )
        self.token = token
        self.calls = 0
        self.end = time.monotonic() + 30

    def __call__(self, path):
        require(
            re.fullmatch(
                re.escape(ROOT)
                + r"(?:|/actions/runs/[1-9][0-9]{0,18}/attempts/[1-9][0-9]{0,18}(?:/jobs\?per_page=100&page=[1-4])?|/actions/jobs/[1-9][0-9]{0,18})",
                path,
            )
        )
        self.calls += 1
        remaining = self.end - time.monotonic()
        require(self.calls <= 12 and remaining > 0)
        result = None
        try:
            request = urllib.request.Request(
                "https://api.github.com" + path,
                method="GET",
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            with urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirect()
            ).open(request, timeout=min(10, remaining)) as response:
                require(response.status == 200)
                raw = response.read(1048577)
            require(len(raw) <= 1048576 and time.monotonic() < self.end)

            def unique(pairs):
                value = {}
                for key, item in pairs:
                    require(key not in value)
                    value[key] = item
                return value

            result = json.loads(
                raw, object_pairs_hook=unique, parse_constant=lambda _: require(False)
            )
        except Exception:
            result = None
        require(type(result) is dict)
        return result


def admit(env, get):
    require(
        env.get("GITHUB_REPOSITORY") == REPO
        and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("GITHUB_JOB") == "owned_trufflehog_v2"
        and env.get("RUNNER_ENVIRONMENT") == "self-hosted"
    )
    run = decimal(env.get("GITHUB_RUN_ID", ""))
    attempt = decimal(env.get("GITHUB_RUN_ATTEMPT", ""))
    identity = decimal(env.get("ACKNOWLEDGED_RUNNER_ID", ""))
    source = env.get("GITHUB_WORKFLOW_SHA", "")
    sdk = env.get("SCANNER_PRODUCER_SDK_SHA", "")
    require(
        re.fullmatch(r"[0-9a-f]{40}", source)
        and re.fullmatch(r"[0-9a-f]{40}", sdk)
        and source == env.get("GITHUB_SHA")
    )
    require(
        env.get("GITHUB_WORKFLOW_REF") == REPO + "/" + WORKFLOW + "@refs/heads/main"
    )
    label = f"dfci-bb-th-{run}-a{attempt}-r1"
    placement = f"borduas-th-run-{run}-a{attempt}-r1-end"
    require(
        json.loads(env.get("ACKNOWLEDGED_TARGETS", ""))
        == ["self-hosted", "linux", "akash", label]
        and env.get("ACKNOWLEDGED_GROUP") == placement
    )
    require(re.fullmatch(r"[0-9a-f]{64}", env.get("ACKNOWLEDGED_JIT_RECORD_SHA", "")))
    repo = get(ROOT)
    require(
        type(repo.get("id")) is int
        and repo["id"] == 1071436278
        and repo.get("full_name") == REPO
        and repo.get("private") is True
    )
    context = get(ROOT + f"/actions/runs/{run}/attempts/{attempt}")
    require(
        positive(context.get("id")) == run
        and positive(context.get("run_attempt")) == attempt
        and context.get("head_sha") == source
        and context.get("head_branch") == "main"
        and context.get("path") == WORKFLOW
        and context.get("event") == "workflow_dispatch"
        and context.get("status") == "in_progress"
        and context.get("conclusion") is None
    )
    require(
        all(
            type(context.get(k)) is dict
            and type(context[k].get("id")) is int
            and context[k]["id"] == 1071436278
            for k in ("repository", "head_repository")
        )
    )
    refs = context.get("referenced_workflows")
    require(type(refs) is list)
    matches = [
        x
        for x in refs
        if type(x) is dict
        and x.get("path")
        == "Digital-Frontier-LDA/just-akash/.github/workflows/runner-pool.yml@" + sdk
    ]
    require(len(matches) == 1 and matches[0].get("sha") == sdk)
    jobs = []
    seen = set()
    total = None
    for page in range(1, 5):
        doc = get(
            ROOT
            + f"/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100&page={page}"
        )
        count, rows = doc.get("total_count"), doc.get("jobs")
        require(
            type(count) is int
            and 0 <= count <= 400
            and (total is None or count == total)
            and type(rows) is list
            and len(rows) <= 100
        )
        total = count
        for row in rows:
            require(type(row) is dict)
            number = positive(row.get("id"))
            require(number not in seen)
            seen.add(number)
            jobs.append(row)
        if len(jobs) == total:
            break
        require(len(rows) == 100 and len(jobs) < total)
    require(len(jobs) == total)
    own = (
        "Run native TruffleHog v2 and original scope oracles "
        + f"scanner-v2-{run}-a{attempt}-r1"
    )
    matches = [r for r in jobs if r.get("name") in (own, PRODUCER)]
    require(len(matches) == 2 and {row["name"] for row in matches} == {own, PRODUCER})
    for row in matches:

        def check(value):
            require(
                positive(value.get("id")) == row["id"]
                and positive(value.get("run_id")) == run
                and positive(value.get("run_attempt")) == attempt
                and value.get("head_sha") == source
                and value.get("name") == row["name"]
            )
            steps = value.get("steps")
            require(type(steps) is list)
            name = STEP if row["name"] == own else PRODUCER_STEP
            selected = [s for s in steps if type(s) is dict and s.get("name") == name]
            require(len(selected) == 1)
            if row["name"] == own:
                require(
                    value.get("status") == "in_progress"
                    and value.get("conclusion") is None
                    and type(value.get("runner_id")) is int
                    and value["runner_id"] == identity
                    and value.get("runner_group_name") == "bb-native-trufflehog-v2"
                    and positive(value.get("runner_group_id")) > 0
                )
                require(
                    selected[0].get("status") == "in_progress"
                    and selected[0].get("conclusion") is None
                )
            else:
                require(
                    value.get("status") == "completed"
                    and value.get("conclusion") == "success"
                    and selected[0].get("status") == "completed"
                    and selected[0].get("conclusion") == "success"
                    and positive(value.get("runner_id")) > 0
                    and value.get("labels") == ["ubuntu-latest"]
                )
            return value["id"], value["runner_id"]

        first = check(row)
        require(check(get(ROOT + f"/actions/jobs/{row['id']}")) == first)
    return identity


def main():
    require(len(sys.argv) == 1)
    result = None
    try:
        result = admit(os.environ, NativeGET(os.environ.get("GH_TOKEN", "")))
    except Exception:
        result = None
    if result is None:
        raise SystemExit("Native scanner-v2 current-slot consumer admission HOLD")
    print("Native acknowledged current slot admitted")


if __name__ == "__main__":
    main()
