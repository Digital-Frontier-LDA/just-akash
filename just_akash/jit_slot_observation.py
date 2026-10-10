"""Read the exact returned JIT slot before routing one job to its unique label.

This is current GitHub observation data, not producer authentication, admission,
Akash allocation or publication authority. The caller must join its durable mint
and delivery records with the actual lease and completed job/artifact. Execute
under the original whole-route hard supervisor: checking its inherited deadline
between requests cannot interrupt a blocked HTTP read or make reads atomic.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from .github_jit import JitHandoff, JitHold, JitPolicy, github_request, verify_group_policy

_DEFAULTS = {"self-hosted", "linux", "x64"}


@dataclass(frozen=True)
class SlotObservation:
    """Non-secret observation of one exact ready registration, never a permit."""

    runner_id: int
    runner_name: str
    group_id: int
    policy_revision: str
    labels: tuple[tuple[int, str, str], ...]
    observed_monotonic: float


def _runner(row: dict, handoff: JitHandoff) -> tuple[tuple[int, str, str], ...]:
    if (
        not isinstance(row, dict)
        or type(row.get("id")) is not int
        or row["id"] != handoff.runner_id
        or row.get("name") != handoff.runner_name
        or row.get("os") != "linux"
        or row.get("status") != "online"
        or row.get("busy") is not False
    ):
        raise JitHold("exact JIT runner is not ready")
    labels = row.get("labels")
    if not isinstance(labels, list) or not 1 <= len(labels) <= 103:
        raise JitHold("incomplete JIT runner labels")
    observed, identities = {}, set()
    expected = {label.casefold() for label in handoff.labels}
    for label in labels:
        if (
            not isinstance(label, dict)
            or type(label.get("id")) is not int
            or label["id"] <= 0
            or label["id"] in identities
            or not isinstance(label.get("name"), str)
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", label["name"]) is None
            or label["name"].casefold() in observed
            or label.get("type") not in {"custom", "read-only"}
        ):
            raise JitHold("invalid or duplicate JIT runner label")
        name = label["name"].casefold()
        if label["type"] == "read-only" and name not in _DEFAULTS:
            raise JitHold("JIT runner platform label differs")
        if name not in expected and not (label["type"] == "read-only" and name in _DEFAULTS):
            raise JitHold("unexpected JIT runner label")
        identities.add(label["id"])
        observed[name] = (label["id"], label["name"], label["type"])
    if not expected <= observed.keys():
        raise JitHold("missing declared JIT runner label")
    return tuple(observed[name] for name in sorted(observed))


def observe_ready_jit_slot(
    policy: JitPolicy,
    handoff: JitHandoff,
    installation_token: str,
    *,
    deadline: float,
    request: Callable[..., dict] = github_request,
) -> SlotObservation:
    """Require two complete one-slot populations and exact numeric readbacks.

    Retain the caller's original monotonic deadline, never a new 600-second
    allowance per observation. No mutation, wait/poll loop or cached readiness
    shortcut exists here. A later job can still race these read-only observations.
    """
    now = time.monotonic()
    if (
        not isinstance(policy, JitPolicy)
        or not isinstance(handoff, JitHandoff)
        or type(handoff.runner_id) is not int
        or handoff.runner_id <= 0
        or handoff.group_id != policy.group_id
        or type(handoff.group_id) is not int
        or handoff.policy_revision != policy.revision
        or not isinstance(handoff.runner_name, str)
        or re.fullmatch(r"dfci-[a-z0-9-]{1,90}", handoff.runner_name) is None
        or type(handoff.labels) is not tuple
        or not 1 <= len(handoff.labels) <= 100
        or any(
            not isinstance(label, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", label) is None
            for label in handoff.labels
        )
        or len({label.casefold() for label in handoff.labels}) != len(handoff.labels)
        or handoff.runner_name not in handoff.labels
        or type(deadline) not in (int, float)
        or not now < deadline <= now + 600
    ):
        raise JitHold("invalid JIT slot observation input")

    def read(method: str, path: str, token: str, body: dict | None = None) -> dict:
        if method != "GET" or body is not None or time.monotonic() >= deadline:
            raise JitHold("JIT slot observation deadline or method refused")
        result = request(method, path, token)
        if time.monotonic() >= deadline or not isinstance(result, dict):
            raise JitHold("JIT slot observation unavailable before deadline")
        return result

    def sample() -> tuple[tuple[int, str, str], ...]:
        verify_group_policy(policy, installation_token, request=read)
        path = f"{policy.api_root}/runner-groups/{policy.group_id}/runners"
        measured = None
        for page in (1, 2):
            document = read("GET", path + f"?per_page=100&page={page}", installation_token)
            rows = document.get("runners")
            if (
                type(document.get("total_count")) is not int
                or document["total_count"] != 1
                or not isinstance(rows, list)
                or len(rows) != (1 if page == 1 else 0)
            ):
                raise JitHold("incomplete exact one-slot group population")
            if page == 1:
                measured = _runner(rows[0], handoff)
        direct = read("GET", f"{policy.api_root}/runners/{handoff.runner_id}", installation_token)
        if measured is None or _runner(direct, handoff) != measured:
            raise JitHold("JIT runner readback differs from its group population")
        return measured

    try:
        first, second = sample(), sample()
        observed = time.monotonic()
        if first != second or observed >= deadline:
            raise JitHold("JIT slot population changed during observation")
        return SlotObservation(
            handoff.runner_id,
            handoff.runner_name,
            policy.group_id,
            policy.revision,
            second,
            observed,
        )
    except Exception:
        # Transport/schema failures must not echo an installation token or body.
        raise JitHold("exact JIT slot readiness is unverified") from None
