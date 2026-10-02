"""Ownership is independent of cheap/proven/preferred third-party fallback."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from just_akash import runner_candidates as rc

OWNED = "akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z"
# Public Akash provider address, also present in test_runner_candidates.py.
FOREIGN = "akash15tl6v6gd0nte0syyxnv57zmmspgju4c3xfmdhk"  # pragma: allowlist secret
FLEET = json.dumps(
    [
        {"address": OWNED, "preferred": False},
        {"address": FOREIGN, "preferred": True, "runner_host": True},
    ]
)
ROOT = Path(__file__).resolve().parents[1]


def test_preference_and_runner_host_do_not_grant_ownership():
    retained = rc.restrict_to_owned(rc.parse_providers(FLEET), OWNED)
    assert [entry["address"] for entry in retained] == [OWNED]
    assert not retained[0]["preferred"]


@pytest.mark.parametrize("owned", ["[]", " ,\n", "[", "akash1typo", json.dumps([FOREIGN])])
def test_bad_or_disjoint_ownership_policy_cannot_fall_through_to_defaults(owned):
    with pytest.raises(rc.ProviderSpecError):
        rc.restrict_to_owned(rc.parse_providers(OWNED), owned)


def test_owned_policy_cannot_resurrect_a_standing_denied_provider():
    providers = rc.parse_providers(json.dumps([{"address": OWNED, "runner_deny": True}]))
    with pytest.raises(rc.ProviderSpecError, match="no eligible"):
        rc.restrict_to_owned(providers, OWNED)


def test_empty_provider_spec_with_ownership_policy_is_a_hard_error():
    with pytest.raises(rc.ProviderSpecError, match="no eligible"):
        rc.restrict_to_owned([], OWNED)


@pytest.mark.parametrize("owned,success", [(OWNED, True), ("[]", False), ("", True)])
def test_effective_workflow_step_filters_both_auction_tiers(tmp_path, owned, success):
    """Execute the real YAML step; the shim removes only dependency installation."""
    document = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())
    step = next(s for s in document["jobs"]["pool"]["steps"] if s.get("id") == "candidates")
    assert step["env"]["AKASH_OWNED_PROVIDERS"] == "${{ inputs.owned-providers }}"
    shim = tmp_path / "uv"
    shim.write_text(
        "#!/usr/bin/env python3\nimport os,sys\n"
        "i=sys.argv.index('python')\n"
        "os.execv(sys.executable,[sys.executable,*sys.argv[i+1:]])\n"
    )
    shim.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "PYTHONPATH": str(ROOT),
            "GITHUB_OUTPUT": str(output),
            "AKASH_PROVIDERS_SPEC": FLEET,
            "AKASH_OWNED_PROVIDERS": owned,
        },
    )
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    if not success:
        assert values == {"failure_reason": "NO_ELIGIBLE_BIDDER"}
    elif owned:
        assert values["preferred_candidates"] == ""
        assert values["fallback_candidates"] == OWNED
        assert FOREIGN not in values["candidates"]
        assert values["proven_hosts"] == "0"
    else:
        assert values["preferred_candidates"] == FOREIGN
        assert values["fallback_candidates"] == OWNED


@pytest.mark.parametrize(
    "preferred,fallback,excluded,expected_preferred,expected_backup",
    [
        (OWNED, "", "", [OWNED], []),
        ("", OWNED, "", [], [OWNED]),
        (OWNED, FOREIGN, FOREIGN, [OWNED], []),
        (OWNED, FOREIGN, "", [OWNED], [FOREIGN]),
    ],
)
def test_effective_deploy_argv_cannot_restore_inherited_tiers(
    tmp_path, monkeypatch, preferred, fallback, excluded, expected_preferred, expected_backup
):
    from just_akash.cli import main
    from just_akash.deploy import _resolve_tier

    document = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())
    step = next(s for s in document["jobs"]["pool"]["steps"] if s.get("id") == "provision")
    body = step["run"]
    selection = body[body.index("PROV_ARGS=()") : body.index("SELECT_ARGS=()")]
    # Execute the workflow's real assignment + deploy invocation. Only the deploy
    # transport is replaced; the real CLI parses the resulting argv below.
    start = body.index("AKASH_PROVIDERS='' ")
    invocation = body[start : body.index("rm -f /tmp/runner-sdl.minted.yaml", start)]
    shim = tmp_path / "record"
    shim.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys\n"
        "print(json.dumps({'argv':sys.argv[1:],"
        "'preferred':os.environ['AKASH_PROVIDERS'],"
        "'backup':os.environ['AKASH_PROVIDERS_BACKUP']}))\n"
    )
    shim.chmod(0o755)
    script = selection + '\nJA=("$RECORDER")\nSELECT_ARGS=()\n' + invocation
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "RECORDER": str(shim),
            "PREFERRED_CANDIDATES": preferred,
            "FALLBACK_CANDIDATES": fallback,
            "EXCLUDED": excluded,
            "REQUIRED_DEPOSIT_USD": "5",
            "AKASH_PROVIDERS": FOREIGN,
            "AKASH_PROVIDERS_BACKUP": FOREIGN,
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = json.loads(result.stdout)
    monkeypatch.setenv("AKASH_PROVIDERS", recorded["preferred"])
    monkeypatch.setenv("AKASH_PROVIDERS_BACKUP", recorded["backup"])
    monkeypatch.setattr(sys, "argv", ["just-akash", *recorded["argv"]])
    with patch("just_akash.deploy.deploy") as deploy, pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 0
    kwargs = deploy.call_args.kwargs
    assert _resolve_tier(kwargs["preferred_providers"], "AKASH_PROVIDERS") == expected_preferred
    assert _resolve_tier(kwargs["backup_providers"], "AKASH_PROVIDERS_BACKUP") == expected_backup
