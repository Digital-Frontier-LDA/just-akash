"""credentials-sops: a caller whose org forbids direct GitHub-secret credentials.

Digital-Frontier-LDA's baseline (df-cicd check_conformance.py, secrets-only-sops) fails
any `secrets.AKASH_API_KEY` / `secrets.GH_RUNNER_PAT` in a caller workflow, and a
reusable's `secrets:` cannot take a SOPS-decrypted value (a masked job output is
dropped). So the teardown must be able to read them from the caller's own SOPS file.
These tests pin the shape: opt-in, ciphertext only, decrypted by this repo's pinned
action, and a directly passed secret still wins so existing callers are unchanged.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def teardown():
    return yaml.safe_load((ROOT / ".github/workflows/runner-teardown.yml").read_text())


def steps():
    return teardown()["jobs"]["teardown"]["steps"]


def test_opt_in_and_only_the_age_key_is_added():
    call = teardown()[True]["workflow_call"]
    assert call["inputs"]["credentials-sops"]["type"] == "boolean"
    assert call["inputs"]["credentials-sops"]["default"] is False
    assert call["secrets"]["CREDENTIALS_AGE_KEY"]["required"] is False
    for name in ("AKASH_API_KEY", "AKASH_API_KEYS", "GH_RUNNER_PAT"):
        assert call["secrets"][name]["required"] is False


def test_only_ciphertext_from_the_exact_caller_revision_is_checked_out():
    checkout = next(
        s for s in steps() if s.get("name") == "Check out the caller encrypted runner credentials"
    )
    assert checkout["if"] == "inputs.credentials-sops"
    assert checkout["with"] == {
        "ref": "${{ github.sha }}",
        "path": ".akash-runner-credentials",
        "sparse-checkout": "secrets/akash-runner.sops.env",
        "sparse-checkout-cone-mode": False,
        "persist-credentials": False,
    }


def test_decryption_is_this_repos_pinned_action_and_precedes_every_reader():
    all_steps = steps()
    names = [s.get("name") for s in all_steps]
    load = names.index("Load the caller runner credentials from SOPS")
    step = all_steps[load]
    assert step["if"] == "inputs.credentials-sops"
    # ./.just-akash is this repo checked out at just-akash-ref, not the caller's tree.
    assert step["uses"] == "./.just-akash/.github/actions/sops-env"
    assert step["with"] == {
        "age-key": "${{ secrets.CREDENTIALS_AGE_KEY }}",
        "file": ".akash-runner-credentials/secrets/akash-runner.sops.env",
    }
    assert all_steps[0]["with"]["path"] == ".just-akash"
    readers = [
        i
        for i, s in enumerate(all_steps)
        if any(
            "AKASH_API_KEY" in str(v) or "GH_RUNNER_PAT" in str(v)
            for v in (s.get("env") or {}).values()
        )
    ]
    assert readers and min(readers) > load


def test_every_credential_read_falls_back_to_the_sops_value_and_a_direct_secret_wins():
    expected = {
        "AKASH_API_KEY": "${{ secrets.AKASH_API_KEY || env.AKASH_API_KEY }}",
        "AKASH_API_KEYS": "${{ secrets.AKASH_API_KEYS || env.AKASH_API_KEYS }}",
        "GH_TOKEN": "${{ secrets.GH_RUNNER_PAT || env.GH_RUNNER_PAT }}",
    }
    seen = 0
    for s in steps():
        for key, value in (s.get("env") or {}).items():
            if "secrets.AKASH_API_KEY" in str(value) or "secrets.GH_RUNNER_PAT" in str(value):
                assert value == expected[key], (s.get("name"), key, value)
                seen += 1
    # Close the lease (2), de-register (1), private repository cleanup (1).
    assert seen == 4
