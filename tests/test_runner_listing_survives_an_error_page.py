"""An error page in the runner listing must not kill the step that handles errors.

`gh api --paginate` emits one JSON document per page. A throttled or transient
failure mid-pagination is an error object — ``{"message": "API rate limit
exceeded", ...}`` — with no ``.runners`` key.

The shipped filter was ``.runners[] | ...``. On that page jq raises "Cannot
iterate over null" and **exits 5**. The step runs under GitHub's default
``bash -e {0}``, where a failing command substitution aborts AT THE ASSIGNMENT —
so ``GH_RC=$?`` and the entire GITHUB_API_UNAVAILABLE branch below it never
execute. The handling was written correctly and was unreachable.

MEASURED, Borduas-Holdings/blazing run 33796902050: lease created and tagged,
``0/4 online`` three times, then::

    jq: error (at <stdin>:5): Cannot iterate over null (null)
    ##[error]Process completed with exit code 5

jq's runtime-error code surfacing as the step's. No diagnosis — and the lease
outlived the run: that DSEQ (1788463980879) appears in this repo's own
``cleanup-stale`` verdict table at 0.7d as ``-> STALE-runner`` (issue #250).

⚠ NULL-SAFETY ALONE WOULD BE WORSE THAN THE CRASH. ``(.runners // [])[]`` stops
the abort, but a rate-limited page then contributes zero runners and the wait
loop UNDERCOUNTS — a healthy pool reads as short and is discarded, destroying a
good lease on a transient API blip. That is why the error page is detected
explicitly and routed to the retry path, and why this file asserts BOTH.
"""

from __future__ import annotations

import pathlib
import re

WORKFLOW = (
    pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "runner-pool.yml"
)


def _source() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _listing_block() -> str:
    """The runner-listing read, from the pages fetch to the online count.

    ⚠ FAILS WITH A VERDICT, NOT A ValueError. The first version used `str.index`
    and raised "substring not found" when the anchor was absent — which is what
    happens on the very tree this file exists to reject. A guard whose failure
    mode is a crash tells the reader nothing about what is wrong, which is the
    same defect class as the bug below.
    """
    src = _source()
    anchor = "RUNNER_PAGES=$(gh api --paginate"
    assert anchor in src, (
        f"{WORKFLOW.name} has no `{anchor}` — the listing is still read in one "
        "unguarded command substitution, so an API error page aborts the step at the "
        "assignment and the error handling below it never runs."
    )
    start = src.index(anchor)
    end_anchor = "online (usable at"
    assert end_anchor in src[start:], f"could not find `{end_anchor}` after the fetch"
    return src[start : src.index(end_anchor, start)]


def test_the_iteration_is_null_safe() -> None:
    """`.runners[]` on an error page exits jq 5 and aborts the step under `bash -e`."""
    block = _listing_block()
    assert "(.runners // [])[]" in block, (
        "the filter iterates `.runners` without a null guard. A page with no `.runners` "
        "key — any API error object — makes jq exit 5, and under `bash -e {0}` that "
        "aborts at the assignment, before the error handling can run."
    )
    assert not re.search(r"(?<!// \[\])\.runners\[\]", block), (
        "a bare `.runners[]` remains in the listing block"
    )


def test_an_error_page_is_detected_rather_than_silently_undercounted() -> None:
    """Null-safety alone would discard healthy pools on a transient throttle."""
    block = _listing_block()
    assert 'has("runners")' in block, (
        "nothing distinguishes a listing page from an API error object, so a "
        "rate-limited page contributes zero runners and a healthy pool reads as short."
    )
    assert "continue" in block, "a bad page must route to the retry path, not fall through"


def test_the_exit_code_can_actually_be_read() -> None:
    """`VAR=$(...)` under `bash -e` aborts on failure; `|| GH_RC=$?` is what makes it readable."""
    block = _listing_block()
    assert "|| GH_RC=$?" in block, (
        "the command substitution is unguarded, so a non-zero exit aborts the step at "
        "the assignment and `GH_RC` is never assigned — the defect this file records."
    )


def test_an_empty_org_is_not_an_error() -> None:
    """`{"total_count":0,"runners":[]}` HAS the key and must count as zero, not as unreadable."""
    block = _listing_block()
    assert 'select(has("runners") | not)' in block, (
        'the bad-page predicate must be `has("runners") | not`. Testing emptiness '
        "instead would classify a legitimately empty org as an unreadable listing and "
        "spin the retry path forever."
    )


def test_the_guard_is_not_vacuous() -> None:
    """If the block cannot be located, every assertion above would be meaningless."""
    block = _listing_block()
    assert len(block) > 400, "listing block suspiciously short — the anchors may have moved"
    assert "RUNNER_IDS=" in block
