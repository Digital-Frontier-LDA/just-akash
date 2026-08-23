"""The pool OWNS its teardown — the leak of 2026-08-23 closed at the structural layer.

★ THE MEASURED DEFECT (census 2026-08-23 13:20Z, TEAMLEAD): 13 `just-akash-runner.<hash>`
deployments held 65 ACT of escrow for up to 23.5h — eleven created in EIGHT MINUTES
(2026-08-22 14:16-14:24Z). Root cause, two layers:

  1. runner-teardown.yml existed, was correct, and was called by ZERO workflows — the
     pool→work→teardown pairing lived only in ITS OWN DOCSTRING. Documentation asserted
     a property nothing implemented.
  2. Even a wired teardown would have seen an EMPTY dseq on the runs that leaked most:
     the pool publishes `dseq` to GITHUB_OUTPUT only in the success block (:685-695,
     ending `exit 0`). A cancelled or failed run — exactly the runs that leave a lease
     alive — produced no output at all.

THE FIX (two parts, both pinned here):
  A. An INTERNALIZED teardown job in runner-pool.yml: `needs: [pool]`, `if: always()`,
     calling the existing runner-teardown.yml. Every future caller INHERITS correct
     teardown instead of remembering to assemble it — the same inversion #1439/#1390
     applied to close-follows-creation. This also CORRECTS THE C5 STANDARD: the
     addendum's three-job protocol assumes the CONSUMER assembles pool→work→teardown;
     internalizing removes that assumption.
  B. EARLY dseq publication: the dseq is written to GITHUB_OUTPUT IMMEDIATELY after
     lease creation, before tagging/registration, so a later failure or cancellation
     still leaves the close identity available. (DEV5's #1439 pattern; empirically
     confirmed there by the failed-provision CI run.)

⚠ THE NO-OP DISCIPLINE (TEAMLEAD's explicit requirement): a pool that fails BEFORE
creating a lease must produce a teardown that exits 0 saying "nothing to close", NOT a
red. A teardown that reds on every failed pool trains people to gate it back on
success — which is precisely how the Blazing-Back defect was born. runner-teardown.yml
already implements this (`if [ -z "${DSEQ}" ]` → noop exit 0); the wiring must not
add a non-empty precondition that re-breaks it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

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


def test_the_pool_workflow_declares_a_teardown_job():
    """★★ THE FIX. A job named teardown exists in runner-pool.yml itself — not in a
    docstring, not in a consumer's memory. Zero-wired-teardown was the leak."""
    assert "teardown" in JOBS, (
        "runner-pool.yml has no teardown job — the pool still abandons its lease to a "
        "pairing that exists only in documentation"
    )


def test_teardown_needs_the_pool_and_runs_always():
    """if: always() — the whole point. A success-gated teardown skips exactly the
    runs that failed, which are the ones holding a live lease. (The Blazing-Back
    twin of this defect: `if: always() && needs...result == 'success'` under a
    comment saying 'Always runs (even on failure/cancel)'.)"""
    td = JOBS.get("teardown", {})
    needs = td.get("needs")
    cond = str(td.get("if", ""))
    assert "pool" in (needs if isinstance(needs, list) else [needs] if needs else []), (
        f"teardown does not need the pool job: {needs!r}"
    )
    assert re.search(r"always\(\)", cond), f"teardown is not if: always(): {cond!r}"
    assert "result" not in cond and "success" not in cond, (
        f"teardown's predicate gates on an upstream result — the exact defect this "
        f"fix exists to remove: {cond!r}"
    )


def test_teardown_calls_the_existing_reusable_teardown():
    """It CALLS runner-teardown.yml rather than duplicating its shell — the close
    logic (ownership-by-provenance, verify-don't-trust, per-label de-registration)
    is already correct and guarded; a copy would fork it."""
    td = JOBS.get("teardown", {})
    uses = str(td.get("uses", ""))
    assert uses.endswith("runner-teardown.yml"), f"teardown does not reuse the workflow: {uses!r}"


def test_teardown_passes_the_pools_own_dseq():
    """The dseq arrives from the pool job's output — the identity the close acts on.
    DSEQ is the lifecycle identity; the wallet index is NOT (deprecated)."""
    td = JOBS.get("teardown", {})
    withs = td.get("with", {}) or {}
    assert withs.get("dseq") == "${{ needs.pool.outputs.dseq }}", (
        f"teardown does not consume the pool's dseq output: {withs!r}"
    )


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
            ok = inner.startswith(("inputs.", "needs.", "env.", "github.run_id", "github.repository_owner", "github.event."))
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
    fix removes. The if: must be always() and nothing else conditional on the dseq."""
    td = JOBS.get("teardown", {})
    cond = str(td.get("if", ""))
    assert "dseq" not in cond, (
        f"the teardown predicate conditions on the dseq — a failed pool (empty dseq) "
        f"would skip teardown, reding or skipping exactly as before: {cond!r}"
    )
