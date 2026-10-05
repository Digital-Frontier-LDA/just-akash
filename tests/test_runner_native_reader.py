"""A native pull reader is a fixed, opt-in, one-job tenant capability."""

import json
import os
import subprocess

import pytest
import yaml

from just_akash.runner_image import (
    NATIVE_READER_IMAGE,
    NATIVE_READER_PROVIDERS,
    configure,
    main,
    validate_native_reader_scope,
)

LABEL = "podman-images-123456-2"
TOKEN = 'fixture: \\"%#=reader'


def scope(monkeypatch):
    values = {
        "RUNNER_NATIVE_PULL_READER": "true",
        "NATIVE_READER_CALLER": "Borduas-Holdings/blazing",
        "NATIVE_READER_ORG": "Borduas-Holdings",
        "NATIVE_READER_SOURCE": "Digital-Frontier-LDA/just-akash",
        "RUNNER_IMAGE": NATIVE_READER_IMAGE,
        "RUNNER_REGISTRY_HOST": "https://index.docker.io/v1/",
        "RUNNER_REGISTRY_USERNAME": "jobordu",
        "NATIVE_READER_POOL_SIZE": "1",
        "NATIVE_READER_MIN_POOL_SIZE": "",
        "NATIVE_READER_EPHEMERAL": "true",
        "NATIVE_READER_PROVIDER_SELECT": "cheapest",
        "NATIVE_READER_TAG_PREFIX": "ci-blazing-podman-images",
        "NATIVE_READER_RUN_ID": "123456",
        "NATIVE_READER_RUN_ATTEMPT": "2",
        "NATIVE_READER_LABEL": LABEL,
        "NATIVE_READER_PLACEMENT": "borduas-runner-run-123456-end",
        "NATIVE_READER_PROVIDERS": json.dumps(
            [
                {"address": address, "preferred": True}
                for address in sorted(NATIVE_READER_PROVIDERS)
            ]
        ),
        "NATIVE_READER_OWNED_PROVIDERS": json.dumps(sorted(NATIVE_READER_PROVIDERS)),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("RUNNER_REGISTRY_PASSWORD", raising=False)
    return values


def template(tmp_path):
    path = tmp_path / "runner.yaml"
    path.write_text(
        'version: "2.0"\nservices:\n  runner:\n'
        "    image: ghcr.io/digital-frontier-lda/df-akash-runner@"
        + NATIVE_READER_IMAGE.split("@", 1)[1]
        + "\n    env:\n"
        "      - RUNNER_TOKEN=@@RUNNER_TOKEN@@\n"
        "      - ORG_NAME=Borduas-Holdings\n"
        "      - RUNNER_SCOPE=org\n"
        f"      - RUNNER_NAME_PREFIX=just-akash-{LABEL}\n"
        f"      - LABELS=self-hosted,linux,akash,{LABEL}\n"
        "      - EPHEMERAL=true\n"
        "      - RUNNER_WORKDIR=/_work\n"
        "      - RUN_AS_ROOT=true\n"
        "    expose:\n      - port: 80\n"
        "profiles:\n  compute:\n    runner:\n      resources:\n        cpu: {units: 4}\n"
        "deployment:\n  runner:\n    owned:\n      count: 1\n"
    )
    return path


def options():
    return {
        "image": NATIVE_READER_IMAGE,
        "host": "https://index.docker.io/v1/",
        "username": "jobordu",
        "password": TOKEN,
    }


def decrypt(tmp_path, monkeypatch, content=None):
    cipher = tmp_path / "registry-pull.sops.env"
    cipher.write_text("encrypted fixture")
    monkeypatch.setenv("SOPS_AGE_KEY", "age-reader-fixture")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=content or f"DOCKERHUB_PULL_USERNAME=jobordu\nDOCKERHUB_PULL_TOKEN={TOKEN}\n",
            stderr="management-fixture-untrusted",
        )

    monkeypatch.setattr(subprocess, "run", run)
    return cipher, calls


def invoke(monkeypatch, path, cipher):
    argv = ["runner_image", "--sdl", str(path)]
    if cipher is not None:
        argv += ["--sops-env-file", str(cipher)]
    monkeypatch.setattr("sys.argv", argv)
    return main()


def test_native_sops_reader_is_quoted_and_only_two_fields_are_added(tmp_path, monkeypatch, capsys):
    scope(monkeypatch)
    cipher, calls = decrypt(tmp_path, monkeypatch)
    path = template(tmp_path)
    before = yaml.safe_load(path.read_text())
    for key, value in {
        "GH_RUNNER_PAT": "registration-admin-fixture",
        "DOCKERHUB_MANAGEMENT_TOKEN": "registry-admin-fixture",
        "DOCKERHUB_WRITER_TOKEN": "registry-writer-fixture",
    }.items():
        monkeypatch.setenv(key, value)
    assert invoke(monkeypatch, path, cipher) == 0
    after = yaml.safe_load(path.read_text())
    assert (
        after["services"]["runner"]["env"]
        == [
            "DOCKERHUB_PULL_USERNAME=jobordu",
            f"DOCKERHUB_PULL_TOKEN={TOKEN}",
        ]
        + before["services"]["runner"]["env"]
    )
    assert after["profiles"] == before["profiles"]
    assert after["deployment"] == before["deployment"]
    assert after["services"]["runner"]["expose"] == before["services"]["runner"]["expose"]
    assert after["services"]["runner"]["credentials"]["password"] == TOKEN
    assert after["services"]["runner"]["image"] == NATIVE_READER_IMAGE
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(list(tmp_path.glob("tmp*"))) == 0
    assert set(calls[0][1]["env"]) <= {"PATH", "LANG", "LC_ALL", "TMPDIR", "SOPS_AGE_KEY"}
    text = path.read_text()
    assert "age-reader-fixture" not in text
    assert "registry-admin-fixture" not in text
    assert "registry-writer-fixture" not in text
    assert "registration-admin-fixture" not in text
    assert capsys.readouterr().out == "::add-mask::" + TOKEN.replace("%", "%25") + "\n"


@pytest.mark.parametrize(
    "key,value",
    [
        ("NATIVE_READER_CALLER", "Borduas-Holdings/Blazing-Back"),
        ("NATIVE_READER_ORG", "another-org"),
        ("NATIVE_READER_SOURCE", "someone/just-akash"),
        ("RUNNER_IMAGE", NATIVE_READER_IMAGE.replace("digitalfrontierunipessoallda", "jobordu")),
        ("RUNNER_IMAGE", NATIVE_READER_IMAGE[:-1] + "0"),
        ("RUNNER_REGISTRY_HOST", "https://docker.io/v1/"),
        ("RUNNER_REGISTRY_USERNAME", "management"),
        ("NATIVE_READER_POOL_SIZE", "2"),
        ("NATIVE_READER_MIN_POOL_SIZE", "2"),
        ("NATIVE_READER_EPHEMERAL", "false"),
        ("NATIVE_READER_PROVIDER_SELECT", "emptiest"),
        ("NATIVE_READER_TAG_PREFIX", "foreign"),
        ("NATIVE_READER_RUN_ID", "0"),
        ("NATIVE_READER_RUN_ID", "１２３４５６"),
        ("NATIVE_READER_RUN_ATTEMPT", "02"),
        ("NATIVE_READER_RUN_ATTEMPT", str(2**64)),
        ("NATIVE_READER_LABEL", "podman-images-123456-1"),
        ("NATIVE_READER_LABEL", "podman-images-foreign"),
        ("NATIVE_READER_PLACEMENT", "dcloud"),
        ("RUNNER_REGISTRY_PASSWORD", "direct-fixture"),
        ("RUNNER_NATIVE_PULL_READER", "unverified"),
    ],
)
def test_foreign_or_unowned_scope_fails_before_decryption(
    tmp_path, monkeypatch, capsys, key, value
):
    scope(monkeypatch)
    cipher, calls = decrypt(tmp_path, monkeypatch)
    path = template(tmp_path)
    original = path.read_bytes()
    monkeypatch.setenv(key, value)
    assert invoke(monkeypatch, path, cipher) == 1
    assert calls == []
    assert path.read_bytes() == original
    output = capsys.readouterr().out
    assert "fixture" not in output
    assert "::add-mask::" not in output


@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "flat",
        "foreign",
        "duplicate",
        "missing",
        "denied",
        "third-party",
        "not-preferred",
        "truthy-marker",
        "duplicate-key",
        "nested-address",
        "unknown-field",
    ],
)
def test_provider_scope_cannot_bypass_owned_placement(monkeypatch, kind):
    values = scope(monkeypatch)
    providers = json.loads(values["NATIVE_READER_PROVIDERS"])
    raw = None
    if kind == "empty":
        providers = []
    elif kind == "flat":
        raw = ",".join(sorted(NATIVE_READER_PROVIDERS))
    elif kind == "foreign":
        providers[0]["address"] = "akash1" + "a" * 38
    elif kind == "duplicate":
        providers[0] = providers[1]
    elif kind == "missing":
        providers.pop()
    elif kind == "denied":
        providers[0]["runner_deny"] = True
    elif kind == "third-party":
        providers[0]["ci_only"] = True
    elif kind == "not-preferred":
        providers[0]["preferred"] = False
    elif kind == "truthy-marker":
        providers[0]["preferred"] = "true"
    elif kind == "duplicate-key":
        raw = values["NATIVE_READER_PROVIDERS"].replace(
            '"preferred": true', '"preferred": false,"preferred": true', 1
        )
    elif kind == "nested-address":
        providers[0]["address"] = {"address": providers[0]["address"]}
    else:
        providers[0]["fallback"] = "foreign"
    monkeypatch.setenv(
        "NATIVE_READER_PROVIDERS", raw if raw is not None else json.dumps(providers)
    )
    with pytest.raises(ValueError, match="provider scope"):
        validate_native_reader_scope(reader_from_sops=True)


def test_native_reader_cannot_use_direct_credentials(tmp_path, monkeypatch, capsys):
    scope(monkeypatch)
    path = template(tmp_path)
    original = path.read_bytes()
    assert invoke(monkeypatch, path, None) == 1
    assert path.read_bytes() == original
    assert "::add-mask::" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "replacement",
    [
        ("EPHEMERAL=true", "EPHEMERAL=false"),
        (f"LABELS=self-hosted,linux,akash,{LABEL}", "LABELS=self-hosted,linux,akash,foreign"),
        ("RUNNER_SCOPE=org", "RUNNER_SCOPE=repo"),
        ("RUN_AS_ROOT=true", "RUN_AS_ROOT=true\n      - DOCKERHUB_PULL_TOKEN=existing"),
        ("RUN_AS_ROOT=true", "RUN_AS_ROOT=true\n      - SOPS_AGE_KEY=existing"),
        ("RUN_AS_ROOT=true", "RUN_AS_ROOT=true\n      - GH_RUNNER_PAT=existing"),
        ("RUN_AS_ROOT=true", "RUN_AS_ROOT=true\n      - RUN_AS_ROOT=true"),
        ("    env:", "    environment:"),
    ],
)
def test_existing_credential_or_foreign_runner_env_is_not_overwritten(
    tmp_path, monkeypatch, replacement
):
    scope(monkeypatch)
    path = template(tmp_path)
    path.write_text(path.read_text().replace(*replacement))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="environment"):
        configure(path, **options(), native_reader=True, reader_from_sops=True)
    assert path.read_bytes() == original


@pytest.mark.parametrize("token", ["", "nul\x00token", "line\nsecret", "nonasciié", "x" * 4097])
def test_native_reader_rejects_control_or_unbounded_tokens(tmp_path, monkeypatch, token):
    scope(monkeypatch)
    path = template(tmp_path)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        configure(
            path, **(options() | {"password": token}), native_reader=True, reader_from_sops=True
        )
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "token",
    ["nul\x00fixture", "del\x7ffixture", "unicodeé", "x" * 4097, "prefix@@RUNNER_TOKEN@@suffix"],
)
def test_malformed_sops_native_token_is_rejected_before_mask_output(
    tmp_path, monkeypatch, capsys, token
):
    scope(monkeypatch)
    cipher, _ = decrypt(
        tmp_path,
        monkeypatch,
        f"DOCKERHUB_PULL_USERNAME=jobordu\nDOCKERHUB_PULL_TOKEN={token}\n",
    )
    path = template(tmp_path)
    original = path.read_bytes()
    assert invoke(monkeypatch, path, cipher) == 1
    assert path.read_bytes() == original
    assert "::add-mask::" not in capsys.readouterr().out


def test_registration_placeholder_in_native_reader_cannot_be_rewritten(tmp_path, monkeypatch):
    scope(monkeypatch)
    path = template(tmp_path)
    original = path.read_bytes()
    token = "prefix@@RUNNER_TOKEN@@suffix"
    # The provisioning shell replaces every occurrence in the complete SDL.
    assert token.replace("@@RUNNER_TOKEN@@", "registration-fixture") != token
    with pytest.raises(ValueError, match="credential format"):
        configure(
            path, **(options() | {"password": token}), native_reader=True, reader_from_sops=True
        )
    assert path.read_bytes() == original


def test_false_mode_retains_existing_private_payload_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("NATIVE_READER_CALLER", "foreign")
    one = template(tmp_path)
    two = tmp_path / "other.yaml"
    two.write_bytes(one.read_bytes())
    configure(one, **options())
    configure(two, **options(), native_reader=False)
    assert one.read_bytes() == two.read_bytes()
    assert "DOCKERHUB_PULL_TOKEN=" not in one.read_text()


def test_empty_sops_role_cannot_fallback_to_management(tmp_path, monkeypatch, capsys):
    scope(monkeypatch)
    cipher, _ = decrypt(tmp_path, monkeypatch, "DOCKERHUB_MANAGEMENT_TOKEN=admin-fixture\n")
    path = template(tmp_path)
    original = path.read_bytes()
    assert invoke(monkeypatch, path, cipher) == 1
    assert path.read_bytes() == original
    assert "admin-fixture" not in capsys.readouterr().out


def test_renderer_does_not_export_reader_to_parent_environment(tmp_path, monkeypatch):
    scope(monkeypatch)
    monkeypatch.delenv("DOCKERHUB_PULL_TOKEN", raising=False)
    monkeypatch.delenv("DOCKERHUB_PULL_USERNAME", raising=False)
    path = template(tmp_path)
    configure(path, **options(), native_reader=True, reader_from_sops=True)
    assert "DOCKERHUB_PULL_TOKEN" not in os.environ
    assert "DOCKERHUB_PULL_USERNAME" not in os.environ


@pytest.mark.parametrize(
    "owned",
    ["", "[]", "null", "{}", "not-json", '["foreign"]', "[{}, {}, {}]"],
)
def test_missing_or_foreign_owned_allowlist_fails_before_decryption(
    tmp_path, monkeypatch, capsys, owned
):
    scope(monkeypatch)
    monkeypatch.setenv("NATIVE_READER_OWNED_PROVIDERS", owned)
    cipher, calls = decrypt(tmp_path, monkeypatch)
    path = template(tmp_path)
    before = path.read_bytes()
    assert invoke(monkeypatch, path, cipher) == 1
    assert calls == []
    assert path.read_bytes() == before
    assert "::add-mask::" not in capsys.readouterr().out


def test_owned_candidates_cannot_replace_one_owned_address_with_duplicate(tmp_path, monkeypatch):
    scope(monkeypatch)
    addresses = sorted(NATIVE_READER_PROVIDERS)
    monkeypatch.setenv("NATIVE_READER_OWNED_PROVIDERS", json.dumps(addresses[:2] + addresses[:1]))
    with pytest.raises(ValueError, match="ownership scope"):
        validate_native_reader_scope(reader_from_sops=True)


def test_native_workflow_uses_only_scoped_hosted_sops_step():
    from pathlib import Path

    doc = yaml.safe_load(
        (Path(__file__).parents[1] / ".github/workflows/runner-pool.yml").read_text()
    )
    call = doc[True]["workflow_call"]
    assert call["inputs"]["runner-native-pull-reader"] == {
        "description": (
            "Opt-in fixed Blazing one-job tenant payload: expose only its hosted SOPS reader "
            "to native private-cache pulls."
        ),
        "required": False,
        "type": "boolean",
        "default": False,
    }
    pool = doc["jobs"]["pool"]
    assert pool["runs-on"] == "ubuntu-latest"
    decrypt_step = next(s for s in pool["steps"] if s.get("name", "").endswith("mirror from SOPS"))
    assert (
        decrypt_step["env"]["RUNNER_NATIVE_PULL_READER"]
        == "${{ inputs.runner-native-pull-reader }}"
    )
    assert decrypt_step["env"]["NATIVE_READER_CALLER"] == "${{ github.repository }}"
    assert decrypt_step["env"]["NATIVE_READER_RUN_ID"] == "${{ github.run_id }}"
    assert decrypt_step["env"]["NATIVE_READER_RUN_ATTEMPT"] == "${{ github.run_attempt }}"
    assert decrypt_step["env"]["NATIVE_READER_OWNED_PROVIDERS"] == "${{ inputs.owned-providers }}"
    assert (
        "SOPS_AGE_KEY" not in next(s for s in pool["steps"] if s.get("id") == "provision")["env"]
    )
    assert not any("DOCKERHUB_PULL_TOKEN" in s.get("env", {}) for s in pool["steps"])
    assert not any("DOCKERHUB_PULL_TOKEN" in key for key in pool["outputs"])
    direct = next(
        s
        for s in pool["steps"]
        if s.get("name") == "Configure the qualified private runner mirror"
    )
    assert direct["env"]["RUNNER_NATIVE_PULL_READER"] == "${{ inputs.runner-native-pull-reader }}"
    assert pool["steps"].index(decrypt_step) < next(
        i for i, s in enumerate(pool["steps"]) if s.get("id") == "provision"
    )
