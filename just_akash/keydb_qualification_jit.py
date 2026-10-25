"""Fixed private Blazing KeyDB qualification JIT adapter; no legacy registration-token path.

The authenticated hosted controller supplies source admission, a bounded transport
and an independently acknowledged recorder. This module is not admission and has
no default transport, credential loader, retry, tenant launch or cleanup actor.
Original github_jit remains unchanged; encoded JIT credentials stay in memory.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any, cast

from .github_jit import JitHandoff, JitHold, JitMintUnknown, _json
from .keydb_qualification_provenance import (
    DurableAck,
    DurableRecorder,
    KeyDBOperation,
    KeyDBQualificationBinding,
    validate_ack,
    validate_binding,
)

REPOSITORY = "Borduas-Holdings/blazing"
REPOSITORY_ID = 1074974924
ORGANIZATION = "Borduas-Holdings"
ROOT = "/orgs/" + ORGANIZATION + "/actions"
ROOT_WORKFLOW = REPOSITORY + (
    "/.github/workflows/keydb-modules-parity.yml"
    # Fixed branch qualification scope, distinct from mainv1.
    "@refs/heads/codex/keydb-parity-owned-d8ab-20261024"
)
PROFILE = "blazing-keydb-parity-qualification-v1"
PRIVATE_IMAGE = (
    "docker.io/digitalfrontierunipessoallda/akash-runner@sha256:"
    "e71bf4be0e3a59a57ff791b0fc3bc3a31f216f66807b6ac41f1a23346321baa4"
)
RUNNER_GROUP_NAME = "blazing-keydb-parity-qualification-v1"
MAX_CONFIG = 32768
ENTRYPOINT_SHA256 = "73f99b97b2ac8ed038a94e8d4290dd55ea28a106e737dd26fba02e9d10686ebd"


def require(value: object) -> None:
    if not value:
        raise JitHold("Private KeyDB qualification JIT source or scope was not verified")


def positive(value: object) -> int:
    require(type(value) is int)
    checked = cast("int", value)
    require(0 < checked < 2**63)
    return checked


def slot(run_id: int, run_attempt: int, sdk_round: int) -> tuple[str, tuple[str, ...]]:
    positive(run_id)
    positive(run_attempt)
    require(type(sdk_round) is int and 1 <= sdk_round <= 3)
    name = f"dfci-bl-keydb-qualification-{run_id}-a{run_attempt}-r{sdk_round}"
    require(len(name) <= 96 and re.fullmatch(r"dfci-[a-z0-9-]+", name) is not None)
    return name, ("self-hosted", "linux", "akash", name)


def canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def verify_group(group_id: int, request: Callable[..., dict[str, Any]], token: str) -> None:
    """One selected private BB workflow group, with bounded complete membership."""
    positive(group_id)
    path = ROOT + f"/runner-groups/{group_id}"

    def policy(value: dict[str, Any]) -> tuple[object, ...]:
        require(type(value) is dict)
        require(positive(value.get("id")) == group_id)
        require(value.get("name") == RUNNER_GROUP_NAME)
        require(value.get("visibility") == "selected")
        require(value.get("allows_public_repositories") is False)
        require(value.get("restricted_to_workflows") is True)
        require(value.get("selected_workflows") == [ROOT_WORKFLOW])
        return (group_id, RUNNER_GROUP_NAME, "selected", False, True, ROOT_WORKFLOW)

    first = policy(request("GET", path, token))
    seen: set[int] = set()
    for page in (1, 2):
        value = request("GET", path + f"/repositories?per_page=100&page={page}", token)
        require(type(value) is dict and type(value.get("total_count")) is int)
        require(value["total_count"] == 1 and type(value.get("repositories")) is list)
        rows = value["repositories"]
        require(len(rows) == (1 if page == 1 else 0))
        for row in rows:
            require(type(row) is dict and positive(row.get("id")) == REPOSITORY_ID)
            require(row.get("full_name") == REPOSITORY and row.get("private") is True)
            require(row["id"] not in seen)
            seen.add(positive(row["id"]))
    require(seen == {REPOSITORY_ID})
    require(policy(request("GET", path, token)) == first)


def handoff_from_response(
    document: dict[str, Any],
    *,
    name: str,
    labels: tuple[str, ...],
    group_id: int,
    revision: str,
) -> JitHandoff:
    """Reuse the original strict JIT encoding contract and require the native ID."""
    try:
        require(type(document) is dict)
        runner, config = document.get("runner"), document.get("encoded_jit_config")
        require(type(runner) is dict)
        runner = cast(dict[str, Any], runner)
        runner_id = positive(runner.get("id"))
        require(runner.get("name") == name)
        require(type(config) is str and 0 < len(config) <= MAX_CONFIG)
        config = cast(str, config)
        raw = base64.b64decode(config, validate=True)
        require(base64.b64encode(raw).decode() == config)
        decoded = _json(raw)
        require(type(decoded) is dict and bool(decoded))
        decoded = cast(dict[str, Any], decoded)
        for filename, content in decoded.items():
            require(
                type(filename) is str
                and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", filename) is not None
            )
            require(filename not in {".", ".."} and type(content) is str and bool(content))
            file_bytes = base64.b64decode(content, validate=True)
            require(bool(file_bytes) and base64.b64encode(file_bytes).decode() == content)
        return JitHandoff(runner_id, name, group_id, revision, labels, config)
    except (JitHold, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise JitMintUnknown(
            "Private KeyDB qualification JIT outcome unknown; reconcile the exact slot"
        ) from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NativeJitRequest:
    """Closed hosted transport: current private group reads and one JIT endpoint.

    The recorder owns cross-process exclusion. The native guard is repeated at
    this final HTTP boundary; no registration-token probe or retry exists here.
    """

    def __init__(self, before_post: Callable[[], None]):
        require(callable(before_post))
        self.before_post = before_post
        self.post_attempted = False
        self.calls = 0

    def __call__(self, method: str, path: str, token: str, body=None) -> dict[str, Any]:
        require(
            type(token) is str
            and 0 < len(token) <= 4096
            and all(33 <= ord(c) < 127 for c in token)
        )
        group_route = re.fullmatch(
            re.escape(ROOT)
            + r"/runner-groups/[1-9][0-9]{0,18}(?:/repositories\?per_page=100&page=[12])?",
            path,
        )
        require(
            (method == "GET" and group_route is not None and body is None)
            or (
                method == "POST"
                and path == ROOT + "/runners/generate-jitconfig"
                and type(body) is dict
            )
        )
        self.calls += 1
        require(self.calls <= 5)
        if method == "POST":
            require(not self.post_attempted)
            self.post_attempted = True
            self.before_post()
        result = None
        try:
            req = urllib.request.Request(
                "https://api.github.com" + path,
                method=method,
                data=canonical(body) if body is not None else None,
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Content-Type": "application/json",
                    "User-Agent": "just-akash-blazing-keydb-qualification-jit-v1",
                },
            )
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(
                req, timeout=20
            ) as response:
                require(response.status == (201 if method == "POST" else 200))
                raw = response.read(MAX_CONFIG * 2 + 1)
            require(len(raw) <= MAX_CONFIG * 2)
            parsed = _json(raw)
            require(type(parsed) is dict)
            result = parsed
        except (OSError, HTTPException, ValueError, TypeError, RecursionError, JitHold):
            pass
        if result is None:
            error = JitMintUnknown if method == "POST" else JitHold
            raise error(
                "Private KeyDB qualification JIT mutation unknown"
                if method == "POST"
                else "Private KeyDB qualification JIT policy unavailable"
            ) from None
        return cast("dict[str, Any]", result)


@dataclass(frozen=True)
class MintedKeyDBJit:
    handoff: JitHandoff
    public_record: dict[str, object]
    ack: DurableAck


def mint_keydb_qualification_jit(
    binding: KeyDBQualificationBinding,
    hosted_token: str,
    *,
    recorder: DurableRecorder | None = None,
    before_post: Callable[[], None],
    request: Callable[..., dict[str, Any]] | None = None,
) -> MintedKeyDBJit:
    """Actual private-group/JIT boundary; native ID is acknowledged before delivery.

    KeyDBQualificationBinding is data, not authentication. Only the fixed hosted controller
    may provide admitted context/guard and the concrete recorder. The default
    missing recorder refuses before any JIT POST; failed ACK never yields config.
    """
    values = validate_binding(binding)
    require(values["role"] == PROFILE and values["image"] == PRIVATE_IMAGE)
    require(
        type(hosted_token) is str
        and 0 < len(hosted_token) <= 4096
        and all(33 <= ord(c) < 127 for c in hosted_token)
    )
    require(callable(before_post))
    name, labels = slot(values["run_id"], values["run_attempt"], values["sdk_round"])
    require(name == values["runner_name"] == values["runner_label"])
    group_id = positive(values["runner_group_id"])
    body = {
        "name": name,
        "runner_group_id": group_id,
        "labels": list(labels),
        "work_folder": "_work",
    }
    details: dict[str, object] = {
        "runner_name": name,
        "runner_group_id": group_id,
        "labels": list(labels),
        "request_body_sha256": hashlib.sha256(canonical(body)).hexdigest(),
    }
    operation = KeyDBOperation(binding, kind="jit", recorder=recorder)
    # Missing/existing/ambiguous storage refuses before even querying a group.
    operation.no_wire(details)
    call = request if request is not None else NativeJitRequest(before_post)
    verify_group(group_id, call, hosted_token)
    operation.before_wire(details)
    before_post()
    try:
        response = call("POST", ROOT + "/runners/generate-jitconfig", hosted_token, body)
        handoff = handoff_from_response(
            response, name=name, labels=labels, group_id=group_id, revision=values["sdk_sha"]
        )
        ack = operation.ack(dict(details, runner_id=handoff.runner_id))
    except Exception:
        raise JitMintUnknown(
            "Private KeyDB qualification JIT outcome unknown; no replacement or tenant delivery"
        ) from None
    return MintedKeyDBJit(handoff, operation.public_record, ack)


def tenant_environment(
    minted: MintedKeyDBJit,
    binding: KeyDBQualificationBinding,
    *,
    image: str,
    entrypoint_sha256: str,
) -> dict[str, str]:
    """Exact E71 env contract, using only acknowledged one-slot credentials.

    Registry credentials remain a hosted SDL credentials node, never env. This
    helper accepts no ambient environment or legacy management credential input.
    """
    values = validate_binding(binding)
    require(image == values["image"] == PRIVATE_IMAGE and entrypoint_sha256 == ENTRYPOINT_SHA256)
    record = minted.public_record
    require(
        record.get("kind") == "jit"
        and record.get("phase") == "ACK"
        and record.get("binding") == values
    )
    validate_ack(record, minted.ack)
    details = cast("dict[str, Any]", record["details"])
    require(positive(details.get("runner_id")) == minted.handoff.runner_id)
    name, labels = slot(values["run_id"], values["run_attempt"], values["sdk_round"])
    require(
        minted.handoff.runner_name == name
        and minted.handoff.labels == labels
        and minted.handoff.group_id == values["runner_group_id"]
        and minted.handoff.policy_revision == values["sdk_sha"]
    )
    require(0 < len(minted.handoff.encoded_config) <= MAX_CONFIG)
    return {
        "RUNNER_JIT_CONFIG": minted.handoff.encoded_config,
        "EPHEMERAL": "true",
        "RUNNER_EXECUTION_USER": "root",
        "RUNNER_NAME": name,
    }
