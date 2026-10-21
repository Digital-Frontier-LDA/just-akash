"""credentials-sops on the pool: same contract as runner-teardown.yml's.

A Digital-Frontier-LDA caller may not reference secrets.AKASH_API_KEY or
secrets.GH_RUNNER_PAT (df-cicd baseline, secrets-only-sops), so the pool reads them from
the caller's own SOPS file. The decrypt has to precede the PAT preflight, every direct
secret must still win, and the pool's own rollback must read the same file: otherwise a
failed provision is the one lease nothing can close.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def pool():
    return yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())


def test_opt_in_and_only_the_age_key_is_added():
    call = pool()[True]["workflow_call"]
    assert call["inputs"]["credentials-sops"]["type"] == "boolean"
    assert call["inputs"]["credentials-sops"]["default"] is False
    assert call["secrets"]["CREDENTIALS_AGE_KEY"]["required"] is False


def test_decrypt_precedes_the_pat_preflight_and_reads_only_caller_ciphertext():
    steps = pool()["jobs"]["pool"]["steps"]
    names = [s.get("name") for s in steps]
    checkout = names.index("Check out the caller encrypted runner credentials")
    load = names.index("Load the caller runner credentials from SOPS")
    preflight = next(i for i, s in enumerate(steps) if s.get("id") == "pat")
    assert steps[0]["with"]["path"] == ".just-akash"
    assert 0 < checkout < load < preflight
    assert steps[checkout]["if"] == steps[load]["if"] == "inputs.credentials-sops"
    assert steps[checkout]["with"] == {
        "ref": "${{ github.sha }}",
        "path": ".akash-runner-credentials",
        "sparse-checkout": "secrets/akash-runner.sops.env",
        "sparse-checkout-cone-mode": False,
        "persist-credentials": False,
    }
    assert steps[load]["uses"] == "./.just-akash/.github/actions/sops-env"
    assert steps[load]["with"] == {
        "age-key": "${{ secrets.CREDENTIALS_AGE_KEY }}",
        "file": ".akash-runner-credentials/secrets/akash-runner.sops.env",
    }


def test_preflight_and_provision_fall_back_to_the_sops_value_and_a_direct_secret_wins():
    steps = {s.get("id"): s for s in pool()["jobs"]["pool"]["steps"] if s.get("id")}
    assert steps["pat"]["env"]["GH_TOKEN"] == "${{ secrets.GH_RUNNER_PAT || env.GH_RUNNER_PAT }}"
    env = steps["provision"]["env"]
    assert env["AKASH_API_KEY"] == "${{ secrets.AKASH_API_KEY || env.AKASH_API_KEY }}"
    assert env["AKASH_API_KEYS"] == "${{ secrets.AKASH_API_KEYS || env.AKASH_API_KEYS }}"
    assert env["GH_TOKEN"] == "${{ secrets.GH_RUNNER_PAT || env.GH_RUNNER_PAT }}"


def test_the_rollback_teardown_reads_the_same_caller_file():
    rollback = pool()["jobs"]["teardown"]
    assert rollback["with"]["credentials-sops"] == "${{ inputs.credentials-sops }}"
    assert rollback["secrets"]["CREDENTIALS_AGE_KEY"] == "${{ secrets.CREDENTIALS_AGE_KEY }}"
