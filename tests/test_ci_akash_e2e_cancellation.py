"""A newer commit must not cancel a workflow that owns a live Akash lease."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def _unsafe_cancellation(source: str) -> tuple[bool, int]:
    document = yaml.safe_load(source)
    jobs = document.get("jobs", {})
    live_e2e_jobs = sum(
        1
        for job in jobs.values()
        if isinstance(job, dict)
        and isinstance(job.get("name"), str)
        and job["name"].startswith("E2E ")
        and any(
            "just_akash.test_" in str(step)
            for step in job.get("steps", [])
            if isinstance(step, dict)
        )
    )
    cancel = document.get("concurrency", {}).get("cancel-in-progress")
    return cancel is True and live_e2e_jobs > 0, live_e2e_jobs


def test_real_akash_e2e_workflow_cannot_be_cancelled_by_a_new_commit() -> None:
    source = WORKFLOW.read_text()
    unsafe, population = _unsafe_cancellation(source)
    assert population == 2, f"expected both real Akash E2E jobs, found {population}"
    assert not unsafe

    target = "  cancel-in-progress: false\n"
    assert source.count(target) == 1, "cancellation mutation target must be exact"
    mutant = source.replace(target, "  cancel-in-progress: true\n", 1)
    assert mutant != source
    assert _unsafe_cancellation(mutant) == (True, 2)


def test_cancellation_is_safe_when_no_job_can_create_an_akash_lease() -> None:
    control = """
concurrency:
  cancel-in-progress: true
jobs:
  lint:
    name: Ruff
    steps:
      - run: ruff check .
"""
    assert _unsafe_cancellation(control) == (False, 0)
