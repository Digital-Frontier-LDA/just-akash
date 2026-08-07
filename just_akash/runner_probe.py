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
import uuid
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
    # Scheduled fine, but registration and/or job execution were never MEASURED. Not a
    # failure and not a qualification — deliberately non-promotable, so a token-less or
    # un-dispatchable run can never reach runner_host on a partial bar.
    SCHEDULED_ONLY = "SCHEDULED_ONLY"
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
    # Did THIS attempt actually observe a running container? Tracked separately from the
    # outcome so a run can tell "we measured no pod" from "we never demonstrated we can
    # measure a pod at all". See require_positive_control().
    observed_pod: bool = False

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
    # None is "never asked", and it must cut BOTH ways. Not demoting on it was already
    # right; PROMOTING on it was the bug — a scheduling-only probe could reach PASS and,
    # three times over, runner_host, on a bar whose registration and job steps nobody
    # measured. Non-promotable is the honest reading of a partial measurement.
    if registered is None or job_ran is None:
        return Outcome.SCHEDULED_ONLY
    return Outcome.PASS


def require_positive_control(verdicts: list[ProviderVerdict]) -> tuple[list[ProviderVerdict], str]:
    """Downgrade every disqualification unless the run PROVED it can detect a live pod.

    Why this exists, concretely: a probe run reported LEASE_NO_POD for the fleet's one
    production-proven runner_host, and across two runs EVERY provider that won a lease
    reported LEASE_NO_POD. `_pod_started` had never once returned True in the field. A
    detector that has only ever produced one answer has not been validated — it has only
    been observed agreeing with itself.

    The underlying ambiguity is real and cannot be reasoned away: for a lease whose
    provider hostUri has not propagated yet, the logs channel returns

        Error: no active lease / provider hostUri for this deployment yet.

    which is indistinguishable, to this code, from a provider that will never schedule.
    Only elapsed time separates them, and calibrating that needs a known-good reading.

    So a NEGATIVE is only trusted once the same run has produced a POSITIVE. Without one,
    disqualifications become INDETERMINATE — "we could not measure", never "it failed".
    That direction is deliberate: runner_deny is permanent and outranks any later passing
    streak, so a false deny silently shrinks a pool that is already one host deep.
    """
    if any(a.observed_pod for v in verdicts for a in v.attempts):
        return verdicts, ""

    downgraded = 0
    for v in verdicts:
        for a in v.attempts:
            if a.outcome in DISQUALIFYING:
                a.outcome = Outcome.INDETERMINATE
                a.detail = (a.detail + "; " if a.detail else "") + "no positive control this run"
                downgraded += 1
    if not downgraded:
        return verdicts, ""
    return verdicts, (
        f"::warning title=No positive control — {downgraded} disqualification(s) withheld::"
        "This run never observed a running container on ANY provider, so a 'no pod' reading "
        "carries no information: an un-propagated lease looks identical to one that will "
        "never schedule. Those outcomes were recorded as INDETERMINATE rather than "
        "runner_deny. Re-run including a provider known to serve, and only trust the "
        "negatives once that provider reads as a pass."
    )


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


def _run(cmd: list[str], timeout: int = 900, env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(  # noqa: S603 - argv list, never a shell string
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, **env} if env else None,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except FileNotFoundError as exc:
        return 127, str(exc)


def mint_registration_token(org: str) -> str:
    """A short-lived org runner-registration token.

    This is what takes the long-lived PAT off the critical path. myoung34/github-runner
    (the base image) accepts RUNNER_TOKEN directly, so an operator holding `admin:org` can
    qualify providers without anyone provisioning a PAT first.

    That matters beyond convenience: a PAT expiry is SILENT. It surfaces as "runner did not
    come online" after a ~15-minute wait, indistinguishable from a provider fault — and an
    expired PAT handed to this probe would report POD_NO_REGISTER and wrongly demote healthy
    providers. Minting fresh each run cannot go stale, so it is the safer input as well as
    the more available one.

    Tokens expire in ~1h; an attempt takes minutes.
    """
    rc, out = _run(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"/orgs/{org}/actions/runners/registration-token",
            "--jq",
            ".token",
        ],
        timeout=60,
    )
    return out.strip() if rc == 0 and out.strip() else ""


def render_sdl(
    dest: Path,
    *,
    cpu: str,
    memory: str,
    storage: str,
    org: str,
    token: str,
    label: str,
    # noqa S107: an env var NAME chosen per auth mode, not a credential.
    token_kind: str = "ACCESS_TOKEN",  # noqa: S107
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
        "TOKEN_ENV": f"{token_kind}={token}" if token else "ACCESS_TOKEN=probe-no-token",
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


def _pod_started(dseq: str) -> bool | None:
    """Is a CONTAINER actually serving? True / False / None where None means UNKNOWN.

    Delegates to smoke_providers._service_availability, which reads
    `leases[].status.services[*].available|ready_replicas` — the field that reflects
    whether the container is SERVING. This module is not the first place in the repo to
    hit this: that function's own docstring records that the lease-level `status: ready`
    "flips the moment a manifest is accepted, long before the pod is up".

    Two signals were tried here before this one and both were wrong in the same way:

      deployment_state == "active"   true for a lease that schedules nothing — measured
                                     across seven leases on a runner_deny provider
      logs --service probe           returns "no active lease / provider hostUri ... yet"
                                     for an un-propagated lease, indistinguishable from
                                     one that will never schedule

    Both collapsed "cannot tell yet" into "no pod", which is what produced a LEASE_NO_POD
    verdict against the fleet's one production-proven host. The tri-state is the whole
    point: None keeps the caller waiting instead of manufacturing a disqualification.
    """
    try:
        from .smoke_providers import _service_availability
    except ImportError:
        return None
    try:
        result = _service_availability(str(dseq))
    except Exception:  # noqa: BLE001 - a read error means "unknown", never "no pod"
        return None
    if result is None:
        return None  # no service reported yet — keep waiting, do not classify
    available, _count = result
    return available >= 1


def _destroy(dseq: str) -> bool:
    for _ in range(3):
        rc, out = _run(["just-akash", "destroy", "--dseq", dseq, "-y"], timeout=300)
        if rc == 0 or re.search(r"Deployment closed|already closed|not found", out, re.I):
            return True
        time.sleep(5)
    return False


def _run_noop_job(org: str, label: str, repo: str, timeout_s: int, token: str = "") -> bool | None:
    """Dispatch the no-op workflow at this runner's label and wait for a conclusion.

    Returns True/False when a verdict was reached, and None when the job could not be
    dispatched at all — most often because runner-probe-job.yml is not yet on the default
    branch, which is where workflow_dispatch resolves. None is NOT a failure: it means the
    step was never measured, and the attempt is then SCHEDULED_ONLY rather than PASS.

    This replaces `job_ran = registered`, which silently equated two different claims. A
    runner can register and still never pick up work — wrong label set, busy/offline flip,
    broken work directory — so inferring it left the strongest part of the ratified bar
    unmeasured while reporting it as met.
    """
    rc, _ = _run(
        [
            "gh",
            "workflow",
            "run",
            "runner-probe-job.yml",
            "--repo",
            repo,
            "-f",
            f"runner-label={label}",
        ],
        timeout=60,
    )
    if rc != 0:
        return None  # not dispatchable — unmeasured, never "failed"

    deadline = time.time() + timeout_s
    # Did the run ever leave `queued`? A run that never did was never assigned to any
    # runner, which says nothing about the provider — see the return below.
    ever_started = False
    while True:
        rc, out = _run(
            [
                "gh",
                "run",
                "list",
                "--repo",
                repo,
                "--workflow",
                "runner-probe-job.yml",
                "--limit",
                "5",
                "--json",
                "status,conclusion,createdAt",
            ],
            timeout=60,
        )
        if rc == 0 and out.strip():
            try:
                runs = json.loads(out)
            except Exception:  # noqa: BLE001 - a bad read just means "keep waiting"
                runs = []
            for r in runs:
                if r.get("status") in ("in_progress", "completed"):
                    ever_started = True
                if r.get("status") == "completed":
                    return r.get("conclusion") == "success"
        # Always take at least one reading before honouring the deadline: a zero or short
        # timeout must mean "look once", not "never look".
        if time.time() >= deadline:
            break
        time.sleep(10)

    # A run that NEVER left `queued` was never assigned to any runner, which is not a
    # statement about the provider. The common cause is org policy: GitHub blocks
    # org-level self-hosted runners from PUBLIC repositories unless the runner group sets
    # allows_public_repositories (measured: just-akash is public and both groups have it
    # false, so the job cannot be assigned no matter which provider hosts the runner).
    # Reporting JOB_NOT_RUN here would blame a provider for OUR configuration.
    if not ever_started:
        return None
    return False


def _registered(org: str, label: str, api_token: str, timeout_s: int) -> bool:
    """Poll GitHub for a runner carrying this probe's label.

    Takes an API credential, which is NOT the same thing as the token in the SDL.

    A runner REGISTRATION token authenticates a runner joining the org; it is rejected by
    the REST API ("Bad credentials", verified). Passing it here as GH_TOKEN breaks every
    poll, so the runner registers fine and we never see it — the attempt reports
    POD_NO_REGISTER and demotes a healthy provider. Measured exactly that against the
    fleet's production-proven host on the first attempt of a run.

    So: a PAT is forwarded, a registration token is NOT, and empty means fall back to the
    ambient gh credential (which is what actually works in CI and locally).
    """
    env = {"GH_TOKEN": api_token} if api_token else None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rc, out = _run(
            [
                "gh",
                "api",
                "--paginate",
                f"orgs/{org}/actions/runners?per_page=100",
                "--jq",
                # status=="online" is LOAD-BEARING. Without it this counts OFFLINE
                # leftovers from earlier runs — and labels repeat across runs, so a dead
                # registration made this return True instantly while no live runner
                # existed. The job then dispatched at a label owned only by corpses and
                # queued until timeout: JOB_NOT_RUN blamed on the provider. Measured with
                # 13 offline probe runners listed and zero online.
                f'[.runners[] | select(.status=="online") '
                f'| select(any(.labels[].name; .=="{label}"))] | length',
            ],
            timeout=60,
            env=env,
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
    job_repo: str = "Digital-Frontier-LDA/just-akash",
    api_token: str = "",
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
        # Two stages, deliberately separate. A lease that never becomes active is a
        # market/provider condition; a lease that IS active while no container ever
        # starts is the disqualifying failure this tool exists to name.
        # None is "not yet measurable" and must keep us waiting; only an explicit False
        # after the deadline is evidence of anything.
        pod_running = False
        # Track whether we ever got an EXPLICIT answer. If _pod_started stayed None for
        # the whole window we never measured anything, and reporting LEASE_NO_POD would
        # permanently runner_deny a provider on an unmeasurable run — contradicting the
        # tri-state this function exists to provide.
        measured = False
        deadline = time.time() + register_timeout
        while True:
            # NOT `started` — that name holds this attempt's start TIME, and shadowing it
            # here silently broke the elapsed-seconds calculation. Caught by pyright,
            # which is the only gate that could have: the tests mock _pod_started, so the
            # shadowed value was never the timestamp during a test run.
            serving = _pod_started(dseq)
            if serving is not None:
                measured = True
            if serving is True:
                pod_running = True
                break
            # ALWAYS take at least one reading before honouring the deadline. A zero or
            # very short timeout must still mean "look once", not "never look" — the
            # latter left every fast attempt unmeasured and therefore INDETERMINATE.
            if time.time() >= deadline:
                break
            time.sleep(10)

        registered = job_ran = None
        if token and pod_running:
            registered = _registered(org, label, api_token, register_timeout)
            if registered:
                # MEASURED, not inferred. `job_ran = registered` equated two different
                # claims and left the bar's strongest step unchecked.
                job_ran = _run_noop_job(org, label, job_repo, register_timeout, api_token)
    finally:
        torn_down = _destroy(dseq)

    if not pod_running and not measured:
        # Never a verdict about the provider: our read never resolved.
        return Attempt(
            outcome=Outcome.INDETERMINATE,
            seconds=time.time() - started,
            dseq=dseq,
            detail="pod state never became measurable",
        )

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
        observed_pod=pod_running,
    )


def _run_id() -> str:
    """A short per-run tag so runner labels cannot collide across runs.

    Labels were `probe-<provider>-<attempt>`, identical on every run of the same
    provider. Offline registrations from earlier runs then matched the current label —
    see _registered — so the probe believed a runner was up when only dead ones shared
    the name. Derived from the temp dir, which is already unique per run.
    """
    return uuid.uuid4().hex[:6]


def _run_probes(args, token: str, token_kind: str, tmpdir: str) -> list[ProviderVerdict]:
    """Probe every provider, rendering SDLs into a caller-owned temp dir.

    The dir is caller-owned so main() can delete it: every rendered SDL contains a LIVE
    credential (a PAT, or a registration token valid ~1h), and mkdtemp without cleanup
    left those readable on disk after exit — on a shared or self-hosted runner a later
    job could read them.
    """
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    verdicts: list[ProviderVerdict] = []
    run_id = _run_id()
    for provider in providers:
        v = ProviderVerdict(address=provider)
        for i in range(args.attempts):
            # Re-mint PER ATTEMPT. A registration token lives ~1h, and a full run is
            # providers x attempts x up to several minutes each — 3x3 can exceed the
            # lifetime outright. A stale token does not fail loudly: the runner simply
            # never registers, the attempt reports POD_NO_REGISTER, and providers are
            # demoted for OUR expired credential. That is the same silent-expiry trap
            # that made minting preferable to a PAT in the first place, reintroduced by
            # minting only once.
            # noqa S105: comparing an env var NAME, not a credential.
            if token_kind == "RUNNER_TOKEN" and args.org:  # noqa: S105
                fresh = mint_registration_token(args.org)
                if fresh:
                    token = fresh
            label = f"probe-{run_id}-{provider[-6:]}-{i}"
            sdl = Path(tmpdir) / f"probe-{provider[-6:]}-{i}.yaml"
            render_sdl(
                sdl,
                cpu=args.cpu,
                memory=args.memory,
                storage=args.storage,
                org=args.org,
                token=token,
                label=label,
                token_kind=token_kind,
            )
            a = probe_once(
                provider,
                sdl=sdl,
                org=args.org,
                label=label,
                token=token,
                bid_wait=args.bid_wait,
                register_timeout=args.register_timeout,
                # Only a PAT is an API credential. A registration token is rejected by
                # the REST API, so forwarding it would break the very poll that decides
                # whether the runner registered.
                api_token=token if token_kind == "ACCESS_TOKEN" else "",  # noqa: S105
            )
            v.attempts.append(a)
            print(
                f"  {provider} attempt {i + 1}/{args.attempts}: {a.outcome.value}",
                file=sys.stderr,
            )
            if a.outcome in DISQUALIFYING:
                break
        verdicts.append(v)
    return verdicts


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
    token_kind = "ACCESS_TOKEN"  # noqa: S105 - an env var NAME, not a credential
    if not token:
        # No PAT: mint a short-lived registration token instead. Full qualification then
        # needs only `admin:org` on the operator's existing credential.
        token = mint_registration_token(args.org) if args.org else ""
        if token:
            token_kind = "RUNNER_TOKEN"  # noqa: S105 - an env var NAME, not a credential
            print(
                f"::notice::minted an org registration token for {args.org} — "
                "full qualification (registration + job) is available without a PAT",
                file=sys.stderr,
            )
    if not token:
        # Say so up front: without it the probe answers only the scheduling question,
        # and a silent downgrade would let a weaker pass read as a full qualification.
        print(
            "::warning::no GH_RUNNER_PAT — probing SCHEDULING only, not registration. "
            "A pass here is not a full runner_host qualification.",
            file=sys.stderr,
        )

    # TemporaryDirectory, not mkdtemp: every rendered SDL embeds a LIVE credential, and
    # the previous mkdtemp was never cleaned up — leaving a PAT (valid until expiry) or a
    # registration token (~1h) readable on disk after exit. On a shared or self-hosted
    # runner a later job could read them.
    with tempfile.TemporaryDirectory(prefix="akash-probe-") as tmpdir:
        os.chmod(tmpdir, 0o700)
        verdicts = _run_probes(args, token, token_kind, tmpdir)

    verdicts, control_warning = require_positive_control(verdicts)
    if control_warning:
        print(control_warning, file=sys.stderr)

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
