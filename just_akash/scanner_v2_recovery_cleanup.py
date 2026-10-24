"""Fixed terminal UNKNOWN-create recovery; known ACK cleanup delegates published747.

No create, lease-bind or registration mint is exposed here. A derived DSEQ is
DATA until renewed independent native/Console/chain and original wallet gates.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import scanner_v2_cleanup as known
from .scanner_cleanup_native import NativeGET, actor, locate_environment, producer
from .scanner_cleanup_transport import RunnerAPI
from .scanner_create_observation import complete_snapshot, unique_new_candidate
from .scanner_provenance import record_sha256, require
from .scanner_recovery_pipe import PipeRecoveryRecorder
from .scanner_recovery_provenance import (
    BUNDLE,
    SOURCE,
    TARGET,
    WITNESS,
    RecoveryOperation,
    identity,
    validate_actor,
    validate_bundle,
    validate_source,
    validate_witness,
)


def observation_client(owner: str):
    """Existing wallet lookup for read-only routing, never close authorization."""
    from .api import CIConsoleAPI
    from .owner_lookup import Deadline, lookup_owner
    from .wallet_pool import configured_api_keys

    keys = configured_api_keys()
    require(bool(keys))
    deadline = Deadline()
    for key in keys:
        client = CIConsoleAPI(key)
        kind, observed = lookup_owner(client, deadline=deadline)
        require(kind == "address")
        if observed == owner:
            return client
    require(False)


def creation_proof(owner: str, dseq: str, groups: list[dict[str, Any]], version: str):
    from . import chain

    return chain.creation_version_population_evidence(owner, dseq, groups, version)


class RecoveryResolver:
    """Fresh complete observations at selection AND every final effect boundary."""

    def __init__(
        self,
        source,
        renew_native,
        *,
        client=observation_client,
        snapshot=complete_snapshot,
        proof=creation_proof,
        now=None,
    ):
        self.source = validate_source(source["actor"], source)
        self.renew_native = renew_native
        self.client = client
        self.snapshot = snapshot
        self.proof = proof
        self.now = now or (lambda: datetime.now(timezone.utc))

    def observe(self, expected_target=None):
        self.renew_native()
        source = self.source
        unknown = source["records"]["deployment"]
        details = unknown["details"]
        owner, version = details["owner"], details["expected_manifest_version"]
        # Existing Console observer independently traverses active+closed twice.
        with (
            open(os.devnull, "w", encoding="utf-8") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            observed = self.snapshot(self.client(owner), owner)
            candidate = unique_new_candidate(
                details["precreate_baseline"], observed, owner, version
            )
            require(candidate is not None)
            proof = self.proof(owner, candidate, details["group_population"], version)
        stable = identity(proof)
        from . import chain

        require(proof["registry_digest"] == chain.OWNER_CORROBORATION_REGISTRY_SHA256)
        require(proof["registry_version"] == chain.OWNER_CORROBORATION_REGISTRY_VERSION)
        require(proof["registry_provenance"] == chain.OWNER_CORROBORATION_REGISTRY_PROVENANCE)
        require(
            proof["registry_provenance_digest"]
            == chain.OWNER_CORROBORATION_REGISTRY_PROVENANCE_SHA256
        )
        require(proof["owner"] == owner and proof["dseq"] == candidate)
        require(proof["groups"] == details["group_population"])
        require(proof["creation_manifest_version"] == proof["current_manifest_version"] == version)
        observed_at, expiry = (
            datetime.fromisoformat(proof[k]) for k in ("observed_at", "expires_at")
        )
        now = self.now()
        require(observed_at.tzinfo is not None and expiry.tzinfo is not None)
        require(observed_at <= now < expiry and (now - observed_at).total_seconds() < 30)
        target = {
            "schema": TARGET,
            "owner": owner,
            "dseq": candidate,
            "returned_runner_id": source["records"]["jit"]["details"]["runner_id"],
            "group_population": details["group_population"],
            "jit_ack_record_sha256": record_sha256(source["records"]["jit"]),
            "deployment_unknown_record_sha256": record_sha256(unknown),
            "expected_manifest_version": version,
            "precreate_baseline_sha256": record_sha256(details["precreate_baseline"]),
            "creation_identity": stable,
        }
        require(expected_target is None or target == expected_target)
        self.renew_native()
        require(self.now() < expiry)
        return validate_witness(
            source,
            {
                "schema": WITNESS,
                "actor": source["actor"],
                "producer": source["producer"],
                "source_bundle_sha256": record_sha256(source),
                "target": target,
                "console_snapshot_sha256": record_sha256(observed),
                "creation_proof": proof,
                "authority": "READONLY_DERIVATION",
            },
        )


class RecoveryEngine(known.CleanupEngine):
    """Original guard/run/closed/signer semantics, with an explicit UNKNOWN intent."""

    def __init__(
        self,
        *,
        actor_value,
        producer_binding,
        target,
        recorder,
        revalidate,
        runner_api,
        derivation_evidence,
        resolver,
        closed=known.chain_closed,
        closer_factory=known.closer_for,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self.actor = validate_actor(actor_value)
        require(self.actor["mode"] == "terminal")
        self.producer = producer_binding
        self.target = target
        self.recorder = recorder
        self.revalidate = revalidate
        self.io = runner_api
        self.authority = lambda _producer, current: resolver.observe(current)
        self.closed = closed
        self.closer_factory = closer_factory
        self.clock = clock
        self.sleep = sleep
        self.attempted = {"close": False, "remove": False}
        self.wire_started = {"close": False, "remove": False}
        self.acks = {"close": False, "remove": False}
        self.unknown = {}
        self.operations = {
            kind: RecoveryOperation(
                actor=self.actor,
                producer=producer_binding,
                target=target,
                kind=kind,
                recorder=recorder,
                derivation_evidence=derivation_evidence,
            )
            for kind in ("close", "remove")
        }
        self.intents = {
            kind: {
                k: target[k] for k in ("owner", "dseq", "group_population", "returned_runner_id")
            }
            | {
                "source_record_sha256": target[
                    "deployment_unknown_record_sha256"
                    if kind == "close"
                    else "jit_ack_record_sha256"
                ]
            }
            for kind in ("close", "remove")
        }


def cleanup(environment):
    locate_environment(environment)
    token = environment.get("GH_TOKEN", "")
    current = actor(environment, NativeGET(token))
    declared = validate_actor(json.loads(environment.get("SCANNER_CLEANUP_ACTOR_JSON", "")))
    require(declared == current)
    recorder = PipeRecoveryRecorder(current)
    bundle = validate_bundle(current, recorder.read_target())
    binding = bundle["producer"]

    def renew():
        get = NativeGET(token)
        require(actor(environment, get) == current)
        producer(current, binding, get, bundle["target"])
        if bundle["schema"] in (SOURCE, BUNDLE):
            # Recovery never selects a stale earlier producer attempt.
            latest = get(f"/repos/Borduas-Holdings/Blazing-Back/actions/runs/{binding['run_id']}")
            require(latest.get("run_attempt") == binding["run_attempt"])

    renew()
    if bundle["schema"] != SOURCE:
        # Real ACK targets use the original engine, operations and five-field intents.
        return known.CleanupEngine(
            actor_value=current,
            producer_binding=binding,
            target=bundle["target"],
            recorder=recorder,
            revalidate=renew,
            runner_api=RunnerAPI(token),
        ).run()
    resolver = RecoveryResolver(bundle, renew)
    witness = resolver.observe()
    bundle = recorder.admit_derivation(witness)
    evidence = bundle["derivation"]
    reference = {
        "artifact_id": evidence["artifact_id"],
        "artifact_digest": evidence["artifact_digest"],
        "record_sha256": evidence["body_sha256"],
    }
    return RecoveryEngine(
        actor_value=current,
        producer_binding=binding,
        target=bundle["target"],
        recorder=recorder,
        revalidate=renew,
        runner_api=RunnerAPI(token),
        derivation_evidence=reference,
        resolver=resolver,
    ).run()


def main():
    require(len(sys.argv) == 1)
    result = None
    try:
        result = cleanup(os.environ)
    except Exception:
        result = None
    if result is None:
        raise SystemExit("Scanner-v2 recovery HOLD; no cleanup qualification or retry authority")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for key, value in result.items():
            text = str(value).lower() if type(value) is bool else str(value)
            require("\n" not in text and "\r" not in text)
            output.write(key + "=" + text + "\n")


if __name__ == "__main__":
    main()
