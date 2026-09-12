"""Create one GitHub JIT runner identity and one Akash service per pool slot.

The encoded configurations are intentionally transient: they are held only long
enough to write the private SDL submitted to Akash and are never stored in the
recovery journal.  The journal contains the exact GitHub runner identities so a
failed provider attempt can delete precisely what it created before another
attempt receives fresh configurations.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

_SLOT_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_DIGEST_IMAGE_RE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
_POSITIVE_DECIMAL_RE = re.compile(r"[1-9][0-9]{0,31}")
_PLACEMENT_PREFIX_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,47}")


def _gh_executable() -> str:
    executable = shutil.which("gh")
    if executable is None:
        raise RuntimeError("GitHub CLI executable was not found")
    return executable


@dataclass(frozen=True)
class RunnerIdentity:
    slot: str
    runner_id: int
    name: str


def parse_slots(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("runner-slots must be a JSON array") from exc
    if not isinstance(value, list) or not value:
        raise ValueError("runner-slots must be a non-empty JSON array")
    if len(value) > 100:
        raise ValueError("runner-slots may contain at most 100 slots")
    if any(not isinstance(slot, str) or not _SLOT_RE.fullmatch(slot) for slot in value):
        raise ValueError("each runner slot must match [a-z0-9][a-z0-9-]{0,31}")
    if len(value) != len(set(value)):
        raise ValueError("runner-slots must be unique")
    return value


def validate_group_id(raw: str) -> int:
    if not raw.isascii() or not raw.isdigit() or raw.startswith("0"):
        raise ValueError("runner-group-id must be a positive base-10 integer")
    value = int(raw)
    if value <= 0:
        raise ValueError("runner-group-id must be a positive base-10 integer")
    return value


def _positive_decimal(raw: str, field: str) -> str:
    if not isinstance(raw, str) or not raw.isascii() or not _POSITIVE_DECIMAL_RE.fullmatch(raw):
        raise ValueError(f"{field} must be a positive canonical decimal integer")
    return raw


def operation_identity(
    placement_prefix: str,
    runner_label: str,
    *,
    operation: str,
    run_id: str,
    run_attempt: str,
    group: int = 1,
) -> tuple[str, str]:
    """Return the idv2 on-chain identity and operation-scoped GitHub label.

    ``operation`` is broker-issued.  This helper only validates and transports it; it
    never derives an ordinal from ambient workflow state.
    """
    if not _PLACEMENT_PREFIX_RE.fullmatch(placement_prefix):
        raise ValueError("placement-prefix must match [a-z0-9][a-z0-9.-]{0,47}")
    if "-idv" in placement_prefix or "-run-" in placement_prefix:
        raise ValueError("placement-prefix must be an unstamped registered repository prefix")
    if not _LABEL_RE.fullmatch(runner_label) or len(runner_label) > 36:
        raise ValueError("runner-label must be at most 36 plain label characters")
    operation = _positive_decimal(operation, "create-operation")
    run_id = _positive_decimal(run_id, "run-id")
    run_attempt = _positive_decimal(run_attempt, "run-attempt")
    group_text = _positive_decimal(str(group), "group")
    stem = placement_prefix if placement_prefix.endswith(("-", ".")) else placement_prefix + "-"
    deployment_group = (
        f"{stem}idv2-class-ci-runner-g{group_text}-op-{operation}"
        f"-attempt-{run_attempt}-run-{run_id}-end"
    )
    operation_label = (
        f"{runner_label}-idv2-g{group_text}-op-{operation}-attempt-{run_attempt}-run-{run_id}"
    )
    if len(operation_label) > 100:
        raise ValueError("operation-scoped runner label exceeds GitHub's 100-character limit")
    return deployment_group, operation_label


def slot_contract(
    raw_slots: str, runner_label: str
) -> tuple[list[str], dict[str, str], dict[str, list[str]]]:
    slots = parse_slots(raw_slots)
    if not _LABEL_RE.fullmatch(runner_label):
        raise ValueError("runner-label must match [A-Za-z0-9][A-Za-z0-9._-]{0,99}")
    labels = {slot: f"{runner_label}-{slot}" for slot in slots}
    if any(len(label) > 100 for label in labels.values()):
        raise ValueError("runner-label plus slot must be at most 100 characters")
    targets = {slot: ["self-hosted", "linux", "akash", label] for slot, label in labels.items()}
    return slots, labels, targets


def _atomic_private_write(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The directory contains one-use JIT authority. 0o700 grants group/other no access;
    # Semgrep's suggested 0o644 would expose names and make the directory untraversable.
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _atomic_private_text(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The SDL contains live encoded JIT configs; group and other require zero access.
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _read_journal(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("journal_type") != "just-akash/jit-pool-attempt/v1":
        raise RuntimeError("JIT attempt journal has an unknown shape")
    runners = data.get("runners")
    if not isinstance(runners, list):
        raise RuntimeError("JIT attempt journal has no runner population")
    return data


def _gh_generate(org: str, payload: dict[str, object]) -> dict[str, object]:
    completed = subprocess.run(  # noqa: S603 -- fixed argv with structured API input
        [
            _gh_executable(),
            "api",
            "--method",
            "POST",
            f"orgs/{org}/actions/runners/generate-jitconfig",
            "--input",
            "-",
        ],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"GitHub JIT configuration request failed (gh rc={completed.returncode})"
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub JIT configuration response was not JSON") from exc
    if not isinstance(response, dict):
        raise RuntimeError("GitHub JIT configuration response was not an object")
    return response


def _gh_delete(org: str, runner_id: int) -> None:
    completed = subprocess.run(  # noqa: S603 -- fixed argv, validated numeric runner id
        [
            _gh_executable(),
            "api",
            "--method",
            "DELETE",
            f"orgs/{org}/actions/runners/{runner_id}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        if re.search(r"\bHTTP 404\b", f"{completed.stdout}\n{completed.stderr}"):
            return
        raise RuntimeError(
            f"GitHub runner deletion failed for id {runner_id} (gh rc={completed.returncode})"
        )


def _decode_json_documents(raw: str) -> list[object]:
    decoder = json.JSONDecoder()
    documents: list[object] = []
    offset = 0
    while offset < len(raw):
        while offset < len(raw) and raw[offset].isspace():
            offset += 1
        if offset == len(raw):
            break
        value, offset = decoder.raw_decode(raw, offset)
        documents.append(value)
    if not documents:
        raise RuntimeError("GitHub runner-group response was empty")
    return documents


def _gh_read_json(path: str, *, paginate: bool = False) -> list[object]:
    command = [_gh_executable(), "api"]
    if paginate:
        command.append("--paginate")
    command.append(path)
    completed = subprocess.run(  # noqa: S603 -- fixed gh argv, API path is internal
        command, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"GitHub runner-group read failed (gh rc={completed.returncode})")
    try:
        return _decode_json_documents(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub runner-group response was not JSON") from exc


def _complete_paginated_population(
    pages: Sequence[object],
    key: str,
    subject: str,
) -> list[object]:
    expected_total: int | None = None
    entries: list[object] = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get(key), list):
            raise RuntimeError(f"{subject} response was unreadable")
        total = page.get("total_count")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise RuntimeError(f"{subject} response omitted a canonical total_count")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise RuntimeError(f"{subject} response carried inconsistent total_count values")
        entries.extend(page[key])
    if expected_total is None or len(entries) != expected_total:
        raise RuntimeError(f"{subject} response was truncated or duplicated")
    return entries


def verify_group_binding(
    group: object,
    repository_pages: Sequence[object],
    *,
    group_id: int,
    repository_id: int,
    workflow_ref: str,
) -> None:
    """Refuse a group that could route this runner outside one repo/workflow."""

    if not isinstance(group, dict) or group.get("id") != group_id:
        raise RuntimeError("runner-group readback did not match the requested group id")
    if group.get("visibility") != "selected":
        raise RuntimeError("runner group must have selected-repository visibility")
    if group.get("allows_public_repositories") is not False:
        raise RuntimeError("runner group must refuse public repositories")
    if group.get("restricted_to_workflows") is not True:
        raise RuntimeError("runner group must restrict access to selected workflows")
    if group.get("selected_workflows") != [workflow_ref]:
        raise RuntimeError("runner group must be bound only to the calling workflow ref")
    selected_ids: list[int] = []
    repositories = _complete_paginated_population(
        repository_pages,
        "repositories",
        "runner-group repository access",
    )
    for repository in repositories:
        if (
            not isinstance(repository, dict)
            or isinstance(repository.get("id"), bool)
            or not isinstance(repository.get("id"), int)
        ):
            raise RuntimeError("runner-group repository access carried an invalid id")
        selected_ids.append(repository["id"])
    if selected_ids != [repository_id]:
        raise RuntimeError("runner group must be bound only to the calling repository")


def verify_live_group_binding(
    org: str,
    group_id: int,
    repository_id: int,
    workflow_ref: str,
    *,
    read: Callable[..., list[object]] = _gh_read_json,
) -> None:
    group_documents = read(f"orgs/{org}/actions/runner-groups/{group_id}")
    if len(group_documents) != 1:
        raise RuntimeError("runner-group read returned an ambiguous population")
    repository_pages = read(
        f"orgs/{org}/actions/runner-groups/{group_id}/repositories?per_page=100",
        paginate=True,
    )
    verify_group_binding(
        group_documents[0],
        repository_pages,
        group_id=group_id,
        repository_id=repository_id,
        workflow_ref=workflow_ref,
    )


def observe_group_population(journal: object, pages: Sequence[object]) -> dict[str, object]:
    """Return the exact expected runners visible in their verified runner group."""

    if not isinstance(journal, dict):
        raise RuntimeError("JIT attempt journal was unreadable")
    group_id = journal.get("group_id")
    operation_label = journal.get("operation_label")
    runners = journal.get("runners")
    if isinstance(group_id, bool) or not isinstance(group_id, int) or group_id <= 0:
        raise RuntimeError("JIT attempt journal omitted its verified runner group")
    if not isinstance(runners, list) or not runners:
        raise RuntimeError("JIT attempt journal has no expected runner population")
    if not isinstance(operation_label, str) or not _LABEL_RE.fullmatch(operation_label):
        raise RuntimeError("JIT attempt journal omitted its operation label")

    expected: dict[int, dict[str, object]] = {}
    for item in runners:
        if not isinstance(item, dict):
            raise RuntimeError("JIT attempt journal contains an invalid runner identity")
        runner_id = item.get("id")
        name = item.get("name")
        labels = item.get("labels")
        if (
            isinstance(runner_id, bool)
            or not isinstance(runner_id, int)
            or runner_id <= 0
            or not isinstance(name, str)
            or not name
            or not isinstance(labels, list)
            or not labels
            or any(not isinstance(label, str) or not label for label in labels)
        ):
            raise RuntimeError("JIT attempt journal contains an invalid runner identity")
        if runner_id in expected:
            raise RuntimeError("JIT attempt journal contains a duplicate runner id")
        expected[runner_id] = item

    observed: dict[int, dict[str, object]] = {}
    population = _complete_paginated_population(pages, "runners", "runner-group population")
    for runner in population:
        if not isinstance(runner, dict):
            raise RuntimeError("runner-group population carried an invalid runner")
        runner_id = runner.get("id")
        raw_labels = runner.get("labels")
        if not isinstance(raw_labels, list):
            raise RuntimeError("runner-group population omitted runner labels")
        actual_labels: set[str] = set()
        for label in raw_labels:
            if not isinstance(label, dict) or not isinstance(label.get("name"), str):
                raise RuntimeError("runner-group population carried an invalid label")
            actual_labels.add(label["name"])
        if runner_id not in expected:
            if operation_label in actual_labels:
                raise RuntimeError("runner group contains an unjournaled operation runner")
            continue
        if runner_id in observed:
            raise RuntimeError("runner-group population repeated an expected runner id")
        if runner.get("name") != expected[runner_id]["name"]:
            raise RuntimeError("runner-group population changed an expected runner name")
        expected_labels = expected[runner_id]["labels"]
        if not isinstance(expected_labels, list) or not set(expected_labels).issubset(
            actual_labels
        ):
            raise RuntimeError("runner-group population omitted expected runner labels")
        observed[runner_id] = runner

    online = [
        runner_id
        for runner_id in expected
        if observed.get(runner_id, {}).get("status") == "online"
        and observed[runner_id].get("busy") is False
    ]
    return {
        "group_id": group_id,
        "expected": len(expected),
        "online": len(online),
        "online_ids": online,
        "versions": [observed[runner_id].get("version") for runner_id in online],
    }


def observe_live_group_population(
    journal_path: Path,
    *,
    read: Callable[..., list[object]] = _gh_read_json,
) -> dict[str, object]:
    journal = _read_journal(journal_path)
    org = journal.get("org")
    group_id = journal.get("group_id")
    if not isinstance(org, str) or not isinstance(group_id, int):
        raise RuntimeError("JIT attempt journal omitted group observation authority")
    pages = read(
        f"orgs/{org}/actions/runner-groups/{group_id}/runners?per_page=100",
        paginate=True,
    )
    return observe_group_population(journal, pages)


def _runner_name(
    runner_label: str,
    operation_label: str,
    repository: str,
    run_id: str,
    run_attempt: str,
    provider_attempt: int,
    slot: str,
) -> str:
    identity = f"{repository}:{operation_label}:{run_id}:{run_attempt}:{provider_attempt}:{slot}"
    identity_key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    name = f"just-akash-{runner_label}-{identity_key}"
    if len(name) > 64:
        raise ValueError("runner-label is too long to preserve its cleanup prefix in runner names")
    return name


def _response_identity(response: dict[str, object]) -> tuple[int, str]:
    runner = response.get("runner")
    if not isinstance(runner, dict):
        raise RuntimeError("GitHub JIT response omitted runner identity")
    runner_id = runner.get("id")
    name = runner.get("name")
    if isinstance(runner_id, bool) or not isinstance(runner_id, int) or runner_id <= 0:
        raise RuntimeError("GitHub JIT response carried an invalid runner id")
    if not isinstance(name, str) or not name:
        raise RuntimeError("GitHub JIT response carried an invalid runner name")
    return runner_id, name


def render_sdl(
    *,
    image: str,
    placement: str,
    slots: Sequence[str],
    configs: dict[str, str],
    cpu: str,
    memory: str,
    storage: str,
) -> str:
    if not _DIGEST_IMAGE_RE.fullmatch(image):
        raise ValueError("runner-image must be pinned by sha256 digest")
    if set(configs) != set(slots) or len(set(configs.values())) != len(slots):
        raise RuntimeError("every slot requires one distinct JIT configuration")
    services: dict[str, object] = {}
    computes: dict[str, object] = {}
    pricing: dict[str, object] = {}
    deployment: dict[str, object] = {}
    for index, slot in enumerate(slots, start=1):
        service = f"runner-{index:03d}"
        services[service] = {
            "image": image,
            "env": [
                f"RUNNER_JIT_CONFIG={configs[slot]}",
                "EPHEMERAL=true",
                "RUNNER_WORKDIR=/_work",
                "RUN_AS_ROOT=true",
            ],
            "expose": [{"port": 80, "as": 80, "to": [{"global": True}]}],
        }
        computes[service] = {
            "resources": {
                "cpu": {"units": cpu},
                "memory": {"size": memory},
                "storage": {"size": storage},
            }
        }
        pricing[service] = {"denom": "uact", "amount": 100000}
        deployment[service] = {placement: {"profile": service, "count": 1}}
    document = {
        "version": "2.0",
        "services": services,
        "profiles": {
            "compute": computes,
            "placement": {placement: {"pricing": pricing}},
        },
        "deployment": deployment,
    }
    return yaml.safe_dump(document, sort_keys=False)


def cleanup_attempt(
    journal_path: Path,
    *,
    delete: Callable[[str, int], None] = _gh_delete,
) -> None:
    journal = _read_journal(journal_path)
    org = journal.get("org")
    runners = journal.get("runners")
    if not isinstance(org, str) or not isinstance(runners, list):
        raise RuntimeError("JIT attempt journal omitted cleanup authority")
    failures: list[str] = []
    remaining = list(runners)
    for item in list(runners):
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            raise RuntimeError("JIT attempt journal contains an invalid runner identity")
        runner_id = item["id"]
        try:
            delete(org, runner_id)
        except RuntimeError as exc:
            failures.append(str(exc))
            continue
        remaining.remove(item)
        journal["runners"] = remaining
        _atomic_private_write(journal_path, journal)
    if failures:
        raise RuntimeError("; ".join(failures))


def prepare_attempt(
    *,
    org: str,
    group_id: int,
    raw_slots: str,
    runner_label: str,
    operation_label: str,
    repository: str,
    run_id: str,
    run_attempt: str,
    provider_attempt: int,
    image: str,
    placement: str,
    cpu: str,
    memory: str,
    storage: str,
    journal_path: Path,
    sdl_path: Path,
    generate: Callable[[str, dict[str, object]], dict[str, object]] = _gh_generate,
    delete: Callable[[str, int], None] = _gh_delete,
) -> list[RunnerIdentity]:
    slots, labels, _targets = slot_contract(raw_slots, operation_label)
    names = {
        slot: _runner_name(
            runner_label,
            operation_label,
            repository,
            run_id,
            run_attempt,
            provider_attempt,
            slot,
        )
        for slot in slots
    }
    if journal_path.exists() or sdl_path.exists():
        raise RuntimeError("attempt paths already exist; JIT configurations are never reusable")
    journal: dict[str, object] = {
        "journal_type": "just-akash/jit-pool-attempt/v1",
        "org": org,
        "group_id": group_id,
        "runner_label": runner_label,
        "operation_label": operation_label,
        "provider_attempt": provider_attempt,
        "runners": [],
    }
    _atomic_private_write(journal_path, journal)
    identities: list[RunnerIdentity] = []
    configs: dict[str, str] = {}
    try:
        for slot in slots:
            name = names[slot]
            response = generate(
                org,
                {
                    "name": name,
                    "runner_group_id": group_id,
                    "labels": [
                        "self-hosted",
                        "linux",
                        "akash",
                        operation_label,
                        labels[slot],
                    ],
                    "work_folder": "_work",
                },
            )
            runner_id, returned_name = _response_identity(response)
            if runner_id in {identity.runner_id for identity in identities}:
                raise RuntimeError("GitHub returned a duplicate runner id")
            identity = RunnerIdentity(slot, runner_id, returned_name)
            identities.append(identity)
            journal["runners"] = [
                {
                    "slot": item.slot,
                    "id": item.runner_id,
                    "name": item.name,
                    "labels": [
                        "self-hosted",
                        "linux",
                        "akash",
                        operation_label,
                        labels[item.slot],
                    ],
                }
                for item in identities
            ]
            _atomic_private_write(journal_path, journal)
            if returned_name != name:
                raise RuntimeError(
                    "GitHub JIT response runner name did not match the requested name"
                )
            encoded = response.get("encoded_jit_config")
            if not isinstance(encoded, str) or not encoded:
                raise RuntimeError("GitHub JIT response omitted encoded configuration")
            if encoded in configs.values():
                raise RuntimeError("GitHub returned a reused JIT configuration")
            configs[slot] = encoded
        sdl = render_sdl(
            image=image,
            placement=placement,
            slots=slots,
            configs=configs,
            cpu=cpu,
            memory=memory,
            storage=storage,
        )
        _atomic_private_text(sdl_path, sdl)
        return identities
    except Exception:
        cleanup_attempt(journal_path, delete=delete)
        with contextlib.suppress(FileNotFoundError):
            sdl_path.unlink()
        raise


def _append_output(path: str, key: str, value: object) -> None:
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(f"{key}={json.dumps(value, separators=(',', ':'))}\n")


def _append_scalar_output(path: str, key: str, value: str) -> None:
    if not value or "\n" in value or "\r" in value:
        raise ValueError("workflow scalar output must be one nonempty line")
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(f"{key}={value}\n")


def select_receipt_owner(deposit_usd: str) -> str:
    try:
        deposit = float(deposit_usd)
    except ValueError as exc:
        raise ValueError("deposit must be a positive finite USD amount") from exc
    if not math.isfinite(deposit) or deposit <= 0:
        raise ValueError("deposit must be a positive finite USD amount")
    from .wallet_pool import select_client_for_create

    selection = select_client_for_create(math.ceil(deposit * 1_000_000))
    return selection.account or selection.client.account_address()


def receipt_summary(path: Path) -> dict[str, object]:
    from .deployment_receipt import decode_receipt

    receipt = decode_receipt(path.read_bytes())
    summary: dict[str, object] = {
        "state": receipt["state"],
        "expected_owner": receipt["expected_owner"],
        "operation_id": receipt["operation_id"],
    }
    if receipt["state"] == "create_response_received":
        summary["dseq"] = receipt["dseq"]
    return summary


def _main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    topology = sub.add_parser("topology")
    topology.add_argument("--slots", required=True)
    topology.add_argument("--runner-label", required=True)
    topology.add_argument("--group-id", required=True)
    topology.add_argument("--github-output", required=True)
    identity = sub.add_parser("identity")
    identity.add_argument("--placement-prefix", required=True)
    identity.add_argument("--runner-label", required=True)
    identity.add_argument("--create-operation", required=True)
    identity.add_argument("--run-id", required=True)
    identity.add_argument("--run-attempt", required=True)
    identity.add_argument("--github-output", required=True)
    prepare = sub.add_parser("prepare")
    for flag in (
        "org",
        "group-id",
        "slots",
        "runner-label",
        "operation-label",
        "repository",
        "run-id",
        "run-attempt",
        "provider-attempt",
        "image",
        "placement",
        "cpu",
        "memory",
        "storage",
        "journal",
        "sdl",
        "github-output",
    ):
        prepare.add_argument(f"--{flag}", required=True)
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--journal", required=True)
    binding = sub.add_parser("verify-binding")
    binding.add_argument("--org", required=True)
    binding.add_argument("--group-id", required=True)
    binding.add_argument("--repository-id", required=True, type=int)
    binding.add_argument("--workflow-ref", required=True)
    binding.add_argument("--github-output", required=True)
    observe = sub.add_parser("observe")
    observe.add_argument("--journal", required=True)
    owner = sub.add_parser("select-owner")
    owner.add_argument("--deposit-usd", required=True)
    receipt = sub.add_parser("receipt")
    receipt.add_argument("--path", required=True)
    args = parser.parse_args()
    if args.command == "identity":
        deployment_group, operation_label = operation_identity(
            args.placement_prefix,
            args.runner_label,
            operation=args.create_operation,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
        )
        _append_scalar_output(args.github_output, "deployment_group", deployment_group)
        _append_scalar_output(args.github_output, "operation_label", operation_label)
        return 0
    if args.command == "topology":
        validate_group_id(args.group_id)
        slots, labels, targets = slot_contract(args.slots, args.runner_label)
        _append_output(args.github_output, "slot_count", len(slots))
        _append_output(args.github_output, "slot_labels", labels)
        _append_output(args.github_output, "runner_targets", targets)
        return 0
    if args.command == "cleanup":
        cleanup_attempt(Path(args.journal))
        return 0
    if args.command == "verify-binding":
        group_id = validate_group_id(args.group_id)
        verify_live_group_binding(
            args.org,
            group_id,
            args.repository_id,
            args.workflow_ref,
        )
        _append_output(args.github_output, "verified_group_id", group_id)
        return 0
    if args.command == "observe":
        print(json.dumps(observe_live_group_population(Path(args.journal)), separators=(",", ":")))
        return 0
    if args.command == "select-owner":
        print(select_receipt_owner(args.deposit_usd))
        return 0
    if args.command == "receipt":
        print(json.dumps(receipt_summary(Path(args.path)), separators=(",", ":")))
        return 0
    identities = prepare_attempt(
        org=args.org,
        group_id=validate_group_id(args.group_id),
        raw_slots=args.slots,
        runner_label=args.runner_label,
        operation_label=args.operation_label,
        repository=args.repository,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        provider_attempt=int(args.provider_attempt),
        image=args.image,
        placement=args.placement,
        cpu=args.cpu,
        memory=args.memory,
        storage=args.storage,
        journal_path=Path(args.journal),
        sdl_path=Path(args.sdl),
    )
    _append_output(args.github_output, "runner_ids", [item.runner_id for item in identities])
    _append_output(args.github_output, "runner_names", [item.name for item in identities])
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
