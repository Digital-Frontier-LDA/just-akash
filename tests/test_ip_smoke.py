"""Unit tests for the leased-IP delivery check (#244).

Everything here runs against fixture payloads — no lease, no escrow, no
network. The one step these cannot prove is the live run itself; everything
that decides what the live run MEANS is decided here.

The bar these are written to: each test should fail if the specific defect it
describes were reintroduced. Several of them exist because the corresponding
mistake was actually made somewhere in this estate this week.
"""

from __future__ import annotations

import json

from just_akash.ip_smoke import (
    IP_LEDGER_SCHEMA,
    IP_SDL,
    OUTCOME_CHURNED,
    OUTCOME_FAIL,
    OUTCOME_PASS,
    IpProbeResult,
    LeasedIP,
    build_ledger_record,
    classify,
    extract_leased_ips,
    ip_in_pool,
    pool_for_cluster,
    write_ledger,
)

# The shape Akash-Console reads (LeaseRow.tsx renders http://{IP}:{ExternalPort}).
LEASE_STATUS_WITH_IP = {
    "services": {"probe": {"name": "probe", "available": 1, "total": 1, "uris": []}},
    "forwarded_ports": {},
    "ips": {
        "probe": [{"IP": "213.58.173.241", "ExternalPort": 80, "Port": 80, "Protocol": "TCP"}]
    },
}

# What a wedged ip-operator produces: the service runs, no address is assigned.
LEASE_STATUS_NO_IP = {
    "services": {"probe": {"name": "probe", "available": 1, "total": 1, "uris": []}},
    "forwarded_ports": {},
    "ips": {},
}


# ── the SDL is shared, not copied ────────────────────────────────────────


class TestSharedSdl:
    def test_ip_sdl_is_the_bid_probe_definition(self):
        """#244 asks for one definition and two consumers. If this module ever
        grows its own copy, bid-probe and the smoke stop asking about the same
        order and their answers stop being comparable."""
        from just_akash.bid_probe import _SDL_IP_LEASE

        assert IP_SDL is _SDL_IP_LEASE

    def test_the_sdl_actually_requests_a_leased_ip(self):
        """Guards the whole premise. An SDL without `kind: ip` produces a
        shared-ingress endpoint, and the stage would prove nothing while
        passing — the exact shape of a check that cannot fail."""
        assert "kind: ip" in IP_SDL
        assert "ip: web" in IP_SDL


# ── reading the address back ─────────────────────────────────────────────


class TestExtractLeasedIps:
    def test_reads_ip_and_external_port(self):
        got = extract_leased_ips(LEASE_STATUS_WITH_IP)
        assert len(got) == 1
        assert got[0].ip == "213.58.173.241"
        assert got[0].external_port == 80
        assert got[0].url == "http://213.58.173.241:80"

    def test_external_port_is_read_not_assumed(self):
        """THE parsing defect worth guarding. The SDL asks for `as: 80`, but the
        console reads ExternalPort back rather than trusting the manifest. A
        probe that hardcoded 80 would curl the wrong port the day a remap
        happens and report a working IP as broken."""
        remapped = {"ips": {"probe": [{"IP": "10.0.0.5", "ExternalPort": 31234}]}}
        got = extract_leased_ips(remapped)
        assert got[0].external_port == 31234
        assert got[0].url == "http://10.0.0.5:31234"
        assert ":80" not in got[0].url

    def test_no_ips_is_empty_not_an_error(self):
        """The wedged-operator shape. It must parse cleanly to [] so the caller
        can report a specific failure, rather than raising and losing the
        payload that proves what happened."""
        assert extract_leased_ips(LEASE_STATUS_NO_IP) == []

    def test_missing_ips_key_entirely(self):
        assert extract_leased_ips({"services": {}, "forwarded_ports": {}}) == []

    def test_falls_back_to_any_service_carrying_an_ip(self):
        """A renamed service in the SDL must not report 'no IP delivered' when
        an IP WAS delivered — that would be a false outage."""
        renamed = {"ips": {"web-svc": [{"IP": "1.2.3.4", "ExternalPort": 8080}]}}
        got = extract_leased_ips(renamed)
        assert len(got) == 1 and got[0].ip == "1.2.3.4"
        assert got[0].service == "web-svc"

    def test_accepts_lowercase_key_spellings(self):
        got = extract_leased_ips({"ips": {"probe": [{"ip": "1.2.3.4", "external_port": 90}]}})
        assert got[0].ip == "1.2.3.4" and got[0].external_port == 90

    def test_malformed_entries_are_skipped_not_fatal(self):
        """A provider answering in a shape we did not expect must degrade to
        'no IPs found', never a traceback."""
        messy = {
            "ips": {
                "probe": [
                    "not-a-dict",
                    {"IP": "", "ExternalPort": 80},
                    {"ExternalPort": 80},
                    {"IP": "1.2.3.4", "ExternalPort": "not-an-int"},
                    {"IP": "9.9.9.9", "ExternalPort": 80},
                ]
            }
        }
        got = extract_leased_ips(messy)
        assert [g.ip for g in got] == ["9.9.9.9"]

    def test_never_raises_on_any_junk(self):
        for junk in [None, [], "", 42, {"ips": "nope"}, {"ips": {"probe": "nope"}}]:
            assert extract_leased_ips(junk) == []


# ── pool conformance ─────────────────────────────────────────────────────


class TestPoolConformance:
    def test_onidc_pool_is_the_full_28(self):
        assert pool_for_cluster("onidc") == "213.58.173.240/28"

    def test_overrides_win(self):
        assert pool_for_cluster("onidc", {"onidc": "10.0.0.0/24"}) == "10.0.0.0/24"

    def test_unknown_cluster_has_no_pool(self):
        assert pool_for_cluster("hetzner_hel") is None

    def test_address_inside_and_outside(self):
        assert ip_in_pool("213.58.173.241", "213.58.173.240/28") is True
        assert ip_in_pool("213.58.173.249", "213.58.173.240/28") is True  # ours too
        assert ip_in_pool("8.8.8.8", "213.58.173.240/28") is False

    def test_unknowable_is_none_not_false(self):
        """Three-valued on purpose: 'cannot check' must never be recorded as
        'checked and failed', or an undeclared pool would read as a violation."""
        assert ip_in_pool("1.2.3.4", None) is None
        assert ip_in_pool("not-an-ip", "213.58.173.240/28") is None
        assert ip_in_pool("1.2.3.4", "not-a-cidr") is None
        assert ip_in_pool("", "213.58.173.240/28") is None


# ── outcome classification ───────────────────────────────────────────────


class TestClassify:
    def test_happy_path(self):
        outcome, _ = classify(
            ips=[LeasedIP("213.58.173.241", 80)],
            reachable=True,
            in_pool=True,
            lease_state="active",
        )
        assert outcome == OUTCOME_PASS

    def test_churn_is_not_failure(self):
        """dseq 1788531162 went active->closed within hours on the day #244 was
        written. Reporting that as FAIL would make a healthy provider look
        broken every time a lease turns over."""
        outcome, reason = classify(ips=[], reachable=None, in_pool=None, lease_state="closed")
        assert outcome == OUTCOME_CHURNED
        assert "closed" in reason

    def test_churn_wins_over_every_other_signal(self):
        """A closed lease invalidates the downstream assertions rather than
        merely explaining one, so it is checked first."""
        outcome, _ = classify(
            ips=[LeasedIP("8.8.8.8", 80)],
            reachable=False,
            in_pool=False,
            lease_state="CLOSED",
        )
        assert outcome == OUTCOME_CHURNED

    def test_no_ip_assigned_is_the_wedged_operator_shape(self):
        outcome, reason = classify(ips=[], reachable=None, in_pool=None, lease_state="active")
        assert outcome == OUTCOME_FAIL
        assert "no IP assigned" in reason

    def test_assigned_but_unreachable_fails(self):
        """The failure mode that ONLY an outside-in curl catches: healthy in
        every provider-side field, and does not route."""
        outcome, reason = classify(
            ips=[LeasedIP("213.58.173.241", 80)],
            reachable=False,
            in_pool=True,
            lease_state="active",
        )
        assert outcome == OUTCOME_FAIL
        assert "not reachable" in reason.lower() or "does not route" in reason.lower()

    def test_out_of_pool_fails_even_when_reachable(self):
        outcome, reason = classify(
            ips=[LeasedIP("8.8.8.8", 80)],
            reachable=True,
            in_pool=False,
            lease_state="active",
        )
        assert outcome == OUTCOME_FAIL
        assert "pool" in reason

    def test_unknown_reachability_is_never_a_pass(self):
        """'We could not test it' must not be recorded as 'it works'. Rule 2 of
        this estate's hard-won lesson set."""
        outcome, _ = classify(
            ips=[LeasedIP("1.2.3.4", 80)],
            reachable=None,
            in_pool=True,
            lease_state="active",
        )
        assert outcome == OUTCOME_FAIL

    def test_undeclared_pool_does_not_block_a_pass(self):
        """No declared pool means we cannot check conformance — but the
        reachability assertion still held, and that is the load-bearing one."""
        outcome, _ = classify(
            ips=[LeasedIP("1.2.3.4", 80)],
            reachable=True,
            in_pool=None,
            lease_state="active",
        )
        assert outcome == OUTCOME_PASS


# ── the hand-off ledger ──────────────────────────────────────────────────


class TestLedger:
    def _result(self) -> IpProbeResult:
        return IpProbeResult(
            provider="akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk",
            cluster="onidc",
            dseq="1788999999999",
            outcome=OUTCOME_PASS,
            reason="ok",
            assigned_ip="213.58.173.241",
            external_port=80,
            in_pool=True,
            pool_cidr="213.58.173.240/28",
            reachable=True,
            http_status=200,
            lease_created_at="2026-09-04T20:00:00+00:00",
        )

    def test_record_carries_lease_created_at(self):
        """THE seam field. Without it the delayed visibility job cannot tell
        'the pricing row has not landed yet' from 'the pipeline dropped it' —
        which is the same indistinguishable-failure defect #244 exists to close,
        just moved to the other half."""
        rec = build_ledger_record(self._result(), run_ts="T", version="v")
        assert rec["lease_created_at"] == "2026-09-04T20:00:00+00:00"
        assert rec["dseq"] == "1788999999999"

    def test_record_is_schema_versioned(self):
        """So a consumer refuses a shape it does not understand rather than
        matching zero rows and reporting green on an empty read."""
        rec = build_ledger_record(self._result(), run_ts="T", version="v")
        assert rec["schema"] == IP_LEDGER_SCHEMA

    def test_record_states_what_the_delayed_job_must_find(self):
        """Stated once, here, rather than re-derived in the other half — two
        copies of an expectation drift."""
        rec = build_ledger_record(self._result(), run_ts="T", version="v")
        assert rec["expect_resource_spec_ip_gt"] == 0
        assert rec["expect_component_prices_ip_gt"] == 0

    def test_record_is_json_serialisable(self):
        json.dumps(build_ledger_record(self._result(), run_ts="T", version="v"))

    def test_write_reports_whether_it_wrote(self, tmp_path):
        p = tmp_path / "nested" / "ip-ledger.jsonl"
        rec = build_ledger_record(self._result(), run_ts="T", version="v")
        assert write_ledger(str(p), rec) is True
        assert json.loads(p.read_text().strip())["dseq"] == "1788999999999"

    def test_write_appends_rather_than_truncates(self, tmp_path):
        p = tmp_path / "ip-ledger.jsonl"
        rec = build_ledger_record(self._result(), run_ts="T", version="v")
        write_ledger(str(p), rec)
        write_ledger(str(p), rec)
        assert len(p.read_text().strip().splitlines()) == 2

    def test_no_path_returns_false_not_silence(self):
        """A ledger that quietly stops recording leaves the delayed job green
        forever on empty input. The caller must be able to SAY it did not
        happen, so this returns a bool rather than swallowing."""
        assert write_ledger(None, {"a": 1}) is False

    def test_unwritable_path_returns_false_and_does_not_raise(self, tmp_path):
        blocker = tmp_path / "afile"
        blocker.write_text("x")
        assert write_ledger(str(blocker / "sub" / "l.jsonl"), {"a": 1}) is False


# ── the last uncovered branches ──────────────────────────────────────────


class TestParsingEdges:
    def test_unparseable_inner_port_degrades_without_losing_the_ip(self):
        """`Port` is context, `ExternalPort` is the one we curl. A junk inner
        port must not discard an otherwise-usable address — that would turn a
        cosmetic field into a false outage."""
        got = extract_leased_ips(
            {"ips": {"probe": [{"IP": "1.2.3.4", "ExternalPort": 80, "Port": "junk"}]}}
        )
        assert len(got) == 1
        assert got[0].ip == "1.2.3.4" and got[0].external_port == 80
        assert got[0].port is None

    def test_utc_now_iso_is_tz_aware(self):
        """The delayed job compares lease_created_at against its own clock. A
        naive timestamp would make that comparison silently wrong by whatever
        the runner's offset happens to be."""
        from just_akash.ip_smoke import utc_now_iso

        ts = utc_now_iso()
        from datetime import datetime

        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None
        offset = parsed.utcoffset()
        assert offset is not None
        assert offset.total_seconds() == 0


# ── the stage: orchestration + the no-leak guarantee ─────────────────────


from just_akash.ip_smoke import run_ip_stage  # noqa: E402


class _Deps:
    """Injected fakes. Records what was called so cleanup can be asserted."""

    def __init__(self, *, status=None, state="active", reachable=(True, 200), raise_on=None):
        self.status = status if status is not None else LEASE_STATUS_WITH_IP
        self.state = state
        self.reachable = reachable
        self.raise_on = raise_on
        self.destroyed: list[str] = []
        self.curled: list[str] = []

    def deploy_and_lease(self):
        if self.raise_on == "deploy":
            raise RuntimeError("deploy blew up")
        return "1788000000001", "2026-09-04T20:00:00+00:00"

    def fetch_lease_status(self, dseq):
        if self.raise_on == "status":
            raise RuntimeError("status blew up")
        return self.status

    def fetch_lease_state(self, dseq):
        return self.state

    def curl(self, url):
        if self.raise_on == "curl":
            raise RuntimeError("curl blew up")
        self.curled.append(url)
        return self.reachable

    def destroy(self, dseq):
        if self.raise_on == "destroy":
            raise RuntimeError("destroy blew up")
        self.destroyed.append(dseq)

    def run(self, **kw):
        return run_ip_stage(
            provider="akash1prov",
            cluster="onidc",
            deploy_and_lease=self.deploy_and_lease,
            fetch_lease_status=self.fetch_lease_status,
            fetch_lease_state=self.fetch_lease_state,
            curl=self.curl,
            destroy=self.destroy,
            **kw,
        )


class TestStageHappyPath:
    def test_pass_and_curls_the_external_port(self):
        d = _Deps()
        r = d.run()
        assert r.outcome == OUTCOME_PASS
        assert r.assigned_ip == "213.58.173.241"
        assert d.curled == ["http://213.58.173.241:80"]
        assert d.destroyed == ["1788000000001"]

    def test_happy_path_does_not_query_the_chain(self):
        """Reading on-chain state costs a round trip and only explains an
        anomaly. Asking on a clean pass would spend it to learn nothing."""
        d = _Deps()
        r = d.run()
        assert r.lease_state_at_failure is None


class TestStageFailureModes:
    def test_wedged_operator_no_ip(self):
        d = _Deps(status=LEASE_STATUS_NO_IP)
        r = d.run()
        assert r.outcome == OUTCOME_FAIL
        assert "no IP assigned" in r.reason
        assert d.curled == []  # nothing to reach; reachability not faked
        assert r.reachable is None  # not measured, not "failed"
        assert d.destroyed == ["1788000000001"]

    def test_assigned_but_unroutable(self):
        d = _Deps(reachable=(False, None))
        r = d.run()
        assert r.outcome == OUTCOME_FAIL
        assert r.assigned_ip == "213.58.173.241"
        assert r.reachable is False

    def test_churn_is_reported_as_churn(self):
        d = _Deps(status=LEASE_STATUS_NO_IP, state="closed")
        r = d.run()
        assert r.outcome == OUTCOME_CHURNED
        assert r.lease_state_at_failure == "closed"
        assert d.destroyed == ["1788000000001"]

    def test_out_of_pool_address(self):
        d = _Deps(status={"ips": {"probe": [{"IP": "8.8.8.8", "ExternalPort": 80}]}})
        r = d.run()
        assert r.outcome == OUTCOME_FAIL
        assert r.in_pool is False
        assert r.pool_cidr == "213.58.173.240/28"


class TestNoLeakGuarantee:
    """#244 criterion 7. This is the proof, and it costs no escrow — which is
    the reason the dependencies are injected at all."""

    def test_destroy_runs_when_a_later_step_raises(self):
        d = _Deps(raise_on="status")
        r = d.run()
        assert d.destroyed == ["1788000000001"], "leaked a deployment on an exception"
        assert r.outcome == OUTCOME_FAIL
        assert "stage raised" in r.reason

    def test_destroy_runs_when_curl_raises(self):
        d = _Deps(raise_on="curl")
        d.run()
        assert d.destroyed == ["1788000000001"]

    def test_no_destroy_when_nothing_was_ever_created(self):
        """The one path that must NOT destroy: there is no dseq to destroy, and
        inventing one would be worse than leaking nothing."""
        d = _Deps(raise_on="deploy")
        r = d.run()
        assert d.destroyed == []
        assert r.outcome == OUTCOME_FAIL

    def test_destroy_failure_does_not_mask_the_verdict(self):
        d = _Deps(raise_on="destroy")
        r = d.run()
        assert r.outcome == OUTCOME_PASS, "cleanup trouble overwrote the real result"
        assert "destroy_error" in r.diagnostics


class TestStageLedger:
    def test_ledger_written_on_every_outcome(self, tmp_path):
        p = tmp_path / "ip-ledger.jsonl"
        for deps in (_Deps(), _Deps(status=LEASE_STATUS_NO_IP), _Deps(raise_on="status")):
            deps.run(ledger_path=str(p), version="v1")
        rows = [json.loads(x) for x in p.read_text().strip().splitlines()]
        assert len(rows) == 3
        assert {r["outcome"] for r in rows} == {OUTCOME_PASS, OUTCOME_FAIL}
        # The seam field must survive every path, including the raising one.
        assert all(r["lease_created_at"] == "2026-09-04T20:00:00+00:00" for r in rows)

    def test_ledger_failure_is_surfaced_not_swallowed(self, tmp_path):
        blocker = tmp_path / "afile"
        blocker.write_text("x")
        r = _Deps().run(ledger_path=str(blocker / "sub" / "l.jsonl"))
        assert r.diagnostics.get("ledger_write_failed") is True
