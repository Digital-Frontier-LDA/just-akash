#!/usr/bin/env python3
"""In-deployment canary agent — what a CUSTOMER experiences, measured from inside.

WHY THIS EXISTS, AND WHY THE SMOKE TEST IS NOT ENOUGH
-----------------------------------------------------
`provider-smoke.yml` answers "can I deploy right now?" It runs once a day, exercises
eleven features, and then deliberately erases itself — `robust_destroy()`, the SIGINT
handler, the post-destroy audit, the no-leak guarantee. That design is correct for what it
measures and it makes the whole class of failures below invisible:

    a lease the provider closes on Tuesday afternoon
    a container restarted at 03:00
    egress that stops resolving DNS for twenty minutes
    an ingress that goes dark while the container is perfectly healthy

None of those happen inside a five-minute window at 07:00 UTC. A customer meets all of
them, because a customer's deployment STAYS UP. This agent is the part that stays up.

WHAT IT MEASURES, AND WHY EACH ONE IS FROM THE INSIDE
----------------------------------------------------
Every signal here is something only the workload itself can see. Reachability from
outside is measured by the collector failing to scrape this endpoint — that is the
customer's "my app is down". These are the complements to it:

  uptime / boot_id     A restart wipes this process. The collector compares boot_id
                       across scrapes; a change is a restart the customer's workload ate.
                       Deliberately NOT a self-counter — a counter that resets on the
                       event it counts cannot count it.
  egress probes        Can the workload reach the internet? A deployment whose outbound
                       calls fail is useless to a customer even while it looks "up",
                       and no external probe can tell you that.
  dns probes           Separated from egress on purpose: DNS breaking and routing
                       breaking are different provider faults with different fixes, and
                       they present identically from outside.
  disk write latency   fsync latency as the workload feels it, not as the host reports it.
  scheduler jitter     How late a 1-second tick actually fires. This is CPU contention
                       from the customer's side — the number a noisy-neighbour victim
                       would feel and could never prove from a host-level metric.

COUNTERS ARE CUMULATIVE AND THAT IS LOAD-BEARING. The collector samples on a workflow
cadence (minutes), not continuously. Cumulative counters mean a sampling gap loses
TIMING PRECISION but never loses EVENTS: twelve egress failures between two scrapes still
show up as twelve. Gauges would silently drop them.

ZERO BOOT-TIME DEPENDENCIES, DELIBERATELY. stdlib only, no pip install, no apk add. The
existing SDLs install packages at start; a canary must not. If this container needed a
package mirror to boot, a PyPI outage would present as a provider failure and we would
chase the wrong thing. It has to be able to start on a broken internet and TELL us the
internet is broken.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"

PORT = int(os.environ.get("CANARY_PORT", "8080"))
PROVIDER = os.environ.get("CANARY_PROVIDER", "unknown")
PROBE_INTERVAL = float(os.environ.get("CANARY_PROBE_INTERVAL", "15"))
# Two independent egress targets. One target cannot distinguish "our egress is broken"
# from "that one host is down", and a canary that cries provider-fault over somebody
# else's outage is worse than no canary.
EGRESS_URLS = [
    u.strip()
    for u in os.environ.get(
        # NOT api.github.com: its unauthenticated limit is 60 requests/hour and this probe
        # runs every 15s (240/hour). The 429s would be counted as egress FAILURES — the
        # canary would manufacture the outages it exists to detect. Both defaults are
        # anycast endpoints built to be hammered and returning tiny bodies.
        "CANARY_EGRESS_URLS",
        "https://cloudflare.com/cdn-cgi/trace,https://1.1.1.1",
    ).split(",")
    if u.strip()
]
DNS_NAMES = [
    n.strip()
    for n in os.environ.get("CANARY_DNS_NAMES", "github.com,akash.network").split(",")
    if n.strip()
]
PROBE_TIMEOUT = float(os.environ.get("CANARY_PROBE_TIMEOUT", "10"))

# BOOT_ID is the restart signal. Regenerated every process start; the collector diffs it.
BOOT_ID = uuid.uuid4().hex[:16]
START_TIME = time.time()

_lock = threading.Lock()
_state = {
    "egress_ok": 0,
    "egress_fail": 0,
    "dns_ok": 0,
    "dns_fail": 0,
    "cycles": 0,
    "disk_write_seconds": -1.0,
    "sched_jitter_seconds": -1.0,
    "last_probe_unixtime": 0.0,
}


def _probe_egress() -> bool:
    """One HTTP round trip per configured target. All must fail to count as a failure."""
    for url in EGRESS_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "akash-canary"})
            with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
                if 200 <= r.status < 500:  # any answer proves the path works
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return False


def _probe_dns() -> bool:
    """Resolve at least one name.

    Deliberately does NOT call socket.setdefaulttimeout(): that mutates the default for
    every socket created anywhere in the process — including the ones the metrics server
    accepts on — so a probe tweak would silently reconfigure the server. getaddrinfo has
    no per-call timeout, so this accepts the resolver's own bound rather than buying a
    weak guarantee at the cost of global state.
    """
    for name in DNS_NAMES:
        try:
            socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
            return True
        except (OSError, socket.gaierror):
            continue
    return False


def _probe_disk() -> float:
    """fsync latency for a small write — what a customer's database would feel.

    Returns seconds, or -1.0 if the write path is unusable (which is itself a finding:
    a read-only or full filesystem is reported rather than crashing the agent).
    """
    path = os.environ.get("CANARY_DISK_PATH", "/tmp/.canary-probe")
    payload = b"x" * 4096
    try:
        start = time.monotonic()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        return time.monotonic() - start
    except OSError:
        return -1.0


def _probe_loop() -> None:
    """Probe forever. Never raises — an agent that dies on a probe error stops being a
    canary at the exact moment something interesting is happening."""
    next_tick = time.monotonic()
    while True:
        next_tick += PROBE_INTERVAL
        sleep_for = next_tick - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        # Jitter = how far past the scheduled tick we actually woke. Under CPU pressure
        # or heavy steal this climbs, and it is the customer-side view of contention.
        jitter = max(0.0, time.monotonic() - next_tick)

        try:
            egress = _probe_egress()
            dns = _probe_dns()
            disk = _probe_disk()
        except Exception:  # noqa: BLE001 - see docstring: never die in the probe loop
            egress, dns, disk = False, False, -1.0

        with _lock:
            _state["egress_ok" if egress else "egress_fail"] += 1
            _state["dns_ok" if dns else "dns_fail"] += 1
            _state["disk_write_seconds"] = disk
            _state["sched_jitter_seconds"] = jitter
            _state["cycles"] += 1
            _state["last_probe_unixtime"] = time.time()


def render_metrics() -> str:
    """Prometheus text exposition of everything this agent knows."""
    with _lock:
        s = dict(_state)
    now = time.time()
    lines = [
        "# HELP akash_canary_build_info Canary agent build and boot identity.",
        "# TYPE akash_canary_build_info gauge",
        f'akash_canary_build_info{{version="{VERSION}",provider="{PROVIDER}",'
        f'boot_id="{BOOT_ID}"}} 1',
        "# HELP akash_canary_start_time_seconds Unix time this agent process started.",
        "# TYPE akash_canary_start_time_seconds gauge",
        f"akash_canary_start_time_seconds {START_TIME:.3f}",
        "# HELP akash_canary_uptime_seconds Seconds since this agent process started.",
        "# TYPE akash_canary_uptime_seconds gauge",
        f"akash_canary_uptime_seconds {now - START_TIME:.3f}",
        "# HELP akash_canary_egress_probe_total Outbound HTTP probes by outcome.",
        "# TYPE akash_canary_egress_probe_total counter",
        f'akash_canary_egress_probe_total{{outcome="ok"}} {s["egress_ok"]}',
        f'akash_canary_egress_probe_total{{outcome="fail"}} {s["egress_fail"]}',
        "# HELP akash_canary_dns_probe_total DNS resolution probes by outcome.",
        "# TYPE akash_canary_dns_probe_total counter",
        f'akash_canary_dns_probe_total{{outcome="ok"}} {s["dns_ok"]}',
        f'akash_canary_dns_probe_total{{outcome="fail"}} {s["dns_fail"]}',
        "# HELP akash_canary_probe_cycles_total Completed probe cycles.",
        "# TYPE akash_canary_probe_cycles_total counter",
        f'akash_canary_probe_cycles_total {s["cycles"]}',
        "# HELP akash_canary_disk_write_seconds Last fsync latency for a 4KiB write "
        "(-1 = write path unusable).",
        "# TYPE akash_canary_disk_write_seconds gauge",
        f'akash_canary_disk_write_seconds {s["disk_write_seconds"]:.6f}',
        "# HELP akash_canary_sched_jitter_seconds How late the last probe tick fired "
        "(CPU contention as the workload feels it).",
        "# TYPE akash_canary_sched_jitter_seconds gauge",
        f'akash_canary_sched_jitter_seconds {s["sched_jitter_seconds"]:.6f}',
        "# HELP akash_canary_last_probe_timestamp_seconds Unix time of the last cycle.",
        "# TYPE akash_canary_last_probe_timestamp_seconds gauge",
        f'akash_canary_last_probe_timestamp_seconds {s["last_probe_unixtime"]:.3f}',
    ]
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.rstrip("/") in ("/metrics", "/metrics"):
            body = render_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.rstrip("/") in ("", "/healthz"):
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args, **kwargs) -> None:  # noqa: D102 - silence access logs
        return


def main() -> None:
    threading.Thread(target=_probe_loop, daemon=True).start()
    # ThreadingHTTPServer, not HTTPServer: a single-threaded server that blocks on one
    # slow client stops answering /metrics, which the collector would read as the
    # deployment being unreachable — a self-inflicted false positive.
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"akash-canary {VERSION} boot_id={BOOT_ID} provider={PROVIDER} port={PORT}",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
