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
PROBE_SDL = f"""\
---
version: "2.0"
services:
  probe:
    image: alpine:3.20
    env:
      - SSH_PUBKEY_B64=PLACEHOLDER_SSH_PUBKEY_B64
      - SMOKE_MARKER={INGRESS_BASELINE}
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
        sleep infinity
profiles:
  compute:
    probe:
      resources:
        cpu: {{ units: 1 }}
        memory: {{ size: 1Gi }}
        storage: [{{ size: 5Gi }}]
  placement:
    akash:
      pricing:
        probe: {{ denom: uact, amount: 10000 }}
deployment:
  probe:
    akash: {{ profile: probe, count: 1 }}
"""

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


def _pkg_version() -> str:
    try:
        from importlib.metadata import version

        return version("just-akash")
    except Exception:  # noqa: BLE001 — telemetry must never break the run
        return "unknown"


def _provider_records(
    provider: str, dseq: str | None, results: dict, latencies: dict
) -> list[dict]:
    """One telemetry record per feature: outcome + how long it took (ms).

    latency_ms is None for a feature that was never reached (e.g. everything
    after a no-bid). Pass/fail is the lagging binary; latency is the leading
    signal that lets us later set percentile timeouts and spot regressions.
    """
    return [
        {
            "provider": provider,
            "feature": feat,
            "outcome": results.get(feat, "-"),
            "latency_ms": latencies.get(feat),
            "dseq": dseq,
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


def _capture_diagnostics(dseq: str, reason: str) -> None:
    """On a failure, dump the provider's lease status + kube events + container
    logs, so an INTERMITTENT problem (e.g. an occasional 'lease never ready')
    self-documents in the run log instead of needing a live catch. The kube
    events are the payoff — they say WHY a pod didn't come up (FailedScheduling,
    Insufficient cpu/memory, ImagePullBackOff, OOMKilled, …). Best-effort: never
    raises, and bounded by each stream's --duration."""
    print(f"  {YELLOW}── diagnostics: {reason} (dseq {dseq}) ──{RESET}")
    try:
        st = _status_json(dseq)
        avail = _service_availability(dseq)
        state = st.get("status") if isinstance(st, dict) else st
        print(f"    lease status={state} availability={avail}")
    except Exception as e:  # noqa: BLE001 — diagnostics must never break the run
        print(f"    status capture failed: {type(e).__name__}: {e}")
    for kind, dur in (("events", 12), ("logs", 8)):
        try:
            r = _run(
                f"uv run just-akash {kind} --dseq {q(dseq)} --duration {dur}", timeout=dur + 25
            )
            lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
            print(f"    --- {kind} ({len(lines)} line(s)) ---")
            for ln in lines[:15]:
                print(f"      {ln}")
            if not lines:
                print(f"      (no {kind} returned — provider unreachable on this path)")
        except Exception as e:  # noqa: BLE001
            print(f"    {kind} capture failed: {type(e).__name__}: {e}")


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


def _deploy(sdl_path: str, provider: str, dseq_ref: dict) -> tuple[str | None, str]:
    """Deploy the probe pinned to ``provider``. Returns (dseq, note).

    dseq is None when the provider did not bid (note == "no-bid") or the deploy
    failed (note == "deploy-failed"). Backups are disabled so the lease can only
    land on the target provider. SSH_PUBKEY (set by main) is substituted into the
    SDL's PLACEHOLDER so sshd trusts our ephemeral key.
    """
    r = _run(
        f"uv run just-akash deploy --sdl {q(sdl_path)} "
        f"--provider {q(provider)} --backup-provider '' "
        f"--bid-wait 120 --bid-wait-retry 60",
        timeout=420,
    )
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"DSEQ[:=]\s*(\d+)", out)
    if m:
        dseq_ref["dseq"] = m.group(1)
        return m.group(1), "ok"
    # Insufficient Console credit is account-wide, not a provider fault: the
    # deployment create returns HTTP 402 and NOTHING is created on-chain. Surface
    # it as its own note so the run skips cleanly instead of scoring the provider
    # FAIL. (This is the authoritative credit check — the Console API exposes no
    # balance endpoint, and a 402 probe commits no resources.)
    if re.search(r"\(402\)|PaymentRequired|[Ii]nsufficient balance", out):
        return None, "no-credit"
    if re.search(r"NO BID|no bid|NONE from our providers|foreign bids", out):
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


def _deployment_dead(dseq: str) -> bool:
    """True only if the deployment/lease is in a terminal state it can't recover
    from (closed, or escrow exhausted) — so readiness waits fail FAST instead of
    burning the whole cap. A transient read error is NOT treated as dead."""
    try:
        dep = _api().get_deployment(dseq)
    except Exception:  # noqa: BLE001
        return False
    states: list = []
    d = dep.get("deployment") if isinstance(dep, dict) else None
    if isinstance(d, dict):
        states.append(d.get("state"))
    for lease in dep.get("leases") or []:
        if isinstance(lease, dict):
            states.append(lease.get("state"))
    dead = {"closed", "insufficient_funds", "insufficientfunds"}
    return any(isinstance(s, str) and s.lower() in dead for s in states)


def _wait_ready(dseq: str, cap_s: float = READY_CAP_S) -> bool:
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
        if _deployment_dead(dseq):
            print(f"  {RED}deployment reached a terminal state{RESET} after {elapsed}s")
            return False
        avail = _service_availability(dseq)
        if avail is not None and avail[0] >= 1:
            print(f"  service available ({avail[0]}/{avail[1]}) after {elapsed}s")
            return True
        # Fallback ~every 30s: a working lease-shell exec proves the container is
        # up even when the provider never populates availability.
        now = time.monotonic()
        if now - last_exec_probe >= 30:
            last_exec_probe = now
            r = _run(
                f"uv run just-akash exec 'echo ready' --dseq {q(dseq)} --transport lease-shell",
                timeout=25,
            )
            if r.returncode == 0 and "ready" in (r.stdout or ""):
                print(
                    f"  container exec-ready after {int(now - start)}s (availability unreported)"
                )
                return True
        time.sleep(6)
    print(f"  {RED}not serving within {int(cap_s)}s{RESET}")
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


def _check_exec(dseq: str) -> bool:
    token = f"smoke-{dseq[-6:]}-ok"
    r = _run(
        f"uv run just-akash exec 'echo {token}' --dseq {q(dseq)} --transport lease-shell",
        timeout=45,
    )
    return r.returncode == 0 and token in (r.stdout or "")


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
        back = _run(
            f"uv run just-akash exec 'cat {q(remote)}' --dseq {q(dseq)} "
            f"--transport {transport} {keyarg}",
            timeout=45,
        )
        return back.returncode == 0 and "injected_ok" in (back.stdout or "")
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


def _check_ingress(dseq: str, uri: str, cap_s: float = INGRESS_CAP_S) -> bool:
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
        except (urllib.error.URLError, OSError) as e:
            last = str(e)[:60]
        time.sleep(6)
    print(f"  {RED}ingress not reachable within {int(cap_s)}s{RESET} (last: {last!r})")
    return False


def _check_update(dseq: str, sdl_path: str, uri: str) -> bool:
    """In-place manifest update: change the served marker and confirm the new
    revision goes live at the same ingress (lease preserved)."""
    token = f"probe-updated-{dseq[-6:]}"
    r = _run(
        f"uv run just-akash update --dseq {q(dseq)} --sdl {q(sdl_path)} "
        f"--env SMOKE_MARKER={token}",
        timeout=120,
    )
    if r.returncode != 0:
        return False
    # The container restarts (and reinstalls its packages), so give it room —
    # same generous cap as the initial ingress check — before the new marker
    # appears at the ingress.
    start = time.monotonic()
    while time.monotonic() - start < INGRESS_CAP_S:
        try:
            if token in _fetch(uri):
                print(f"  update live at ingress after {int(time.monotonic() - start)}s")
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(6)
    return False


# ── orchestration ────────────────────────────────────────────────────


def smoke_provider(provider: str, sdl_path: str, key: str, records: list | None = None) -> dict:
    """Run the full feature matrix against one provider.

    The ``finally`` guarantees the deployment is destroyed and (when ``records``
    is provided) appends one telemetry record per feature — outcome + latency.
    A hard error in the deploy/readiness helpers (e.g. a subprocess timeout) is
    not swallowed here — it propagates to ``main()``, which records the provider
    as all-FAIL and moves on, so one provider's failure never aborts the run.
    """
    results = dict.fromkeys(FEATURES, "-")
    latencies: dict[str, float] = {}
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
        diag_done = {"v": False}

        def _diag_once(reason: str) -> None:
            if not diag_done["v"]:
                diag_done["v"] = True
                _capture_diagnostics(dseq, reason)

        _t0 = time.monotonic()
        ready = _wait_ready(dseq)
        latencies["ready"] = round((time.monotonic() - _t0) * 1000)
        results["ready"] = "PASS" if ready else "FAIL"
        if not ready:
            # A lease that never becomes ready is a real failure, not a pass: every
            # untested feature must read FAIL so the provider counts against the run
            # (the overall verdict only trips on "FAIL", never on "-").
            print(f"  {RED}lease never became ready{RESET} — marking untested features FAIL")
            _diag_once("lease never became ready")  # the intermittent-reliability case
            for feat in FEATURES:
                if results[feat] == "-":
                    results[feat] = "FAIL"
            return results
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
                _diag_once(f"{name} FAILed")

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
            run_check("ingress", lambda: _check_ingress(dseq, uri))
            # update restarts the container, so it runs last, after every other check
            run_check("update", lambda: _check_update(dseq, sdl_path, uri))
        else:
            results["ingress"] = results["update"] = "FAIL"
            print(f"  {RED}ingress: FAIL{RESET} (no ingress URI assigned)")
            _diag_once("no ingress URI assigned")

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
            records.extend(_provider_records(provider, _dseq, results, latencies))


def _print_matrix(rows: dict) -> None:
    _hdr("SMOKE TEST MATRIX")
    wp = max((len(p) for p in rows), default=10)
    header = f"{'provider'.ljust(wp)}  " + " ".join(f.ljust(8) for f in FEATURES)
    print(header)
    print("-" * len(header))
    for prov, res in rows.items():
        cells = []
        for f in FEATURES:
            v = res.get(f, "-")
            skips = ("-", "NO-BID", "NO-ROOM", "NO-CREDIT")
            color = GREEN if v == "PASS" else (YELLOW if v in skips else RED)
            cells.append(f"{color}{v.ljust(8)}{RESET}")
        print(f"{prov.ljust(wp)}  " + " ".join(cells))


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
    records: list | None = [] if args.telemetry_file else None

    rows: dict = {}
    credit_exhausted = False
    try:
        for provider in providers:
            try:
                rows[provider] = smoke_provider(provider, sdl_path, key_path, records=records)
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
        if args.telemetry_file and records:
            _write_telemetry(args.telemetry_file, run_ts, _pkg_version(), records)

    _print_matrix(rows)
    print()

    # Insufficient credit is not a provider verdict at all — nothing was tested.
    # Exit clean (0) so the scheduled run is a no-op, not a red failure.
    if credit_exhausted:
        print(f"{YELLOW}SMOKE TEST SKIPPED{RESET}: insufficient Console credit to deploy probes.")
        return 0

    # A provider fails the smoke test if any testable feature is FAIL. NO-BID /
    # NO-ROOM / NO-CREDIT are skips (provider offered no capacity, or we couldn't
    # afford to test), never failures.
    failed = {p: r for p, r in rows.items() if any(v == "FAIL" for v in r.values())}
    if failed:
        print(f"{RED}SMOKE TEST FAILED{RESET}: {len(failed)} provider(s) with broken features:")
        for p in failed:
            broken = [f for f in FEATURES if rows[p].get(f) == "FAIL"]
            print(f"  {p}: {', '.join(broken)}")
        return 1
    print(f"{GREEN}SMOKE TEST PASSED{RESET}: all testable providers support every feature.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
