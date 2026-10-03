"""Private registry auth must preserve the qualified payload and tenant resources."""

import os

import pytest
import yaml

from just_akash.runner_image import configure

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
