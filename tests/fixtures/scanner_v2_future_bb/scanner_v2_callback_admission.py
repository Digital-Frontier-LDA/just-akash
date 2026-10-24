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


def admit(env, event, get):
    require(
        env.get("GITHUB_REPOSITORY") == REPO
        and env.get("GITHUB_EVENT_NAME") == "workflow_run"
        and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("RUNNER_ENVIRONMENT") == "github-hosted"
    )
    require(
        env.get("GITHUB_WORKFLOW_REF")
        == REPO + "/.github/workflows/native-scanner-v2-cleanup.yml@refs/heads/main"
    )
    require(type(event) is dict and event.get("action") == "completed")
    root = event.get("workflow_run")
    require(type(root) is dict)
    run = positive(root.get("id"))
    attempt = positive(root.get("run_attempt"))
    repo = get(ROOT)
    require(
        repo.get("id") == 1071436278
        and type(repo.get("id")) is int
        and repo.get("private") is True
        and repo.get("full_name") == REPO
    )
    native = get(ROOT + f"/actions/runs/{run}/attempts/{attempt}")
    require(
        positive(native.get("id")) == run
        and positive(native.get("run_attempt")) == attempt
        and native.get("status") == "completed"
        and native.get("conclusion") in ("success", "failure", "cancelled", "timed_out")
        and native.get("path") == WORKFLOW
        and native.get("event") == "workflow_dispatch"
        and native.get("head_branch") == "main"
    )
    require(
        all(
            type(native.get(k)) is dict
            and type(native[k].get("id")) is int
            and native[k]["id"] == 1071436278
            for k in ("repository", "head_repository")
        )
    )
    rows = []
    seen = set()
    total = None
    for page in range(1, 5):
        doc = get(
            ROOT
            + f"/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100&page={page}"
        )
        count, items = doc.get("total_count"), doc.get("jobs")
        require(
            type(count) is int
            and 0 <= count <= 400
            and (total is None or total == count)
            and type(items) is list
            and len(items) <= 100
        )
        total = count
        for row in items:
            require(type(row) is dict)
            number = positive(row.get("id"))
            require(number not in seen)
            seen.add(number)
            rows.append(row)
        if len(rows) == total:
            break
        require(len(items) == 100 and len(rows) < total)
    require(len(rows) == total)
    selected = [row for row in rows if row.get("name") == PRODUCER]
    require(len(selected) <= 1)
    # Complete native absence/skipped job is NOT cleanup or artifact authority.
    if not selected:
        return False
    row = selected[0]
    require(
        positive(row.get("run_id")) == run
        and positive(row.get("run_attempt")) == attempt
        and row.get("head_sha") == native.get("head_sha")
    )
    if row.get("status") == "completed" and row.get("conclusion") == "skipped":
        return False
    direct = get(ROOT + f"/actions/jobs/{positive(row.get('id'))}")
    for value in (row, direct):
        require(
            positive(value.get("id")) == row["id"]
            and positive(value.get("run_id")) == run
            and positive(value.get("run_attempt")) == attempt
            and value.get("head_sha") == native.get("head_sha")
            and value.get("name") == PRODUCER
        )
        require(
            value.get("status") == "completed"
            and value.get("conclusion")
            in ("success", "failure", "cancelled", "timed_out")
            and positive(value.get("runner_id")) > 0
            and type(value.get("started_at")) is str
            and bool(value["started_at"])
        )
        steps = value.get("steps")
        require(type(steps) is list)
        step = [x for x in steps if type(x) is dict and x.get("name") == PRODUCER_STEP]
        require(len(step) == 1)
        # Failed/cancelled actual controller may have durable known identities.
        require(
            step[0].get("status") == "completed"
            and step[0].get("conclusion")
            in ("success", "failure", "cancelled", "timed_out")
        )
    return True


def main():
    require(len(sys.argv) == 1)
    value = None
    try:
        import stat

        fd = os.open(
            os.environ["GITHUB_EVENT_PATH"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        try:
            info = os.fstat(fd)
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and 0 < info.st_size <= 1048576
            )
            raw = os.read(fd, 1048577)
            require(len(raw) == info.st_size)
        finally:
            os.close(fd)
        value = admit(
            os.environ, json.loads(raw), NativeGET(os.environ.get("GH_TOKEN", ""))
        )
    except Exception:
        value = None
    if value is None:
        raise SystemExit("Native scanner-v2 callback target HOLD")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("eligible=" + str(value).lower() + "\n")


if __name__ == "__main__":
    main()
