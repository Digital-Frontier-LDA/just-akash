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

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

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


# ==========================================================================
# Driver
#
# The logic above was written without one, which made it unrunnable — a
# qualification bar nothing could measure against. It orchestrates the `just-akash`
# CLI as a subprocess rather than reaching into internals: deploy/status/destroy are
# the surfaces the CI path itself uses, so a probe that passes here has exercised the
# same code that provisions the pool.
# ==========================================================================

SDL_TEMPLATE = Path(__file__).resolve().parent.parent / "sdl" / "github-runner-probe.yaml"


def _run(cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    try:
        p = subprocess.run(  # noqa: S603 - argv list, never a shell string
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except FileNotFoundError as exc:
        return 127, str(exc)


def render_sdl(
    dest: Path, *, cpu: str, memory: str, storage: str, org: str, token: str, label: str
) -> Path:
    """Substitute the probe SDL at the CALLER'S profile.

    The profile is not incidental: storage is the tightest constraint on which
    providers can host the runner, so a provider qualified at 30Gi is not thereby
    qualified at 100Gi.
    """
    body = SDL_TEMPLATE.read_text()
    for key, val in {
        "CPU": cpu,
        "MEMORY": memory,
        "STORAGE": storage,
        "ORG_NAME": org,
        # A placeholder still forces the container to be SCHEDULED and started, which is
        # the discriminator. It simply fails to register afterwards.
        "ACCESS_TOKEN": token or "probe-no-token",
        "RUNNER_NAME_PREFIX": label,
        "LABELS": f"self-hosted,linux,akash,{label}",
    }.items():
        body = body.replace("{{" + key + "}}", val)
    dest.write_text(body)
    return dest


def _deploy(sdl: Path, provider: str, bid_wait: int) -> tuple[str | None, str]:
    rc, out = _run(
        [
            "just-akash",
            "deploy",
            "--sdl",
            str(sdl),
            "--provider",
            provider,
            "--bid-wait",
            str(bid_wait),
        ]
    )
    m = re.search(r"^\s*DSEQ:\s*(\d+)", out, re.M)
    return (m.group(1) if m else None), out


def _state(dseq: str) -> str:
    rc, out = _run(["just-akash", "status", "--dseq", dseq, "--json"], timeout=120)
    try:
        return (json.loads(out.strip().splitlines()[-1]) or {}).get("state", "")
    except Exception:
        return ""


def _destroy(dseq: str) -> bool:
    for _ in range(3):
        rc, out = _run(["just-akash", "destroy", "--dseq", dseq, "-y"], timeout=300)
        if rc == 0 or re.search(r"Deployment closed|already closed|not found", out, re.I):
            return True
        time.sleep(5)
    return False


def _registered(org: str, label: str, token: str, timeout_s: int) -> bool:
    """Poll GitHub for a runner carrying this probe's label."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rc, out = _run(
            [
                "gh",
                "api",
                "--paginate",
                f"orgs/{org}/actions/runners?per_page=100",
                "--jq",
                f'[.runners[] | select(any(.labels[].name; .=="{label}"))] | length',
            ],
            timeout=60,
        )
        if rc == 0 and out.strip().isdigit() and int(out.strip()) > 0:
            return True
        time.sleep(10)
    return False


def probe_once(
    provider: str,
    *,
    sdl: Path,
    org: str,
    label: str,
    token: str,
    bid_wait: int,
    register_timeout: int,
) -> Attempt:
    """One attempt against one provider, classified by the stage that failed."""
    started = time.time()
    dseq, out = _deploy(sdl, provider, bid_wait)
    if not dseq:
        if re.search(r"PaymentRequiredError|Insufficient balance|HTTP 402", out, re.I):
            # Never the provider's fault: no order existed, so nobody was asked to bid.
            return Attempt(outcome=Outcome.INDETERMINATE, detail="wallet underfunded (402)")
        return Attempt(outcome=Outcome.NO_BID, seconds=time.time() - started)

    try:
        pod_running = False
        deadline = time.time() + register_timeout
        while time.time() < deadline:
            if _state(dseq) == "active":
                pod_running = True
                break
            time.sleep(5)

        registered = job_ran = None
        if token and pod_running:
            registered = _registered(org, label, token, register_timeout)
            # A runner that registered can take a job; the pool's own wait proves the
            # rest, and asking for more here would need a real workflow dispatch.
            job_ran = registered
    finally:
        torn_down = _destroy(dseq)

    return Attempt(
        outcome=classify(
            bid=True,
            pod_running=pod_running,
            registered=registered,
            job_ran=job_ran,
            torn_down=torn_down,
        ),
        seconds=time.time() - started,
        dseq=dseq,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Qualify providers as runner hosts.")
    ap.add_argument("--providers", required=True, help="comma-separated akash1… addresses")
    ap.add_argument("--cpu", default="4")
    ap.add_argument("--memory", default="16Gi")
    ap.add_argument("--storage", default="30Gi")
    ap.add_argument("--org", default=os.environ.get("GITHUB_ORG", ""))
    ap.add_argument("--attempts", type=int, default=REQUIRED_CONSECUTIVE_PASSES)
    ap.add_argument("--required", type=int, default=REQUIRED_CONSECUTIVE_PASSES)
    ap.add_argument("--bid-wait", type=int, default=60)
    ap.add_argument("--register-timeout", type=int, default=REGISTER_TIMEOUT_S)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if not shutil.which("just-akash"):
        print("just-akash is not on PATH", file=sys.stderr)
        return 127

    token = os.environ.get("GH_RUNNER_PAT", "")
    if not token:
        # Say so up front: without it the probe answers only the scheduling question,
        # and a silent downgrade would let a weaker pass read as a full qualification.
        print(
            "::warning::no GH_RUNNER_PAT — probing SCHEDULING only, not registration. "
            "A pass here is not a full runner_host qualification.",
            file=sys.stderr,
        )

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    verdicts: list[ProviderVerdict] = []
    tmpdir = tempfile.mkdtemp(prefix="akash-probe-")

    for provider in providers:
        v = ProviderVerdict(address=provider)
        for i in range(args.attempts):
            label = f"probe-{provider[-6:]}-{i}"
            sdl = Path(tmpdir) / f"probe-{provider[-6:]}-{i}.yaml"
            render_sdl(
                sdl,
                cpu=args.cpu,
                memory=args.memory,
                storage=args.storage,
                org=args.org,
                token=token,
                label=label,
            )
            a = probe_once(
                provider,
                sdl=sdl,
                org=args.org,
                label=label,
                token=token,
                bid_wait=args.bid_wait,
                register_timeout=args.register_timeout,
            )
            v.attempts.append(a)
            print(
                f"  {provider} attempt {i + 1}/{args.attempts}: {a.outcome.value}", file=sys.stderr
            )
            # Stop early on a disqualifying outcome: it already outranks any streak,
            # and each further attempt costs a lease and real escrow.
            if a.outcome in DISQUALIFYING:
                break
        verdicts.append(v)

    if args.json:
        print(
            json.dumps(
                {
                    "providers": [
                        {
                            "address": v.address,
                            "marker": v.marker(args.required),
                            "consecutive_passes": v.consecutive_passes,
                            "attempts": [
                                {
                                    "outcome": a.outcome.value,
                                    "dseq": a.dseq,
                                    "seconds": a.seconds,
                                    "detail": a.detail,
                                }
                                for a in v.attempts
                            ],
                        }
                        for v in verdicts
                    ],
                    "proven_hosts": sum(
                        1 for v in verdicts if v.marker(args.required) == "runner_host"
                    ),
                    "min_proven_hosts": MIN_PROVEN_HOSTS,
                },
                indent=2,
            )
        )
    else:
        print("\n".join(render_verdicts(verdicts, args.required)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
