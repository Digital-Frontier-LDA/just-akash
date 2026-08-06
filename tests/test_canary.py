"""Tests for the persistent per-provider canary (canary/canary.py, canary/collect.py).

The bug these are really guarding against is a SILENT one. Every number here is derived —
restarts from a boot_id diff, reachability from a failed fetch, everything cumulative and
carried across runs. Get any of it subtly wrong and the canary still deploys, still
publishes a file, still looks healthy on a dashboard, and simply reports zero forever.
That is the same failure class the whole observability push exists to remove, so the
derivations are pinned here rather than trusted.
"""

from __future__ import annotations

import json

import canary.canary as agent
from canary.collect import (
    extract_boot_id,
    load_json_mapping,
    merge,
    metrics_url,
    parse_exposition,
    render,
    scrape,
)
from canary.details import fetch
from canary.ensure import load_details, name_for, plan, providers_from_env

ALPHA = "alphavps"

# Public provider addresses — the same three in .env.example / AKASH_PROVIDERS.
ADDR_ALPHAVPS = "akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z"  # pragma: allowlist secret
ADDR_ONIDC = "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk"  # pragma: allowlist secret
ADDR_HETZNER = "akash1z9nr23cgweu45g2jktfx95v7g2xp8qlsa3ys2x"  # pragma: allowlist secret


def _body(
    boot: str,
    *,
    uptime: float = 100.0,
    egress_ok: int = 10,
    egress_fail: int = 0,
    dns_ok: int = 10,
    dns_fail: int = 0,
) -> str:
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
    ok, body, elapsed = scrape("127.0.0.1:1", timeout=1.0)
    assert ok is False
    assert body == ""
    assert elapsed >= 0.0


def test_parse_exposition_ignores_comments_and_junk():
    text = '# HELP x y\n# TYPE x gauge\nx 1\nnot a metric line\ny{a="b"} 2.5\n'
    s = parse_exposition(text)
    assert s[("x", ())] == 1.0
    assert s[("y", (("a", "b"),))] == 2.5


# ── ingress URI handling: the value is chosen by the PROVIDER ────────────────────────────


def test_bare_ingress_host_becomes_a_plain_http_metrics_url():
    """Akash lease status yields a bare host[:port] over plain http — the same shape
    just_akash.smoke_providers._ingress_uri returns. Assuming a full URL would make every
    scrape fail while the canary was perfectly healthy."""
    assert (
        metrics_url("abc123.provider.example.com") == "http://abc123.provider.example.com/metrics"
    )
    assert metrics_url("host.example.com:8080") == "http://host.example.com:8080/metrics"


def test_full_urls_are_rejected_even_though_they_look_harmless():
    """An earlier revision accepted these "for local testing". That branch bypassed the
    bare-host guard entirely, and the value comes from the PROVIDER — so it was an SSRF
    hole dressed as a convenience. Local testing uses 127.0.0.1:8080, which takes the
    guarded path like everything else."""
    for u in ("http://127.0.0.1:8080", "https://evil.example.com/x"):
        try:
            metrics_url(u)
        except ValueError:
            continue
        raise AssertionError(f"{u!r} should have been rejected")
    assert metrics_url("127.0.0.1:8080") == "http://127.0.0.1:8080/metrics"


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


ALL_PAIRS = [
    (ALPHA, ADDR_ALPHAVPS),
    ("onidc", ADDR_ONIDC),
    ("hetzner_hel", ADDR_HETZNER),
]
ALPHA_ONLY = [(ALPHA, ADDR_ALPHAVPS)]


def _dep(addr, dseq, uri=None, state="active", services=("canary",)):
    """A deployment DETAIL, shaped like the API's — the only shape plan() ever sees.

    Both identifying facts hang off `leases`: the provider on `id.provider`, the service set
    on `status.services`. Neither is present on a `list --json` summary row, which is exactly
    the bug these tests now pin: ensure.py used to be fed those rows.
    """
    svc = {name: ({"uris": [uri]} if uri else {}) for name in services}
    lease = {"id": {"provider": addr}, "status": {"services": svc}}
    return {"dseq": dseq, "state": state, "leases": [lease]}


def test_plan_finds_live_canaries_and_flags_missing_ones():
    details = [
        _dep(ADDR_ALPHAVPS, "100", "a.example.com"),
        _dep(ADDR_ONIDC, "200", "b.example.com"),
    ]
    targets, missing, _ = plan(details, ALL_PAIRS)
    assert targets[ALPHA] == {"uri": "a.example.com", "dseq": "100"}
    assert missing == ["hetzner_hel"]


def test_plan_ignores_deployments_that_are_not_canaries():
    """The smoke probes share this wallet, these images, and these providers. The SERVICE
    SET is what separates them, so a probe must never be adopted as a canary."""
    details = [_dep(ADDR_ALPHAVPS, "999", "x.example.com", services=("probe",))]
    targets, missing, _ = plan(details, ALPHA_ONLY)
    assert missing == [ALPHA]
    assert ALPHA not in targets


def test_plan_ignores_a_canary_on_someone_elses_provider():
    """Attribution is by lease provider. A canary on a provider we no longer watch must not
    be credited to one we do — that would report a live endpoint for the wrong cluster."""
    details = [_dep("akash1someoneelse", "100", "a.example.com")]
    _, missing, _ = plan(details, ALPHA_ONLY)
    assert missing == [ALPHA]


def test_closed_deployment_counts_as_missing():
    details = [_dep(ADDR_ALPHAVPS, "100", "a.example.com", state="closed")]
    _, missing, _ = plan(details, ALPHA_ONLY)
    assert missing == [ALPHA]


def test_unknown_state_fails_open_rather_than_deploying_a_second_canary():
    """Mislabelling a live canary as missing makes the workflow open a SECOND lease on
    that provider. Two canaries reporting the same provider is worse than a late
    redeploy, so an unrecognised state must be treated as live."""
    details = [_dep(ADDR_ALPHAVPS, "100", "a.example.com", state="some-new-state")]
    _, missing, _ = plan(details, ALPHA_ONLY)
    assert missing == []


def test_missing_provider_keeps_its_previous_target():
    """So the collector records the outage against the right endpoint instead of dropping
    the provider from the exposition entirely."""
    prev = {ALPHA: {"uri": "old.example.com", "dseq": "50"}}
    targets, missing, _ = plan([], ALPHA_ONLY, prev)
    assert missing == [ALPHA]
    assert targets[ALPHA]["uri"] == "old.example.com"


def test_live_canary_without_ingress_yet_keeps_previous_uri():
    """Ingress propagation lags a redeploy; blanking the uri would report a healthy
    provider as unreachable."""
    prev = {ALPHA: {"uri": "old.example.com", "dseq": "50"}}
    targets, _, _ = plan([_dep(ADDR_ALPHAVPS, "100")], ALPHA_ONLY, prev)
    assert targets[ALPHA] == {"uri": "old.example.com", "dseq": "100"}


def test_plan_accepts_a_wrapped_details_object():
    details = {"deployments": [_dep(ADDR_ONIDC, "7", "z.example.com")]}
    targets, _, _ = plan(details, [("onidc", ADDR_ONIDC)])
    assert targets["onidc"]["dseq"] == "7"


# ── the tag bug: local state cannot identify a lease from an ephemeral runner ────────────


def test_a_tagged_deployment_is_not_required_to_be_recognised():
    """THE REGRESSION THIS FIX EXISTS FOR. Matching used to be on `just-akash tag` names,
    which live in .tags.json in the working copy — never on chain, never in the API. A
    GitHub runner is wiped after each job, so by the next run the tag was gone, every
    provider reported NEEDS DEPLOY, and autodeploy would have opened three leases every
    thirty minutes forever. Identity must come off the deployment, with no tag in sight."""
    details = [_dep(ADDR_ALPHAVPS, "100", "a.example.com")]
    assert "name" not in details[0] and "tag" not in details[0]
    targets, missing, _ = plan(details, ALPHA_ONLY)
    assert missing == []
    assert targets[ALPHA]["dseq"] == "100"


def test_a_starting_canary_is_not_reported_missing():
    """A provider populates status.services only once the workload is running, so a canary
    reads as an empty service set for its first minutes. Calling that 'no canary' is how a
    second lease gets opened on top of the first."""
    details = [_dep(ADDR_ALPHAVPS, "100", services=())]
    _, missing, notes = plan(details, ALPHA_ONLY)
    assert missing == []
    assert any("no services yet" in n for n in notes)


def test_incomplete_details_never_report_anything_missing():
    """A failed API read must not read as an empty account. Deploying on 'could not look'
    duplicates every canary at once — and a canary matches no reaper, so nothing sweeps the
    duplicates up."""
    _, missing, notes = plan({"complete": False, "deployments": []}, ALL_PAIRS)
    assert missing == []
    assert any("INCOMPLETE" in n for n in notes)


def test_complete_details_do_report_missing():
    """The guard above must not be so broad it never deploys: a document that read cleanly
    and found nothing is a real answer."""
    _, missing, _ = plan({"complete": True, "deployments": []}, ALPHA_ONLY)
    assert missing == [ALPHA]


def test_duplicate_canaries_report_the_newest_and_say_so():
    """dseqs are ms epochs, so the largest is newest. The older one bills until a human
    closes it, which is why it gets named rather than silently ignored."""
    details = [
        _dep(ADDR_ALPHAVPS, "1786017183151", "old.example.com"),
        _dep(ADDR_ALPHAVPS, "1786020000000", "new.example.com"),
    ]
    targets, missing, notes = plan(details, ALPHA_ONLY)
    assert targets[ALPHA]["dseq"] == "1786020000000"
    assert missing == []
    assert any("1786017183151" in n and "by hand" in n for n in notes)


# ── the two data-integrity bugs the review caught ───────────────────────────────────────


def test_redeploy_observed_during_an_outage_is_still_a_lease_replacement():
    """The realistic sequence, and the one an earlier revision got wrong.

    A lease lapses; the workflow redeploys; the collector runs while the NEW container is
    still coming up, so the scrape fails but the targets file already carries the new
    dseq. If that dseq were recorded during the outage, the eventual boot_id change would
    look like a restart on an unchanged lease — booking a provider-closed deployment as a
    container restart, which is precisely the conflation this design exists to prevent.
    """
    st = merge({}, ALPHA, "100", True, _body("aaa"), 0.1, 1000.0)
    st = merge(st, ALPHA, "200", False, "", 20.0, 1100.0)  # redeployed, not up yet
    assert st[ALPHA]["dseq"] == "100", "dseq must not advance on an unverified lease"
    st = merge(st, ALPHA, "200", True, _body("zzz"), 0.1, 1200.0)  # new container answers
    assert st[ALPHA]["lease_replacements_total"] == 1
    assert st[ALPHA]["restarts_total"] == 0


def test_egress_failures_survive_a_restart_instead_of_being_discarded():
    """The agent's counters are process-local and reset to zero on restart. Passing them
    through would publish a *_total that decreases and would throw away every failure from
    the previous process lifetime."""
    st = merge({}, ALPHA, "100", True, _body("aaa", egress_fail=5), 0.1, 1000.0)
    assert st[ALPHA]["egress_fail"] == 5
    # restart: the agent's own counter is back to 0 and climbs again to 2
    st = merge(st, ALPHA, "100", True, _body("bbb", egress_fail=2), 0.1, 1100.0)
    assert st[ALPHA]["restarts_total"] == 1
    assert st[ALPHA]["egress_fail"] == 7, "5 before the restart + 2 after, not 2"


def test_counter_accumulation_is_monotonic_across_normal_scrapes():
    st = merge({}, ALPHA, "100", True, _body("aaa", egress_fail=1), 0.1, 1000.0)
    st = merge(st, ALPHA, "100", True, _body("aaa", egress_fail=4), 0.1, 1100.0)
    st = merge(st, ALPHA, "100", True, _body("aaa", egress_fail=9), 0.1, 1200.0)
    assert st[ALPHA]["egress_fail"] == 9, "same process: track the raw value, do not sum it"


# ── provider config comes from AKASH_PROVIDERS, not a canary-specific copy ───────────────


def test_known_provider_addresses_resolve_to_fleet_names():
    """The same three addresses AKASH_PROVIDERS carries, df-grafana label_replaces into
    cluster names, and the autobidder dashboards pin."""
    assert name_for(ADDR_ALPHAVPS) == "alphavps"
    assert name_for(ADDR_ONIDC) == "onidc"
    assert name_for(ADDR_HETZNER) == "hetzner_hel"


def test_unknown_provider_gets_an_ugly_label_rather_than_being_dropped():
    """Adding a fourth provider to AKASH_PROVIDERS must not silently exclude it from the
    canary. An ugly label is a visible prompt to name it; a silent omission is a provider
    nobody is watching."""
    label = name_for("akash1newprovideraddress0000000000000000ab")
    assert label.startswith("akash1new") and label.endswith("0000ab")


def test_two_unknown_providers_never_collide_on_one_label():
    """Every Akash address starts `akash1`, so a plain prefix truncation leaves only a few
    distinguishing characters. A collision here is silently destructive, not just ugly:
    plan() and the targets file are keyed by this name, so two providers would fold into
    one entry and one would go unwatched — the exact outcome the fallback exists to
    prevent. These two share their first 30 characters."""
    a = name_for("akash1qqqqqqqqqqqqqqqqqqqqqqqqqqqqq7h9dx4")
    b = name_for("akash1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqk2m5wz")
    assert a != b


def test_providers_parse_from_the_real_akash_providers_format():
    pairs = providers_from_env(f"{ADDR_ONIDC},{ADDR_ALPHAVPS},{ADDR_HETZNER}")
    assert [n for n, _ in pairs] == ["onidc", "alphavps", "hetzner_hel"]
    assert all(a.startswith("akash1") for _, a in pairs)


def test_blank_entries_and_whitespace_are_tolerated():
    """A trailing comma in a SOPS-managed env value must not create a phantom provider."""
    pairs = providers_from_env(f" {ADDR_ALPHAVPS} , ,")
    assert pairs == [("alphavps", ADDR_ALPHAVPS)]


def test_a_repeated_address_does_not_create_two_leases():
    """AKASH_PROVIDERS is a hand-maintained comma-separated string in a SOPS file, so a
    repeated address is an ordinary copy-paste slip. Un-deduplicated it would put the
    provider in `missing` twice, run the deploy loop twice, and open TWO leases on one
    provider — paying twice to watch the same thing."""
    addr = ADDR_ALPHAVPS
    assert providers_from_env(f"{addr},{addr}") == [("alphavps", addr)]


def test_duplicate_addresses_do_not_produce_duplicate_plan_entries():
    addr = ADDR_ALPHAVPS
    pairs = providers_from_env(f"{addr},{addr},{addr}")
    _, missing, _ = plan([], pairs)
    assert missing == ["alphavps"], "one provider, one deploy"


# ── state files that are missing, empty or corrupt ──────────────────────────────────────


def test_empty_state_file_is_treated_as_no_prior_state(tmp_path):
    """`git show BRANCH:file > out` creates `out` BEFORE the command runs, so a first run —
    where the telemetry branch has no such file — leaves a zero-byte file rather than none.
    json.loads("") then raises and takes the whole run down, which is exactly how the first
    live dispatch failed."""
    p = tmp_path / "targets.json"
    p.write_text("", encoding="utf-8")
    assert load_json_mapping(p) == {}


def test_missing_state_file_is_treated_as_no_prior_state(tmp_path):
    assert load_json_mapping(tmp_path / "nope.json") == {}


def test_corrupt_state_file_degrades_instead_of_crashing(tmp_path):
    """An unreadable state file must degrade to 'no prior state', never to a crash: the
    collector exists to publish a reading, and refusing to run because its own bookkeeping
    is unparseable loses the measurement."""
    p = tmp_path / "state.json"
    p.write_text("{not json at all", encoding="utf-8")
    assert load_json_mapping(p) == {}


def test_a_json_list_is_not_mistaken_for_a_mapping(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_json_mapping(p) == {}


def test_a_real_mapping_is_returned_unchanged(tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"alphavps": {"dseq": "1"}}', encoding="utf-8")
    assert load_json_mapping(p) == {"alphavps": {"dseq": "1"}}


def test_binary_state_file_degrades_instead_of_crashing(tmp_path):
    """UnicodeDecodeError is NOT an OSError, so it escapes an OSError-only guard. A
    truncated or binary state file — an interrupted write, a git read that produced junk —
    raises it from read_text rather than from json, which is the corruption case this
    helper most plausibly meets."""
    p = tmp_path / "state.json"
    p.write_bytes(b"\xff\xfe\x00\x01binary garbage\x80\x81")
    assert load_json_mapping(p) == {}


def test_both_modules_share_one_implementation():
    """Two copies of a 'degrade, never crash' helper drift: one gains an exception class the
    other lacks, and the narrower copy starts dying on files the other tolerates."""
    import canary.collect as collect
    import canary.ensure as ensure

    assert ensure.load_json_mapping is collect.load_json_mapping


# ── wallet credit republishing ──────────────────────────────────────────────────────────

CREDIT = {
    "check": "deploy_credit",
    "status": "OK",
    "account": "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee",  # pragma: allowlist secret
    "deploy_credit_usd": 81.37,
    "free_usd": 81.37,
    "granted_usd": 154.33,
    "locked_in_escrow_usd": 72.95,
    "min_usd": 25.0,
}


def test_credit_is_republished_with_the_three_components():
    """free / granted / locked are different questions and the wallet's behaviour is only
    legible with all three: a flat grant with rising escrow looks identical to a draining
    wallet if you only plot the free figure."""
    out = render({}, 1.0, CREDIT)
    s = parse_exposition(out)
    acct = (("account", CREDIT["account"]),)
    assert s[("akash_wallet_free_credit_usd", acct)] == 81.37
    assert s[("akash_wallet_granted_usd", acct)] == 154.33
    assert s[("akash_wallet_locked_in_escrow_usd", acct)] == 72.95
    assert ("akash_wallet_credit_timestamp_seconds", ()) in s


def test_credit_metrics_do_not_reuse_the_smoke_metric_name():
    """Two series with one name across two jobs would BOTH be scraped, and df-grafana's
    rule takes max(just_akash_deploy_credit_usd) — a stale HIGH reading would mask a fresh
    low one and suppress exactly the alert that matters."""
    assert "just_akash_deploy_credit_usd" not in render({}, 1.0, CREDIT)


def test_no_credit_file_means_no_credit_series_rather_than_zeros():
    """A missing reading must be ABSENT, not published as 0 — a zero would read as an empty
    wallet and fire the low-credit alert on a measurement gap."""
    out = render({}, 1.0, {})
    assert "akash_wallet_" not in out


def test_partial_credit_json_emits_only_what_it_has():
    out = render({}, 1.0, {"account": "akash1x", "free_usd": 10.0})
    assert "akash_wallet_free_credit_usd" in out
    assert "akash_wallet_granted_usd" not in out


def test_boolean_credit_values_are_not_published_as_numbers():
    """A bool IS an int in Python, so a stray `true` would publish as `1` — a wallet
    reading of $1. The repo's _is_number excludes bool for exactly this reason."""
    out = render({}, 1.0, {"account": "akash1x", "free_usd": True, "granted_usd": 5.0})
    assert "akash_wallet_free_credit_usd" not in out
    assert "akash_wallet_granted_usd" in out


def test_account_label_is_escaped():
    """The account comes from JSON. An unescaped quote would produce a malformed line and
    make the WHOLE exposition unparseable, taking every other series down with it.

    Built with chr() rather than escape sequences: counting backslashes across a Python
    literal, a test file and an exposition line is how you write an assertion that passes
    for the wrong reason.
    """
    bs, q = chr(92), chr(34)
    raw = "a" + q + "b" + bs + "c"  # a"b\c
    expected = "account=" + q + "a" + bs + q + "b" + bs + bs + "c" + q
    out = render({}, 1.0, {"account": raw, "free_usd": 1.0})
    assert expected in out
    parse_exposition(out)  # must still parse


def test_timestamp_is_omitted_when_no_credit_value_was_emitted():
    """A mapping with only status fields must not publish a freshness stamp for data that
    was never published — that would assert a reading exists when none does."""
    out = render({}, 1.0, {"status": "OK", "check": "deploy_credit"})
    assert "akash_wallet_credit_timestamp_seconds" not in out


def test_timestamp_reports_when_credit_was_read_not_when_collected():
    """The balance step runs before the deploy step, which can take minutes. Stamping with
    collection time would overstate freshness by exactly the interval that matters."""
    out = render({}, 9_999.0, {"account": "a", "free_usd": 1.0}, credit_read_at=1_000.0)
    s = parse_exposition(out)
    assert s[("akash_wallet_credit_timestamp_seconds", ())] == 1000.0


# ── details.fetch: turning summary rows into the detail plan() actually needs ────────────


class _FakeClient:
    """Answers get_deployment from a dict; anything absent raises, as the API would."""

    def __init__(self, by_dseq):
        self.by_dseq = by_dseq
        self.asked = []

    def get_deployment(self, dseq):
        self.asked.append(dseq)
        if dseq not in self.by_dseq:
            raise RuntimeError(f"no such deployment {dseq}")
        return self.by_dseq[dseq]


def test_fetch_expands_every_row_into_a_detail():
    """The whole point: list rows carry no leases, details do. plan() reads leases."""
    client = _FakeClient({"100": _dep(ADDR_ALPHAVPS, "100", "a.example.com")})
    details, errors = fetch(client, [{"dseq": "100"}])
    assert errors == []
    assert details[0]["leases"][0]["id"]["provider"] == ADDR_ALPHAVPS


def test_one_unreadable_deployment_does_not_lose_the_others():
    client = _FakeClient({"100": _dep(ADDR_ALPHAVPS, "100", "a.example.com")})
    details, errors = fetch(client, [{"dseq": "100"}, {"dseq": "404"}])
    assert len(details) == 1
    assert len(errors) == 1 and "404" in errors[0]


def test_a_failed_read_makes_the_document_incomplete_so_nothing_deploys():
    """The two halves joined up: a fetch error must reach plan() as `complete: false`, or
    the failure silently reads as an empty account and every canary is deployed again."""
    client = _FakeClient({})
    details, errors = fetch(client, [{"dseq": "404"}])
    doc = {"complete": not errors, "deployments": details}
    _, missing, notes = plan(doc, ALL_PAIRS)
    assert missing == [], "an unreadable API must never authorise spending"
    assert any("INCOMPLETE" in n for n in notes)


def test_a_row_without_a_dseq_counts_as_an_error_not_a_skip():
    """Silently dropping a row is how a live canary becomes invisible — and invisible reads
    as missing, which spends money."""
    _, errors = fetch(_FakeClient({}), [{"no": "dseq"}])
    assert len(errors) == 1 and "without a dseq" in errors[0]


def test_an_empty_detail_response_is_an_error_not_a_deployment():
    """get_deployment returns {} on an unrecognised envelope rather than raising. Treating
    that as a real deployment would put an unidentifiable entry into the picture."""
    details, errors = fetch(_FakeClient({"100": {}}), [{"dseq": "100"}])
    assert details == []
    assert len(errors) == 1 and "empty detail" in errors[0]


# ── the details document must be self-identifying ───────────────────────────────────────


def _write(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_a_listing_passed_as_details_is_refused(tmp_path):
    """listing.json and details.json sit side by side in the workflow and both parse as
    JSON. A listing has no leases, so plan() would match nothing, call every provider
    missing, and deploy a duplicate canary onto each — the exact failure this module was
    rewritten to remove. Wiring the wrong filename must stop the run, not proceed."""
    p = _write(tmp_path, "listing.json", [{"dseq": "100", "state": "active"}])
    try:
        load_details(p)
    except ValueError as exc:
        assert "not a details document" in str(exc)
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("a bare listing must be refused, not accepted as complete")


def test_a_real_details_document_loads(tmp_path):
    p = _write(tmp_path, "details.json", {"complete": True, "deployments": []})
    assert load_details(p) == {"complete": True, "deployments": []}


def test_an_incomplete_details_document_still_loads(tmp_path):
    """Refusal is about the SHAPE being unrecognisable. `complete: false` is a valid
    document making a true statement, and plan() needs to see it to suppress deploying."""
    p = _write(tmp_path, "details.json", {"complete": False, "deployments": []})
    assert load_details(p)["complete"] is False
