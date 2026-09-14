"""Read-only retirement policy for the stale-deployment reaper.

The policy accepts one of a finite set of cleanup intents.  A service name, age,
repository prefix, or generic truthy value is never an intent.  Versioned CI
identities additionally need fresh run/attempt completion.  The three explicitly
named legacy test roles are migration exceptions and require a single complete
group whose exact placement identity agrees with the intent.  Everything else,
including idv2 until its operation-aware reader lands, remains held.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from enum import Enum
from urllib.parse import urlencode, urlsplit

from . import chain
from .workload_identity import classify_groups

MAX_DSEQ = 2**64 - 1


class CleanupIntent(Enum):
    """The complete set of intents this reaper knows how to authorize."""

    PROBE = "stale-probe"
    BACKTEST = "stale-backtest"
    RUNNER = "stale-runner"
    OWNED_CI = "stale-owned-ci"
    PROVIDER_CLOSED_CI = "stale-provider-closed-ci"


_VERSIONED_CLASSES = {
    CleanupIntent.PROBE: frozenset({"ci-payload"}),
    CleanupIntent.BACKTEST: frozenset({"ci-payload"}),
    CleanupIntent.RUNNER: frozenset({"ci-runner"}),
    CleanupIntent.OWNED_CI: frozenset({"ci-payload"}),
    CleanupIntent.PROVIDER_CLOSED_CI: frozenset({"ci-runner", "ci-payload"}),
}
_LEGACY_ROLES = {
    CleanupIntent.PROBE: "probe",
    CleanupIntent.BACKTEST: "backtest",
    CleanupIntent.RUNNER: "runner",
}


def _canonical_dseq(value: object) -> str | None:
    """Return a canonical Akash uint64 DSEQ, never an unbounded decimal."""

    if (
        not isinstance(value, str)
        or not value.isascii()
        or not re.fullmatch(r"[1-9][0-9]{0,19}", value)
    ):
        return None
    return value if int(value) <= MAX_DSEQ else None


def _legacy_name(prefix: str, intent: CleanupIntent, name: object) -> bool:
    """Whether one legacy single-group name declares exactly ``intent``.

    Legacy writers emitted an exact workload key, optionally followed by one of
    the two run stamps used by this repository.  Prefix-only matches are rejected:
    ``just-akash-research`` is attributable to the repo but is not a disposable
    ``just-akash-backtest`` deployment.
    """

    role = _LEGACY_ROLES.get(intent)
    if role is None or not isinstance(name, str):
        return False
    stem = prefix if prefix.endswith(("-", ".")) else prefix + "-"
    base = re.escape(stem + role)
    return bool(
        re.fullmatch(
            rf"{base}(?:\.[a-f0-9]{{6,32}}|-run-[1-9][0-9]{{0,31}}-end)?",
            name,
        )
    )


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


def agreeing_group_names(owner: str, dseq: str) -> dict[str, str] | None:
    """Two distinct HTTPS hosts must agree on all group IDs/names for this owner/dseq."""
    if (
        not isinstance(owner, str)
        or not owner.isascii()
        or not re.fullmatch(r"akash1[a-z0-9]{38,58}", owner)
        or _canonical_dseq(dseq) is None
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
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username
            or parsed.password
            or hostname in hosts
        ):
            continue
        hosts.add(hostname)
        try:
            doc = chain._lcd_get(path, base=base)
            # deployments/info is an exact, non-paginated read.  Pagination on
            # this endpoint means the response contract changed; treating that
            # first page as complete could authorize from a strict subset.
            if "pagination" in doc:
                return None
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
                gid = group["id"]
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
                names[str(int(gseq))] = name
            snapshots.append(names)
        except RuntimeError:
            continue
        except (KeyError, TypeError, ValueError):
            return None
        if len(snapshots) == 2:
            return names if snapshots[0] == snapshots[1] else None
    return None


def eligible(
    owner: str,
    dseq: str,
    prefix: str,
    register: dict | None,
    intent: CleanupIntent,
) -> tuple[bool, str]:
    """Apply the finite stale-reaper policy to one exact owner/DSEQ subject."""
    try:
        if not isinstance(intent, CleanupIntent):
            return False, "missing or unsupported typed cleanup intent"
        if _canonical_dseq(dseq) is None:
            return False, "deployment DSEQ is not a canonical Akash uint64"
        if not isinstance(register, dict) or prefix not in register:
            return False, "missing ownership register or requested prefix"
        names = agreeing_group_names(owner, dseq)
        expected_population = [
            {"gseq": int(gseq), "name": name} for gseq, name in (names or {}).items()
        ]
        if names is None or not chain.owner_close_population_evidence(
            owner, dseq, expected_population
        ):
            return False, "complete signed creation population is unreadable or disagrees"
        population = classify_groups(list(names.values()) if names else None, register)
        if population.held:
            # A bounded migration path for the three historical disposable
            # identities.  It is deliberately single-group: legacy names carry
            # no gseq, so a multi-group population cannot prove group identity.
            if (
                names is not None
                and list(names) == ["1"]
                and _legacy_name(prefix, intent, names["1"])
            ):
                return True, f"exact legacy {intent.value} single-group identity"
            return False, population.reason
        if names is None or any(
            identity.group != int(gseq)
            for gseq, identity in zip(names, population.identities, strict=True)
        ):
            return False, "embedded group identity disagrees with chain group ID"
        identity = population.identities[0]
        if identity.prefix != prefix:
            return False, "different registered repository namespace"
        if identity.workload_class not in _VERSIONED_CLASSES[intent]:
            return False, "workload class is protected or disagrees with cleanup intent"
        if identity.run is None or identity.attempt is None:
            return False, "CI identity is missing its run or attempt"
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
