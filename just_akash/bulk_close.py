"""Scope for a bulk close: only what this run created, or an explicit allowlist (just-akash-73o).

On 2026-11-19 at 13:53-13:54Z one client closed EVERY deployment on the shared console-2
account, Flynn's four prod leases included. `destroy-all` was exactly that shape: list once,
DELETE each, no scope. Fleet rule (GA, 2026-11-19): no agent or agent-launched job runs an
unscoped bulk close. So a bulk close here has no unscoped mode at all:

* CLOSE   a dseq whose group name carries THIS run's id (`just-akash-<x>.<run_id>`), or
          one named on the explicit allowlist;
* REFUSE  a dseq with a Flynn prod placement (`flynn-*`, `fleet-*`, `dcloud`, `akash`)
          or on `JUST_AKASH_PROTECTED_DSEQS`, EVEN WHEN ALLOWLISTED; and an allowlisted
          dseq whose group names cannot be read (its protection cannot be confirmed);
* SKIP    everything else, with the reason.

Refused dseqs can be closed only with `override_protected`, which is for a human at a
terminal: it raises PermissionError under CI, GitHub Actions, an agent, or without a TTY.
Even then it never widens past the allowlist.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .provenance import PLACEMENT_PREFIX, RUN_SEPARATOR, is_ours_placement, run_id_of

BEAD = "just-akash-73o"
PROTECTED_DSEQS_ENV = "JUST_AKASH_PROTECTED_DSEQS"
# Any of these set means "not a human at a terminal": the override is refused.
NON_HUMAN_ENV = ("CI", "GITHUB_ACTIONS", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX")

CLOSE, SKIP, REFUSE = "close", "skip", "refuse"


@dataclass(frozen=True)
class PlanItem:
    dseq: str
    action: str
    reason: str
    groups: tuple[str, ...]


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def override_refusal() -> str | None:
    """Why the protection override is refused here, or None for a human at a terminal."""
    for name in NON_HUMAN_ENV:
        if os.environ.get(name, "").strip():
            return (
                f"{name} is set: the protection override is human-only "
                "and never runs in CI or from an agent"
            )
    if not _stdin_is_tty():
        return "stdin is not a terminal: the protection override is human-only"
    return None


def valid_run_tag(run_tag: str) -> bool:
    return bool(run_tag) and run_id_of(f"{PLACEMENT_PREFIX}x{RUN_SEPARATOR}{run_tag}") == run_tag


def protected_dseqs_from_env() -> frozenset[str]:
    raw = os.environ.get(PROTECTED_DSEQS_ENV, "")
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def parse_dseq_list(values: Iterable[str] | None) -> list[str]:
    """`--allow-dseq` is repeatable and comma-separated; dseqs are digits only."""
    out: list[str] = []
    for value in values or ():
        for part in str(value).split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit():
                raise ValueError(f"not a dseq: {part!r}")
            if part not in out:
                out.append(part)
    return out


def group_names(doc: Any) -> tuple[str, ...]:
    """Every `group_spec.name` in a Console/chain deployment document, wherever it sits."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            spec = node.get("group_spec")
            if isinstance(spec, dict) and isinstance(spec.get("name"), str):
                found.append(spec["name"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc)
    return tuple(dict.fromkeys(found))


def plan_bulk_close(
    deployments: Iterable[dict[str, Any]],
    *,
    run_tag: str | None,
    allow_dseqs: Iterable[str],
    protected_dseqs: Iterable[str] = (),
    override_protected: bool = False,
) -> list[PlanItem]:
    from .api import _extract_dseq

    allow = set(allow_dseqs)
    protected = set(protected_dseqs)
    plan: list[PlanItem] = []
    for doc in deployments:
        dseq = _extract_dseq(doc)
        if not dseq:
            continue
        names = group_names(doc)
        prod = [n for n in names if is_ours_placement(n)]
        own_run = bool(run_tag) and any(run_id_of(n) == run_tag for n in names)
        allowlisted = dseq in allow

        refusal = ""
        if prod:
            refusal = f"prod placement {prod[0]!r}"
        elif dseq in protected:
            refusal = f"on {PROTECTED_DSEQS_ENV}"
        elif allowlisted and not names:
            refusal = "group names unreadable: protection cannot be confirmed"

        if refusal and (own_run or allowlisted):
            if override_protected and allowlisted:
                plan.append(
                    PlanItem(dseq, CLOSE, f"allowlisted; protection overridden ({refusal})", names)
                )
            else:
                plan.append(PlanItem(dseq, REFUSE, refusal, names))
        elif refusal:
            plan.append(
                PlanItem(dseq, REFUSE if prod or dseq in protected else SKIP, refusal, names)
            )
        elif allowlisted:
            plan.append(PlanItem(dseq, CLOSE, "allowlisted", names))
        elif own_run:
            plan.append(PlanItem(dseq, CLOSE, f"created by run {run_tag}", names))
        elif any(run_id_of(n) for n in names):
            plan.append(PlanItem(dseq, SKIP, "another run's deployment", names))
        elif any(n.startswith(PLACEMENT_PREFIX) for n in names):
            plan.append(PlanItem(dseq, SKIP, "this repo's, but not run-attributable", names))
        else:
            plan.append(PlanItem(dseq, SKIP, "not this run's and not allowlisted", names))
    return plan


def format_plan(plan: list[PlanItem]) -> str:
    if not plan:
        return "  (no deployments listed)"
    rows = [
        f"  {p.action.upper():<7} {p.dseq:<16} {','.join(p.groups) or '-':<40} {p.reason}"
        for p in plan
    ]
    return "\n".join(rows)


UNSCOPED_MESSAGE = (
    "Refusing an unscoped bulk close: pass --run-tag <run id> and/or --allow-dseq <dseq>. "
    "Closing every deployment on a shared account closed Flynn's prod leases "
    f"on 2026-11-19 ({BEAD}); "
    "the fleet rule forbids an unscoped bulk close."
)
