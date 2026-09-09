"""Read-only retirement eligibility for versioned CI identities.

Legacy, staging and production populations remain held. This protects only callers
that invoke this gate; direct CLI, orphan and deploy recovery integration is pending.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from urllib.parse import urlencode, urlsplit

from . import chain
from .workload_identity import classify_groups


def completed_run(repository: str, run: int) -> dict | None:
    """Fresh authenticated GitHub lookup, never a cache or anonymous fallback."""
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or type(run) is not int
        or run <= 0
    ):
        return None
    executable = shutil.which("gh")
    if executable is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 — resolved executable, fixed argv, validated path components
            [executable, "api", f"repos/{repository}/actions/runs/{run}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            return None
        value = json.loads(result.stdout)
        return value if isinstance(value, dict) else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def agreeing_group_names(owner: str, dseq: str) -> list[str] | None:
    """Two distinct HTTPS hosts must agree on all group IDs/names for this owner/dseq."""
    if not re.fullmatch(r"akash1[a-z0-9]{38,58}", owner) or not re.fullmatch(
        r"[1-9][0-9]{0,31}", dseq
    ):
        return None
    path = "/akash/deployment/v1beta4/deployments/info?" + urlencode(
        {"id.owner": owner, "id.dseq": dseq}
    )
    snapshots = []
    hosts = set()
    try:
        endpoints = chain.rest_urls()
    except (RuntimeError, ValueError):
        return None
    for base in endpoints:
        try:
            parsed = urlsplit(base)
        except (TypeError, ValueError):
            continue
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.hostname in hosts
        ):
            continue
        hosts.add(parsed.hostname)
        try:
            doc = chain._lcd_get(path, base=base)
            deployment = doc["deployment"]
            if (
                deployment["id"] != {"owner": owner, "dseq": dseq}
                or deployment["state"] != "active"
            ):
                return None
            groups = doc["groups"]
            if not isinstance(groups, list) or not groups:
                return None
            names = {}
            for group in groups:
                gid = group["group_id"]
                gseq = gid["gseq"]
                name = group["group_spec"]["name"]
                if (
                    gid["owner"] != owner
                    or str(gid["dseq"]) != dseq
                    or isinstance(gseq, bool)
                    or not re.fullmatch(r"[1-9][0-9]{0,31}", str(gseq))
                    or str(gseq) in names
                    or not isinstance(name, str)
                    or not name
                ):
                    return None
                names[str(gseq)] = name
            snapshots.append(names)
        except RuntimeError:
            continue
        except (KeyError, TypeError, ValueError):
            return None
        if len(snapshots) == 2:
            return list(names.values()) if snapshots[0] == snapshots[1] else None
    return None


def eligible(owner: str, dseq: str, prefix: str, register: dict | None) -> tuple[bool, str]:
    """Classification plus fresh same-attempt completion; no age-based authorization."""
    try:
        if not isinstance(register, dict) or prefix not in register:
            return False, "missing ownership register or requested prefix"
        names = agreeing_group_names(owner, dseq)
        population = classify_groups(names, register)
        if population.held:
            return False, population.reason
        identity = population.identities[0]
        if identity.prefix != prefix:
            return False, "different registered repository namespace"
        if identity.workload_class not in {"ci-runner", "ci-payload"}:
            return False, "protected staging or production class"
        state = completed_run(identity.owner, identity.run)
        if not state or state.get("repository", {}).get("full_name") != identity.owner:
            return False, "owning repository run unreadable or mismatched"
        if (
            type(state.get("id")) is not int
            or state["id"] != identity.run
            or type(state.get("run_attempt")) is not int
            or state["run_attempt"] != identity.attempt
            or state.get("status") != "completed"
        ):
            return False, "run or attempt differs, is unreadable, or remains live"
        return True, "same owning run and attempt completed"
    except (TypeError, ValueError, AttributeError):
        return False, "invalid ownership register or response"
