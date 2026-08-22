"""A provider that leases and never schedules must never be tried for the runner.

Two measured facts drive every test here, both recorded in the fleet's curation
file after they cost CI runs:

  1. Some providers BID, WIN, and never schedule the runner pod. Observed at both
     16Gi/30Gi and 32Gi/30Gi, so memory is ruled out. One was traced to an 1800s
     stall. Such a lease is worse than no bid: it burns the attempt, holds escrow,
     and stalls to timeout.

  2. just-akash takes the CHEAPEST bid within the set it is given. An unproven
     provider at ~24 uact undercut the proven one at ~27 and captured + killed the
     runner. Price therefore actively selects the broken host unless proven hosts
     are a strictly earlier tier.

So ordering is host-first THEN priority — not priority with a host tiebreak.
"""

from __future__ import annotations

import json

import pytest

from just_akash import runner_candidates as rc

A = "akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z"  # proven host
D = "akash19zzh7whjt4vfwxd5wtj3tjtyatnpntfhldshd8"  # leases, never schedules
X = "akash1eskq5dpjl2lffykc56vuj3je4pkxshd0apxq4v"  # synthetic second denied entry
T = "akash15tl6v6gd0nte0syyxnv57zmmspgju4c3xfmdhk"  # third-party, ci_only

FLEET = [
    {"address": D, "runner_deny": True, "failover_priority": 10, "name": "runner trap"},
    {"address": A, "runner_host": True, "failover_priority": 20, "name": "Sofia"},
    {"address": X, "runner_deny": True, "failover_priority": 40, "name": "denied test provider"},
    {"address": T, "ci_only": True, "failover_priority": 100, "name": "hurricane"},
]


# --------------------------------------------------------------------------
# runner_deny — the rule blazing did not have at all
# --------------------------------------------------------------------------


def test_denied_providers_are_never_candidates():
    ordered, denied = rc.select_candidates(FLEET)
    addrs = [p["address"] for p in ordered]
    assert D not in addrs and X not in addrs
    assert {p["address"] for p in denied} == {D, X}


def test_deny_wins_even_at_the_best_priority():
    """The trap has the LOWEST failover_priority in the fleet. Ordering alone
    would put it first; the deny marker must remove it regardless."""
    ordered, _ = rc.select_candidates(FLEET)
    assert ordered[0]["address"] != D


def test_a_fleet_of_only_denied_providers_yields_nothing_to_try():
    ordered, denied = rc.select_candidates([FLEET[0], FLEET[2]])
    assert ordered == [] and len(denied) == 2


# --------------------------------------------------------------------------
# Ordering — price is the adversary
# --------------------------------------------------------------------------


def test_proven_hosts_come_strictly_before_unproven():
    """Not a tiebreak: if an unproven provider shares the tier, the cheapest bid
    wins and that is exactly how the runner got captured and killed."""
    fleet = [
        {"address": T, "failover_priority": 1},  # best priority, unproven
        {"address": A, "runner_host": True, "failover_priority": 999},
    ]
    ordered, _ = rc.select_candidates(fleet)
    assert ordered[0]["address"] == A, "an unproven provider outranked a proven host"


def test_priority_orders_within_the_same_proven_class():
    fleet = [
        {"address": T, "failover_priority": 50},
        {"address": X, "failover_priority": 10},
    ]
    assert [p["failover_priority"] for p in rc.select_candidates(fleet)[0]] == [10, 50]


def test_unordered_providers_sort_after_ordered_ones():
    fleet = [{"address": T}, {"address": X, "failover_priority": 5}]
    assert rc.select_candidates(fleet)[0][0]["address"] == X


def test_ordering_is_deterministic_for_equal_keys():
    """Two providers with identical markers must not reorder run to run, or a
    'flaky provider' is really a flaky sort."""
    fleet = [{"address": X, "failover_priority": 7}, {"address": A, "failover_priority": 7}]
    once = [p["address"] for p in rc.select_candidates(fleet)[0]]
    twice = [p["address"] for p in rc.select_candidates(list(reversed(fleet)))[0]]
    assert once == twice


# --------------------------------------------------------------------------
# Parsing — polarity
# --------------------------------------------------------------------------


def test_flat_comma_list_still_works():
    """Back-compat with AKASH_PROVIDERS. Unmarked means UNPROVEN, not denied."""
    got = rc.parse_providers(f"{A},{T}")
    assert [p["address"] for p in got] == [A, T]
    assert not any(p["runner_deny"] for p in got)
    assert not any(p["runner_host"] for p in got)


def test_json_form_carries_the_markers():
    got = rc.parse_providers(f'[{{"address":"{A}","runner_host":true,"preferred":true}}]')
    assert got[0]["runner_host"] is True
    assert got[0]["preferred"] is True


def test_operator_preferred_does_not_claim_runner_host_proof():
    got = rc.parse_providers(f'[{{"address":"{X}","preferred":true}}]')
    assert got[0]["preferred"] is True
    assert got[0]["runner_host"] is False


def test_explicit_nonpreferred_runner_host_stays_in_fallback():
    providers = rc.parse_providers(
        json.dumps([{"address": A, "runner_host": True, "preferred": False}])
    )
    ordered, denied = rc.select_candidates(providers)
    assert denied == []
    assert ordered[0]["runner_host"] is True
    assert ordered[0]["preferred"] is False


def test_legacy_runner_host_is_preferred_when_marker_absent():
    providers = rc.parse_providers(json.dumps([{"address": A, "runner_host": True}]))
    assert providers[0]["preferred"] is True


def test_preferred_provider_cannot_be_standing_denied():
    with pytest.raises(rc.ProviderSpecError, match="preferred/runner_host and runner_deny"):
        rc.parse_providers(json.dumps([{"address": A, "preferred": True, "runner_deny": True}]))


def test_empty_input_is_empty_not_an_error():
    assert rc.parse_providers("") == []
    assert rc.parse_providers("   ") == []


def test_malformed_json_raises_rather_than_degrading():
    """Returning [] would fall through to just-akash's built-in defaults and
    ignore every runner_deny the operator recorded."""
    with pytest.raises(rc.ProviderSpecError, match="not valid JSON"):
        rc.parse_providers('[{"address": ')


def test_a_typo_address_is_rejected_loudly():
    """A bad address silently never bids, which reads as a market outage."""
    with pytest.raises(rc.ProviderSpecError, match="Akash address"):
        rc.parse_providers('[{"address":"aaksh1typo"}]')


@pytest.mark.parametrize("suffix", [",akash1injected", "\nakash1injected", " ", "-bad"])
def test_provider_address_cannot_inject_an_extra_csv_candidate(suffix):
    """GitHub outputs are CSV; delimiters or controls in one JSON address must
    not become another provider argument in the workflow shell."""
    with pytest.raises(rc.ProviderSpecError, match="Akash address"):
        rc.parse_providers(__import__("json").dumps([{"address": A + suffix}]))


def test_contradictory_markers_are_not_guessed():
    with pytest.raises(rc.ProviderSpecError, match="preferred/runner_host and runner_deny"):
        rc.parse_providers(f'[{{"address":"{A}","runner_host":true,"runner_deny":true}}]')


def test_non_integer_priority_is_rejected():
    with pytest.raises(rc.ProviderSpecError, match="failover_priority"):
        rc.parse_providers(f'[{{"address":"{A}","failover_priority":"soon"}}]')


# --------------------------------------------------------------------------
# Readiness + reporting
# --------------------------------------------------------------------------


def test_proven_host_count_matches_the_measured_fleet():
    """Exactly one proven host today — the single point of failure the quorum
    gated `runner-v1` on."""
    assert rc.proven_host_count(rc.parse_providers(__import__("json").dumps(FLEET))) == 1


def test_denied_hosts_do_not_count_toward_readiness():
    """A provider carrying BOTH markers must not inflate the readiness count.

    `_normalise` rejects that combination, so this can only arrive via an
    unnormalised mapping — which `proven_host_count` accepts by design, being
    total like `select_candidates`. The first version of this test used a
    deny-only provider, which does not exercise the clause at all: deleting
    `and not runner_deny` left it green. Mutation testing caught that.
    """
    contradictory = {"address": D, "runner_host": True, "runner_deny": True}
    assert rc.proven_host_count([contradictory]) == 0, (
        "a runner_deny provider was counted as a proven host — the readiness gate "
        "would then clear on providers that cannot schedule the runner"
    )
    fleet = [{"address": A, "runner_host": True}, contradictory]
    assert rc.proven_host_count(fleet) == 1


def test_report_warns_while_the_pool_is_one_provider_deep():
    ordered, denied = rc.select_candidates(FLEET)
    body = "\n".join(rc.render_report(ordered, denied, min_hosts=3))
    assert "one provider deep" in body
    assert "billed runners" in body


def test_report_names_deny_as_configuration_not_outage():
    """The whole point: an empty candidate list must not read as 'no capacity'."""
    ordered, denied = rc.select_candidates([FLEET[0], FLEET[2]])
    body = "\n".join(rc.render_report(ordered, denied))
    assert "NOT a market outage" in body
    assert body.count("::error") == 1


def test_report_warns_when_nothing_is_proven():
    ordered, denied = rc.select_candidates([{"address": T}])
    body = "\n".join(rc.render_report(ordered, denied))
    assert "No PROVEN runner host" in body


def test_a_healthy_pool_emits_no_warning():
    fleet = [{"address": a, "runner_host": True} for a in (A, X, T)]
    ordered, denied = rc.select_candidates(fleet)
    body = "\n".join(rc.render_report(ordered, denied, min_hosts=3))
    assert "::warning" not in body and "::error" not in body


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_bad_spec_exits_two_not_zero(capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert rc.main(["--providers", '[{"address":"nope"}]']) == 2
    assert "::error" in capsys.readouterr().err


def test_all_denied_exits_nonzero(capsys, monkeypatch):
    import json as _j

    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert rc.main(["--providers", _j.dumps([FLEET[0], FLEET[2]])]) == 1


def test_empty_spec_is_not_fatal(capsys, monkeypatch):
    """No spec means "caller did not supply one" — just-akash defaults then apply."""
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert rc.main(["--providers", ""]) == 0


def test_outputs_carry_the_ordered_candidates(tmp_path, monkeypatch, capsys):
    import json as _j

    out = tmp_path / "out"
    out.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    rc.main(["--providers", _j.dumps(FLEET), "--github-output"])
    body = out.read_text()
    assert f"candidates={A},{T}" in body, body
    output_lines = body.splitlines()
    assert f"preferred_candidates={A}" in output_lines, body
    assert f"fallback_candidates={T}" in output_lines, body
    assert f"excluded_candidates={D},{X}" in output_lines, body
    assert "proven_hosts=1" in body
    assert "denied=2" in body
