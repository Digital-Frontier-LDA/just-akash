"""The age rule must be reachable for ANY service whose ownership is proven on chain.

⛔ THE DEFECT (measured 2026-09-01). `stamp_run`'s docstring promises that an unstamped
deployment "falls back to the age rule". It could not. `classify` only reaches an age test
after matching one of three EXACT service names — `probe`, `backtest`, `runner`. Anything
else returns LEAVE-real-or-unknown, where no age is ever consulted.

Six Blazing-Back E2E deployments (service ``app``, images ``postgres:16-alpine`` and
``uzyexe/tetris``) therefore sat 62-139h holding 28.28 ACT while the scheduled reaper
reported ``stale (closable): 0`` on every run.

⚠ `E2E_SERVICE = "backtest"` is just-akash's OWN vocabulary. The sibling repo that shares
this sweeper names its service ``app``. A service-name allowlist is a CONVENTION between
repos; ownership is a FACT on chain. The allowlist was standing in for the fact.

⭐ So the fix does not add ``app`` to the allowlist — that would fix one instance and leave
the class, and the next repo with a fourth vocabulary would leak in exactly the same way.
It lets the ownership proof the runner branch already uses (``group_spec.name`` carrying our
placement prefix) gate the age rule for any service.
"""

from __future__ import annotations

import time

from just_akash.cleanup_stale import (
    PLACEMENT_PREFIX,
    STALE_OWNED_AGE_SECONDS,
    STALE_VERDICTS,
    classify,
)

OLD = STALE_OWNED_AGE_SECONDS + 3600
YOUNG = 600


def _detail(service: str = "app") -> dict:
    """Deployment detail whose only service is ``service``.

    ⚠ Shape matches `_deployment_service_names`, which reads
    ``leases[].status.services.<name>`` — the provider's live manifest, not the SDL. A
    fixture that puts services anywhere else classifies as LEAVE-unclassifiable and every
    assertion below would pass for the wrong reason.
    """
    return {"leases": [{"id": {"provider": "akash1prov"}, "status": {"services": {service: {}}}}]}


def _dseq_aged(seconds: float) -> str:
    """A dseq is ms-since-epoch, so age is decodable from it."""
    return str(int((time.time() - seconds) * 1000))


def _classify(service, age_s, group_names, reap_owned=True):
    return classify(
        _detail(service), _dseq_aged(age_s), group_names=group_names, reap_owned=reap_owned
    )[0]


def test_an_owned_aged_app_deployment_is_stale() -> None:
    """The case that leaked: service `app`, our prefix, older than the floor."""
    v = _classify("app", OLD, [f"{PLACEMENT_PREFIX}app"])
    assert v == "STALE-owned", v
    assert v in STALE_VERDICTS, "STALE-owned must be closable, or the verdict is decorative"


def test_the_same_deployment_is_left_alone_before_the_opt_in() -> None:
    """⛔ NO CALLER CHANGES BEHAVIOUR BY UPGRADING. Without `reap_owned` the verdict is
    exactly what it was, so this cannot surprise an existing scheduled sweep."""
    assert (
        _classify("app", OLD, [f"{PLACEMENT_PREFIX}app"], reap_owned=False)
        == "LEAVE-real-or-unknown"
    )


def test_a_foreign_prefix_is_refused_however_old() -> None:
    """⛔ THE KNOWN-NEGATIVE. These wallets are SHARED. A deployment that is old, opted-in
    and NOT ours must still be refused — otherwise this change destroys a sibling repo's
    work, which is the failure the runner branch was written to prevent."""
    assert _classify("app", OLD, ["someone-else-app"]) == "LEAVE-not-ours"
    assert (
        _classify("app", OLD, ["dfci-infra-lookalike".replace("dfci", "notdfci")])
        == "LEAVE-not-ours"
    )


def test_unreadable_ownership_is_not_unowned() -> None:
    """A failed chain read must never license a delete. Destroying on an unreadable is the
    same class of error as destroying on a guess."""
    assert _classify("app", OLD, None) == "LEAVE-unverified-owned"
    assert _classify("app", OLD, []) == "LEAVE-unverified-owned"


def test_a_young_owned_deployment_is_left_alone() -> None:
    """The age floor is the entire safety margin for a live workload."""
    assert _classify("app", YOUNG, [f"{PLACEMENT_PREFIX}app"]) == "LEAVE-recent-owned"


def test_the_existing_service_verdicts_are_unchanged() -> None:
    """⚠ NON-REGRESSION. The three original classes must keep their own thresholds and
    verdicts — this change adds a fallback, it does not re-route them."""
    assert _classify("runner", OLD, [f"{PLACEMENT_PREFIX}runner"]) != "STALE-owned"
    assert classify(_detail("backtest"), _dseq_aged(OLD), reap_owned=True)[0] == "STALE-e2e"


# ── the wiring, not just the rule ─────────────────────────────────────────────────────
#
# ⛔ THE RULE ABOVE WAS CORRECT AND UNREACHABLE. As first written, `run()` had no
# `reap_owned` parameter, `classify` was called POSITIONALLY (so the flag could never
# arrive), and provenance was read only for `services == {runner}` — so even a wired flag
# would have seen `group_names=None` and returned LEAVE-unverified-owned for everything.
#
# ⚠ Three independent breaks, each of which alone makes the feature inert, and NONE of
# which a test of `classify` can see. A rule nobody can invoke is the failure this estate
# keeps finding; these tests exist so it cannot recur silently here.


def test_run_accepts_the_flag() -> None:
    """`run()` must take `reap_owned` — without it the CLI has nothing to pass."""
    import inspect

    from just_akash.cleanup_stale import run

    assert "reap_owned" in inspect.signature(run).parameters, (
        "run() has no reap_owned parameter — the rule in classify() is unreachable"
    )


def test_classify_receives_the_flag_by_keyword() -> None:
    """⛔ POSITIONAL CALLS SILENTLY DROP A TRAILING PARAMETER.

    `classify(detail, dseq, now, reap_runners, names, prefix)` passes six positionals; the
    seventh keeps its default however the caller was invoked. Pinned as a keyword so adding
    a parameter cannot quietly disable this one.
    """
    import inspect

    from just_akash import cleanup_stale

    src = inspect.getsource(cleanup_stale.run)
    assert "reap_owned=reap_owned" in src, (
        "run() does not pass reap_owned to classify() by keyword — the flag is inert"
    )


def test_provenance_is_read_when_the_flag_is_on() -> None:
    """The verdict rests on `group_spec.name`, so the sweep must fetch it for ANY service.

    Reading it only for `runner` leaves every other service at group_names=None, which
    returns LEAVE-unverified-owned — a sweep that reports a clean account it never judged.
    """
    import inspect

    from just_akash import cleanup_stale

    src = inspect.getsource(cleanup_stale.run)
    # ⚠ Matched on the GATE, not the whole line. The first version pinned the literal
    # `elif reap_owned:` and went red when the branch gained a narrowing predicate — a test
    # failing on a refactor that preserved its subject. It must still catch REMOVAL, which
    # the group_names count does.
    assert "elif reap_owned" in src and src.count("deployment_group_names") >= 2, (
        "provenance is fetched only for runners — reap_owned would always be unverified"
    )


def test_the_cli_exposes_the_flag() -> None:
    """An operator has to be able to turn it on, or the scheduled sweep never will."""
    import inspect

    from just_akash import cleanup_stale

    src = inspect.getsource(cleanup_stale)
    assert '"--reap-owned"' in src, "no --reap-owned CLI flag"
    assert "reap_owned=args.reap_owned" in src, "the CLI flag is parsed but never passed to run()"


# ── the provenance read is narrowed to what classify actually consults ────────────────


def test_provenance_is_skipped_where_classify_ignores_it() -> None:
    """⛔ THE PREDICATE MUST MIRROR classify's EARLY RETURNS.

    `{probe}`, `{backtest}`, `{runner}` and the empty set all return BEFORE the reap_owned
    branch, so `group_names` is never consulted for them. The first version read provenance
    for every deployment regardless — a chain round-trip each, on an account of hundreds,
    for a value nothing looks at.

    ⚠ This pins the PAIRING, not the saving. If a future edit gives one of those service
    sets a reap_owned path, this test goes red and the predicate must be updated with it —
    which is the point: a skipped read that classify later needs returns
    LEAVE-unverified-owned, i.e. unreadable dressed as unowned.
    """
    from just_akash.cleanup_stale import (
        E2E_SERVICE,
        PROBE_SERVICE,
        RUNNER_SERVICE,
        _wants_owned_provenance,
    )

    for svc in (PROBE_SERVICE, E2E_SERVICE, RUNNER_SERVICE):
        assert not _wants_owned_provenance(_detail(svc), _dseq_aged(OLD)), (
            f"provenance read for services=={{{svc}}}, whose classify branch returns before "
            "group_names is consulted — a wasted chain round-trip"
        )
    assert not _wants_owned_provenance({"leases": []}, _dseq_aged(OLD)), (
        "provenance read for a deployment with no services (LEAVE-unclassifiable)"
    )


def test_provenance_is_read_for_the_case_that_leaked() -> None:
    """Anti-vacuity for the test above: if it never returned True, the flag would be inert
    again and every assertion there would pass while nothing was ever judged."""
    from just_akash.cleanup_stale import _wants_owned_provenance

    assert _wants_owned_provenance(_detail("app"), _dseq_aged(OLD)), (
        "service `app` older than the floor is exactly the leaked class — it MUST be read"
    )


def test_a_young_deployment_needs_no_provenance() -> None:
    """Younger than the floor returns LEAVE-recent-owned whatever the ownership, so the read
    is unread. On a busy account this is the majority case and most of the saving."""
    from just_akash.cleanup_stale import _wants_owned_provenance

    assert not _wants_owned_provenance(_detail("app"), _dseq_aged(YOUNG))


def test_an_unaged_deployment_is_still_read() -> None:
    """⛔ UNREADABLE IS NOT UNOWNED, HERE TOO. An undecodable dseq means classify cannot rule
    on age; skipping the read would silently produce LEAVE-unverified-owned rather than a
    verdict about ownership."""
    from just_akash.cleanup_stale import _wants_owned_provenance

    assert _wants_owned_provenance(_detail("app"), "not-a-dseq")
