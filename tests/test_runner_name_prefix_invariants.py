"""The backstop's configured prefix must match what the SDLs actually emit.

A de-registration backstop can only scope itself by runner NAME — GitHub cannot filter
runners by label. So the name prefix is a load-bearing contract between two files that
have no other connection, and nothing but this module makes them move together.

⛔ THE FAILURE THIS EXISTS TO CATCH IS A HALF-APPLIED RENAME. Rename one emitter and not
the other, or rename the emitters and not the reaper's `name-prefixes`, and the reaper
matches nothing, deletes nothing, and reports success. That is strictly worse than having
no backstop, because it reads as coverage. The same defect was caught twice by hand
elsewhere in the fleet before it was made mechanical.

⛔ AND THE PREFIX MUST NOT COLLIDE WITH A SIBLING'S, IN EITHER DIRECTION. If ours were a
proper prefix of theirs, our reaper would delete their runners. If theirs were a proper
prefix of ours, theirs would delete ours. Both are silent — the victim sees runners vanish
and blames the provider.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
REAPER = REPO / ".github" / "workflows" / "reap-stale-runners.yml"

OWNED_PREFIX = "just-akash-"

# Prefixes owned by OTHER repos that register into the same org(s).
FOREIGN_PREFIXES = ("df-core-", "df-flow-", "df-cicd-", "akash-", "akash-ci-", "akash-integration-")

# `RUNNER_NAME_PREFIX=<literal>${VAR}` (SDL) or `"RUNNER_NAME_PREFIX": f"<literal>{var}"` (python).
EMITTERS = (
    (Path(".github/workflows/runner-pool.yml"), re.compile(r"RUNNER_NAME_PREFIX=([A-Za-z0-9._-]*)")),
    (Path("just_akash/runner_probe.py"), re.compile(r'"RUNNER_NAME_PREFIX":\s*f?"([A-Za-z0-9._-]*)')),
)


def _emitted_prefixes() -> list[tuple[str, str]]:
    """(file, literal prefix) for every site that sets a runner name prefix.

    Template placeholders (`{{RUNNER_NAME_PREFIX}}`) are NOT emitters — they are filled in
    by one of the emitters above — and the regexes exclude them by requiring the literal
    to be plain name characters.
    """
    found: list[tuple[str, str]] = []
    for rel, pattern in EMITTERS:
        path = REPO / rel
        assert path.exists(), f"{rel} is gone — this test is now blind to one emitter"
        for m in pattern.finditer(path.read_text()):
            literal = m.group(1)
            if literal:
                found.append((str(rel), literal))
    return found


def _reaper_job() -> dict:
    doc = yaml.safe_load(REAPER.read_text())
    jobs = doc.get("jobs") or {}
    assert jobs, "the reaper declares no jobs — the backstop is not wired"
    return next(iter(jobs.values()))


def _configured_prefixes() -> list[str]:
    raw = _reaper_job()["with"]["name-prefixes"]
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    assert parts, "name-prefixes is blank — the reusable workflow fails closed on this, but so should we"
    return parts


# --------------------------------------------------------------------------- #
# Non-vacuity first: a green over an empty subject is not evidence.
# --------------------------------------------------------------------------- #


def test_the_emitters_are_actually_found() -> None:
    emitted = _emitted_prefixes()
    assert len(emitted) >= 2, (
        f"expected at least two name-prefix emitters (the pool SDL and the probe), found {emitted}. "
        "If an emitter moved, every assertion below passes over an empty set."
    )


# --------------------------------------------------------------------------- #
# The contract.
# --------------------------------------------------------------------------- #


def test_every_emitted_prefix_is_covered_by_the_backstop() -> None:
    """The half-applied rename. Rename an emitter and the reaper stops matching it."""
    configured = _configured_prefixes()
    for where, emitted in _emitted_prefixes():
        assert any(emitted.startswith(c) for c in configured), (
            f"{where} emits runner names beginning {emitted!r}, which no configured "
            f"name-prefix {configured} matches. The reaper would find nothing and report success."
        )


def test_the_backstop_claims_nothing_it_does_not_emit() -> None:
    """The other direction: a configured prefix nobody emits is dead scope, and if it
    belongs to a sibling it is worse than dead."""
    emitted = [e for _, e in _emitted_prefixes()]
    for c in _configured_prefixes():
        assert any(e.startswith(c) for e in emitted), (
            f"name-prefixes claims {c!r} but no emitter in this repo produces it"
        )


@pytest.mark.parametrize("foreign", FOREIGN_PREFIXES)
def test_no_sibling_prefix_collision_in_either_direction(foreign: str) -> None:
    assert not OWNED_PREFIX.startswith(foreign), (
        f"{foreign!r} is a prefix of ours — THEIR reaper would delete OUR runners"
    )
    assert not foreign.startswith(OWNED_PREFIX), (
        f"ours is a prefix of {foreign!r} — OUR reaper would delete THEIR runners"
    )


def test_the_owned_prefix_is_what_is_configured() -> None:
    assert _configured_prefixes() == [OWNED_PREFIX], (
        "this module's sibling-collision check is written against OWNED_PREFIX; if the "
        "workflow's name-prefixes diverges from it, the collision check stops covering "
        "what actually ships"
    )


# --------------------------------------------------------------------------- #
# The pin.
# --------------------------------------------------------------------------- #


def test_the_backstop_is_pinned_to_an_immutable_commit() -> None:
    uses = _reaper_job()["uses"]
    ref = uses.split("@", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{40}", ref), (
        f"the backstop is pinned to {ref!r}. A tag or branch can move, and a de-registration "
        "loop whose behaviour changes without a commit here is not a backstop."
    )


def test_the_reaper_is_callable_and_takes_the_org_from_its_caller() -> None:
    """This repo holds no PAT and cannot know the org — so it must not pretend to.

    `.github/scripts/check_repo_invariants.py` states the policy: "a caller brings its own
    Akash account and its own runner PAT, and this repo must never hold either". A cron
    here would run with an empty secret against a guessed org: a backstop that reaps
    nothing and reports success. The CALLER schedules it, as it already does for
    runner-pool.yml and runner-teardown.yml.
    """
    doc = yaml.safe_load(REAPER.read_text())
    # YAML 1.1 parses a bare `on:` as the boolean True.
    triggers = doc.get("on") or doc.get(True) or {}
    assert "workflow_call" in triggers, "the reaper is not adoptable — callers cannot reach it"
    assert "schedule" not in triggers, (
        "a cron here would run with no PAT and a guessed org — see this test's docstring"
    )
    assert "github-org" in (triggers["workflow_call"].get("inputs") or {}), (
        "the org must come from the caller; there is none this repo could hard-code correctly"
    )
    org = _reaper_job()["with"]["org"]
    assert "inputs.github-org" in str(org), f"org is not caller-supplied: {org!r}"
