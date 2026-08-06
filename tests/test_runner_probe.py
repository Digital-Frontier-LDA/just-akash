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


def _ok(**kw):
    base = dict(bid=True, pod_running=True, registered=True, job_ran=True, torn_down=True)
    base.update(kw)
    return classify(**base)


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


def test_unasked_registration_does_not_demote():
    """Without a token the probe answers only the scheduling question. Reporting
    'never registered' when we never asked would demote on evidence we did not
    gather — and scheduling is already the discriminator for the recorded failures."""
    assert _ok(registered=None, job_ran=None) is Outcome.PASS


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
    dseq="123",
    deploy_out="  DSEQ: 123",
    state="active",
    destroyed=True,
    registered=True,
):
    monkeypatch.setattr(rp, "_deploy", lambda *a, **k: (dseq, deploy_out))
    monkeypatch.setattr(rp, "_lease_active", lambda d: state == "active")
    monkeypatch.setattr(rp, "_pod_started", lambda d: state == "active")
    monkeypatch.setattr(rp, "_destroy", lambda d: destroyed)
    monkeypatch.setattr(rp, "_registered", lambda *a, **k: registered)
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)


def test_a_clean_probe_passes(monkeypatch, tmp_path):
    _driver(monkeypatch)
    a = rp.probe_once(
        "akash1x", sdl=tmp_path, org="o", label="l", token="t", bid_wait=1, register_timeout=1
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

    monkeypatch.setattr(rp, "_lease_active", boom)
    with pytest.raises(RuntimeError):
        rp.probe_once(
            "akash1x", sdl=tmp_path, org="o", label="l", token="t", bid_wait=1, register_timeout=1
        )
    assert seen == ["999"], "the lease must be closed on the failure path too"


def test_without_a_token_registration_is_unasked_not_failed(monkeypatch, tmp_path):
    """None != False. Reporting POD_NO_REGISTER when we never asked would demote a
    provider on evidence we did not gather."""
    _driver(monkeypatch, registered=False)
    a = rp.probe_once(
        "akash1x", sdl=tmp_path, org="o", label="l", token="", bid_wait=1, register_timeout=1
    )
    assert a.outcome is rp.Outcome.PASS


def test_an_active_lease_running_nothing_is_not_a_pass(monkeypatch, tmp_path):
    """THE trap. `deployment_state == "active"` is true the moment the deployment
    exists and stays true for a lease that never schedules anything — measured across
    seven simultaneous leases on a provider already marked runner_deny, every one
    reporting active while running nothing.

    Keying the probe on it would return PASS for exactly the failure it exists to find,
    and would promote the worst providers in the fleet to runner_host.
    """
    monkeypatch.setattr(rp, "_deploy", lambda *a, **k: ("77", "  DSEQ: 77"))
    monkeypatch.setattr(rp, "_lease_active", lambda d: True)  # lease IS active
    monkeypatch.setattr(rp, "_pod_started", lambda d: False)  # ...running nothing
    monkeypatch.setattr(rp, "_destroy", lambda d: True)
    monkeypatch.setattr(rp.time, "sleep", lambda s: None)
    a = rp.probe_once(
        "akash1x", sdl=tmp_path, org="o", label="l", token="t", bid_wait=1, register_timeout=0
    )
    assert a.outcome is rp.Outcome.LEASE_NO_POD


def test_provider_transport_noise_is_not_container_output(monkeypatch):
    """An unreachable provider returns an error on the logs channel. Counting that as
    'the pod started' reads a broken host as healthy."""
    monkeypatch.setattr(rp, "_run", lambda *a, **k: (0, "Error: no such pod"))
    assert rp._pod_started("1") is False
    monkeypatch.setattr(rp, "_run", lambda *a, **k: (0, "Runner listening for jobs"))
    assert rp._pod_started("1") is True


def test_empty_log_output_is_not_a_started_pod(monkeypatch):
    monkeypatch.setattr(rp, "_run", lambda *a, **k: (0, "   \n"))
    assert rp._pod_started("1") is False
