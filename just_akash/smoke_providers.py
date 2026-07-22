#!/usr/bin/env python3
"""Provider capability smoke test.

Deploys a tiny throwaway workload to each configured provider and exercises every
just-akash feature that depends on the provider, then destroys it and prints a
provider x feature pass/fail matrix (non-zero exit if any provider fails any
feature). Features covered:

  deploy    bid + lease creation
  status    lease status from the provider
  exec      run a command over the lease-shell WebSocket (tty=false)
  inject    write a file over lease-shell
  logs      stream container logs (bounded snapshot)
  events    stream kube events (bounded snapshot)
  ssh       exec + inject over the SSH transport (provider port-forwarding)
  connect   interactive session over SSH
  ingress   the provider routes the exposed HTTP port to the container
  update    in-place manifest update (provider applies a new revision)

The point: catch a provider that accepts deployments and runs containers -- so it
looks healthy by every rental metric -- but has a broken shell/logs/exec/ingress
path. That is the v0.14.2-df.1 regression where lease-shell returned HTTP 500
while the provider bid and ran workloads fine; a normal rental never exercises
that path, so nothing else surfaces it.

Usage:
    uv run python -m just_akash.smoke_providers            # preferred tier (AKASH_PROVIDERS)
    uv run python -m just_akash.smoke_providers --all       # preferred + backup tiers
    uv run python -m just_akash.smoke_providers --provider akash1... [--provider ...]

Costs a small amount of AKT: one minimal lease per provider, destroyed
immediately (and on Ctrl-C). An ephemeral SSH keypair is generated per run for
the SSH-transport checks.

Preflight guards (so a low balance or a full provider doesn't score a FALSE
failure): before deploying, a provider whose published capacity can't fit the
probe (or that reports offline) is skipped as NO-ROOM; a deploy that returns
HTTP 402 (insufficient Console credit — nothing is created on-chain) skips the
whole run as NO-CREDIT. Along with NO-BID, these are "couldn't test", never
"failed".

Run from the repository root: cleanup goes through robust_destroy(), which shells
out to `just destroy` / `just list`, so the Justfile and `just` must be available.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from shlex import quote as q

from ._diagnostics import Code, emit
from ._e2e import (
    GREEN,
    RED,
    RESET,
    YELLOW,
    _run,
    install_signal_cleanup,
    resolve_tiers,
    robust_destroy,
)
from ._states import TERMINAL_DEPLOYMENT_STATES
from .api import AkashConsoleAPI, _extract_dseq

# The baseline HTTP marker the probe serves; the update check changes it and
# re-reads it through the ingress to prove a new revision went live.
INGRESS_BASELINE = "probe-baseline"

# Readiness caps (seconds). These are *ceilings*, not fixed waits — a healthy
# provider returns in well under a minute; the cap only bites on a slow or truly
# broken one. Generous on purpose: the failures we chase are readiness LAG
# (service available / ingress route propagation) crossing a short fixed timeout,
# which made a fine provider look broken. Env-tunable so the ceiling can later be
# set from observed p99 latency without a code change.
READY_CAP_S = float(os.environ.get("SMOKE_READY_CAP_S", "240"))
INGRESS_CAP_S = float(os.environ.get("SMOKE_INGRESS_CAP_S", "180"))

# The inject check reads its file back with a lease-shell exec, which can hit the
# cold-stdout race (rc=0 with EMPTY stdout: the exit-code frame arrives before the
# stdout frame) even though the write succeeded — so a healthy inject reads back as
# a FAIL. Retry ONLY that signature a few times; a nonzero rc or wrong content still
# fails on the first read (never mask a genuine inject regression). This is the
# retry-on-empty-stdout remedy the quorum approved for the same fleet-wide race.
_INJECT_READBACK_ATTEMPTS = int(os.environ.get("SMOKE_INJECT_READBACK_ATTEMPTS", "3"))
_INJECT_READBACK_BACKOFF_S = float(os.environ.get("SMOKE_INJECT_READBACK_BACKOFF_S", "2"))

# After a check's cap expires (verdict already FAIL) — readiness, initial-ingress,
# and update-cutover — keep probing for at most this long to classify the failure as
# SLOW (resource eventually appears → the cap was too tight) vs STUCK (never appears
# → a genuine provider defect).
# Diagnostic-only: this NEVER changes the PASS/FAIL verdict — it only records
# evidence so cap-widening is data-driven instead of blind. Paid only on an already-
# failing run, so it costs nothing on the happy path; hard-bounded so a truly-stuck
# provider cannot hang the run. (Quorum-designed; see CHANGELOG 1.18.0.)
POST_CAP_OBSERVE_S = float(os.environ.get("SMOKE_POST_CAP_OBSERVE_S", "90"))

# Probe resource needs, matching PROBE_SDL below. Used by the pre-deploy room
# check so we skip an offline/full provider (NO-ROOM) instead of wasting a deploy
# + bid-wait on it and then mis-reading the result. CPU is in milli-units (Akash
# stats report available cpu as milli-cpu: 1000 = 1 core).
_PROBE_CPU_MILLI = 1000  # 1 cpu
_PROBE_MEM_BYTES = 1 * 1024**3  # 1Gi
_PROBE_STORAGE_BYTES = 5 * 1024**3  # 5Gi

# A single richer probe drives every check: alpine that runs sshd on 22 (SSH
# transport + connect), serves the marker over HTTP on 80 (ingress + update), and
# idles. openssh + busybox-extras (for httpd) are installed at boot -- the stock
# busybox has no httpd applet. Nothing about this workload can explain a provider
# feature failing, so a failure is unambiguously the provider's.
PROBE_SDL = """\
---
version: "2.0"
services:
  probe:
    image: alpine:3.20
    env:
      - SSH_PUBKEY_B64=PLACEHOLDER_SSH_PUBKEY_B64
      - SMOKE_MARKER=__SMOKE_MARKER__
      - BEACON_URL=__BEACON_URL__
    expose:
      - port: 22
        as: 22
        to:
          - global: true
      - port: 80
        as: 80
        to:
          - global: true
    args:
      - sh
      - -c
      - |
        set -e
        # HTTP first: it only needs busybox-extras, so the ingress backend starts
        # serving ASAP -- decoupled from the slower openssh install that used to
        # gate it and inflate ingress readiness latency + variance.
        apk add --no-cache busybox-extras >/dev/null 2>&1
        mkdir -p /www
        printf '%s' "$SMOKE_MARKER" > /www/index.html
        busybox-extras httpd -p 80 -h /www
        echo probe-http-up
        # Then SSH (a separate, later install; sshd readiness is checked on its own).
        apk add --no-cache openssh >/dev/null 2>&1
        mkdir -p /run/sshd /root/.ssh
        echo "$SSH_PUBKEY_B64" | base64 -d > /root/.ssh/authorized_keys
        chmod 700 /root/.ssh; chmod 600 /root/.ssh/authorized_keys
        ssh-keygen -A
        sed -i 's/#\\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
        /usr/sbin/sshd
        echo probe-container-up
        # --- instrumentation: heartbeat + death-cause capture (issue #646) --------
        # Keep the container alive AND make a lease-down self-diagnosing. Each
        # heartbeat pins liveness + the memory/PSI pressure at that instant; the
        # signal trap names a GRACEFUL termination (the provider deleting the pod).
        # A hard kill (OOM / eviction / node loss) is un-trappable -- it just stops
        # the heartbeats, so the gap + the last pressure reading is the tell.
        # PSI (/proc/pressure) + cgroup throttling are the right in-container health
        # signals (CPU steal is hypervisor-level and ~0 on bare-metal k8s).
        # BEACON_URL (optional, unset by default): POST each heartbeat + the dying
        # line to a user-run collector so the evidence survives the lease teardown
        # (provider logs can go empty the moment the lease closes). Needs curl, which
        # busybox wget can't POST -- installed only when the beacon is enabled.
        if [ -n "$BEACON_URL" ]; then apk add --no-cache curl >/dev/null 2>&1 || true; fi
        beacon() {
          [ -z "$BEACON_URL" ] && return 0
          command -v curl >/dev/null 2>&1 || return 0
          curl -sf -m 3 -o /dev/null --data-urlencode "probe=$1" "$BEACON_URL" || true
        }
        die() {
          M="PROBE-DYING signal=$1 ts=$(date +%s)"
          echo "$M"; beacon "$M"; exit 0
        }
        set +e  # this monitor loop is deliberately failure-tolerant
        HB_START=$(date +%s)
        trap 'die TERM' TERM
        trap 'die INT' INT
        trap 'die QUIT' QUIT
        while true; do
          NOW=$(date +%s)
          MEM=$(cat /sys/fs/cgroup/memory.current 2>/dev/null || echo -1)
          MMAX=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo -1)
          MPSI=$(grep '^some' /proc/pressure/memory 2>/dev/null | cut -d' ' -f2)
          CPSI=$(grep '^some' /proc/pressure/cpu 2>/dev/null | cut -d' ' -f2)
          THR=$(grep nr_throttled /sys/fs/cgroup/cpu.stat 2>/dev/null | cut -d' ' -f2)
          HB="PROBE-HB ts=$NOW up=$((NOW-HB_START)) mem=$MEM/$MMAX mpsi=$MPSI cpsi=$CPSI thr=$THR"
          echo "$HB"
          beacon "$HB"
          sleep 5 &
          wait "$!"
        done
profiles:
  compute:
    probe:
      resources:
        cpu: { units: 1 }
        memory: { size: 1Gi }
        storage: [{ size: 5Gi }]
  placement:
    akash:
      pricing:
        probe: { denom: uact, amount: 10000 }
deployment:
  probe:
    akash: { profile: probe, count: 1 }
""".replace("__SMOKE_MARKER__", INGRESS_BASELINE).replace(
    # The death-cause beacon ships DARK: BEACON_URL reaches the container only when
    # the operator sets SMOKE_BEACON_URL in the smoke's environment (a collector on
    # their infra). Empty by default -> the in-probe `[ -z "$BEACON_URL" ]` guard
    # no-ops it, so nothing is POSTed and no curl is installed.
    # .strip() so a pasted trailing newline/space in the secret can't inject a broken
    # line or a spurious extra env entry into the SDL YAML.
    "__BEACON_URL__",
    os.environ.get("SMOKE_BEACON_URL", "").strip(),
)

# Ordered feature columns for the report.
FEATURES = [
    "deploy",
    "status",
    "exec",
    "inject",
    "logs",
    "events",
    "ssh",
    "connect",
    "ingress",
    "update",
]

# Telemetry rows = every feature plus "ready" (time-to-serving — not a matrix
# column, but the leading latency signal for the readiness-lag we chase).
_TELEMETRY_FEATURES = [*FEATURES, "ready"]

# Outcomes that count as a provider FAILURE (trip the run + carry diagnostics).
# LEASE-DOWN is distinct from FAIL — the provider ACCEPTED the bid and the lease
# then terminated on-chain (state failed/closed), a fulfillment failure rather than
# a broken feature — but it is still a genuine reliability failure, unlike the
# pre-commitment NO-BID / NO-ROOM / NO-CREDIT skips. (Quorum-designed, unanimous.)
LEASE_DOWN = "LEASE-DOWN"
_FAILING_OUTCOMES = ("FAIL", LEASE_DOWN)


def _quarantined_providers() -> set[str]:
    """Providers explicitly quarantined as genuinely-unreliable (env
    SMOKE_QUARANTINE_PROVIDERS, comma-separated). Their PROVIDER-RELIABILITY failures
    (LEASE-DOWN, or a proven ingress-routing stall) are still deployed, tested, shown
    and recorded — but do NOT gate CI. A TOOLING regression on them (a feature breaking
    on a HEALTHY lease) STILL gates. Lets a known-bad provider stay monitored without
    its infra flakiness reddening the run. (Quorum-designed, unanimous.)"""
    raw = os.environ.get("SMOKE_QUARANTINE_PROVIDERS", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def _service_ready(service_at_timeout: object) -> bool:
    """Parse a 'ready/total' snapshot string; True if >=1 replica was ready."""
    if not isinstance(service_at_timeout, str) or "/" not in service_at_timeout:
        return False
    try:
        return int(service_at_timeout.split("/", 1)[0]) >= 1
    except ValueError:
        return False


def _is_reliability_failure(feature: str, outcome: str, diag: dict | None) -> bool:
    """Does this failing cell reflect PROVIDER infra (demote for a quarantined provider)
    vs a just-akash TOOLING bug (always gate)?

    LEASE-DOWN always qualifies (the lease died on-chain). An update-cutover stall
    qualifies ONLY when the diagnostics prove the new pod was healthy but the ingress
    never routed, or the marker eventually served (merely slow) — NOT when the update
    command itself failed (``fail_mode``) or the update never reached the pod
    (``in_pod_marker == 'old'``, the genuine-bug signature that must stay gating).
    A real just-akash regression is deterministic across providers, so the multi-
    provider matrix still catches it. (Quorum-designed, 2 unanimous rounds.)"""
    if outcome == LEASE_DOWN:
        return True
    if outcome == "FAIL" and feature == "update" and isinstance(diag, dict):
        if diag.get("fail_mode") == "update_command":
            return False  # the update command itself failed = tooling bug → gate
        eventual = diag.get("eventual")
        if eventual == "arrived":
            return True  # the marker eventually served = provider was slow, not a bug
        if eventual == "never":
            in_pod = diag.get("in_pod_marker")
            if in_pod == "new":
                return True  # new pod HAS the env, ingress never routed = provider infra
            if in_pod == "unreachable" and _service_ready(diag.get("service_at_timeout")):
                return True  # exec flaky but service healthy = the unreliability we quarantined
    return False


def _mass_lease_down(rows: dict) -> bool:
    """True when EVERY provider that got a lease (deploy PASS) LEASE-DOWNed in this
    run AND at least 2 did. A LEASE-DOWN is normally independent per-provider infra,
    but a fleet-wide SIMULTANEOUS one is deterministic — the tell-tale of a just-akash
    manifest/deploy bug (a malformed SDL every provider accepts then fails) rather than
    coincident hiccups. The >=2 floor stops a single-provider run from degenerating to
    'gate on any LEASE-DOWN', which is the exact flakiness we're removing."""
    leased = [p for p, r in rows.items() if r.get("deploy") == "PASS"]
    if len(leased) < 2:
        return False
    return all(any(rows[p].get(f) == LEASE_DOWN for f in FEATURES) for p in leased)


def _gating_providers(rows: dict, records: list, quarantined: set) -> dict:
    """Providers whose failures GATE the run → ``{provider: [gating features]}``.

    LEASE-DOWN is a fleet-wide PROVIDER-INFRA outcome (the provider accepted the bid
    then the lease died on-chain) — never a just-akash tooling bug — so it is
    NON-GATING for every provider, and stays visible in the matrix + telemetry. The
    one exception is the mass-lease-down safety valve (see ``_mass_lease_down``): a
    simultaneous fleet-wide lease death is re-gated as a likely just-akash manifest
    bug. A TOOLING regression (a feature broken on a healthy lease) always gates; a
    quarantined provider's proven update-ingress stall is additionally demoted."""
    diag_by = {(r.get("provider"), r.get("feature")): r.get("diag") for r in records}
    out: dict = {}
    for provider, row in rows.items():
        gating = []
        for f in FEATURES:
            v = row.get(f)
            if v not in _FAILING_OUTCOMES:
                continue
            if v == LEASE_DOWN:
                continue  # provider infra, non-gating (mass check below re-gates a fleet-wide one)
            if provider in quarantined and _is_reliability_failure(
                f, v, diag_by.get((provider, f))
            ):
                continue  # a quarantined provider's proven update-ingress stall
            gating.append(f)
        if gating:
            out[provider] = gating
    # Safety valve: a simultaneous fleet-wide lease death is likely OUR manifest bug.
    # MERGE the LEASE-DOWN features into any existing gating list (don't setdefault —
    # a provider could already be gating on a tooling FAIL, and its LEASE-DOWN features
    # must still be added, not dropped), preserving feature order.
    if _mass_lease_down(rows):
        for p, r in rows.items():
            if r.get("deploy") != "PASS":
                continue
            existing = set(out.get(p, []))
            merged = [f for f in FEATURES if f in existing or r.get(f) == LEASE_DOWN]
            if merged:
                out[p] = merged
    return out


def _pkg_version() -> str:
    try:
        from importlib.metadata import version

        return version("just-akash")
    except Exception:  # noqa: BLE001 — telemetry must never break the run
        return "unknown"


def _provider_records(
    provider: str,
    dseq: str | None,
    results: dict,
    latencies: dict,
    diagnostics: dict | None = None,
    frame_shape: str | None = None,
    exit_code_shapes: set[str] | None = None,
) -> list[dict]:
    """One telemetry record per feature: outcome + how long it took (ms).

    latency_ms is None for a feature that was never reached (e.g. everything
    after a no-bid). Pass/fail is the lagging binary; latency is the leading
    signal that lets us later set percentile timeouts and spot regressions. A
    feature with a failing outcome (FAIL or LEASE-DOWN) also carries a ``diag``
    classification (slow-vs-stuck, or the lease-down terminal state), so a failure
    says WHY without turning green.
    """
    diagnostics = diagnostics or {}
    return [
        {
            "provider": provider,
            "feature": feat,
            "outcome": results.get(feat, "-"),
            "latency_ms": latencies.get(feat),
            "dseq": dseq,
            # frame_shape rides on the exec record for EVERY exec (pass or fail) so the
            # DROP-vs-drained-reorder rate is quantifiable over time (issue #3438).
            **({"frame_shape": frame_shape} if feat == "exec" and frame_shape else {}),
            # issue #85 survey: the null/missing-exit_code shapes the shim reported
            # anywhere in this probe. Recorded ONLY when the shim actually fired, so
            # the field's absence is the clean signal the 30-day streak counts — a
            # present-but-empty field would be indistinguishable from "not measured"
            # in the historical records written before this existed.
            **(
                {"exit_code_shapes": sorted(exit_code_shapes)}
                if feat == "exec" and exit_code_shapes
                else {}
            ),
            # diag is failure evidence — attach it only to a failing outcome (FAIL or
            # LEASE-DOWN), so a PASS/skip can never carry a stale/partial diag payload.
            **(
                {"diag": diagnostics[feat]}
                if results.get(feat) in _FAILING_OUTCOMES and diagnostics.get(feat)
                else {}
            ),
        }
        for feat in _TELEMETRY_FEATURES
    ]


def _write_telemetry(path: str, run_ts: str, version: str, records: list[dict]) -> None:
    """Append one JSON line per record. Best-effort: a telemetry failure must
    never fail the smoke run."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps({"ts": run_ts, "version": version, **rec}) + "\n")
        print(f"  telemetry: wrote {len(records)} record(s) to {path}")
    except OSError as e:
        print(f"  {YELLOW}telemetry write failed: {e}{RESET}")


_API: AkashConsoleAPI | None = None


def _api() -> AkashConsoleAPI:
    global _API
    if _API is None:
        _API = AkashConsoleAPI(os.environ["AKASH_API_KEY"])
    return _API


def _hdr(msg: str) -> None:
    print(f"\n{YELLOW}== {msg} =={RESET}", flush=True)


# ── orphaned-probe sweep ─────────────────────────────────────────────
#
# A run that is hard-killed (CI job timeout -> SIGKILL, runner crash) can die
# after creating a probe lease but before its finally / signal-handler cleanup
# destroys it. Nothing else reaps that probe, so it drains escrow for days until
# the chain closes it. So every run sweeps FIRST: it reaps any deployment whose
# only service is the probe service, before deploying fresh probes -- making the
# daily job self-healing. Identification is surgical (the probe SDL names its one
# service `probe`, which real workloads like runner/train never use) and
# fail-safe (a deployment we cannot positively identify is left alone). An age
# floor spares a probe that a *concurrent* run is still holding.

# The probe SDL's sole service name. A deployment whose service set is exactly
# {PROBE_SERVICE} is unambiguously a leaked smoke probe, never a user workload.
PROBE_SERVICE = "probe"

# Don't reap a probe younger than this: a concurrent smoke run could still be
# using it (a run holds one probe for up to the whole matrix, ~tens of minutes).
# A genuine orphan is seen by the next daily run (~24h later), far past this.
MIN_ORPHAN_AGE_SECONDS = 3600  # 1 hour


def _deployment_service_names(detail: dict) -> set[str]:
    """Service names an active deployment is running, from its lease status.

    Reads leases[].status.services.<name>; the provider populates this from the
    live manifest. Empty when the provider reported no services (down / still
    starting) -- callers must treat "empty" as "cannot classify", not "probe".
    """
    names: set[str] = set()
    for lease in detail.get("leases") or []:
        if not isinstance(lease, dict):
            continue
        # Guard every hop: a non-dict `status` (string/list from a malformed or
        # partial provider response) would make `.get` raise and abort the
        # best-effort sweep. Treat anything unexpected as "no services".
        status = lease.get("status")
        services = status.get("services") if isinstance(status, dict) else None
        if isinstance(services, dict):
            names.update(str(k) for k in services)
    return names


def _probe_age_seconds(dseq: str | None, now: float | None = None) -> float | None:
    """Age of a deployment in seconds, derived from its millisecond-epoch dseq.

    just-akash mints dseqs as ms-since-epoch timestamps, so the dseq itself is a
    reliable creation clock (the on-chain created_at is a block height, not a
    wall time). Returns None if the dseq is missing or is not a plausible recent
    ms timestamp (e.g. a legacy block-height dseq) so we never mis-age and reap
    wrongly.
    """
    try:
        created = int(dseq) / 1000.0  # type: ignore[arg-type]  # None -> TypeError, handled below
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    # A real ms-epoch dseq lands in a sane window: after 2020 and not implausibly
    # far in the future (allow ~1 day of clock skew between us and the chain).
    # Anything outside it (tiny block height, garbage) -> unknown age.
    if created < 1_577_836_800 or created > now + 86_400:  # 2020-01-01 .. now+1d
        return None
    return now - created


def _is_orphan_probe(
    detail: dict, dseq: str, *, min_age_seconds: float, now: float | None = None
) -> bool:
    """True only if `detail` is unambiguously a reapable leaked probe.

    Requires BOTH: the service set is exactly {PROBE_SERVICE}, and the
    deployment is at least `min_age_seconds` old (so a concurrent run's live
    probe is spared). Anything we cannot positively identify is left alone.
    """
    if _deployment_service_names(detail) != {PROBE_SERVICE}:
        return False
    age = _probe_age_seconds(dseq, now)
    # Unknown age -> do not reap (fail safe). Known-but-young -> spare it.
    return age is not None and age >= min_age_seconds


def _is_not_found_error(exc: Exception) -> bool:
    """True only for a 404 (deployment already gone), not any other API error.

    The API client raises RuntimeError("API Error (404): ...") with the status
    code as a fixed prefix, so match that prefix -- a bare "(404)" substring
    could spuriously match a non-404 error whose body merely mentions "(404)"
    and wrongly treat an uninspected deployment as gone, hiding a leak.
    """
    return str(exc).startswith("API Error (404)")


def sweep_orphan_probes(
    *, dry_run: bool = False, min_age_seconds: float = MIN_ORPHAN_AGE_SECONDS
) -> list[str]:
    """Reap probe deployments leaked by a hard-killed earlier run.

    Returns the dseqs destroyed (or, in dry_run, the ones that would be).
    Best-effort: never raises -- a sweep failure must not block the smoke run.
    """
    now = time.time()
    try:
        deployments = _api().list_deployments(active_only=True)
    except Exception as e:  # noqa: BLE001 -- sweep must never abort the run
        print(f"  {YELLOW}orphan sweep skipped: list_deployments failed: {e}{RESET}")
        return []
    found: list[str] = []  # orphans identified
    swept: list[str] = []  # orphans confirmed destroyed (or, in dry-run, matched)
    uninspected: list[str] = []  # listed but detail fetch genuinely failed
    for dep in deployments:
        dseq = _extract_dseq(dep)
        if not dseq:
            continue
        try:
            detail = _api().get_deployment(dseq)
        except Exception as e:  # noqa: BLE001 -- must not abort the sweep
            # A 404 means the deployment is already gone -> not an active leak,
            # safe to skip. Any OTHER error means we could not inspect it, so the
            # sweep is INCOMPLETE and must say so rather than report an all-clear
            # (list said it exists; we just couldn't confirm what it is).
            if not _is_not_found_error(e):
                uninspected.append(dseq)
                print(f"  {YELLOW}orphan sweep: could not inspect {dseq}: {str(e)[:120]}{RESET}")
            continue
        if not _is_orphan_probe(detail, dseq, min_age_seconds=min_age_seconds, now=now):
            continue
        found.append(dseq)
        age = _probe_age_seconds(dseq, now)
        age_note = f"{int(age // 60)}m old" if age is not None else "age unknown"
        # In dry-run we only report; say so, so the per-probe line can't be read
        # as "this was destroyed" for a safety-critical cleanup that did nothing.
        action = "would reap (dry-run)" if dry_run else "reaping"
        print(
            f"  {YELLOW}orphaned probe {dseq} ({age_note}) — leaked by an earlier "
            f"run; {action}{RESET}"
        )
        if dry_run or robust_destroy(dseq):
            swept.append(dseq)
    # An incomplete sweep must never masquerade as a clean all-clear: flag any
    # deployment we could not inspect so the log reflects that a leak may have
    # gone unseen.
    if uninspected:
        print(
            f"  {YELLOW}orphan sweep INCOMPLETE: {len(uninspected)} deployment(s) "
            f"could not be inspected: {', '.join(uninspected)}{RESET}"
        )
    if not found:
        suffix = " among inspected deployments" if uninspected else ""
        print(f"  orphan sweep: no leaked probes found{suffix}")
    elif dry_run:
        print(f"  orphan sweep: would reap {len(swept)} leaked probe(s): {', '.join(swept)}")
    else:
        print(
            f"  orphan sweep: reaped {len(swept)}/{len(found)} leaked probe(s): {', '.join(swept)}"
        )
        # A found-but-not-destroyed orphan must NOT be silently reported as clean:
        # it is still draining escrow and needs a human. Surface it loudly.
        stuck = [d for d in found if d not in swept]
        if stuck:
            print(
                f"  {RED}orphan sweep: {len(stuck)} probe(s) could NOT be destroyed "
                f"(manual cleanup required): {', '.join(stuck)}{RESET}"
            )
    return swept


# ── readiness + resolution helpers ───────────────────────────────────


def _death_cause(log_lines: list[str], lease_down: bool) -> str | None:
    """Summarize HOW the container died, from instrumented-probe logs (issue #646).

    A ``PROBE-DYING`` line after the probe's last heartbeat (i.e. not followed by a
    restart's newer heartbeats) means it caught a termination signal — the provider
    deleted the pod, i.e. a graceful, deliberate close. If the
    lease is down but the probe only left heartbeats (no dying line), it vanished
    without a signal: a hard kill (OOM / eviction / node loss); the last heartbeat
    pins the death instant + the memory/PSI pressure then, separating an OOM (pressure
    climbing) from a clean close. When the lease is NOT down (a feature flaked on a
    live container) heartbeats are just liveness, not a death — return None so we
    never mislabel a healthy-lease failure as a kill. Also None when uninstrumented.
    """
    # Token membership (not substring) so an unrelated line mentioning the marker in
    # prose can't false-match; the log line is "[pod] PROBE-HB …" so the marker is its
    # own whitespace-delimited token.
    dying_idx = max(
        (i for i, ln in enumerate(log_lines) if "PROBE-DYING" in ln.split()), default=-1
    )
    hb_idx = max((i for i, ln in enumerate(log_lines) if "PROBE-HB" in ln.split()), default=-1)
    # A dying line AFTER the last heartbeat = a termination signal was the final thing
    # the probe emitted (a stale one before newer heartbeats would be a restart).
    if dying_idx > hb_idx:
        return f"death-cause: GRACEFUL termination — {log_lines[dying_idx].strip()}"
    if lease_down and hb_idx >= 0:
        return (
            "death-cause: NO termination signal (hard kill / OOM / eviction) — "
            f"last heartbeat: {log_lines[hb_idx].strip()}"
        )
    return None


def _capture_diagnostics(dseq: str, reason: str) -> None:
    """On a failure, dump the provider's lease status + kube events + container
    logs, so an INTERMITTENT problem (e.g. an occasional 'lease never ready')
    self-documents in the run log instead of needing a live catch. The kube
    events are the payoff — they say WHY a pod didn't come up (FailedScheduling,
    Insufficient cpu/memory, ImagePullBackOff, OOMKilled, …). Best-effort: never
    raises, and bounded by each stream's --duration."""
    print(f"  {YELLOW}── diagnostics: {reason} (dseq {dseq}) ──{RESET}")
    avail: tuple[int, int] | None = None
    try:
        st = _status_json(dseq)
        avail = _service_availability(dseq)
        state = st.get("status") if isinstance(st, dict) else st
        print(f"    lease status={state} availability={avail}")
    except Exception as e:  # noqa: BLE001 — diagnostics must never break the run
        print(f"    status capture failed: {type(e).__name__}: {e}")
    log_lines: list[str] = []
    for kind, dur in (("events", 12), ("logs", 8)):
        try:
            r = _run(
                f"uv run just-akash {kind} --dseq {q(dseq)} --duration {dur}", timeout=dur + 25
            )
            lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
            if kind == "logs":
                log_lines = lines
            # Show the TAIL, not the head: the death signal (PROBE-DYING), the most
            # recent heartbeats, and the terminating kube events (Killing/OOMKilled)
            # all live at the END of the stream -- the head is just startup noise.
            trunc = f" (last 20 of {len(lines)})" if len(lines) > 20 else ""
            print(f"    --- {kind} ({len(lines)} line(s)){trunc} ---")
            for ln in lines[-20:]:
                print(f"      {ln}")
            # Surface a stream failure (non-zero exit / stderr) in ALL cases, even
            # when it also produced some stdout — a partial/errored stream is
            # itself diagnostic, and a bare "(no output)" would hide it. Collapse
            # stderr to one line so a multi-line message can't break indentation.
            err = " ".join((r.stderr or "").split())
            if r.returncode != 0 or err:
                print(f"      ({kind} stream errored: rc={r.returncode} {err[:200]})")
            elif not lines:
                print(f"      (no {kind} returned — nothing to show)")
        except Exception as e:  # noqa: BLE001
            print(f"    {kind} capture failed: {type(e).__name__}: {e}")
    # Is the lease actually down? Trust the availability check (0 serving replicas)
    # as the primary, reason-independent signal, so a real lease-down that surfaced
    # first as a feature failure (reason "status check failed", not "lease …") is
    # still classified — falling back to the reason string only when availability is
    # unknown (None, e.g. lazily-unreported). Diagnostic only; the raw events tail
    # still shows an OOMKilled/Killing event even if this stays silent.
    # Classify a death only when the lease is terminally down via a PROVIDER-fulfillment
    # state (_LEASE_DOWN_STATES = failed/closed) — not insufficient_funds (a funding
    # close, not a provider kill). Fall back to the reason string ("lease never became
    # ready") only when the on-chain state is unreadable. Diagnostic only; the events
    # tail still shows OOMKilled/Killing even when this stays silent.
    lease_down = _dead_state(dseq) in _LEASE_DOWN_STATES or "lease" in reason.lower()
    cause = _death_cause(log_lines, lease_down=lease_down)
    if cause:
        print(f"    {YELLOW}{cause}{RESET}")


def _provider_room(provider: str) -> tuple[bool, str]:
    """(has_room, reason) from the provider's published capacity stats.

    A proactive pre-deploy check: skip an offline or full provider instead of
    spending a deploy + bid-wait on it. FAIL-OPEN — if capacity can't be read
    (registry miss, stats absent, API error), return True and let the bid decide,
    so a stats hiccup never skips a healthy provider.
    """
    try:
        p = _api().get_provider(provider)
    except Exception as e:  # noqa: BLE001 — capacity check must never abort the run
        return True, f"capacity unknown ({type(e).__name__}); proceeding"
    if not isinstance(p, dict):
        return True, "provider not in registry; proceeding"
    if p.get("isOnline") is False:
        return False, "provider reports offline"
    stats = p.get("stats")
    if not isinstance(stats, dict):
        return True, "no capacity stats; proceeding"

    def _avail(*keys) -> float | None:
        node = stats
        for k in keys:
            node = node.get(k) if isinstance(node, dict) else None
        return node.get("available") if isinstance(node, dict) else None

    checks = (
        ("cpu", _avail("cpu"), _PROBE_CPU_MILLI),
        ("memory", _avail("memory"), _PROBE_MEM_BYTES),
        ("storage", _avail("storage", "ephemeral"), _PROBE_STORAGE_BYTES),
    )
    for name, avail, need in checks:
        if isinstance(avail, (int, float)) and avail < need:
            return False, f"insufficient {name} (available {avail} < {need} needed)"
    return True, "ok"


def _bidders_from_output(out: str) -> list[str]:
    """Provider addresses that DID bid, parsed from deploy's bid-table output.

    deploy logs every bid it saw as ``provider=akash1... price=N uact state=...``
    (both the poll lines and the tier tables), so a no-bid run still carries proof
    of who was in the market. Deduped, order-preserving.
    """
    seen: dict[str, None] = {}
    for m in re.finditer(r"provider=(akash1[a-z0-9]+)", out):
        seen.setdefault(m.group(1), None)
    return list(seen)


def _record_no_bid_evidence(provider: str, out: str) -> None:
    """Explain a NO-BID instead of silently recording it.

    A bare "NO-BID" cannot distinguish "the provider declined" from "we never got a
    usable answer" — that ambiguity let a HEALTHY, actively-bidding provider read as
    absent for 32 consecutive runs. So capture the evidence that was already on
    screen but thrown away: WHO did bid on the same order, and the target's on-chain
    status. Emits a structured PROVIDER_NO_BID / PROVIDER_OFFLINE / ... diagnostic
    (docs/diagnostics.md) so CI/Sentry can act on it, plus a human line.

    Best-effort and never raises: this is diagnostics, not control flow.
    """
    try:
        bidders = _bidders_from_output(out)
        others = [b for b in bidders if b != provider]
        info = {}
        try:
            info = _api().get_provider(provider) or {}
        except Exception:  # noqa: BLE001 — evidence is best-effort
            info = {}
        online = info.get("isOnline")
        valid = info.get("isValidVersion")
        raw_stats = info.get("stats")
        stats: dict = raw_stats if isinstance(raw_stats, dict) else {}
        raw_cpu = stats.get("cpu")
        cpu: dict = raw_cpu if isinstance(raw_cpu, dict) else {}
        raw_mem = stats.get("memory")
        mem: dict = raw_mem if isinstance(raw_mem, dict) else {}

        # Classify from the on-chain status, mirroring deploy.py's no-bid block.
        if not info:
            code, msg = Code.PROVIDER_UNKNOWN, "not found in the provider registry"
        elif online is False:
            code, msg = Code.PROVIDER_OFFLINE, "provider reports offline"
        elif valid is False:
            code, msg = Code.PROVIDER_INVALID_VERSION, "provider runs an invalid version"
        else:
            code, msg = (
                Code.PROVIDER_NO_BID,
                f"healthy on-chain but did not bid, while {len(others)} other "
                "provider(s) bid on the same order",
            )
        emit(
            code,
            "warning",
            f"NO-BID {provider}: {msg}",
            provider=provider,
            isOnline=online,
            isValidVersion=valid,
            cpu_available=cpu.get("available"),
            mem_available=mem.get("available"),
            other_bidders=len(others),
            market_had_bids=bool(bidders),
        )
        # Human line: the market context is the part that makes a NO-BID readable.
        if others:
            print(
                f"  {YELLOW}NO-BID evidence{RESET}: {len(others)} other provider(s) bid "
                f"on this order — {msg} (isOnline={online} isValidVersion={valid})"
            )
        else:
            print(
                f"  {YELLOW}NO-BID evidence{RESET}: NOBODY bid on this order "
                f"(market-wide, not {provider[:14]}…-specific)"
            )
    except Exception as e:  # noqa: BLE001 — diagnostics must never break the run
        print(f"  {YELLOW}NO-BID evidence unavailable{RESET}: {type(e).__name__}: {e}")


def _deploy(sdl_path: str, provider: str, dseq_ref: dict) -> tuple[str | None, str]:
    """Deploy the probe pinned to ``provider``. Returns (dseq, note).

    dseq is None when the provider did not bid (note == "no-bid") or the deploy
    failed (note == "deploy-failed"). Backups are disabled so the lease can only
    land on the target provider. SSH_PUBKEY (set by main) is substituted into the
    SDL's PLACEHOLDER so sshd trusts our ephemeral key.

    A returned dseq means a lease genuinely exists: deploy exits non-zero on every
    pre-lease failure, and only ever returns 0 after create_lease succeeds. That is
    what entitles the caller to read a later terminal state as a real LEASE-DOWN
    rather than a lease that never formed. ``dseq_ref`` is populated whenever a dseq
    was seen at all — including on failure — so cleanup can never miss one.
    """
    r = _run(
        f"uv run just-akash deploy --sdl {q(sdl_path)} "
        f"--provider {q(provider)} --backup-provider '' "
        f"--bid-wait 120 --bid-wait-retry 60",
        timeout=420,
    )
    out = (r.stdout or "") + (r.stderr or "")
    # findall + [-1], never search: on the stale-bid path (issue #19) deploy closes
    # the original order and re-creates a fresh one, printing BOTH dseqs. The LAST
    # is the live lease; the first is already closed. Taking the first would test a
    # dead deployment (every feature reads LEASE-DOWN) AND orphan the live lease to
    # drain escrow. Only one dseq can ever be live: the re-deploy aborts outright if
    # the original's close fails ("not re-deploying, to avoid double escrow").
    dseqs = re.findall(r"DSEQ[:=]\s*(\d+)", out)
    if dseqs:
        # Record for cleanup even when the deploy FAILED: deploy closes its own
        # deployment on the way out, but that close is best-effort and can itself
        # fail, so the finally must still be able to destroy it. A redundant destroy
        # is a no-op; a missed one drains real escrow.
        dseq_ref["dseq"] = dseqs[-1]
    # A printed DSEQ is NOT success. deploy prints it at CREATE time, long before
    # bidding, then closes the deployment and exits non-zero on every no-bid path.
    # Gating on the exit code is what stops a no-bid (a market condition) from being
    # misreported as a provider LEASE-DOWN — and it is what makes the notes below
    # reachable at all: without it the DSEQ match short-circuits every one of them.
    if dseqs and r.returncode == 0:
        return dseqs[-1], "ok"
    # Insufficient Console credit is account-wide, not a provider fault: the
    # deployment create returns HTTP 402 and NOTHING is created on-chain. Surface
    # it as its own note so the run skips cleanly instead of scoring the provider
    # FAIL. (This is the authoritative credit check — the Console API exposes no
    # balance endpoint, and a 402 probe commits no resources.)
    if re.search(r"\(402\)|PaymentRequired|[Ii]nsufficient balance", out):
        return None, "no-credit"
    # Case-insensitive, and "bids" as well as "bid": deploy's wording differs by path
    # — "NO BID FROM n allowlisted provider(s)" when an allowlisted provider ignored
    # us, but "No bids received within Ns" when nothing bid at all. The old
    # case-sensitive "NO BID|no bid" matched neither "No bids", so that second path
    # fell through to deploy-failed and scored a pure market condition as a provider
    # FAIL. It only classified correctly by accident, via the co-occurring
    # "Cleaning up deployment N (no bids)" log line — i.e. the verdict hung on
    # incidental log wording. Matching the real message removes that dependency.
    if re.search(r"no bids?\b|none from our providers|foreign bids", out, re.IGNORECASE):
        _record_no_bid_evidence(provider, out)
        return None, "no-bid"
    return None, "deploy-failed"


def _status_json(dseq: str) -> dict:
    r = _run(f"uv run just-akash status --dseq {q(dseq)} --json", timeout=30)
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _service_availability(dseq: str) -> tuple[int, int] | None:
    """(available_replicas, service_count) from the provider's lease status.

    Reads leases[].status.services[*].available / ready_replicas — the field that
    reflects whether the container is actually SERVING, which the lease-level
    ``status: ready`` does not (that flips the moment a manifest is accepted, long
    before the pod is up). Returns None when no service was reported yet (can't
    classify — keep waiting), so callers never read "unreported" as "available".
    """
    try:
        dep = _api().get_deployment(dseq)
    except Exception:  # noqa: BLE001 — a transient read error just means "keep waiting"
        return None
    available = 0
    count = 0
    saw_service = False
    for lease in dep.get("leases") or []:
        if not isinstance(lease, dict):
            continue
        status = lease.get("status")
        services = status.get("services") if isinstance(status, dict) else None
        if not isinstance(services, dict):
            continue
        for info in services.values():
            if not isinstance(info, dict):
                continue
            saw_service = True
            count += 1
            # Providers populate one or both; take the larger as "serving replicas".
            a = info.get("available")
            rr = info.get("ready_replicas")
            serving = max(
                a if isinstance(a, (int, float)) else 0,
                rr if isinstance(rr, (int, float)) else 0,
            )
            available += int(serving)
    return (available, count) if saw_service else None


# Terminal on-chain states readiness can't recover from. "failed"/"closed" are the
# provider mapping of Console status "down" (api.py canopy_status) — the provider
# accepted the bid then the lease died = a LEASE-DOWN fulfillment failure. Escrow
# exhaustion is OUR funding issue, not a provider fault, so it is dead but NOT
# lease-down. "failed" was originally missing here — that omission is why a failed
# lease wasn't fast-failed and readiness burned the whole cap. _DEAD_STATES is the
# shared TERMINAL_DEPLOYMENT_STATES (single-sourced in _states.py); _LEASE_DOWN_STATES
# is a distinct, smoke-specific subset (failed/closed = provider infra fault).
_DEAD_STATES = TERMINAL_DEPLOYMENT_STATES
_LEASE_DOWN_STATES = {"failed", "closed"}


def _dead_state(dseq: str) -> str | None:
    """The specific terminal state the deployment/lease is stuck in, or None if it is
    still live (or unreadable — a transient read error is NOT treated as dead)."""
    try:
        dep = _api().get_deployment(dseq)
    except Exception:  # noqa: BLE001
        return None
    states: list = []
    d = dep.get("deployment") if isinstance(dep, dict) else None
    if isinstance(d, dict):
        states.append(d.get("state"))
    for lease in dep.get("leases") or []:
        if isinstance(lease, dict):
            states.append(lease.get("state"))
    for s in states:
        if isinstance(s, str) and s.lower() in _DEAD_STATES:
            return s.lower()
    return None


def _deployment_dead(dseq: str) -> bool:
    """True if the deployment/lease is in a terminal state it can't recover from,
    so readiness waits fail FAST instead of burning the whole cap."""
    return _dead_state(dseq) is not None


def _wait_ready(dseq: str, cap_s: float = READY_CAP_S, diag: dict | None = None) -> bool:
    """Wait until the container is genuinely SERVING, not just lease-'ready'.

    The lease flips to ``status: ready`` the moment the provider accepts a
    manifest — well before the pod is scheduled and serving — so gating on that
    is exactly why a healthy provider looked "ready" and then failed every
    downstream check. We instead gate on the service's reported availability
    (ready_replicas/available >= 1), with a lease-shell exec as a fallback for
    providers that don't populate availability (a working exec proves the
    container is running). Fails FAST on a terminal deployment state, and waits
    up to a generous cap otherwise so readiness LAG isn't misread as failure.
    """
    start = time.monotonic()
    time.sleep(6)
    last_exec_probe = 0.0
    while time.monotonic() - start < cap_s:
        elapsed = int(time.monotonic() - start)
        dead_state = _dead_state(dseq)
        if dead_state is not None:
            print(
                f"  {RED}deployment reached terminal state '{dead_state}'{RESET} after {elapsed}s"
            )
            if diag is not None:
                diag["terminal_state"] = dead_state
                # LEASE-DOWN only for a provider-fulfillment terminal state (the lease
                # failed/closed after the bid was accepted). Escrow exhaustion
                # (insufficient_funds) is our funding issue → a plain readiness FAIL.
                if dead_state in _LEASE_DOWN_STATES:
                    diag["fail_kind"] = "lease-down"
            return False
        avail = _service_availability(dseq)
        if avail is not None and avail[0] >= 1:
            print(f"  service available ({avail[0]}/{avail[1]}) after {elapsed}s")
            return True
        # Fallback ~every 30s: a working lease-shell exec proves the container is
        # up even when the provider never populates availability. _exec_works is
        # exception-isolated, so a subprocess timeout/OSError from the probe can't
        # escape and abort the readiness wait (and its slow-vs-stuck diagnostics).
        now = time.monotonic()
        if now - last_exec_probe >= 30:
            last_exec_probe = now
            if _exec_works(dseq):
                print(
                    f"  container exec-ready after {int(now - start)}s (availability unreported)"
                )
                return True
        time.sleep(6)
    print(f"  {RED}not serving within {int(cap_s)}s{RESET}")
    # Cap exceeded → FAIL. Classify slow-vs-stuck WITHOUT changing that verdict.
    _record_ready_timeout(dseq, diag, cap_s)
    return False


def _wait_exec_ready(dseq: str, attempts: int = 12, interval: int = 8) -> bool:
    """Poll until an exec both succeeds AND returns its output.

    Two warm-up effects to clear before the matrix is meaningful: (1) a lease
    reports ready before its container has finished starting, so an early exec
    fails outright; (2) even once exec succeeds, the very first command against a
    freshly-started container can come back rc=0 with EMPTY stdout (the exit-code
    frame arriving ahead of the stdout frame). Verifying a round-tripped marker
    clears both, so a healthy provider is never misreported as broken.
    """
    marker = "exec-ready-probe"
    for _ in range(attempts):
        r = _run(
            f"uv run just-akash exec 'echo {marker}' --dseq {q(dseq)} --transport lease-shell",
            timeout=30,
        )
        if r.returncode == 0 and marker in (r.stdout or ""):
            return True
        time.sleep(interval)
    return False


def _ssh_info(dseq: str) -> tuple[str, int] | None:
    """(host, port) for the forwarded SSH port, or None if the provider isn't
    forwarding port 22 yet / at all."""
    data = _status_json(dseq)
    host, port = data.get("ssh_host"), data.get("ssh_port")
    if host and port:
        return host, int(port)
    return None


def _ingress_uri(dseq: str) -> str | None:
    """The provider-assigned ingress hostname for the exposed HTTP service."""
    try:
        dep = _api().get_deployment(dseq)
    except Exception:  # noqa: BLE001 — resolution failure just means "no ingress yet"
        return None
    for lease in dep.get("leases") or []:
        if not isinstance(lease, dict):
            continue
        # Guard the status hop: a non-dict `status` (string/list from a malformed
        # or partial provider response) would make `.get("services")` raise.
        status = lease.get("status")
        services = status.get("services") if isinstance(status, dict) else None
        for svc in services.values() if isinstance(services, dict) else []:
            uris = svc.get("uris") if isinstance(svc, dict) else None
            if isinstance(uris, list) and uris:
                return uris[0]
    return None


def _fetch(uri: str, timeout: int = 10) -> str:
    # `uri` is the provider-assigned ingress hostname from lease status; require it to
    # be a bare host[:port] so a surprising value can't smuggle a scheme or path into
    # the fetched URL. The scheme is hard-coded http://, so file:// is unreachable.
    if not re.fullmatch(r"[A-Za-z0-9.\-:]+", uri):
        raise ValueError(f"unexpected ingress host: {uri!r}")
    with urllib.request.urlopen(f"http://{uri}/", timeout=timeout) as r:  # plain-http ingress
        return r.read().decode("utf-8", "replace")


def _wait_ssh_ready(dseq: str, key: str, attempts: int = 15, interval: int = 8) -> bool:
    """Poll SSH exec until it works — sshd comes up only after the boot-time
    `apk add openssh`, well after lease-shell is ready.

    The forwarded SSH port itself can also lag in lease status, so re-check for it
    on every iteration rather than bailing out if it's absent on the first poll.
    """
    for _ in range(attempts):
        if _ssh_info(dseq) is not None:
            r = _run(
                f"uv run just-akash exec 'echo ssh-ready' --dseq {q(dseq)} "
                f"--transport ssh --key {q(key)}",
                timeout=30,
            )
            if r.returncode == 0 and "ssh-ready" in (r.stdout or ""):
                return True
        time.sleep(interval)
    return False


# ── per-feature checks (each returns bool, never raises here) ─────────


def _check_status(dseq: str) -> bool:
    return bool(_status_json(dseq).get("provider"))


# exec frame-shape captured from the transport's FRAME-TRACE stderr line, keyed by
# dseq, for telemetry enrichment (issue #3438: quantify a true DROP vs a drained
# reorder over time without mining CI logs).
_EXEC_FRAME_SHAPES: dict[str, str] = {}

# dseq -> the null/missing-exit_code shapes the issue-#85 shim reported during this
# run, across every exec (not just the `exec` check). Absent means the shim never
# fired, which is the outcome the removal condition is waiting to see hold.
_EXEC_EXIT_CODE_SHAPES: dict[str, set[str]] = {}


def _frame_trace_line(stderr: str) -> str | None:
    """The transport's one-line FRAME-TRACE from captured stderr, if present.

    Anchor on the stable ``[lease-shell] FRAME-TRACE`` prefix so unrelated stderr that
    merely contains the token (a wrapper, a dependency, a future command) can't be
    mistaken for the trace and fed to the shape parser.
    """
    for line in (stderr or "").splitlines():
        if line.lstrip().startswith("[lease-shell] FRAME-TRACE"):
            return line.strip()
    return None


def _exit_code_shapes(stderr: str) -> set[str]:
    """Null/missing-``exit_code`` shapes reported by the issue-#85 compatibility
    shim, parsed from a command's captured stderr.

    Reads the structured ``akash-diag`` event (``EXEC_EXIT_CODE_UNKNOWN``) rather
    than the human warning beside it: the shim's removal condition is a COUNT per
    provider over time, and a prose log line cannot be counted. Unparseable or
    unrelated stderr yields an empty set — this must never raise into a check.
    """
    shapes: set[str] = set()
    for line in (stderr or "").splitlines():
        line = line.strip()
        if not line.startswith("{") or Code.EXEC_EXIT_CODE_UNKNOWN not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("code") != Code.EXEC_EXIT_CODE_UNKNOWN:
            continue
        # stderr is untrusted input: a line can carry the right code with a
        # scalar/list context, and `.get` on that would raise straight through
        # the no-raise contract and fail the smoke check this only rides along on.
        context = event.get("context")
        if not isinstance(context, dict):
            continue
        shape = context.get("shape")
        if isinstance(shape, str) and shape:
            shapes.add(shape)
    return shapes


def _note_exit_code_shapes(dseq: str, stderr: str) -> None:
    """Accumulate shim occurrences seen on ANY exec for ``dseq``.

    Every exec the smoke runs is a survey sample, not just the ``exec`` check —
    under-counting here would let the 30-day clean streak run out on partial
    evidence and retire the shim early.
    """
    shapes = _exit_code_shapes(stderr)
    if shapes:
        _EXEC_EXIT_CODE_SHAPES.setdefault(dseq, set()).update(shapes)


def _frame_shape(trace_line: str | None) -> str | None:
    """Ordered frame shape (e.g. 'stdout,result') parsed from a FRAME-TRACE line."""
    if not trace_line:
        return None
    m = re.search(r"shape=\[([^\]]*)\]", trace_line)
    return m.group(1) if m else None


def _check_exec(dseq: str) -> bool:
    token = f"smoke-{dseq[-6:]}-ok"
    # Default the shape to "unavailable" BEFORE running the subprocess, so an exec that
    # dies via exception (e.g. subprocess.TimeoutExpired, which run_check catches and
    # still records as an exec FAIL) keeps a frame_shape on its telemetry record. It is
    # overwritten below with the real shape when a FRAME-TRACE is parsed. Recording a
    # shape for EVERY exec that ran is the point (issue #3438 quantification); an absent
    # field then unambiguously means the exec never ran (no-bid / never-ready).
    _EXEC_FRAME_SHAPES[dseq] = "unavailable"
    # JUST_AKASH_TRACE_FRAMES makes the transport emit a FRAME-TRACE line to stderr.
    # Prefixing it into the shell command scopes it to THIS subprocess only -- an
    # inherited env var would leak the trace into every other check that runs exec.
    # On an empty-stdout FAIL the trace is the frame-level evidence for issue #3438
    # (a genuine DROP shows shape=[result] with no stdout frame).
    r = _run(
        f"JUST_AKASH_TRACE_FRAMES=1 uv run just-akash exec 'echo {token}' "
        f"--dseq {q(dseq)} --transport lease-shell",
        timeout=45,
    )
    ok = r.returncode == 0 and token in (r.stdout or "")
    _note_exit_code_shapes(dseq, r.stderr or "")
    trace = _frame_trace_line(r.stderr or "")
    shape = _frame_shape(trace)
    if shape:
        _EXEC_FRAME_SHAPES[dseq] = shape
    if not ok and trace:
        # Surface the frame evidence inline so it lands in the CI log at the failure
        # point, next to the kube-event/log diagnostics captured by run_check.
        print(f"    {YELLOW}exec {trace}{RESET}")
    return ok


def _inject_and_read(dseq: str, transport: str, key: str = "") -> bool:
    """Inject an env file over ``transport`` then read it back via exec."""
    remote = f"/tmp/smoke-inject-{transport}.env"  # path is inside the probe container
    keyarg = f"--key {q(key)}" if key else ""
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write("SMOKE_SECRET=injected_ok\nSECOND_VAR=hello_world\n")  # pragma: allowlist secret
        env_file = f.name
    try:
        inj = _run(
            f"uv run just-akash inject --dseq {q(dseq)} --env-file {q(env_file)} "
            f"--remote-path {q(remote)} --transport {transport} {keyarg}",
            timeout=60,
        )
        if inj.returncode != 0:
            return False
        # Read the file back. The readback is a lease-shell exec, so it can hit the
        # cold-stdout race (rc=0 + EMPTY stdout) even though the file was written
        # fine — a transport flake, not a failed inject. Retry ONLY that signature;
        # a nonzero rc or non-empty-but-wrong content is a real failure and returns
        # immediately, so a genuine inject regression is never masked.
        for attempt in range(_INJECT_READBACK_ATTEMPTS):
            back = _run(
                f"uv run just-akash exec 'cat {q(remote)}' --dseq {q(dseq)} "
                f"--transport {transport} {keyarg}",
                timeout=45,
            )
            out = back.stdout or ""
            _note_exit_code_shapes(dseq, back.stderr or "")
            if back.returncode == 0 and "injected_ok" in out:
                return True
            # Only rc=0-with-empty-stdout (the race) is retryable — stop otherwise.
            if not (back.returncode == 0 and not out.strip()):
                return False
            if attempt + 1 < _INJECT_READBACK_ATTEMPTS:
                print(
                    f"    {YELLOW}inject: empty readback (cold-stdout race) — "
                    f"retry {attempt + 2}/{_INJECT_READBACK_ATTEMPTS}{RESET}"
                )
                time.sleep(_INJECT_READBACK_BACKOFF_S)
        return False
    finally:
        os.unlink(env_file)


def _check_inject(dseq: str) -> bool:
    return _inject_and_read(dseq, "lease-shell")


def _check_stream(dseq: str, command: str) -> bool:
    """logs/events must return within the bounded --duration window (no hang)
    AND produce readable output.

    Exit-0 alone is not enough: providers that stream each frame as a JSON
    log/event object (rather than base64) used to exit cleanly while every line
    was silently discarded as "undecodable", so a "PASS" masked a blind stream.
    Require at least one non-empty output line — the CLI writes only the streamed
    lines to stdout (errors go to stderr), so any content means real output.

    logs/events are lease-shell-only and take no --transport flag (passing one is
    an argparse error), so the command must not include it.
    """
    start = time.monotonic()
    r = _run(f"uv run just-akash {command} --dseq {q(dseq)} --duration 8", timeout=40)
    elapsed = time.monotonic() - start
    got_output = any(line.strip() for line in (r.stdout or "").splitlines())
    return r.returncode == 0 and elapsed < 35 and got_output


def _check_ssh(dseq: str, key: str) -> bool:
    """exec + inject over the SSH transport (provider port-forwarding)."""
    r = _run(
        f"uv run just-akash exec 'echo SSH_OK' --dseq {q(dseq)} --transport ssh --key {q(key)}",
        timeout=45,
    )
    if not (r.returncode == 0 and "SSH_OK" in (r.stdout or "")):
        return False
    return _inject_and_read(dseq, "ssh", key)


def _check_connect(dseq: str, key: str) -> bool:
    """Interactive session over SSH, driven by piped stdin.

    Lease-shell connect deliberately refuses a non-TTY stdin, so it can't be
    exercised headlessly; SSH connect accepts piped input and is what this covers.
    """
    marker = f"CONNECT_{dseq[-6:]}"
    try:
        # List form (no shell) — the connect command needs piped stdin, which _run
        # doesn't provide. Args are internal (a numeric dseq and a temp key path).
        r = subprocess.run(
            [
                "uv",
                "run",
                "just-akash",
                "connect",
                "--dseq",
                dseq,
                "--transport",
                "ssh",
                "--key",
                key,
            ],
            input=f"echo {marker}\nexit\n",
            capture_output=True,
            text=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0 and marker in (r.stdout or "")


def _check_ingress(
    dseq: str, uri: str, cap_s: float = INGRESS_CAP_S, diag: dict | None = None
) -> bool:
    """The provider routes the exposed HTTP port to the container's httpd.

    Polls the ingress for the marker up to a generous cap. Route propagation
    (the ingress controller registering the container as a healthy backend) lags
    behind the service becoming available, and returns 404/503 in the meantime —
    a short fixed budget here is a top cause of false ingress FAILs.
    """
    start = time.monotonic()
    last = ""
    while time.monotonic() - start < cap_s:
        try:
            body = _fetch(uri)
            if INGRESS_BASELINE in body:
                print(f"  ingress reachable after {int(time.monotonic() - start)}s")
                return True
            last = body[:60].replace("\n", " ")
        except (urllib.error.URLError, OSError, ValueError) as e:
            # ValueError covers a malformed provider-reported URI — record it and keep
            # polling; the timeout path classifies it, rather than aborting the check.
            last = str(e)[:60]
        time.sleep(6)
    print(f"  {RED}ingress not reachable within {int(cap_s)}s{RESET} (last: {last!r})")
    # Cap exceeded → FAIL. Classify slow-vs-stuck WITHOUT changing that verdict.
    _record_ingress_timeout(dseq, uri, last, diag, cap_s)
    return False


def _classify_served(body: str | None, new_token: str) -> str:
    """What the ingress was serving at the timeout instant: 'new' (the update is
    live — a race we lost), 'old' (serving prior content, cutover pending), 'none'
    (empty body) or 'unreachable' (fetch raised)."""
    if body is None:
        return "unreachable"
    if new_token in body:
        return "new"
    if not body.strip():
        return "none"
    return "old"


def _probe_in_pod_marker(dseq: str, expected_token: str) -> str:
    """Best-effort: exec into the container and read the SMOKE_MARKER env the update
    set. This is the ONE signal that splits the two look-alike update-timeout causes:
    'new' => the new revision reached the pod, so a still-stale ingress is a routing
    lag; 'old' => the update never propagated to the container; 'unreachable' => exec
    failed (a flaky exec must never be mistaken for a real signal). Diagnostic-only."""
    try:
        r = _run(
            f"uv run just-akash exec 'printenv SMOKE_MARKER' "
            f"--dseq {q(dseq)} --transport lease-shell",
            timeout=45,
        )
    except Exception:  # noqa: BLE001 — a diagnostic probe must never raise
        return "unreachable"
    if r.returncode != 0:
        return "unreachable"
    out = (r.stdout or "").strip()
    if not out:
        # rc=0 with empty stdout is the exec cold-stdout race (fixed in v1.17.0, but
        # still possible on an un-upgraded path) — a flaky read, NOT proof the env is
        # old. Never let it masquerade as a real 'old'/stale-update signal.
        return "unreachable"
    return "new" if expected_token in out else "old"


def _observe_after_cap(probe, window_s: float = POST_CAP_OBSERVE_S) -> tuple[str, int | None]:
    """After a check's cap has expired (verdict is already FAIL), keep polling for up
    to ``window_s`` to classify SLOW vs STUCK. Returns ('arrived', <seconds into the
    post-cap window>) the moment ``probe()`` is truthy, else ('never', None). Bounded
    and diagnostic-only — it never changes the caller's verdict, and a raising probe
    is swallowed so the classification itself can never fail the run."""
    if window_s <= 0:
        return ("never", None)
    start = time.monotonic()
    while time.monotonic() - start < window_s:
        try:
            if probe():
                return ("arrived", int(time.monotonic() - start))
        except Exception:  # noqa: BLE001 — a diagnostic probe must never raise
            pass
        # Recompute the remaining time AFTER probe() (which may itself be slow) and
        # clamp to [0, 6]: never overshoot a short window by a full poll interval,
        # and never pass a negative duration to sleep() if the probe outlasts it.
        time.sleep(max(0.0, min(6.0, window_s - (time.monotonic() - start))))
    return ("never", None)


def _record_update_timeout(
    dseq: str, uri: str, token: str, last_body: str | None, diag: dict | None
) -> None:
    """Classify + report an update-cutover timeout (verdict already FAIL). Populates
    ``diag`` for telemetry and prints one self-explaining line. Read it as:
    eventual=arrived => SLOW (cap too tight, widen); eventual=never + in_pod_marker=new
    => ingress routing STUCK; eventual=never + in_pod_marker=old => update never reached
    the pod (STUCK, deeper). Never flips the verdict."""
    served = _classify_served(last_body, token)
    try:
        avail = _service_availability(dseq)
    except Exception:  # noqa: BLE001 — one failing probe must not abort the classification
        avail = None
    service = f"{avail[0]}/{avail[1]}" if avail else None
    in_pod = _probe_in_pod_marker(dseq, token)
    eventual, after_s = _observe_after_cap(lambda: token in _fetch(uri))
    if diag is not None:
        diag.update(
            {
                "fail_cap_s": int(INGRESS_CAP_S),
                "body_at_timeout": served,
                "service_at_timeout": service,
                "in_pod_marker": in_pod,
                "eventual": eventual,
                "eventual_after_s": after_s,
            }
        )
    print(
        f"  update slow-vs-stuck: served={served} service={service or 'unknown'} "
        f"in_pod_marker={in_pod} eventual={eventual}{_post_cap_tail(after_s)}"
    )


def _post_cap_tail(after_s: int | None) -> str:
    """One-line suffix noting when (if) the resource arrived in the post-cap window."""
    if after_s is None:
        return ""
    return f" (arrived {after_s}s into the {int(POST_CAP_OBSERVE_S)}s post-cap window)"


def _availability_ready(dseq: str) -> bool:
    """Exception-safe: is the lease service reporting >=1 ready replica right now?"""
    try:
        a = _service_availability(dseq)
    except Exception:  # noqa: BLE001 — a diagnostic probe must never raise
        return False
    return a is not None and a[0] >= 1


def _exec_works(dseq: str) -> bool:
    """Exception-safe one-shot: does a lease-shell exec round-trip a marker? Treats an
    rc=0-but-empty-stdout (the cold-stdout race) as NOT working, so a flaky read is
    never mistaken for a live container."""
    try:
        r = _run(
            f"uv run just-akash exec 'echo ready' --dseq {q(dseq)} --transport lease-shell",
            timeout=25,
        )
    except Exception:  # noqa: BLE001 — a diagnostic probe must never raise
        return False
    return r.returncode == 0 and "ready" in (r.stdout or "").strip()


def _record_ready_timeout(dseq: str, diag: dict | None, cap_s: float = READY_CAP_S) -> None:
    """Classify a readiness timeout (verdict already FAIL): SLOW (the container becomes
    ready within the post-cap window → the cap was too tight) vs STUCK (never → a
    genuine defect: a dead lease, an unschedulable pod, or a container that never
    serves). Every probe is exception-isolated; the verdict never changes."""
    try:
        dead = _deployment_dead(dseq)
    except Exception:  # noqa: BLE001 — one failing probe must not abort the classification
        dead = False
    avail = None
    try:
        avail = _service_availability(dseq)
    except Exception:  # noqa: BLE001
        avail = None
    service = f"{avail[0]}/{avail[1]}" if avail else None
    exec_state = "ok" if _exec_works(dseq) else "unreachable"
    # Cheap eventual-probe: availability only (an exec every 6s would overlap its own
    # 25s timeout). The one-shot exec above already snapshots the exec path.
    eventual, after_s = _observe_after_cap(lambda: _availability_ready(dseq))
    if eventual == "never" and exec_state == "ok":
        # Availability never populated but exec works => the container IS up; the
        # readiness signal, not the container, was the laggard. Record it as arrived.
        eventual, after_s = "arrived", 0
    if diag is not None:
        diag.update(
            {
                "fail_cap_s": int(cap_s),
                "service_at_timeout": service,
                "dead_at_timeout": dead,
                "exec_at_timeout": exec_state,
                "eventual": eventual,
                "eventual_after_s": after_s,
            }
        )
    print(
        f"  ready slow-vs-stuck: service={service or 'unknown'} dead={dead} "
        f"exec={exec_state} eventual={eventual}{_post_cap_tail(after_s)}"
    )


def _record_ingress_timeout(
    dseq: str, uri: str, last: str, diag: dict | None, cap_s: float = INGRESS_CAP_S
) -> None:
    """Classify an initial-ingress timeout (verdict already FAIL): SLOW (the marker
    routes within the post-cap window → route propagation was just slow) vs STUCK
    (never). Records the lease service state + last error so a genuine backend failure
    is distinguishable from pure routing lag. Never flips the verdict."""
    avail = None
    try:
        avail = _service_availability(dseq)
    except Exception:  # noqa: BLE001
        avail = None
    service = f"{avail[0]}/{avail[1]}" if avail else None
    eventual, after_s = _observe_after_cap(lambda: INGRESS_BASELINE in _fetch(uri))
    if diag is not None:
        diag.update(
            {
                "fail_cap_s": int(cap_s),
                "service_at_timeout": service,
                "last_at_timeout": last or None,
                "eventual": eventual,
                "eventual_after_s": after_s,
            }
        )
    print(
        f"  ingress slow-vs-stuck: service={service or 'unknown'} "
        f"eventual={eventual}{_post_cap_tail(after_s)}"
    )


def _check_update(dseq: str, sdl_path: str, uri: str, diag: dict | None = None) -> bool:
    """In-place manifest update: change the served marker and confirm the new
    revision goes live at the same ingress (lease preserved).

    On a timeout the verdict stays FAIL, but before returning we classify WHY —
    slow-vs-stuck, and routing-lag-vs-stale-update — so the failure self-explains in
    the run log + telemetry and any later cap change is data-driven, not a guess. The
    diagnostics never flip the verdict: a genuine provider defect must stay visible."""
    token = f"probe-updated-{dseq[-6:]}"
    r = _run(
        f"uv run just-akash update --dseq {q(dseq)} --sdl {q(sdl_path)} "
        f"--env SMOKE_MARKER={token}",
        timeout=120,
    )
    if r.returncode != 0:
        err = " ".join((r.stderr or r.stdout or "").split())
        print(f"  update command failed (rc={r.returncode}): {err[:200]}")
        if diag is not None:
            diag["fail_mode"] = "update_command"
        return False
    # The container restarts (and reinstalls its packages), so give it room —
    # same generous cap as the initial ingress check — before the new marker
    # appears at the ingress.
    start = time.monotonic()
    last: str | None = None
    while time.monotonic() - start < INGRESS_CAP_S:
        try:
            last = _fetch(uri)
            if token in last:
                print(f"  update live at ingress after {int(time.monotonic() - start)}s")
                return True
        except (urllib.error.URLError, OSError, ValueError):
            # ValueError covers a malformed provider-reported URI — treat as a
            # transient unreachable and keep polling; the timeout path classifies it.
            last = None
        time.sleep(6)
    # Cap exceeded → FAIL. Classify slow-vs-stuck WITHOUT changing that verdict.
    _record_update_timeout(dseq, uri, token, last, diag)
    return False


# ── orchestration ────────────────────────────────────────────────────


def _benchmark_provider(dseq: str, provider: str) -> dict | None:
    """Grade the LIVE lease's hardware (quality), on top of the feature matrix
    (responsiveness). Runs the benchmark CLI against the probe and returns its
    parsed metrics as one telemetry record, or None.

    Best-effort and NON-GATING by construction: it runs only AFTER the feature
    matrix is complete and recorded, so benchmark load can never mask a feature
    result (the #61 concern), and any failure here is swallowed — the smoke's
    pass/fail is never affected by the grade. Enabled only when SMOKE_BENCHMARK_FILE
    is set, so the default daily run is unchanged unless a benchmark sink is wired.
    """
    if not os.environ.get("SMOKE_BENCHMARK_FILE", "").strip():
        return None
    try:
        print("  benchmark: grading hardware (non-gating)...")
        r = _run(f"uv run just-akash benchmark --dseq {q(dseq)} --json", timeout=150)
        line = next(
            (ln for ln in (r.stdout or "").splitlines() if ln.strip().startswith("{")), None
        )
        if not line:
            print(f"  {YELLOW}benchmark: no metrics returned (non-gating){RESET}")
            return None
        rec = json.loads(line)
        if not isinstance(rec, dict):
            return None
        # Trust our provider/dseq over anything the probe emitted (same rule as the
        # benchmark CLI's build_json_record).
        rec["provider"] = provider
        rec["dseq"] = dseq
        return rec
    except Exception as e:  # noqa: BLE001 — grading must never break the smoke
        print(f"  {YELLOW}benchmark: skipped ({type(e).__name__}) — non-gating{RESET}")
        return None


def smoke_provider(
    provider: str,
    sdl_path: str,
    key: str,
    records: list | None = None,
    bench_records: list | None = None,
) -> dict:
    """Run the full feature matrix against one provider.

    The ``finally`` guarantees the deployment is destroyed and (when ``records``
    is provided) appends one telemetry record per feature — outcome + latency.
    A hard error in the deploy/readiness helpers (e.g. a subprocess timeout) is
    not swallowed here — it propagates to ``main()``, which records the provider
    as all-FAIL and moves on, so one provider's failure never aborts the run.

    When ``bench_records`` is provided AND the lease came up healthy, a hardware
    benchmark runs on the live lease AFTER the matrix (quality, not just pass/fail)
    and one record is appended — see :func:`_benchmark_provider`.
    """
    results = dict.fromkeys(FEATURES, "-")
    latencies: dict[str, float] = {}
    # Per-feature slow-vs-stuck evidence captured on a timeout (see _check_update).
    # Defined before the try so the finally can always attach it to telemetry.
    diagnostics: dict[str, dict] = {}
    dseq_ref: dict = {"dseq": None}
    install_signal_cleanup(dseq_ref)
    _hdr(f"provider {provider}")
    try:
        # Pre-deploy room check: don't spend a deploy on an offline/full provider.
        room_ok, room_reason = _provider_room(provider)
        if not room_ok:
            results["deploy"] = "NO-ROOM"
            print(f"  {YELLOW}NO-ROOM{RESET}: {room_reason} — skipping (not a failure)")
            return results

        _t0 = time.monotonic()
        dseq, note = _deploy(sdl_path, provider, dseq_ref)
        latencies["deploy"] = round((time.monotonic() - _t0) * 1000)
        if not dseq:
            # NO-BID (no capacity/interest) and NO-CREDIT (account-wide, nothing
            # created on-chain) are skips, not provider failures.
            results["deploy"] = {
                "no-bid": "NO-BID",
                "no-credit": "NO-CREDIT",
            }.get(note, "FAIL")
            colour = RED if results["deploy"] == "FAIL" else YELLOW
            print(f"  {colour}{note}{RESET} — cannot test remaining features")
            return results
        results["deploy"] = "PASS"
        print(f"  {GREEN}deployed{RESET} DSEQ={dseq}, waiting for lease...")

        # Capture diagnostics on the FIRST failure only (a readiness failure
        # cascades to every feature; one events/logs dump is enough to root-cause
        # it, and avoids 10x captures).
        diag_captured = False

        def _diag_once(reason: str) -> None:
            nonlocal diag_captured
            if not diag_captured:
                diag_captured = True
                _capture_diagnostics(dseq, reason)

        _t0 = time.monotonic()
        ready = _wait_ready(dseq, diag=diagnostics.setdefault("ready", {}))
        latencies["ready"] = round((time.monotonic() - _t0) * 1000)
        if not ready:
            # A lease that never serves is a real failure, not a pass: every untested
            # feature reads a failing outcome so the provider counts against the run.
            # Distinguish LEASE-DOWN (provider accepted the bid, the lease then died
            # on-chain — a fulfillment failure) from a plain readiness FAIL (container
            # slow/never-served). Both trip the run; LEASE-DOWN is labelled distinctly
            # so it isn't mistaken for a broken-feature regression.
            lease_down = diagnostics.get("ready", {}).get("fail_kind") == "lease-down"
            outcome = LEASE_DOWN if lease_down else "FAIL"
            results["ready"] = outcome
            label = (
                "lease went down (terminal on-chain state)"
                if lease_down
                else "lease never became ready"
            )
            print(f"  {RED}{label}{RESET} — marking untested features {outcome}")
            _diag_once(label)
            for feat in FEATURES:
                if results[feat] == "-":
                    results[feat] = outcome
            return results
        results["ready"] = "PASS"
        if not _wait_exec_ready(dseq):
            print(f"  {YELLOW}container slow to accept exec{RESET} — checks may reflect that")

        def run_check(name: str, fn) -> None:
            _t = time.monotonic()
            try:
                ok = fn()
            except Exception as e:  # noqa: BLE001 — a broken feature must not abort the run
                ok = False
                print(f"  {name}: raised {type(e).__name__}: {e}")
            latencies[name] = round((time.monotonic() - _t) * 1000)
            results[name] = "PASS" if ok else "FAIL"
            print(f"  {GREEN if ok else RED}{name}: {results[name]}{RESET}")
            if not ok:
                _diag_once(f"{name} check failed")

        # lease-shell features (container is exec-ready)
        run_check("status", lambda: _check_status(dseq))
        run_check("exec", lambda: _check_exec(dseq))
        run_check("inject", lambda: _check_inject(dseq))
        run_check("logs", lambda: _check_stream(dseq, "logs"))
        run_check("events", lambda: _check_stream(dseq, "events"))

        # SSH transport + connect (sshd starts only after the boot-time apk install)
        if _wait_ssh_ready(dseq, key):
            run_check("ssh", lambda: _check_ssh(dseq, key))
            run_check("connect", lambda: _check_connect(dseq, key))
        else:
            results["ssh"] = results["connect"] = "FAIL"
            print(f"  {RED}ssh: FAIL{RESET} (no forwarded SSH port / sshd never came up)")
            _diag_once("no forwarded SSH port / sshd never came up")

        # ingress + update (need the exposed HTTP endpoint serving)
        uri = _ingress_uri(dseq)
        if uri:
            run_check(
                "ingress",
                lambda: _check_ingress(dseq, uri, diag=diagnostics.setdefault("ingress", {})),
            )
            # update restarts the container, so it runs last, after every other check
            run_check(
                "update",
                lambda: _check_update(
                    dseq, sdl_path, uri, diag=diagnostics.setdefault("update", {})
                ),
            )
        else:
            results["ingress"] = results["update"] = "FAIL"
            print(f"  {RED}ingress: FAIL{RESET} (no ingress URI assigned)")
            _diag_once("no ingress URI assigned")

        # QUALITY grade on the live lease — AFTER the feature matrix above is fully
        # recorded, so the benchmark's load can never mask a feature result. Only on
        # a healthy lease (deploy + ready PASS): grading a dead one is pointless.
        if (
            bench_records is not None
            and results.get("deploy") == "PASS"
            and results.get("ready") == "PASS"
            and dseq_ref["dseq"]
        ):
            rec = _benchmark_provider(dseq_ref["dseq"], provider)
            if rec is not None:
                bench_records.append(rec)

        return results
    finally:
        # Capture the dseq before cleanup clears the ref, so telemetry keeps it.
        _dseq = dseq_ref["dseq"]
        if _dseq:
            print(f"  cleanup: destroying {_dseq}...")
            robust_destroy(_dseq)
            # Clear the ref so a later Ctrl-C's signal handler skips this already-
            # destroyed deployment instead of re-issuing destroy against it.
            dseq_ref["dseq"] = None
        # Emit telemetry even on an early return or a propagating error (the
        # finally runs in all cases), so a no-bid / never-ready / crashed provider
        # is still recorded with whatever was measured.
        if records is not None:
            records.extend(
                _provider_records(
                    provider,
                    _dseq,
                    results,
                    latencies,
                    diagnostics,
                    # pop (not get): telemetry emission is the sole consumer, so drop
                    # the entry here to keep the process-global cache from growing and
                    # to prevent any stale-state reuse if a dseq ever recurs.
                    frame_shape=_EXEC_FRAME_SHAPES.pop(_dseq, None),
                    exit_code_shapes=_EXEC_EXIT_CODE_SHAPES.pop(_dseq, None),
                )
            )


def _fmt_latency(ms: object) -> str:
    """A latency in ms as a compact cell suffix ('354ms', '1.6s'), or '' when the
    feature was never reached (skips carry no latency)."""
    # NaN/inf reach here from arbitrary JSON in the telemetry records, and both
    # slip past `ms < 0` (NaN compares False to everything) to render as "nans"
    # / "infs". A latency we cannot state is a blank cell, like any other.
    if isinstance(ms, bool) or not isinstance(ms, (int, float)) or not math.isfinite(ms) or ms < 0:
        return ""
    return f"{int(ms)}ms" if ms < 1000 else f"{ms / 1000:.1f}s"


def _latency_index(records: list | None) -> dict[tuple[str, str], object]:
    """``(provider, feature) -> latency_ms`` from the telemetry records.

    Read back from the records rather than threaded separately so the matrix and
    the telemetry can never disagree about how long something took — they are
    literally the same numbers.
    """
    index: dict[tuple[str, str], object] = {}
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        provider, feature = rec.get("provider"), rec.get("feature")
        if isinstance(provider, str) and isinstance(feature, str):
            index[(provider, feature)] = rec.get("latency_ms")
    return index


def _print_matrix(rows: dict, records: list | None = None) -> None:
    _hdr("SMOKE TEST MATRIX")
    latencies = _latency_index(records)
    # Truncate to the same 14 chars the accrued telemetry report uses, so a
    # provider reads identically in both tables. The full address is printed in
    # this run's per-provider header above, so nothing is lost.
    label = {p: p[:14] for p in rows}
    wp = max([len("provider")] + [len(v) for v in label.values()])

    def _cell(prov: str, feat: str) -> tuple[str, str]:
        """(text, color) for one cell — outcome plus how long it took."""
        v = rows.get(prov, {}).get(feat, "-")
        skips = ("-", "NO-BID", "NO-ROOM", "NO-CREDIT")
        color = GREEN if v == "PASS" else (YELLOW if v in skips else RED)
        # A FAILING feature keeps its latency: "failed after 45s" and "failed
        # instantly" are different problems, and the timing is the tell.
        t = _fmt_latency(latencies.get((prov, feat)))
        return (f"{v} {t}" if t else v), color

    # Size every column to its widest cell so the table stays aligned once the
    # timings vary in width (354ms vs 39.4s).
    widths = {f: max([len(f)] + [len(_cell(p, f)[0]) for p in rows] + [8]) for f in FEATURES}
    header = f"{'provider'.ljust(wp)}  " + " ".join(f.ljust(widths[f]) for f in FEATURES)
    print(header)
    print("-" * len(header))
    for prov in rows:
        cells = []
        for f in FEATURES:
            text, color = _cell(prov, f)
            cells.append(f"{color}{text.ljust(widths[f])}{RESET}")
        print(f"{label[prov].ljust(wp)}  " + " ".join(cells))


def _generate_keypair() -> str:
    """Create an ephemeral ed25519 keypair, export the public key via SSH_PUBKEY
    (which deploy substitutes into the SDL), and return the private key path."""
    key_dir = tempfile.mkdtemp(prefix="smoke-ssh-")
    key_path = os.path.join(key_dir, "id_ed25519")
    try:
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path, "-C", "smoke-probe"],
            check=True,
            capture_output=True,
        )
        with open(f"{key_path}.pub") as f:
            os.environ["SSH_PUBKEY"] = f.read().strip()
    except Exception:
        # Don't leave a half-generated (unencrypted) private key on disk if keygen
        # or the pubkey read fails partway.
        shutil.rmtree(key_dir, ignore_errors=True)
        raise
    return key_path


def main() -> int:
    ap = argparse.ArgumentParser(description="Provider capability smoke test.")
    ap.add_argument("--all", action="store_true", help="Test backup providers too")
    ap.add_argument(
        "--provider",
        action="append",
        dest="providers",
        help="Test only this provider (repeatable)",
    )
    ap.add_argument(
        "--no-sweep",
        action="store_true",
        help="Skip the startup sweep for probes leaked by a hard-killed prior run",
    )
    ap.add_argument(
        "--sweep-only",
        action="store_true",
        help="Only reap leaked probes (no deploy), then exit",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="With --sweep-only (or the default startup sweep): report leaked probes "
        "without destroying them",
    )
    ap.add_argument(
        "--min-age",
        type=float,
        default=MIN_ORPHAN_AGE_SECONDS,
        metavar="SECONDS",
        help="Only reap probes at least this old (default 3600s). Pass 0 for an "
        "end-of-job cleanup that reaps this run's own fresh leak -- safe only when "
        "no other run is concurrent (CI serializes runs).",
    )
    ap.add_argument(
        "--telemetry-file",
        metavar="PATH",
        default=os.environ.get("SMOKE_TELEMETRY_FILE"),
        help="Append one JSON line per (provider, feature) with outcome + latency "
        "(also settable via SMOKE_TELEMETRY_FILE). Foundation for percentile "
        "timeouts and latency-regression detection.",
    )
    args = ap.parse_args()

    if not os.environ.get("AKASH_API_KEY"):
        print("Error: AKASH_API_KEY not set.", file=sys.stderr)
        return 1

    if not math.isfinite(args.min_age) or args.min_age < 0:
        print("Error: --min-age must be a non-negative number of seconds.", file=sys.stderr)
        return 1

    # Sweep first (unless disabled): reap any probe a hard-killed earlier run
    # orphaned, before we deploy fresh ones. Self-healing across runs. The header
    # tracks dry-run so it can't read as "destruction happened" when it didn't.
    sweep_hdr = (
        "Scanning for probes leaked by a previous hard-killed run (dry-run)"
        if args.dry_run
        else "Reaping probes leaked by a previous hard-killed run"
    )
    if args.sweep_only:
        _hdr(sweep_hdr)
        sweep_orphan_probes(dry_run=args.dry_run, min_age_seconds=args.min_age)
        return 0

    # Sweep before resolving providers, not after: the sweep scans all
    # deployments and does not depend on the provider list, so it must run even
    # when no providers are configured -- otherwise a no-providers run would
    # return below without reaping, defeating the self-healing guarantee.
    if not args.no_sweep:
        _hdr(sweep_hdr)
        sweep_orphan_probes(dry_run=args.dry_run, min_age_seconds=args.min_age)

    if args.providers:
        providers = args.providers
    else:
        preferred, backup, _ = resolve_tiers()
        providers = preferred + backup if args.all else preferred
    if not providers:
        print("No providers to test (set AKASH_PROVIDERS or pass --provider).", file=sys.stderr)
        return 1

    print(f"Smoke-testing {len(providers)} provider(s): one throwaway lease each.")
    key_path = _generate_keypair()
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(PROBE_SDL)
        sdl_path = f.name

    # One timestamp for the whole run so every record shares a run key.
    run_ts = datetime.now(timezone.utc).isoformat()
    # Always collect records in memory (the gate reads their diag to demote a
    # quarantined provider's reliability failures); only WRITE them if asked.
    records: list = []
    # Hardware-quality records (separate schema from the per-feature latency rows):
    # one benchmark grade per healthy lease, written to SMOKE_BENCHMARK_FILE.
    bench_records: list = []
    quarantined = _quarantined_providers()

    rows: dict = {}
    credit_exhausted = False
    try:
        for provider in providers:
            try:
                rows[provider] = smoke_provider(
                    provider, sdl_path, key_path, records=records, bench_records=bench_records
                )
            except Exception as e:  # noqa: BLE001 — one provider's hard error must not abort the run
                print(f"  {RED}{provider} aborted: {type(e).__name__}: {e}{RESET}")
                rows[provider] = dict.fromkeys(FEATURES, "FAIL")
            # Insufficient credit is account-wide — every other provider would 402
            # too. Stop here and skip the run cleanly rather than churn 402s.
            if rows[provider].get("deploy") == "NO-CREDIT":
                credit_exhausted = True
                print(
                    f"  {YELLOW}insufficient Console credit — skipping remaining providers{RESET}"
                )
                break
    finally:
        os.unlink(sdl_path)
        # Remove the ephemeral keypair — the unencrypted private key must not be
        # left behind in the temp dir after the run.
        shutil.rmtree(os.path.dirname(key_path), ignore_errors=True)
        # Tag telemetry so the SLO report can track reliability: quarantined rows, and
        # a fleet-wide simultaneous lease death (the mass-lease-down manifest-bug tell).
        mass_ld = _mass_lease_down(rows)
        for rec in records:
            if rec.get("provider") in quarantined:
                rec["quarantined"] = True
            if mass_ld and rec.get("outcome") == LEASE_DOWN:
                rec["mass_lease_down"] = True
        if args.telemetry_file and records:
            _write_telemetry(args.telemetry_file, run_ts, _pkg_version(), records)
        # Hardware grades go to their own sink so the quality analyzer reads them
        # independently of the latency telemetry (they have different schemas).
        bench_file = os.environ.get("SMOKE_BENCHMARK_FILE", "").strip()
        if bench_file and bench_records:
            _write_telemetry(bench_file, run_ts, _pkg_version(), bench_records)

    _print_matrix(rows, records)
    print()

    # Insufficient credit is not a provider verdict at all — nothing was tested.
    # Exit clean (0) so the scheduled run is a no-op, not a red failure.
    if credit_exhausted:
        print(f"{YELLOW}SMOKE TEST SKIPPED{RESET}: insufficient Console credit to deploy probes.")
        return 0

    # The gate trips only on a TOOLING regression (a feature broken on a healthy lease)
    # or the mass-lease-down safety valve. LEASE-DOWN is fleet-wide provider infra and
    # is NON-GATING (a quarantined provider's proven update stall is demoted too) — but
    # stays visible below + in telemetry. NO-BID / NO-ROOM / NO-CREDIT are skips.
    failed = _gating_providers(rows, records, quarantined)
    mass_ld = _mass_lease_down(rows)
    # Self-document every demoted (non-gating) reliability failure so a red matrix row
    # that didn't fail the run is never a silent mystery.
    for p in sorted(rows):
        r = rows.get(p, {})
        gating = failed.get(p, [])
        demoted = [f for f in FEATURES if r.get(f) in _FAILING_OUTCOMES and f not in gating]
        if demoted:
            print(
                f"{YELLOW}[NON-GATING]{RESET} {p}: {', '.join(demoted)} — provider-"
                "reliability failure (lease-down / quarantined stall), tracked but NOT gating"
            )
    if failed:
        reason = (
            "fleet-wide simultaneous LEASE-DOWN — likely a just-akash manifest/deploy bug"
            if mass_ld
            else "broken features"
        )
        print(f"{RED}SMOKE TEST FAILED{RESET}: {len(failed)} provider(s) — {reason}:")
        for p, broken in failed.items():
            print(f"  {p}: {', '.join(broken)}")
        return 1
    print(f"{GREEN}SMOKE TEST PASSED{RESET}: all testable providers support every feature.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
