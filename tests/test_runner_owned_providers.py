"""Ownership is independent of cheap/proven/preferred third-party fallback."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

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
