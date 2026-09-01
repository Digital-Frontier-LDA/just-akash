"""A provider-closed deployment is closable when — and only when — ownership is proven.

⛔ THE DEFECT (Blazing-Back #1763, observed 2026-09-01). A deployment the provider
stopped stays OPEN on chain, holds its escrow, and with auto top-up active keeps
drawing funds into something running nothing. Console's own text: "close it to recover
any unused funds" — an explicit close is the only thing that recovers them.

The classifier could not see it BY CONSTRUCTION: `classify`'s empty-services branch
returned LEAVE-unclassifiable BEFORE the reap_owned path could run, so the one class
that most needs closing was unreachable by the one proof that would license it. The
scheduled sweep (run 33431994913) reported `stale (closable): 0` while dseq
1788245492506 (group ``dfci-infra-app``, $5.00 balance, auto top-up Active) sat in
exactly that state.

⭐ THE FIX ADDS A VERDICT, NEVER A WIDENING. LEAVE-unclassifiable remains the verdict
for every caller that has not opted into ``--reap-owned``, and for every opted-in
caller it survives as the fall-through AFTER the ownership proof fails: unreadable
provenance, a foreign or unattributable group name, or an age under the floor. The
safe default is not relaxed — a proof is inserted in front of it.

⚠ THE FLOOR IS THE PROBE'S, NOT THE OWNED 48h. A provider-closed deployment has no
workload lifetime to protect (it cannot become live again; only a NEW deployment can
replace it). The floor guards the READ RACE: a healthy deployment minutes old may
show no live service manifest yet — run 33431994913 printed `age= 0.0d  services=-`
for dseq 1788201742510 — and MIN_ORPHAN_AGE_SECONDS (1h) is this module's smallest
existing margin over every deploy→lease→manifest window in this fleet's pipelines.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from just_akash import cleanup_stale as cs
from just_akash.cleanup_stale import (
    MIN_ORPHAN_AGE_SECONDS,
    STALE_VERDICTS,
    _wants_owned_provenance,
    classify,
)

NOW = time.time()
OLD = MIN_ORPHAN_AGE_SECONDS + 3600  # past the probe floor
YOUNG = 600  # 10 min: inside the read-race window

# The prefix a caller declares as ITS OWN. The shared wallet demonstrably hosts other
# repos' deployments, so "ours" is a parameter of the reap — here, the sibling's
# vocabulary, standing in for any repo's declared prefix.
OURS = "dfci-infra-"


def _detail_closed() -> dict:
    """A provider-closed deployment: a lease whose live service manifest is empty.

    ⚠ Shape matches `_deployment_service_names`, which reads
    ``leases[].status.services`` — the provider's LIVE manifest, not the SDL. Empty is
    what the provider-stopped state produces, and what the read race produces for a
    deployment not yet leased. A fixture that expressed "closed" any other way would
    classify as something else and pass for the wrong reason.
    """
    return {"leases": [{"id": {"provider": "akash1prov"}, "status": {"services": {}}}]}


def _dseq_aged(seconds: float) -> str:
    return str(int((NOW - seconds) * 1000))


def _verdict(group_names, age_s=OLD, reap_owned=True):
    return classify(
        _detail_closed(),
        _dseq_aged(age_s),
        NOW,
        group_names=group_names,
        placement_prefix=OURS,
        reap_owned=reap_owned,
    )[0]


def test_an_owned_provider_closed_deployment_is_stale() -> None:
    """KNOWN-POSITIVE: the observed shape (#1788245492506, group dfci-infra-app)."""
    v = _verdict([f"{OURS}app"])
    assert v == "STALE-provider-closed", v
    assert v in STALE_VERDICTS, (
        "STALE-provider-closed must be closable, or the verdict is decorative"
    )


def test_the_default_is_not_widened_by_the_opt_in() -> None:
    """⛔ THE NO-WIDENING PIN. Without `reap_owned` the verdict is EXACTLY what it was.

    The module's own header rule — "unknown age -> LEAVE, never mis-age and reap
    wrongly" — is correct in isolation and has prevented real damage. This branch must
    insert a proof IN FRONT of that default, never loosen it: an existing caller that
    has not opted in sees LEAVE-unclassifiable for the same deployment that an opted-in
    caller may close.
    """
    assert _verdict([f"{OURS}app"], reap_owned=False) == "LEAVE-unclassifiable"


def test_unreadable_provenance_is_still_a_leave() -> None:
    """UNREADABLE is not UNOWNED, and here it is not CLASSIFIABLE either."""
    v = _verdict([])
    assert v == "LEAVE-unverified-provider-closed", v
    assert v not in STALE_VERDICTS


def test_foreign_and_unattributable_groups_are_refused_however_old() -> None:
    """⛔ THE KNOWN-NEGATIVES, NAMED RATHER THAN COUNTED (each prints what it spared).

    The shared wallet carries other repos' deployments; the account's records are not
    one project's. `group_spec.name` is author-controlled, so only names carrying the
    caller's declared prefix are closable. Everything else — another repo's scheme, or
    a bare unattributable name — is left exactly where the safe default leaves it.
    """
    for group in ("just-akash-runner.15", "strategy-repo-prod", "dcloud", "akash1jadu3abc"):
        v = _verdict([group])
        assert v == "LEAVE-not-ours-provider-closed", f"group={group!r} was judged {v!r}"
        print(f"spared (not ours): group={group!r}")
        assert v not in STALE_VERDICTS


def test_the_read_race_window_is_respected() -> None:
    """Young empty-services is the read race, not the leak — observed at age=0.0d."""
    v = _verdict([f"{OURS}app"], age_s=YOUNG)
    assert v == "LEAVE-young-or-unaged-provider-closed", v


def test_unknown_age_is_never_mis_aged() -> None:
    """A legacy block-height dseq has no decodable age; the module rule survives here."""
    verdict, _, age = classify(
        _detail_closed(),
        "1234567",
        NOW,
        group_names=[f"{OURS}app"],
        placement_prefix=OURS,
        reap_owned=True,
    )
    assert verdict == "LEAVE-young-or-unaged-provider-closed" and age is None


def test_provenance_is_requested_for_the_old_empty_set_and_skipped_for_the_young() -> None:
    """The fetch predicate must keep mirroring classify — including the NEW branch.

    The empty set used to be provably-skipped (nothing consulted group_names for it).
    It now has an owned path, so an old-or-unaged empty set MUST be read or the verdict
    silently downgrades to LEAVE-unverified-*: unreadable dressed as unowned.
    """
    assert _wants_owned_provenance(_detail_closed(), _dseq_aged(OLD))
    assert _wants_owned_provenance(_detail_closed(), "1234567")  # unaged: still read
    assert not _wants_owned_provenance(_detail_closed(), _dseq_aged(YOUNG))


def _run_level_mocks(client, group_names, dseq):
    # ⚠ The dseq must be the REAL ms-epoch string: age is decoded FROM it, and a literal
    # like "d1" is an UNAGED deployment that classifies LEAVE-young-or-unaged by the
    # module's own never-mis-age rule — a fixture that forgot this would pass the mock
    # and fail the close, looking like a wiring bug.
    records = [{"deployment": {"state": "active", "id": {"owner": "akash1me", "dseq": dseq}}}]
    return (
        patch.object(cs, "AkashConsoleAPI", return_value=client),
        patch.object(cs.chain, "list_active_deployments", return_value=records),
        patch.object(cs.chain, "deploy_credit", return_value={"uact": 100_000_000}),
        patch.object(
            cs,
            "escrow_locked",
            return_value={"locked_uact": 50_000_000, "deployments": 1, "by_deployment": {}},
        ),
        patch.object(cs.chain, "deployment_group_names", return_value=group_names),
        patch.dict("os.environ", {"AKASH_API_KEY": "k"}),
        patch.object(cs.time, "sleep", lambda s: None),
    )


def test_run_closes_it_prints_the_group_and_spares_the_unreadable(capsys) -> None:
    """Both halves at run() level: the chain read happens, the group NAME is printed
    (per-deployment ownership proof in the report itself), the owned one closes on
    --execute, and the unreadable sibling is left alone."""
    closed_owned = _dseq_aged(OLD)
    closed_unreadable = _dseq_aged(OLD)

    def _client() -> MagicMock:
        c = MagicMock()
        c.account_address.return_value = "akash1me"
        c.get_deployment.side_effect = lambda d: _detail_closed()
        return c

    for dseq, groups in ((closed_owned, [f"{OURS}app"]), (closed_unreadable, [])):
        client = _client()
        patches = _run_level_mocks(client, groups, dseq)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            rc = cs.run(execute=True, now=NOW, reap_owned=True, placement_prefix=OURS)
        assert rc == 0
        if groups:
            client.close_deployment.assert_called_once_with(dseq)
            assert f"group={OURS}app" in capsys.readouterr().out
        else:
            client.close_deployment.assert_not_called()
            out = capsys.readouterr().out
            assert "LEAVE-unverified-provider-closed" in out and "group=?" in out
