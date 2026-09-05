"""Lock the properties of runner-pool.yml that were each learned from an incident.

This is a REUSABLE workflow: consumers pin it by tag, so a regression here reaches every
repo that calls it at once. Each test below names the failure it prevents, because a
guard whose reason is not written down gets "simplified" away by the next reader.

Every assertion is mutation-tested — see test_the_guards_are_not_vacuous at the bottom,
which proves these tests can actually fail. Fourteen source-inspection guards in a sibling
repo asserted nothing at all; a guard that cannot fail is worse than no guard, because it
reports safety it never checked.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# Overridable so the anti-vacuity pass below can re-run this whole module against a
# deliberately-broken copy and require it to go RED.
WF_PATH = Path(
    os.environ.get(
        "RUNNER_POOL_WF",
        Path(__file__).resolve().parents[1] / ".github/workflows/runner-pool.yml",
    )
)
SRC = WF_PATH.read_text(encoding="utf-8")
DOC = yaml.safe_load(SRC)
CALL = (DOC.get("on") or DOC.get(True))["workflow_call"]
INPUTS = CALL["inputs"]
OUTPUTS = CALL["outputs"]
STEPS = DOC["jobs"]["pool"]["steps"]


# A cross-repo-callable reusable reference: owner and repo, then the workflow path, then
# a pinned SHA. Each component is anchored to `[A-Za-z0-9]` because GitHub owner and repo
# names must begin with one — without that anchor a lone `.` or `..` matches the class and
# `././…` and `../../…` sail through, which is exactly the hole this guard exists to close.
REUSABLE_WORKFLOW_REF = (
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*"
    r"/\.github/workflows/[A-Za-z0-9._-]+\.ya?ml@[0-9a-f]{40}"
)


def _step(fragment: str) -> dict:
    for s in STEPS:
        hay = (s.get("name", "") + s.get("id", "") + s.get("uses", "")).lower()
        if fragment.lower() in hay:
            return s
    raise AssertionError(f"no step matching {fragment!r}")


PROVISION = _step("Provision")


def _code(body: str) -> str:
    """A shell body with its comment lines removed.

    These guards assert what the shell DOES, and matching raw text also matches the prose
    explaining why. A comment that names the very construct it warns against — "never
    `| length`" — then trips the guard forbidding it, and the cheapest way to go green is
    to delete the explanation. That inverts the point of writing the reason down, so
    strip comments and assert on code.
    """
    return "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))


# --------------------------------------------------------------------------
# tag-prefix — a shared default destroyed another repo's live deployment
# --------------------------------------------------------------------------


def test_tag_prefix_is_required_and_has_no_default():
    """Two repos both defaulting to `ci-<id>` meant one repo's sweeper matched and
    destroyed the OTHER's live deployment. Any default resurrects that collision, so
    the caller must be forced to name itself."""
    assert INPUTS["tag-prefix"]["required"] is True
    assert "default" not in INPUTS["tag-prefix"], (
        "a default tag-prefix is how a cross-repo sweep destroyed a live deployment"
    )


def test_the_just_akash_ref_is_required_and_has_no_default():
    """A default here is the #184 bug written down as configuration.

    The pin cannot be derived — no context exposes a reusable workflow's own revision to
    itself (`github.workflow_*` names the CALLER's entry workflow; `job.*` carries only
    check_run_id/container/services/status). So the only honest options are "the caller
    supplies it" or "it floats". A default makes it float while LOOKING pinned from the
    caller's side, which is how every runner came to be built from main's tip at
    deploy-second while callers believed their pin was honoured.

    Same reasoning as tag-prefix above, and the same remedy: force the caller to say it.
    """
    assert INPUTS["just-akash-ref"]["required"] is True
    assert "default" not in INPUTS["just-akash-ref"], (
        "a default just-akash-ref silently provisions from whatever main is at "
        "deploy-second — the unpinned window #184 closed"
    )


def test_the_tag_carries_run_identity():
    """Without run_id a sweeper cannot distinguish this run's lease from a sibling's."""
    assert "${TAG_PREFIX}-${RUN_ID}" in PROVISION["run"]


def test_the_lease_is_tagged_before_the_wait_not_after():
    """The wait is the long part and the likeliest place to be cancelled. A lease
    tagged only afterwards is invisible to every sweeper and leaks escrow forever."""
    body = PROVISION["run"]
    assert body.index('"${JA[@]}" tag') < body.index("online (usable at"), (
        "tag must precede the runner wait, or a cancellation leaks an untagged lease"
    )


# --------------------------------------------------------------------------
# Teardown may only ever destroy what this loop created
# --------------------------------------------------------------------------


def test_teardown_targets_one_locally_parsed_dseq():
    """A sweep destroyed 14 third-party deployments once. Every destroy here must name
    a single DSEQ parsed from this job's own deploy output — never a tag glob, never
    an --all."""
    for m in re.finditer(r'"\$\{JA\[@\]\}" destroy[^\n]*', PROVISION["run"]):
        line = m.group(0)
        assert '--dseq "$DSEQ"' in line, f"destroy must name this run's dseq: {line}"
        assert not re.search(r"--all\b|--tag\b|\*", line), f"blast radius too wide: {line}"


def test_a_discarded_lease_is_actually_destroyed():
    """Rejecting a pool without closing it holds escrow against the same grant the
    next attempt spends from — the failure compounds itself."""
    body = PROVISION["run"]
    # Scoped to the DISCARD branch. A whole-body index comparison broke the moment an
    # earlier orphan-cleanup destroy was added — the guard was right, the assertion was
    # positional. Assert the property, not the ordering of the first match.
    discard = body[body.index("discarding this lease") :]
    assert '"${JA[@]}" destroy --dseq "$DSEQ"' in discard[:900]


# --------------------------------------------------------------------------
# A partial pool is usable — rejecting it destroys the only working runners
# --------------------------------------------------------------------------


def test_the_usable_threshold_is_min_pool_not_pool_size():
    """A provider delivered 6 of 12 and was rejected AND torn down, removing the only
    provider actually serving us. The discard branch must compare against MIN_POOL."""
    body = PROVISION["run"]
    discard = body[body.index('if [ "${ONLINE:-0}"') : body.index('EXCLUDED="$EXCLUDED')]
    assert '-lt "${MIN_POOL}"' in discard, (
        "comparing the discard against POOL_SIZE restores all-or-nothing, which "
        "destroyed the only provider actually serving us"
    )
    assert '-lt "${POOL_SIZE}"' not in discard


def test_min_pool_is_clamped_against_junk_and_out_of_range():
    """min-pool-size is a free-text workflow input. Empty, 'two', 0 or 99 must all
    degrade to pool-size rather than making the discard test meaningless."""
    body = PROVISION["run"]
    assert "*[!0-9]*" in body, "non-numeric input must be rejected"
    # Named, not bare "-ge 1": a second clamp elsewhere in the step (runner-wait-tries)
    # satisfied the loose form and made this guard vacuous — caught by the anti-vacuity
    # pass below, which is exactly the failure it exists to surface.
    assert '[ "$MIN_POOL" -ge 1 ]' in body, "0 would accept an empty pool as healthy"
    assert '-le "$POOL_SIZE"' in body, "above pool-size the loop can never satisfy it"


def test_accepting_a_partial_pool_is_announced():
    """Silently running at half capacity looks like a slow CI, not a degraded one."""
    assert "Partial pool accepted" in PROVISION["run"]


# --------------------------------------------------------------------------
# GitHub paginates — and an aggregating --jq silently inverts this job's verdict
# --------------------------------------------------------------------------


def test_the_online_count_survives_a_paginated_org():
    """`gh api --paginate --jq` runs the filter against EACH PAGE and concatenates the
    results, so an aggregating filter emits one value PER PAGE: an org with >100 runners
    returned "2\\n1" instead of "3".

    That is not a miscount, it inverts the verdict. `[ "$ONLINE" -ge N ]` on a
    multi-line value exits NON-ZERO with "integer expression expected", so the
    `-lt "${MIN_POOL}"` discard branch is never taken and a pool with ZERO online
    runners is published as healthy — `runs-on` then targets a label nothing answers
    to, which is worse than the hosted fallback this workflow exists to provide.

    >100 runners is the normal case, not an exotic one: offline registrations
    accumulate and once overflowed an org's runner listing outright.
    """
    code = _code(PROVISION["run"])
    poll = code[code.index("gh api --paginate") : code.index("online (usable at")]
    assert "| length" not in poll, (
        "an aggregating jq filter emits one value per page under --paginate, and a "
        "multi-line ONLINE makes every integer test below error-out into 'healthy'"
    )
    assert "grep -c" in poll or "--slurp" in poll, (
        "the count must survive page concatenation — count matched LINES, or --slurp"
    )


def test_the_online_count_is_a_single_integer_on_every_path():
    """ONLINE feeds three integer comparisons and $GITHUB_OUTPUT. Anything that can
    return empty or multi-line corrupts all four — a multi-line value written to
    $GITHUB_OUTPUT without a heredoc delimiter also breaks every LATER output in the
    file, not just this one."""
    code = _code(PROVISION["run"])
    poll = code[code.index("gh api --paginate") : code.index("online (usable at")]
    assert "grep -c ." in poll, "grep -c prints exactly one integer, 0 included"
    assert "|| true" in poll or "|| echo 0" in poll, (
        "grep -c exits 1 on zero matches; the assignment must not carry that outward"
    )


def test_a_failed_listing_query_is_not_counted_as_zero_runners():
    """A throttled `gh` must not walk the 'the provider never delivered' path.

    Swallowing a non-zero exit into 0 destroys the lease, EXCLUDES the provider from
    later attempts, and reports RUNNER_NEVER_REGISTERED — which names that provider a
    runner_deny candidate for what is our own API budget. That is the exact class of
    misdiagnosis this workflow exists to remove.

    It is also not a rare path at pool scale: GitHub cannot filter runners by label
    server-side, so each poll costs ceil(org_runners/100) requests against a PAT's
    5,000/hour, shared across every token and repo of that user."""
    code = _code(PROVISION["run"])
    poll = code[code.index("gh api --paginate") : code.index('if [ "$API_OK"')]
    # Positive assertions: `2>/dev/null not in poll` would also forbid the legitimate
    # suppression on the `tail` that reports the error, and a guard that forbids the
    # remedy gets weakened rather than obeyed.
    assert re.search(r"2>\s*/tmp/\S+\.err", poll), (
        "gh's stderr must be captured, not discarded — it carries the 403 that explains "
        "the failure, and discarding it is how a rate limit became 'zero runners'"
    )
    assert re.search(r"GH_RC=\$\?", poll), "the exit status must be captured, not ignored"
    assert re.search(r"GH_RC.*-ne 0", poll), "a non-zero gh must take its own branch"
    assert "API_OK" in poll, "the loop must record whether the listing was EVER readable"


def test_an_unreadable_listing_is_its_own_failure_world():
    """GITHUB_API_UNAVAILABLE exists because the remedy (API budget) has nothing to do
    with Akash. Folding it into RUNNER_NEVER_REGISTERED sends the operator at providers
    — and at runner_deny — for a GitHub rate limit."""
    body = SRC
    assert "failure_reason=GITHUB_API_UNAVAILABLE" in body
    assert "GITHUB_API_UNAVAILABLE" in OUTPUTS["failure_reason"]["description"], (
        "a reason the caller cannot find documented is a reason they will misread"
    )
    unreadable = body[body.index('if [ "$API_OK"') :]
    assert "NOT a provider fault" in unreadable, "must say what it is not"


def test_an_unreadable_listing_still_closes_the_lease_but_spares_the_provider():
    """Two independent properties, and both are load-bearing.

    The lease must close: an unverifiable pool cannot be handed to the caller, and
    leaving it holds escrow against the grant the next run spends from.

    The provider must NOT be excluded and no further attempt spent: re-selecting
    providers cannot fix GitHub's API, and a retry doubles the request load that is the
    most likely cause of the failure in the first place."""
    body = PROVISION["run"]
    unreadable = body[body.index('if [ "$API_OK"') : body.index('if [ "${ONLINE:-0}" -lt')]
    assert '"${JA[@]}" destroy --dseq "$DSEQ"' in unreadable, "an unread lease still leaks"
    assert "EXCLUDED=" not in unreadable, "a rate limit is not evidence about a provider"
    assert "exit 1" in unreadable, "must not spend another attempt on an API failure"


def test_a_throttled_poll_backs_off_further_than_the_healthy_cadence():
    """The secondary limit is a per-MINUTE budget, so retrying a throttled read at the
    healthy 5s cadence spends the window it is waiting for."""
    body = PROVISION["run"]
    poll = body[body.index("gh api --paginate") : body.index('if [ "$API_OK"')]
    fail_branch = poll[poll.index("GH_RC") : poll.index("API_OK=1")]
    assert re.search(r"sleep (1[0-9]|[2-9][0-9])", fail_branch), (
        "the failure path must back off further than the 5s healthy poll"
    )


def test_the_wait_window_is_budgeted_by_time_not_iterations():
    """The healthy poll sleeps 5s and the throttled poll sleeps 15s, but a
    `seq 1 $RUNNER_WAIT_TRIES` loop spends one iteration on either — so an all-throttled
    run waited 15 x 90 = 22.5 MINUTES while the input description, the docs and the
    error text all promised 7.5. The caller's jobs sit behind that.

    A wall-clock deadline keeps the promised window honest whatever mix of waits occurs.
    """
    code = _code(PROVISION["run"])
    assert "WAIT_DEADLINE" in code, "the wait must be bounded by time, not iteration count"
    assert not re.search(r"seq 1 \"\$RUNNER_WAIT_TRIES\"", code), (
        "an iteration budget lets the 15s throttled sleep triple the promised window"
    )


def test_the_runner_query_is_still_paginated_at_all():
    """Without --paginate the poll only ever sees the first 100 runners, so a pool whose
    registrations land on page 2 reads as never having come online — RUNNER_NEVER_
    REGISTERED against providers that did their job."""
    poll = PROVISION["run"]
    assert "--paginate" in poll and "per_page=100" in poll


# --------------------------------------------------------------------------
# Naming the failure — "(infra)" for everything is what grew the bill
# --------------------------------------------------------------------------


def test_every_failure_world_has_its_own_reason():
    """A funding problem, a market outage and a broken host each need a different
    remedy. Collapsing them made 'move jobs to hosted runners' the standing fix."""
    body = SRC
    for reason in (
        "WALLET_UNDERFUNDED",
        "PROVIDER_CAPACITY",
        "RUNNER_NEVER_REGISTERED",
        "NO_ELIGIBLE_BIDDER",
    ):
        assert f"failure_reason={reason}" in body, f"{reason} is never emitted"
    assert "failure_reason" in OUTPUTS, "the caller cannot see why it fell back"


def _checkout(steps: list) -> dict:
    return next(s for s in steps if "actions/checkout" in s.get("uses", ""))


def test_the_workflow_checks_out_just_akash_not_the_caller():
    """A reusable workflow's job runs in the CALLER's context, so `github.repository` is
    THEIR repo and a bare checkout fetches THEIR code. `uv run --with .` then installs
    the caller's package — or fails outright when they have no pyproject.toml —
    `just-akash` is never on PATH, and `python -m just_akash.runner_candidates` raises
    ModuleNotFoundError.

    This is the difference between "works in this repo" and "works for a consumer", and
    nothing in this repo calls these workflows, so it was never exercised."""
    for label, steps in (("pool", STEPS), ("teardown", TD_STEPS)):
        with_ = _checkout(steps).get("with", {})
        repo = str(with_.get("repository", ""))
        assert repo, f"{label}: a bare checkout fetches the CALLER's repo, which has no just_akash"
        assert "just-akash-repository" in repo or repo.endswith("/just-akash"), (
            f"{label}: repository={repo!r} does not name just-akash"
        )
        assert with_.get("path"), f"{label}: must not overwrite the caller's workspace root"


def test_the_cli_source_is_pinned_to_the_ref_the_caller_pinned():
    """Tracking a branch would let the classification tables, the SDL and the
    provider-qualification bar change under a consumer whose pin never moved — which is
    the entire reason they pinned a ref.

    THIS TEST USED TO ASSERT `job.workflow_sha`, AND THAT PINNED THE BUG. That property
    does not exist — `job` carries only check_run_id/container/services/status — so it
    evaluated to the empty string and checkout silently took the default branch. The old
    docstring foresaw the failure ("an undefined property evaluates to empty ... the
    checkout degrades to just-akash's default branch") but assumed the property existed
    and might one day be withdrawn. It never existed, so the degraded state was the ONLY
    state, and this test held it there: green on the broken workflow, red on the fix.

    Assert the PROPERTY the docstring names — the ref is explicitly supplied and does not
    float — not the MECHANISM that was supposed to deliver it (#184).
    """
    for label, steps in (("pool", STEPS), ("teardown", TD_STEPS)):
        ref = str(_checkout(steps).get("with", {}).get("ref", ""))
        assert ref, f"{label}: an empty ref floats to the default branch"
        assert "inputs.just-akash-ref" in ref, (
            f"{label}: ref={ref!r} — the pin must come from a required input; a derived "
            f"or literal ref floats and breaks the guarantee a caller pins for"
        )
        assert "job.workflow_sha" not in ref, (
            f"{label}: `job.workflow_sha` is not a real context property — it resolves to "
            f"the empty string and the checkout takes the default branch (#184)"
        )
        assert "github.workflow_sha" not in ref, (
            f"{label}: the github context is the CALLER's workflow, not this one"
        )


def test_every_uv_invocation_runs_from_the_just_akash_checkout():
    """`uv run --with .` resolves `.` against the working directory, so checking the
    source into a path without pointing the run steps at it reintroduces the same
    failure one layer down."""
    for label, doc, job in (("pool", DOC, "pool"), ("teardown", TD, "teardown")):
        wd = doc["jobs"][job].get("defaults", {}).get("run", {}).get("working-directory", "")
        assert wd, f"{label}: run steps still execute from the caller's workspace root"
        path = _checkout(doc["jobs"][job]["steps"]).get("with", {}).get("path", "")
        assert wd.strip("./") == path.strip("./"), (
            f"{label}: working-directory {wd!r} does not match checkout path {path!r}"
        )


def test_the_pool_runs_the_image_providers_were_qualified_against():
    """A provider earns runner_host by scheduling the PROBE image three consecutive
    times. Running a different image in the pool means the pool is trusting a
    measurement taken of something else — and `:latest` made that gap permanent and
    silent, since the tag can move between the qualification and the run relying on it.

    The probe SDL already explains why it pins a digest; this asserts the pool did not
    quietly opt out of that reasoning."""
    probe_sdl = (Path(__file__).resolve().parents[1] / "sdl/github-runner-probe.yaml").read_text(
        encoding="utf-8"
    )

    def _image(text: str, what: str) -> str:
        m = re.search(r"image:\s*(\S+)", text)
        assert m, f"no image found in {what}"
        return m.group(1)

    probe_img = _image(probe_sdl, "the probe SDL")
    pool_img = _image(_code(_step("Render runner SDL")["run"]), "the rendered pool SDL")

    assert "@sha256:" in pool_img, f"the pool image must be digest-pinned, got {pool_img}"
    assert pool_img == probe_img, (
        f"pool runs {pool_img} but providers are qualified against {probe_img} — "
        "the qualification measures a different artifact than the pool runs"
    )


def test_wallet_contention_is_not_reported_as_a_market_outage():
    """AKASH_API_KEY is ONE Cosmos account, which cannot carry two transactions at once:
    concurrent provisioners reject each other with account-sequence mismatches, and
    nothing in just_akash retries that.

    No order is created, so no provider is ever asked to bid — reporting PROVIDER_CAPACITY
    is a fabricated market outage, and it fires hardest during a spike, when the cause is
    our own concurrency and the market is fine. It must be checked BEFORE the capacity
    verdict, since the capacity branch is the `else`.
    """
    code = _code(PROVISION["run"])
    assert "failure_reason=WALLET_TX_CONTENTION" in code
    assert re.search(r"account sequence mismatch|sequence mismatch", code, re.I), (
        "the rejection has to be recognised before it can be classified"
    )
    # Anchored on the classification BRANCH, not the name. `code.index(
    # "SAW_SEQ_CONTENTION")` found the `SAW_SEQ_CONTENTION=0` initialisation near the
    # top of the step, which precedes the capacity verdict no matter where the branch
    # moves — so the guard held even with the ordering it exists to lock reversed.
    branch = code.index('elif [ "$SAW_SEQ_CONTENTION" = "1" ]')
    assert branch < code.index("failure_reason=PROVIDER_CAPACITY"), (
        "contention must be classified before falling through to a capacity verdict"
    )


def test_wallet_contention_backs_off_with_jitter_instead_of_recolliding():
    """An immediate retry re-collides with whatever won the race — that is the definition
    of the failure. Jitter is what breaks the lockstep between concurrent callers, so a
    fixed sleep would just move the collision."""
    code = _code(PROVISION["run"])
    seq = code[code.index("SAW_SEQ_CONTENTION=1") : code.index("no lease within the bid window")]
    assert "RANDOM" in seq, "a fixed backoff keeps concurrent callers in lockstep"
    assert re.search(r"sleep\s+\"?\$", seq), "must actually wait before the next attempt"


def test_wallet_contention_does_not_claim_a_bid_was_seen():
    """SAW_BID drives the RUNNER_NEVER_REGISTERED verdict, which names a provider as a
    runner_deny candidate. A transaction the chain rejected never reached a provider."""
    code = _code(PROVISION["run"])
    seq = code[code.index("SAW_SEQ_CONTENTION=1") : code.index("no lease within the bid window")]
    assert "SAW_BID=1" not in seq, "a rejected transaction is not a bid"


def test_an_unclassified_deploy_failure_prints_its_raw_output():
    """The classifiers are not equally evidenced, and the workflow must not pretend they
    are. The 402 signature is observed; the account-sequence signature is INFERRED from
    this repo's reasoning about one Cosmos account, not from a captured failure — and the
    one concurrency-shaped failure on record surfaced as a bare
    `HTTP 500 {"error":"InternalServerError"}` with no Cosmos detail at all.

    A matcher that can never fire is worse than none: WALLET_TX_CONTENTION would read as
    a handled case while every occurrence fell through to PROVIDER_CAPACITY — the
    fabricated-outage verdict this workflow exists to stop reporting. Printing the raw
    output is what turns the next occurrence into evidence."""
    code = _code(PROVISION["run"])
    unclassified = code[code.index("unclassified deploy failure") :]
    assert "tail -40 /tmp/ja.log" in unclassified, (
        "an unclassified failure must surface what actually happened"
    )
    assert "NOT classified" in unclassified, (
        "the warning must not read as a market verdict when nothing was classified"
    )
    # It has to come BEFORE the generic bid-window line, or the evidence is buried under
    # a message that already claims to know the cause.
    assert code.index("unclassified deploy failure") < code.index("no lease within the bid window")


def test_a_402_is_not_reported_as_a_missing_bid():
    """Insufficient balance is rejected BEFORE an order exists, so no provider ever
    saw it. Calling that 'no bid' sends the investigation at providers instead of at
    the wallet — which is exactly the misdiagnosis that kept recurring."""
    body = PROVISION["run"]
    assert re.search(r"PaymentRequiredError|HTTP 402", body)
    m402 = body.index("HTTP 402")
    assert body.index("WALLET_UNDERFUNDED", m402) < body.index("no lease within the bid window")


def test_a_402_does_not_retry():
    """Retrying a balance rejection burns the whole attempt budget to reach the same
    answer, and the run then reports a market outage."""
    body = PROVISION["run"]
    tail = body[body.index("HTTP 402") :]
    assert "exit 1" in tail[: tail.index("::endgroup::") + 40]


def test_the_underfunded_message_says_it_is_not_a_ci_defect():
    """The whole point: an agent reading this must not 'fix' it by switching to paid
    runners, which is the cost this exists to remove."""
    provision = PROVISION["run"]
    assert "No order was created" in provision
    assert "Top up the wallet" in provision
    assert "::error title=" in provision, "a step summary alone is missed in a red run"


# --------------------------------------------------------------------------
# Provider selection must not silently fall back
# --------------------------------------------------------------------------


def test_a_bad_provider_spec_fails_the_step():
    """continue-on-error here would fall through to just-akash's defaults and could
    re-select a provider the operator recorded as unable to schedule the runner pod."""
    assert _step("Select providers").get("continue-on-error") is not True


def test_providers_input_has_no_default_fleet():
    """runner_host/runner_deny are measurements of ONE fleet. Shipping a default list
    would make one operator's trust decision everyone's."""
    assert INPUTS["providers"].get("default") == ""


def test_the_provision_step_reads_the_filtered_list_only():
    """If the raw spec reached the provision step it could bypass the deny filter."""
    env = PROVISION.get("env", {})
    assert "steps.candidates.outputs.preferred_candidates" in env.get(
        "PREFERRED_CANDIDATES_CSV", ""
    )
    assert "steps.candidates.outputs.fallback_candidates" in env.get("FALLBACK_CANDIDATES_CSV", "")
    assert not any("inputs.providers" in str(v) for v in env.values()), (
        "the unfiltered provider spec must not be in scope where the deploy happens"
    )


def test_proven_and_unproven_candidates_reach_distinct_auction_tiers():
    """Sorting a CSV is not a preference contract: if every address is passed with
    --provider, the auction sees one tier and a cheaper unproven host can win."""
    body = PROVISION["run"]
    assert 'PROV_ARGS+=(--provider "$p")' in body
    assert 'PROV_ARGS+=(--backup-provider "$p")' in body


# --------------------------------------------------------------------------
# The caller's fallback contract
# --------------------------------------------------------------------------


def test_runner_targets_falls_back_to_a_hosted_runner():
    """runs-on cannot be conditional, so an unhealthy pool must still yield a runnable
    label or every downstream job fails to schedule."""
    job_out = DOC["jobs"]["pool"]["outputs"]["runner-targets"]
    assert '["ubuntu-latest"]' in job_out, "no hosted fallback: downstream jobs cannot schedule"
    assert "runner-targets" in OUTPUTS, "the fallback never reaches the caller"


def test_runner_targets_is_only_set_on_a_healthy_pool():
    """Emitting the pool's labels after a failed provision would route jobs at runners
    that do not exist, and they would queue until the job timeout."""
    body = PROVISION["run"]
    assert body.index("provision_healthy=true") - 200 < body.index("runner_targets=[")


def test_the_pool_label_carries_run_identity():
    """A shared static label lets one run's jobs land on another run's runners."""
    assert "${RUNNER_LABEL}" in _step("Render runner SDL")["run"]


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------


def test_the_rendered_sdl_is_echoed_without_the_token():
    """The SDL embeds a PAT with org runner-registration rights. Actions masks known
    secrets, but a rendered file printed wholesale is exactly how one escaped before."""
    render = _step("Render runner SDL")["run"]
    assert "grep -vE 'ACCESS_TOKEN'" in render
    assert "cat /tmp/runner-sdl.yaml" not in render


def test_checkout_does_not_persist_credentials():
    """A persisted token on a runner that later executes caller-supplied jobs is a
    credential the job did not ask for."""
    co = _step("actions/checkout")
    assert co["with"]["persist-credentials"] is False


def test_actions_are_pinned_to_a_sha():
    """A moving tag on a third-party action is arbitrary code execution with the
    runner PAT in scope."""
    for s in STEPS:
        if "uses" in s:
            assert re.search(r"@[0-9a-f]{40}$", s["uses"]), f"{s['uses']} is not sha-pinned"


def test_permissions_are_read_only():
    assert DOC["permissions"] == {"contents": "read"}


# --------------------------------------------------------------------------
# provider-select — #211 armed `--select emptiest` in the CLI; nothing could reach it
# --------------------------------------------------------------------------


def test_provider_select_input_exists_optional_and_empty_by_default():
    """⛔ THE GAP THIS CLOSES, measured 2026-08-29: `just-akash deploy --select
    {cheapest,emptiest}` shipped in #211 and the pool workflow exposed NO input for it —
    zero call sites pass the flag, so every consumer got `cheapest` regardless. A
    capability no caller can reach is the merged-not-invoked defect one level down.

    The default is EMPTY on purpose: the CLI owns the real default, and an empty value
    must contribute NO flag — `--select ""` is an argparse exit 2, and the deploy call's
    deliberate `|| true` (auction rounds retry) would swallow it silently."""
    spec = INPUTS.get("provider-select")
    assert spec is not None, (
        "no provider-select input — the CLI's --select is unreachable from any consumer"
    )
    assert spec.get("required") is False
    assert spec.get("default") == ""


def test_provider_select_is_validated_before_the_first_attempt():
    """The deploy invocation ends in `|| true` because auction rounds legitimately fail
    and retry. That tolerance would also swallow argparse's exit 2 on a misspelled
    --select value — every attempt would burn a bid window reporting nothing. So the
    workflow rejects an unknown value ITSELF, before the first attempt."""
    code = _code(PROVISION["run"])
    assert "cheapest|emptiest" in code, (
        "no case guard constrains provider-select — a typo is retried as an auction failure"
    )


def test_provider_select_env_is_wired_from_the_input():
    env = PROVISION.get("env") or {}
    assert env.get("PROVIDER_SELECT") == "${{ inputs.provider-select }}"


def test_provider_select_reaches_the_deploy_invocation():
    """The point of the input: the consumer's choice must arrive at `deploy`. An input
    that validates but is never passed is decorative."""
    code = _code(PROVISION["run"])
    line = next((ln for ln in code.splitlines() if '"${PROV_ARGS[@]}"' in ln), "")
    assert '"${SELECT_ARGS[@]}"' in line, (
        "the deploy invocation takes --provider args but no --select"
    )


# --------------------------------------------------------------------------
# Anti-vacuity — prove the guards above can actually fail
# --------------------------------------------------------------------------

# ─── placement key: the on-chain ownership marker ────────────────────────────
#
# ⛔ WHY THIS IS AN INPUT AT ALL. The key was a literal, and the comment beside it said
# what it is for: it becomes `group_spec.name` on chain and is what stops a sibling
# repo's sweeper closing this pool mid-CI. But every consumer of this workflow shares one
# Console wallet, so a literal means every consumer's pools carry the SAME marker — and
# `reusable-akash-escrow-reaper.yml` requires a `placement-prefix` with no default
# precisely so a consumer cannot claim what is not its own. With one shared value, the
# only prefix matching a consumer's pools also matches everyone else's, including this
# repo's provider canary. Measured 2026-09-03 in Borduas-Holdings/blazing: three
# different placement keys across four producers, and the pools — the biggest spender —
# were the ones no prefix could safely claim.


def test_the_placement_key_is_optional_and_defaults_to_the_module_s_marker():
    """A caller that does not set it must be byte-identical to before the input existed.

    The default is asserted against `provenance.PLACEMENT_PREFIX` rather than typed here,
    so changing the module's marker cannot silently leave this workflow stamping the old
    one — the drift shape this repo fixes by importing constants instead of copying them.
    """
    from just_akash.provenance import PLACEMENT_PREFIX

    spec = INPUTS["placement-key"]
    assert spec.get("required") is False, (
        "placement-key must be optional, or every existing caller breaks"
    )
    assert spec["default"] == f"{PLACEMENT_PREFIX}runner", (
        f"the default is {spec['default']!r} but the module stamps {PLACEMENT_PREFIX!r} — "
        "a caller that sets nothing would get a marker no sweeper in this repo matches"
    )


def test_both_sdl_sites_take_the_key_from_the_input():
    """`placement.<KEY>` and `deployment.<svc>.<KEY>` must be the SAME key.

    Substituting one and leaving the other a literal renders an SDL whose deployment
    references a placement that does not exist — rejected at MsgCreateDeployment, with a
    message about the SDL rather than about this input.
    """
    render = _step("Render runner SDL")["run"]
    assert render.count("${PLACEMENT_KEY}:") == 2, (
        "expected the key under both `placement:` and `deployment.runner:`; found "
        f"{render.count('${PLACEMENT_KEY}:')}"
    )
    assert "just-akash-runner:" not in render, (
        "the SDL still hardcodes a placement key. The default belongs on the INPUT, where "
        "a caller can override it; hardcoded, every consumer shares one marker again."
    )
    assert "PLACEMENT_KEY: ${{ inputs.placement-key }}" in SRC, (
        "the render step does not receive the input"
    )


def test_the_guard_refuses_the_sibling_prefix_the_module_names():
    """The literal in the guard must be the module's, not a second copy of it.

    `provenance.SIBLING_REAPED_PREFIX` exists so this repo can assert it never collides
    with the sibling. A hand-typed copy in the workflow drifts from it silently, and the
    failure is a pool the sibling's scheduled sweeper closes mid-CI.
    """
    from just_akash.provenance import SIBLING_REAPED_PREFIX

    guard = _code(_step("Render runner SDL")["run"])
    assert f"{SIBLING_REAPED_PREFIX}*)" in guard, (
        f"the guard does not refuse {SIBLING_REAPED_PREFIX!r} — stamping the sibling's "
        "prefix hands our pool to their reaper"
    )


@pytest.mark.parametrize(
    "key,accepted",
    [
        ("just-akash-runner", True),
        ("just-akash-runner.", True),  # the register's own form, with the dot
        ("ci-blazing-pool", True),
        ("", False),
        ("   ", False),
        ("dcloud", False),
        # ⛔ WHITESPACE IS NOT COSMETIC HERE. `dcloud ` does not match the `dcloud` pattern,
        # so an unnormalised value walks past the reserved-key check and is then written
        # into the SDL, where YAML swallows the space and the deployment is stamped
        # `dcloud` after all. Raised by CodeRabbit on the PR that added this input.
        ("dcloud ", False),
        (" dcloud ", False),
        ("dfci-infra-runner", False),
        # ⛔ THE KEY IS INTERPOLATED INTO A YAML HEREDOC, so a value carrying `:` or a
        # newline does not make a bad key — it makes a DIFFERENT DOCUMENT.
        ("a: b", False),
        ("x\ny", False),
        ("-leading-dash", False),
        (".leading-dot", False),
    ],
)
def test_the_guard_actually_runs_and_decides(key, accepted, tmp_path):
    """Executed, not read. A `case` that never matches looks identical to one that does.

    `dcloud` is the Akash-wide DEFAULT placement name, used by most SDLs on the network
    and owned by nobody, so a reaper aimed at it matches strangers' deployments; the empty
    key cannot be written to chain at all; the sibling's prefix is actively reaped.
    """
    render = _step("Render runner SDL")["run"]
    script = tmp_path / "render.sh"
    script.write_text(render)
    env = {
        **os.environ,
        "GH_RUNNER_PAT": "x",
        "ORG": "o",
        "RUNNER_LABEL": "l",
        "POOL_SIZE": "1",
        "CPU": "1",
        "MEMORY": "1Gi",
        "STORAGE": "1Gi",
        "EPHEMERAL": "true",
        "PLACEMENT_KEY": key,
    }
    proc = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    if accepted:
        assert proc.returncode == 0, f"{key!r} was refused: {proc.stdout} {proc.stderr}"
    else:
        assert proc.returncode == 2, f"{key!r} was ACCEPTED (rc={proc.returncode})"
        assert "::error" in (proc.stdout + proc.stderr), "refused without saying why"


MUTATIONS = [
    (
        "placement-key keeps its default",
        lambda s: s.replace(
            "        required: false\n        default: just-akash-runner",
            "        required: true",
        ),
    ),
    (
        "SDL takes the key from the input",
        lambda s: s.replace("${PLACEMENT_KEY}:", "just-akash-runner:"),
    ),
    (
        "the guard still refuses the network default",
        lambda s: s.replace("            dcloud|dcloud-*)", "            never-matches-me)"),
    ),
    (
        '"default" not in tag-prefix',
        # ⚠ ANCHORED ON WHAT FOLLOWS tag-prefix, WHICH MOVED. This mutation used to end
        # at `just-akash-ref:`; adding `placement-key:` between the two silently stopped
        # it matching, and the harness caught that by refusing a mutation that no longer
        # changes the text. Re-anchor rather than loosen: a mutation that matches the
        # wrong block tests the wrong guard.
        lambda s: s.replace(
            "        required: true\n        type: string\n      placement-key:",
            "        required: true\n        type: string\n"
            "        default: 'ci-shared'\n      placement-key:",
        ),
    ),
    (
        "destroy stays narrow",
        lambda s: s.replace('"${JA[@]}" destroy --dseq "$DSEQ" -y', '"${JA[@]}" destroy --all -y'),
    ),
    ("discard uses MIN_POOL", lambda s: s.replace('-lt "${MIN_POOL}"', '-lt "${POOL_SIZE}"')),
    ("min-pool clamp", lambda s: s.replace('[ "$MIN_POOL" -ge 1 ]', '[ "$MIN_POOL" -ge 0 ]')),
    (
        "402 is distinct",
        lambda s: s.replace("failure_reason=WALLET_UNDERFUNDED", "failure_reason=INFRA"),
    ),
    ("sdl token redacted", lambda s: s.replace("grep -vE 'ACCESS_TOKEN'", "cat")),
    (
        "pool image matches the probe",
        lambda s: s.replace(
            "github-runner@sha256:7509763af8209796f3e7fde5fb536c742075ec1a59ad1b36e3c9c27bc3bafc67",
            "github-runner:latest",
        ),
    ),
    # The count must survive a multi-page org. Both shapes below are what a reader
    # "tidying up" the jq would plausibly write, and each restores the bug.
    ("online count is line-based", lambda s: s.replace("grep -c .", "head -1")),
    ("no aggregating jq under paginate", lambda s: s.replace("| .id'", "] | length'")),
    ("runner poll stays paginated", lambda s: s.replace("--paginate ", "")),
    # A throttled read must never be absorbed into "the provider delivered nothing".
    (
        "failed listing is not zero",
        lambda s: s.replace("2>/tmp/gh-runners.err", "2>/dev/null"),
    ),
    (
        "api failure has its own reason",
        lambda s: s.replace(
            "failure_reason=GITHUB_API_UNAVAILABLE", "failure_reason=RUNNER_NEVER_REGISTERED"
        ),
    ),
    ("throttle backs off", lambda s: s.replace("sleep 15", "sleep 5")),
    (
        "wait is time-budgeted",
        lambda s: s.replace(
            'while [ "$(date +%s)" -lt "$WAIT_DEADLINE" ]; do',
            'for i in $(seq 1 "$RUNNER_WAIT_TRIES"); do',
        ),
    ),
    # Wallet contention must not be laundered into a market verdict.
    (
        "contention is not capacity",
        lambda s: s.replace(
            "failure_reason=WALLET_TX_CONTENTION", "failure_reason=PROVIDER_CAPACITY"
        ),
    ),
    ("contention backoff is jittered", lambda s: s.replace("(RANDOM % 20) + 10", "15")),
    ("unclassified failures print evidence", lambda s: s.replace("tail -40 /tmp/ja.log", "true")),
    # provider-select: an input that validates but never reaches deploy is the
    # merged-not-invoked defect one level down; and the guard exists because the
    # deploy's deliberate `|| true` would swallow argparse's exit 2 on a bad value.
    (
        "select reaches the deploy call",
        lambda s: s.replace('"${SELECT_ARGS[@]}" "${PROV_ARGS[@]}"', '"${PROV_ARGS[@]}"'),
    ),
    (
        "select is validated before deploy",
        lambda s: s.replace("cheapest|emptiest)", "cheapest) # unguarded:"),
    ),
    # The consumer-facing trio: fetch OUR source, at the ref they pinned, and run there.
    (
        "checkout names just-akash",
        lambda s: re.sub(r"\n\s*repository: [^\n]*just-akash[^\n]*", "", s, count=1),
    ),
    (
        "cli ref is the pinned one",
        lambda s: s.replace("${{ inputs.just-akash-ref }}", "${{ github.ref }}"),
    ),
    (
        "just-akash-ref has no default",
        lambda s: s.replace(
            "        required: true\n        type: string\n      just-akash-repository:",
            "        required: true\n        default: 'main'\n"
            "        type: string\n      just-akash-repository:",
        ),
    ),
    (
        "uv runs from our checkout",
        lambda s: s.replace("working-directory: .just-akash", "working-directory: ."),
    ),
    (
        "checkout credentials",
        lambda s: s.replace("persist-credentials: false", "persist-credentials: true"),
    ),
]


# pytest's own exit codes. Only 0 and 1 mean "the suite RAN and produced a verdict":
#   0 all passed   1 tests failed   2 interrupted
#   3 internal error   4 USAGE ERROR   5 no tests collected
# 2-5 all exit non-zero while proving nothing about any guard.
_INNER_RAN = frozenset({0, 1})


def _classify_inner_run(returncode: int, out: str) -> tuple[str, str]:
    """RAN, or UNREADABLE with the reason. Never a verdict about the guard.

    ⛔ THE DISTINCTION THIS FUNCTION EXISTS FOR. "The mutation survived" and "the
    instrument did not run" are different findings, and only the first says anything
    about the guard under test. Collapsing them is how this harness told a maintainer
    that 27 WORKING guards were "decorative" — measured 2026-09-05: the inner run
    inherited `addopts = --cov=just_akash` from pyproject, the interpreter had no
    `pytest_cov`, and pytest exited 4 with `unrecognized arguments`. No "N failed"
    appeared, so every mutation read as survived.

    ⚠ THAT IS WORSE THAN FAILING OPEN. A gate that fails open loses a check. A harness
    that fails into a FALSE ACCUSATION invites someone to delete 27 real controls
    because it told them they were decorative.

    The old code guarded exactly one of the modes its own comment names -- "a collection
    error, an import failure or a crash all exit non-zero while proving nothing" -- and
    a usage error is the sibling it names in principle and misses in code. So this
    requires POSITIVE evidence of execution rather than the absence of one known
    failure: a zero proves neither that the suite ran nor that the guard held.
    """
    if "error during collection" in out or "errors during collection" in out:
        return "UNREADABLE", "the inner run failed to COLLECT, so no guard was evaluated"
    if returncode not in _INNER_RAN:
        return "UNREADABLE", (
            f"pytest exited {returncode} (not 0/1), which means it did not run the suite "
            f"— usage error, internal error, interruption, or nothing collected"
        )
    # ⚠ `errors?` BELONGS HERE. A test that ERRORS (a fixture raising, say) is a test
    # that was collected and attempted — the suite demonstrably ran. Measured 2026-09-05:
    # a fixture raising RuntimeError gives exit 1 and a summary of "1 warning, 1 error in
    # 0.21s" with no passed/failed/skipped/xfailed anywhere, so the old regex called a
    # genuine run UNREADABLE. That direction is safe here (the caller asserts RAN, so it
    # fails loudly rather than silently) but it is still a false alarm on a real result.
    #
    # This does NOT re-admit collection errors: those are caught above by name and again
    # by exit code 2, which is not in _INNER_RAN. Both guards still stand in front.
    if not re.search(r"\d+ (?:passed|failed|skipped|xfailed|errors?)", out):
        return "UNREADABLE", (
            "the inner run reported no test outcomes at all, so nothing was evaluated"
        )
    return "RAN", ""


# ⛔ REAL pytest summary lines, captured 2026-09-05 by actually running each shape rather
# than by writing down what pytest is believed to print. The fixture-error row is the one
# the classifier used to get wrong: exit 1, a genuine run, and not one of
# passed/failed/skipped/xfailed anywhere in the summary.
_CLASSIFY_CASES = [
    (
        "fixture error is a RUN",
        1,
        "ERROR test_x.py::test_a - RuntimeError: boom\n"
        "========== 1 warning, 1 error in 0.21s ==========",
        "RAN",
    ),
    (
        "collection error is not",
        2,
        "ERROR test_x.py\n"
        "!!!!! Interrupted: 1 error during collection !!!!!\n"
        "========== 1 error in 0.09s ==========",
        "UNREADABLE",
    ),
    ("ordinary failure", 1, "========== 1 failed, 2 passed in 0.30s ==========", "RAN"),
    ("all green", 0, "========== 27 passed in 1.10s ==========", "RAN"),
    (
        "usage error — the original incident",
        4,
        "ERROR: unrecognized arguments: --cov=just_akash",
        "UNREADABLE",
    ),
    ("no outcomes at all", 1, "some stray output with no summary line", "UNREADABLE"),
]


@pytest.mark.parametrize("label,rc,out,want", _CLASSIFY_CASES, ids=[c[0] for c in _CLASSIFY_CASES])
def test_classify_inner_run_separates_a_run_from_an_instrument_failure(label, rc, out, want):
    """★ Pinned in BOTH directions: what must read as RAN, and what must not.

    A classifier tested only on the failures it was written for will happily
    misread a success — which is how a fixture error, exit 1 and unmistakably a
    real run, came back as "the instrument did not run".
    """
    verdict, _why = _classify_inner_run(rc, out)
    assert verdict == want, f"{label}: expected {want}, got {verdict}"


@pytest.mark.skipif(
    os.environ.get("RUNNER_POOL_WF") is not None,
    reason="inner mutation run — must not recurse",
)
@pytest.mark.parametrize("label,mutate", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_the_guards_are_not_vacuous(label, mutate, tmp_path):
    """Break the workflow on purpose, re-run every guard above against the broken copy,
    and require the suite to go RED.

    Asserting only that the mutation changed the text would prove nothing about the
    guards — that weaker shape is precisely how fourteen guards in a sibling repo came
    to assert nothing while reporting safety. This runs them.
    """
    mutated = mutate(SRC)
    assert mutated != SRC, f"mutation {label!r} no longer matches the workflow text"

    broken = tmp_path / "runner-pool.yml"
    broken.write_text(mutated)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            __file__,
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            # ⚠ THE PROXIMATE CAUSE, and it must be explicit. Without this the inner run
            # inherits `addopts = --cov=just_akash --cov-report=term-missing` from
            # pyproject.toml, so on any interpreter without `pytest_cov` it dies with a
            # usage error before running a single test. The harness then has no outcomes
            # to read and — before the classification below — called that a surviving
            # mutation. This makes the inner run independent of the outer environment.
            "-o",
            "addopts=",
        ],
        env={**os.environ, "RUNNER_POOL_WF": str(broken)},
        capture_output=True,
        text=True,
    )
    out = proc.stdout + proc.stderr

    # A non-zero exit is NOT enough, and neither is the absence of one known failure
    # mode. Establish that the instrument RAN before reading anything as a verdict
    # about the guard — see `_classify_inner_run`.
    verdict, why = _classify_inner_run(proc.returncode, out)
    assert verdict == "RAN", (
        f"mutation {label!r}: UNREADABLE — {why}. This says NOTHING about the guard; "
        f"do not read it as 'the guard is decorative'.\n{out[-1500:]}"
    )
    assert re.search(r"\d+ failed", out), (
        f"mutation {label!r} left the suite GREEN — the guard for it is decorative.\n{out[-1500:]}"
    )


# --------------------------------------------------------------------------
# runner-teardown.yml — two things leak, and only one of them is the lease
# --------------------------------------------------------------------------

# Resolved from the REPO, never from WF_PATH. Deriving it from WF_PATH.parent meant that
# during the mutation pass — where WF_PATH points at a temp copy — this became
# tmp_path/runner-teardown.yml, which does not exist. The module then raised
# FileNotFoundError at import, the inner pytest exited non-zero during COLLECTION, and
# `assert proc.returncode != 0` was satisfied by the import error for every mutation,
# including ones whose guard checks nothing. The anti-vacuity harness was itself vacuous.
TD_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/runner-teardown.yml"
TD_SRC = TD_PATH.read_text(encoding="utf-8")
TD = yaml.safe_load(TD_SRC)
TD_STEPS = TD["jobs"]["teardown"]["steps"]
TD_CLOSE = next(s for s in TD_STEPS if s.get("id") == "close")
TD_DEREG = next(s for s in TD_STEPS if s.get("id") == "dereg")


def test_teardown_verifies_the_close_instead_of_trusting_the_exit_code():
    """`just-akash destroy` exits non-zero while printing 'Deployment closed', and a
    zero exit is not proof either. Reporting a success we did not achieve is how
    leases accumulate for weeks while every run looks green."""
    body = TD_CLOSE["run"]
    assert '"${JA[@]}" status' in body and ".get('state'" in body.replace('"', "'")
    assert body.index('"${JA[@]}" destroy') < body.rindex('"${JA[@]}" status'), (
        "must read state back AFTER destroying"
    )


def test_an_already_closed_lease_is_a_success():
    assert re.search(r"Deployment closed\|already closed\|not found", TD_CLOSE["run"])


def test_an_unclosed_lease_fails_loudly_and_says_what_it_will_break():
    """A silently-leaked lease makes the NEXT run's funding failure look like a market
    outage — which is the misdiagnosis this whole effort exists to remove."""
    body = TD_CLOSE["run"]
    assert "closed=false" in body
    assert "::error title=" in body and "escrow" in body.lower()
    assert "just-akash destroy --dseq" in body, "must tell the operator how to fix it by hand"


def test_no_dseq_is_a_noop_not_a_failure():
    """The pool can fail before taking any lease; a red teardown there would mask the
    real failure with a second one."""
    assert "closed=noop" in TD_CLOSE["run"]


def test_deregistration_runs_even_when_the_close_failed():
    """The registration outlives the pod, so it leaks independently of the lease."""
    assert "always()" in str(TD_DEREG.get("if", ""))


def test_deregistration_is_scoped_to_this_runs_label():
    """An org-wide 'delete every offline runner' races other repos' provisioning,
    where a runner is briefly offline between registering and coming up."""
    body = TD_DEREG["run"]
    assert "${RUNNER_LABEL}" in body
    assert 'select(.status=="offline")' in body.replace('\\"', '"')


def test_deregistration_sees_every_page_of_the_org():
    """This step is where pagination bites hardest, in both directions.

    Without --paginate it only ever sees the first 100 runners, so the offline
    registrations that overflowed page 1 — the exact ones that broke provisioning for
    every repo in the org — are the ones it can never clean, and the leak is
    self-sustaining.

    And the filter must stay a STREAM of ids: `gh api --paginate --jq` runs the filter
    per page and concatenates, so `.runners[] | ... | .id` yields a correct id list
    across pages while any aggregating form yields one value per page.
    """
    body = TD_DEREG["run"]
    assert "--paginate" in body, "page 1 only cannot drain a listing that overflowed"
    assert "| length" not in body, "an aggregate emits one value per page, not one per runner"
    assert re.search(r"\|\s*\.id", body), "must emit one id per line to survive concatenation"


def test_an_unreadable_listing_is_not_reported_as_a_clean_sweep():
    """Same conflation as the pool's poll, and worse here. `|| true` turned a throttled
    `gh` into an empty IDS, and the step then published deregistered=0 AND
    deregister_failed=0 — a clean sweep it never performed, over registrations it never
    enumerated. That silence is exactly what lets the listing grow, which raises the
    request cost of every later poll."""
    body = TD_DEREG["run"]
    assert "2>/dev/null || true" not in body, "discarding the failure reports a false zero"
    assert re.search(r"GH_RC=\$\?", body), "the listing query's exit status must be captured"
    assert "unmeasured" in body, (
        "an unreadable listing needs a value distinct from 0 — they are different claims"
    )


def test_wallet_policy_is_not_reimplemented_in_workflow_shell():
    """Balance ranking and DSEQ ownership belong to just-akash, not copied shell."""
    code = _code(PROVISION["run"]) + _code(TD_CLOSE["run"])
    assert "RUN_ID %" not in code
    assert "mapfile -t KEYS" not in code
    assert "richest funded account" in PROVISION["run"]


def test_the_wallet_key_never_leaves_via_an_output():
    """Outputs are persisted and surfaced to the caller. The KEY is a credential; only the
    index and the ADDRESS may travel."""
    for name, val in OUTPUTS.items():
        assert "AKASH_API_KEY" not in str(val.get("value", "")), name
    assert "wallet_address" in OUTPUTS and "wallet_index" in OUTPUTS


def test_pool_and_teardown_pass_the_complete_wallet_pool_to_just_akash():
    assert PROVISION["env"]["AKASH_API_KEYS"]
    assert TD_CLOSE["env"]["AKASH_API_KEYS"]
    assert '"${JA[@]}" deploy' in _code(PROVISION["run"])
    assert '"${JA[@]}" destroy --dseq "$DSEQ"' in _code(TD_CLOSE["run"])


def test_required_deposit_drives_native_wallet_funding_floor():
    assert PROVISION["env"]["REQUIRED_DEPOSIT_USD"] == "${{ inputs.required-deposit-usd }}"
    assert '--deposit "$REQUIRED_DEPOSIT_USD"' in _code(PROVISION["run"])


def test_a_single_key_behaves_exactly_as_before():
    """AKASH_API_KEYS is optional. An empty pool must fall through to AKASH_API_KEY with
    no change in behaviour, or adding the input would break every existing caller."""
    assert INPUTS.get("providers") is not None  # sanity: we are reading the right doc
    assert CALL["secrets"]["AKASH_API_KEYS"]["required"] is False
    assert CALL["secrets"]["AKASH_API_KEY"]["required"] is False


def test_teardown_routes_by_dseq_instead_of_wallet_position():
    """The CLI must receive the DSEQ and full pool; it resolves the owner internally."""
    body = TD_CLOSE["run"]
    assert "positively reads" in body
    assert '"${JA[@]}" destroy --dseq "$DSEQ"' in body
    assert "WANT_ADDR" not in body


def test_wallet_address_is_optional_compatibility_data_not_a_safety_dependency():
    td_call = (TD.get("on") or TD.get(True))["workflow_call"]
    assert td_call["inputs"]["wallet-address"]["required"] is False
    assert "Deprecated compatibility" in td_call["inputs"]["wallet-address"]["description"]


def test_teardown_does_not_claim_an_ownership_check_it_cannot_perform():
    """The original form of this guard forbade a refusal path outright, because the
    ownership check then on the table was a TAG readback: just-akash tags live in a local
    file and `status --json` emits no tag, so a cross-job lookup returns empty every time
    and the gate would take its 'could not verify, proceed anyway' branch on every run
    while reporting that ownership had been verified.

    A refusal path is now correct — but only because it rests on something that can
    actually answer. The wallet check reads the account back from `balance --json` and
    compares it to the address the pool published, so it has three real outcomes: match,
    mismatch, unreadable. What must stay banned is the thing that never worked."""
    body = TD_CLOSE["run"]
    assert "get('tag'" not in body.replace('"', "'"), "status --json has no tag field"
    if "closed=refused" in body:
        assert "WANT_ADDR" in body and "balance --json" in body, (
            "a refusal path is only legitimate when backed by a check that can fail — "
            "an address read back from the chain, not a tag that is never there"
        )
        # And it must not silently proceed when it could not check: an unreadable answer
        # is the case the tag version got permanently stuck in.
        assert "Could not identify the teardown wallet" in body


# --------------------------------------------------------------------------
# Escrow leaks found in review — a lease we parsed but walked away from
# --------------------------------------------------------------------------


def test_a_dseq_without_a_provider_is_still_closed():
    """`deploy` can emit a DSEQ with no `Provider:` line. Treating that identically to
    "no deployment" walks away from a REAL lease: untagged, undestroyed, holding escrow
    against the grant the next attempt spends from."""
    body = PROVISION["run"]
    assert '[ -n "$DSEQ" ] && [ -z "$PROVIDER" ]' in body, "the orphan branch is missing"
    orphan = body[body.index('[ -n "$DSEQ" ] && [ -z "$PROVIDER" ]') :]
    assert '"${JA[@]}" destroy --dseq "$DSEQ"' in orphan[:900], "an orphan dseq must be destroyed"


def test_an_unreadable_state_is_not_reported_as_closed():
    """An empty STATE means we could not READ it — a transient API error, a non-zero
    exit, unparseable JSON. Reporting closed=true there is a success we did not achieve,
    which is the precise failure this step exists to prevent."""
    body = TD_CLOSE["run"]
    assert "closed=unknown" in body, "an unverifiable close must not report success"
    import re as _re

    m = _re.search(r'case "\$STATE" in\s*\n\s*([^\n)]*)\)', body)
    assert m and m.group(1).strip() == "closed", (
        f"the first case arm must be 'closed' alone, not {m.group(1) if m else None!r} — "
        "sharing it with '' is how an unreadable state reported success"
    )
    assert "already closed|not found" in body, "only a provably-gone deployment may pass on ''"


# --------------------------------------------------------------------------
# A credential failure must never masquerade as a provider failure
# --------------------------------------------------------------------------


def test_the_pat_is_validated_before_provisioning():
    """A PAT expiry is otherwise SILENT: the runner never registers, the pool times out
    after ~15 minutes, and the run reports RUNNER_NEVER_REGISTERED — indistinguishable
    from a provider that leases and never schedules. That reading sends the investigation
    at Akash and ends in "switch back to hosted runners", which is the bill this workflow
    exists to remove. One API call turns 15 silent minutes into a named failure."""
    names = [s.get("name", "") for s in STEPS]
    pat_i = next(i for i, n in enumerate(names) if "PAT must still be valid" in n)
    prov_i = next(i for i, n in enumerate(names) if "Provision" in n)
    assert pat_i < prov_i, "the PAT check must run before any lease is taken"


def test_an_expired_pat_is_not_reported_as_a_provider_failure():
    body = _step("PAT must still be valid")["run"]
    assert "failure_reason=RUNNER_PAT_INVALID" in body
    assert "RUNNER_NEVER_REGISTERED" in body, (
        "the message must name the symptom it prevents, or the next reader will not "
        "connect a 15-minute timeout to a credential"
    )
    assert "not a provider" in body.lower() and "rotate" in body.lower()


def test_a_missing_pat_is_distinct_from_an_invalid_one():
    """Never set and expired need different remedies — one is configuration, the other
    is rotation."""
    body = _step("PAT must still be valid")["run"]
    assert "failure_reason=RUNNER_PAT_MISSING" in body


def test_the_pat_failure_reason_reaches_the_caller():
    assert "steps.pat.outputs.failure_reason" in DOC["jobs"]["pool"]["outputs"]["failure_reason"]
    assert "RUNNER_PAT_INVALID" in OUTPUTS["failure_reason"]["description"]


# ── cross-repo callability ───────────────────────────────────────────────────


def test_no_job_in_this_reusable_uses_a_bare_local_path():
    """⛔ `./` IN A REUSABLE RESOLVES IN THE CALLER'S TREE, NOT OURS.

    A reusable workflow's job runs in the caller's context, so `uses: ./…` is looked up
    in the CONSUMER's repository — where the file does not exist. The job cannot be
    created, the graph cannot be built, and the consumer's run dies with `jobs=0`: a
    startup_failure rendered as a generic "workflow file issue" against THEIR workflow.

    ⚠ IT PASSES IN THIS REPO'S OWN CI EITHER WAY, which is why it shipped — here the
    caller IS just-akash. A reusable workflow cannot test its own cross-repo callability
    from inside its own repo, so this static check is the only thing that can.

    Measured 2026-09-03 (just-akash#247): Borduas-Holdings/blazing bumped past #243 and
    both of its Akash workflows returned startup_failure with zero jobs. And it is a
    recurrence — akash-github-runner#149 was the same bug with the same signature, one
    repo over.
    """
    for job_name, job in DOC["jobs"].items():
        uses = str(job.get("uses") or "")
        if not uses:
            continue
        assert re.fullmatch(REUSABLE_WORKFLOW_REF, uses), (
            f"job {job_name!r} calls {uses!r}. From a consumer, anything but the full "
            "owner/repo path resolves in THEIR tree. Use "
            "<owner>/<repo>/.github/workflows/<file>.yml@<40-hex sha>."
        )


LOCAL_FORMS_THAT_MUST_BE_REJECTED = [
    "./.github/workflows/runner-teardown.yml@" + "a" * 40,
    "././.github/workflows/runner-teardown.yml@" + "a" * 40,
    "../.github/workflows/runner-teardown.yml@" + "a" * 40,
    "../../.github/workflows/runner-teardown.yml@" + "a" * 40,
    ".github/workflows/runner-teardown.yml@" + "a" * 40,
    "runner-teardown.yml@" + "a" * 40,
    "Digital-Frontier-LDA/just-akash/.github/workflows/runner-teardown.yml@main",
]


@pytest.mark.parametrize("uses", LOCAL_FORMS_THAT_MUST_BE_REJECTED)
def test_every_caller_relative_or_unpinned_form_is_rejected(uses):
    """`not uses.startswith("./")` was the whole guard, and it let five of these through.

    Reported by Copilot review on just-akash#248. `.github/workflows/…`, `../…` and a
    bare filename all resolve in the CONSUMER's tree exactly as `./` does — the guard
    would have gone green on a recurrence of just-akash#247. The last case is unpinned:
    a moving ref lets the close logic change under a consumer that changed nothing.
    """
    assert not re.fullmatch(REUSABLE_WORKFLOW_REF, uses)


def test_the_real_reference_is_accepted():
    """Known-negative: the reference runner-pool.yml actually carries must still pass.

    Read from the workflow rather than written out here. A literal 40-hex SHA in a test
    is flagged by detect-secrets as a high-entropy string (it was, on this PR), and a
    pasted pin also goes stale the moment the real one is bumped.
    """
    assert re.fullmatch(REUSABLE_WORKFLOW_REF, str(DOC["jobs"]["teardown"]["uses"]))


def test_the_nested_teardown_pin_matches_the_file_it_calls():
    """The pinned teardown must be byte-identical to the working copy.

    Referencing by pin means the pool calls the teardown as it was at that SHA. Harmless
    while they agree, and silent drift the moment they do not — which is the failure a pin
    is supposed to prevent. Asserting identity forces the bump into the SAME change that
    edits the teardown.

    ⚠ NOT "lags by exactly one commit" — an earlier version of this docstring claimed that
    and the repo cannot guarantee it: multi-commit PRs and squash merges both break the
    distance. What is enforced is IDENTITY, which is the property that matters; commit
    distance is not.

    ⛔ AND THIS GUARD MUST NOT SKIP IN CI. `actions/checkout` fetches shallow, so the
    pinned commit is usually absent and `git show` fails — turning the whole check into a
    silent skip on the one surface it exists to protect. That is the "a check that cannot
    fail" class this repo keeps finding. So: fetch the object on demand, and if it still
    cannot be read, FAIL under CI and skip only on a developer machine.
    """
    import os
    import subprocess

    uses = str(DOC["jobs"]["teardown"]["uses"])
    pin = uses.rsplit("@", 1)[-1]
    assert re.fullmatch(r"[0-9a-f]{40}", pin), f"teardown pinned to {pin!r}, not a 40-hex SHA"

    # ⚠ From __file__, never from WF_PATH: the mutation harness overrides RUNNER_POOL_WF to
    # a temp copy, and deriving the repo root from it would point git at /tmp.
    root = pathlib.Path(__file__).resolve().parents[1]

    def _show() -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "show",
                f"{pin}:.github/workflows/runner-teardown.yml",
            ],
            capture_output=True,
            timeout=60,
        )  # ⚠ no text=True: decoding hides a CRLF/LF difference, and this asserts BYTES

    shown = _show()
    if shown.returncode != 0:
        # Shallow clone: ask for just this object, then retry once.
        subprocess.run(
            ["git", "-C", str(root), "fetch", "--depth=1", "origin", pin],
            capture_output=True,
            text=True,
            timeout=120,
        )
        shown = _show()

    if shown.returncode != 0:
        detail = shown.stderr.decode("utf-8", "replace").strip()[:120]
        assert not os.environ.get("CI"), (
            f"cannot read runner-teardown.yml at the pinned {pin[:8]} even after fetching "
            f"({detail}). Under CI this is a FAILURE, not a skip: a drift guard that skips "
            "on the surface it protects is a check that cannot fail."
        )
        pytest.skip(f"pinned commit {pin[:8]} unavailable locally: {detail}")

    # ⚠ read_bytes, not read_text. The docstring claims byte-identity; comparing decoded
    # text would make a line-ending difference invisible and the claim false — an overclaim
    # of the same kind this file already corrected once.
    current = (root / ".github/workflows/runner-teardown.yml").read_bytes()
    assert shown.stdout == current, (
        f"runner-teardown.yml has changed since the pinned {pin[:8]}, so the pool calls a "
        "STALE copy of its own teardown. Bump the pin in this change."
    )


# ==========================================================================
# The harness's own harness.
#
# `test_the_guards_are_not_vacuous` renders a verdict about 27 real guards. On
# 2026-09-05 it rendered the WRONG one: the inner run inherited
# `addopts = --cov=just_akash` from pyproject, the interpreter had no
# `pytest_cov`, pytest exited 4 with `unrecognized arguments`, no "N failed"
# appeared, and every mutation was reported as "the guard for it is decorative".
#
# ⛔ THAT IS WORSE THAN FAILING OPEN, which is why these tests exist. A gate that
# fails open loses a check. A harness that fails into a FALSE ACCUSATION invites
# a maintainer to DELETE 27 working controls because it told them to.
#
# Both error rates are pinned below. A classifier that returns UNREADABLE for
# everything would satisfy the first half and destroy the harness — it is the
# same defect wearing the safe colour.
# ==========================================================================


@pytest.mark.parametrize(
    "returncode,out,reason_fragment",
    [
        (
            4,
            "ERROR: usage: pytest [options]\nunrecognized arguments: --cov=just_akash\n",
            "exited 4",
        ),
        (5, "no tests ran in 0.01s\n", "exited 5"),
        (3, "INTERNALERROR> Traceback\n", "exited 3"),
        (2, "!!! KeyboardInterrupt !!!\n", "exited 2"),
        (1, "ERROR tests/x.py\n1 errors during collection\n", "COLLECT"),
        (0, "", "no test outcomes"),
    ],
    ids=[
        "usage-error",
        "nothing-collected",
        "internal-error",
        "interrupted",
        "collection-error",
        "silent-success",
    ],
)
def test_an_inner_run_that_did_not_execute_is_UNREADABLE_not_a_verdict(
    returncode, out, reason_fragment
):
    """★ THE FALSE-ACCUSATION SIDE.

    None of these say anything about a guard. Reporting any of them as "the guard
    is decorative" is an accusation the evidence cannot support — and the usage-error
    row is the one that actually fired.
    """
    verdict, why = _classify_inner_run(returncode, out)
    assert verdict == "UNREADABLE", f"rc={returncode} was read as a verdict about the guard"
    assert reason_fragment in why, f"the reason must name what happened, got: {why!r}"


@pytest.mark.parametrize(
    "returncode,out",
    [
        (1, "F....\n1 failed, 4 passed in 0.30s\n"),
        (0, ".....\n5 passed in 0.20s\n"),
        (1, "5 failed, 92 passed in 5.91s\n"),
        (0, "3 passed, 1 skipped in 0.10s\n"),
    ],
    ids=["one-failure", "all-passed", "many-failures", "passed-with-skips"],
)
def test_a_run_that_really_executed_is_RAN(returncode, out):
    """★ THE ANTI-VACUITY SIDE, and it is the half that keeps the harness alive.

    A classifier returning UNREADABLE for everything would pass every test above
    and silently disable all 27 guard checks — the same defect in the safe colour.
    These are the shapes that MUST still reach a verdict.
    """
    verdict, why = _classify_inner_run(returncode, out)
    assert verdict == "RAN", f"a real run (rc={returncode}) was suppressed as UNREADABLE: {why}"


def test_the_surviving_mutation_verdict_still_reaches_its_conclusion():
    """A genuinely-surviving mutation must still be reported as decorative.

    The point of the UNREADABLE state is to remove FALSE accusations, not to remove
    the harness's ability to accuse at all. An inner run that executed and reported
    zero failures is exactly the case the harness exists to catch.
    """
    out = ".....\n5 passed in 0.20s\n"
    verdict, _ = _classify_inner_run(0, out)
    assert verdict == "RAN", "an executed run must be judgeable"
    assert not re.search(r"\d+ failed", out), "and this one legitimately shows no failures"


def test_the_inner_run_does_not_inherit_addopts():
    """The proximate cause, pinned at the call site.

    Without an explicit `-o addopts=` the inner pytest picks up
    `--cov=just_akash --cov-report=term-missing` from pyproject.toml and dies with a
    usage error on any interpreter lacking `pytest_cov`. The UNREADABLE verdict now
    stops that from becoming an accusation; this stops it from happening at all.
    """
    src = pathlib.Path(__file__).read_text(encoding="utf-8")
    call = src[src.index("def test_the_guards_are_not_vacuous") :]
    call = call[: call.index("out = proc.stdout")]
    assert '"-o",' in call and '"addopts=",' in call, (
        "the inner pytest invocation must pass `-o addopts=` so it does not depend on "
        "the outer environment having every plugin pyproject's addopts names"
    )
