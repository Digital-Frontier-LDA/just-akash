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

import ast
import os
import pathlib
import re
import shutil
import subprocess
import sys
import textwrap
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
    assert body.index('"${JA[@]}" tag') < body.index("exact JIT runners online"), (
        "tag must precede the runner wait, or a cancellation leaks an untagged lease"
    )


# --------------------------------------------------------------------------
# Teardown may only ever destroy what this loop created
# --------------------------------------------------------------------------


def test_teardown_targets_one_locally_parsed_dseq():
    """A sweep destroyed 14 third-party deployments once. Every destroy here must name
    a single DSEQ parsed from this job's own deploy output and carry the owner emitted by
    that same create attempt. The count pins every immediate rollback call site."""
    destroys = re.findall(r'"\$\{JA\[@\]\}" destroy[^\n]*', PROVISION["run"])
    assert len(destroys) == 4
    for line in destroys:
        assert '--dseq "$DSEQ"' in line, f"destroy must name this run's dseq: {line}"
        assert '--expected-owner "$WALLET"' in line, (
            f"destroy must retain this create attempt's owner: {line}"
        )
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
# JIT pools are one-job identities — partial handoff is not a usable topology
# --------------------------------------------------------------------------


def test_the_usable_threshold_is_the_complete_slot_population():
    body = PROVISION["run"]
    start = body.index('if [ "${ONLINE:-0}"')
    discard = body[start : body.index('if [ -z "${RUNNER_VERSIONS:-}"', start)]
    assert '-lt "${POOL_SIZE}"' in discard
    assert "cleanup_jit_attempt || exit 1" in discard


def test_pool_size_is_derived_from_validated_slots():
    assert PROVISION["env"]["POOL_SIZE"] == "${{ steps.render.outputs.slot_count }}"
    assert "pool-size" not in INPUTS and "min-pool-size" not in INPUTS


def test_a_partial_pool_is_never_announced_as_accepted():
    assert "Partial pool accepted" not in PROVISION["run"]


# --------------------------------------------------------------------------
# GitHub paginates — and an aggregating --jq silently inverts this job's verdict
# --------------------------------------------------------------------------


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
    poll = code[code.index("OBSERVATION=") : code.index('if [ "$API_OK"')]
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
    poll = body[body.index("OBSERVATION=") : body.index('if [ "$API_OK"')]
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
    assert 'observe --journal "$JIT_JOURNAL"' in poll


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


def test_the_pool_requires_a_digest_pinned_jit_capable_image():
    """A provider earns runner_host by scheduling the PROBE image three consecutive
    times. Running a different image in the pool means the pool is trusting a
    measurement taken of something else — and `:latest` made that gap permanent and
    silent, since the tag can move between the qualification and the run relying on it.

    The probe SDL already explains why it pins a digest; this asserts the pool did not
    quietly opt out of that reasoning."""
    spec = INPUTS["runner-image"]
    assert spec["required"] is True and "default" not in spec
    assert '--image "$RUNNER_IMAGE"' in PROVISION["run"]


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


def test_runner_targets_are_a_slot_keyed_exact_routing_map():
    job_out = DOC["jobs"]["pool"]["outputs"]["runner-targets"]
    assert job_out == "${{ steps.render.outputs.runner_targets }}"
    assert "runner-targets" in OUTPUTS, "the fallback never reaches the caller"


def test_runner_targets_are_derived_before_credentials_or_deploy():
    render = _step("Render runner SDL")["run"]
    assert "jit_pool topology" in render and '--runner-label "$RUNNER_LABEL"' in render


def test_the_pool_label_carries_run_identity():
    """A shared static label lets one run's jobs land on another run's runners."""
    assert "RUNNER_LABEL: ${{ inputs.runner-label }}" in SRC


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------


def test_the_secret_bearing_sdl_is_never_echoed():
    body = PROVISION["run"]
    assert "cat $JIT_SDL" not in body and 'cat "$JIT_SDL"' not in body
    assert 'deploy --sdl "$JIT_SDL"' in body


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


def test_generated_sdl_takes_the_attributed_key_from_the_input():
    """`placement.<KEY>` and `deployment.<svc>.<KEY>` must be the SAME key.

    Substituting one and leaving the other a literal renders an SDL whose deployment
    references a placement that does not exist — rejected at MsgCreateDeployment, with a
    message about the SDL rather than about this input.
    """
    body = PROVISION["run"]
    assert '--placement "$DEPLOYMENT_GROUP"' in body
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
    script.write_text(render, encoding="utf-8")
    env = {
        **os.environ,
        "GH_RUNNER_PAT": "x",
        "ORG": "o",
        "RUNNER_LABEL": "l",
        "RUNNER_SLOTS": '["one"]',
        "RUNNER_GROUP_ID": "17",
        "PLACEMENT_KEY": key,
        # The step sets this from `github.run_id` for the attribution stamp (#311). The
        # harness must supply what the real step supplies, or it tests a different script.
        "GH_RUN_ID": "34228480597",
        "GH_RUN_ATTEMPT": "2",
        "CREATE_OPERATION": "7",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
    }
    proc = subprocess.run(["bash", "-e", str(script)], env=env, capture_output=True, text=True)
    if accepted:
        assert proc.returncode == 0, f"{key!r} was refused: {proc.stdout} {proc.stderr}"
    else:
        assert proc.returncode == 2, f"{key!r} was ACCEPTED (rc={proc.returncode})"
        assert "::error" in (proc.stdout + proc.stderr), "refused without saying why"


def test_no_guard_is_satisfied_by_prose(tmp_path):
    """Re-run every guard against a workflow with ALL comments stripped, and require green.

    ⛔ These guards assert on the shell body, and a body contains its own explanation.
    A PRESENCE assertion against the raw text can therefore be satisfied by the comment
    describing the construct rather than the construct — it reports that a behaviour
    exists when only its description does, and it keeps passing after the behaviour is
    deleted. `_code()` exists for this, but nothing required its use, so one assertion
    drifted onto the raw body and went unnoticed.

    This is the complement of test_the_guards_are_not_vacuous: that one breaks the CODE
    and demands red, this one removes the PROSE and demands green. Between them a guard
    must depend on the code and only on the code.
    """
    # ⛔ RUN ONCE, AGAINST THE REAL WORKFLOW. test_the_guards_are_not_vacuous spawns an
    # inner pytest of this whole file per mutation with no -k filter, so without this
    # skip each of those ~29 runs would spawn ANOTHER full suite from here — roughly
    # tripling CI time to re-answer a question about the committed workflow that only
    # has one answer. RUNNER_POOL_WF is set exactly when we are that inner run.
    if os.environ.get("RUNNER_POOL_WF"):
        pytest.skip("inner run of the mutation harness; this guard runs once, outermost")

    stripped = "\n".join(ln for ln in SRC.splitlines() if not ln.lstrip().startswith("#"))
    assert stripped != SRC, "no comments were stripped — this guard would be vacuous"
    copy = tmp_path / "runner-pool.yml"
    copy.write_text(stripped + "\n", encoding="utf-8")

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
            "--no-cov",
            "-k",
            "not vacuous and not prose",
        ],
        env={**os.environ, "RUNNER_POOL_WF": str(copy)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        "a guard passes on the commented workflow and fails without comments, so it is "
        "asserting on prose rather than on code:\n" + proc.stdout[-3000:]
    )


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
        lambda s: s.replace('--placement "$DEPLOYMENT_GROUP"', '--placement "just-akash-runner"'),
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
        "destroy stays owner-bound and narrow",
        lambda s: s.replace(
            '"${JA[@]}" destroy --dseq "$DSEQ" --expected-owner "$WALLET" '
            '--expected-group "$DEPLOYMENT_GROUP" -y',
            '"${JA[@]}" destroy --all -y',
        ),
    ),
    ("discard requires every slot", lambda s: s.replace('-lt "${POOL_SIZE}"', '-lt "0"')),
    (
        "pool size comes from slots",
        lambda s: s.replace(
            "POOL_SIZE: ${{ steps.render.outputs.slot_count }}",
            "POOL_SIZE: ${{ inputs.pool-size }}",
        ),
    ),
    (
        "402 is distinct",
        lambda s: s.replace("failure_reason=WALLET_UNDERFUNDED", "failure_reason=INFRA"),
    ),
    (
        "secret SDL is not dumped",
        lambda s: s.replace(
            'deploy --sdl "$JIT_SDL"',
            'deploy --sdl "$JIT_SDL"; cat "$JIT_SDL"',
        ),
    ),
    (
        "pool image matches the probe",
        lambda s: s.replace(
            "      runner-image:\n        description:",
            "      runner-image:\n"
            "        default: ghcr.io/example/runner:latest\n"
            "        description:",
        ),
    ),
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
    broken.write_text(mutated, encoding="utf-8")
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


def test_teardown_verifies_the_close_instead_of_trusting_the_exit_code(tmp_path):
    from tests.test_runner_teardown_shell_probes import test_real_close_step

    test_real_close_step(tmp_path, "console_closed_chain_active")


def test_an_already_closed_lease_is_a_success(tmp_path):
    from tests.test_runner_teardown_shell_probes import test_real_close_step

    test_real_close_step(tmp_path, "agreeing_terminal")


def test_an_unclosed_lease_fails_loudly_and_says_what_it_will_break(tmp_path):
    from tests.test_runner_teardown_shell_probes import test_real_close_step

    test_real_close_step(tmp_path, "active")
    assert "::error title=" in TD_CLOSE["run"]
    assert "escrow" in TD_CLOSE["run"]
    assert "just-akash verify-closed --dseq" in TD_CLOSE["run"]


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
    assert "select(.busy==false)" in body.replace('\\"', '"')


def test_deregistration_prefers_exact_ids_when_the_pool_published_them():
    pool_teardown = DOC["jobs"]["teardown"]
    assert pool_teardown["with"]["runner-ids"] == "${{ needs.pool.outputs.runner-ids }}"
    body = TD_DEREG["run"]
    assert "RUNNER_IDS_JSON: ${{ inputs.runner-ids }}" in TD_SRC
    assert "printf '%s' \"$RUNNER_IDS_JSON\"" in body
    assert 'gh api -X DELETE "orgs/${ORG}/actions/runners/${id}"' in body


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
    # ⛔ ON _code(), NOT THE RAW BODY. This asserted "richest funded account", which
    # appears ONLY in the comment above the delegation — so it passed by reading prose
    # and would have kept passing with the delegation deleted. Its two siblings above
    # already use _code(); this line did not, and that asymmetry is the whole bug.
    # (Found by test_no_guard_is_satisfied_by_prose. Reported by CodeRabbit on #253.)
    assert "JA=(uv run --with . just-akash)" in _code(PROVISION["run"]), (
        "wallet selection must be DELEGATED — the workflow invokes just-akash rather "
        "than ranking balances in shell"
    )


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
    teardown_code = _code(TD_CLOSE["run"])
    assert 'DESTROY_ARGS=(--dseq "$DSEQ" -y)' in teardown_code
    assert '"${JA[@]}" destroy "${DESTROY_ARGS[@]}"' in teardown_code


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
    """The resolver and mutating process must receive the same DSEQ and bound owner."""
    body = _code(TD_CLOSE["run"])
    assert 'DESTROY_ARGS=(--dseq "$DSEQ" -y)' in body
    assert 'DESTROY_ARGS+=(--expected-owner "$OWNER")' in body
    assert '"${JA[@]}" destroy "${DESTROY_ARGS[@]}"' in body
    assert "WANT_ADDR" not in body


def test_wallet_address_is_optional_but_has_a_bound_owner_safety_path():
    td_call = (TD.get("on") or TD.get(True))["workflow_call"]
    assert td_call["inputs"]["wallet-address"]["required"] is False
    description = td_call["inputs"]["wallet-address"]["description"]
    assert "configured Console credential" in description
    assert "exact owner/DSEQ chain read" in description
    body = TD_CLOSE["run"]
    assert body.count('RESOLVE_ARGS+=(--expected-owner "$WALLET_ADDRESS")') == 1
    assert body.count('DESTROY_ARGS+=(--expected-owner "$OWNER")') == 1


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


def test_a_dseq_without_a_provider_stops_before_another_create():
    """`deploy` can emit a DSEQ with no `Provider:` line. Treating that identically to
    "no deployment" walks away from a REAL lease: untagged, undestroyed, holding escrow
    against the grant the next attempt spends from."""
    body = PROVISION["run"]
    assert '[ -n "$DSEQ" ] && [ -z "$PROVIDER" ]' in body, "the orphan branch is missing"
    orphan = body[body.index('[ -n "$DSEQ" ] && [ -z "$PROVIDER" ]') :]
    assert "failure_reason=CREATE_WITHOUT_PROVIDER" in orphan[:900]
    assert "exit 1" in orphan[:900]
    assert "continue" not in orphan[:900]


def test_an_unreadable_state_is_not_reported_as_closed(tmp_path):
    from tests.test_runner_teardown_shell_probes import test_real_close_step

    test_real_close_step(tmp_path, "destroy_closed_chain_unavailable")


# --------------------------------------------------------------------------
# A missing credential is refused before any runner or lease exists.


def test_a_missing_pat_is_distinct_from_group_binding_failure():
    body = _step("runner administration credential")["run"]
    assert "failure_reason=RUNNER_PAT_MISSING" in body
    failure = DOC["jobs"]["pool"]["outputs"]["failure_reason"]
    assert "steps.pat.outputs.failure_reason" in failure
    assert "steps.group-binding.outputs.failure_reason" in failure


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


# ⛔ BOTH DIRECTIONS, including the case the substring form got wrong. The old check
# would have PASSED "separated" below — `-o` present, `addopts=` present, but the inner
# run still inheriting addopts because they were never a pair.
_ADDOPTS_CASES = [
    ("adjacent", ["-m", "pytest", "-o", "addopts=", "-q"], True),
    ("adjacent at end", ["-m", "pytest", "-o", "addopts="], True),
    ("separated", ["-o", "cov=x", "-q", "addopts=", "-p"], False),
    ("reversed", ["addopts=", "-o"], False),
    ("-o with another value", ["-o", "cache_dir=/tmp", "-q"], False),
    ("absent entirely", ["-m", "pytest", "-q"], False),
    ("non-literal in the slot", ["-o", None, "addopts="], False),
]


@pytest.mark.parametrize("label,argv,want", _ADDOPTS_CASES, ids=[c[0] for c in _ADDOPTS_CASES])
def test_the_addopts_matcher_requires_adjacency(label, argv, want):
    """★ The pin's own control. A matcher tested only on the passing case cannot see
    the arrangement that satisfies it while the behaviour is absent."""
    assert _passes_o_addopts(argv) is want, f"{label}: expected {want}"


def _inner_pytest_argv(source: str) -> list[str | None]:
    """The literal argv list handed to `subprocess.run` inside the mutation harness.

    ⚠ PARSED, NOT GREPPED. The previous version substring-matched `\'"-o",\'` and
    `\'"addopts=",\'` in the source text, which was wrong in two directions:

      * it hard-coded DOUBLE quotes, so a formatter flipping quote style would fail a
        test whose behaviour had not changed (cries wolf), and
      * it never checked ADJACENCY, so `"-o", "something-else"` plus the string
        `"addopts="` anywhere else in the window — a comment, another argument — would
        satisfy it while the inner run still inherited addopts (fails OPEN).

    Non-literal elements come back as None so they cannot accidentally satisfy a pair.
    """
    tree = ast.parse(source)
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "test_the_guards_are_not_vacuous"
        ),
        None,
    )
    assert fn is not None, "the mutation harness function was renamed — this pin is stale"
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name != "run" or not node.args or not isinstance(node.args[0], ast.List):
            continue
        return [
            e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else None
            for e in node.args[0].elts
        ]
    raise AssertionError("no subprocess.run([...]) call found in the mutation harness")


def _passes_o_addopts(argv: list) -> bool:
    """`-o` immediately followed by `addopts=` — adjacency is the whole point."""
    # strict=False is correct and deliberate: the two sequences differ in length by
    # construction (pairwise over a single list), so strict=True would always raise.
    return any(a == "-o" and b == "addopts=" for a, b in zip(argv, argv[1:], strict=False))


def test_the_inner_run_does_not_inherit_addopts():
    """The proximate cause, pinned at the call site.

    Without an explicit `-o addopts=` the inner pytest picks up
    `--cov=just_akash --cov-report=term-missing` from pyproject.toml and dies with a
    usage error on any interpreter lacking `pytest_cov`. The UNREADABLE verdict now
    stops that from becoming an accusation; this stops it from happening at all.
    """
    argv = _inner_pytest_argv(pathlib.Path(__file__).read_text(encoding="utf-8"))
    assert _passes_o_addopts(argv), (
        "the inner pytest invocation must pass `-o addopts=` as ADJACENT arguments so it "
        f"does not inherit pyproject's addopts. Parsed argv: {argv}"
    )


def _verdict_script(tmp_path, response: str) -> tuple[str, str]:
    """Run the verdict step's credential re-check with `gh` stubbed.

    Extracted rather than run whole: the block lives inside a 501-line `run:`
    scalar. The slice is delimited by the VERDICT_RESP capture and the
    RUNNER_NEVER_REGISTERED line that follows the case, so a restructure that
    moves either one fails here rather than silently testing nothing.
    """

    body = _step("Provision")["run"]
    start = body.index("VERDICT_RESP=")
    # Cut at the LINE boundary, not the substring: ending mid-`echo "..."` leaves
    # an unterminated quote and bash dies at EOF with an empty output file — which
    # a test asserting "X not in out" would read as PASSING. An extractor that
    # produces a broken script fails the wrong way round.
    end = body.rindex("\n", 0, body.index("failure_reason=RUNNER_NEVER_REGISTERED")) + 1
    block = textwrap.dedent(body[start:end])
    tail = 'echo "failure_reason=RUNNER_NEVER_REGISTERED" >> "$GITHUB_OUTPUT"\n'

    out = tmp_path / "out.txt"
    script = tmp_path / "verdict.sh"
    script.write_text(
        "set -uo pipefail\n"
        "ORG=testorg\n"
        "VERIFIED_RUNNER_GROUP_ID=17\n"
        f'GITHUB_OUTPUT="{out}"\n'
        f': > "{out}"\n' + block + tail,
        encoding="utf-8",
    )
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "gh").write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$FAKE_RESP"\nexit "$FAKE_RC"\n', encoding="utf-8"
    )
    (fake / "gh").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
        "FAKE_RESP": response,
        "FAKE_RC": "0" if response.split()[1:2] in (["200"], ["201"]) else "1",
    }
    proc = subprocess.run(["bash", "-e", str(script)], env=env, capture_output=True, check=False)
    combined = (proc.stdout + proc.stderr).decode()
    # ⛔ VACUITY GUARD. Every test here asserts a reason is ABSENT, and absence is
    # what a broken extractor produces: a slice ending mid-`echo "..."` leaves an
    # unterminated quote, bash dies at EOF, and the output file is empty — so
    # `"RUNNER_NEVER_REGISTERED" not in out` passes while proving nothing. Measured:
    # that is exactly what happened on the first cut of this harness.
    assert "unexpected EOF" not in combined, f"the extracted block is not valid bash:\n{combined}"
    # ⛔ CONTENT, not existence. `out.exists()` was ALWAYS true: the generated script
    # runs `: > "$out"` on its 4th line, before the extracted block, so the file is
    # created on every invocation. `combined.strip() or out.exists()` was therefore
    # `<anything> or True` — a guard that could not fail on any input ever given to it.
    #
    # ⚠ AND IT PROTECTS NOTHING TODAY. Measured, not assumed. The comment this replaces
    # asserted "Every test here asserts a reason is ABSENT"; that is false. All six
    # callers also assert a PRESENCE — "runner_deny" in log, RUNNER_PAT_INVALID in out,
    # RUNNER_NEVER_REGISTERED in out, INDETERMINATE in out (x2), "no status" in log —
    # and a presence assertion already fails on empty output. Mutating the extractor to
    # emit nothing turns all six red with this line or without it.
    #
    # So this is a net for a caller that does not exist yet: an absence-ONLY one, which
    # the false premise above claims is the normal case here. Worth keeping correct
    # rather than deleting, because a guard that cannot fire advertises a protection
    # nothing provides — but do not credit it with catching anything that ships today.
    #
    # (Reported by Copilot on #253. See line 1257: the anti-vacuity harness for
    # runner-teardown was itself vacuous as well, for an unrelated reason. Twice.)
    produced = out.read_text(encoding="utf-8") if out.exists() else ""
    assert combined.strip() or produced.strip(), "the verdict block produced no output at all"
    return (produced, combined)


class TestBothProbesUseTheWriteVerb:
    """⛔ A BARE LIST READ CANNOT PROVE THAT THE CREDENTIAL CAN CREATE JIT RUNNERS.

    The preflight now checks only that the credential exists. The first API read
    verifies the exact group policy, and the first write creates the distinct JIT
    registrations that are journaled before any Akash deployment is created.
    """

    def test_jit_creation_is_the_only_write_and_is_journaled(self):
        preflight = _step("runner administration credential")["run"]
        provision = _step("Provision")["run"]
        assert "gh api" not in preflight
        assert "--method POST" not in preflight
        assert '"${JIT[@]}" prepare' in provision
        assert 'cat "$JIT_OUTPUT" >> "$GITHUB_OUTPUT"' in provision

    def test_the_preflight_no_longer_probes_with_a_bare_list_read(self):
        body = _step("runner administration credential")["run"]
        assert "actions/runners?per_page=1" not in body, (
            "a GET proves only that the PAT can LIST runners; the workload mints"
        )


class TestTheVerdictDoesNotBlameAProviderForOurCredential:
    """⛔ THE MORE SERIOUS SITE. This step decides whether to accuse a PROVIDER.

    Its own 401 text already said the container "mints its registration token
    with this same credential" — it NAMED the write path while testing a read.
    So a PAT that could list but not mint returned 200 here, the check fell
    through, and the run blamed a host that did nothing wrong.

    A preflight failing a run is recoverable and self-evident. A fabricated
    provider fault is somebody else's reputation, decided by a check that was
    asking the wrong question.
    """

    @pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
    def test_a_write_403_does_NOT_produce_a_provider_verdict(self, tmp_path):
        out, log = _verdict_script(tmp_path, "HTTP/2.0 403 Forbidden")
        assert "failure_reason=RUNNER_NEVER_REGISTERED" not in out, (
            "the credential was the fault and the run accused a provider"
        )
        assert "runner_deny" in log.lower()
        assert "do not runner_deny" in log.lower(), (
            "the operator must be told explicitly not to act on this"
        )

    @pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
    def test_a_write_401_does_NOT_produce_a_provider_verdict(self, tmp_path):
        out, _log = _verdict_script(tmp_path, "HTTP/2.0 401 Unauthorized")
        assert "failure_reason=RUNNER_PAT_INVALID" in out
        assert "failure_reason=RUNNER_NEVER_REGISTERED" not in out

    @pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
    def test_200_group_read_is_success_not_indeterminate(self, tmp_path):
        out, _log = _verdict_script(tmp_path, "HTTP/2.0 200 OK")
        assert "failure_reason=INDETERMINATE" not in out
        assert "failure_reason=RUNNER_NEVER_REGISTERED" in out, (
            "with the credential proven healthy, the provider verdict must stand"
        )

    @pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
    def test_an_unreadable_check_still_refuses_to_accuse(self, tmp_path):
        out, _log = _verdict_script(tmp_path, "dial tcp: i/o timeout")
        assert "failure_reason=INDETERMINATE" in out
        assert "failure_reason=RUNNER_NEVER_REGISTERED" not in out


class TestTheVerdictCannotFabricateAStatus:
    """⛔ A NON-HTTP FIRST LINE MUST NOT BECOME A STATUS CODE.

    `awk 'NR==1{print $2}'` takes the second WORD of whatever the first line is.
    A transport failure emits a plain error string, so `dial tcp: lookup ...
    i/o timeout` yields `tcp:` — and that word then SELECTS A CASE ARM in the
    step that decides whether to accuse a provider.

    ⚠ It matters more here than at the preflight, which captures `|| RC=$?` and
    branches on it. This capture ends `|| true`, so the status line is the ONLY
    evidence — an unguarded parse is the whole input, not one input of two.
    """

    @pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
    @pytest.mark.parametrize(
        "junk",
        [
            "dial tcp: lookup api.github.com: i/o timeout",
            "error: 403 something",
            "gh: command failed",
        ],
    )
    def test_a_non_http_first_line_reaches_no_accusatory_arm(self, tmp_path, junk):
        out, log = _verdict_script(tmp_path, junk)
        assert "failure_reason=INDETERMINATE" in out, (
            f"{junk!r} was parsed into a status instead of being rejected"
        )
        assert "failure_reason=RUNNER_NEVER_REGISTERED" not in out
        assert "do not runner_deny" in log.lower()

    @pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
    def test_the_word_that_would_have_been_taken_is_not_reported_as_a_status(self, tmp_path):
        """`error: 403 something` has `403` as its second word. Unguarded, that
        selects the rate-limit arm and reports a status the server never sent —
        a fabricated fact presented as a measurement."""

        _out, log = _verdict_script(tmp_path, "error: 403 something")
        assert "HTTP 403" not in log, "a status was invented from an error string"
        assert "no status" in log.lower()


# ⛔ AN ASSIGNMENT IS NOT A LINE. `re.match(r"\s*GONE=", ln)` — what this file used first —
# is a text matcher pointed at shell: it MISSES `then GONE=no`, `cmd; GONE=no` and
# `export GONE=no`, and it ACCEPTS `echo "GONE=yes"`, which assigns nothing. CodeRabbit
# caught it on just-akash#308. It is the same instrument-shape error as a `head -6` that
# made four build sites look like three, and a line scan that cannot see a multi-line
# call: a text instrument aimed at a structured artefact. Match the assignment TOKEN —
# at a command position, and never inside an echo/printf argument.
_CMD_POS = r"(?:^|[;&|]|\b(?:then|do|else|elif|export|local|declare|readonly)\s+)\s*"


def _assigns(var: str, text: str) -> list[str]:
    """Lines of `text` that actually ASSIGN `var` (not merely mention it)."""
    out = []
    for ln in text.splitlines():
        # ⚠ STOP AT THE COMMAND SEPARATOR. `\b(?:echo|printf)\b.*$` swallowed the rest of
        # the line, so a REAL assignment after an echo — `echo hi; GONE=yes` — vanished
        # with it, reintroducing the false negative this helper exists to remove.
        stripped = re.sub(r"\b(?:echo|printf)\b[^;&|]*", "", ln)
        if re.search(_CMD_POS + re.escape(var) + r"=", stripped):
            out.append(ln)
    return out


def test_the_verification_settles_before_it_calls_a_lease_open():
    from tests.test_verify_closed_cli import (
        test_cli_subprocess_active_first_then_closed_closes_via_retry,
    )

    test_cli_subprocess_active_first_then_closed_closes_via_retry()


def test_the_teardown_error_does_not_assert_a_count_it_never_made():
    code = _code(TD_CLOSE["run"])
    err = next(ln for ln in code.splitlines() if "::error title=Could not VERIFY" in ln)
    assert "after 3 destroy attempts" not in err
    assert "$REASON" in err and "$VERIFY_RC" in err


def test_the_read_loops_observation_survives_to_the_classifier(tmp_path):
    from tests.test_runner_teardown_shell_probes import test_real_close_step

    test_real_close_step(tmp_path, "verifier_true_nonzero")


def test_the_nested_teardown_pin_is_reachable_from_main():
    """Byte-identity is not enough — the pinned commit must still be REACHABLE.

    ⛔ THE FAILURE THIS EXISTS TO PREVENT, MEASURED. #308 was squash-merged. The
    identity guard above requires the pin to name a commit whose runner-teardown.yml
    matches this one byte for byte, which during the PR is the BRANCH commit
    (d5e64da8). A squash merge does not keep that commit in main's history:

        compare d5e64da8...main -> "diverged"      (orphaned)
        compare c2cad20a...main -> "ahead"         (the previous pin, an ancestor)

    The previous pin survived only because its PR was not squashed. Once orphaned,
    GitHub Actions cannot resolve the nested `uses:` while building the job graph, and
    every downstream caller dies as a STARTUP FAILURE — a run with ZERO jobs and no
    logs, which renders as a grey X indistinguishable from a generic CI blip.

    ⇒ blazing#927 hit exactly this: three commits, `jobs=0` on every one, no logs, no
    annotation, and nothing anywhere naming the unresolvable ref. Cost far more to
    diagnose than to prevent.

    ★ Identity and reachability are independent properties and the identity guard
    silently traded one for the other: it FORCES the pin onto a branch commit (that is
    the only place the bytes match mid-PR), which is precisely the commit a squash
    merge destroys. The two guards must therefore both hold, and the resolution is to
    pin the post-merge main SHA — byte-identical AND an ancestor.
    """
    import os
    import subprocess

    uses = str(DOC["jobs"]["teardown"]["uses"])
    pin = uses.rsplit("@", 1)[-1]
    assert re.fullmatch(r"[0-9a-f]{40}", pin), f"teardown pinned to {pin!r}, not a 40-hex SHA"

    root = pathlib.Path(__file__).resolve().parents[1]

    def _git(*args) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60
        )

    # A PR's own head is legitimately not yet on main; what must never happen is a pin
    # that is orphaned. Fetch on demand — CI checks out shallow.
    if _git("cat-file", "-e", f"{pin}^{{commit}}").returncode != 0:
        # ⚠ DEPTH 1 IS ENOUGH HERE, and depth 200 was cargo. This fetch only has to
        # MATERIALISE the pinned object; the ancestry walk below runs over MAIN's
        # history, which the unshallow deepens separately. Matches the identity guard
        # above, which has always used depth 1.
        _git("fetch", "--quiet", "--depth", "1", "origin", pin)

    if _git("cat-file", "-e", f"{pin}^{{commit}}").returncode != 0:
        # ⛔ MUST NOT SKIP IN CI — that is the surface this protects.
        assert not os.environ.get("CI"), (
            f"the pinned teardown commit {pin[:8]} cannot be read even after a fetch. "
            f"Under CI that is the orphaned-pin condition itself, not a local gap."
        )
        pytest.skip("pinned commit unavailable locally; this guard is enforced in CI")

    # ⛔ THE MAIN REF MUST BE MADE TO EXIST, NOT ASSUMED. The first version of this
    # guard ended in `pytest.skip("no main ref available")`, and `actions/checkout`
    # is shallow by default with no guarantee of `origin/main` — so under CI, the one
    # surface this protects, it would have SKIPPED rather than enforced. That is the
    # same "a check that cannot fail" defect this file keeps finding, committed inside
    # the guard written to prevent it. Copilot and CodeRabbit both caught it.
    #
    # ⚠ AND DEPTH MATTERS INDEPENDENTLY OF PRESENCE. A depth-1 `main` makes
    # `merge-base --is-ancestor` answer NO for a genuinely older ancestor, because the
    # parent history it needs is simply absent — a false ORPHAN report, which would
    # fail honest PRs and teach the next person to delete this test. So deepen before
    # concluding anything.
    def _main_ref() -> str | None:
        for ref in ("origin/main", "main"):
            if _git("rev-parse", "--verify", "--quiet", ref).returncode == 0:
                return ref
        _git("fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main")
        for ref in ("origin/main", "main"):
            if _git("rev-parse", "--verify", "--quiet", ref).returncode == 0:
                return ref
        return None

    ref = _main_ref()
    if ref is None:
        assert not os.environ.get("CI"), (
            "no main ref is available even after an explicit fetch. Under CI this is a "
            "broken checkout, not an excuse to skip: the reachability property would go "
            "unchecked on the exact surface this guard exists to protect."
        )
        pytest.skip("no main ref available locally; this guard is enforced in CI")

    # Deepen so merge-base has the history it needs. --unshallow is the reliable form;
    # fall back to a bounded deepen if the remote refuses it. ⚠ The `and` short-circuits,
    # so --unshallow is attempted ONLY on a shallow repo — same semantics as the nested
    # ifs this replaced (ruff SIM102), not a widening.
    if (
        _git("rev-parse", "--is-shallow-repository").stdout.strip() == "true"
        and _git("fetch", "--quiet", "--unshallow", "origin").returncode != 0
    ):
        _git("fetch", "--quiet", "--deepen=1000", "origin")

    head = _git("rev-parse", ref).stdout.strip()
    if pin == head or _git("merge-base", "--is-ancestor", pin, ref).returncode == 0:
        return  # reachable from main

    # Not on main. Legitimate ONLY while this is the PR that will put it there — the
    # pin is an ancestor of HEAD but not yet of main.
    if _git("merge-base", "--is-ancestor", pin, "HEAD").returncode == 0:
        return

    raise AssertionError(
        f"the pinned teardown commit {pin[:8]} is not reachable from {ref} and is not "
        f"on this branch either — it has been orphaned, most likely by a squash merge. "
        f"Every downstream caller of runner-pool.yml will die as a STARTUP FAILURE: "
        f"zero jobs, no logs, and nothing naming the unresolvable ref. Re-pin to the "
        f"post-merge main SHA, which is both byte-identical and an ancestor."
    )
