"""Exact IDs win over status; label recovery holds runners that may be executing."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/runner-teardown.yml"


def _body() -> str:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return next(
        step["run"] for step in doc["jobs"]["teardown"]["steps"] if step.get("id") == "dereg"
    )


def _run(
    tmp_path: Path,
    *,
    runner_ids: str,
    listing: Mapping[str, object],
    body: str | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls"
    runners = listing.get("runners")
    exact = runners[0] if isinstance(runners, list) and runners else {}
    gh = fake_bin / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$CALLS"\n'
        'case " $* " in\n'
        '  *" -X DELETE "*) exit 0 ;;\n'
        f"  *\"orgs/example/actions/runners/701\"*) printf '%s\\n' '{json.dumps(exact)}' ;;\n"
        f"  *) printf '%s\\n' '{json.dumps(listing)}' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return subprocess.run(
        ["/bin/bash", "-e", "-c", body or _body()],
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "GH_TOKEN": "test",
            "ORG": "example",
            "RUNNER_LABEL": "pool-operation",
            "RUNNER_IDS_JSON": runner_ids,
            "GITHUB_OUTPUT": str(tmp_path / "output"),
            "CALLS": str(calls),
        },
        text=True,
        capture_output=True,
        check=False,
    )


def test_exact_identity_is_deleted_even_when_online_and_busy(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        runner_ids="[701]",
        listing={
            "runners": [
                {
                    "id": 701,
                    "name": "just-akash-pool-abcdef",
                    "status": "online",
                    "busy": True,
                    "labels": [
                        {"name": "self-hosted"},
                        {"name": "akash"},
                        {"name": "pool-operation"},
                    ],
                }
            ]
        },
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text(encoding="utf-8")
    assert "-X DELETE orgs/example/actions/runners/701" in calls
    assert "actions/runners?per_page=100" not in calls


def test_label_recovery_holds_online_or_busy_identity(tmp_path: Path) -> None:
    listing = {
        "runners": [
            {
                "id": 701,
                "status": "online",
                "busy": True,
                "labels": [{"name": "pool-operation"}],
            }
        ]
    }
    result = _run(tmp_path, runner_ids="[]", listing=listing)
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text(encoding="utf-8")
    assert "actions/runners?per_page=100" in calls
    assert "-X DELETE" not in calls


def test_exact_wrong_label_holds_the_complete_delete_population(tmp_path: Path) -> None:
    listing = {
        "runners": [
            {
                "id": 701,
                "name": "just-akash-pool-abcdef",
                "status": "offline",
                "busy": False,
                "labels": [
                    {"name": "self-hosted"},
                    {"name": "akash"},
                    {"name": "another-operation"},
                ],
            }
        ]
    }
    result = _run(tmp_path, runner_ids="[701]", listing=listing)
    assert result.returncode == 0, result.stderr
    assert "-X DELETE" not in (tmp_path / "calls").read_text(encoding="utf-8")
    assert "deregister_failed=unmeasured" in (tmp_path / "output").read_text()


def test_duplicate_exact_id_population_is_held_before_any_api_call(tmp_path: Path) -> None:
    result = _run(tmp_path, runner_ids="[701,701]", listing={"runners": []})
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "calls").exists()
    assert "deregister_failed=unmeasured" in (tmp_path / "output").read_text()


def test_exact_id_call_site_mutation_changes_the_cleanup_effect(tmp_path: Path) -> None:
    body = _body()
    target = 'if [ -n "${RUNNER_IDS_JSON:-}" ] && [ "$RUNNER_IDS_JSON" != "[]" ]; then'
    assert body.count(target) == 1
    mutant = body.replace(target, "if false; then")
    listing = {
        "runners": [
            {
                "id": 701,
                "status": "online",
                "busy": True,
                "name": "just-akash-pool-abcdef",
                "labels": [
                    {"name": "self-hosted"},
                    {"name": "akash"},
                    {"name": "pool-operation"},
                ],
            }
        ]
    }
    result = _run(tmp_path, runner_ids="[701]", listing=listing, body=mutant)
    assert result.returncode == 0, result.stderr
    assert "-X DELETE" not in (tmp_path / "calls").read_text(encoding="utf-8")


def test_operation_label_binding_effect_mutation_deletes_the_wrong_runner(tmp_path: Path) -> None:
    body = _body()
    target = " and any(.labels[].name; .==$L)"
    assert body.count(target) == 1
    mutant = body.replace(target, "")
    listing = {
        "runners": [
            {
                "id": 701,
                "name": "just-akash-pool-abcdef",
                "status": "offline",
                "busy": False,
                "labels": [
                    {"name": "self-hosted"},
                    {"name": "akash"},
                    {"name": "another-operation"},
                ],
            }
        ]
    }
    result = _run(tmp_path, runner_ids="[701]", listing=listing, body=mutant)
    assert result.returncode == 0, result.stderr
    assert "-X DELETE orgs/example/actions/runners/701" in (tmp_path / "calls").read_text(
        encoding="utf-8"
    )
