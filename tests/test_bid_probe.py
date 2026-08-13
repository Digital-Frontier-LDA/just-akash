"""Bid-probe: pinning, verdict classification, and the metric contract.

The metric tests are not decoration. The per-cluster alert multiplies by a
`skipped == bool 0` factor, so if `just_akash_bidprobe_skipped` is ever absent
for a pair the alert evaluates to an empty vector and CANNOT page — the failure
is silent and looks exactly like health. That contract is asserted here.
"""

from __future__ import annotations

from typing import Any

import pytest

from just_akash.bid_probe import (
    M_PAIR_TS,
    M_RESULT,
    M_SKIPPED,
    OUTCOME_BID,
    OUTCOME_ERROR,
    OUTCOME_INDEX_LAG,
    OUTCOME_NO_BID,
    OUTCOME_NO_CREDIT,
    PROVIDERS,
    SCENARIOS,
    ProbeRecord,
    ProviderTarget,
    eligible_pairs,
    inject_placement_attributes,
    render_prom,
    run_probe,
)

ONIDC = next(p for p in PROVIDERS if p.cluster == "onidc")
HETZNER = next(p for p in PROVIDERS if p.cluster == "hetzner_hel")


# --------------------------------------------------------------------------
# Eligibility — a cluster's probe must never ask for hardware it does not have
# --------------------------------------------------------------------------


def test_only_onidc_is_asked_about_gpu():
    gpu = {p.cluster for p, s in eligible_pairs() if s.name == "gpu"}
    assert gpu == {"onidc"}


def test_hetzner_is_not_asked_about_ip_lease():
    pairs = {(p.cluster, s.name) for p, s in eligible_pairs()}
    assert ("hetzner_hel", "ip-lease") not in pairs
    assert ("hetzner_hel", "cpu") in pairs


def test_every_provider_has_pinning_attributes():
    # An unpinned order loses the 20-bid cap race and reads as a false NO-BID.
    for p in PROVIDERS:
        assert p.attributes, f"{p.cluster} has no placement attributes"


def test_pinning_attributes_are_unique_per_provider():
    seen = {}
    for p in PROVIDERS:
        key = tuple(sorted(p.attributes.items()))
        assert key not in seen, (
            f"{p.cluster} and {seen[key]} share placement attributes — an order "
            "pinned with them could be bid on by either, so a NO-BID would be "
            "attributed to the wrong provider"
        )
        seen[key] = p.cluster


# --------------------------------------------------------------------------
# Pinning
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_attributes_inject_into_every_scenario_sdl(name):
    out = inject_placement_attributes(SCENARIOS[name].sdl, {"region": "eu-west"})
    assert "      attributes:\n        region: eu-west\n" in out
    assert "      pricing:" in out


def test_injection_refuses_an_unpinned_order():
    with pytest.raises(ValueError, match="unpinned"):
        inject_placement_attributes(SCENARIOS["cpu"].sdl, {})


def test_injection_refuses_an_unexpected_sdl_shape():
    # The in-cluster original warned and submitted anyway, which produces the
    # exact false NO-BID the pinning exists to prevent.
    with pytest.raises(ValueError, match="placement block"):
        inject_placement_attributes('version: "2.0"\n', {"region": "eu-west"})


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------


class FakeClient:
    """Minimal Console API stand-in. Records every close so leaks are visible.

    Payload shapes mirror the real Console responses that `_extract_dseq` and
    `_is_open_bid` actually parse (see tests/test_capacity.py) — a fake with an
    invented shape passes while the production path fails.
    """

    def __init__(self, bidders_per_call):
        self._bidders = list(bidders_per_call)
        self.created = 0
        self.closed: list[str] = []

    def create_deployment(self, sdl, deposit=0.5):
        self.created += 1
        return {"deployment": {"id": {"owner": "akash1me", "dseq": str(1000 + self.created)}}}

    def get_bids(self, dseq):
        return self._bidders.pop(0) if self._bidders else []

    def close_deployment(self, dseq):
        self.closed.append(dseq)
        return {}


def _bid_from(provider):
    return [
        {
            "id": {"provider": provider},
            "price": {"denom": "uact", "amount": "1200"},
            "state": "open",
        }
    ]


def test_our_bid_is_recorded_and_the_order_is_always_closed(monkeypatch):
    client = FakeClient([_bid_from(HETZNER.wallet)])
    recs = run_probe(
        client,
        providers=[
            ProviderTarget("hetzner_hel", HETZNER.wallet, frozenset({"cpu"}), HETZNER.attributes)
        ],
        sleep=lambda _s: None,
        wait_s=0,
    )
    assert [r.outcome for r in recs] == [OUTCOME_BID]
    assert recs[0].skipped is False
    assert client.closed, "probe leaked an open order — escrow stays locked"


def test_no_bid_is_confirmed_by_a_retry_before_being_believed(monkeypatch):
    monkeypatch.setattr("just_akash.smoke_providers._chain_bids_exist", lambda dseq: False)
    # First poll empty, retry finds our bid -> a flake must not page.
    client = FakeClient([[], _bid_from(HETZNER.wallet)])
    slept = []
    recs = run_probe(
        client,
        providers=[
            ProviderTarget("hetzner_hel", HETZNER.wallet, frozenset({"cpu"}), HETZNER.attributes)
        ],
        sleep=slept.append,
        wait_s=0,
    )
    assert recs[0].outcome == OUTCOME_BID
    assert recs[0].retried is True
    assert slept == [60]
    assert len(client.closed) == 2, "both the probe and its retry must close"


def test_sustained_no_bid_survives_the_retry(monkeypatch):
    monkeypatch.setattr("just_akash.smoke_providers._chain_bids_exist", lambda dseq: False)
    client = FakeClient([[], []])
    recs = run_probe(
        client,
        providers=[
            ProviderTarget("hetzner_hel", HETZNER.wallet, frozenset({"cpu"}), HETZNER.attributes)
        ],
        sleep=lambda _s: None,
        wait_s=0,
    )
    assert recs[0].outcome == OUTCOME_NO_BID
    assert recs[0].skipped is False, "a real no-bid is an ANSWER, not a skip"


def test_index_lag_is_a_skip_not_a_provider_fault(monkeypatch):
    # Console returned no bids but the chain says bids exist: our index lied.
    monkeypatch.setattr("just_akash.smoke_providers._chain_bids_exist", lambda dseq: True)
    client = FakeClient([[], []])
    recs = run_probe(
        client,
        providers=[
            ProviderTarget("hetzner_hel", HETZNER.wallet, frozenset({"cpu"}), HETZNER.attributes)
        ],
        sleep=lambda _s: None,
        wait_s=0,
    )
    assert recs[0].outcome == OUTCOME_INDEX_LAG
    assert recs[0].skipped is True


def test_credit_exhaustion_marks_every_remaining_pair_skipped():
    """A dry grant must be LOUD. Emitting nothing looks like a run that never
    happened and is only caught by staleness hours later."""

    class BrokeClient(FakeClient):
        def create_deployment(self, sdl, deposit=0.5):
            raise RuntimeError("HTTP 402: insufficient credit")

    recs = run_probe(BrokeClient([]), providers=PROVIDERS, sleep=lambda _s: None)
    assert len(recs) == len(eligible_pairs()), "every pair must still be reported"
    assert all(r.outcome == OUTCOME_NO_CREDIT for r in recs)
    assert all(r.skipped for r in recs)


def test_probe_error_is_a_skip_and_does_not_abort_the_run(monkeypatch):
    # The second pair reaches the no-bid path, which cross-checks the chain.
    # Without this stub the test would make live calls to the public LCD
    # endpoints — slow, flaky, and dependent on someone else's uptime.
    monkeypatch.setattr("just_akash.smoke_providers._chain_bids_exist", lambda dseq: False)

    class FlakyClient(FakeClient):
        def create_deployment(self, sdl, deposit=0.5):
            self.created += 1
            if self.created == 1:
                raise RuntimeError("connection reset")
            return {"deployment": {"deployment_id": {"dseq": "42"}}}

    client = FlakyClient([[], _bid_from(ONIDC.wallet)])
    recs = run_probe(
        client,
        providers=[
            ProviderTarget("onidc", ONIDC.wallet, frozenset({"cpu", "gpu"}), ONIDC.attributes)
        ],
        sleep=lambda _s: None,
        wait_s=0,
    )
    assert recs[0].outcome == OUTCOME_ERROR
    assert recs[0].skipped is True
    assert len(recs) == 2, "one pair erroring must not abandon the others"


# --------------------------------------------------------------------------
# Metric contract — the part the alert rules stand on
# --------------------------------------------------------------------------


def _samples(text, metric):
    return [ln for ln in text.splitlines() if ln.startswith(metric + "{")]


def test_skipped_is_emitted_for_every_pair_including_the_zero_case():
    recs = [
        ProbeRecord("onidc", ONIDC.wallet, "cpu", OUTCOME_BID, ts=100),
        ProbeRecord("onidc", ONIDC.wallet, "gpu", OUTCOME_NO_BID, ts=100),
        ProbeRecord("onidc", ONIDC.wallet, "ip-lease", OUTCOME_NO_CREDIT, ts=100),
    ]
    out = render_prom(recs, run_ts=100)
    skipped = _samples(out, M_SKIPPED)
    assert len(skipped) == 3, "a missing skipped series makes the alert unable to fire"
    assert any(s.endswith(" 0") and 'scenario="cpu"' in s for s in skipped)
    assert any(s.endswith(" 0") and 'scenario="gpu"' in s for s in skipped)
    assert any(s.endswith(" 1") and 'scenario="ip-lease"' in s for s in skipped)


def test_a_skipped_pair_asserts_no_result_in_either_direction():
    recs = [ProbeRecord("onidc", ONIDC.wallet, "gpu", OUTCOME_NO_CREDIT, ts=100)]
    out = render_prom(recs, run_ts=100)
    assert _samples(out, M_RESULT) == [], "untestable must not claim healthy OR failed"
    assert _samples(out, M_PAIR_TS) == [], "a skip must not refresh freshness"


def test_result_carries_per_pair_freshness_so_a_dead_producer_is_detectable():
    # Prometheus re-ingests a static file forever; freshness must live IN the data.
    recs = [ProbeRecord("onidc", ONIDC.wallet, "cpu", OUTCOME_BID, ts=1786600000)]
    out = render_prom(recs, run_ts=1786600000)
    assert any("1786600000" in s for s in _samples(out, M_PAIR_TS))
    assert "just_akash_bidprobe_run_timestamp 1786600000" in out


def test_bid_price_is_absent_rather_than_zero_when_there_was_no_bid():
    recs = [ProbeRecord("onidc", ONIDC.wallet, "cpu", OUTCOME_NO_BID, ts=100)]
    out = render_prom(recs, run_ts=100)
    assert "just_akash_bidprobe_bid_price{" not in out


def test_every_metric_family_is_declared_before_use():
    recs = [
        ProbeRecord(
            "onidc",
            ONIDC.wallet,
            "cpu",
            OUTCOME_BID,
            price_amount=1200,
            price_denom="uact",
            ts=100,
        )
    ]
    out = render_prom(recs, run_ts=100)
    declared = {ln.split()[2] for ln in out.splitlines() if ln.startswith("# TYPE ")}
    used = {
        ln.split("{")[0].split()[0] for ln in out.splitlines() if ln and not ln.startswith("#")
    }
    assert used <= declared, f"undeclared families: {used - declared}"


def test_a_non_finite_price_is_omitted_rather_than_poisoning_the_scrape():
    # _extract_bid_price falls back to float('inf') on a malformed bid, and an
    # `inf` sample is a parse error that drops EVERY series in the document.
    # Typed Any deliberately: the point is that a value the type system says
    # cannot arrive here does arrive here, from _extract_bid_price's fallback.
    bad_values: list[Any] = [float("inf"), float("nan"), None, "not-a-number"]
    for bad in bad_values:
        recs = [
            ProbeRecord(
                "onidc",
                ONIDC.wallet,
                "cpu",
                OUTCOME_BID,
                price_amount=bad,
                price_denom="uact",
                ts=100,
            )
        ]
        out = render_prom(recs, run_ts=100)
        assert "just_akash_bidprobe_bid_price{" not in out, f"rendered {bad!r}"
        # The verdict itself must still be published — the price is incidental.
        assert _samples(out, M_RESULT), f"{bad!r} suppressed the verdict too"


def test_carriage_returns_cannot_split_a_sample_line():
    recs = [ProbeRecord("oni\rdc", ONIDC.wallet, "cpu", OUTCOME_BID, ts=100)]
    body = [ln for ln in render_prom(recs, run_ts=100).splitlines() if ln]
    assert all("\r" not in ln for ln in body)


def test_retry_delay_zero_disables_the_retry_entirely(monkeypatch):
    # The CLI documents 0 as "disables the retry", so it must not merely make
    # the confirming re-probe instant — that doubles the orders on every no-bid.
    monkeypatch.setattr("just_akash.smoke_providers._chain_bids_exist", lambda dseq: False)
    client = FakeClient([[], []])
    recs = run_probe(
        client,
        providers=[
            ProviderTarget("hetzner_hel", HETZNER.wallet, frozenset({"cpu"}), HETZNER.attributes)
        ],
        sleep=lambda _s: None,
        wait_s=0,
        retry_delay_s=0,
    )
    assert recs[0].outcome == OUTCOME_NO_BID
    assert recs[0].retried is False
    assert client.created == 1, "retry_delay=0 must not submit a second order"


def test_an_unknown_capability_fails_loudly_instead_of_probing_nothing():
    # A typo'd capability silently drops that order shape from the sweep and the
    # series simply never appears, which reads as health forever.
    bad = ProviderTarget(
        "onidc", ONIDC.wallet, frozenset({"gpu", "presistent-beta3"}), ONIDC.attributes
    )
    with pytest.raises(ValueError, match="presistent-beta3"):
        eligible_pairs([bad])


@pytest.mark.parametrize(
    "msg",
    [
        "order 402318 could not be created",
        "read 402 bytes then failed",
    ],
)
def test_a_bare_402_in_prose_is_not_a_credit_verdict(msg):
    # A credit verdict aborts every remaining pair, so a false positive blinds
    # the whole fleet for the run.
    class Client(FakeClient):
        def create_deployment(self, sdl, deposit=0.5):
            raise RuntimeError(msg)

    recs = run_probe(Client([]), providers=PROVIDERS, sleep=lambda _s: None, wait_s=0)
    assert all(r.outcome == OUTCOME_ERROR for r in recs), (
        "a coincidental 402 must not be read as credit exhaustion"
    )
    assert len(recs) == len(eligible_pairs())


def test_a_real_credit_error_still_aborts_the_sweep():
    class Client(FakeClient):
        def create_deployment(self, sdl, deposit=0.5):
            raise RuntimeError("Console API returned HTTP 402: payment required")

    recs = run_probe(Client([]), providers=PROVIDERS, sleep=lambda _s: None, wait_s=0)
    assert all(r.outcome == OUTCOME_NO_CREDIT for r in recs)


def test_a_scoped_run_refuses_to_publish_a_partial_fleet_exposition(tmp_path, monkeypatch):
    """The .prom is overwritten wholesale, so a one-cluster run publishing it
    would delete every other cluster's series — measured live on 2026-08-13."""
    from just_akash import bid_probe

    prom = tmp_path / "out.prom"
    jsonl = tmp_path / "out.jsonl"
    monkeypatch.setenv("AKASH_API_KEY", "x")
    monkeypatch.setattr(
        bid_probe,
        "run_probe",
        lambda *a, **k: [ProbeRecord("onidc", ONIDC.wallet, "cpu", OUTCOME_BID, ts=100)],
    )
    monkeypatch.setattr("just_akash.api.AkashConsoleAPI", lambda key: object())

    rc = bid_probe.main(["--cluster", "onidc", "--prom-out", str(prom), "--jsonl-out", str(jsonl)])
    assert rc == 0
    assert not prom.exists(), "a scoped run must not overwrite the fleet exposition"
    assert jsonl.exists(), "the append-only audit trail must still record it"


def test_a_fleet_run_does_publish(tmp_path, monkeypatch):
    from just_akash import bid_probe

    prom = tmp_path / "out.prom"
    monkeypatch.setenv("AKASH_API_KEY", "x")
    monkeypatch.setattr(
        bid_probe,
        "run_probe",
        lambda *a, **k: [ProbeRecord("onidc", ONIDC.wallet, "cpu", OUTCOME_BID, ts=100)],
    )
    monkeypatch.setattr("just_akash.api.AkashConsoleAPI", lambda key: object())

    assert bid_probe.main(["--prom-out", str(prom)]) == 0
    assert M_RESULT in prom.read_text()


def test_exposition_survives_the_consumer_allowlist_shape():
    """The autobidder/df-grafana consumers drop the WHOLE document on one
    malformed line, so every line must match the strict sample grammar."""
    import re

    recs = [
        ProbeRecord(
            "onidc",
            ONIDC.wallet,
            "cpu",
            OUTCOME_BID,
            price_amount=1200,
            price_denom="uact",
            ts=100,
        ),
        ProbeRecord("onidc", ONIDC.wallet, "gpu", OUTCOME_ERROR, ts=100),
    ]
    sample = re.compile(
        r'^just_akash_[A-Za-z0-9_]+(\{[A-Za-z_][A-Za-z0-9_]*="[^"\\]*"'
        r'(,[A-Za-z_][A-Za-z0-9_]*="[^"\\]*")*\})? -?\d+(\.\d+)?$'
    )
    for ln in render_prom(recs, run_ts=100).splitlines():
        if not ln or ln.startswith("#"):
            continue
        assert sample.match(ln), f"line would poison the whole scrape: {ln!r}"
