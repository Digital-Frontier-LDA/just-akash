"""Configure a credential-backed mirror of the qualified tenant runner image."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import stat
import subprocess
import tempfile
import urllib.request
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlparse

from just_akash.runner_repository import (
    NativeReaderRepositoryError,
    verify_native_reader_repository,
)

NATIVE_READER_IMAGE = (
    "docker.io/digitalfrontierunipessoallda/akash-runner@sha256:"
    "aaf3799b5e138abef0831bb8467ded7325164316f0cfde5c183fe6e129eae79e"  # pragma: allowlist secret
)
NATIVE_READER_PROVIDERS = frozenset(
    {
        "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk",
        "akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z",
        "akash1z9nr23cgweu45g2jktfx95v7g2xp8qlsa3ys2x",  # pragma: allowlist secret
    }
)
NATIVE_READER_REPOSITORIES = {
    1074974924: "Borduas-Holdings/blazing",
    1071436278: "Borduas-Holdings/Blazing-Back",
}
_GROUP_ROOT = "/orgs/Borduas-Holdings/actions/runner-groups"


class NativeReaderGroupError(ValueError):
    """Fixed hold stage for unverified hosted GitHub admission."""


class NativeReaderRoleError(ValueError):
    """Fixed hold stage when fresh Docker Hub issuer claims do not prove a reader."""


def verify_native_reader_role(username: str, token: str) -> None:
    """Read-only fresh TLS issuer proof; no underlying writer can enter native env."""
    if username != "jobordu":
        raise NativeReaderRoleError("Native reader Docker Hub role was not verified")
    _verify_native_token(token)
    try:
        request = urllib.request.Request(
            "https://hub.docker.com/v2/auth/token",
            data=json.dumps({"identifier": username, "secret": token}).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.build_opener(_GroupNoRedirect()).open(request, timeout=20) as response:
            if response.status != 200:
                raise ValueError("Unverified Hub authentication response")
            raw = response.read(65536 + 1)
        if len(raw) > 65536:
            raise ValueError("Unverified Hub authentication response")
        document = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(document, dict):
            raise ValueError("Unverified Hub authentication response")
        issued = document.get("access_token")
        if not isinstance(issued, str) or len(issued) > 49154:
            raise ValueError("Unverified Hub issuer token")
        parts = issued.split(".")
        if len(parts) != 3 or any(
            not re.fullmatch(r"[A-Za-z0-9_-]{1,16384}", part) for part in parts
        ):
            raise ValueError("Unverified Hub issuer token")
        decoded = [
            base64.b64decode(part + "=" * (-len(part) % 4), altchars=b"-_", validate=True)
            for part in parts
        ]
        header = json.loads(decoded[0], object_pairs_hook=_unique_object)
        claims = json.loads(decoded[1], object_pairs_hook=_unique_object)
        if (
            not isinstance(header, dict)
            or not isinstance(header.get("alg"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", header["alg"])
            or header["alg"].lower() == "none"
            or not decoded[2]
            or not isinstance(claims, dict)
        ):
            raise ValueError("Unverified Hub issuer token")
        # These are claims from this fresh trusted HTTPS response, not a claim of
        # independently verifying the JWT signature or a pull-scoped registry grant.
        scopes: set[str] = set()
        for key in ("scope", "scopes", "access_token_scope"):
            if key not in claims:
                continue
            value = claims[key]
            if isinstance(value, str) and 0 < len(value) <= 256:
                values = value.split()
            elif isinstance(value, list) and 0 < len(value) <= 8:
                values = value
            else:
                raise ValueError("Unverified Hub credential scope")
            if not values or any(
                not isinstance(item, str) or item not in {"repo:read", "repo:write", "repo:admin"}
                for item in values
            ):
                raise ValueError("Unverified Hub credential scope")
            scopes.update(values)
        if scopes == {"repo:read"}:
            return
    except (OSError, HTTPException, ValueError, TypeError, RecursionError, binascii.Error):
        pass
    # Raised outside any handler: neither remote bodies nor the submitted token survive
    # in a chained exception, formatted traceback or error message.
    raise NativeReaderRoleError("Native reader Docker Hub role was not verified")


class _GroupNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _native_group_request(path: str) -> dict:
    """Fixed-origin hosted reads; neither bearer credentials nor remote prose escape."""
    token = os.environ.get("GH_TOKEN", "")
    valid_path = re.fullmatch(
        re.escape(_GROUP_ROOT) + r"(?:\?per_page=100&page=[12]|/[1-9][0-9]{0,19}"
        r"(?:/repositories\?per_page=100&page=[12])?)",
        path,
    )
    if (
        valid_path is None
        or not token
        or len(token) > 4096
        or any(not 33 <= ord(c) < 127 for c in token)
    ):
        raise NativeReaderGroupError("Native reader GitHub group authority was not verified")
    try:
        request = urllib.request.Request(
            "https://api.github.com" + path,
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "just-akash-native-reader-policy",
            },
            method="GET",
        )
        with urllib.request.build_opener(_GroupNoRedirect()).open(request, timeout=20) as response:
            if response.status != 200:
                raise ValueError("Unverified group response")
            raw = response.read(1024 * 1024 + 1)
        if len(raw) <= 1024 * 1024:
            document = json.loads(raw, object_pairs_hook=_unique_object)
            if isinstance(document, dict):
                return document
    except (OSError, HTTPException, ValueError, TypeError, RecursionError):
        pass
    # Raise outside the handler: HTTP bodies, URLs and tokens must not survive in a cause/context.
    raise NativeReaderGroupError("Native reader GitHub group authority was not verified")


def _group_identity(value: object) -> bool:
    return type(value) is int and 0 < value <= 2**64 - 1


def _group_policy(group: dict, group_id: int) -> tuple:
    if (
        group.get("id") != group_id
        or not _group_identity(group.get("id"))
        or group.get("default") is not True
        or group.get("visibility") != "selected"
        or group.get("allows_public_repositories") is not False
        or group.get("restricted_to_workflows") is not False
        or group.get("selected_workflows") != []
    ):
        raise NativeReaderGroupError("Native reader Default runner group policy was not verified")
    return tuple(
        group[key]
        for key in (
            "id",
            "default",
            "visibility",
            "allows_public_repositories",
            "restricted_to_workflows",
        )
    )


def _native_group_repositories(path: str) -> dict[int, str]:
    repositories: dict[int, str] = {}
    count = None
    for page in (1, 2):
        document = _native_group_request(f"{path}/repositories?per_page=100&page={page}")
        total, rows = document.get("total_count"), document.get("repositories")
        if (
            type(total) is not int
            or total not in (1, 2)
            or count is not None
            and count != total
            or not isinstance(rows, list)
            or len(rows) > 2
        ):
            raise NativeReaderGroupError(
                "Native reader selected repository population was not verified"
            )
        count = total
        for repo in rows:
            if (
                not isinstance(repo, dict)
                or not _group_identity(repo.get("id"))
                or repo["id"] in repositories
                or repo.get("full_name") != NATIVE_READER_REPOSITORIES.get(repo["id"])
                or repo.get("private") is not True
            ):
                raise NativeReaderGroupError("Native reader repository access was not verified")
            repositories[repo["id"]] = repo["full_name"]
        if (page == 1 and len(repositories) != count) or (page == 2 and rows):
            raise NativeReaderGroupError(
                "Native reader selected repository population was not verified"
            )
    return repositories


def verify_native_reader_group() -> None:
    """Observe the actual legacy registration target; caller flags cannot grant access."""
    groups: dict[int, dict] = {}
    count = None
    for page in (1, 2):
        document = _native_group_request(f"{_GROUP_ROOT}?per_page=100&page={page}")
        total, rows = document.get("total_count"), document.get("runner_groups")
        if (
            type(total) is not int
            or not 1 <= total <= 100
            or count is not None
            and count != total
            or not isinstance(rows, list)
            or len(rows) > 100
        ):
            raise NativeReaderGroupError("Native reader runner group population was not verified")
        count = total
        for group in rows:
            if (
                not isinstance(group, dict)
                or not _group_identity(group.get("id"))
                or type(group.get("default")) is not bool
                or group["id"] in groups
            ):
                raise NativeReaderGroupError(
                    "Native reader runner group population was not verified"
                )
            groups[group["id"]] = group
        if (page == 1 and len(groups) != count) or (page == 2 and rows):
            raise NativeReaderGroupError("Native reader runner group population was not verified")
    defaults = [group for group in groups.values() if group["default"]]
    if len(defaults) != 1:
        raise NativeReaderGroupError("Native reader Default runner group was not verified")
    group_id = defaults[0]["id"]
    path = f"{_GROUP_ROOT}/{group_id}"
    first = _group_policy(_native_group_request(path), group_id)
    if _group_policy(defaults[0], group_id) != first:
        raise NativeReaderGroupError(
            "Native reader runner group policy changed during observation"
        )
    repositories = _native_group_repositories(path)
    if _native_group_repositories(path) != repositories:
        raise NativeReaderGroupError("Native reader repository policy changed during observation")
    if repositories.get(1074974924) != "Borduas-Holdings/blazing":
        raise NativeReaderGroupError("Native reader caller repository access was not verified")
    if _group_policy(_native_group_request(path), group_id) != first:
        raise NativeReaderGroupError(
            "Native reader runner group policy changed during observation"
        )


def native_repository_scope(*, native_reader: bool) -> bool:
    value = os.environ.get("RUNNER_NATIVE_REPOSITORY_SCOPE", "false")
    if value not in ("true", "false", "") or value == "true" and not native_reader:
        raise NativeReaderRepositoryError("Native reader repository scope was not verified")
    return value == "true"


def verify_native_reader_admission() -> None:
    if native_repository_scope(native_reader=True):
        verify_native_reader_repository()
    else:
        verify_native_reader_group()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Native reader provider scope was not verified")
        result[key] = value
    return result


def validate_native_reader_scope(*, reader_from_sops: bool) -> str:
    """Fail closed before decryption or lease creation; inputs are hosted context only."""
    expected = {
        "NATIVE_READER_CALLER": "Borduas-Holdings/blazing",
        "NATIVE_READER_ORG": "Borduas-Holdings",
        "NATIVE_READER_SOURCE": "Digital-Frontier-LDA/just-akash",
        "RUNNER_IMAGE": NATIVE_READER_IMAGE,
        "RUNNER_REGISTRY_HOST": "https://index.docker.io/v1/",
        "RUNNER_REGISTRY_USERNAME": "jobordu",
        "NATIVE_READER_POOL_SIZE": "1",
        "NATIVE_READER_EPHEMERAL": "true",
        "NATIVE_READER_PROVIDER_SELECT": "cheapest",
        "NATIVE_READER_TAG_PREFIX": "ci-blazing-podman-images",
    }
    if not reader_from_sops or any(os.environ.get(k) != v for k, v in expected.items()):
        raise ValueError("Native reader tenant scope was not verified")
    if os.environ.get("RUNNER_REGISTRY_PASSWORD"):
        raise ValueError("Native reader requires only the hosted SOPS reader bundle")
    identities = [
        os.environ.get(k, "") for k in ("NATIVE_READER_RUN_ID", "NATIVE_READER_RUN_ATTEMPT")
    ]
    if any(
        re.fullmatch(r"[1-9][0-9]{0,19}", value) is None or int(value) > 2**64 - 1
        for value in identities
    ):
        raise ValueError("Native reader run identity was not verified")
    run_id, attempt = identities
    label = f"podman-images-{run_id}-{attempt}"
    if (
        os.environ.get("NATIVE_READER_LABEL") != label
        or os.environ.get("NATIVE_READER_PLACEMENT") != f"borduas-runner-run-{run_id}-end"
        or os.environ.get("NATIVE_READER_MIN_POOL_SIZE", "") not in ("", "1")
    ):
        raise ValueError("Native reader run ownership was not verified")
    raw = os.environ.get("NATIVE_READER_PROVIDERS", "")
    if len(raw) > 8192:
        raise ValueError("Native reader provider scope was not verified")
    try:
        providers = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as exc:
        raise ValueError("Native reader provider scope was not verified") from exc
    allowed_keys = {
        "address",
        "preferred",
        "runner_host",
        "runner_deny",
        "ci_only",
        "name",
        "failover_priority",
    }
    if not isinstance(providers, list) or len(providers) != 3:
        raise ValueError("Native reader provider scope was not verified")
    addresses = []
    for provider in providers:
        if (
            not isinstance(provider, dict)
            or set(provider) - allowed_keys
            or not isinstance(provider.get("address"), str)
            or provider.get("address") not in NATIVE_READER_PROVIDERS
            or provider.get("preferred") is not True
            or provider.get("runner_deny", False) is not False
            or provider.get("ci_only", False) is not False
            or type(provider.get("runner_host", False)) is not bool
            or ("name" in provider and not isinstance(provider["name"], str))
            or (
                "failover_priority" in provider
                and (
                    type(provider["failover_priority"]) is not int
                    or provider["failover_priority"] < 0
                )
            )
        ):
            raise ValueError("Native reader provider scope was not verified")
        addresses.append(provider["address"])
    if set(addresses) != NATIVE_READER_PROVIDERS:
        raise ValueError("Native reader provider scope was not verified")
    owned_raw = os.environ.get("NATIVE_READER_OWNED_PROVIDERS", "")
    if len(owned_raw) > 8192:
        raise ValueError("Native reader ownership scope was not verified")
    try:
        owned = json.loads(owned_raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError("Native reader ownership scope was not verified") from exc
    if (
        not isinstance(owned, list)
        or len(owned) != 3
        or any(not isinstance(address, str) for address in owned)
        or set(owned) != NATIVE_READER_PROVIDERS
    ):
        raise ValueError("Native reader ownership scope was not verified")
    return label


def _verify_native_token(password: str) -> None:
    if (
        not password
        or len(password) > 4096
        or any(not 32 <= ord(c) < 127 for c in password)
        or "@@RUNNER_TOKEN@@" in password
    ):
        raise ValueError("Native reader credential format was not verified")


def _native_reader_env(text: str, indent: str, *, label: str, password: str) -> str:
    """Add only fixed reader scalars to the generated, run-bound runner environment."""
    _verify_native_token(password)
    header = re.compile(r"^" + re.escape(indent) + r"env:[ \t]*$", re.MULTILINE)
    matches = list(header.finditer(text))
    if len(matches) != 1:
        raise ValueError("Native reader runner environment was not verified")
    match = matches[0]
    existing: dict[str, str] = {}
    for line in text[match.end() :].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(indent + "  "):
            break
        scalar = line.strip()
        if not scalar.startswith("- "):
            raise ValueError("Native reader runner environment was not verified")
        scalar = scalar[2:]
        if scalar.startswith('"'):
            try:
                scalar = json.loads(scalar)
            except ValueError as exc:
                raise ValueError("Native reader runner environment was not verified") from exc
        if not isinstance(scalar, str):
            raise ValueError("Native reader runner environment was not verified")
        key, separator, value = scalar.partition("=")
        if not separator or key in existing:
            raise ValueError("Native reader runner environment was not verified")
        existing[key] = value
    if existing != {
        "RUNNER_TOKEN": "@@RUNNER_TOKEN@@",
        "ORG_NAME": "Borduas-Holdings",
        "RUNNER_SCOPE": "org",
        "RUNNER_NAME_PREFIX": f"just-akash-{label}",
        "LABELS": f"self-hosted,linux,akash,{label}",
        "EPHEMERAL": "true",
        "RUNNER_WORKDIR": "/_work",
        "RUN_AS_ROOT": "true",
    }:
        raise ValueError("Native reader runner environment was not verified")
    lines = "".join(
        f"\n{indent}  - {json.dumps(key + '=' + value)}"
        for key, value in (
            ("DOCKERHUB_PULL_USERNAME", "jobordu"),
            ("DOCKERHUB_PULL_TOKEN", password),
        )
    )
    updated = text[: match.end()] + lines + text[match.end() :]
    if native_repository_scope(native_reader=True):
        updated, count = re.subn(
            r"^" + re.escape(indent) + r"  - RUNNER_SCOPE=org[ \t]*$",
            indent + '  - "RUNNER_SCOPE=repo"\n' + indent + '  - "REPO_NAME=blazing"',
            updated,
            flags=re.MULTILINE,
        )
        if count != 1:
            raise NativeReaderRepositoryError(
                "Native reader repository environment was not verified"
            )
    return updated


def read_pull_credentials(
    path: Path, *, username: str, password: str, native_reader: bool = False
) -> tuple[str, str]:
    """Decrypt only the caller's pull bundle, without exporting credentials to the job."""
    if password or not username or not os.environ.get("SOPS_AGE_KEY"):
        raise ValueError("SOPS mode requires an age key, explicit username and no direct password")
    if path.is_symlink() or not path.is_file():
        raise ValueError("Pull bundle must be a regular file")
    child_env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SOPS_AGE_KEY")
        if key in os.environ
    }
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, bounded trusted SOPS binary
            ["sops", "decrypt", "--input-type", "dotenv", "--output-type", "dotenv", str(path)],  # noqa: S607
            env=child_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Pull bundle decryption failed") from exc
    if result.returncode or len(result.stdout) > 16384:
        raise ValueError("Pull bundle decryption failed")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if (
            not separator
            or key not in {"DOCKERHUB_PULL_USERNAME", "DOCKERHUB_PULL_TOKEN"}
            or key in values
            or not value
            or "\r" in value
        ):
            raise ValueError("Pull bundle contains invalid credentials")
        values[key] = value
    if set(values) != {"DOCKERHUB_PULL_USERNAME", "DOCKERHUB_PULL_TOKEN"}:
        raise ValueError("Pull bundle must contain exactly the reader credentials")
    if values["DOCKERHUB_PULL_USERNAME"] != username:
        raise ValueError("Pull identity does not match the configured registry username")
    token = values["DOCKERHUB_PULL_TOKEN"]
    if native_reader:
        _verify_native_token(token)
        verify_native_reader_role(username, token)
    # Actions command escaping prevents '%' in a token becoming a command escape.
    print("::add-mask::" + token.replace("%", "%25"))
    return username, token


PUBLIC_BB_CE1_IMAGE = (
    "ghcr.io/digital-frontier-lda/df-akash-runner:2.337.0@sha256:"
    "ce1b123c98e273479e08e6315fc81f7017957c23dbf88878076e75572b7b18cc"  # pragma: allowlist secret
)


def validate_public_profile(profile: str) -> str:
    """Admit only the three fixed BB roles, without any private credential transport."""
    if profile != "bb-ce1" or os.environ.get("RUNNER_PRIVATE_PROFILE", ""):
        raise ValueError("Public BB profile was not verified")
    values = os.environ
    if any(
        values.get(key, "")
        for key in (
            "RUNNER_IMAGE",
            "RUNNER_REGISTRY_HOST",
            "RUNNER_REGISTRY_USERNAME",
            "RUNNER_REGISTRY_PASSWORD",
            "SOPS_AGE_KEY",
            "RUNNER_REGISTRY_AGE_KEY",
            "DOCKERHUB_PULL_USERNAME",
            "DOCKERHUB_PULL_TOKEN",
        )
    ) or any(
        values.get(key, "false") != "false"
        for key in (
            "RUNNER_NATIVE_PULL_READER",
            "RUNNER_NATIVE_REPOSITORY_SCOPE",
            "PUBLIC_PROFILE_SOPS",
            "PUBLIC_PROFILE_CREDENTIALS_PRESENT",
        )
    ):
        raise ValueError("Public BB profile was not verified")
    if (
        values.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or values.get("GITHUB_REPOSITORY") != "Borduas-Holdings/Blazing-Back"
        or values.get("PUBLIC_PROFILE_SOURCE") != "Digital-Frontier-LDA/just-akash"
        or values.get("PUBLIC_PROFILE_ORG") != "Borduas-Holdings"
        or values.get("PUBLIC_PROFILE_POOL_SIZE") != "1"
        or values.get("PUBLIC_PROFILE_MIN_POOL_SIZE", "") not in ("", "1")
    ):
        raise ValueError("Public BB profile was not verified")
    run, attempt = values.get("GITHUB_RUN_ID", ""), values.get("GITHUB_RUN_ATTEMPT", "")
    if any(
        re.fullmatch(r"[1-9][0-9]{0,19}", value) is None or int(value) > 2**64 - 1
        for value in (run, attempt)
    ):
        raise ValueError("Public BB profile was not verified")

    def unique_pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Public BB profile was not verified")
            result[key] = value
        return result

    ownership = values.get("PUBLIC_PROFILE_OWNED_PROVIDERS", "")
    if not 0 < len(ownership) <= 512:
        raise ValueError("Public BB profile was not verified")
    try:
        owners = json.loads(ownership, object_pairs_hook=unique_pairs)
    except ValueError:
        raise ValueError("Public BB profile was not verified") from None
    if (
        not isinstance(owners, list)
        or len(owners) != 3
        or any(not isinstance(value, str) for value in owners)
        or set(owners) != NATIVE_READER_PROVIDERS
    ):
        raise ValueError("Public BB profile was not verified")
    label = values.get("PUBLIC_PROFILE_LABEL", "")
    identity = (
        label,
        values.get("PUBLIC_PROFILE_PLACEMENT"),
        values.get("PUBLIC_PROFILE_TAG_PREFIX"),
        values.get("PUBLIC_PROFILE_EPHEMERAL"),
    )
    if identity not in (
        (
            f"fast-pool-{run}",
            f"dfci-infra-runner-run-{run}-end",
            "ci-blazing-back-fast-pool",
            "false",
        ),
        (
            f"sentry-{run}-{attempt}",
            f"borduas-sentry-run-{run}-end",
            "ci-blazing-back-sentry",
            "true",
        ),
        (
            f"apps-{run}-{attempt}",
            f"borduas-apps-run-{run}-end",
            "ci-blazing-back-apps",
            "true",
        ),
    ):
        raise ValueError("Public BB profile was not verified")
    if label == f"apps-{run}-{attempt}" and values.get("PUBLIC_PROFILE_MIN_POOL_SIZE") != "1":
        raise ValueError("Public BB profile was not verified")
    return label


PRIVATE_BB_CE1_IMAGE = (
    "docker.io/digitalfrontierunipessoallda/df-akash-runner@sha256:"
    "ce1b123c98e273479e08e6315fc81f7017957c23dbf88878076e75572b7b18cc"  # pragma: allowlist secret
)


PRIVATE_BLAZING_AAF_IMAGE = (
    "docker.io/digitalfrontierunipessoallda/df-akash-runner@"
    + NATIVE_READER_IMAGE.rsplit("@", 1)[1]
)


def validate_private_blazing_profile(profile: str) -> str:
    """Preserve the fixed Blazing fast/E2E pool contracts with a hosted reader."""
    values = os.environ
    if (
        profile != "blazing-aaf"
        or values.get("RUNNER_PUBLIC_PROFILE", "") != ""
        or values.get("RUNNER_IMAGE", "") != ""
        or values.get("RUNNER_REGISTRY_HOST") != "https://index.docker.io/v1/"
        or values.get("RUNNER_REGISTRY_USERNAME") != "jobordu"
        or values.get("RUNNER_REGISTRY_PASSWORD", "") != ""
        or values.get("PRIVATE_PROFILE_SOPS") != "true"
        or values.get("PRIVATE_PROFILE_AGE_PRESENT") != "true"
        or values.get("PRIVATE_PROFILE_PASSWORD_PRESENT") != "false"
        or any(
            values.get(key, "false") != "false"
            for key in ("RUNNER_NATIVE_PULL_READER", "RUNNER_NATIVE_REPOSITORY_SCOPE")
        )
        or values.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or values.get("GITHUB_REPOSITORY") != "Borduas-Holdings/blazing"
        or values.get("PRIVATE_PROFILE_SOURCE") != "Digital-Frontier-LDA/just-akash"
        or values.get("PRIVATE_PROFILE_ORG") != "Borduas-Holdings"
        or values.get("PRIVATE_PROFILE_EPHEMERAL") != "false"
    ):
        raise ValueError("Private Blazing profile was not verified")
    run, attempt = values.get("GITHUB_RUN_ID", ""), values.get("GITHUB_RUN_ATTEMPT", "")
    if any(
        re.fullmatch(r"[1-9][0-9]{0,19}", value) is None or int(value) > 2**64 - 1
        for value in (run, attempt)
    ):
        raise ValueError("Private Blazing profile was not verified")
    ownership = values.get("PRIVATE_PROFILE_OWNED_PROVIDERS", "")
    if not 0 < len(ownership) <= 512:
        raise ValueError("Private Blazing profile was not verified")
    try:
        owners = json.loads(ownership)
    except ValueError:
        raise ValueError("Private Blazing profile was not verified") from None
    if (
        not isinstance(owners, list)
        or len(owners) != 3
        or any(not isinstance(value, str) for value in owners)
        or set(owners) != NATIVE_READER_PROVIDERS
    ):
        raise ValueError("Private Blazing profile was not verified")
    label = values.get("PRIVATE_PROFILE_LABEL", "")
    identity = (
        label,
        values.get("PRIVATE_PROFILE_PLACEMENT"),
        values.get("PRIVATE_PROFILE_TAG_PREFIX"),
        values.get("PRIVATE_PROFILE_POOL_SIZE"),
    )
    if identity not in (
        (f"fast-pool-{run}-{attempt}", f"borduas-runner-run-{run}-end", "ci-blazing-fast", "4"),
        (f"e2epool-{run}-{attempt}", f"borduas-runner-run-{run}-end", "ci-blazing-e2e", "2"),
    ) or values.get("PRIVATE_PROFILE_MIN_POOL_SIZE", "") not in (
        "",
        values.get("PRIVATE_PROFILE_POOL_SIZE"),
    ):
        raise ValueError("Private Blazing profile was not verified")
    return label


def validate_private_profile(profile: str) -> str:
    """Admit fixed BB roles with hosted SOPS transport and standard SDL credentials."""
    if profile == "blazing-aaf":
        return validate_private_blazing_profile(profile)
    values = os.environ
    if (
        profile != "bb-ce1"
        or values.get("RUNNER_PUBLIC_PROFILE", "") != ""
        or values.get("RUNNER_IMAGE", "") != ""
        or values.get("RUNNER_REGISTRY_HOST") != "https://index.docker.io/v1/"
        or values.get("RUNNER_REGISTRY_USERNAME") != "jobordu"
        or values.get("RUNNER_REGISTRY_PASSWORD", "") != ""
        or values.get("PRIVATE_PROFILE_SOPS") != "true"
        or values.get("PRIVATE_PROFILE_AGE_PRESENT") != "true"
        or values.get("PRIVATE_PROFILE_PASSWORD_PRESENT") != "false"
        or any(
            values.get(key, "false") != "false"
            for key in ("RUNNER_NATIVE_PULL_READER", "RUNNER_NATIVE_REPOSITORY_SCOPE")
        )
    ):
        raise ValueError("Private BB profile was not verified")
    if (
        values.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or values.get("GITHUB_REPOSITORY") != "Borduas-Holdings/Blazing-Back"
        or values.get("PRIVATE_PROFILE_SOURCE") != "Digital-Frontier-LDA/just-akash"
        or values.get("PRIVATE_PROFILE_ORG") != "Borduas-Holdings"
    ):
        raise ValueError("Private BB profile was not verified")
    run, attempt = values.get("GITHUB_RUN_ID", ""), values.get("GITHUB_RUN_ATTEMPT", "")
    if any(
        re.fullmatch(r"[1-9][0-9]{0,19}", value) is None or int(value) > 2**64 - 1
        for value in (run, attempt)
    ):
        raise ValueError("Private BB profile was not verified")

    def unique_pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Private BB profile was not verified")
            result[key] = value
        return result

    ownership = values.get("PRIVATE_PROFILE_OWNED_PROVIDERS", "")
    if not 0 < len(ownership) <= 512:
        raise ValueError("Private BB profile was not verified")
    try:
        owners = json.loads(ownership, object_pairs_hook=unique_pairs)
    except ValueError:
        raise ValueError("Private BB profile was not verified") from None
    if (
        not isinstance(owners, list)
        or len(owners) != 3
        or any(not isinstance(value, str) for value in owners)
        or set(owners) != NATIVE_READER_PROVIDERS
    ):
        raise ValueError("Private BB profile was not verified")
    label = values.get("PRIVATE_PROFILE_LABEL", "")
    identity = (
        label,
        values.get("PRIVATE_PROFILE_PLACEMENT"),
        values.get("PRIVATE_PROFILE_TAG_PREFIX"),
        values.get("PRIVATE_PROFILE_EPHEMERAL"),
    )
    pool_size = values.get("PRIVATE_PROFILE_POOL_SIZE")
    minimum = values.get("PRIVATE_PROFILE_MIN_POOL_SIZE", "")
    if identity == (
        f"b-tier-{run}-{attempt}",
        f"dfci-infra-b-tier-run-{run}-end",
        "ci-blazing-back-b-tier",
        "true",
    ):
        if pool_size not in ("1", "2", "3") or minimum != pool_size:
            raise ValueError("Private BB profile was not verified")
        return label
    if pool_size != "1" or minimum not in ("", "1"):
        raise ValueError("Private BB profile was not verified")
    if identity not in (
        (
            f"fast-pool-{run}",
            f"dfci-infra-runner-run-{run}-end",
            "ci-blazing-back-fast-pool",
            "false",
        ),
        (
            f"sentry-{run}-{attempt}",
            f"borduas-sentry-run-{run}-end",
            "ci-blazing-back-sentry",
            "true",
        ),
        (
            f"apps-{run}-{attempt}",
            f"borduas-apps-run-{run}-end",
            "ci-blazing-back-apps",
            "true",
        ),
    ):
        raise ValueError("Private BB profile was not verified")
    if label == f"apps-{run}-{attempt}" and values.get("PRIVATE_PROFILE_MIN_POOL_SIZE") != "1":
        raise ValueError("Private BB profile was not verified")
    return label


def _public_profile_payload(
    path: Path,
    label: str,
    *,
    selected_image: str = PUBLIC_BB_CE1_IMAGE,
    ephemeral: str | None = None,
) -> str:
    """Select the qualified image while retaining the SDK's reaped runner-name prefix."""
    details = path.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_nlink != 1
        or not 0 < details.st_size <= 65536
    ):
        raise ValueError("Public BB template was not verified")
    text = path.read_text()
    # Generated mappings have plain keys and no aliases. Refuse credential nodes,
    # quoted mapping keys or aliases rather than interpreting an expanded template.
    content = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    if re.search(
        r"(?im)^\s*[\"']?credentials[\"']?\s*:|^\s*[\"'][^\n]*?:|(?:^|\s)[&*][A-Za-z_]", content
    ):
        raise ValueError("Public BB template was not verified")
    matches = list(re.finditer(r"^([ \t]*)image:[ \t]+(\S+)[ \t]*$", text, re.MULTILINE))
    if (
        len(matches) != 1
        or matches[0].group(2)
        != "ghcr.io/digital-frontier-lda/df-akash-runner@" + NATIVE_READER_IMAGE.rsplit("@", 1)[1]
    ):
        raise ValueError("Public BB template was not verified")
    image = matches[0]
    indent = image.group(1)
    headers = list(re.finditer(r"^" + re.escape(indent) + r"env:[ \t]*$", text, re.MULTILINE))
    if len(headers) != 1:
        raise ValueError("Public BB template was not verified")
    existing = {}
    for line in text[headers[0].end() :].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(indent + "  "):
            break
        scalar = line.strip()
        if not scalar.startswith("- "):
            raise ValueError("Public BB template was not verified")
        key, separator, value = scalar[2:].partition("=")
        if not separator or key in existing:
            raise ValueError("Public BB template was not verified")
        existing[key] = value
    if existing != {
        "RUNNER_TOKEN": "@@RUNNER_TOKEN@@",
        "ORG_NAME": "Borduas-Holdings",
        "RUNNER_SCOPE": "org",
        "RUNNER_NAME_PREFIX": f"just-akash-{label}",
        "LABELS": f"self-hosted,linux,akash,{label}",
        "EPHEMERAL": (os.environ["PUBLIC_PROFILE_EPHEMERAL"] if ephemeral is None else ephemeral),
        "RUNNER_WORKDIR": "/_work",
        "RUN_AS_ROOT": "true",
    }:
        raise ValueError("Public BB template was not verified")
    return text[: image.start()] + indent + "image: " + selected_image + text[image.end() :]


def configure(
    path: Path,
    *,
    image: str,
    host: str,
    username: str,
    password: str,
    native_reader: bool = False,
    reader_from_sops: bool = False,
    public_profile: str = "",
    private_profile: str = "",
) -> None:
    if public_profile and private_profile:
        raise ValueError("BB profiles cannot be combined")
    if private_profile != "":
        if native_reader or not reader_from_sops or image:
            raise ValueError("Private BB profile was not verified")
        label = validate_private_profile(private_profile)
        if username != "jobordu" or host != "https://index.docker.io/v1/":
            raise ValueError("Private BB mirror identity was not verified")
        verify_native_reader_role(username, password)
        updated = _public_profile_payload(
            path,
            label,
            selected_image=(
                PRIVATE_BLAZING_AAF_IMAGE
                if private_profile == "blazing-aaf"
                else PRIVATE_BB_CE1_IMAGE
            ),
            ephemeral=os.environ["PRIVATE_PROFILE_EPHEMERAL"],
        )
        matches = list(re.finditer(r"^([ \t]*)image:[ \t]+(\S+)[ \t]*$", updated, re.MULTILINE))
        if len(matches) != 1:
            raise ValueError("Private BB template was not verified")
        match = matches[0]
        credentials = "\n" + match.group(1) + "credentials:"
        for key, value in (("host", host), ("username", username), ("password", password)):
            credentials += "\n" + match.group(1) + "  " + key + ": " + json.dumps(value)
        updated = updated[: match.end()] + credentials + updated[match.end() :]
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(updated)
                stream.flush()
                os.chmod(temporary, 0o600)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return
    if public_profile != "":
        if native_reader or reader_from_sops or any((image, host, username, password)):
            raise ValueError("Public BB profile was not verified")
        label = validate_public_profile(public_profile)
        updated = _public_profile_payload(path, label)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(updated)
                stream.flush()
                os.chmod(temporary, 0o600)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return
    label = (
        validate_native_reader_scope(reader_from_sops=reader_from_sops) if native_reader else ""
    )
    native_repository_scope(native_reader=native_reader)
    if native_reader:
        verify_native_reader_admission()
    if native_reader and (
        image != NATIVE_READER_IMAGE
        or username != "jobordu"
        or host != "https://index.docker.io/v1/"
    ):
        raise ValueError("Native reader mirror identity was not verified")
    if native_reader:
        verify_native_reader_role(username, password)
    if not any((image, host, username, password)):
        return
    if path.is_symlink():
        raise ValueError("Runner template must be a regular task-owned file")
    text = path.read_text()
    matches = list(re.finditer(r"^([ \t]*)image:[ \t]+(\S+)[ \t]*$", text, re.MULTILINE))
    if len(matches) != 1:
        raise ValueError("Runner template must contain exactly one image")
    match = matches[0]
    original = match.group(2)
    chosen = image or original
    if image and not all((host, username, password)):
        raise ValueError("A private runner mirror requires complete registry credentials")
    if not re.fullmatch(
        r"[a-z0-9][a-z0-9.:-]*/(?:[a-z0-9][a-z0-9._-]*/)+[a-z0-9][a-z0-9._-]*@sha256:[0-9a-f]{64}",
        chosen,
    ):
        raise ValueError("Runner mirror requires a complete registry reference and digest")
    if chosen.rsplit("@", 1)[-1] != original.rsplit("@", 1)[-1]:
        raise ValueError("Runner mirror must preserve the qualified image digest")
    credential_lines = ""
    if any((host, username, password)):
        if not all((host, username, password)):
            raise ValueError(
                "Private registry credentials must include host, username and password"
            )
        parsed = urlparse(host)
        registry = chosen.split("/", 1)[0]
        allowed_hosts = {registry}
        if registry == "docker.io":
            allowed_hosts.add("index.docker.io")
        if (
            parsed.scheme != "https"
            or parsed.netloc not in allowed_hosts
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Registry credential host must match the runner mirror over HTTPS")
        if any("\n" in value or "\r" in value for value in (host, username, password)):
            raise ValueError(
                "Registry credentials must use single-line token or base64-key values"
            )
        indent = match.group(1)
        credential_lines = f"\n{indent}credentials:"
        for key, value in (("host", host), ("username", username), ("password", password)):
            credential_lines += f"\n{indent}  {key}: {json.dumps(value)}"
    if re.search(r"^\s*credentials:", text, re.MULTILINE):
        raise ValueError("Runner template already contains registry credentials")
    replacement = match.group(1) + "image: " + chosen + credential_lines
    updated = text[: match.start()] + replacement + text[match.end() :]
    if native_reader:
        updated = _native_reader_env(updated, match.group(1), label=label, password=password)
    # The template begins holding registry credentials here. Replace it atomically
    # with an owner-only file; never print its contents or credentials.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(updated)
            stream.flush()
            os.chmod(temporary, 0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdl", type=Path, required=True)
    parser.add_argument("--sops-env-file", type=Path)
    parser.add_argument("--check-public-profile", action="store_true")
    parser.add_argument("--check-private-profile", action="store_true")
    args = parser.parse_args()
    try:
        public_profile = os.environ.get("RUNNER_PUBLIC_PROFILE", "")
        private_profile = os.environ.get("RUNNER_PRIVATE_PROFILE", "")
        if public_profile and private_profile:
            raise ValueError("BB profiles cannot be combined")
        if private_profile:
            validate_private_profile(private_profile)
            if not args.check_private_profile and args.sops_env_file is None:
                raise ValueError("Private BB profile requires hosted SOPS")
        if args.check_private_profile:
            if not private_profile or args.check_public_profile:
                raise ValueError("Private BB profile was not verified")
            return 0
        if public_profile != "":
            if args.sops_env_file:
                raise ValueError("Public BB profile was not verified")
            validate_public_profile(public_profile)
        if args.check_public_profile:
            if public_profile == "":
                raise ValueError("Public BB profile was not verified")
            return 0
        native_mode = os.environ.get("RUNNER_NATIVE_PULL_READER", "false")
        if native_mode not in ("true", "false", ""):
            raise ValueError("Native reader option was not verified")
        native_reader = native_mode == "true"
        native_repository_scope(native_reader=native_reader)
        if native_reader:
            validate_native_reader_scope(reader_from_sops=args.sops_env_file is not None)
            verify_native_reader_admission()
        username = os.environ.get("RUNNER_REGISTRY_USERNAME", "")
        password = os.environ.get("RUNNER_REGISTRY_PASSWORD", "")
        if args.sops_env_file:
            username, password = read_pull_credentials(
                args.sops_env_file,
                username=username,
                password=password,
                native_reader=native_reader,
            )
        configure(
            args.sdl,
            image=os.environ.get("RUNNER_IMAGE", ""),
            host=os.environ.get("RUNNER_REGISTRY_HOST", ""),
            username=username,
            password=password,
            native_reader=native_reader,
            reader_from_sops=args.sops_env_file is not None,
            public_profile=public_profile,
            private_profile=private_profile,
        )
    except NativeReaderRoleError:
        print("Runner image configuration held: NATIVE_READER_ROLE_UNQUALIFIED")
        return 1
    except NativeReaderRepositoryError:
        print("Runner image configuration held: NATIVE_READER_REPOSITORY_UNQUALIFIED")
        return 1
    except NativeReaderGroupError:
        print("Runner image configuration held: NATIVE_READER_GROUP_UNQUALIFIED")
        return 1
    except (ValueError, OSError):
        print(
            "Runner image configuration failed; "
            "mirror identity or registry credentials were not verified"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
