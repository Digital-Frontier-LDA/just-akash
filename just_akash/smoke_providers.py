#!/usr/bin/env python3
"""Provider capability smoke test.

Deploys a tiny throwaway workload to each configured provider and exercises every
just-akash feature that talks to the provider over the lease-shell transport —
deploy, exec, inject, logs, events, status — then destroys it. Prints a
provider x feature pass/fail matrix and exits non-zero if any provider fails any
feature.

The point: catch a provider that accepts deployments (so it looks healthy by
rental metrics) but has a broken shell/logs/exec path — exactly the class of
outage that a normal rental never exercises. See the v0.14.2-df.1 regression
where lease-shell returned HTTP 500 while the provider bid and ran containers
fine.

Usage:
    uv run python -m just_akash.smoke_providers            # preferred tier (AKASH_PROVIDERS)
    uv run python -m just_akash.smoke_providers --all       # preferred + backup tiers
    uv run python -m just_akash.smoke_providers --provider akash1... [--provider ...]

Costs a small amount of AKT: one minimal lease per provider, destroyed
immediately (and on Ctrl-C). Providers that do not bid on the probe profile are
reported as NO-BID (cannot be tested), not as failures.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time

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

# Minimal, boring workload: stock alpine that prints one line then idles. One
# global port so the manifest is valid; the resource profile matches what the
# dedicated providers actually bid on. Nothing about this workload can explain a
# shell/logs failure, so a failure is unambiguously the provider's.
PROBE_MARKER = "probe-container-up"
PROBE_SDL = f"""\
---
version: "2.0"
services:
  probe:
    image: alpine:3.20
    expose:
      - port: 80
        as: 80
        to:
          - global: true
    args:
      - sh
      - -c
      - echo {PROBE_MARKER}; sleep infinity
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
FEATURES = ["deploy", "status", "exec", "inject", "logs", "events"]


def _hdr(msg: str) -> None:
    print(f"\n{YELLOW}== {msg} =={RESET}", flush=True)


def _deploy(sdl_path: str, provider: str, dseq_ref: dict) -> tuple[str | None, str]:
    """Deploy the probe pinned to ``provider``. Returns (dseq, note).

    dseq is None when the provider did not bid (note == "no-bid") or the deploy
    failed (note == "deploy-failed"). Backups are disabled so the lease can only
    land on the target provider.
    """
    r = _run(
        f"uv run just-akash deploy --sdl {sdl_path} "
        f"--provider {provider} --backup-provider '' "
        f"--bid-wait 120 --bid-wait-retry 60",
        timeout=360,
    )
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"DSEQ[:=]\s*(\d+)", out)
    if m:
        dseq_ref["dseq"] = m.group(1)
        return m.group(1), "ok"
    if re.search(r"NO BID|no bid|NONE from our providers|foreign bids", out):
        return None, "no-bid"
    return None, "deploy-failed"


def _wait_ready(dseq: str) -> str | None:
    """Poll status until the lease reports ready. Returns provider addr or None."""
    time.sleep(8)
    for _ in range(18):
        r = _run(f"uv run just-akash status --dseq {dseq} --json", timeout=30)
        try:
            data = json.loads(r.stdout)
        except (json.JSONDecodeError, TypeError):
            data = {}
        if isinstance(data, dict) and (data.get("status") == "ready" or data.get("ssh_host")):
            return data.get("provider")
        time.sleep(5)
    return None


def _wait_exec_ready(dseq: str, attempts: int = 12, interval: int = 8) -> bool:
    """Poll until an exec both succeeds AND returns its output, not just until the
    lease is 'ready'.

    Two separate warm-up effects to clear before the feature matrix is meaningful:
    (1) a lease reports ready as soon as it is active, but the container inside may
    still be pulling its image, so an early exec fails outright on a slower provider;
    (2) even once exec succeeds, the very first command against a freshly-started
    container can come back rc=0 with EMPTY stdout (the exit-code frame arrives while
    the stdout frame is still in flight). Verifying a round-tripped marker clears both
    -- so a provider that genuinely runs commands is never mis-reported as broken, and
    only a container that never produces working exec output within the window fails.
    """
    marker = "exec-ready-probe"
    for _ in range(attempts):
        r = _run(
            f"uv run just-akash exec 'echo {marker}' --dseq {dseq} --transport lease-shell",
            timeout=30,
        )
        if r.returncode == 0 and marker in (r.stdout or ""):
            return True
        time.sleep(interval)
    return False


def _check_status(dseq: str) -> bool:
    r = _run(f"uv run just-akash status --dseq {dseq} --json", timeout=30)
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(data, dict) and bool(data.get("provider"))


def _check_exec(dseq: str) -> bool:
    token = f"smoke-{dseq[-6:]}-ok"
    r = _run(
        f"uv run just-akash exec 'echo {token}' --dseq {dseq} --transport lease-shell",
        timeout=45,
    )
    return r.returncode == 0 and token in (r.stdout or "")


def _check_inject(dseq: str) -> bool:
    """Inject an env file over lease-shell, then read it back via exec."""
    # This path is inside the ephemeral probe container, not the local host, so the
    # usual /tmp predictability concern does not apply.
    remote = "/tmp/smoke-inject.env"  # noqa: S108
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write("SMOKE_SECRET=injected_ok\nSECOND_VAR=hello_world\n")
        env_file = f.name
    try:
        inj = _run(
            f"uv run just-akash inject --dseq {dseq} --env-file {env_file} "
            f"--remote-path {remote} --transport lease-shell",
            timeout=60,
        )
        if inj.returncode != 0:
            return False
        back = _run(
            f"uv run just-akash exec 'cat {remote}' --dseq {dseq} --transport lease-shell",
            timeout=45,
        )
        return back.returncode == 0 and "injected_ok" in (back.stdout or "")
    finally:
        os.unlink(env_file)


def _check_stream(dseq: str, command: str) -> bool:
    """logs/events must return within the bounded --duration window (no hang).

    logs/events are lease-shell-only and take no --transport flag (passing one is
    an argparse error), so the command must not include it.
    """
    start = time.monotonic()
    r = _run(
        f"uv run just-akash {command} --dseq {dseq} --duration 8",
        timeout=40,
    )
    elapsed = time.monotonic() - start
    # Success = clean exit AND it actually returned near the duration bound rather
    # than being killed by our outer timeout (the old hang symptom).
    return r.returncode == 0 and elapsed < 35


def smoke_provider(provider: str, sdl_path: str) -> dict:
    """Run the full feature matrix against one provider. Never raises."""
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

        if _wait_ready(dseq) is None:
            print(f"  {RED}lease never became ready{RESET} — skipping feature checks")
            return results

        # Wait for the container to actually accept an exec before running the
        # feature matrix, so a slow-starting container doesn't read as a broken
        # shell. If it never becomes execable, exec/inject will fail below (real).
        if not _wait_exec_ready(dseq):
            print(f"  {YELLOW}container slow to accept exec{RESET} — checks may reflect that")

        checks = {
            "status": lambda: _check_status(dseq),
            "exec": lambda: _check_exec(dseq),
            "inject": lambda: _check_inject(dseq),
            "logs": lambda: _check_stream(dseq, "logs"),
            "events": lambda: _check_stream(dseq, "events"),
        }
        for name, fn in checks.items():
            try:
                ok = fn()
            except Exception as e:  # noqa: BLE001 — a broken feature must not abort the run
                ok = False
                print(f"  {name}: raised {type(e).__name__}: {e}")
            results[name] = "PASS" if ok else "FAIL"
            print(f"  {GREEN if ok else RED}{name}: {results[name]}{RESET}")
        return results
    finally:
        if dseq_ref["dseq"]:
            print(f"  cleanup: destroying {dseq_ref['dseq']}...")
            robust_destroy(dseq_ref["dseq"])


def _print_matrix(rows: dict) -> None:
    _hdr("SMOKE TEST MATRIX")
    wp = max(len(p) for p in rows) if rows else 10
    header = f"{'provider'.ljust(wp)}  " + "  ".join(f.ljust(7) for f in FEATURES)
    print(header)
    print("-" * len(header))
    for prov, res in rows.items():
        cells = []
        for f in FEATURES:
            v = res.get(f, "-")
            color = GREEN if v == "PASS" else (YELLOW if v in ("-", "NO-BID") else RED)
            cells.append(f"{color}{v.ljust(7)}{RESET}")
        print(f"{prov.ljust(wp)}  " + "  ".join(cells))


def main() -> int:
    ap = argparse.ArgumentParser(description="Provider capability smoke test.")
    ap.add_argument("--all", action="store_true", help="Test backup providers too")
    ap.add_argument(
        "--provider",
        action="append",
        dest="providers",
        help="Test only this provider (repeatable)",
    )
    args = ap.parse_args()

    if not os.environ.get("AKASH_API_KEY"):
        print("Error: AKASH_API_KEY not set.", file=sys.stderr)
        return 1

    if args.providers:
        providers = args.providers
    else:
        preferred, backup, _ = resolve_tiers()
        providers = preferred + backup if args.all else preferred
    if not providers:
        print("No providers to test (set AKASH_PROVIDERS or pass --provider).", file=sys.stderr)
        return 1

    print(f"Smoke-testing {len(providers)} provider(s): one throwaway lease each.")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(PROBE_SDL)
        sdl_path = f.name

    rows: dict = {}
    try:
        for provider in providers:
            rows[provider] = smoke_provider(provider, sdl_path)
    finally:
        os.unlink(sdl_path)

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
