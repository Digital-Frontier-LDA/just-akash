"""Tests for the persistent per-provider canary (canary/canary.py, canary/collect.py).

The bug these are really guarding against is a SILENT one. Every number here is derived —
restarts from a boot_id diff, reachability from a failed fetch, everything cumulative and
carried across runs. Get any of it subtly wrong and the canary still deploys, still
publishes a file, still looks healthy on a dashboard, and simply reports zero forever.
That is the same failure class the whole observability push exists to remove, so the
derivations are pinned here rather than trusted.
"""

from __future__ import annotations

import canary.canary as agent
from canary.collect import (
    extract_boot_id,
    merge,
    metrics_url,
    parse_exposition,
    render,
    scrape,
)
from canary.ensure import plan

ALPHA = "alphavps"


def _body(boot: str, *, uptime: float = 100.0, egress_ok: int = 10,
          egress_fail: int = 0, dns_ok: int = 10, dns_fail: int = 0) -> str:
    return (
        f'akash_canary_build_info{{version="1.0.0",provider="{ALPHA}",boot_id="{boot}"}} 1\n'
        f"akash_canary_uptime_seconds {uptime}\n"
        f'akash_canary_egress_probe_total{{outcome="ok"}} {egress_ok}\n'
        f'akash_canary_egress_probe_total{{outcome="fail"}} {egress_fail}\n'
        f'akash_canary_dns_probe_total{{outcome="ok"}} {dns_ok}\n'
        f'akash_canary_dns_probe_total{{outcome="fail"}} {dns_fail}\n'
        "akash_canary_disk_write_seconds 0.002\n"
        "akash_canary_sched_jitter_seconds 0.01\n"
    )


# ── the agent ───────────────────────────────────────────────────────────────────────────

def test_agent_exposition_parses_and_carries_boot_identity():
    """The agent's own output must be valid exposition AND carry boot_id — the collector
    cannot detect a restart without it."""
    text = agent.render_metrics()
    samples = parse_exposition(text)
    assert ("akash_canary_uptime_seconds", ()) in samples
    assert ("akash_canary_egress_probe_total", (("outcome", "ok"),)) in samples
    assert extract_boot_id(text) == agent.BOOT_ID


def test_agent_boot_id_is_stable_within_a_process():
    """Two scrapes of a running agent must NOT look like a restart."""
    assert extract_boot_id(agent.render_metrics()) == extract_boot_id(agent.render_metrics())


# ── restart vs redeploy: the distinction that makes the signal usable ────────────────────

def test_boot_id_change_on_same_lease_counts_as_a_restart():
    st = merge({}, ALPHA, "100", True, _body("aaa"), 0.1, 1000.0)
    assert st[ALPHA]["restarts_total"] == 0, "first sighting is not a restart"
    st = merge(st, ALPHA, "100", True, _body("bbb"), 0.1, 1100.0)
    assert st[ALPHA]["restarts_total"] == 1
    assert st[ALPHA]["lease_replacements_total"] == 0


def test_redeploy_is_not_counted_as_a_restart():
    """THE case that keeps the two signals apart. A replaced lease means a brand-new
    container, so its boot_id necessarily changes. Attributing that to a restart would
    inflate restarts every time a provider closed our deployment — making the two faults
    indistinguishable in exactly the situation where telling them apart matters."""
    st = merge({}, ALPHA, "100", True, _body("aaa"), 0.1, 1000.0)
    st = merge(st, ALPHA, "200", True, _body("zzz"), 0.1, 1100.0)
    assert st[ALPHA]["lease_replacements_total"] == 1
    assert st[ALPHA]["restarts_total"] == 0


def test_stable_boot_id_counts_nothing():
    st = merge({}, ALPHA, "100", True, _body("aaa"), 0.1, 1000.0)
    for i in range(5):
        st = merge(st, ALPHA, "100", True, _body("aaa"), 0.1, 1000.0 + i)
    assert st[ALPHA]["restarts_total"] == 0
    assert st[ALPHA]["checks_total"] == 6


# ── reachability: the customer-visible up/down ──────────────────────────────────────────

def test_unreachable_records_the_outage_without_inventing_a_restart():
    st = merge({}, ALPHA, "100", True, _body("aaa"), 0.1, 1000.0)
    st = merge(st, ALPHA, "100", False, "", 20.0, 1100.0)
    assert st[ALPHA]["reachable"] == 0
    assert st[ALPHA]["unreachable_checks_total"] == 1
    assert st[ALPHA]["restarts_total"] == 0, "a scrape failure is not evidence of a restart"
    assert st[ALPHA]["boot_id"] == "aaa", "last known boot_id must survive an outage"


def test_recovery_after_outage_with_a_new_boot_id_is_a_restart():
    """Down, then back with a different boot_id: the container died and came back. That
    IS a restart the customer's workload experienced, and it must be counted even though
    a scrape was missed in between."""
    st = merge({}, ALPHA, "100", True, _body("aaa"), 0.1, 1000.0)
    st = merge(st, ALPHA, "100", False, "", 20.0, 1100.0)
    st = merge(st, ALPHA, "100", True, _body("ccc", uptime=5.0), 0.1, 1200.0)
    assert st[ALPHA]["restarts_total"] == 1
    assert st[ALPHA]["reachable"] == 1


def test_counters_are_cumulative_across_runs():
    """Sampling on a workflow cadence must lose timing precision, never events."""
    st = {}
    for i in range(4):
        st = merge(st, ALPHA, "100", False, "", 1.0, 1000.0 + i)
    assert st[ALPHA]["unreachable_checks_total"] == 4
    assert st[ALPHA]["checks_total"] == 4


def test_inside_the_deployment_values_are_passed_through():
    st = merge({}, ALPHA, "100", True, _body("aaa", egress_fail=7, dns_fail=3), 0.1, 1000.0)
    assert st[ALPHA]["egress_fail"] == 7
    assert st[ALPHA]["dns_fail"] == 3
    assert st[ALPHA]["disk_write_seconds"] == 0.002


# ── the published file ──────────────────────────────────────────────────────────────────

def test_render_emits_per_provider_series_and_is_parseable():
    st = merge({}, ALPHA, "100", True, _body("aaa"), 0.1, 1000.0)
    st = merge(st, "onidc", "300", False, "", 20.0, 1000.0)
    out = render(st, 1234567890.0)
    samples = parse_exposition(out)
    assert samples[("akash_canary_reachable", (("provider", ALPHA),))] == 1.0
    assert samples[("akash_canary_reachable", (("provider", "onidc"),))] == 0.0
    assert ("akash_canary_last_collect_timestamp_seconds", ()) in samples


def test_render_omits_missing_gauges_rather_than_emitting_none():
    """A provider that has never been reachable has no inside-the-deployment values. It
    must be absent from those series, not published as a literal `None`, which would make
    the whole exposition file unparseable and take every other provider down with it."""
    st = merge({}, "onidc", "300", False, "", 20.0, 1000.0)
    out = render(st, 1.0)
    assert "None" not in out
    parse_exposition(out)  # must not raise
    assert 'akash_canary_reachable{provider="onidc"} 0' in out


# ── scrape() must never raise ───────────────────────────────────────────────────────────

def test_scrape_of_a_dead_endpoint_reports_rather_than_raises():
    """An unreachable canary is the measurement. If this raised, the collector would die
    on the first provider outage and publish nothing — losing the reading entirely."""
    ok, body, elapsed = scrape("http://127.0.0.1:1/", timeout=1.0)
    assert ok is False
    assert body == ""
    assert elapsed >= 0.0


def test_parse_exposition_ignores_comments_and_junk():
    text = "# HELP x y\n# TYPE x gauge\nx 1\nnot a metric line\ny{a=\"b\"} 2.5\n"
    s = parse_exposition(text)
    assert s[("x", ())] == 1.0
    assert s[("y", (("a", "b"),))] == 2.5


# ── ingress URI handling: the value is chosen by the PROVIDER ────────────────────────────

def test_bare_ingress_host_becomes_a_plain_http_metrics_url():
    """Akash lease status yields a bare host[:port] over plain http — the same shape
    just_akash.smoke_providers._ingress_uri returns. Assuming a full URL would make every
    scrape fail while the canary was perfectly healthy."""
    assert metrics_url("abc123.provider.example.com") == \
        "http://abc123.provider.example.com/metrics"
    assert metrics_url("host.example.com:8080") == "http://host.example.com:8080/metrics"


def test_full_url_is_accepted_for_local_testing():
    assert metrics_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080/metrics"
    assert metrics_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080/metrics"


def test_hostile_ingress_value_cannot_smuggle_a_scheme_or_path():
    """The provider chooses this string. Mirrors the guard in smoke_providers._fetch."""
    for bad in ("file:///etc/passwd", "host/../../evil", "host name", "host?x=1"):
        try:
            metrics_url(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have been rejected")


def test_malformed_host_is_reported_unreachable_not_raised():
    """One bad target must not take the whole collection down with it."""
    ok, body, _ = scrape("not a host", timeout=1.0)
    assert ok is False and body == ""


# ── ensure.plan: which providers still have a live canary ───────────────────────────────

def _dep(tag, dseq, uri=None, state="active"):
    d = {"name": tag, "dseq": dseq, "state": state}
    if uri:
        d["leases"] = [{"status": {"services": {"canary": {"uris": [uri]}}}}]
    return d


def test_plan_finds_live_canaries_and_flags_missing_ones():
    listing = [_dep("canary-alphavps", "100", "a.example.com"),
               _dep("canary-onidc", "200", "b.example.com")]
    targets, missing = plan(listing, ["alphavps", "onidc", "hetzner_hel"])
    assert targets["alphavps"] == {"uri": "a.example.com", "dseq": "100"}
    assert missing == ["hetzner_hel"]


def test_plan_ignores_untagged_deployments():
    """The smoke probes share this wallet and these images. Only the tag distinguishes a
    canary, so an untagged deployment must never be adopted as one."""
    targets, missing = plan([_dep("smoke-probe-xyz", "999", "x.example.com")], ["alphavps"])
    assert missing == ["alphavps"]
    assert "alphavps" not in targets


def test_closed_deployment_counts_as_missing():
    listing = [_dep("canary-alphavps", "100", "a.example.com", state="closed")]
    _, missing = plan(listing, ["alphavps"])
    assert missing == ["alphavps"]


def test_unknown_state_fails_open_rather_than_deploying_a_second_canary():
    """Mislabelling a live canary as missing makes the workflow open a SECOND lease on
    that provider. Two canaries reporting the same provider is worse than a late
    redeploy, so an unrecognised state must be treated as live."""
    listing = [{"name": "canary-alphavps", "dseq": "100", "state": "some-new-state",
                "leases": [{"status": {"services": {"c": {"uris": ["a.example.com"]}}}}]}]
    _, missing = plan(listing, ["alphavps"])
    assert missing == []


def test_missing_provider_keeps_its_previous_target():
    """So the collector records the outage against the right endpoint instead of dropping
    the provider from the exposition entirely."""
    prev = {"alphavps": {"uri": "old.example.com", "dseq": "50"}}
    targets, missing = plan([], ["alphavps"], prev)
    assert missing == ["alphavps"]
    assert targets["alphavps"]["uri"] == "old.example.com"


def test_live_canary_without_ingress_yet_keeps_previous_uri():
    """Ingress propagation lags a redeploy; blanking the uri would report a healthy
    provider as unreachable."""
    prev = {"alphavps": {"uri": "old.example.com", "dseq": "50"}}
    targets, _ = plan([_dep("canary-alphavps", "100")], ["alphavps"], prev)
    assert targets["alphavps"] == {"uri": "old.example.com", "dseq": "100"}


def test_plan_accepts_a_wrapped_listing_object():
    targets, _ = plan({"deployments": [_dep("canary-onidc", "7", "z.example.com")]}, ["onidc"])
    assert targets["onidc"]["dseq"] == "7"
