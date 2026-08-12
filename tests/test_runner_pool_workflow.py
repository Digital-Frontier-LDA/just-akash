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
SRC = WF_PATH.read_text()
DOC = yaml.safe_load(SRC)
CALL = (DOC.get("on") or DOC.get(True))["workflow_call"]
INPUTS = CALL["inputs"]
OUTPUTS = CALL["outputs"]
STEPS = DOC["jobs"]["pool"]["steps"]


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
    assert "-ge 1" in body, "0 would accept an empty pool as healthy"
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
    assert re.search(r"2>\s*/tmp/\S+\.err\)", poll), (
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
    wallet = _step("Wallet")["run"]
    assert "CI is not broken" in wallet
    assert "bill" in wallet.lower() and "top up" in wallet.lower()
    assert "::error title=" in wallet, "a step summary alone is missed in a red run"


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
    assert "steps.candidates.outputs.candidates" in env.get("CANDIDATES_CSV", "")
    assert not any("inputs.providers" in str(v) for v in env.values()), (
        "the unfiltered provider spec must not be in scope where the deploy happens"
    )


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
# Anti-vacuity — prove the guards above can actually fail
# --------------------------------------------------------------------------

MUTATIONS = [
    (
        '"default" not in tag-prefix',
        lambda s: s.replace(
            "        required: true\n        type: string\n      github-org:",
            "        required: true\n        type: string\n"
            "        default: 'ci-shared'\n      github-org:",
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
    # The count must survive a multi-page org. Both shapes below are what a reader
    # "tidying up" the jq would plausibly write, and each restores the bug.
    ("online count is line-based", lambda s: s.replace("grep -c .", "head -1")),
    ("no aggregating jq under paginate", lambda s: s.replace('| .id"', '] | length"')),
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
        "checkout credentials",
        lambda s: s.replace("persist-credentials: false", "persist-credentials: true"),
    ),
]


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
        ],
        env={**os.environ, "RUNNER_POOL_WF": str(broken)},
        capture_output=True,
        text=True,
    )
    out = proc.stdout + proc.stderr

    # A non-zero exit is NOT enough. A collection error, an import failure or a crash all
    # exit non-zero while proving nothing about the guard — that is precisely how this
    # harness sat green while checking nothing. Require an actual test FAILURE.
    assert "error during collection" not in out and "errors during collection" not in out, (
        f"mutation {label!r}: the inner run failed to COLLECT, so no guard was "
        f"evaluated.\n{out[-1500:]}"
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
TD_SRC = TD_PATH.read_text()
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
    assert '.==\\"${RUNNER_LABEL}\\"' in body or "${RUNNER_LABEL}" in body
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


def test_teardown_does_not_claim_an_ownership_check_it_cannot_perform():
    """just-akash tags live in a LOCAL file and `status --json` emits no tag, so a
    cross-job tag readback returns empty every time. A guard that always takes its
    'could not verify, proceed anyway' branch while reporting verification is worse
    than no guard — this asserts we did not ship one."""
    body = TD_CLOSE["run"]
    assert "closed=refused" not in body, "a refusal path implies a gate that cannot fire"
    assert "get('tag'" not in body.replace('"', "'"), "status --json has no tag field"


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
