#!/usr/bin/env python3
"""Decide whether a provider may be trusted to host an ephemeral CI runner.

WHY THIS IS NOT `provider-smoke`

`provider-smoke` answers "can I deploy here right now" and `provider-canary` answers
"does a deployment survive over time". Neither answers the question that decides a CI
pool: **will the runner pod actually be scheduled and come up on this provider?**

That is a distinct failure. Three providers were recorded as healthy, well-provisioned,
willing to bid, winning the lease — and never scheduling the runner pod. It reproduced at
BOTH 16Gi/30Gi and 32Gi/30Gi, ruling memory out; ephemeral storage and the port-80 global
ingress are the live hypotheses. Such a lease is worse than no bid: it consumes the
attempt, holds escrow, and stalls to the timeout. One was traced to an 1800s stall.

It also cannot be inferred from price or capacity. just-akash takes the CHEAPEST bid in
whatever set it is given, so an unproven provider that undercuts a proven one CAPTURES the
runner and kills it — measured at ~24 uact against ~27. Which is why a pool must order
proven hosts as a strictly earlier tier, and why "proven" needs a measurement rather than
an opinion.

THE BAR, as ratified (4/4, 3 rounds):

    the real runner SDL, at the CALLER'S profile, port-80 global ingress
      -> pod scheduled and Running
      -> runner registers with GitHub within 120s        [needs a token]
      -> a no-op job runs on it                          [needs a token]
      -> teardown closes the lease cleanly
    passed 3 CONSECUTIVE times on fresh provisions

120s is a WASTE BOUND, not a quality discriminator: a proven host registers in ~30s, so
the budget only limits how long a bad one costs us. Three consecutive runs is the minimum
that distinguishes a pattern from luck — the one provider currently qualified is recorded
as bidding intermittently, so a single pass proves reachability, not reliability.

WHY THE OUTCOMES ARE NAMED

Every stage that can fail gets its own outcome, because collapsing them is what made this
expensive: "it printed (infra) for everything" meant a funding problem, a market outage
and a broken host were indistinguishable, so the standing fix became "use paid runners".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# Ratified: a proven host registers in ~30s; this only bounds a bad one's cost.
REGISTER_TIMEOUT_S = 120
# A pattern, not luck. The single qualified provider bids intermittently.
REQUIRED_CONSECUTIVE_PASSES = 3
# Below this many proven hosts, one silent provider takes the whole pool down.
MIN_PROVEN_HOSTS = 3


class Outcome(str, Enum):
    """Named per stage. Each implies a different remedy, which is the point."""

    NO_BID = "NO_BID"  # never bid — capacity or price, not a defect
    LEASE_NO_POD = "LEASE_NO_POD"  # bid, won, never scheduled — DISQUALIFYING
    POD_NO_REGISTER = "POD_NO_REGISTER"  # came up, never reached GitHub
    JOB_NOT_RUN = "JOB_NOT_RUN"  # registered, could not execute
    TEARDOWN_FAILED = "TEARDOWN_FAILED"  # worked, but leaked the lease
    # noqa S105: an outcome name whose value equals its own name, not a credential.
    PASS = "PASS"  # noqa: S105
    INDETERMINATE = "INDETERMINATE"  # the probe itself failed — never a verdict


# LEASE_NO_POD is singled out: it is the failure this tool exists to find, and the only
# one that is strictly worse than not bidding at all.
DISQUALIFYING = frozenset({Outcome.LEASE_NO_POD, Outcome.POD_NO_REGISTER, Outcome.JOB_NOT_RUN})


@dataclass
class Attempt:
    outcome: Outcome
    seconds: float | None = None
    dseq: str | None = None
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASS


@dataclass
class ProviderVerdict:
    address: str
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def consecutive_passes(self) -> int:
        """Trailing run of passes. Consecutive, not total: a provider that alternates
        pass/fail is exactly the intermittent host this bar exists to exclude."""
        n = 0
        for a in reversed(self.attempts):
            if not a.passed:
                break
            n += 1
        return n

    @property
    def disqualified(self) -> bool:
        return any(a.outcome in DISQUALIFYING for a in self.attempts)

    @property
    def indeterminate(self) -> bool:
        """Every attempt failed for a reason that says nothing about the provider."""
        return bool(self.attempts) and all(
            a.outcome in (Outcome.INDETERMINATE, Outcome.NO_BID) for a in self.attempts
        )

    def marker(self, required: int = REQUIRED_CONSECUTIVE_PASSES) -> str:
        """The marker to record in the fleet's provider list.

        Order matters. Disqualification WINS over a passing streak: a provider that
        stranded the runner even once has demonstrated it can, and the cost of trying it
        again is a stalled lease. Promotion has to be harder than demotion here, because
        the failure is expensive and silent while the success is cheap and obvious.
        """
        if self.disqualified:
            return "runner_deny"
        if self.indeterminate:
            return "unknown"
        if self.consecutive_passes >= required:
            return "runner_host"
        return "unproven"


def classify(
    *,
    bid: bool,
    pod_running: bool,
    registered: bool | None,
    job_ran: bool | None,
    torn_down: bool,
    probe_error: str = "",
) -> Outcome:
    """One attempt's outcome, in stage order.

    `registered` / `job_ran` are None when no GitHub token was supplied — the probe then
    answers only the scheduling question, which is still the discriminator for the
    recorded failures. A None must NOT read as False: reporting "never registered" when
    we never asked would demote a provider on evidence we did not gather.
    """
    if probe_error:
        return Outcome.INDETERMINATE
    if not bid:
        return Outcome.NO_BID
    if not pod_running:
        return Outcome.LEASE_NO_POD
    if registered is False:
        return Outcome.POD_NO_REGISTER
    if job_ran is False:
        return Outcome.JOB_NOT_RUN
    if not torn_down:
        return Outcome.TEARDOWN_FAILED
    return Outcome.PASS


def render_verdicts(
    verdicts: list[ProviderVerdict], required: int = REQUIRED_CONSECUTIVE_PASSES
) -> list[str]:
    lines = []
    hosts = [v for v in verdicts if v.marker(required) == "runner_host"]
    for v in verdicts:
        m = v.marker(required)
        streak = f"{v.consecutive_passes}/{required}"
        worst = next((a.outcome.value for a in v.attempts if a.outcome in DISQUALIFYING), "")
        lines.append(f"  {m:<11} {streak:>5}  {v.address}" + (f"  ({worst})" if worst else ""))

    lines.append("")
    lines.append(f"proven runner hosts: {len(hosts)} (want >= {MIN_PROVEN_HOSTS})")
    if len(hosts) < MIN_PROVEN_HOSTS:
        lines.append(
            f"::warning title=Runner pool would still be {len(hosts)} deep::"
            f"{len(hosts)} proven host(s) of {MIN_PROVEN_HOSTS} required. Until that is met, "
            "one silent provider takes the whole pool down and CI falls back to billed "
            "runners — which is the cost this qualification exists to remove."
        )
    return lines
