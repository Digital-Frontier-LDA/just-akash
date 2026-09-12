"""A failed or partial runner-group page is never counted as an empty population."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from just_akash import jit_pool

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/runner-pool.yml"


def _journal() -> dict[str, object]:
    return {
        "group_id": 17,
        "runner_label": "pool-1",
        "operation_label": "pool-1",
        "runners": [
            {
                "id": 101,
                "name": "runner-one",
                "slot": "one",
                "labels": ["self-hosted", "linux", "akash", "pool-1", "pool-1-one"],
            }
        ],
    }


def test_concatenated_pagination_documents_are_all_decoded() -> None:
    raw = (
        json.dumps({"total_count": 1, "runners": []})
        + "\n"
        + json.dumps({"total_count": 1, "runners": [{"id": 101}]})
    )
    documents = jit_pool._decode_json_documents(raw)
    assert len(documents) == 2
    second = documents[1]
    assert isinstance(second, dict) and second["runners"] == [{"id": 101}]


def test_an_error_page_is_unknown_not_an_empty_page() -> None:
    with pytest.raises(RuntimeError, match="unreadable"):
        jit_pool.observe_group_population(
            _journal(),
            [
                {"total_count": 1, "runners": []},
                {"message": "API rate limit exceeded"},
            ],
        )


def test_a_truncated_page_is_unknown_not_a_smaller_population() -> None:
    with pytest.raises(RuntimeError, match="truncated"):
        jit_pool.observe_group_population(
            _journal(),
            [{"total_count": 1, "runners": []}],
        )


def test_a_complete_empty_group_is_readable_but_not_ready() -> None:
    result = jit_pool.observe_group_population(
        _journal(),
        [{"total_count": 0, "runners": []}],
    )
    assert result["online"] == 0
    assert result["expected"] == 1


def test_workflow_captures_observer_failure_before_classifying_the_pool() -> None:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    body = next(
        step["run"] for step in document["jobs"]["pool"]["steps"] if step.get("id") == "provision"
    )
    start = body.index("OBSERVATION=")
    end = body.index('if [ "$API_OK"', start)
    poll = body[start:end]
    assert 'observe --journal "$JIT_JOURNAL"' in poll
    assert "|| GH_RC=$?" in poll
    assert 'if [ "$GH_RC" -ne 0 ]' in poll
    assert "continue" in poll
