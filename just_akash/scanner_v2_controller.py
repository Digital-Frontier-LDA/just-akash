"""Fixed hosted scanner-v2 entry; no arbitrary JSON-to-effect command interface."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from .github_jit import _json
from .scanner_jit_v2 import (
    ENTRYPOINT_SHA256,
    PRIVATE_IMAGE,
    PROFILE,
    NativeJitRequest,
    mint_scanner_jit,
    tenant_environment,
    verify_group,
)
from .scanner_provenance import PROVIDERS, ScannerBinding, validate_ack

REPOSITORY = "Borduas-Holdings/Blazing-Back"
REPOSITORY_ID = 1071436278
ROOT = "/repos/" + REPOSITORY
WORKFLOW = ".github/workflows/secret-scanning.yml"
NATIVE_JOB = (
    "Provision one owned native TruffleHog runner v2 / "
    "Provision scanner-v2 through acknowledged JIT"
)
NATIVE_STEP = "Run the acknowledged scanner-v2 controller"
SDK_REPOSITORY = "Digital-Frontier-LDA/just-akash"
SDK_WORKFLOW = ".github/workflows/runner-pool.yml"
ORGANIZATION_ID = 208689568
RECONCILED_PROFILE = "bb-native-trufflehog-v2-reconciled"


class ControllerHeld(RuntimeError):
    pass


def require(value: object) -> None:
    if not value:
        raise ControllerHeld("Hosted scanner-v2 context or result was not verified")


def number(value: str) -> int:
    require(type(value) is str and re.fullmatch(r"[1-9][0-9]{0,18}", value) is not None)
    result = int(value)
    require(0 < result < 2**63)
    return result


def inputs(environment: Mapping[str, str]) -> dict[str, Any]:
    require(environment.get("RUNNER_PRIVATE_PROFILE") in (PROFILE, RECONCILED_PROFILE))
    exact = {
        "SCANNER_PUBLIC_PROFILE": "",
        "SCANNER_IMAGE": "",
        "SCANNER_REGISTRY_HOST": "https://index.docker.io/v1/",
        "SCANNER_REGISTRY_USERNAME": "jobordu",
        "SCANNER_REGISTRY_SOPS": "true",
        "SCANNER_NATIVE_PULL_READER": "false",
        "SCANNER_NATIVE_REPOSITORY_SCOPE": "false",
        "SCANNER_CREDENTIALS_SOPS": "false",
        "SCANNER_EPHEMERAL": "true",
        "SCANNER_POOL_SIZE": "1",
        "SCANNER_MIN_POOL_SIZE": "1",
        "SCANNER_CPU": "4",
        "SCANNER_MEMORY": "16Gi",
        "SCANNER_STORAGE": "96Gi",
        "SCANNER_SELECT": "cheapest",
        "SCANNER_ORG": "Borduas-Holdings",
        "SCANNER_DIRECT_PASSWORD_PRESENT": "false",
    }
    require(all(environment.get(k) == v for k, v in exact.items()))
    for key in ("SCANNER_PROVIDERS", "SCANNER_OWNED_PROVIDERS"):
        raw = environment.get(key, "")
        require(0 < len(raw) <= 512)
        rows = json.loads(raw)
        require(
            type(rows) is list
            and len(rows) == 3
            and all(type(x) is str for x in rows)
            and set(rows) == PROVIDERS
        )
    require(environment.get("GITHUB_REPOSITORY") == REPOSITORY)
    require(
        environment.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and environment.get("GITHUB_REF") == "refs/heads/main"
    )
    require(
        environment.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and environment.get("GITHUB_JOB") == "scanner_v2"
    )
    source, sdk = environment.get("GITHUB_SHA", ""), environment.get("SCANNER_SDK_SHA", "")
    require(
        re.fullmatch(r"[0-9a-f]{40}", source) is not None
        and re.fullmatch(r"[0-9a-f]{40}", sdk) is not None
    )
    require(environment.get("GITHUB_WORKFLOW_SHA") == source)
    require(
        environment.get("GITHUB_WORKFLOW_REF") == REPOSITORY + "/" + WORKFLOW + "@refs/heads/main"
    )
    run, attempt = (
        number(environment.get("GITHUB_RUN_ID", "")),
        number(environment.get("GITHUB_RUN_ATTEMPT", "")),
    )
    sdk_round = number(environment.get("SCANNER_SDK_ROUND", ""))
    require(1 <= sdk_round <= 3)
    name = f"dfci-bb-th-{run}-a{attempt}-r{sdk_round}"
    return dict(
        repository=REPOSITORY,
        repository_id=REPOSITORY_ID,
        workflow_path=WORKFLOW,
        workflow_sha=source,
        native_head_sha=source,
        sdk_sha=sdk,
        event="workflow_dispatch",
        ref="refs/heads/main",
        run_id=run,
        run_attempt=attempt,
        sdk_round=sdk_round,
        producer_job_id=number(environment.get("SCANNER_PRODUCER_JOB_ID", "")),
        role=PROFILE,
        image=PRIVATE_IMAGE,
        providers=sorted(PROVIDERS),
        resources=dict(cpu="4", memory="16Gi", storage="96Gi", count=1),
        runner_name=name,
        runner_label=name,
        akash_group=f"borduas-th-run-{run}-a{attempt}-r{sdk_round}-end",
        runner_group_id=number(environment.get("SCANNER_RUNNER_GROUP_ID", "")),
    )


def resolve_producer(environment: Mapping[str, str], token: str) -> int:
    """Read-only locator; the executing controller independently admits the job."""
    from .runner_scanner_caller import NativeGET

    require(environment.get("GITHUB_REPOSITORY") == REPOSITORY)
    run = number(environment.get("GITHUB_RUN_ID", ""))
    attempt = number(environment.get("GITHUB_RUN_ATTEMPT", ""))
    get = NativeGET(token)
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    total = None
    for page in range(1, 5):
        document = get(
            ROOT + f"/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100&page={page}"
        )
        count, batch = document.get("total_count"), document.get("jobs")
        require(type(count) is int and 0 <= count <= 400 and (total is None or total == count))
        require(type(batch) is list and len(batch) <= 100)
        total = cast(int, count)
        batch = cast(list[dict[str, Any]], batch)
        for row in batch:
            require(
                type(row) is dict
                and type(row.get("id")) is int
                and row["id"] > 0
                and row["id"] not in seen
            )
            seen.add(cast(int, row["id"]))
            rows.append(row)
        if len(rows) == total:
            break
        require(len(batch) == 100 and len(rows) < total)
    require(len(rows) == total)
    matches = [row for row in rows if row.get("name") == NATIVE_JOB]
    require(len(matches) == 1)
    row = matches[0]
    require(row.get("status") == "in_progress" and row.get("conclusion") is None)
    require(row.get("run_id") == run and row.get("run_attempt") == attempt)
    require(row.get("head_sha") == environment.get("GITHUB_SHA"))
    require(
        row.get("runner_name") == environment.get("RUNNER_NAME")
        and type(row.get("runner_id")) is int
        and row["runner_id"] > 0
    )
    return row["id"]


def admit_native(binding: ScannerBinding, environment: Mapping[str, str], token: str) -> None:
    from .runner_scanner_caller import NativeGET

    get = NativeGET(token)
    values = binding.to_dict()
    run, attempt = values["run_id"], values["run_attempt"]
    repo = get(ROOT)
    require(
        type(repo.get("id")) is int
        and repo["id"] == REPOSITORY_ID
        and repo.get("full_name") == REPOSITORY
        and repo.get("private") is True
    )
    require(
        type(repo.get("owner")) is dict
        and type(repo["owner"].get("id")) is int
        and repo["owner"]["id"] == ORGANIZATION_ID
        and repo["owner"].get("login") == "Borduas-Holdings"
    )

    def context():
        value = get(ROOT + f"/actions/runs/{run}")
        require(
            type(value.get("id")) is int
            and value["id"] == run
            and type(value.get("run_attempt")) is int
            and value["run_attempt"] == attempt
        )
        require(
            value.get("event") == "workflow_dispatch"
            and value.get("path") == WORKFLOW
            and value.get("head_sha") == values["workflow_sha"]
            and value.get("head_branch") == "main"
            and value.get("status") == "in_progress"
            and value.get("conclusion") is None
        )
        require(
            all(
                type(value.get(k)) is dict
                and type(value[k].get("id")) is int
                and value[k]["id"] == REPOSITORY_ID
                for k in ("repository", "head_repository")
            )
        )
        require(
            get(ROOT + "/git/ref/heads/main").get("object", {}).get("sha")
            == values["workflow_sha"]
        )
        refs = value.get("referenced_workflows")
        require(type(refs) is list and 1 <= len(refs) <= 64)
        matches = [
            x
            for x in cast(list[dict[str, Any]], refs)
            if type(x) is dict
            and x.get("path") == SDK_REPOSITORY + "/" + SDK_WORKFLOW + "@" + values["sdk_sha"]
        ]
        require(len(matches) == 1 and matches[0].get("sha") == values["sdk_sha"])

    context()
    jobs, seen, total = [], set(), None
    for page in range(1, 5):
        doc = get(ROOT + f"/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100&page={page}")
        count, batch = doc.get("total_count"), doc.get("jobs")
        require(
            type(count) is int
            and 0 <= count <= 400
            and (total is None or total == count)
            and type(batch) is list
            and len(batch) <= 100
        )
        total = cast(int, count)
        batch = cast(list[dict[str, Any]], batch)
        for row in batch:
            require(
                type(row) is dict
                and type(row.get("id")) is int
                and row["id"] > 0
                and row["id"] not in seen
            )
            seen.add(cast(int, row["id"]))
            jobs.append(row)
        if len(jobs) == total:
            break
        require(len(batch) == 100 and len(jobs) < total)
    require(len(jobs) == total)

    def job(row):
        require(
            type(row.get("id")) is int
            and row["id"] == values["producer_job_id"]
            and row.get("name") == NATIVE_JOB
        )
        require(
            type(row.get("run_id")) is int
            and row["run_id"] == run
            and type(row.get("run_attempt")) is int
            and row["run_attempt"] == attempt
            and row.get("head_sha") == values["workflow_sha"]
        )
        require(
            row.get("status") == "in_progress"
            and row.get("conclusion") is None
            and row.get("runner_name") == environment.get("RUNNER_NAME")
            and type(row.get("runner_id")) is int
            and row["runner_id"] > 0
            and row.get("labels") == ["ubuntu-latest"]
        )
        steps = row.get("steps")
        require(type(steps) is list and len(steps) <= 256)
        active = [x for x in steps if type(x) is dict and x.get("name") == NATIVE_STEP]
        require(
            len(active) == 1
            and active[0].get("status") == "in_progress"
            and active[0].get("conclusion") is None
        )
        return row["id"], row["runner_id"], row["runner_name"]

    matches = [r for r in jobs if r.get("id") == values["producer_job_id"]]
    require(len(matches) == 1)
    first = job(matches[0])
    require(job(get(ROOT + f"/actions/jobs/{values['producer_job_id']}")) == first)
    context()
    require(job(get(ROOT + f"/actions/jobs/{values['producer_job_id']}")) == first)
    verify_group(values["runner_group_id"], NativeJitRequest(lambda: None), token)


def write_sdl(
    path: Path,
    binding: ScannerBinding,
    environment: dict[str, str],
    password: str,
    *,
    reconciled: bool = False,
) -> None:
    import yaml

    values = binding.to_dict()
    require(
        set(environment)
        == {"RUNNER_JIT_CONFIG", "EPHEMERAL", "RUNNER_EXECUTION_USER", "RUNNER_NAME"}
    )
    require(
        environment["EPHEMERAL"] == "true"
        and environment["RUNNER_EXECUTION_USER"] == "root"
        and environment["RUNNER_NAME"] == values["runner_name"]
    )
    require(
        type(password) is str
        and 0 < len(password) <= 4096
        and "\n" not in password
        and "\r" not in password
    )
    group = values["akash_group"]
    document = {
        "version": "2.0",
        "services": {
            "runner": {
                "image": PRIVATE_IMAGE,
                "credentials": {
                    "host": "https://index.docker.io/v1/",
                    "username": "jobordu",
                    "password": password,
                },
                "env": [k + "=" + v for k, v in environment.items()],
            }
        },
        "profiles": {
            "compute": {
                "runner": {
                    "resources": {
                        "cpu": {"units": "4"},
                        "memory": {"size": "16Gi"},
                        "storage": [{"size": "96Gi"}],
                    }
                }
            },
            "placement": {group: {"pricing": {"runner": {"denom": "uact", "amount": 100000}}}},
        },
        "deployment": {"runner": {group: {"profile": "runner", "count": 1}}},
    }
    require(type(reconciled) is bool)
    if reconciled:
        document["services"]["runner"]["expose"] = [
            {"port": 65535, "as": 65535, "proto": "tcp", "to": [{"global": True}]}
        ]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        yaml.safe_dump(document, stream, sort_keys=False)
        stream.flush()
        os.fsync(stream.fileno())


def native_runner_read(runner_id: int, token: str) -> dict[str, Any] | None:
    from .scanner_jit_v2 import _NoRedirect

    require(type(runner_id) is int and 0 < runner_id < 2**63)
    require(
        type(token) is str and 0 < len(token) <= 4096 and all(33 <= ord(c) < 127 for c in token)
    )
    result = None
    not_found = False
    try:
        request = urllib.request.Request(
            f"https://api.github.com/orgs/Borduas-Holdings/actions/runners/{runner_id}",
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="GET",
        )
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(
            request, timeout=10
        ) as response:
            require(response.status == 200)
            raw = response.read(65537)
        require(len(raw) <= 65536)
        parsed = _json(raw)
        require(type(parsed) is dict)
        result = parsed
    except urllib.error.HTTPError as error:
        not_found = error.code == 404
    except Exception:
        result = None
    require(result is not None or not_found)
    return result


def wait_for_returned_runner(
    runner_id,
    binding,
    token,
    admission,
    *,
    read=native_runner_read,
    clock=time.monotonic,
    sleep=time.sleep,
) -> None:
    """No hosted fallback; only the already acknowledged native ID may be ready."""
    deadline = clock() + 240
    for _ in range(120):
        require(clock() < deadline)
        value = read(runner_id, token)
        if value is not None:
            require(type(value.get("id")) is int and value["id"] == runner_id)
            require(
                value.get("name") == binding.runner_name
                and value.get("status") in ("online", "offline")
            )
            require(type(value.get("busy")) is bool)
            labels = value.get("labels")
            require(
                type(labels) is list
                and all(type(x) is dict and type(x.get("name")) is str for x in labels)
            )
            require(
                {"self-hosted", "linux", "akash", binding.runner_label}
                <= {x["name"].lower() for x in cast(list[dict[str, Any]], labels)}
            )
            if value["status"] == "online" and value["busy"] is False:
                admission()
                require(clock() < deadline)
                return
        remaining = deadline - clock()
        require(remaining > 0)
        sleep(min(2, remaining))
    raise ControllerHeld("The acknowledged scanner-v2 native runner did not become ready")


def provision(environment: Mapping[str, str]) -> dict[str, Any]:
    from .runner_image import read_pull_credentials, verify_native_reader_role
    from .scanner_durable_pipe import PipeDurableRecorder
    from .scanner_lease_provenance import deploy_scanner_v2, deploy_scanner_v2_reconcilable

    binding = ScannerBinding(inputs(environment))
    reconciled = environment.get("RUNNER_PRIVATE_PROFILE") == RECONCILED_PROFILE
    if reconciled:
        helper_path = environment.get("SCANNER_SDL_VERSION_HELPER", "")
        helper_sha256 = environment.get("SCANNER_SDL_VERSION_HELPER_SHA256", "")
        require(Path(helper_path).is_absolute() and Path(helper_path).is_file())
        require(re.fullmatch(r"[a-f0-9]{64}", helper_sha256) is not None)
    token = environment.get("GH_TOKEN", "")
    require(bool(token))
    require(bool(environment.get("AKASH_API_KEY") or environment.get("AKASH_API_KEYS")))
    require(bool(environment.get("SOPS_AGE_KEY")))
    source = Path(environment.get("SCANNER_REGISTRY_SOPS_PATH", ""))
    require(source.is_absolute() and source.is_file() and not source.is_symlink())
    parent = Path(environment.get("RUNNER_TEMP", ""))
    require(parent.is_absolute() and parent.is_dir())
    recorder = PipeDurableRecorder()

    def admission():
        admit_native(binding, environment, token)

    admission()
    minted = mint_scanner_jit(binding, token, recorder=recorder, before_post=admission)
    # Registry-only decryption on the hosted node, after native/group and ID ACK.
    with (
        open(os.devnull, "w", encoding="utf-8") as sink,
        contextlib.redirect_stdout(sink),
        contextlib.redirect_stderr(sink),
    ):
        username, password = read_pull_credentials(
            source, username="jobordu", password="", native_reader=False
        )
        verify_native_reader_role(username, password)
    guest = tenant_environment(
        minted, binding, image=PRIVATE_IMAGE, entrypoint_sha256=ENTRYPOINT_SHA256
    )
    # Retain the SDK local returned-candidate receipt even if the later durable ACK
    # fails. The credential-bearing SDL is removed on every normal exception path.
    temporary = Path(tempfile.mkdtemp(prefix="scanner-v2-", dir=parent))
    path = temporary / "private-runner.yaml"
    try:
        if reconciled:
            write_sdl(path, binding, guest, password, reconciled=True)
        else:
            write_sdl(path, binding, guest, password)
        with (
            open(os.devnull, "w", encoding="utf-8") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            deploy_arguments: dict[str, Any] = dict(
                sdl_path=str(path),
                binding=binding,
                returned_runner_id=minted.handoff.runner_id,
                jit_ack_record=minted.public_record,
                jit_ack=minted.ack,
                encoded_jit_config=minted.handoff.encoded_config,
                recorder=recorder,
                receipt_path=str(Path(temporary) / "local-deploy-receipt.json"),
                admission=admission,
            )
            if reconciled:
                result = deploy_scanner_v2_reconcilable(
                    helper_path=helper_path,
                    expected_binary_sha256=helper_sha256,
                    environment=environment,
                    token=token,
                    **deploy_arguments,
                )
            else:
                result = deploy_scanner_v2(**deploy_arguments)
    finally:
        path.unlink(missing_ok=True)
    require(
        type(result) is dict
        and result.get("schema") == "scanner-v2-deploy-result/v1"
        and result.get("returned_runner_id") == minted.handoff.runner_id
        and result.get("cleanup_qualified") is False
    )
    result = cast(dict[str, Any], result)
    for prefix in ("deployment", "lease"):
        validate_ack(result[prefix + "_ack_record"], result[prefix + "_ack"])
    wait_for_returned_runner(minted.handoff.runner_id, binding, token, admission)
    return dict(
        schema="scanner-v2-provision-candidate/v1",
        dseq=result["dseq"],
        owner=result["owner"],
        provider=result["provider"],
        gseq=result["gseq"],
        returned_runner_id=minted.handoff.runner_id,
        runner_targets=["self-hosted", "linux", "akash", binding.runner_label],
        jit_ack_record_sha256=minted.ack["record_sha256"],
        deployment_ack_record_sha256=result["deployment_ack"]["record_sha256"],
        lease_ack_record_sha256=result["lease_ack"]["record_sha256"],
        deployment_outcome="created",
        create_ambiguous=False,
        provision_healthy=True,
        cleanup_qualified=False,
        runtime_qualified=False,
    )


def main() -> None:
    require(len(sys.argv) == 1)
    result = None
    try:
        result = provision(os.environ)
    except Exception:
        result = None
    if result is None:
        raise SystemExit("Scanner-v2 controller held; no successful handoff or cleanup claim")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        public = {
            "dseq": result["dseq"],
            "wallet_address": result["owner"],
            "returned_runner_id": str(result["returned_runner_id"]),
            "runner-targets": json.dumps(result["runner_targets"], separators=(",", ":")),
            "deployment_outcome": "created",
            "create_ambiguous": "false",
            "provision_healthy": "true",
            "cleanup_qualified": "false",
            "deployment_group": "borduas-th-run-"
            + str(number(os.environ["GITHUB_RUN_ID"]))
            + "-a"
            + str(number(os.environ["GITHUB_RUN_ATTEMPT"]))
            + "-r"
            + str(number(os.environ["SCANNER_SDK_ROUND"]))
            + "-end",
            "runners_online": "1/1",
            **{
                key: result[key]
                for key in (
                    "jit_ack_record_sha256",
                    "deployment_ack_record_sha256",
                    "lease_ack_record_sha256",
                )
            },
        }
        for key, value in public.items():
            text = str(value)
            require("\r" not in text and "\n" not in text)
            stream.write(key + "=" + text + "\n")


if __name__ == "__main__":
    main()
