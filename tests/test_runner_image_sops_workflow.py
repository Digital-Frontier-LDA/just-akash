"""The caller's pull bundle must be decrypted only by pinned provisioning code."""

from pathlib import Path

import yaml


def workflow():
    return yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/runner-pool.yml").read_text()
    )


def test_sops_is_opt_in_and_only_the_reader_age_key_is_declared():
    doc = workflow()
    call = doc[True]["workflow_call"]
    assert call["inputs"]["runner-registry-sops"]["default"] is False
    assert call["secrets"]["RUNNER_REGISTRY_AGE_KEY"]["required"] is False
    steps = doc["jobs"]["pool"]["steps"]
    decrypt = next(s for s in steps if s.get("name", "").endswith("mirror from SOPS"))
    assert decrypt["if"] == "inputs.runner-registry-sops"
    assert decrypt["env"]["SOPS_AGE_KEY"] == "${{ secrets.RUNNER_REGISTRY_AGE_KEY }}"
    assert (
        "--sops-env-file ../.runner-registry-source/secrets/registry-pull.sops.env"
        in decrypt["run"]
    )
    assert "GITHUB_ENV" not in decrypt["run"]
    assert "GITHUB_OUTPUT" not in decrypt["run"]
    assert [s["name"] for s in steps if "SOPS_AGE_KEY" in s.get("env", {})] == [decrypt["name"]]
    direct = next(
        s for s in steps if s.get("name") == "Configure the qualified private runner mirror"
    )
    assert direct["if"] == "${{ !inputs.runner-registry-sops }}"


def test_only_ciphertext_is_checked_out_from_the_exact_caller_revision():
    steps = workflow()["jobs"]["pool"]["steps"]
    checkout = next(
        s for s in steps if s.get("name") == "Check out the caller encrypted reader bundle"
    )
    assert checkout["with"] == {
        "ref": "${{ github.sha }}",
        "path": ".runner-registry-source",
        "sparse-checkout": "secrets/registry-pull.sops.env",
        "sparse-checkout-cone-mode": False,
        "persist-credentials": False,
    }
    install = next(
        s for s in steps if s.get("name", "").startswith("Install checksum-pinned SOPS")
    )
    assert "620a9d7e3352ababeca6908cea24a6e8b14ce89a448ddbd3f94f1ef3398f470a" in install["run"]
    assert install["run"].index("sha256sum --check") < install["run"].index("install -m 0755")


def test_all_credential_bearing_templates_are_private_and_always_removed():
    steps = workflow()["jobs"]["pool"]["steps"]
    provision = next(s for s in steps if s.get("id") == "provision")
    assert provision["run"].index("umask 077") < provision["run"].index(
        "> /tmp/runner-sdl.minted.yaml"
    )
    cleanup = steps[-1]
    assert cleanup["if"] == "always()"
    assert cleanup["run"] == "rm -f -- /tmp/runner-sdl.yaml /tmp/runner-sdl.minted.yaml"
