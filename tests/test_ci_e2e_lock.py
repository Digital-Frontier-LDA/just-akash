"""Two PRs' real-Akash E2E jobs must never run at the same time (#267).

Both E2E jobs deploy with the same AKASH_API_KEY (one Akash account). Inside one run,
`needs: e2e-shell` already serialises them, but ci.yml's workflow concurrency group is per
ref, so two PRs' runs overlap freely and collide on the account (account-sequence
mismatches, a sibling's deployment read as stale). The ruling on #267 is a job-level lock
in ci.yml: one repo-wide group shared by exactly the jobs that hold a lease, never the
reusable runner pool (that would serialise every caller's CI) and never `akash-wallet`
(that would let CI cancel a queued maintenance run, or the reverse).
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
LOCK = "akash-ci-e2e"


def _lease_jobs(document: dict) -> dict[str, dict]:
    return {
        job_id: job
        for job_id, job in document.get("jobs", {}).items()
        if isinstance(job, dict)
        and any(
            "just_akash.test_" in str(step)
            for step in job.get("steps", [])
            if isinstance(step, dict)
        )
    }


def _lock_of(job: dict) -> tuple[object, object]:
    concurrency = job.get("concurrency")
    if not isinstance(concurrency, dict):
        return None, None
    return concurrency.get("group"), concurrency.get("cancel-in-progress")


def test_every_lease_holding_e2e_job_takes_the_one_repo_wide_lock() -> None:
    jobs = _lease_jobs(yaml.safe_load(WORKFLOW.read_text()))
    assert set(jobs) == {"e2e-shell", "e2e-secrets"}, sorted(jobs)
    for job_id, job in jobs.items():
        group, cancel = _lock_of(job)
        assert group == LOCK, f"{job_id}: concurrency group is {group!r}, want {LOCK!r}"
        # A literal: any ${{ ... }} (ref, run id, PR number) would split the lock per run.
        assert "${{" not in str(group), job_id
        # Never cancel a job that may own a live lease; it must get to its own cleanup.
        assert cancel is False, f"{job_id}: cancel-in-progress must be false, got {cancel!r}"


def test_the_lock_is_not_the_maintenance_mutex() -> None:
    assert LOCK != "akash-wallet"
    text = WORKFLOW.read_text()
    assert "group: akash-wallet" not in text


def test_a_job_without_a_lease_does_not_take_the_lock() -> None:
    """THE CONTROL: lint, typecheck and unit tests stay unserialised across PRs."""
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    for job_id in ("lint", "typecheck", "unit-tests"):
        assert _lock_of(jobs[job_id])[0] is None, job_id
