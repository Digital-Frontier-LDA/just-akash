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
the SSH-transport checks. Providers that do not bid on the probe profile are
reported NO-BID (cannot be tested), not failed.

Run from the repository root: cleanup goes through robust_destroy(), which shells
out to `just destroy` / `just list`, so the Justfile and `just` must be available.
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
import urllib.error
import urllib.request
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
        apk add --no-cache openssh busybox-extras >/dev/null 2>&1
        mkdir -p /run/sshd /root/.ssh /www
        echo "$SSH_PUBKEY_B64" | base64 -d > /root/.ssh/authorized_keys
        chmod 700 /root/.ssh; chmod 600 /root/.ssh/authorized_keys
        ssh-keygen -A
        sed -i 's/#\\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
        /usr/sbin/sshd
        printf '%s' "$SMOKE_MARKER" > /www/index.html
        busybox-extras httpd -p 80 -h /www
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
        services = ((lease.get("status") or {}).get("services")) or {}
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
    # A real ms-epoch dseq lands in a sane window: after 2020 and not in the
    # future. Anything else (tiny block height, garbage) -> unknown age.
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
            if "(404)" not in str(e):
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


def _wait_ready(dseq: str) -> bool:
    """Poll status until the lease reports ready."""
    time.sleep(8)
    for _ in range(18):
        data = _status_json(dseq)
        if data.get("status") == "ready" or data.get("ssh_host"):
            return True
        time.sleep(5)
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
        services = (lease.get("status") or {}).get("services") or {}
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
    """logs/events must return within the bounded --duration window (no hang).

    logs/events are lease-shell-only and take no --transport flag (passing one is
    an argparse error), so the command must not include it.
    """
    start = time.monotonic()
    r = _run(f"uv run just-akash {command} --dseq {q(dseq)} --duration 8", timeout=40)
    elapsed = time.monotonic() - start
    return r.returncode == 0 and elapsed < 35


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


def _check_ingress(dseq: str, uri: str) -> bool:
    """The provider routes the exposed HTTP port to the container's httpd."""
    for _ in range(15):
        try:
            if INGRESS_BASELINE in _fetch(uri):
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(8)
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
    # The container restarts (and reinstalls openssh/busybox-extras), so give it
    # room before the new marker appears at the ingress.
    for _ in range(20):
        try:
            if token in _fetch(uri):
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(8)
    return False


# ── orchestration ────────────────────────────────────────────────────


def smoke_provider(provider: str, sdl_path: str, key: str) -> dict:
    """Run the full feature matrix against one provider.

    The ``finally`` guarantees the deployment is destroyed. A hard error in the
    deploy/readiness helpers (e.g. a subprocess timeout) is not swallowed here — it
    propagates to ``main()``, which records the provider as all-FAIL and moves on, so
    one provider's failure never aborts the run.
    """
    results = dict.fromkeys(FEATURES, "-")
    dseq_ref: dict = {"dseq": None}
    install_signal_cleanup(dseq_ref)
    _hdr(f"provider {provider}")
    try:
        dseq, note = _deploy(sdl_path, provider, dseq_ref)
        if not dseq:
            results["deploy"] = "NO-BID" if note == "no-bid" else "FAIL"
            print(f"  {RED}{note}{RESET} — cannot test remaining features")
            return results
        results["deploy"] = "PASS"
        print(f"  {GREEN}deployed{RESET} DSEQ={dseq}, waiting for lease...")

        if not _wait_ready(dseq):
            # A lease that never becomes ready is a real failure, not a pass: every
            # untested feature must read FAIL so the provider counts against the run
            # (the overall verdict only trips on "FAIL", never on "-").
            for feat in FEATURES:
                if results[feat] == "-":
                    results[feat] = "FAIL"
            print(f"  {RED}lease never became ready{RESET} — marking untested features FAIL")
            return results
        if not _wait_exec_ready(dseq):
            print(f"  {YELLOW}container slow to accept exec{RESET} — checks may reflect that")

        def run_check(name: str, fn) -> None:
            try:
                ok = fn()
            except Exception as e:  # noqa: BLE001 — a broken feature must not abort the run
                ok = False
                print(f"  {name}: raised {type(e).__name__}: {e}")
            results[name] = "PASS" if ok else "FAIL"
            print(f"  {GREEN if ok else RED}{name}: {results[name]}{RESET}")

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

        # ingress + update (need the exposed HTTP endpoint serving)
        uri = _ingress_uri(dseq)
        if uri:
            run_check("ingress", lambda: _check_ingress(dseq, uri))
            # update restarts the container, so it runs last, after every other check
            run_check("update", lambda: _check_update(dseq, sdl_path, uri))
        else:
            results["ingress"] = results["update"] = "FAIL"
            print(f"  {RED}ingress: FAIL{RESET} (no ingress URI assigned)")

        return results
    finally:
        if dseq_ref["dseq"]:
            print(f"  cleanup: destroying {dseq_ref['dseq']}...")
            robust_destroy(dseq_ref["dseq"])
            # Clear the ref so a later Ctrl-C's signal handler skips this already-
            # destroyed deployment instead of re-issuing destroy against it.
            dseq_ref["dseq"] = None


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
            color = GREEN if v == "PASS" else (YELLOW if v in ("-", "NO-BID") else RED)
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
    args = ap.parse_args()

    if not os.environ.get("AKASH_API_KEY"):
        print("Error: AKASH_API_KEY not set.", file=sys.stderr)
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
        sweep_orphan_probes(dry_run=args.dry_run)
        return 0

    # Sweep before resolving providers, not after: the sweep scans all
    # deployments and does not depend on the provider list, so it must run even
    # when no providers are configured -- otherwise a no-providers run would
    # return below without reaping, defeating the self-healing guarantee.
    if not args.no_sweep:
        _hdr(sweep_hdr)
        sweep_orphan_probes(dry_run=args.dry_run)

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

    rows: dict = {}
    try:
        for provider in providers:
            try:
                rows[provider] = smoke_provider(provider, sdl_path, key_path)
            except Exception as e:  # noqa: BLE001 — one provider's hard error must not abort the run
                print(f"  {RED}{provider} aborted: {type(e).__name__}: {e}{RESET}")
                rows[provider] = dict.fromkeys(FEATURES, "FAIL")
    finally:
        os.unlink(sdl_path)
        # Remove the ephemeral keypair — the unencrypted private key must not be
        # left behind in the temp dir after the run.
        shutil.rmtree(os.path.dirname(key_path), ignore_errors=True)

    _print_matrix(rows)

    # A provider fails the smoke test if any testable feature is FAIL. NO-BID is
    # not a failure (the provider offered no capacity for the probe profile).
    failed = {p: r for p, r in rows.items() if any(v == "FAIL" for v in r.values())}
    print()
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
