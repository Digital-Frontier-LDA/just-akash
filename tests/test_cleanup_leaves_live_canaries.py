"""The stale sweep must never judge the persistent canary stale.

⛔ THE DEFECT (measured 2026-10-22). sdl/canary.yaml says the canary "MUST NOT BE REAPED"
and rests that on its service name matching no stale rule. #241 then let STALE-owned reach
ANY owned service older than 48h, and a canary is owned and lives for weeks. From 09-09
every scheduled sweep printed all three live fleet canaries (onidc 1789100636167, alphavps
1789100716448, hetzner_hel 1789100804519, the exact dseqs in canary-targets.json) as
STALE-owned. The identity policy held their legacy group names, which kept them alive and
made every run exit 2: 40+ consecutive red runs with `stale (closable): 0`.

The run-level half (a canary carrying a CLOSABLE identity is still never closed, and the
sweep exits 0) lives with the identity harness in tests/test_cleanup_identity.py.
"""

from __future__ import annotations

import time

import pytest

from canary import ensure
from just_akash import cleanup_stale
from just_akash.cleanup_stale import (
    CANARY_SERVICE,
    KNOWN_VERDICTS,
    PLACEMENT_PREFIX,
    STALE_OWNED_AGE_SECONDS,
    STALE_VERDICTS,
    _wants_owned_provenance,
    classify,
)

# The live canaries were 40.9 days old on the day this was found.
AGES = [600, STALE_OWNED_AGE_SECONDS + 3600, 41 * 86400]


def _canary() -> dict:
    """Shape matches `_deployment_service_names`: the provider's live manifest."""
    return {"leases": [{"id": {"provider": "akash1prov"}, "status": {"services": {"canary": {}}}}]}


def _dseq_aged(seconds: float) -> str:
    return str(int((time.time() - seconds) * 1000))


def test_the_name_is_the_one_canary_ensure_adopts() -> None:
    """⛔ A RENAME ON ONE SIDE ALONE RE-ARMS THE CLOSE. canary/ensure.py recognises its
    deployment by this exact service set; if the two drift, the exclusion protects a name
    no canary carries and the owned age rule reaches the real one again."""
    assert CANARY_SERVICE == ensure.CANARY_SERVICE


@pytest.mark.parametrize("age", AGES)
@pytest.mark.parametrize(
    "group_names",
    [
        [f"{PLACEMENT_PREFIX}canary.09a75b4f3236"],  # the live canaries' legacy names
        [f"{PLACEMENT_PREFIX}idv1-class-ci-payload-g1-attempt-1-run-99-end"],
        None,  # unreadable provenance
    ],
)
@pytest.mark.parametrize("reap_owned", [True, False])
@pytest.mark.parametrize("reap_runners", [True, False])
def test_a_canary_is_left_whatever_its_age_or_ownership(
    age, group_names, reap_owned, reap_runners
) -> None:
    """The case that went red: owned, opted in, 41 days old. Every flag combination the
    scheduled and manual sweeps pass must land on the same verdict."""
    verdict, services, _ = classify(
        _canary(),
        _dseq_aged(age),
        reap_runners=reap_runners,
        group_names=group_names,
        reap_owned=reap_owned,
    )
    assert verdict == "LEAVE-canary", verdict
    assert services == ["canary"]


def test_the_verdict_is_known_and_never_closable() -> None:
    """KNOWN_VERDICTS gates the execute path: an unknown verdict makes --execute refuse the
    whole run. And a LEAVE verdict that sat in STALE_VERDICTS would close the canary."""
    assert "LEAVE-canary" in KNOWN_VERDICTS
    assert "LEAVE-canary" not in STALE_VERDICTS


@pytest.mark.parametrize("dseq", [_dseq_aged(41 * 86400), "not-a-dseq"])
def test_no_provenance_read_for_a_canary(dseq) -> None:
    """classify returns before group_names is consulted, so the chain round-trip would be
    read by nothing. Unaged too: the canary branch does not look at age either."""
    assert not _wants_owned_provenance(_canary(), dseq)


def test_a_canary_with_a_second_service_is_not_the_canary() -> None:
    """The exclusion is the EXACT set, as ensure.py adopts it. A deployment that merely
    includes a `canary` service is some other workload and keeps the ordinary rules."""
    detail = {"leases": [{"status": {"services": {"canary": {}, "app": {}}}}]}
    verdict, _, _ = classify(
        detail,
        _dseq_aged(41 * 86400),
        group_names=[f"{PLACEMENT_PREFIX}app"],
        reap_owned=True,
    )
    assert verdict == "STALE-owned", verdict


def test_the_module_docstring_lists_the_rule() -> None:
    """The docstring is the operator's map of what the sweep closes."""
    assert "LEAVE-canary" in (cleanup_stale.__doc__ or "")
