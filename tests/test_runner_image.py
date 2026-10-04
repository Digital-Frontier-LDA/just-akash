"""Private registry auth must preserve the qualified payload and tenant resources."""

import os
import subprocess

import pytest
import yaml

from just_akash.runner_image import configure, main, read_pull_credentials

SOURCE = "ghcr.io/digital-frontier-lda/df-akash-runner@sha256:" + "1" * 64
MIRROR = "docker.io/jobordu/private-runner@sha256:" + "1" * 64


def template(tmp_path):
    path = tmp_path / "runner.yaml"
    path.write_text(
        f'version: "2.0"\nservices:\n  runner:\n\n    image: {SOURCE}\n'
        "    env:\n      - RUNNER_TOKEN=@@RUNNER_TOKEN@@\n"
        "profiles:\n  compute:\n    runner:\n      resources:\n"
        "        cpu:\n          units: 4\n"
    )
    return path


def test_private_mirror_retains_payload_identity_env_and_resources(tmp_path):
    path = template(tmp_path)
    before = yaml.safe_load(path.read_text())
    configure(
        path,
        image=MIRROR,
        host="https://index.docker.io/v1/",
        username="jobordu",
        password='reader"token',
    )
    after = yaml.safe_load(path.read_text())
    assert after["profiles"] == before["profiles"]
    assert after["services"]["runner"]["env"] == before["services"]["runner"]["env"]
    assert after["services"]["runner"]["image"] == MIRROR
    assert after["services"]["runner"]["credentials"] == {
        "host": "https://index.docker.io/v1/",
        "username": "jobordu",
        "password": 'reader"token',
    }
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_default_call_does_not_rewrite_existing_template(tmp_path):
    path = template(tmp_path)
    original = path.read_bytes()
    configure(path, image="", host="", username="", password="")
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "values",
    [
        {"image": MIRROR[:-1] + "2"},
        {"image": MIRROR.split("@", 1)[0] + ":latest"},
        {"host": "https://unowned.example"},
        {"host": "http://index.docker.io/v1/"},
        {"password": ""},
        {"host": "", "username": "", "password": ""},
        {"password": "reader\nsecond-line"},
        {"username": "user\nmalformed"},
        {"host": "https://index.docker.io/v1/?token=forbidden"},
    ],
)
def test_invalid_mirror_or_credentials_leave_template_unchanged(tmp_path, values):
    path = template(tmp_path)
    original = path.read_bytes()
    options = {
        "image": MIRROR,
        "host": "https://index.docker.io/v1/",
        "username": "jobordu",
        "password": "reader-fixture",
    }
    with pytest.raises(ValueError):
        configure(path, **(options | values))
    assert path.read_bytes() == original


def test_existing_credentials_are_not_overwritten(tmp_path):
    path = template(tmp_path)
    path.write_text(
        path.read_text().replace(
            "    env:", "    credentials:\n      password: existing\n    env:"
        )
    )
    with pytest.raises(ValueError, match="already contains"):
        configure(
            path,
            image=MIRROR,
            host="https://index.docker.io/v1/",
            username="jobordu",
            password="reader-fixture",
        )


def test_another_tasks_symlink_cannot_receive_credentials(tmp_path):
    target = template(tmp_path)
    link = tmp_path / "linked.yaml"
    link.symlink_to(target)
    original = target.read_bytes()
    with pytest.raises(ValueError, match="task-owned"):
        configure(
            link,
            image=MIRROR,
            host="https://index.docker.io/v1/",
            username="jobordu",
            password="reader-fixture",
        )
    assert target.read_bytes() == original


def decrypt_fixture(tmp_path, monkeypatch, content, *, returncode=0):
    cipher = tmp_path / "registry-pull.sops.env"
    cipher.write_text("encrypted-fixture")
    monkeypatch.setenv("SOPS_AGE_KEY", "age-reader-fixture")
    calls = []

    def decrypt(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode, stdout=content, stderr="secret-error")

    monkeypatch.setattr(subprocess, "run", decrypt)
    return cipher, calls


def test_sops_reader_is_masked_and_decryption_does_not_inherit_control_secrets(
    tmp_path, monkeypatch, capsys
):
    cipher, calls = decrypt_fixture(
        tmp_path, monkeypatch, "DOCKERHUB_PULL_USERNAME=jobordu\nDOCKERHUB_PULL_TOKEN=reader%=\n"
    )
    monkeypatch.setenv("GH_RUNNER_PAT", "control-plane-fixture")
    monkeypatch.setenv("AKASH_API_KEY", "wallet-fixture")
    monkeypatch.setenv("DOCKERHUB_MANAGEMENT_TOKEN", "management-fixture")
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", "/not-the-reader")
    assert read_pull_credentials(cipher, username="jobordu", password="") == (
        "jobordu",
        "reader%=",
    )
    assert capsys.readouterr().out == "::add-mask::reader%25=\n"
    argv, options = calls[0]
    assert argv == [
        "sops",
        "decrypt",
        "--input-type",
        "dotenv",
        "--output-type",
        "dotenv",
        str(cipher),
    ]
    assert options["env"]["SOPS_AGE_KEY"] == "age-reader-fixture"
    assert set(options["env"]) <= {"PATH", "LANG", "LC_ALL", "TMPDIR", "SOPS_AGE_KEY"}
    assert options["timeout"] == 30
    assert options["capture_output"] is True


@pytest.mark.parametrize(
    "content",
    [
        "DOCKERHUB_PULL_USERNAME=someone\nDOCKERHUB_PULL_TOKEN=reader\n",
        "DOCKERHUB_PULL_USERNAME=jobordu\nDOCKERHUB_PULL_TOKEN=\n",
        "DOCKERHUB_PULL_USERNAME=jobordu\n",
        "DOCKERHUB_PULL_USERNAME=jobordu\nDOCKERHUB_PULL_TOKEN=reader\nDOCKERHUB_PULL_TOKEN=other\n",
        "DOCKERHUB_PULL_USERNAME=jobordu\nDOCKERHUB_PULL_TOKEN=reader\nGH_RUNNER_PAT=control\n",
        "DOCKERHUB_MANAGEMENT_USERNAME=jobordu\nDOCKERHUB_MANAGEMENT_TOKEN=admin\n",
        "DOCKERHUB_PULL_USERNAME=jobordu\nDOCKERHUB_PULL_TOKEN=reader\nsecond-line\n",
    ],
)
def test_sops_invalid_identity_or_role_leaves_sdl_unchanged(
    tmp_path, monkeypatch, capsys, content
):
    cipher, _ = decrypt_fixture(tmp_path, monkeypatch, content)
    path = template(tmp_path)
    original = path.read_bytes()
    monkeypatch.setenv("RUNNER_IMAGE", MIRROR)
    monkeypatch.setenv("RUNNER_REGISTRY_HOST", "https://index.docker.io/v1/")
    monkeypatch.setenv("RUNNER_REGISTRY_USERNAME", "jobordu")
    monkeypatch.delenv("RUNNER_REGISTRY_PASSWORD", raising=False)
    monkeypatch.setattr(
        "sys.argv", ["runner_image", "--sdl", str(path), "--sops-env-file", str(cipher)]
    )
    assert main() == 1
    assert path.read_bytes() == original
    assert "::add-mask::" not in capsys.readouterr().out


def test_failed_mac_never_uses_or_prints_partial_decryption(tmp_path, monkeypatch, capsys):
    cipher, _ = decrypt_fixture(
        tmp_path,
        monkeypatch,
        "DOCKERHUB_PULL_USERNAME=jobordu\nDOCKERHUB_PULL_TOKEN=partial-secret",
        returncode=1,
    )
    with pytest.raises(ValueError, match="decryption failed"):
        read_pull_credentials(cipher, username="jobordu", password="")
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "condition", ["direct-password", "missing-age", "symlink", "missing-file"]
)
def test_invalid_sops_configuration_never_invokes_decryption(tmp_path, monkeypatch, condition):
    cipher, calls = decrypt_fixture(tmp_path, monkeypatch, "unused")
    password = ""
    if condition == "direct-password":
        password = "reader"
    elif condition == "missing-age":
        monkeypatch.delenv("SOPS_AGE_KEY")
    elif condition == "symlink":
        link = tmp_path / "linked.env"
        link.symlink_to(cipher)
        cipher = link
    else:
        cipher.unlink()
    with pytest.raises(ValueError):
        read_pull_credentials(cipher, username="jobordu", password=password)
    assert calls == []


def test_sops_cli_writes_only_reader_to_owner_only_sdl(tmp_path, monkeypatch, capsys):
    cipher, _ = decrypt_fixture(
        tmp_path, monkeypatch, "DOCKERHUB_PULL_USERNAME=jobordu\nDOCKERHUB_PULL_TOKEN=reader=\n"
    )
    path = template(tmp_path)
    monkeypatch.setenv("RUNNER_IMAGE", MIRROR)
    monkeypatch.setenv("RUNNER_REGISTRY_HOST", "https://index.docker.io/v1/")
    monkeypatch.setenv("RUNNER_REGISTRY_USERNAME", "jobordu")
    monkeypatch.delenv("RUNNER_REGISTRY_PASSWORD", raising=False)
    monkeypatch.setattr(
        "sys.argv", ["runner_image", "--sdl", str(path), "--sops-env-file", str(cipher)]
    )
    assert main() == 0
    payload = path.read_text()
    assert yaml.safe_load(payload)["services"]["runner"]["credentials"]["password"] == "reader="
    assert "age-reader-fixture" not in payload
    assert path.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().out == "::add-mask::reader=\n"
