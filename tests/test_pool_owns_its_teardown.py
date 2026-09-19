"""The pool owns failed/cancelled provisioning rollback until successful handoff.

The caller cannot start consumers until the reusable workflow finishes. Internal
unconditional close therefore destroys a healthy pool before its consumers run.
Early dseq publication still matters: rollback must receive the lease created before
registration or validation fails. Abrupt cancellation can suppress job outputs or
cleanup scheduling, so independent scheduled cleanup remains required.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

WF_PATH = Path(
    os.environ.get(
        "RUNNER_POOL_WF",
        Path(__file__).resolve().parents[1] / ".github/workflows/runner-pool.yml",
    )
)
SRC = WF_PATH.read_text()
DOC = yaml.safe_load(SRC)
JOBS = DOC["jobs"]


# ── Part A: the internalized teardown job ──────────────────────────────────────


# GitHub owner and repo names must START with an alphanumeric, which is what keeps a
# lone `.` or `..` component out. Without that anchor `././…` and `../../…` match.
OUR_TEARDOWN = "Digital-Frontier-LDA/just-akash/.github/workflows/runner-teardown.yml"

# The shape check is kept as well: it is what the parametrised rejection cases below
# exercise, and it states WHY an arbitrary path is wrong, not merely that it differs.
TEARDOWN_MUST_MATCH = (
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
    r"/\.github/workflows/runner-teardown\.yml"
)


def test_the_pool_workflow_declares_a_teardown_job():
    """★★ THE FIX. A job named teardown exists in runner-pool.yml itself — not in a
    docstring, not in a consumer's memory. Zero-wired-teardown was the leak."""
    assert "teardown" in JOBS, (
        "runner-pool.yml has no teardown job — the pool still abandons its lease to a "
        "pairing that exists only in documentation"
    )


def test_teardown_needs_the_pool_and_only_rolls_back_failed_handoff():
    td = JOBS.get("teardown", {})
    needs = td.get("needs")
    assert "pool" in (needs if isinstance(needs, list) else [needs] if needs else [])
    assert td.get("if") == "always() && needs.pool.result != 'success'", (
        "internal cleanup must run for failed/cancelled provisioning and leave successful "
        "handoff alive until caller consumers finish"
    )


def test_teardown_calls_the_existing_reusable_teardown():
    """It CALLS runner-teardown.yml rather than duplicating its shell — the close
    logic (ownership-by-provenance, verify-don't-trust, per-label de-registration)
    is already correct and guarded; a copy would fork it.

    ⚠ THE PROPERTY, NOT THE LITERAL — and this test previously asserted the literal.
    `uses.endswith("runner-teardown.yml")` was true only of the `./` form, so it went RED
    on the fix for just-akash#247 and stayed GREEN on the defect: a reusable's `./`
    resolves in the CALLER's tree, so every consumer's run died with `jobs=0`. That is
    exactly the trap akash-github-runner#149 records — "asserting the caller-relative
    literal made the broken form mandatory: green on the defect, red on the fix" — and
    this file walked into it one repo over.

    So: it must name runner-teardown.yml, by full path, at a pinned SHA.
    """
    td = JOBS.get("teardown", {})
    uses = str(td.get("uses", ""))
    path, _, ref = uses.partition("@")
    # ⚠ THE FULLY-QUALIFIED PATH, NOT A SUFFIX. `endswith(...)` plus "not `./`" still
    # accepts `runner-teardown.yml@<sha>` and `../runner-teardown.yml@<sha>`, neither of
    # which resolves from a consumer — so the guard would pass on values that reproduce
    # the very bug it exists to stop.
    # ⚠ AND THE COMPONENTS MUST BE REAL OWNER/REPO NAMES. A character class of
    # `[A-Za-z0-9._-]+` matches a lone `.` or `..`, so `././…` and `../../…` both
    # satisfied a "fully qualified" regex while still being caller-relative — the
    # guard would have gone green on the exact defect again. GitHub owner and repo
    # names must begin with an alphanumeric, so requiring that closes it.
    # ⚠ WELL-FORMED IS NOT THE SAME AS OURS. The pattern below proves only the SHAPE of
    # a cross-repo reference; a typo'd owner, or another repository entirely, satisfies it
    # just as well. And the SHA check that follows is a LOCAL `git show`, which succeeds
    # on any commit we happen to have — it does not verify the path. So assert the
    # identity first, then the shape, then the pin.
    # (Reported by CodeRabbit on just-akash#248.)
    assert path == OUR_TEARDOWN, (
        f"teardown must call {OUR_TEARDOWN}, got {uses!r}. Another owner/repo is a "
        "perfectly well-formed reference to a workflow this repo does not control."
    )
    assert re.fullmatch(TEARDOWN_MUST_MATCH, path), (
        f"teardown must call <owner>/<repo>/.github/workflows/runner-teardown.yml, got "
        f"{uses!r}. A bare `./` or a relative path resolves in the CONSUMER's tree and "
        "makes this workflow uncallable from any other repo (just-akash#247)."
    )
    assert re.fullmatch(r"[0-9a-f]{40}", ref), (
        f"teardown must pin the reusable to a 40-hex SHA, got {ref!r} — an unpinned ref "
        "lets the close logic change under a consumer that changed nothing."
    )


@pytest.mark.parametrize(
    "path",
    [
        "./.github/workflows/runner-teardown.yml",
        "././.github/workflows/runner-teardown.yml",
        "../.github/workflows/runner-teardown.yml",
        "../../.github/workflows/runner-teardown.yml",
        ".github/workflows/runner-teardown.yml",
        "runner-teardown.yml",
    ],
)
def test_caller_relative_forms_are_rejected(path):
    """Every one of these resolves in the CONSUMER's tree, which is just-akash#247.

    `././…` and `../../…` are the ones that matter: they passed the first version of
    this guard, because `[A-Za-z0-9._-]+` happily matches a lone `.` or `..`.
    """
    assert not re.fullmatch(TEARDOWN_MUST_MATCH, path)


def test_a_well_formed_reference_to_someone_elses_repo_is_not_enough():
    """Shape is not identity — the gap CodeRabbit found on just-akash#248.

    `Someone-Else/their-fork/.github/workflows/runner-teardown.yml` is a perfectly
    well-formed cross-repo reference. It passes the shape pattern, and the SHA check
    that follows is a LOCAL `git show` that never looks at the path, so a typo'd owner
    could satisfy both while GitHub loads a workflow this repo does not control.
    """
    foreign = "Someone-Else/their-fork/.github/workflows/runner-teardown.yml"
    assert re.fullmatch(TEARDOWN_MUST_MATCH, foreign), "shape check should accept it"
    assert foreign != OUR_TEARDOWN, "identity check must reject it"


def test_the_real_reference_is_accepted():
    """Known-negative: the guard must not reject the form the fix actually uses."""
    assert re.fullmatch(
        TEARDOWN_MUST_MATCH,
        OUR_TEARDOWN,
    )


def test_teardown_passes_the_pools_own_dseq():
    """The dseq arrives from the pool job's output — the identity the close acts on.
    DSEQ is the lifecycle identity; the wallet index is NOT (deprecated)."""
    td = JOBS.get("teardown", {})
    withs = td.get("with", {}) or {}
    assert withs.get("dseq") == "${{ needs.pool.outputs.dseq }}", (
        f"teardown does not consume the pool's dseq output: {withs!r}"
    )


def test_teardown_passes_the_pools_create_time_owner():
    td = JOBS.get("teardown", {})
    withs = td.get("with", {}) or {}
    assert withs.get("wallet-address") == "${{ needs.pool.outputs.wallet_address }}"


def test_wallet_address_is_published_before_failure_prone_validation():
    parse = SRC.find("WALLET=$(awk")
    publish = SRC.find('echo "wallet_address=$WALLET" >> "$GITHUB_OUTPUT"')
    first_validation = SRC.find('if [ -n "$DSEQ" ] && [ -z "$PROVIDER" ]', parse)
    assert parse != -1 and parse < publish < first_validation


def test_teardown_forwards_the_secrets_the_close_needs():
    """The close authenticates through the Console wallet pool; de-registration
    needs GH_RUNNER_PAT. Missing secrets make the close a silent no-op."""
    secrets = set((JOBS.get("teardown", {}).get("secrets") or {}).keys())
    for name in ("AKASH_API_KEY", "AKASH_API_KEYS", "GH_RUNNER_PAT"):
        assert name in secrets, f"teardown does not forward {name}: {secrets!r}"


def test_teardown_receives_usable_inputs_not_empty_context_lookups():
    """★★ THE REVIEW CATCH (sentinel x2 + CodeRabbit on #182): the first wiring passed
    `github-org: ${{ github.organization }}` — a context property that DOES NOT EXIST
    and evaluates to EMPTY STRING silently. All six required checks were green on that
    form: ruff, pyright, tests, gitleaks, semgrep, CVE — none of them read workflow
    expressions. The teardown would have run faithfully on every failure and been
    UNABLE TO DE-REGISTER, because it did not know which org the runners were in:
    reachable-but-inert, the same defect class as the dseq-publication bug this PR
    fixes — the value's AVAILABILITY, not the call's reachability.
    The pin: every input the teardown consumes must name the SAME source the pool's
    own steps use (its own inputs), never an invented context property."""
    td = JOBS.get("teardown", {})
    withs = td.get("with", {}) or {}
    assert withs.get("github-org") == "${{ inputs.github-org }}", (
        f"teardown's github-org does not read the pool's own input: "
        f"{withs.get('github-org')!r} — a non-existent context property (e.g. github."
        f"organization) resolves to EMPTY silently and the de-registration no-ops"
    )
    # And the generalizable half of the pin: NO teardown input may name a context
    # property that GitHub does not define. The three real ones used here are
    # inputs.* and needs.*; anything else in a `with:` must be audited by hand.
    for key, expr in withs.items():
        expr = str(expr)
        if expr.startswith("${{"):
            inner = expr[3:-3].strip()
            known = (
                "inputs.",
                "needs.",
                "env.",
                "github.run_id",
                "github.repository_owner",
                "github.event.",
            )
            ok = inner.startswith(known)
            assert ok, (
                f"teardown input {key}={expr!r} does not name a known context "
                f"property — invalid ones resolve to EMPTY STRING silently"
            )


def test_teardown_label_wiring_uses_the_pools_own_label():
    """De-registration must be scoped to THIS pool's label (an org-wide offline sweep
    races other repos' in-flight provisioning). The label comes from the pool's own
    input, not a consumer's."""
    td = JOBS.get("teardown", {})
    withs = td.get("with", {}) or {}
    assert withs.get("runner-label") == "${{ inputs.runner-label }}", (
        f"de-registration label is not the pool's own: {withs!r}"
    )


# ── Part B: early dseq publication ─────────────────────────────────────────────


def test_dseq_is_published_before_the_registration_wait():
    """★★ The output-survival half. The dseq must reach GITHUB_OUTPUT BEFORE the
    registration wait — the long phase where cancellation is most likely — not only
    in the success block. Published late, a failed run leaves teardown with nothing."""
    early = SRC.find('echo "dseq=$DSEQ" >> "$GITHUB_OUTPUT"')
    assert early != -1, "no early dseq publication found in the provision step"
    success_block = SRC.find("provision_healthy=true")
    assert success_block != -1
    # The FIRST publication must precede the success block (DEV5's #1439 pattern).
    assert early < success_block, (
        "dseq is only published in the success block — a failed/cancelled run still "
        "leaves the teardown with an empty identity"
    )


def test_early_publication_sits_right_after_the_dseq_parse():
    """The publication belongs immediately after DSEQ is parsed from the deploy log —
    before the orphan-close branch, before any wait. Anything between parse and
    publication is a leak window."""
    parse = SRC.find("DSEQ=$(awk")
    publish = SRC.find('echo "dseq=$DSEQ" >> "$GITHUB_OUTPUT"')
    assert parse != -1 and publish != -1 and publish > parse, (
        "the early dseq publication does not follow the DSEQ parse"
    )
    # And nothing cancellable-long between them: the gap must be small (no wait loop).
    between = SRC[parse:publish]
    assert "sleep" not in between and "seq 1" not in between, (
        "a wait sits between the DSEQ parse and its publication — the lease identity "
        "is still unrecorded through that window"
    )


# ── the no-op discipline (must survive the wiring) ─────────────────────────────


def test_teardown_has_no_nonempty_dseq_precondition():
    """⚠ TEAMLEAD's explicit requirement: the WIRING must not add a precondition like
    `needs.pool.outputs.dseq != ''`. runner-teardown.yml already treats empty as a
    successful no-op; gating it in the caller would re-train the success-gating this
    fix removes. Rollback must not add an identity-presence condition."""
    td = JOBS.get("teardown", {})
    cond = str(td.get("if", ""))
    assert "dseq" not in cond, (
        f"the teardown predicate conditions on the dseq — a failed pool (empty dseq) "
        f"would skip teardown, reding or skipping exactly as before: {cond!r}"
    )
