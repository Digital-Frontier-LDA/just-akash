"""Execute private cleanup and its verdict without GitHub or Akash access."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
LABEL = "podman-images-123-2"
ROW = {
    "id": 701,
    "name": f"just-akash-{LABEL}-aabbcc",
    "busy": False,
    "status": "offline",
    "labels": [{"name": value} for value in (LABEL, "self-hosted", "akash")],
}


def steps():
    return yaml.safe_load((ROOT / ".github/workflows/runner-teardown.yml").read_text())["jobs"][
        "teardown"
    ]["steps"]


def private_step():
    return next(step for step in steps() if step.get("id") == "dereg_private")


def verdict_step():
    return next(
        step
        for step in steps()
        if step.get("name") == "Require measured private repository cleanup"
    )


def verdict(outcome="success", removed="0", failed="0"):
    return subprocess.run(
        ["/bin/bash", "-e", "-c", verdict_step()["run"]],
        env={"CLEANUP_OUTCOME": outcome, "DEREGISTERED": removed, "DEREGISTER_FAILED": failed},
        capture_output=True,
        text=True,
        timeout=10,
    )


def cleanup(
    tmp_path,
    *,
    rows=(),
    current=None,
    pages=None,
    exact_ids="",
    identity_fail_at=0,
    read_rc=0,
    list_rc=0,
    delete_rc=0,
    env_changes=None,
    body=None,
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    program = bindir / "python3"
    program.write_text(
        f"#!{sys.executable}\n"
        "import os,sys\nfrom pathlib import Path\n"
        "sys.stdin.read()\np=Path(os.environ['IDENTITY_READS'])\n"
        "n=int(p.read_text())+1 if p.exists() else 1\np.write_text(str(n))\n"
        "raise SystemExit(int(n==int(os.environ['IDENTITY_FAIL_AT'])))\n"
    )
    program.chmod(0o700)
    gh = bindir / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json,os,sys\nfrom pathlib import Path\n"
        "args=sys.argv[1:]\np=Path(os.environ['CALLS'])\n"
        "with p.open('a') as f: f.write(json.dumps(args)+'\\n')\n"
        "if '--paginate' in args:\n"
        " print(os.environ['PAGES']); raise SystemExit(int(os.environ['FAKE_LIST_RC']))\n"
        "if 'DELETE' in args: raise SystemExit(int(os.environ['DELETE_RC']))\n"
        "print(os.environ['CURRENT']); raise SystemExit(int(os.environ['READ_RC']))\n"
    )
    gh.chmod(0o700)
    output = tmp_path / "outputs"
    env = {
        "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
        "GH_TOKEN": "PATCANARY",
        "ORG": "Borduas-Holdings",
        "REPOSITORY": "Borduas-Holdings/blazing",
        "CALLER": "Borduas-Holdings/blazing",
        "RUN_ID": "123",
        "RUN_ATTEMPT": "2",
        "RUNNER_LABEL": LABEL,
        "RUNNER_IDS_JSON": exact_ids,
        "GITHUB_OUTPUT": str(output),
        "IDENTITY_READS": str(tmp_path / "identity-reads"),
        "IDENTITY_FAIL_AT": str(identity_fail_at),
        "CALLS": str(tmp_path / "calls"),
        "PAGES": json.dumps(
            pages if pages is not None else [{"total_count": len(rows), "runners": rows}]
        ),
        "CURRENT": json.dumps(current if current is not None else ROW),
        "READ_RC": str(read_rc),
        "FAKE_LIST_RC": str(list_rc),
        "DELETE_RC": str(delete_rc),
    }
    env.update(env_changes or {})
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", body or private_step()["run"]],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    calls = (
        [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
        if (tmp_path / "calls").exists()
        else []
    )
    final = verdict(
        "success" if result.returncode == 0 else "failure",
        values["deregistered"],
        values["deregister_failed"],
    )
    return result, final, calls, values


def test_private_cleanup_and_verdict_always_run_without_ignoring_failures():
    for step in (private_step(), verdict_step()):
        assert step["if"] == "always() && inputs.github-repository != ''"
        assert not step.get("continue-on-error", False)


@pytest.mark.parametrize("removed", ["0", "1", "18446744073709551615"])
def test_measured_successful_counts_pass(removed):
    assert verdict(removed=removed).returncode == 0


@pytest.mark.parametrize(
    "outcome,removed,failed",
    [
        ("failure", "0", "0"),
        ("skipped", "0", "0"),
        ("cancelled", "0", "0"),
        ("", "0", "0"),
        ("success", "", "0"),
        ("success", "unmeasured", "0"),
        ("success", "-1", "0"),
        ("success", "00", "0"),
        ("success", "18446744073709551616", "0"),
        ("success", "0", ""),
        ("success", "0", "unmeasured"),
        ("success", "1", "1"),
        ("success", "0\nSYNTHETICECHO", "0"),
    ],
)
def test_failed_missing_invalid_or_unmeasured_evidence_never_passes(outcome, removed, failed):
    result = verdict(outcome, removed, failed)
    assert result.returncode == 1
    assert "SYNTHETICECHO" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "change", [{"GH_TOKEN": ""}, {"REPOSITORY": "foreign/blazing"}, {"RUNNER_LABEL": "foreign"}]
)
def test_early_unmeasured_exit_fails_without_any_github_request(tmp_path, change):
    result, final, calls, values = cleanup(tmp_path, env_changes=change)
    assert result.returncode == final.returncode == 1
    assert calls == [] and values["deregister_failed"] == "unmeasured"


def test_complete_zero_population_requires_fresh_identity_after_enumeration(tmp_path):
    result, final, calls, values = cleanup(tmp_path)
    assert result.returncode == final.returncode == 0
    assert values == {"deregistered": "0", "deregister_failed": "0"}
    assert (tmp_path / "identity-reads").read_text() == "2"
    assert len(calls) == 1 and "--slurp" in calls[0]


def test_private_identity_moved_after_zero_enumeration_is_held(tmp_path):
    result, final, calls, _ = cleanup(tmp_path, identity_fail_at=2)
    assert result.returncode == final.returncode == 1
    assert len(calls) == 1 and all("DELETE" not in call for call in calls)


def test_removing_post_enumeration_identity_guard_falsely_passes_same_failure(tmp_path):
    body = private_step()["run"]
    target = (
        "# repository still has the same fresh server-derived identity.\n"
        "verify_repository || hold_cleanup"
    )
    assert body.count(target) == 1
    mutant = body.replace(target, "# mutation removes only the post-enumeration check\ntrue")
    result, final, calls, _ = cleanup(tmp_path, identity_fail_at=2, body=mutant)
    assert result.returncode == final.returncode == 0
    assert len(calls) == 1


@pytest.mark.parametrize(
    "options",
    [
        {"list_rc": 1},
        {"identity_fail_at": 1},
        {"pages": []},
        {"pages": [{"total_count": 1, "runners": []}]},
        {"pages": [{"total_count": 0, "runners": []}, {"total_count": 1, "runners": []}]},
        {"rows": [ROW | {"busy": True}]},
        {"rows": [ROW | {"name": "another-operation"}]},
        {"exact_ids": "[701]", "read_rc": 1},
        {"exact_ids": "[701]", "current": ROW | {"busy": True}},
        {"exact_ids": "[701]", "current": ROW | {"name": "another-operation"}},
        {"rows": [ROW], "current": ROW | {"busy": True}},
        {"rows": [ROW], "delete_rc": 1},
    ],
)
def test_failed_reads_busy_moved_identity_or_incomplete_population_cannot_green(tmp_path, options):
    result, final, calls, values = cleanup(tmp_path, **options)
    assert result.returncode == final.returncode == 1
    assert values["deregister_failed"] == "unmeasured"
    if not options.get("delete_rc"):
        assert all("DELETE" not in call for call in calls)


def test_actual_removal_is_measured_in_the_same_fixed_repository(tmp_path):
    result, final, calls, values = cleanup(tmp_path, rows=[ROW])
    assert result.returncode == final.returncode == 0
    assert values == {"deregistered": "1", "deregister_failed": "0"}
    assert calls[-1] == [
        "api",
        "-X",
        "DELETE",
        "repos/Borduas-Holdings/blazing/actions/runners/701",
    ]
    assert all("orgs/" not in str(call) for call in calls)


def test_partial_removal_count_is_retained_when_next_identity_read_fails(tmp_path):
    result, final, calls, values = cleanup(
        tmp_path, rows=[ROW, ROW | {"id": 702}], identity_fail_at=4
    )
    assert result.returncode == final.returncode == 1
    assert values == {"deregistered": "1", "deregister_failed": "unmeasured"}
    deleted = [call for call in calls if "DELETE" in call]
    assert deleted == [
        ["api", "-X", "DELETE", "repos/Borduas-Holdings/blazing/actions/runners/701"]
    ]
