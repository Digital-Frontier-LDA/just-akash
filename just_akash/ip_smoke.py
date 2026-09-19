"""Leased-IP delivery check: does an IP we hand out actually work?

just-akash#244. The gap this closes is narrow and was invisible for eight
months: oni_dc's `akash-ip-operator` ran from 2026-01-21 with 19 orphaned
`ProviderLeasedIP` CRs wedging its startup reconcile, and in that time it
delivered zero leased IPs while

  - `/status` advertised 16 allocatable,
  - bid-probe recorded onidc bidding on ip-lease orders 132/150, and
  - every daily provider-smoke run stayed green.

Not one signal we own noticed, because not one of them ever *leased*. Bidding
is not delivering, and this module is the difference.

WHAT ACTUALLY HAS TO BE ASSERTED, and why status is not enough
--------------------------------------------------------------
Two distinct failure modes, and only one of them is visible from the provider:

  1. No IP is ever assigned (wedged operator). `ips` comes back empty, so a
     presence check DOES catch this one. Worth stating plainly because the
     issue thread argued otherwise for a while: a lease-status check would not
     have slept through the whole outage — it would have failed at step 3.
  2. An IP IS assigned, reports healthy in every field, and does not route to
     us. Nothing on the provider side can see this. Only a packet arriving
     from outside distinguishes "we allocated an address" from "we allocated
     an address that works".

So `assert_reachable` is the load-bearing assertion, and it exists for mode 2.

WHERE THE ADDRESS COMES FROM
----------------------------
`lease-status` returns, alongside `services` and `forwarded_ports`, an `ips`
map keyed by service name. Confirmed against Akash-Console, which renders
exactly this for tenants (`LeaseRow.tsx` links ``http://{IP}:{ExternalPort}``):

    {"ips": {"probe": [{"IP": "213.58.173.241", "ExternalPort": 80,
                        "Port": 80, "Protocol": "TCP"}]}}

``ExternalPort`` is READ, never assumed to be 80. The SDL asks for ``as: 80``
and today that is what comes back, but the console reads it rather than
trusting the manifest, and a probe that hardcodes 80 turns a port remap into a
false "the IP does not work".
"""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# One definition, two consumers (#244): the bid probe asks whether the provider
# BIDS on this shape; this module asks whether it can DELIVER it. They must be
# the same order or the two answers are about different questions.
from .bid_probe import _SDL_IP_LEASE

IP_SDL = _SDL_IP_LEASE

# The service name inside _SDL_IP_LEASE. `ips` is keyed by service, so this is
# the lookup key, not decoration.
IP_SERVICE_NAME = "probe"


# ── outcomes ─────────────────────────────────────────────────────────────

# noqa: S105 — the rule fires on the NAME containing "PASS", not on anything
# secret. "PASS"/"FAIL" are the outcome vocabulary the rest of the smoke already
# prints and writes to telemetry; renaming them to satisfy a substring match
# would make this stage's output inconsistent with every other stage.
OUTCOME_PASS = "PASS"  # noqa: S105
OUTCOME_FAIL = "FAIL"
# A lease that closed under us mid-probe. NOT a failure, and deliberately not
# folded into FAIL: the two known IP-bearing deployments on h4i churn within
# hours (dseq 1788531162 went active->closed the same day #244 was written), so
# this WILL fire in normal operation. Reporting churn as FAIL would make a
# healthy provider look broken; reporting it as PASS would hide a real outage.
# Distinguishing them is the entire point of the issue this module closes —
# collapsing two fail-closed paths into one indistinguishable signal is the bug
# class, so reproducing it here would be self-defeating.
OUTCOME_CHURNED = "CHURNED"
# The provider does not advertise ip-lease; nothing to assert.
OUTCOME_SKIP = "SKIP"


# ── per-cluster address pools ────────────────────────────────────────────
#
# Config, not hardcoded per #244 criterion 5. An assigned address outside the
# configured pool means MetalLB is handing out something we did not declare.
#
# NOTE ON JUSTIFICATION: this check was originally proposed as a defence
# against oni_dc's pool over-claiming a neighbouring company's /29. That was
# REFUTED on 2026-09-03 — the full 213.58.173.240/28 is ours, proven by pinning
# test LoadBalancers to .241 and .249 and reaching both from three networks
# (akash-providers-IaC#190). The check survives on the narrower, still-real
# ground that a pool-conformance violation should be loud. Do not reintroduce
# the neighbour-address rationale; it is not true.
POOL_BY_CLUSTER: dict[str, str] = {
    "onidc": "213.58.173.240/28",
}


def pool_for_cluster(cluster: str, overrides: dict[str, str] | None = None) -> str | None:
    """The declared address pool for a cluster, or None if we have not declared
    one. None means "cannot check", which is reported as such — never as a pass.
    """
    if overrides and cluster in overrides:
        return overrides[cluster]
    return POOL_BY_CLUSTER.get(cluster)


def ip_in_pool(ip: str, cidr: str | None) -> bool | None:
    """Is `ip` inside `cidr`? None when the answer is unknowable.

    Three-valued on purpose. A malformed address, a malformed CIDR, or no
    declared pool are all "we could not check", which must stay distinct from
    "we checked and it is outside" — the same not-measured vs measured-false
    distinction the rest of this module turns on.
    """
    if not cidr or not ip:
        return None
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None


def is_curlable(ip: str) -> bool:
    """May we send a request to this address?

    The address comes from a PROVIDER-CONTROLLED payload (`lease-status`), and
    this probe runs on a CI runner that can reach our own infrastructure. So a
    provider returning `127.0.0.1` or `10.0.0.5` would have the runner make a
    request into its own network on the provider's say-so. Mirrors the same
    predicate `provider_capacity._is_public_host` applies to provider-advertised
    URLs — the trust boundary is identical and there should not be two answers
    to the same question.

    Note the direction of the failure: refusing to curl leaves `reachable` as
    None, which classify() reports as "reachability was never established", i.e.
    a FAIL. Being unable to safely test is never a pass.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


# ── reading the assigned address off the lease ───────────────────────────


@dataclass(frozen=True)
class LeasedIP:
    """One assigned address, as the provider reports it."""

    ip: str
    external_port: int
    port: int | None = None
    protocol: str | None = None
    service: str = IP_SERVICE_NAME

    @property
    def url(self) -> str:
        """What to curl. ExternalPort, never an assumed 80."""
        return f"http://{self.ip}:{self.external_port}"


def extract_leased_ips(lease_status: Any, service: str = IP_SERVICE_NAME) -> list[LeasedIP]:
    """Pull assigned addresses out of a lease-status payload.

    Tolerant by design: this parses a provider's response, and a provider that
    answers with a shape we did not expect must produce "no IPs found" (which
    the caller reports as a failure with the raw payload attached) rather than
    a traceback that loses the evidence.

    Returns [] for every absence — missing `ips`, empty map, wrong service,
    unparseable entries. Absence is the signal for failure mode 1.
    """
    if not isinstance(lease_status, dict):
        return []
    ips = lease_status.get("ips")
    if not isinstance(ips, dict):
        return []

    # Prefer the SDL's service, but fall back to scanning every service: a
    # renamed service in the SDL should not silently report "no IP delivered"
    # when an IP was in fact delivered under another key.
    entries: list[Any] = []
    if isinstance(ips.get(service), list):
        entries = list(ips[service])
        found_service = service
    else:
        found_service = ""
        for name, val in ips.items():
            if isinstance(val, list) and val:
                entries = list(val)
                found_service = str(name)
                break

    out: list[LeasedIP] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        # The provider uses capitalised keys (IP, ExternalPort); accept the
        # lowercase spellings too rather than depending on a casing convention
        # we do not control.
        raw_ip = e.get("IP") or e.get("ip")
        raw_port = e.get("ExternalPort")
        if raw_port is None:
            raw_port = e.get("external_port")
        if not isinstance(raw_ip, str) or not raw_ip or raw_port is None:
            continue
        # Narrowed explicitly rather than leaning on the `except TypeError`
        # below: the runtime guard alone leaves the None case invisible to the
        # type checker, and a reader cannot see which absences are handled.
        try:
            ext = int(raw_port)
        except (TypeError, ValueError):
            continue
        inner = e.get("Port", e.get("port"))
        inner_port: int | None = None
        if inner is not None:
            try:
                inner_port = int(inner)
            except (TypeError, ValueError):
                inner_port = None
        proto = e.get("Protocol", e.get("protocol"))
        out.append(
            LeasedIP(
                ip=raw_ip,
                external_port=ext,
                port=inner_port,
                protocol=str(proto) if isinstance(proto, str) else None,
                service=found_service or service,
            )
        )
    return out


# ── outcome classification ───────────────────────────────────────────────


@dataclass
class IpProbeResult:
    """Everything the stage learned, including how it failed."""

    provider: str
    cluster: str
    dseq: str | None = None
    outcome: str = OUTCOME_FAIL
    reason: str = ""
    assigned_ip: str | None = None
    external_port: int | None = None
    in_pool: bool | None = None
    pool_cidr: str | None = None
    reachable: bool | None = None
    http_status: int | None = None
    lease_created_at: str | None = None
    lease_state_at_failure: str | None = None
    diagnostics: dict = field(default_factory=dict)


def classify(
    *,
    ips: list[LeasedIP],
    reachable: bool | None,
    in_pool: bool | None,
    lease_state: str | None,
) -> tuple[str, str]:
    """Decide the outcome, and say why in the same breath.

    `lease_state` is the on-chain state read AT FAILURE TIME, and it is what
    makes churn separable from breakage. Without it, "the IP never answered"
    and "the lease closed before we asked" are the same observation — and that
    is precisely the indistinguishable-failure-paths defect #244 exists to
    close, so it would be perverse to reproduce it in the checker.

    Order matters: churn is checked FIRST, because a closed lease invalidates
    every downstream assertion rather than merely explaining one.
    """
    if lease_state and lease_state.lower() == "closed":
        return OUTCOME_CHURNED, "lease closed mid-probe (on-chain state=closed)"

    if not ips:
        return (
            OUTCOME_FAIL,
            "no IP assigned — lease-status carried no `ips` entry "
            "(this is the wedged-ip-operator shape)",
        )
    if in_pool is False:
        return (
            OUTCOME_FAIL,
            "assigned IP is outside the cluster's declared MetalLB pool",
        )
    if reachable is False:
        return (
            OUTCOME_FAIL,
            "IP assigned and in-pool but NOT reachable from outside — "
            "allocated an address that does not route",
        )
    if reachable is None:
        return OUTCOME_FAIL, "reachability was never established"
    # in_pool None (no declared pool) is not a pass-blocker: we cannot check
    # what we have not declared, and the reachability assertion still held. But
    # the reason string must SAY which checks ran — claiming "in declared pool"
    # when no pool was checked is the same not-measured-vs-measured-true
    # conflation this module exists to prevent, and it would mislead exactly the
    # ledger consumers that read these strings.
    if in_pool is None:
        return (
            OUTCOME_PASS,
            "IP assigned and reachable from outside; pool conformance NOT "
            "checked (no declared pool for this cluster)",
        )
    return OUTCOME_PASS, "IP assigned, in declared pool, reachable from outside"


# ── the hand-off to the delayed visibility job ───────────────────────────
#
# #244 criterion 6 (per_dseq_pricing visibility) CANNOT be asserted here.
# `per_dseq_pricing` lives in akash-accounting, and its read path is doubly
# asynchronous: ingest_spool -> consume_pricing (batch drain) -> the table ->
# compute_per_dseq.py (second batch) -> data/per_dseq.json -> /api/per-dseq.
# A stage that creates and destroys a lease inside a few minutes would be
# asserting on a row that has not landed yet, and "absent because too early"
# would be indistinguishable from "absent because the pipeline dropped it".
# That is the same bug class again, so the two halves are split.
#
# This ledger is the seam. It exists so the delayed job can answer the ONE
# question a synchronous check structurally cannot: has enough time passed?
# `lease_created_at` is therefore the load-bearing field, not decoration —
# without it the delayed job inherits the same not-yet-vs-never ambiguity.

IP_LEDGER_SCHEMA = 1


def build_ledger_record(result: IpProbeResult, *, run_ts: str, version: str) -> dict:
    """One durable record per IP-stage attempt, for the delayed visibility job.

    `schema` is present from day one so the consumer can refuse a shape it does
    not understand instead of silently reading zero matching rows — a consumer
    that reports "no visibility failures" because it could not parse anything
    is the failure this whole issue is about.
    """
    return {
        "schema": IP_LEDGER_SCHEMA,
        "ts": run_ts,
        "version": version,
        "provider": result.provider,
        "cluster": result.cluster,
        "dseq": result.dseq,
        "outcome": result.outcome,
        "reason": result.reason,
        # THE seam field: the delayed job compares this against its own clock to
        # decide whether an absent per_dseq_pricing row means "not yet" or
        # "never". Everything else here is context; this is the contract.
        "lease_created_at": result.lease_created_at,
        "assigned_ip": result.assigned_ip,
        "external_port": result.external_port,
        "in_pool": result.in_pool,
        "pool_cidr": result.pool_cidr,
        "reachable": result.reachable,
        "http_status": result.http_status,
        # What the delayed job must find on the other side, stated here rather
        # than re-derived there, so the two halves cannot drift apart.
        "expect_resource_spec_ip_gt": 0,
        "expect_component_prices_ip_gt": 0,
    }


def write_ledger(path: str | None, record: dict) -> bool:
    """Append one ledger record. Returns whether it was written.

    Best-effort like `_write_telemetry`, with one difference: it returns a bool
    rather than swallowing silently, so the caller can SAY the hand-off did not
    happen. A ledger that quietly stops recording would leave the delayed job
    reporting green forever on an empty input — which is exactly the shape of
    the eight-month outage this module exists to prevent.
    """
    if not path:
        return False
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return True
    except OSError:
        return False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── the stage itself ─────────────────────────────────────────────────────
#
# Dependencies are INJECTED rather than imported, for one reason that matters:
# criterion 7 asks how the no-leak guarantee is proved. With injection it is
# proved by a test that makes a step raise and asserts `destroy` still ran —
# no lease, no escrow, no network. A version that reached for the real
# deployment helpers could only be proved by spending money, which means in
# practice it would not be proved at all.


def run_ip_stage(
    *,
    provider: str,
    cluster: str,
    deploy_and_lease,
    fetch_lease_status,
    fetch_lease_state,
    curl,
    destroy,
    pool_overrides: dict[str, str] | None = None,
    ledger_path: str | None = None,
    run_ts: str | None = None,
    version: str = "",
) -> IpProbeResult:
    """Lease an IP-bearing SDL, prove the address works from outside, destroy.

    Contract of the injected callables:
      deploy_and_lease()      -> (dseq, lease_created_at_iso)
      fetch_lease_status(dseq)-> the provider's lease-status payload (dict)
      fetch_lease_state(dseq) -> on-chain lease state ("active"/"closed"/None)
      curl(url)               -> (reachable: bool, http_status: int | None)
      destroy(dseq)           -> None

    `destroy` runs on EVERY exit path, including an exception raised anywhere
    above it, and including the case where we never obtained an address. The
    only path that does not destroy is the one where no dseq was ever created.
    """
    run_ts = run_ts or utc_now_iso()
    result = IpProbeResult(provider=provider, cluster=cluster)
    cidr = pool_for_cluster(cluster, pool_overrides)
    result.pool_cidr = cidr
    dseq: str | None = None

    try:
        dseq, created_at = deploy_and_lease()
        result.dseq = dseq
        result.lease_created_at = created_at

        status = fetch_lease_status(dseq)
        ips = extract_leased_ips(status)
        if ips:
            first = ips[0]
            result.assigned_ip = first.ip
            result.external_port = first.external_port
            result.in_pool = ip_in_pool(first.ip, cidr)
            # Only curl an address we actually got, and only one it is safe and
            # meaningful to curl. Reachability stays None otherwise — "not
            # measured", never "failed" and never "fine".
            if result.in_pool is False:
                # Already a FAIL on pool conformance; curling it would add SSRF
                # exposure without changing the verdict.
                result.diagnostics["curl_skipped"] = "address outside declared pool"
            elif not is_curlable(first.ip):
                result.diagnostics["curl_skipped"] = (
                    "address is not public (loopback/private/link-local/"
                    "reserved) — refusing to send a request into our own network "
                    "on a provider's say-so"
                )
            else:
                result.reachable, result.http_status = curl(first.url)

        # Read on-chain state ONLY when something looks wrong. A lease that
        # churned mid-probe explains the anomaly; asking on the happy path
        # would spend a chain round-trip to learn nothing.
        lease_state = None
        if not ips or result.in_pool is False or result.reachable is not True:
            lease_state = fetch_lease_state(dseq)
            result.lease_state_at_failure = lease_state

        result.outcome, result.reason = classify(
            ips=ips,
            reachable=result.reachable,
            in_pool=result.in_pool,
            lease_state=lease_state,
        )
        return result
    except Exception as e:  # noqa: BLE001 — the finally below is the point
        result.outcome = OUTCOME_FAIL
        result.reason = f"stage raised: {type(e).__name__}: {e}"
        result.diagnostics["exception"] = repr(e)
        return result
    finally:
        if dseq is not None:
            # Never let a cleanup failure mask the stage's own verdict, and
            # never let it stop the ledger write below.
            try:
                destroy(dseq)
            except Exception as e:  # noqa: BLE001
                result.diagnostics["destroy_error"] = repr(e)
        if ledger_path:
            wrote = write_ledger(
                ledger_path, build_ledger_record(result, run_ts=run_ts, version=version)
            )
            if not wrote:
                result.diagnostics["ledger_write_failed"] = True
