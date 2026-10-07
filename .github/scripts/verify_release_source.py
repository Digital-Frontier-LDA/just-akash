#!/usr/bin/env python3
"""Read-only release fences for the exact reviewed source (Python 3.11+)."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlencode

import tomllib

EXPECTED_GATES = {
    "ci.yml": {
        "Ruff lint + format",
        "Pyright type check",
        "Unit tests",
        "E2E lease-shell transport",
        "E2E secrets injection",
    },
    "secrets.yml": {
        "Repo invariants (SOPS-only secrets, changelog)",
        "Gitleaks",
        "TruffleHog",
        "detect-secrets",
    },
    "security.yml": {"Semgrep SAST", "Dependency CVE audit"},
}


class ReleaseHeld(RuntimeError):
    """A fixed public refusal; never include transport output or credentials."""


def _command(args: tuple[str, ...], *, timeout: float) -> str:
    return subprocess.run(  # noqa: S603 — fixed argv git/gh reads, never a shell
        args, check=True, text=True, capture_output=True, timeout=timeout
    ).stdout.strip()


def verify_release_source(
    *,
    expected_source: str | None = None,
    recorded_evidence: dict | None = None,
    runner: Callable | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """Resolve the remote tag and recheck exact main push runs and attempts.

    Final publication supplies the initial source and evidence. Any moved tag,
    newer run/attempt, pending/failed/skipped job or incomplete population holds.
    """
    deadline = monotonic() + 180

    def command(*args: str) -> str:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ReleaseHeld("release source verification deadline exceeded")
        output = (runner or _command)(tuple(args), timeout=min(20, remaining))
        if monotonic() >= deadline or len(output.encode()) > 2_097_152:
            raise ReleaseHeld("release source verification bounds exceeded")
        return output

    def api(path: str) -> dict:
        value = json.loads(command("gh", "api", path))
        if not isinstance(value, dict):
            raise ReleaseHeld("release source document is malformed")
        return value

    repo, tag = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_REF_NAME"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ReleaseHeld("release repository identity is invalid")
    metadata = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    version = metadata["version"]
    if (
        metadata["name"] != "just-akash"
        or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag)
        or tag != "v" + version
    ):
        raise ReleaseHeld("release tag/project identity/version mismatch")
    source = command("git", "rev-parse", "HEAD")
    if (
        re.fullmatch(r"[0-9a-f]{40}", source) is None
        or expected_source is not None
        and expected_source != source
    ):
        raise ReleaseHeld("release checkout differs from the recorded source")
    if source != command("git", "rev-parse", tag + "^{commit}"):
        raise ReleaseHeld("checkout is not the exact local tag commit")
    remote = api(f"repos/{repo}/git/ref/tags/{tag}")["object"]
    visited = set()
    for _ in range(8):
        if (
            not isinstance(remote, dict)
            or re.fullmatch(r"[0-9a-f]{40}", str(remote.get("sha"))) is None
        ):
            raise ReleaseHeld("remote tag object is malformed")
        if remote.get("type") == "commit":
            break
        if remote.get("type") != "tag" or remote["sha"] in visited:
            raise ReleaseHeld("remote tag cannot be completely peeled")
        visited.add(remote["sha"])
        remote = api(f"repos/{repo}/git/tags/{remote['sha']}")["object"]
    else:
        raise ReleaseHeld("remote tag peeling bound exceeded")
    if remote["sha"] != source:
        raise ReleaseHeld("remote tag moved away from the reviewed source")
    main = api(f"repos/{repo}/git/ref/heads/main")["object"]["sha"]
    if not isinstance(main, str) or re.fullmatch(r"[0-9a-f]{40}", main) is None:
        raise ReleaseHeld("main identity is malformed")
    command("git", "fetch", "--no-tags", "origin", main)
    command("git", "merge-base", "--is-ancestor", source, main)
    evidence = []
    for workflow, names in EXPECTED_GATES.items():
        query = urlencode({"head_sha": source, "event": "push", "branch": "main", "per_page": 100})
        runs = api(f"repos/{repo}/actions/workflows/{workflow}/runs?{query}")
        population, total = runs.get("workflow_runs"), runs.get("total_count")
        if (
            not isinstance(population, list)
            or type(total) is not int
            or not 1 <= total <= 100
            or len(population) != total
        ):
            raise ReleaseHeld("missing or incomplete exact source runs")
        run = max(population, key=lambda item: item["id"])
        if (
            run["head_sha"] != source
            or run["head_branch"] != "main"
            or run["event"] != "push"
            or run["status"] != "completed"
            or run["conclusion"] != "success"
        ):
            raise ReleaseHeld("latest exact source push run is not successful")
        jobs = api(
            f"repos/{repo}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100"
        )
        population, total = jobs.get("jobs"), jobs.get("total_count")
        if (
            not isinstance(population, list)
            or type(total) is not int
            or not 1 <= total <= 100
            or len(population) != total
        ):
            raise ReleaseHeld("incomplete exact source job population")
        for name in names:
            matching = [job for job in population if job["name"] == name]
            if (
                len(matching) != 1
                or matching[0]["head_sha"] != source
                or matching[0]["status"] != "completed"
                or matching[0]["conclusion"] != "success"
            ):
                raise ReleaseHeld("required exact source gate is not successful")
        evidence.append(
            {
                "workflow": workflow,
                "run_id": run["id"],
                "attempt": run["run_attempt"],
                "head_sha": source,
            }
        )
    if recorded_evidence is not None and (
        recorded_evidence.get("tag") != tag
        or recorded_evidence.get("source") != source
        or recorded_evidence.get("gates") != evidence
    ):
        raise ReleaseHeld("recorded source run or attempt changed before publication")
    try:
        api(f"repos/{repo}/releases/tags/{tag}")
    except subprocess.CalledProcessError as error:
        if "(HTTP 404)" not in (error.stderr or ""):
            raise ReleaseHeld("release absence could not be established") from None
    else:
        raise ReleaseHeld("release already exists")
    return {"tag": tag, "version": version, "source": source, "main": main, "gates": evidence}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-source")
    parser.add_argument("--recorded-evidence", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--write-env", action="store_true")
    args = parser.parse_args()
    try:
        recorded = (
            json.loads(args.recorded_evidence.read_text()) if args.recorded_evidence else None
        )
        evidence = verify_release_source(
            expected_source=args.expected_source, recorded_evidence=recorded
        )
    except Exception:  # noqa: BLE001 — fail closed without raw remote/credential output
        print(
            "::error::Release held: exact source, remote tag or complete CI evidence "
            "could not be verified"
        )
        return 1
    if args.output:
        args.output.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n")
    if args.write_env:
        with open(os.environ["GITHUB_ENV"], "a") as output:
            output.write(
                f"RELEASE_VERSION={evidence['version']}\nRELEASE_SOURCE={evidence['source']}\n"
            )
    print(
        "Release source fence passed for the exact remote tag and successful recorded CI attempts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
