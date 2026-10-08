"""Hosted, opt-in private BB Sentry admission immediately before each token mint.

The original SOPS configuration step owns decryption. This reader examines only
its already rendered, owner-only SDL; AGE authority never enters provision.
Original Blazing admission and issuer-role policy remain authoritative.
"""

from __future__ import annotations

import json
import os
import re
import stat
import urllib.error
import urllib.request
from http.client import HTTPException
from pathlib import Path

import yaml

from just_akash import runner_image as sdk
from just_akash.workload_identity import Identity, format_identity

REPOSITORY = "Borduas-Holdings/Blazing-Back"
RID = 1071436278
MAX_SDL = 1024 * 1024


class SentryAdmissionError(ValueError):
    """One fixed refusal; credentials and remote bodies must never escape."""


def _require(value: object) -> None:
    if not value:
        raise SentryAdmissionError("Sentry before-mint admission was not verified")


class _UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            _require(isinstance(key, str) and key not in result)
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _document(path: Path) -> dict:
    """Read a bounded regular task-owned file without following a symlink."""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600)
        _require(info.st_uid == os.getuid() and 0 < info.st_size <= MAX_SDL)
        raw = stream.read(MAX_SDL + 1)
    _require(0 < len(raw) <= MAX_SDL)
    text = raw.decode("utf-8")
    _require(not any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text)))
    loader = _UniqueLoader(text)
    try:
        document = loader.get_single_data()
    finally:
        loader.dispose()
    _require(isinstance(document, dict))
    return document


def _scope(document: dict) -> tuple[str, str]:
    """One canonical private one-job payload, owned suppliers and idv1 identity."""
    expected = {
        "RUNNER_SENTRY_MINT_ADMISSION": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_REPOSITORY": REPOSITORY,
        "SENTRY_ADMISSION_SOURCE": "Digital-Frontier-LDA/just-akash",
        "RUNNER_NATIVE_PULL_READER": "false",
        "RUNNER_NATIVE_REPOSITORY_SCOPE": "false",
        "SENTRY_ADMISSION_SOPS": "true",
        "SENTRY_ADMISSION_PRIVATE_PROFILE": "",
        "SENTRY_ADMISSION_PUBLIC_PROFILE": "",
        "ORG": "Borduas-Holdings",
        "POOL_SIZE": "1",
        "MIN_POOL_SIZE": "1",
        "TAG_PREFIX": "ci-blazing-back-sentry",
        "PROVIDER_SELECT": "cheapest",
    }
    _require(all(os.environ.get(key) == value for key, value in expected.items()))
    run, attempt = os.environ.get("RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    _require(all(re.fullmatch(r"[1-9][0-9]{0,18}", value) for value in (run, attempt)))
    label = f"sentry-{run}-{attempt}"
    placement = format_identity(
        Identity("borduas-sentry-", REPOSITORY, "ci-runner", 1, int(run), int(attempt)),
        {"borduas-sentry-": REPOSITORY},
    )
    _require(os.environ.get("RUNNER_LABEL") == label)
    _require(os.environ.get("DEPLOYMENT_GROUP") == placement)
    owned = json.loads(os.environ.get("SENTRY_ADMISSION_OWNED_PROVIDERS", ""))
    _require(isinstance(owned, list) and len(owned) == 3)
    _require(all(isinstance(address, str) for address in owned))
    _require(set(owned) == sdk.NATIVE_READER_PROVIDERS)
    providers = json.loads(
        os.environ.get("SENTRY_ADMISSION_PROVIDERS", ""), object_pairs_hook=sdk._unique_object
    )
    _require(isinstance(providers, list) and len(providers) == 3)
    _require(
        all(
            isinstance(row, dict)
            and set(row) == {"address", "preferred"}
            and row["preferred"] is True
            for row in providers
        )
    )
    addresses = [row["address"] for row in providers]
    _require(all(isinstance(address, str) for address in addresses))
    _require(set(addresses) == sdk.NATIVE_READER_PROVIDERS)
    _require(set(document) == {"version", "services", "profiles", "deployment"})
    _require(document["version"] == "2.0" and set(document["services"]) == {"runner"})
    runner = document["services"]["runner"]
    _require(set(runner) == {"image", "credentials", "env", "expose"})
    _require(runner["image"] == sdk.NATIVE_READER_IMAGE)
    credentials = runner["credentials"]
    _require(
        isinstance(credentials, dict) and set(credentials) == {"host", "username", "password"}
    )
    _require(
        credentials["host"] == "https://index.docker.io/v1/"
        and credentials["username"] == "jobordu"
    )
    _require(isinstance(credentials["password"], str))
    sdk._verify_native_token(credentials["password"])
    _require(
        runner["env"]
        == [
            "RUNNER_TOKEN=@@RUNNER_TOKEN@@",
            "ORG_NAME=Borduas-Holdings",
            "RUNNER_SCOPE=org",
            "RUNNER_NAME_PREFIX=just-akash-" + label,
            "LABELS=self-hosted,linux,akash," + label,
            "EPHEMERAL=true",
            "RUNNER_WORKDIR=/_work",
            "RUN_AS_ROOT=true",
        ]
    )
    _require(runner["expose"] == [{"port": 80, "as": 80, "to": [{"global": True}]}])
    _require(
        document["profiles"]
        == {
            "compute": {
                "runner": {
                    "resources": {
                        "cpu": {"units": 2},
                        "memory": {"size": "6Gi"},
                        "storage": {"size": "40Gi"},
                    }
                }
            },
            "placement": {placement: {"pricing": {"runner": {"denom": "uact", "amount": 100000}}}},
        }
    )
    _require(document["deployment"] == {"runner": {placement: {"profile": "runner", "count": 1}}})
    _require(type(document["deployment"]["runner"][placement]["count"]) is int)
    return credentials["username"], credentials["password"]


def _private_repository() -> None:
    """Fixed private BB identity; GET only, no registration token is minted here."""
    token = os.environ.get("GH_TOKEN", "")
    _require(0 < len(token) <= 4096 and all(33 <= ord(c) < 127 for c in token))
    identity = None
    try:
        request = urllib.request.Request(
            "https://api.github.com/repos/" + REPOSITORY,
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="GET",
        )
        with urllib.request.build_opener(sdk._GroupNoRedirect()).open(
            request, timeout=20
        ) as response:
            _require(response.status == 200)
            raw = response.read(1024 * 1024 + 1)
        _require(len(raw) <= 1024 * 1024)
        identity = json.loads(raw, object_pairs_hook=sdk._unique_object)
    except urllib.error.HTTPError as error:
        error.close()  # Remote error bodies are never read or retained.
    except (OSError, HTTPException, ValueError, TypeError, RecursionError):
        pass
    _require(
        isinstance(identity, dict) and type(identity.get("id")) is int and identity["id"] == RID
    )
    _require(
        identity.get("full_name") == REPOSITORY
        and identity.get("private") is True
        and identity.get("visibility") == "private"
    )


def verify_sentry_mint_admission(path: Path) -> None:
    """Fresh issuer, original Blazing oracle, then exact BB membership each call."""
    username, token = _scope(_document(path))
    sdk.verify_native_reader_role(username, token)
    _private_repository()
    sdk.verify_native_reader_group()
    group_path = sdk._GROUP_ROOT + "/1"
    policy = sdk._group_policy(sdk._native_group_request(group_path), 1)
    repositories = sdk._native_group_repositories(group_path)
    _require(
        repositories.get(RID) == REPOSITORY
        and repositories.get(1074974924) == "Borduas-Holdings/blazing"
    )
    _require(sdk._native_group_repositories(group_path) == repositories)
    _require(sdk._group_policy(sdk._native_group_request(group_path), 1) == policy)
    _private_repository()


def main() -> int:
    try:
        # Existing SDK output only; _document requires no-follow, owner, mode and bounds.
        verify_sentry_mint_admission(Path("/tmp/runner-sdl.yaml"))  # noqa: S108
    except (OSError, ValueError, TypeError, KeyError, RecursionError, yaml.YAMLError):
        failed = True
    else:
        failed = False
    if failed:
        print("Sentry before-mint admission was not verified")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
