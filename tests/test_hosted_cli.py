"""Actual old SDK CLI/deploy/profile/API/wallet call stack; closed transport only."""

import base64
import json
import sys
import time
from types import SimpleNamespace
from typing import cast

import httpx
import pytest

from just_akash import api, chain, cli, deploy
from just_akash import guardian_legacy_lease_backend as backend
from just_akash import hosted_legacy_cli as hosted
from just_akash import legacy_capacity_cleanup as cleanup

OWNER = "akash14n4rkmz64rn0tey0r5g07l8q5x0fh2h4hu44kt"
PROVIDER = "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk"
HASH = "A" * 64


@pytest.fixture
def scene(tmp_path, monkeypatch):
    # Reuse raw signed-model fixtures only; product modules imported normally.
    from support_broker import Broker, Clock, Stream

    c = Clock()
    broker = Broker(backend, c)
    monkeypatch.setattr(time, "monotonic", c.mono)
    monkeypatch.setattr(time, "time", c.wall)
    monkeypatch.setattr(
        time,
        "sleep",
        lambda n: (setattr(c, "value", c.value + n), setattr(c, "utc", c.utc + int(n))),
    )
    monkeypatch.setattr(backend.httpx, "Client", broker.client)
    monkeypatch.setattr(backend.httpx, "AsyncClient", broker.mint_client)
    monkeypatch.setenv("AKASH_API_KEY", "RAM_ONLY_TOKEN")
    monkeypatch.delenv("AKASH_API_KEYS", raising=False)
    monkeypatch.delenv("AKASH_DIAGNOSTICS", raising=False)
    monkeypatch.setattr(api, "TAGS_FILE", tmp_path / "tags.json")
    state = SimpleNamespace(
        clock=c,
        broker=broker,
        console=[],
        sdl=None,
        profiles=None,
        delete_result={"txhash": HASH},
        expire_owner=False,
    )

    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.body

    def console(request, *, timeout):
        assert request.full_url.startswith("https://console-api.akash.network/")
        method = request.get_method()
        path = request.full_url.removeprefix("https://console-api.akash.network")
        body = json.loads(request.data) if request.data else None
        state.console.append((method, path, body))
        if path == "/v1/create-jwt-token":
            assert cast("dict[str, dict[str, object]]", body)["data"]["ttl"] == 300
            part = (
                base64.urlsafe_b64encode(json.dumps({"iss": OWNER}).encode()).decode().rstrip("=")
            )
            value = {"data": {"token": "public." + part + ".synthetic"}}
        elif path == "/v1/deployments" and method == "POST":
            state.sdl = cast("dict[str, dict[str, object]]", body)["data"]["sdl"]
            state.manifest = "PRIVATE_MANIFEST_SENTINEL"
            value = {"data": {"dseq": "123", "manifest": state.manifest}}
        elif path == "/v1/bids/123" or (method == "GET" and "bids" in path):
            value = {
                "data": [
                    {
                        "id": {"provider": PROVIDER, "gseq": 1, "oseq": 1},
                        "provider": PROVIDER,
                        "gseq": 1,
                        "state": "open",
                        "price": {"amount": "1", "denom": "uakt"},
                    }
                ]
            }
        elif path == "/v1/leases" and method == "POST":
            value = {"data": {"lease_created": True}}
        elif path == "/v1/deployments/123" and method == "DELETE":
            value = {"data": state.delete_result}
        else:
            raise AssertionError("UNEXPECTED_CLOSED_CONSOLE_ROUTE:" + method + ":" + path)
        return Response(json.dumps(value).encode())

    monkeypatch.setattr(api.urllib.request, "urlopen", console)
    # Only actual native read acquisition is mocked. Original configured-wallet
    # selection/factory/closer/last expiry check all execute unchanged.
    monkeypatch.setattr(chain, "deploy_credit", lambda owner: {"uact": 10000000})
    monkeypatch.setattr(
        chain, "corroborated_deployment_group_names", lambda owner, dseq, group: [group]
    )
    monkeypatch.setattr(
        chain,
        "owner_close_evidence",
        lambda owner, dseq, group: dict(
            owner=owner,
            dseq=dseq,
            expires_at="2000-01-01T00:00:00+00:00"
            if state.expire_owner
            else "2099-01-01T00:00:00+00:00",
        ),
    )
    old_derive = deploy.derive_resource_profiles

    def derive(sdl):
        state.profiles = old_derive(sdl)
        return state.profiles

    monkeypatch.setattr(deploy, "derive_resource_profiles", derive)
    # Extend the EXISTING finite model transport for the separate25 close codec.
    old_handle = broker.handle

    def handle(req):
        if str(req.url) != cleanup.URL:
            return old_handle(req)
        assert "x-api-key" not in req.headers and "x-akash-console-key" not in req.headers
        data = json.loads(req.content)
        consume = cleanup.historical_consume(data["parent_observation"].encode(), broker.policy)
        out = dict(
            schema=cleanup.SCHEMA,
            audience="akash-legacy-capacity-closure-observation",
            key_id=broker.policy.key_id,
            verifier_policy_revision=broker.policy.revision,
            broker_identity=broker.policy.broker_identity,
            phase="close",
            identity=consume["identity"],
            operation_id=consume["operation_id"],
            attempt=consume["attempt"],
            attempt_id=consume["attempt_id"],
            parent_digest=consume["parent_digest"],
            parent_revision=consume["parent_revision"] + 1,
            parent_revision_digest="b" * 64,
            provider_revision=consume["provider_revision"],
            provider_revision_digest="c" * 64,
            binding_digest=consume["binding_digest"],
            predecessor_sha256=backend.sha(data["parent_observation"].encode()),
            close_tx_hash=data["close_tx_hash"],
            closure_evidence_digest="d" * 64,
            placement_state="closed",
            execution_closed=True,
            settlement_proven=False,
            original_deadline_at=consume["original_deadline_at"],
            observed_at=c.utc,
            valid_until=c.utc + 20,
        )
        raw = backend.canonical(out)
        wire = backend.canonical(
            dict(
                payload=base64.b64encode(raw).decode(),
                signature=base64.b64encode(broker.key.sign(cleanup.DOMAIN + raw)).decode(),
            )
        )
        broker.phases.append("close")
        broker.requests.append(data)
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(wire)),
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
            },
            stream=Stream(wire, broker),
        )

    monkeypatch.setattr(broker, "handle", handle)
    state.sdl_path = tmp_path / "runner.yaml"
    state.receipt_path = tmp_path / "create-receipt.json"
    state.sdl_path.write_text("""version: "2.0"
services:
  runner:
    image: public-fixture.invalid/runner:fixed
profiles:
  compute:
    runner:
      resources:
        cpu: {units: "500m"}
        memory: {size: 512Mi}
        storage: {size: 512Mi}
  placement:
    dcloud:
      pricing:
        runner: {denom: uakt, amount: 100}
deployment:
  runner:
    dcloud: {profile: runner, count: 1}
""")

    def principals():
        return hosted.HostedGHAPrincipals(
            "PUBLIC_SYNTHETIC_OIDC", "PUBLIC_SYNTHETIC_READ", c.utc + 120
        )

    state.principals = principals
    actual_parent = "a85f7147c9e6d36807cff54d4823d9146b7e43d2"
    state.registration = lambda: hosted.ProvisionCLIRegistration(
        actual_parent,
        broker.policy,
        "operation",
        str(state.receipt_path),
        "9" * 40,
        "slot",
        900.0,
        1800000900,
        "akashnet-2",
        "key-candidate",
        principals,
    )
    return state


def invoke_deploy(s, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "just-akash",
            "deploy",
            "--sdl",
            str(s.sdl_path),
            "--bid-wait",
            "1",
            "--bid-wait-retry",
            "1",
            "--receipt-path",
            str(s.receipt_path),
            "--receipt-operation-id",
            "operation",
            "--provider",
            PROVIDER,
        ],
    )
    with pytest.raises(SystemExit) as result:
        cli.main()
    return result.value.code


def test_actual_CLI_deploy_sender_profile_and_bound_destroy_callback(scene, monkeypatch):
    s = scene
    with hosted.install_root_hosted_cli(s.registration()) as active:
        assert invoke_deploy(s, monkeypatch) == 0, s.broker.phases
        assert (
            active.used
            and cast(hosted.HostedCallResult, active.result).consume_locators
            and not cast(hosted.HostedCallResult, active.result).locator_custody_unknown
        )
        wire = cast(
            "tuple[bytes, ...]", cast(hosted.HostedCallResult, active.result).consume_locators
        )[0]
    assert s.profiles.profiles[1].cpu_millicores == 500
    assert s.profiles.profiles[1].memory_bytes == 512 * 1024**2
    assert json.loads(s.receipt_path.read_bytes())["state"] == "create_response_received"
    assert [x[0] for x in s.console if x[1] == "/v1/leases"] == ["POST"]
    assert backend.hook._reviewed_hook() is None
    s.clock.utc = 1800001000  # expired provisioning is ONLY a historical locator
    reg = hosted.CleanupCLIRegistration(
        s.broker.policy, wire, 900.0, s.clock.utc + 300, s.principals
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "just-akash",
            "destroy",
            "--dseq",
            "123",
            "--expected-owner",
            OWNER,
            "--expected-group",
            "dcloud",
            "--yes",
        ],
    )
    with hosted.install_root_hosted_cli(reg) as active:
        cli.main()
        assert (
            cast(
                cleanup.ClosedCapacityObservation,
                cast(hosted.HostedCallResult, active.result).cleanup_observation,
            ).execution_closed
            is True
        )
    assert sum(m == "DELETE" for m, p, b in s.console) == 1
    assert s.broker.phases == ["reserve", "consume", "outcome", "close"]
    assert hosted._reviewed_hosted_cli() is cleanup._reviewed_cleanup() is None


def test_lost_reserve_ACK_is_terminal_unknown_without_fake_zero(scene, monkeypatch):
    s = scene
    s.broker.fail_phase = "reserve"
    with hosted.install_root_hosted_cli(s.registration()) as active:
        code = invoke_deploy(s, monkeypatch)
        assert cast(hosted.HostedCallResult, active.result).locator_custody_unknown
        assert cast(hosted.HostedCallResult, active.result).consume_locators is None
        assert cast(hosted.HostedCallResult, active.result).primary_failure_class is not None
        assert s.broker.phases == ["reserve"]
        assert not any(p == "/v1/leases" for m, p, b in s.console)
        assert code == 78


def test_default_none_keeps_original_cli_dispatch_and_capture_inert(scene, monkeypatch):
    s = scene
    called = []
    monkeypatch.setattr(deploy, "deploy", lambda **kw: called.append(kw))
    assert hosted._reviewed_hosted_cli() is None
    hosted.capture_prepared_lease(object(), None, False, None, None)
    assert invoke_deploy(s, monkeypatch) == 0
    assert len(called) == 1 and called[0]["receipt_operation_id"] == "operation"
    assert not s.console and not s.broker.phases


@pytest.mark.parametrize(
    "failure",
    [
        "expired_mono",
        "expired_wall",
        "principal_missing",
        "principal_expired",
        "receipt_mismatch",
        "source_epoch",
        "tampered_function",
    ],
)
def test_opted_in_preflight_refuses_before_any_console_or_broker_effect(
    scene, monkeypatch, failure
):
    from dataclasses import replace

    s = scene
    reg = s.registration()
    if failure == "expired_mono":
        reg = replace(reg, original_end=s.clock.value)
    elif failure == "expired_wall":
        reg = replace(reg, original_deadline_at=s.clock.utc)
    elif failure == "principal_missing":
        reg = replace(reg, acquire_principals=lambda: None)
    elif failure == "principal_expired":
        reg = replace(
            reg, acquire_principals=lambda: hosted.HostedGHAPrincipals("oidc", "read", s.clock.utc)
        )
    elif failure == "receipt_mismatch":
        reg = replace(reg, receipt_path=reg.receipt_path + ".other")
    elif failure == "source_epoch":
        reg = replace(reg, sdk_parent_ref="f" * 40)
    elif failure == "tampered_function":
        monkeypatch.setattr(deploy, "deploy", lambda **kw: None)
    with hosted.install_root_hosted_cli(reg) as active:
        assert invoke_deploy(s, monkeypatch) == 78
        assert active.used
    assert not s.console and not s.broker.phases
    assert backend.hook._reviewed_hook() is None


def test_one_registration_cannot_reenter_after_success(scene, monkeypatch):
    s = scene
    with hosted.install_root_hosted_cli(s.registration()) as active:
        assert invoke_deploy(s, monkeypatch) == 0
        before = len(s.console)
        assert invoke_deploy(s, monkeypatch) == 78
        assert len(s.console) == before
    assert active._capture is None


@pytest.mark.parametrize("substitution", ["dseq", "manifest", "sdl", "operation", "client"])
def test_captured_actual_parent_cannot_be_rebound(scene, monkeypatch, substitution):
    s = scene
    with hosted.install_root_hosted_cli(s.registration()) as active:
        assert invoke_deploy(s, monkeypatch) == 0
        client, dseq, sdl, manifest, owner = cast(
            "tuple[api.AkashConsoleAPI, str, str, str, str]", active._capture
        )
        wire = json.loads(s.receipt_path.read_bytes())
        # Actual prepared tuple is submitting, derived from the genuine receipt.
        prepared = {
            k: v
            for k, v in wire.items()
            if k
            not in {
                "authority",
                "signer_owner_candidate",
                "owner_evidence_source",
                "dseq",
                "create_response_digest",
                "create_transaction_identifiers",
                "response_received_at",
            }
        }
        prepared["state"] = "submitting"
        from just_akash import deployment_receipt

        if substitution == "dseq":
            dseq = "124"
        elif substitution == "manifest":
            manifest += "tamper"
        elif substitution == "sdl":
            sdl += "\n# changed SDL bytes\n"
        elif substitution == "operation":
            prepared["operation_id"] = "other"
        elif substitution == "client":
            client = api.AkashConsoleAPI(api_key="RAM_ONLY_TOKEN")
        encoded = deployment_receipt._canonical_bytes(prepared)
        before = len(s.console)
        with pytest.raises(hosted.HostedCLIUnknown):
            hosted.capture_prepared_lease(
                client, (s.receipt_path, prepared, encoded), dseq, sdl, manifest
            )
        assert len(s.console) == before and len(s.broker.phases) == 3


def test_actual_selected_wallet_expiry_still_prevents_original_delete(scene, monkeypatch):
    s = scene
    with hosted.install_root_hosted_cli(s.registration()) as active:
        assert invoke_deploy(s, monkeypatch) == 0
        wire = cast(
            "tuple[bytes, ...]", cast(hosted.HostedCallResult, active.result).consume_locators
        )[0]
    s.expire_owner = True
    reg = hosted.CleanupCLIRegistration(
        s.broker.policy, wire, 900.0, s.clock.utc + 300, s.principals
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "just-akash",
            "destroy",
            "--dseq",
            "123",
            "--expected-owner",
            OWNER,
            "--expected-group",
            "dcloud",
            "--yes",
        ],
    )
    with hosted.install_root_hosted_cli(reg):
        with pytest.raises(SystemExit) as outcome:
            cli.main()
        assert outcome.value.code != 0
    assert not any(m == "DELETE" for m, p, b in s.console)
    assert s.broker.phases == ["reserve", "consume", "outcome"]


@pytest.mark.parametrize("bad", ["unsigned_locator", "owner_mismatch", "expired_teardown"])
def test_cleanup_missing_binding_or_fresh_lifetime_refuses_before_delete(scene, monkeypatch, bad):
    s = scene
    with hosted.install_root_hosted_cli(s.registration()) as active:
        assert invoke_deploy(s, monkeypatch) == 0
        wire = cast(
            "tuple[bytes, ...]", cast(hosted.HostedCallResult, active.result).consume_locators
        )[0]
    reg = hosted.CleanupCLIRegistration(
        s.broker.policy, wire, 900.0, s.clock.utc + 300, s.principals
    )
    if bad == "unsigned_locator":
        from dataclasses import replace

        reg = replace(reg, consume_wire=b"{}")
    if bad == "expired_teardown":
        from dataclasses import replace

        reg = replace(reg, cleanup_deadline_at=s.clock.utc)
    owner = PROVIDER if bad == "owner_mismatch" else OWNER
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "just-akash",
            "destroy",
            "--dseq",
            "123",
            "--expected-owner",
            owner,
            "--expected-group",
            "dcloud",
            "--yes",
        ],
    )
    before = len(s.console)
    with hosted.install_root_hosted_cli(reg):
        with pytest.raises(SystemExit) as result:
            cli.main()
        assert result.value.code == 78
    assert len(s.console) == before and s.broker.phases == ["reserve", "consume", "outcome"]


def test_configured_cleanup_imports_real_deploy_in_separate_process_layout(scene, monkeypatch):
    # A fresh destroy process has not imported deploy. Use the genuine loader,
    # not a fake app/SDK module. Restore the prior module after this fixture.
    s = scene
    monkeypatch.delitem(sys.modules, "just_akash.deploy")
    hosted._sources(backend, 900.0, s.clock.utc + 300)
    loaded = sys.modules["just_akash.deploy"]
    assert loaded.__file__ == deploy.__file__
    assert loaded.deploy.__wrapped__.__code__.co_filename == deploy.__file__
