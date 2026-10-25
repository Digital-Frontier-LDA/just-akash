"""Fixed authenticated known-ID normal/terminal cleanup entry; no arbitrary delete CLI."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from .keydb_qualification_cleanup_native import NativeGET, actor, obj, producer, require
from .keydb_qualification_cleanup_provenance import (
    CleanupOperation,
    validate_actor,
    validate_bundle,
)
from .keydb_qualification_cleanup_transport import RunnerAPI
from .keydb_qualification_provenance import record_sha256


def population_authority(
    producer_binding: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any]:
    from . import chain

    value = chain.owner_close_population_evidence(
        target["owner"], target["dseq"], target["group_population"]
    )
    require(type(value) is dict)
    value = obj(value)
    require(
        value.get("owner") == target["owner"]
        and value.get("dseq") == target["dseq"]
        and value.get("groups") == target["group_population"]
        and value.get("population_count") == 1
    )
    require(
        value.get("registry_digest") == chain.OWNER_CORROBORATION_REGISTRY_SHA256
        and value.get("source_ids") == ["allnodes", "polkachu"]
    )
    expiry = datetime.fromisoformat(value["expires_at"])
    observed = datetime.fromisoformat(value["observed_at"])
    now = datetime.now(timezone.utc)
    require(
        expiry.tzinfo is not None
        and observed.tzinfo is not None
        and observed <= now < expiry
        and (now - observed).total_seconds() < 30
    )
    return value


def chain_closed(producer_binding: dict[str, Any], target: dict[str, Any]) -> bool:
    from . import chain
    from ._lease_verification import verdict

    proof = population_authority(producer_binding, target)
    height = proof.get("height")
    require(type(height) is int and height > 0)
    bases = tuple(s["url"] for s in chain.OWNER_CORROBORATION_SOURCES_V2)

    def get(url):
        parsed = urllib.parse.urlsplit(url)
        base = parsed.scheme + "://" + parsed.netloc
        require(
            base in bases and not parsed.fragment and not parsed.username and not parsed.password
        )
        route = parsed.path + ("?" + parsed.query if parsed.query else "")
        require(
            route.startswith("/akash/market/v1beta5/leases/list?")
            or route.startswith("/akash/deployment/v1beta4/deployments/info?")
        )
        return chain._lcd_get(route, base=base, height=height)

    result = verdict(target["dseq"], target["owner"], bases, get, retries=1)
    require(datetime.now(timezone.utc) < datetime.fromisoformat(proof["expires_at"]))
    return result.get("closed") is True and result.get("sources") == list(bases)


def closer_for(target: dict[str, Any], before_delete):
    from .api import CI_CONSOLE_ORIGIN, CONSOLE_HTTP_TIMEOUT, CIConsoleAPI
    from .keydb_qualification_cleanup_native import NoRedirect
    from .wallet_pool import authorize_client_for_bound_owner

    class GuardedClient(CIConsoleAPI):
        def _open_request(self, request):
            url = urllib.parse.urlsplit(request.full_url)
            require(
                url.scheme + "://" + url.netloc == CI_CONSOLE_ORIGIN
                and not url.username
                and not url.password
                and not url.fragment
            )
            if request.method == "DELETE":
                require(url.path == "/v1/deployments/" + target["dseq"] and not url.query)
                before_delete()
            elif request.method == "POST":
                # Existing owner lookup's host-only short-lived JWT observation.
                require(url.path == "/v1/create-jwt-token" and not url.query)
                require(type(request.data) is bytes)
                require(
                    json.loads(cast(bytes, request.data))
                    == {"data": {"ttl": 300, "leases": {"access": "scoped", "scope": ["shell"]}}}
                )
            else:
                require(False)
            return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(
                request, timeout=CONSOLE_HTTP_TIMEOUT
            )

    # Preserve the original actual signer/owner/expiry-gated wallet selection.
    with (
        open(os.devnull, "w", encoding="utf-8") as sink,
        contextlib.redirect_stdout(sink),
        contextlib.redirect_stderr(sink),
    ):
        closer, _ = authorize_client_for_bound_owner(
            target["dseq"],
            target["owner"],
            target["group_population"][0]["name"],
            client_factory=GuardedClient,
        )
    return closer


class CleanupEngine:
    def __init__(
        self,
        *,
        actor_value,
        producer_binding,
        target,
        recorder,
        revalidate,
        runner_api,
        authority=population_authority,
        closed=chain_closed,
        closer_factory=closer_for,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self.actor = validate_actor(actor_value)
        self.producer = producer_binding
        self.target = target
        self.recorder = recorder
        self.revalidate = revalidate
        self.io = runner_api
        self.authority = authority
        self.closed = closed
        self.closer_factory = closer_factory
        self.clock = clock
        self.sleep = sleep
        self.attempted = {"close": False, "remove": False}
        self.wire_started = {"close": False, "remove": False}
        self.acks = {"close": False, "remove": False}
        self.unknown = {}
        self.operations = {
            kind: CleanupOperation(
                actor=self.actor,
                producer=producer_binding,
                target=target,
                kind=kind,
                recorder=recorder,
            )
            for kind in ("close", "remove")
        }
        self.intents = {
            kind: {
                k: target[k] for k in ("owner", "dseq", "group_population", "returned_runner_id")
            }
            | dict(
                source_record_sha256=target[
                    "deployment_ack_record_sha256" if kind == "close" else "jit_ack_record_sha256"
                ]
            )
            for kind in ("close", "remove")
        }

    def history(self):
        self.revalidate()
        value = self.recorder.history(self.producer, self.target)
        require(
            value.get("complete") is True
            and value.get("missing") == []
            and value.get("incomplete") is False
            and value.get("target_sha256") == record_sha256(self.target)
        )
        require(value.get("actor") == self.actor and value.get("producer") == self.producer)
        return value

    def guard(self, kind):
        # Poison before any final check; neither error nor ACK loss retries a send.
        require(not self.attempted[kind])
        self.attempted[kind] = True
        start = self.clock()
        history = self.history()
        require(history["wire_clear"][kind] is True)
        ack = self.unknown[kind]
        held = [
            r
            for r in history["current_actor_records"]
            if r["record"].get("kind") == kind and r["record"].get("phase") == "UNKNOWN"
        ]
        require(
            len(held) == 1
            and held[0]["body_sha256"] == ack["record_sha256"]
            and held[0]["artifact_id"] == ack["artifact_id"]
        )
        self.io.not_busy(self.target["returned_runner_id"], self.producer["runner_group_id"])
        self.authority(self.producer, self.target)
        if kind == "remove":
            require(self.closed(self.producer, self.target))
        require(self.clock() - start < 30)
        self.wire_started[kind] = True

    def wait_closed(self):
        for index in range(8):
            if self.closed(self.producer, self.target):
                return True
            if index < 7:
                self.sleep(3)
        return False

    def run(self):
        history = self.history()
        self.io.not_busy(self.target["returned_runner_id"], self.producer["runner_group_id"])
        self.authority(self.producer, self.target)
        already_closed = self.closed(self.producer, self.target)
        absent = self.io.absent(self.target["returned_runner_id"])
        # Missing/incomplete history already held. Fully observed end state needs no wire.
        if already_closed and absent:
            return self.result()
        for kind in ("close", "remove"):
            self.operations[kind].no_wire(self.intents[kind])
        if not already_closed:
            require(history["wire_clear"]["close"] is True)
            # Owner lookup precedes UNKNOWN; the actual transport invokes guard again.
            closer = self.closer_factory(self.target, lambda: self.guard("close"))
            self.unknown["close"] = self.operations["close"].before_wire(self.intents["close"])
            try:
                with (
                    open(os.devnull, "w", encoding="utf-8") as sink,
                    contextlib.redirect_stdout(sink),
                    contextlib.redirect_stderr(sink),
                ):
                    closer.close_deployment(self.target["dseq"])
                self.operations["close"].ack(self.intents["close"] | dict(response_received=True))
                self.acks["close"] = True
            except Exception:
                # UNKNOWN is never rewritten/retried. Only native terminal proof can proceed.
                self.acks["close"] = False
                require(self.wire_started["close"])
            require(self.wait_closed())
            if not self.acks["close"]:
                # A failed pipe ACK is fail-stop. Only a fully observed no-wire result
                # is possible in this handler; another actor must admit further work.
                require(self.io.absent(self.target["returned_runner_id"]))
                return self.result()
        else:
            require(self.closed(self.producer, self.target))
        if not self.io.absent(self.target["returned_runner_id"]):
            require(self.history()["wire_clear"]["remove"] is True)
            self.unknown["remove"] = self.operations["remove"].before_wire(self.intents["remove"])
            try:
                self.io.remove(self.target["returned_runner_id"], lambda: self.guard("remove"))
                self.operations["remove"].ack(
                    self.intents["remove"] | dict(response_received=True)
                )
                self.acks["remove"] = True
            except Exception:
                self.acks["remove"] = False
                require(self.wire_started["remove"])
        require(
            self.closed(self.producer, self.target)
            and self.io.absent(self.target["returned_runner_id"])
        )
        return self.result()

    def result(self):
        self.revalidate()
        return dict(
            outcome="CLEANED",
            cleanup_qualified=True,
            lease_closed=True,
            registration_absent=True,
            returned_runner_id=self.target["returned_runner_id"],
            dseq=self.target["dseq"],
            wallet_address=self.target["owner"],
            target_run_id=self.producer["run_id"],
            target_run_attempt=self.producer["run_attempt"],
            target_sdk_round=self.producer["sdk_round"],
            close_wire_attempted=self.wire_started["close"],
            remove_wire_attempted=self.wire_started["remove"],
            close_ack_received=self.acks["close"],
            remove_ack_received=self.acks["remove"],
        )


def cleanup(environment):
    from .keydb_qualification_cleanup_native import locate_environment
    from .keydb_qualification_cleanup_pipe import PipeCleanupRecorder

    locate_environment(environment)
    token = environment.get("GH_TOKEN", "")
    current = actor(environment, NativeGET(token))
    declared = validate_actor(
        json.loads(environment.get("KEYDB_QUALIFICATION_CLEANUP_ACTOR_JSON", ""))
    )
    require(declared == current)
    recorder = PipeCleanupRecorder(current)
    bundle = validate_bundle(current, recorder.read_target())
    binding, target = bundle["producer"], bundle["target"]

    def renew():
        get = NativeGET(token)
        require(actor(environment, get) == current)
        producer(current, binding, get, target)

    renew()
    return CleanupEngine(
        actor_value=current,
        producer_binding=binding,
        target=target,
        recorder=recorder,
        revalidate=renew,
        runner_api=RunnerAPI(token),
    ).run()


def main():
    require(len(sys.argv) == 1)
    result = None
    try:
        result = cleanup(os.environ)
    except Exception:
        result = None
    if result is None:
        raise SystemExit(
            "KeyDB qualification cleanup HOLD; no cleanup qualification or retry authority"
        )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for key, value in result.items():
            text = str(value).lower() if type(value) is bool else str(value)
            require("\n" not in text and "\r" not in text)
            output.write(key + "=" + text + "\n")


if __name__ == "__main__":
    main()
