#!/usr/bin/env python3
"""Scrape every provider's canary and publish one Prometheus exposition file.

WHERE THE DURABLE STATE LIVES, AND WHY IT IS HERE AND NOT IN THE AGENT
---------------------------------------------------------------------
A restart wipes the agent. So the agent CANNOT count its own restarts — a counter that
resets on the event it counts is a counter that always reads zero. The agent therefore
publishes a `boot_id` that changes every process start, and the durable counting happens
here, by diffing that boot_id against the previous run's state.

Same argument for reachability: the agent cannot report that it was unreachable. Only the
side that failed to reach it can. `akash_canary_reachable` is produced here, and a failed
scrape is not an error in this script — it is the measurement.

REDEPLOY IS NOT A RESTART, and conflating them would poison the signal. When the workflow
has to recreate a lapsed lease the container is new, so its boot_id changes too. The
targets file carries the `dseq`; if that changed, the boot_id change is attributed to the
redeploy and counted as `akash_canary_lease_replacements_total`, never as a restart. A
provider closing our deployment and a provider restarting our container are different
faults and must not land in the same number.

Cumulative counters are carried forward across runs so a workflow-cadence sample loses
timing precision but never loses events.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import time
import urllib.error
import urllib.request

SCRAPE_TIMEOUT = 20.0

_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?P<labels>\{[^}]*\})?"
    r"\s+(?P<value>-?[\d.eE+]+)\s*$"
)
_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def parse_exposition(text: str) -> dict:
    """Parse Prometheus text exposition into {(name, frozenset(labels)): float}."""
    out: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_RE.match(line)
        if not m:
            continue
        labels = tuple(sorted(_LABEL_RE.findall(m.group("labels") or "")))
        try:
            out[(m.group("name"), labels)] = float(m.group("value"))
        except ValueError:
            continue
    return out


def extract_boot_id(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("akash_canary_build_info"):
            m = dict(_LABEL_RE.findall(line))
            return m.get("boot_id")
    return None


def get(samples: dict, name: str, **labels) -> float | None:
    key = (name, tuple(sorted(labels.items())))
    return samples.get(key)


_HOST_RE = re.compile(r"[A-Za-z0-9.\-:]+")


def metrics_url(uri: str) -> str:
    """Build the metrics URL from a provider-assigned ingress value.

    Akash lease status yields a BARE host[:port] (`leases[].status.services[].uris[0]`),
    served over plain http — see just_akash.smoke_providers._ingress_uri/_fetch. Accept
    that form, and tolerate a full http(s) URL for local testing.

    The bare-host branch is regex-guarded for the same reason `_fetch` guards it: the
    value is chosen by the PROVIDER, and an unvalidated one could smuggle a scheme or
    path into the request. Hard-coding the scheme keeps file:// unreachable.
    """
    if uri.startswith(("http://", "https://")):
        return uri.rstrip("/") + "/metrics"
    if not _HOST_RE.fullmatch(uri):
        raise ValueError(f"unexpected ingress host: {uri!r}")
    return f"http://{uri}/metrics"


def scrape(uri: str, timeout: float = SCRAPE_TIMEOUT) -> tuple[bool, str, float]:
    """Fetch the canary's /metrics. Returns (reachable, body, elapsed_seconds).

    Never raises: an unreachable canary is the measurement, not an exception. A
    malformed ingress host is likewise reported as unreachable rather than crashing the
    collector and losing every other provider's reading with it.
    """
    start = time.monotonic()
    try:
        url = metrics_url(uri)
    except ValueError:
        return (False, "", 0.0)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "akash-canary-collect"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return (r.status == 200, body, time.monotonic() - start)
    except (urllib.error.URLError, OSError, ValueError):
        return (False, "", time.monotonic() - start)


def merge(prev: dict, provider: str, dseq: str, reachable: bool, body: str,
          elapsed: float, now: float) -> dict:
    """Fold one scrape into the durable per-provider state."""
    p = dict(prev.get(provider, {}))
    p.setdefault("restarts_total", 0)
    p.setdefault("lease_replacements_total", 0)
    p.setdefault("checks_total", 0)
    p.setdefault("unreachable_checks_total", 0)
    p.setdefault("boot_id", None)
    p.setdefault("dseq", None)

    p["checks_total"] += 1
    p["reachable"] = 1 if reachable else 0
    p["scrape_seconds"] = round(elapsed, 4)
    p["last_collect"] = now

    if not reachable:
        p["unreachable_checks_total"] += 1
        p["dseq"] = dseq or p["dseq"]
        return {**prev, provider: p}

    boot = extract_boot_id(body)
    dseq_changed = dseq and p["dseq"] and dseq != p["dseq"]
    if dseq_changed:
        # New lease: the container is new by construction. Not a restart.
        p["lease_replacements_total"] += 1
    elif boot and p["boot_id"] and boot != p["boot_id"]:
        p["restarts_total"] += 1

    p["boot_id"] = boot or p["boot_id"]
    p["dseq"] = dseq or p["dseq"]

    s = parse_exposition(body)
    p["uptime_seconds"] = get(s, "akash_canary_uptime_seconds") or 0.0
    p["egress_ok"] = get(s, "akash_canary_egress_probe_total", outcome="ok") or 0.0
    p["egress_fail"] = get(s, "akash_canary_egress_probe_total", outcome="fail") or 0.0
    p["dns_ok"] = get(s, "akash_canary_dns_probe_total", outcome="ok") or 0.0
    p["dns_fail"] = get(s, "akash_canary_dns_probe_total", outcome="fail") or 0.0
    p["disk_write_seconds"] = get(s, "akash_canary_disk_write_seconds")
    p["sched_jitter_seconds"] = get(s, "akash_canary_sched_jitter_seconds")
    return {**prev, provider: p}


def render(state: dict, now: float) -> str:
    """Emit the exposition file df-grafana scrapes off the telemetry branch."""
    L: list[str] = []
    add = L.append
    add("# Canary telemetry — persistent per-provider deployments, measured from INSIDE.")
    add("# Generated by canary/collect.py. Do not hand-edit.")
    add("# HELP akash_canary_reachable 1 if the provider served the canary's /metrics on "
        "its ingress at the last check. This is the customer-visible up/down.")
    add("# TYPE akash_canary_reachable gauge")
    for prov, p in sorted(state.items()):
        add(f'akash_canary_reachable{{provider="{prov}"}} {p.get("reachable", 0)}')
    add("# HELP akash_canary_restarts_total Container restarts observed via boot_id "
        "change within the SAME lease. Excludes lease replacements.")
    add("# TYPE akash_canary_restarts_total counter")
    for prov, p in sorted(state.items()):
        add(f'akash_canary_restarts_total{{provider="{prov}"}} {p.get("restarts_total", 0)}')
    add("# HELP akash_canary_lease_replacements_total Times the lease had to be recreated "
        "(dseq changed) — a deployment the provider closed or that lapsed.")
    add("# TYPE akash_canary_lease_replacements_total counter")
    for prov, p in sorted(state.items()):
        add(f'akash_canary_lease_replacements_total{{provider="{prov}"}} '
            f'{p.get("lease_replacements_total", 0)}')
    add("# HELP akash_canary_unreachable_checks_total Checks where the canary could not "
        "be reached on its ingress.")
    add("# TYPE akash_canary_unreachable_checks_total counter")
    for prov, p in sorted(state.items()):
        add(f'akash_canary_unreachable_checks_total{{provider="{prov}"}} '
            f'{p.get("unreachable_checks_total", 0)}')
    add("# HELP akash_canary_checks_total Total collector checks.")
    add("# TYPE akash_canary_checks_total counter")
    for prov, p in sorted(state.items()):
        add(f'akash_canary_checks_total{{provider="{prov}"}} {p.get("checks_total", 0)}')

    # Pass-through of the inside-the-deployment view.
    passthrough = [
        ("akash_canary_uptime_seconds", "uptime_seconds", "gauge",
         "Seconds since the canary process started (resets on restart)."),
        ("akash_canary_egress_ok_total", "egress_ok", "counter",
         "Successful outbound HTTP probes from inside the deployment."),
        ("akash_canary_egress_fail_total", "egress_fail", "counter",
         "FAILED outbound HTTP probes from inside — the workload could not reach out."),
        ("akash_canary_dns_ok_total", "dns_ok", "counter",
         "Successful DNS resolutions from inside the deployment."),
        ("akash_canary_dns_fail_total", "dns_fail", "counter",
         "FAILED DNS resolutions from inside the deployment."),
        ("akash_canary_disk_write_seconds", "disk_write_seconds", "gauge",
         "fsync latency for a 4KiB write as the workload feels it (-1 = unusable)."),
        ("akash_canary_sched_jitter_seconds", "sched_jitter_seconds", "gauge",
         "How late the probe tick fired — CPU contention from the customer's side."),
        ("akash_canary_scrape_seconds", "scrape_seconds", "gauge",
         "Time to fetch the canary's /metrics over its public ingress."),
    ]
    for metric, key, typ, help_ in passthrough:
        add(f"# HELP {metric} {help_}")
        add(f"# TYPE {metric} {typ}")
        for prov, p in sorted(state.items()):
            v = p.get(key)
            if v is not None:
                add(f'{metric}{{provider="{prov}"}} {v}')

    add("# HELP akash_canary_last_collect_timestamp_seconds Unix time of this collection.")
    add("# TYPE akash_canary_last_collect_timestamp_seconds gauge")
    add(f"akash_canary_last_collect_timestamp_seconds {now:.0f}")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--targets", required=True,
                    help='JSON: {"provider": {"uri": "http://...", "dseq": "123"}}')
    ap.add_argument("--state", required=True, help="Durable state JSON (read+write)")
    ap.add_argument("--out", required=True, help="Exposition file to write")
    ap.add_argument("--timeout", type=float, default=SCRAPE_TIMEOUT)
    a = ap.parse_args()

    targets = json.loads(pathlib.Path(a.targets).read_text(encoding="utf-8"))
    sp = pathlib.Path(a.state)
    state = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    now = time.time()

    for provider, t in sorted(targets.items()):
        uri = t.get("uri") or ""
        ok, body, elapsed = (False, "", 0.0) if not uri else scrape(uri, a.timeout)
        state = merge(state, provider, str(t.get("dseq") or ""), ok, body, elapsed, now)
        print(f"{provider:14} reachable={int(ok)} "
              f"restarts={state[provider]['restarts_total']} "
              f"lease_replacements={state[provider]['lease_replacements_total']} "
              f"({elapsed:.2f}s)", flush=True)

    sp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    pathlib.Path(a.out).write_text(render(state, now), encoding="utf-8")
    # Exit 0 even when a canary is down: an unreachable provider is the DATA, and a
    # non-zero exit here would fail the workflow and stop the file being published —
    # losing the very measurement we came for.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
