"""A provider that stranded the runner once must never be promoted by a later streak.

The failure being qualified against: a provider is online, well-provisioned, bids,
WINS — and never schedules the runner pod. Recorded for three providers at both
16Gi/30Gi and 32Gi/30Gi, so memory is ruled out; ephemeral storage and port-80 global
ingress are the live hypotheses. One instance stalled 1800s.

That lease is worse than no bid: it consumes the attempt, holds escrow, and stalls to
timeout. So `LEASE_NO_POD` is disqualifying, and disqualification outranks any passing
streak — promotion has to be harder than demotion when the failure is expensive and
silent while success is cheap and obvious.
"""

from __future__ import annotations

import json

import pytest

from just_akash.runner_probe import (
    MIN_PROVEN_HOSTS,
    REQUIRED_CONSECUTIVE_PASSES,
    Attempt,
    Outcome,
    ProviderVerdict,
    classify,
    render_verdicts,
)

P = "akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z"


def _ok(**kw) -> Outcome:
    base: dict[str, object] = dict(
        bid=True, pod_running=True, registered=True, job_ran=True, torn_down=True
    )
    base.update(kw)
    return classify(**base)  # pyright: ignore[reportArgumentType]


# --------------------------------------------------------------------------
# Stage classification — each failure implies a different remedy
# --------------------------------------------------------------------------


def test_a_clean_run_passes():
    assert _ok() is Outcome.PASS


def test_no_bid_is_not_a_provider_defect():
    """Capacity or price. Retrying elsewhere is correct; demoting is not."""
    assert _ok(bid=False) is Outcome.NO_BID


def test_lease_without_a_pod_is_its_own_outcome():
    """THE failure this tool exists to find. Folding it into NO_BID is what made it
    look like a market outage for months."""
    assert _ok(pod_running=False) is Outcome.LEASE_NO_POD


def test_stage_order_reports_the_earliest_failure():
    """A provider that never scheduled cannot also be blamed for not registering."""
    assert _ok(pod_running=False, registered=False, job_ran=False) is Outcome.LEASE_NO_POD


def test_registered_but_no_job_is_distinct():
    assert _ok(job_ran=False) is Outcome.JOB_NOT_RUN


def test_a_leaked_lease_is_not_a_pass():
    """It worked and still cost us escrow — the caller must see that."""
    assert _ok(torn_down=False) is Outcome.TEARDOWN_FAILED


def test_probe_error_is_indeterminate_never_a_verdict():
    """The probe's own failure must not become the provider's."""
    assert _ok(probe_error="console 500") is Outcome.INDETERMINATE


# --------------------------------------------------------------------------
# None != False — the token-less mode
# --------------------------------------------------------------------------


def test_unmeasured_stages_neither_demote_NOR_promote():
    """None must cut BOTH ways.

    Not demoting on it was always right: reporting "never registered" when we never
    asked would condemn a provider on evidence we did not gather. But PROMOTING on it
    was a real bug — a scheduling-only probe reached PASS, and three of those reach
    runner_host, on a bar whose registration and job steps nobody measured.

    SCHEDULED_ONLY is neither: not a failure, not a qualification."""
    assert _ok(registered=None, job_ran=None) is Outcome.SCHEDULED_ONLY


def test_scheduled_only_can_never_reach_runner_host():
    """The property that matters. Any number of partial measurements must not qualify."""
    v = _v(Outcome.SCHEDULED_ONLY, Outcome.SCHEDULED_ONLY, Outcome.SCHEDULED_ONLY)
    assert v.consecutive_passes == 0
    assert v.marker() == "unproven"


def test_an_unmeasured_job_alone_blocks_promotion():
    """Registration measured, job execution not — still not a full pass."""
    assert _ok(registered=True, job_ran=None) is Outcome.SCHEDULED_ONLY


def test_a_measured_job_failure_is_still_disqualifying():
    """SCHEDULED_ONLY must not become a hiding place for real failures."""
    assert _ok(job_ran=False) is Outcome.JOB_NOT_RUN


def test_but_a_measured_failure_to_register_does_demote():
    assert _ok(registered=False) is Outcome.POD_NO_REGISTER


# --------------------------------------------------------------------------
# The streak must be CONSECUTIVE
# --------------------------------------------------------------------------


def _v(*outcomes):
    return ProviderVerdict(address=P, attempts=[Attempt(outcome=o) for o in outcomes])


def test_three_consecutive_passes_qualify():
    v = _v(Outcome.PASS, Outcome.PASS, Outcome.PASS)
    assert v.consecutive_passes == 3
    assert v.marker() == "runner_host"


def test_two_passes_are_not_enough():
    assert _v(Outcome.PASS, Outcome.PASS).marker() == "unproven"


def test_an_intermittent_provider_does_not_qualify_on_totals():
    """4 passes, but never 3 in a row. The one currently-qualified provider is
    recorded as bidding intermittently, which is exactly what this excludes."""
    v = _v(
        Outcome.PASS,
        Outcome.NO_BID,
        Outcome.PASS,
        Outcome.NO_BID,
        Outcome.PASS,
        Outcome.NO_BID,
        Outcome.PASS,
    )
    assert len([a for a in v.attempts if a.passed]) == 4, "four passes in total"
    assert v.consecutive_passes == 1, "but the trailing run is only one"
    assert v.marker() == "unproven"


def test_the_streak_is_the_TRAILING_run():
    v = _v(Outcome.NO_BID, Outcome.PASS, Outcome.PASS, Outcome.PASS)
    assert v.consecutive_passes == 3 and v.marker() == "runner_host"


# --------------------------------------------------------------------------
# Disqualification outranks a streak
# --------------------------------------------------------------------------


def test_one_stranded_lease_disqualifies_despite_a_later_streak():
    """The asymmetry that matters. A provider that stranded the runner has shown it
    can, and the cost of finding out again is a stalled lease holding escrow."""
    v = _v(Outcome.LEASE_NO_POD, Outcome.PASS, Outcome.PASS, Outcome.PASS)
    assert v.consecutive_passes == 3, "the streak is real"
    assert v.marker() == "runner_deny", "and it must still not be promoted"


@pytest.mark.parametrize(
    "bad", [Outcome.LEASE_NO_POD, Outcome.POD_NO_REGISTER, Outcome.JOB_NOT_RUN]
)
def test_every_disqualifying_outcome_sticks(bad):
    assert _v(bad, Outcome.PASS, Outcome.PASS, Outcome.PASS).marker() == "runner_deny"


def test_a_leaked_teardown_does_not_disqualify_the_provider():
    """Escrow leaked, but the provider hosted the runner fine — that is our bug to
    fix, not grounds to remove a scarce proven host."""
    v = _v(Outcome.TEARDOWN_FAILED, Outcome.PASS, Outcome.PASS, Outcome.PASS)
    assert v.marker() == "runner_host"


def test_no_bid_alone_never_demotes():
    """A provider that simply never bid told us nothing about its hosting."""
    assert _v(Outcome.NO_BID, Outcome.NO_BID, Outcome.NO_BID).marker() == "unknown"


def test_probe_failures_alone_are_unknown_not_unproven():
    """'We could not measure' and 'it failed' are different claims."""
    assert _v(Outcome.INDETERMINATE, Outcome.INDETERMINATE).marker() == "unknown"


def test_no_attempts_is_not_a_qualification():
    assert ProviderVerdict(address=P).marker() == "unproven"


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def test_report_warns_while_the_pool_would_still_be_shallow():
    body = "\n".join(render_verdicts([_v(Outcome.PASS, Outcome.PASS, Outcome.PASS)]))
    assert f"want >= {MIN_PROVEN_HOSTS}" in body
    assert "billed runners" in body


def test_report_is_quiet_once_the_bar_is_met():
    hosts = [_v(Outcome.PASS, Outcome.PASS, Outcome.PASS) for _ in range(MIN_PROVEN_HOSTS)]
    body = "\n".join(render_verdicts(hosts))
    assert "::warning" not in body


def test_report_names_the_disqualifying_outcome():
    """'runner_deny' without the reason invites someone to re-add the provider —
    which already happened once."""
    body = "\n".join(render_verdicts([_v(Outcome.LEASE_NO_POD)]))
    assert "LEASE_NO_POD" in body and "runner_deny" in body


def test_required_passes_is_configurable_but_defaults_to_three():
    assert REQUIRED_CONSECUTIVE_PASSES == 3
    assert _v(Outcome.PASS, Outcome.PASS).marker(required=2) == "runner_host"


# ==========================================================================
# The driver — the logic above was unrunnable until this existed
# ==========================================================================

from just_akash import runner_probe as rp  # noqa: E402


def test_the_sdl_template_ships_with_the_package():
    """A driver pointing at a missing template fails only at probe time, on a real
    provider, after a real lease."""
    assert rp.SDL_TEMPLATE.exists(), rp.SDL_TEMPLATE


def test_render_leaves_no_placeholder_behind(tmp_path):
    """An unsubstituted {{STORAGE}} yields an SDL that no provider can price, which
    would read as NO_BID — a broken probe blaming the market."""
    out = rp.render_sdl(
        tmp_path / "p.yaml",
        cpu="4",
        memory="16Gi",
        storage="30Gi",
        org="acme",
        token="tok",
        label="probe-x",
    )
    body = out.read_text()
    assert "{{" not in body, "unsubstituted placeholder"
    assert "30Gi" in body and "acme" in body


def test_render_uses_a_placeholder_token_when_none_is_supplied(tmp_path):
    """An empty ACCESS_TOKEN can make the container exit before it is scheduled, which
    would report LEASE_NO_POD for a provider that is fine."""
    body = rp.render_sdl(
        tmp_path / "p.yaml",
        cpu="4",
        memory="16Gi",
        storage="30Gi",
        org="acme",
        token="",
        label="probe-x",
    ).read_text()
    assert "ACCESS_TOKEN=probe-no-token" in body


def test_the_service_is_still_named_probe(tmp_path):
    """cleanup_stale classifies by SERVICE SET: {probe} is reaped after 1h, anything
    else is LEAVE and accumulates forever — holding escrow against the same grant CI
    spends from. Renaming this service makes the qualification tool degrade the thing
    it exists to fix."""
    import yaml as _yaml

    body = rp.render_sdl(
        tmp_path / "p.yaml", cpu="4", memory="16Gi", storage="30Gi", org="a", token="t", label="l"
    ).read_text()
    assert set(_yaml.safe_load(body)["services"]) == {"probe"}


def _driver(
    monkeypatch,
    *,
    dseq: str | None = "123",
    deploy_out="  DSEQ: 123",
    state="active",
    destroyed=True,
    registered=True,
    job_ran=True,
):
    monkeypatch.setattr(rp, "_deploy", lambda *a, **k: (dseq, deploy_out))
    monkeypatch.setattr(rp, "_pod_started", lambda d: state == "active")
    monkeypatch.setattr(rp, "_destroy", lambda d: destroyed)
    monkeypatch.setattr(rp, "_registered", lambda *a, **k: registered)
    monkeypatch.setattr(rp, "_run_noop_job", lambda *a, **k: job_ran)
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)


def test_a_clean_probe_passes(monkeypatch, tmp_path):
    """A full PASS needs a job_repo: without one the job step is not attempted, and the
    attempt correctly caps at SCHEDULED_ONLY rather than claiming an unmeasured stage."""
    _driver(monkeypatch)
    a = rp.probe_once(
        "akash1x",
        sdl=tmp_path,
        org="o",
        label="l",
        token="t",
        bid_wait=1,
        register_timeout=1,
        job_repo="o/private",
    )
    assert a.outcome is rp.Outcome.PASS and a.dseq == "123"


def test_a_lease_that_never_goes_active_is_the_disqualifying_outcome(monkeypatch, tmp_path):
    """THE failure this tool exists to find."""
    _driver(monkeypatch, state="pending")
    a = rp.probe_once(
        "akash1x", sdl=tmp_path, org="o", label="l", token="t", bid_wait=1, register_timeout=0
    )
    assert a.outcome is rp.Outcome.LEASE_NO_POD


def test_a_402_is_indeterminate_not_a_provider_verdict(monkeypatch, tmp_path):
    """No order existed, so no provider was ever asked. Recording NO_BID here would
    blame the market for an empty wallet — the misdiagnosis that started all this."""
    _driver(monkeypatch, dseq=None, deploy_out="PaymentRequiredError: HTTP 402")
    a = rp.probe_once(
        "akash1x", sdl=tmp_path, org="o", label="l", token="t", bid_wait=1, register_timeout=1
    )
    assert a.outcome is rp.Outcome.INDETERMINATE and "402" in a.detail


def test_no_bid_is_not_confused_with_a_402(monkeypatch, tmp_path):
    _driver(monkeypatch, dseq=None, deploy_out="no bids received")
    a = rp.probe_once(
        "akash1x", sdl=tmp_path, org="o", label="l", token="t", bid_wait=1, register_timeout=1
    )
    assert a.outcome is rp.Outcome.NO_BID


def test_the_lease_is_destroyed_even_when_the_probe_raises(monkeypatch, tmp_path):
    """A probe that leaks the lease it created holds escrow against the grant CI
    spends from — the tool would then cause the failure it measures."""
    seen = []
    monkeypatch.setattr(rp, "_deploy", lambda *a, **k: ("999", "  DSEQ: 999"))
    monkeypatch.setattr(rp, "_destroy", lambda d: seen.append(d) or True)
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)

    def boom(d):
        raise RuntimeError("console 500")

    monkeypatch.setattr(rp, "_pod_started", boom)
    with pytest.raises(RuntimeError):
        rp.probe_once(
            "akash1x", sdl=tmp_path, org="o", label="l", token="t", bid_wait=1, register_timeout=1
        )
    assert seen == ["999"], "the lease must be closed on the failure path too"


def test_without_a_token_the_attempt_is_scheduled_only(monkeypatch, tmp_path):
    """None != False, and it is also != PASS.

    Reporting POD_NO_REGISTER when we never asked would demote on evidence we did not
    gather. But returning PASS would qualify a provider on a bar whose registration and
    job steps were never measured — three of those reach runner_host. SCHEDULED_ONLY is
    the only honest reading of a partial measurement."""
    _driver(monkeypatch, registered=False)
    a = rp.probe_once(
        "akash1x", sdl=tmp_path, org="o", label="l", token="", bid_wait=1, register_timeout=1
    )
    assert a.outcome is rp.Outcome.SCHEDULED_ONLY
    assert a.observed_pod is True, "scheduling WAS measured and still counts as a control"


def test_an_active_lease_running_nothing_is_not_a_pass(monkeypatch, tmp_path):
    """THE trap. `deployment_state == "active"` is true the moment the deployment
    exists and stays true for a lease that never schedules anything — measured across
    seven simultaneous leases on a provider already marked runner_deny, every one
    reporting active while running nothing.

    Keying the probe on it would return PASS for exactly the failure it exists to find,
    and would promote the worst providers in the fleet to runner_host.
    """
    monkeypatch.setattr(rp, "_deploy", lambda *a, **k: ("77", "  DSEQ: 77"))
    monkeypatch.setattr(rp, "_pod_started", lambda d: False)  # MEASURED: serving nothing
    monkeypatch.setattr(rp, "_destroy", lambda d: True)
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    a = rp.probe_once(
        "akash1x", sdl=tmp_path, org="o", label="l", token="t", bid_wait=1, register_timeout=0
    )
    assert a.outcome is rp.Outcome.LEASE_NO_POD


def test_an_unreported_service_is_UNKNOWN_not_a_missing_pod(monkeypatch):
    """The whole reason this is tri-state. Reading "not reported yet" as "no pod" is what
    produced a LEASE_NO_POD verdict against the fleet's one production-proven host."""
    monkeypatch.setattr("just_akash.smoke_providers._service_availability", lambda d: None)
    assert rp._pod_started("1") is None


def test_a_serving_replica_is_a_started_pod(monkeypatch):
    monkeypatch.setattr("just_akash.smoke_providers._service_availability", lambda d: (1, 1))
    assert rp._pod_started("1") is True


def test_zero_available_of_a_REPORTED_service_is_a_measured_no(monkeypatch):
    """Once the provider reports the service, 0 available is real evidence."""
    monkeypatch.setattr("just_akash.smoke_providers._service_availability", lambda d: (0, 1))
    assert rp._pod_started("1") is False


def test_a_read_error_is_UNKNOWN_never_a_missing_pod(monkeypatch):
    def boom(d):
        raise RuntimeError("console 500")

    monkeypatch.setattr("just_akash.smoke_providers._service_availability", boom)
    assert rp._pod_started("1") is None


# ==========================================================================
# Registration tokens — full qualification without a long-lived PAT
# ==========================================================================


def test_a_registration_token_renders_as_RUNNER_TOKEN(tmp_path):
    """myoung34/github-runner (the base image) accepts a pre-minted registration token.
    Rendering it as ACCESS_TOKEN instead would make the runner try to MINT one from it
    and fail — reporting POD_NO_REGISTER for a provider that was fine."""
    import yaml as _yaml

    body = rp.render_sdl(
        tmp_path / "p.yaml",
        cpu="4",
        memory="16Gi",
        storage="30Gi",
        org="o",
        token="ABC123",
        label="l",
        token_kind="RUNNER_TOKEN",
    ).read_text()
    env = _yaml.safe_load(body)["services"]["probe"]["env"]
    assert "RUNNER_TOKEN=ABC123" in env
    assert not any(e.startswith("ACCESS_TOKEN=") for e in env), "both would conflict"


def test_a_pat_still_renders_as_ACCESS_TOKEN(tmp_path):
    import yaml as _yaml

    body = rp.render_sdl(
        tmp_path / "p.yaml",
        cpu="4",
        memory="16Gi",
        storage="30Gi",
        org="o",
        token="ghp_x",
        label="l",
    ).read_text()
    env = _yaml.safe_load(body)["services"]["probe"]["env"]
    assert "ACCESS_TOKEN=ghp_x" in env


def test_minting_returns_empty_rather_than_a_bogus_token(monkeypatch):
    """A failed mint must not yield a truthy string. Rendering junk as RUNNER_TOKEN
    would make every provider report POD_NO_REGISTER and demote the whole fleet on
    what is really our own credential failure."""
    monkeypatch.setattr(rp, "_run", lambda *a, **k: (1, "gh: HTTP 403"))
    assert rp.mint_registration_token("org") == ""
    monkeypatch.setattr(rp, "_run", lambda *a, **k: (0, "  \n"))
    assert rp.mint_registration_token("org") == ""


def test_a_minted_token_is_returned_stripped(monkeypatch):
    monkeypatch.setattr(rp, "_run", lambda *a, **k: (0, "AAABBB\n"))
    assert rp.mint_registration_token("org") == "AAABBB"


# ==========================================================================
# Positive control — a negative only counts if a positive was demonstrated
# ==========================================================================


def _att(outcome, observed=False):
    return Attempt(outcome=outcome, observed_pod=observed)


def test_a_run_with_no_positive_control_withholds_disqualifications():
    """The incident: a probe reported LEASE_NO_POD for the fleet's one production-proven
    runner_host, and across two runs EVERY leasing provider reported LEASE_NO_POD while
    `_pod_started` had never once returned True. An un-propagated lease is
    indistinguishable from one that will never schedule, so without a known-good reading
    a 'no pod' result carries no information."""
    v = ProviderVerdict(address=P, attempts=[_att(Outcome.LEASE_NO_POD)])
    out, warning = rp.require_positive_control([v])
    assert out[0].attempts[0].outcome is Outcome.INDETERMINATE
    assert out[0].marker() == "unknown", "must not be runner_deny"
    assert "positive control" in warning.lower()


def test_a_run_WITH_a_positive_control_keeps_its_disqualifications():
    """Once the instrument is shown capable of a True, a False means something."""
    good = ProviderVerdict(address="akash1good", attempts=[_att(Outcome.PASS, observed=True)])
    bad = ProviderVerdict(address="akash1bad", attempts=[_att(Outcome.LEASE_NO_POD)])
    out, warning = rp.require_positive_control([good, bad])
    assert out[1].attempts[0].outcome is Outcome.LEASE_NO_POD
    assert out[1].marker() == "runner_deny"
    assert warning == ""


def test_the_control_can_come_from_a_DIFFERENT_provider():
    """It validates the INSTRUMENT, not the provider under test."""
    good = ProviderVerdict(
        address="akash1a", attempts=[_att(Outcome.TEARDOWN_FAILED, observed=True)]
    )
    bad = ProviderVerdict(address="akash1b", attempts=[_att(Outcome.POD_NO_REGISTER)])
    out, _ = rp.require_positive_control([good, bad])
    assert out[1].attempts[0].outcome is Outcome.POD_NO_REGISTER


def test_no_bid_is_untouched_by_the_control_gate():
    """NO_BID needs no pod detection, so it is unaffected either way."""
    v = ProviderVerdict(address=P, attempts=[_att(Outcome.NO_BID)])
    out, warning = rp.require_positive_control([v])
    assert out[0].attempts[0].outcome is Outcome.NO_BID
    assert warning == "", "nothing was withheld, so nothing to warn about"


def test_probe_once_records_whether_a_pod_was_seen(monkeypatch, tmp_path):
    _driver(monkeypatch)
    a = rp.probe_once(
        "akash1x", sdl=tmp_path, org="o", label="l", token="t", bid_wait=1, register_timeout=1
    )
    assert a.observed_pod is True
    _driver(monkeypatch, state="pending")
    b = rp.probe_once(
        "akash1x", sdl=tmp_path, org="o", label="l", token="t", bid_wait=1, register_timeout=0
    )
    assert b.observed_pod is False


def test_an_undispatchable_job_is_unmeasured_not_failed(monkeypatch):
    """workflow_dispatch only resolves on the DEFAULT branch, so before this workflow
    merges the dispatch 404s. That must read as "not measured" — returning False would
    invent a JOB_NOT_RUN and disqualify every provider on OUR deployment gap."""
    monkeypatch.setattr(rp, "_run", lambda *a, **k: (1, "could not find any workflows"))
    assert rp._run_noop_job("org", "label", "o/r", 1) is None


def test_a_successful_run_is_a_measured_pass(monkeypatch):
    calls = {"n": 0}

    def fake(cmd, timeout=60, env=None):
        calls["n"] += 1
        if cmd[1] == "workflow":
            return 0, ""
        return 0, json.dumps(
            [{"status": "completed", "conclusion": "success", "createdAt": "2999-01-01T00:00:00Z"}]
        )

    monkeypatch.setattr(rp, "_run", fake)
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    assert rp._run_noop_job("org", "label", "o/r", 30) is True


def test_a_failed_run_is_a_measured_failure(monkeypatch):
    def fake(cmd, timeout=60, env=None):
        if cmd[1] == "workflow":
            return 0, ""
        return 0, json.dumps(
            [{"status": "completed", "conclusion": "failure", "createdAt": "2999-01-01T00:00:00Z"}]
        )

    monkeypatch.setattr(rp, "_run", fake)
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    assert rp._run_noop_job("org", "label", "o/r", 30) is False


def test_an_unmeasurable_pod_state_is_indeterminate_not_a_disqualification(monkeypatch, tmp_path):
    """If _pod_started stays None for the whole window we never measured anything.
    Reporting LEASE_NO_POD there would permanently runner_deny a provider on OUR failure
    to read, which is exactly what the tri-state exists to prevent."""
    monkeypatch.setattr(rp, "_deploy", lambda *a, **k: ("55", "  DSEQ: 55"))
    monkeypatch.setattr(rp, "_pod_started", lambda d: None)  # never resolves
    monkeypatch.setattr(rp, "_destroy", lambda d: True)
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    a = rp.probe_once(
        "akash1x", sdl=tmp_path, org="o", label="l", token="t", bid_wait=1, register_timeout=0
    )
    assert a.outcome is rp.Outcome.INDETERMINATE
    assert "measurable" in a.detail


def test_a_measured_false_still_disqualifies(monkeypatch, tmp_path):
    """The escape hatch must not swallow a real LEASE_NO_POD."""
    monkeypatch.setattr(rp, "_deploy", lambda *a, **k: ("55", "  DSEQ: 55"))
    monkeypatch.setattr(rp, "_pod_started", lambda d: False)  # MEASURED: not serving
    monkeypatch.setattr(rp, "_destroy", lambda d: True)
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    a = rp.probe_once(
        "akash1x", sdl=tmp_path, org="o", label="l", token="t", bid_wait=1, register_timeout=0
    )
    assert a.outcome is rp.Outcome.LEASE_NO_POD


def test_the_registration_token_is_reminted_per_attempt(monkeypatch, tmp_path):
    """A registration token lives ~1h and a 3x3 run can outlast it. A stale token fails
    SILENTLY — the runner never registers, the attempt reports POD_NO_REGISTER, and
    providers are demoted for our own expired credential."""
    minted = []
    monkeypatch.setattr(
        rp, "mint_registration_token", lambda org: minted.append(org) or f"tok{len(minted)}"
    )
    monkeypatch.setattr(
        rp, "probe_once", lambda *a, **k: Attempt(outcome=Outcome.PASS, observed_pod=True)
    )
    monkeypatch.setattr(rp, "render_sdl", lambda dest, **k: dest)

    class A:
        providers = "akash1a,akash1b"
        attempts = 3
        cpu = memory = storage = "x"
        org = "acme"
        bid_wait = register_timeout = 1
        job_repo = ""

    rp._run_probes(A(), "seed", "RUNNER_TOKEN", str(tmp_path))
    assert len(minted) == 6, f"expected one mint per attempt (2 providers x 3), got {len(minted)}"


def test_a_pat_is_not_reminted(monkeypatch, tmp_path):
    """Only registration tokens expire on this timescale; a supplied PAT must be used
    as given rather than replaced by a minted token."""
    minted = []
    monkeypatch.setattr(rp, "mint_registration_token", lambda org: minted.append(org) or "tok")
    monkeypatch.setattr(
        rp, "probe_once", lambda *a, **k: Attempt(outcome=Outcome.PASS, observed_pod=True)
    )
    monkeypatch.setattr(rp, "render_sdl", lambda dest, **k: dest)

    class A:
        providers = "akash1a"
        attempts = 2
        cpu = memory = storage = "x"
        org = "acme"
        bid_wait = register_timeout = 1
        job_repo = ""

    rp._run_probes(A(), "ghp_real", "ACCESS_TOKEN", str(tmp_path))
    assert minted == [], "a PAT must not be replaced by a minted token"


def test_a_registration_token_is_never_used_as_an_api_credential(monkeypatch, tmp_path):
    """A runner REGISTRATION token authenticates a runner joining the org and is rejected
    by the REST API ("Bad credentials", verified live). Forwarding it as GH_TOKEN breaks
    the poll that decides whether the runner registered — so the runner registers fine,
    we never see it, and a healthy provider is demoted with POD_NO_REGISTER. Measured
    against the production-proven host on attempt 1 of a real run."""
    seen = {}
    monkeypatch.setattr(rp, "mint_registration_token", lambda org: "REGTOKEN")
    monkeypatch.setattr(rp, "render_sdl", lambda dest, **k: dest)
    monkeypatch.setattr(
        rp,
        "probe_once",
        lambda *a, **k: seen.update(k) or Attempt(outcome=Outcome.PASS, observed_pod=True),
    )

    class A:
        providers = "akash1a"
        attempts = 1
        cpu = memory = storage = "x"
        org = "acme"
        bid_wait = register_timeout = 1
        job_repo = ""

    rp._run_probes(A(), "REGTOKEN", "RUNNER_TOKEN", str(tmp_path))
    assert seen["token"] == "REGTOKEN", "the SDL still needs the registration token"
    assert seen["api_token"] == "", "but it must NOT be offered to the REST API"


def test_a_pat_IS_used_as_the_api_credential(monkeypatch, tmp_path):
    """The original finding was real for a PAT: without forwarding it the poll depends on
    an ambient credential that may not exist."""
    seen = {}
    monkeypatch.setattr(rp, "render_sdl", lambda dest, **k: dest)
    monkeypatch.setattr(
        rp,
        "probe_once",
        lambda *a, **k: seen.update(k) or Attempt(outcome=Outcome.PASS, observed_pod=True),
    )

    class A:
        providers = "akash1a"
        attempts = 1
        cpu = memory = storage = "x"
        org = "acme"
        bid_wait = register_timeout = 1
        job_repo = ""

    rp._run_probes(A(), "ghp_real", "ACCESS_TOKEN", str(tmp_path))
    assert seen["api_token"] == "ghp_real"


def test_registration_counts_only_ONLINE_runners(monkeypatch):
    """Offline leftovers must not read as a live runner.

    Labels repeat across runs, so dead registrations from an earlier run matched the
    current label. Without a status filter the probe believed a runner was up, dispatched
    the no-op job at a label owned only by corpses, and the job queued until timeout —
    JOB_NOT_RUN, blamed on the provider. Measured with 13 offline probe runners listed
    and zero online.
    """
    seen = {}

    def fake(cmd, timeout=60, env=None):
        seen["jq"] = cmd[cmd.index("--jq") + 1]
        return 0, "1"

    monkeypatch.setattr(rp, "_run", fake)
    rp._registered("org", "probe-x", "", 1)
    assert '.status=="online"' in seen["jq"], "an offline leftover must not count as registered"


def test_registration_survives_a_multi_page_org(monkeypatch):
    """GitHub paginates `orgs/{org}/actions/runners`, and `gh api --paginate --jq`
    applies the filter to EACH PAGE separately, concatenating the results.

    So an aggregating filter (`[...] | length`) emits one number PER PAGE. On an org
    with >100 runners this read back "2\\n1", whose `.isdigit()` is False — so the
    probe reported the runner as never registered no matter how many were online.
    That is POD_NO_REGISTER, i.e. a PERMANENT runner_deny (it outranks any later
    streak) against a provider that hosted the runner correctly, and it only fires on
    the large orgs where a runner pool is worth having.

    The fix is shape, not parsing: one line per match survives concatenation.
    """
    monkeypatch.setattr(rp, "_run", lambda cmd, timeout=60, env=None: (0, "241\n578\n"))
    assert rp._registered("org", "probe-x", "", 1) is True, (
        "two pages of one matching runner each must read as registered"
    )


def test_registration_jq_emits_one_line_per_runner_not_a_count(monkeypatch):
    """Locks the shape above. An aggregating filter is the natural thing to write here
    and it is wrong specifically under --paginate."""
    seen = {}

    def fake(cmd, timeout=60, env=None):
        seen["jq"] = cmd[cmd.index("--jq") + 1]
        assert "--paginate" in cmd, "page 1 only cannot see a runner that landed on page 2"
        return 0, "1"

    monkeypatch.setattr(rp, "_run", fake)
    rp._registered("org", "probe-x", "", 1)
    assert "length" not in seen["jq"], "an aggregate emits one value per page"


def test_an_unreadable_listing_is_None_not_absent(monkeypatch):
    """A throttled or failing `gh` must never become POD_NO_REGISTER.

    That outcome is a PERMANENT runner_deny which outranks any later streak, so a
    provider that hosted the runner correctly would be struck off the fleet because OUR
    API budget ran out. And this is reachable in normal operation: GitHub cannot filter
    runners by label server-side (`?name=` is exact-match), so every poll pages the whole
    org listing against a PAT's 5,000/hour shared across all of that user's tokens.

    None routes to SCHEDULED_ONLY — non-promotable AND non-demoting, which is the honest
    reading of "we never looked successfully".
    """
    monkeypatch.setattr(rp, "_run", lambda cmd, timeout=60, env=None: (1, ""))
    assert rp._registered("org", "probe-x", "", 0) is None

    assert (
        rp.classify(bid=True, pod_running=True, registered=None, job_ran=None, torn_down=True)
        is Outcome.SCHEDULED_ONLY
    ), "an unmeasured registration must not promote"
    assert (
        rp.classify(bid=True, pod_running=True, registered=False, job_ran=None, torn_down=True)
        is Outcome.POD_NO_REGISTER
    ), "a MEASURED absence must still demote — the fix must not blunt the real signal"


def test_one_good_read_makes_a_later_failure_meaningful(monkeypatch):
    """The tri-state keys on 'was the listing EVER readable', not on the last poll. A run
    that read the listing fine and simply never saw the runner has measured something,
    and must still be able to report POD_NO_REGISTER."""
    reads = iter([(0, ""), (1, ""), (1, "")])
    # Deterministic clock: deadline is set from the first tick, so 0 -> deadline 10,
    # one poll at t=5, then t=100 ends it. No real sleeping, no wall-clock flake.
    ticks = iter([0.0, 5.0, 100.0])

    monkeypatch.setattr(rp, "_run", lambda cmd, timeout=60, env=None: next(reads, (1, "")))
    monkeypatch.setattr(rp.time, "time", lambda: next(ticks, 100.0))
    monkeypatch.setattr(rp.time, "sleep", lambda *_: None)
    assert rp._registered("org", "probe-x", "", 10) is False


def test_no_matching_runner_is_still_not_registered(monkeypatch):
    """The counterpart to the pagination fix: relaxing the parse must not make empty
    output read as success. A false PASS promotes a provider on a bar nobody measured,
    which is the failure SCHEDULED_ONLY exists to prevent.

    A READABLE listing with no match is a real measurement, so it stays False and can
    still demote."""

    # Every scenario here asserts the query actually RAN. A timeout that lets the loop
    # exit before its first poll would satisfy both verdicts below while measuring
    # nothing — the tests would then be asserting the shape of a function they never
    # called, which is the vacuity this module's anti-vacuity pass exists to catch.
    def _poll(rc: int, out: str) -> tuple[bool | None, list]:
        calls = []
        # deadline = 0 + 10; the post-read check reads 100, so exactly one poll happens.
        ticks = iter([0.0, 100.0])

        def fake(cmd, timeout=60, env=None):
            calls.append(cmd)
            return rc, out

        monkeypatch.setattr(rp, "_run", fake)
        monkeypatch.setattr(rp.time, "time", lambda: next(ticks, 100.0))
        monkeypatch.setattr(rp.time, "sleep", lambda *_: None)
        return rp._registered("org", "probe-x", "", 10), calls

    verdict, calls = _poll(0, "\n  \n")
    assert len(calls) == 1, "the listing must actually be queried before any verdict"
    assert verdict is False

    # A non-zero `gh` is a THIRD answer, not this one. Output on a failed call is not
    # evidence either way — see test_an_unreadable_listing_is_None_not_absent.
    verdict, calls = _poll(1, "241\n")
    assert len(calls) == 1, "the listing must actually be queried before any verdict"
    assert verdict is None, "a failed gh is not evidence"


def test_runner_labels_are_unique_per_run(monkeypatch, tmp_path):
    """Two runs of the same provider+attempt must not share a label, or each run inherits
    the previous run's dead registrations."""
    labels = []
    monkeypatch.setattr(rp, "render_sdl", lambda dest, **k: labels.append(k["label"]) or dest)
    monkeypatch.setattr(
        rp, "probe_once", lambda *a, **k: Attempt(outcome=Outcome.PASS, observed_pod=True)
    )

    class A:
        providers = "akash1aaaaaa"
        attempts = 1
        cpu = memory = storage = "x"
        org = "acme"
        bid_wait = register_timeout = 1
        job_repo = ""

    rp._run_probes(A(), "t", "ACCESS_TOKEN", str(tmp_path))
    rp._run_probes(A(), "t", "ACCESS_TOKEN", str(tmp_path))
    assert len(labels) == 2 and labels[0] != labels[1], f"labels collided across runs: {labels}"


def test_a_job_that_never_leaves_queued_is_unmeasured_not_failed(monkeypatch):
    """A run stuck in `queued` was never assigned to a runner. The usual cause is org
    policy — GitHub blocks org self-hosted runners from PUBLIC repos unless the runner
    group sets allows_public_repositories, and just-akash is public with it false. That
    is OUR configuration, so reporting JOB_NOT_RUN would blame the provider for it."""

    def fake(cmd, timeout=60, env=None):
        if cmd[1] == "workflow":
            return 0, ""
        return 0, json.dumps(
            [{"status": "queued", "conclusion": None, "createdAt": "2999-01-01T00:00:00Z"}]
        )

    monkeypatch.setattr(rp, "_run", fake)
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    assert rp._run_noop_job("org", "label", "o/r", 0) is None


def test_a_job_that_STARTED_and_failed_is_a_real_failure(monkeypatch):
    """Once a runner picked it up, the verdict is about the runner."""

    def fake(cmd, timeout=60, env=None):
        if cmd[1] == "workflow":
            return 0, ""
        return 0, json.dumps(
            [{"status": "completed", "conclusion": "failure", "createdAt": "2999-01-01T00:00:00Z"}]
        )

    monkeypatch.setattr(rp, "_run", fake)
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    assert rp._run_noop_job("org", "label", "o/r", 0) is False


def test_without_a_job_repo_the_attempt_cannot_reach_PASS(monkeypatch, tmp_path):
    """No dispatch target means the job step is never attempted. That is unmeasured, and
    unmeasured must not promote — a public job_repo left the job queued forever, so
    silently skipping it had to cap the attempt rather than pass it."""
    _driver(monkeypatch)
    a = rp.probe_once(
        "akash1x", sdl=tmp_path, org="o", label="l", token="t", bid_wait=1, register_timeout=1
    )
    assert a.outcome is rp.Outcome.SCHEDULED_ONLY


def test_a_run_from_an_EARLIER_attempt_is_ignored(monkeypatch):
    """Attempt N must not read attempt N-1's success. Without a dispatch cutoff the poll
    takes the first completed run it sees and passes without its own job finishing —
    every attempt after the first becomes a false PASS the moment one job fails."""

    def fake(cmd, timeout=60, env=None):
        if cmd[1] == "workflow":
            return 0, ""
        # A stale success from before this dispatch.
        return 0, json.dumps(
            [{"status": "completed", "conclusion": "success", "createdAt": "2000-01-01T00:00:00Z"}]
        )

    monkeypatch.setattr(rp, "_run", fake)
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    assert rp._run_noop_job("org", "label", "o/r", 0) is None, "a stale run must not count"


def test_the_job_poll_is_authenticated_too(monkeypatch):
    """PAT mode must authenticate BOTH the dispatch and the poll. Authenticating only the
    dispatch leaves the attempt unmeasured whenever no ambient credential exists."""
    envs = []

    def fake(cmd, timeout=60, env=None):
        envs.append(env)
        if cmd[1] == "workflow":
            return 0, ""
        return 0, json.dumps(
            [{"status": "completed", "conclusion": "success", "createdAt": "2999-01-01T00:00:00Z"}]
        )

    monkeypatch.setattr(rp, "_run", fake)
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    rp._run_noop_job("org", "label", "o/r", 0, api_token="ghp_x")
    assert all(e == {"GH_TOKEN": "ghp_x"} for e in envs), f"unauthenticated call: {envs}"
