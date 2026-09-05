"""A `?name=` filter on the org runner listing is EXACT-MATCH, so a prefix returns zero.

★ MEASURED 2026-09-05 against the live org:

    ?name=<exact full runner name>   -> 1
    ?name=<a true prefix of it>      -> 0
    unfiltered                       -> 1683

Runner names are `<RUNNER_NAME_PREFIX>-<random suffix>` — the image randomises the
suffix per replica — so NO prefix query can ever match one. GitHub does not support
server-side label filtering either, which is why this workflow pages the whole listing
and matches client-side on `.labels[].name`.

⛔ WHY THIS IS PINNED RATHER THAN COMMENTED. `?name=` looks like an obvious quota
optimisation: this poll pages the entire org listing every 5 seconds, and the file
documents that budget as its binding constraint. Someone WILL reach for it. And the
failure is silent and inverted — the query returns 0 rows, the landing gate sees an
empty projection, and (correctly, by its own rule) treats a read disagreeing with
itself as a discard. Every healthy lease would be closed, one per attempt, and the
pool would look like a provider problem.

That is expensive in the opposite direction from the defect the gate was built for:
the gate exists to stop paying for corpses, and this would make it refuse the living.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# A runners query carrying a `name=` parameter. Deliberately NOT a bare "name=" search:
# `.labels[].name` and `--arg L "$RUNNER_LABEL"` are legitimate client-side field access
# and would make this fire on correct code — a control that cries wolf gets deleted.
_FILTERED_LISTING = re.compile(r"actions/runners\?[^\"'\s]*\bname=")


def _uncommented(text: str) -> list[str]:
    """A commented-out example is not a query. Same convention as the conformance rules."""
    return [
        ln
        for ln in text.splitlines()
        if not ln.strip().lstrip("#").strip().startswith("#") and not ln.strip().startswith("#")
    ]


def test_no_workflow_filters_the_runner_listing_by_name():
    offenders = []
    scanned = 0
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        scanned += 1
        for i, line in enumerate(_uncommented(wf.read_text()), 1):
            if _FILTERED_LISTING.search(line):
                offenders.append(f"{wf.name}:{i}: {line.strip()[:110]}")
    # Non-vacuity: if the workflows directory moves, this must fail rather than pass
    # over an empty scan — a locator that finds nothing proves nothing.
    assert scanned, (
        f"no workflows found under {WORKFLOWS} — the locator is stale, not the repo clean"
    )
    assert not offenders, (
        "a runner listing is filtered by `name=`, which is EXACT-match and returns 0 for "
        "any prefix. The landing gate would then see an empty projection and discard every "
        "healthy lease:\n  " + "\n  ".join(offenders)
    )


def test_the_matcher_fires_on_the_shape_it_guards():
    """★ The control's own control. Without this, a broken regex reads as a clean repo."""
    assert _FILTERED_LISTING.search(
        'gh api "orgs/${ORG}/actions/runners?per_page=100&name=${RUNNER_NAME_PREFIX}"'
    ), "the matcher must catch a name-filtered listing"


def test_the_matcher_does_not_fire_on_legitimate_client_side_matching():
    """The false-positive side. `.labels[].name` is how this repo matches correctly."""
    for benign in (
        (
            """RUNNER_IDS=$(printf '%s' "$RUNNER_PAGES" | jq -r --arg L "$RUNNER_LABEL" """
            """'.runners[] | select(any(.labels[].name; .==$L)) | .id')"""
        ),
        'gh api --paginate "orgs/${ORG}/actions/runners?per_page=100"',
        'RESP=$(gh api "orgs/${ORG}/actions/runners?per_page=1" -i 2>&1)',
    ):
        assert not _FILTERED_LISTING.search(benign), f"false positive on: {benign[:70]}"


def test_version_comparisons_use_fixed_string_matching():
    """A version is full of `.`, and `grep` reads its pattern as a REGEX.

    ⛔ THE MISCOUNT FAILS OPEN, which is why this is pinned rather than left to review.
    Measured against versions [2.337.0, 2X337X0, 2.336.0] with EXPECTED=2.337.0:

        grep -cvx   -> 1 wrong     <- treats 2X337X0 as CORRECT
        grep -cvxF  -> 2 wrong

    So the regex form UNDER-counts wrong versions and a bad pool passes the landing
    gate. `-x` alone does not save it: whole-line matching still lets `.` match any
    character within the line.

    Pinned on the SHAPE — every grep in the gate's version comparison must carry -F —
    rather than on today's four call sites, so a fifth comparison added later is held
    to the same rule.
    """
    wf = (WORKFLOWS / "runner-pool.yml").read_text()
    offenders = [
        ln.strip()
        for ln in _uncommented(wf)
        if ("RUNNER_VERSIONS" in ln or "EXPECTED_RUNNER_VERSION" in ln)
        and "grep" in ln
        and not all("F" in flag for flag in re.findall(r"grep\s+(-[a-zA-Z]+)", ln))
    ]
    assert not offenders, (
        "a version comparison greps without -F, so `.` matches any character and wrong "
        "versions are under-counted — the gate would fail OPEN:\n  " + "\n  ".join(offenders)
    )


def test_that_matcher_would_catch_a_regex_comparison():
    """★ The control's own control: without this, a broken matcher reads as clean."""
    line = (
        """GATE_WRONG=$(printf '%s\\n' "$RUNNER_VERSIONS" """
        """| grep -cvx "${EXPECTED_RUNNER_VERSION}")"""
    )
    flags = re.findall(r"grep\s+(-[a-zA-Z]+)", line)
    assert flags and not all("F" in f for f in flags), "the matcher must reject a regex comparison"
