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

from canary._state import load_json_mapping

# Reused from the exporter rather than re-implemented: _escape_label_value handles the
# exposition-format escapes, and _is_number excludes bool (a bool IS an int, so a
# stray `true` would otherwise publish as `1`). canary/canary.py stays stdlib-only
# because it runs INSIDE the lease; this module runs in CI where just_akash is
# installed, so it should use the repo's implementations, not copies of them.
from just_akash.prometheus_exporter import _escape_label_value, _is_number

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

    BARE host[:port] ONLY. Akash lease status yields that form
    (`leases[].status.services[].uris[0]`), served over plain http — see
    just_akash.smoke_providers._ingress_uri/_fetch.

    An earlier revision also accepted a full http(s) URL "for local testing", and that
    was a hole rather than a convenience: this value is chosen by the PROVIDER, so the
    accepting branch let a hostile ingress smuggle a scheme, host or path straight into
    the request (SSRF). There is no need for it — local testing passes `127.0.0.1:8080`,
    which takes the guarded path like everything else. Rejecting the full-URL form is what
    makes hard-coding the scheme actually mean something, and keeps file:// unreachable.
    """
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


def merge(
    prev: dict, provider: str, dseq: str, reachable: bool, body: str, elapsed: float, now: float
) -> dict:
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
        # DELIBERATELY NOT advancing dseq here. A redeploy is normally observed while the
        # new lease is still coming up, so recording the new dseq now would mean that by
        # the time the canary answers, dseq looks unchanged — and the inevitable boot_id
        # change would then be booked as a RESTART instead of a lease replacement. The
        # dseq only advances on a scrape that actually saw the new container, which is the
        # only moment we can attribute the boot_id change correctly.
        return {**prev, provider: p}

    boot = extract_boot_id(body)
    dseq_changed = bool(dseq and p["dseq"] and dseq != p["dseq"])
    boot_changed = bool(boot and p["boot_id"] and boot != p["boot_id"])
    if dseq_changed:
        # New lease: the container is new by construction. Not a restart.
        p["lease_replacements_total"] += 1
    elif boot_changed:
        p["restarts_total"] += 1

    s = parse_exposition(body)
    # The agent's counters are PROCESS-LOCAL and reset to zero on every restart. Passing
    # them straight through would publish a *_total that decreases, and would silently
    # discard every failure from earlier process lifetimes. Accumulate the deltas here
    # instead, where the state is durable: a reset (boot_id changed, or the raw value went
    # backwards) contributes the new reading in full rather than a negative delta.
    for key, metric, labels in (
        ("egress_ok", "akash_canary_egress_probe_total", {"outcome": "ok"}),
        ("egress_fail", "akash_canary_egress_probe_total", {"outcome": "fail"}),
        ("dns_ok", "akash_canary_dns_probe_total", {"outcome": "ok"}),
        ("dns_fail", "akash_canary_dns_probe_total", {"outcome": "fail"}),
    ):
        raw = get(s, metric, **labels) or 0.0
        prev_raw = p.get(f"raw_{key}")
        # `base` is how much of the previous reading still counts. Zero on a reset — the
        # agent restarted (boot_id changed), we have no prior reading, or the raw value
        # went backwards — so the whole new reading is the delta rather than a negative
        # one. Written as a single expression so the type of `base` is unambiguous.
        base = (
            prev_raw
            if isinstance(prev_raw, (int, float)) and not boot_changed and raw >= prev_raw
            else 0.0
        )
        p[key] = p.get(key, 0.0) + (raw - base)
        p[f"raw_{key}"] = raw

    p["boot_id"] = boot or p["boot_id"]
    p["dseq"] = dseq or p["dseq"]
    p["uptime_seconds"] = get(s, "akash_canary_uptime_seconds") or 0.0
    p["disk_write_seconds"] = get(s, "akash_canary_disk_write_seconds")
    p["sched_jitter_seconds"] = get(s, "akash_canary_sched_jitter_seconds")
    return {**prev, provider: p}


def render(
    state: dict,
    now: float,
    credit: dict | None = None,
    credit_read_at: float | None = None,
) -> str:
    """Emit the exposition file df-grafana scrapes off the telemetry branch."""
    L: list[str] = []
    add = L.append
    add("# Canary telemetry — persistent per-provider deployments, measured from INSIDE.")
    add("# Generated by canary/collect.py. Do not hand-edit.")
    add(
        "# HELP akash_canary_reachable 1 if the provider served the canary's /metrics on "
        "its ingress at the last check. This is the customer-visible up/down."
    )
    add("# TYPE akash_canary_reachable gauge")
    for prov, p in sorted(state.items()):
        add(f'akash_canary_reachable{{provider="{prov}"}} {p.get("reachable", 0)}')
    add(
        "# HELP akash_canary_restarts_total Container restarts observed via boot_id "
        "change within the SAME lease. Excludes lease replacements."
    )
    add("# TYPE akash_canary_restarts_total counter")
    for prov, p in sorted(state.items()):
        add(f'akash_canary_restarts_total{{provider="{prov}"}} {p.get("restarts_total", 0)}')
    add(
        "# HELP akash_canary_lease_replacements_total Times the lease had to be recreated "
        "(dseq changed) — a deployment the provider closed or that lapsed."
    )
    add("# TYPE akash_canary_lease_replacements_total counter")
    for prov, p in sorted(state.items()):
        add(
            f'akash_canary_lease_replacements_total{{provider="{prov}"}} '
            f"{p.get('lease_replacements_total', 0)}"
        )
    add(
        "# HELP akash_canary_unreachable_checks_total Checks where the canary could not "
        "be reached on its ingress."
    )
    add("# TYPE akash_canary_unreachable_checks_total counter")
    for prov, p in sorted(state.items()):
        add(
            f'akash_canary_unreachable_checks_total{{provider="{prov}"}} '
            f"{p.get('unreachable_checks_total', 0)}"
        )
    add("# HELP akash_canary_checks_total Total collector checks.")
    add("# TYPE akash_canary_checks_total counter")
    for prov, p in sorted(state.items()):
        add(f'akash_canary_checks_total{{provider="{prov}"}} {p.get("checks_total", 0)}')

    # Pass-through of the inside-the-deployment view.
    passthrough = [
        (
            "akash_canary_uptime_seconds",
            "uptime_seconds",
            "gauge",
            "Seconds since the canary process started (resets on restart).",
        ),
        (
            "akash_canary_egress_ok_total",
            "egress_ok",
            "counter",
            "Successful outbound HTTP probes from inside the deployment.",
        ),
        (
            "akash_canary_egress_fail_total",
            "egress_fail",
            "counter",
            "FAILED outbound HTTP probes from inside — the workload could not reach out.",
        ),
        (
            "akash_canary_dns_ok_total",
            "dns_ok",
            "counter",
            "Successful DNS resolutions from inside the deployment.",
        ),
        (
            "akash_canary_dns_fail_total",
            "dns_fail",
            "counter",
            "FAILED DNS resolutions from inside the deployment.",
        ),
        (
            "akash_canary_disk_write_seconds",
            "disk_write_seconds",
            "gauge",
            "fsync latency for a 4KiB write as the workload feels it (-1 = unusable).",
        ),
        (
            "akash_canary_sched_jitter_seconds",
            "sched_jitter_seconds",
            "gauge",
            "How late the probe tick fired — CPU contention from the customer's side.",
        ),
        (
            "akash_canary_scrape_seconds",
            "scrape_seconds",
            "gauge",
            "Time to fetch the canary's /metrics over its public ingress.",
        ),
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
    # ── wallet credit, republished on the CANARY's cadence ──────────────────────────────
    # just_akash_deploy_credit_usd is written once per day by the smoke, so anything reading
    # it sees a figure that can be a full day old while looking current. These carry the same
    # numbers on this workflow's cadence instead.
    #
    # ALL THREE COMPONENTS, deliberately. The free figure alone is ambiguous: a constant
    # grant with escrow climbing looks exactly like a wallet draining, and the two call for
    # opposite responses (reclaim leases vs. add funds). granted and locked disambiguate it.
    #
    # DELIBERATELY DIFFERENT METRIC NAMES from the smoke's. Two series sharing one name
    # across two jobs would both be scraped, and a rule taking max() over them would let a
    # stale HIGH reading mask a fresh low one — suppressing the alert that matters.
    if credit:
        acct = _escape_label_value(str(credit.get("account", "")))
        emitted = 0
        for metric, key, help_ in (
            (
                "akash_wallet_free_credit_usd",
                "free_usd",
                "Deploy credit available NOW. This is what gates the next deploy.",
            ),
            (
                "akash_wallet_granted_usd",
                "granted_usd",
                "Total Console grant. Nearly constant, and NOT the spendable figure.",
            ),
            (
                "akash_wallet_locked_in_escrow_usd",
                "locked_in_escrow_usd",
                "Grant currently held in escrow by live deployments.",
            ),
        ):
            v = credit.get(key)
            if _is_number(v):
                add(f"# HELP {metric} {help_}")
                add(f"# TYPE {metric} gauge")
                add(f'{metric}{{account="{acct}"}} {v}')
                emitted += 1
        # Only when a value actually went out, and stamped with when the CREDIT was read —
        # not with collection time. The balance step runs before the deploy step, which can
        # take minutes, so `now` would overstate freshness by exactly the interval that
        # matters when judging whether to trust the number.
        if emitted:
            add("# HELP akash_wallet_credit_timestamp_seconds Unix time the credit was READ.")
            add("# TYPE akash_wallet_credit_timestamp_seconds gauge")
            add(f"akash_wallet_credit_timestamp_seconds {credit_read_at or now:.0f}")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--targets", required=True, help='JSON: {"provider": {"uri": "http://...", "dseq": "123"}}'
    )
    ap.add_argument("--state", required=True, help="Durable state JSON (read+write)")
    ap.add_argument("--out", required=True, help="Exposition file to write")
    ap.add_argument("--timeout", type=float, default=SCRAPE_TIMEOUT)
    ap.add_argument("--credit", help="`balance --check --json` output to republish")
    a = ap.parse_args()

    targets = json.loads(pathlib.Path(a.targets).read_text(encoding="utf-8"))
    sp = pathlib.Path(a.state)
    state = load_json_mapping(sp)
    now = time.time()

    for provider, t in sorted(targets.items()):
        uri = t.get("uri") or ""
        ok, body, elapsed = (False, "", 0.0) if not uri else scrape(uri, a.timeout)
        state = merge(state, provider, str(t.get("dseq") or ""), ok, body, elapsed, now)
        print(
            f"{provider:14} reachable={int(ok)} "
            f"restarts={state[provider]['restarts_total']} "
            f"lease_replacements={state[provider]['lease_replacements_total']} "
            f"({elapsed:.2f}s)",
            flush=True,
        )

    sp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    credit_path = pathlib.Path(a.credit) if a.credit else None
    credit = load_json_mapping(credit_path) if credit_path else {}
    # The file's mtime IS the moment the balance was read — the workflow writes it
    # straight from `balance --check`. Using it needs no extra plumbing and cannot
    # drift from reality the way a hand-passed timestamp would.
    credit_read_at = None
    if credit_path is not None:
        try:
            credit_read_at = credit_path.stat().st_mtime
        except OSError:
            credit_read_at = None
    pathlib.Path(a.out).write_text(render(state, now, credit, credit_read_at), encoding="utf-8")
    # Exit 0 even when a canary is down: an unreachable provider is the DATA, and a
    # non-zero exit here would fail the workflow and stop the file being published —
    # losing the very measurement we came for.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
