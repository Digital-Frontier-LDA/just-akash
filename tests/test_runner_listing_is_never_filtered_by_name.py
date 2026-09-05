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

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# A runners query carrying a `name=` parameter. Deliberately NOT a bare "name=" search:
# `.labels[].name` and `--arg L "$RUNNER_LABEL"` are legitimate client-side field access
# and would make this fire on correct code — a control that cries wolf gets deleted.
_FILTERED_LISTING = re.compile(r"actions/runners\?[^\"'\s]*\bname=")


# Every `grep ...` up to the next pipe or command separator. Matching the SEGMENT
# rather than the first flag token is deliberate — see the three ways the token form
# was wrong in `test_version_comparisons_use_fixed_string_matching`.
_GREP_SEGMENT = re.compile(r"grep\b([^|;]*)")
_SHORT_FLAGS = re.compile(r"(?<!\S)-([a-zA-Z]+)")


def _greps_without_fixed_strings(line: str) -> list[str]:
    """Segments of `line` whose grep does NOT ask for literal matching.

    ⚠ FAILS CLOSED. A grep with no flag token at all is an offender, not a pass:
    `all([])` is True, and that vacuity let `grep "$EXPECTED"` — the plainest regex
    comparison there is — through the previous version of this check silently.
    """
    bad = []
    for seg in _GREP_SEGMENT.findall(line):
        if re.search(r"(?<!\S)--fixed-strings\b", seg):
            continue
        if any("F" in tok for tok in _SHORT_FLAGS.findall(seg)):
            continue
        bad.append(seg.strip())
    return bad


def _uncommented(text: str) -> list[str]:
    """A commented-out example is not a query. Same convention as the conformance rules."""
    return [ln for ln in text.splitlines() if not ln.strip().startswith("#")]


def test_no_workflow_filters_the_runner_listing_by_name():
    offenders = []
    scanned = 0
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        scanned += 1
        for i, line in enumerate(_uncommented(wf.read_text(encoding="utf-8")), 1):
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
    wf = (WORKFLOWS / "runner-pool.yml").read_text(encoding="utf-8")
    offenders = [
        ln.strip()
        for ln in _uncommented(wf)
        if ("RUNNER_VERSIONS" in ln or "EXPECTED_RUNNER_VERSION" in ln)
        and "grep" in ln
        and _greps_without_fixed_strings(ln)
    ]
    assert not offenders, (
        "a version comparison greps without -F, so `.` matches any character and wrong "
        "versions are under-counted — the gate would fail OPEN:\n  " + "\n  ".join(offenders)
    )


# ⛔ BOTH DIRECTIONS, because the first version of this matcher was wrong in THREE ways
# and its control caught none of them — the control only asserted that one wrong form was
# rejected, so nothing tested what it did to correct forms or to the empty case.
#
# The old predicate was `all("F" in f for f in re.findall(r"grep\s+(-[a-zA-Z]+)", ln))`,
# which reads only the FIRST flag token after each `grep`. Measured:
#
#   grep -c -x -F "$E"          correct  -> FLAGGED    (false alarm; a guard that cries
#                                                       wolf is a guard that gets deleted)
#   grep -c --fixed-strings     correct  -> FLAGGED    (same, long form)
#   grep "$E"                   WRONG    -> passed     (⚠ all([]) is True — FAILS OPEN)
#
# The third is the one that matters: a fail-open inside the check whose whole purpose is
# to catch a fail-open.
_FIXED_STRING_CASES = [
    # (label, line, is_offender)
    ("combined flags", 'X=$(printf "%s" "$V" | grep -cvxF "${EXP}")', False),
    ("separated flags", 'X=$(printf "%s" "$V" | grep -c -x -F "${EXP}")', False),
    ("long option", 'X=$(printf "%s" "$V" | grep -c --fixed-strings "${EXP}")', False),
    ("two greps, both fixed", 'printf "%s" "$V" | grep -vxF "n" | grep -cvxF "${EXP}"', False),
    ("regex comparison", 'X=$(printf "%s" "$V" | grep -cvx "${EXP}")', True),
    ("no flags at all", 'X=$(printf "%s" "$V" | grep "${EXP}")', True),
    ("second grep unfixed", 'printf "%s" "$V" | grep -vxF "n" | grep -cvx "${EXP}"', True),
]


@pytest.mark.parametrize(
    "label,line,is_offender", _FIXED_STRING_CASES, ids=[c[0] for c in _FIXED_STRING_CASES]
)
def test_that_matcher_would_catch_a_regex_comparison(label, line, is_offender):
    """★ The control's own control, in BOTH directions.

    A matcher is only trustworthy if it is pinned on what it must ACCEPT as well as
    what it must reject. Testing rejection alone is how a matcher that flagged correct
    code and passed `grep "$E"` read as working.
    """
    offenders = _greps_without_fixed_strings(line)
    if is_offender:
        assert offenders, f"{label}: must be rejected — this grep matches a version as a REGEX"
    else:
        assert not offenders, f"{label}: must be accepted — this grep already matches literally"
