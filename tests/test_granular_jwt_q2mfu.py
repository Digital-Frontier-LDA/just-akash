"""openmix-q2mfu: every Console JWT names the deployment it is for, and finding the key's
account mints none.

On 2026-11-19 the live Console refused two df-cicd akash-lease runs (44522018790 and
44522346315) with HTTP 403 "Name the deployments the token is for": the GPU check's
lease-shell JWT, then the close's ``account_address()``, which minted a dseq-0 token to read
its issuer. Both leases stayed up. These tests drive the two paths df-cicd's
infra/scripts/akash_lease.py runs (``close`` and ``_lease_smi``), call for call, against the
localhost fake Console in tests/_fake_akash.py, which now answers the recorded 403 to any
JWT body that does not name its deployments one by one, and records every body it got.

No real Console call can happen: the fixture refuses any request that is not to the
localhost fake before it is sent.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

import pytest

from tests._fake_akash import (
    _PROVIDER,
    FAKE_OWNER,
    GRANULAR_TOKEN,
    JWT_REFUSED_BODY,
    JWT_REFUSED_STATUS,
    WALLET_WIDE_PROBE,
    granular_names_its_deployments,
)

DSEQ = "1795091557151"
SMI = b"NVIDIA V100-SXM2-32GB, 32768 MiB\n"
GPU_SMI_SCRIPT = "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader"


@pytest.fixture
def fake(monkeypatch):
    import just_akash.api as api
    import just_akash.transport.lease_shell as lease_shell
    from tests._fake_akash import FakeAkash

    fa = FakeAkash()
    real_urlopen = urllib.request.urlopen

    def localhost_only(request, *args, **kwargs):
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        host = urllib.parse.urlparse(url).hostname
        assert host == "127.0.0.1", f"a request left the fake Console: {url}"
        return real_urlopen(request, *args, **kwargs)

    monkeypatch.setattr(api.urllib.request, "urlopen", localhost_only)
    # The two TLS seams of tests/test_integration_fake.py: the proxy socket goes to the
    # fake's local stub (exactly fa.proxy_url, on 127.0.0.1), which takes no ssl= argument.
    # Every other URL keeps its TLS context.
    monkeypatch.setattr(
        lease_shell.LeaseShellTransport, "_get_proxy_ws_url", lambda self: fa.proxy_url
    )
    real_connect = lease_shell.connect

    def _connect(url, **kw):
        if url == fa.proxy_url:
            kw.pop("ssl", None)
        return real_connect(url, **kw)

    monkeypatch.setattr(lease_shell, "connect", _connect)
    monkeypatch.setenv("AKASH_CONSOLE_URL", fa.console_url)
    fa.shell.script_overrides["nvidia-smi"] = SMI
    try:
        yield fa
    finally:
        fa.stop()


def _outcome(fn):
    """("ok", value) or ("refused", the error's text): a failure lands on an assert line."""
    try:
        return "ok", fn()
    except Exception as e:  # noqa: BLE001 -- the outcome IS the assertion subject
        return "refused", f"{type(e).__name__}: {e}"


def _named(body: dict) -> list[tuple[int, str, tuple[str, ...], tuple[str, ...]]]:
    """(dseq, provider, scope, services) for every deployment a JWT body names."""
    return [
        (dep["dseq"], perm["provider"], tuple(dep["scope"]), tuple(dep["services"]))
        for perm in body["data"]["leases"]["permissions"]
        for dep in perm["deployments"]
    ]


def _client():
    from just_akash.api import AkashConsoleAPI

    return AkashConsoleAPI("test-key")  # exactly as akash_lease.py builds it (env base URL)


# ── the fake refuses what the live Console refused ───────────────────────────


@pytest.mark.parametrize(
    "leases",
    [
        {"access": "scoped", "scope": ["status"]},  # the dseq-0 owner probe's body
        {"access": "scoped", "scope": ["shell"]},  # create_jwt's old body
        {  # create_jwt_with_provider's old body: provider-wide, no deployment named
            "access": "granular",
            "permissions": [{"provider": _PROVIDER, "access": "scoped", "scope": ["shell"]}],
        },
        {  # granular, but a deployment entry without its dseq
            "access": "granular",
            "permissions": [
                {
                    "provider": _PROVIDER,
                    "access": "granular",
                    "deployments": [{"scope": ["shell"], "services": ["web"]}],
                }
            ],
        },
        {  # granular, but a scope entry that is no string
            "access": "granular",
            "permissions": [
                {
                    "provider": _PROVIDER,
                    "access": "granular",
                    "deployments": [{"dseq": 7, "scope": [{}], "services": ["web"]}],
                }
            ],
        },
        {  # granular naming dseq 0
            "access": "granular",
            "permissions": [
                {
                    "provider": _PROVIDER,
                    "access": "granular",
                    "deployments": [{"dseq": 0, "scope": ["shell"], "services": ["web"]}],
                }
            ],
        },
    ],
    ids=["owner-probe", "scoped", "provider-wide", "no-dseq", "scope-not-str", "dseq-0"],
)
def test_the_fake_answers_the_recorded_403_to_every_unnamed_grant(fake, leases) -> None:
    from just_akash.api import AkashAPIError

    client = _client()
    body = {"data": {"ttl": 300, "leases": leases}}
    got = _outcome(lambda: client._request("POST", "/v1/create-jwt-token", body))
    assert got[0] == "refused"
    assert f"API Error ({JWT_REFUSED_STATUS}): {JWT_REFUSED_BODY['message']}" in got[1]
    assert AkashAPIError.__name__ in got[1]
    assert fake.console.app.jwt_requests == [body]


def test_the_fake_accepts_the_granular_grant_naming_the_dseq(fake) -> None:
    body = {
        "data": {
            "ttl": 300,
            "leases": {
                "access": "granular",
                "permissions": [
                    {
                        "provider": _PROVIDER,
                        "access": "granular",
                        "deployments": [
                            {"dseq": int(DSEQ), "scope": ["shell"], "services": ["web"]}
                        ],
                    }
                ],
            },
        }
    }
    assert granular_names_its_deployments(body)
    got = _outcome(lambda: _client()._request("POST", "/v1/create-jwt-token", body))
    assert got == ("ok", {"data": {"token": GRANULAR_TOKEN}})


# ── account_address(): the key's account without a JWT ──────────────────────


def test_account_address_reads_the_account_when_the_probe_is_refused(fake) -> None:
    """A project-limited key: the wallet-wide probe is refused (the live 403), so the
    account comes from /v1/user/me and /v1/wallets, and no other JWT is minted."""
    got = _outcome(lambda: _client().account_address())
    assert got == ("ok", FAKE_OWNER)
    assert fake.console.app.jwt_requests == [WALLET_WIDE_PROBE]
    assert fake.console.app.user_me_reads == 1


def test_account_address_keeps_the_probe_for_a_wallet_key(fake) -> None:
    """THE CONTROL (Sentinel option a): a key that may still mint the probe takes today's
    path, unchanged, and never reads /v1/user/me."""
    fake.console.app.wallet_wide_probe_allowed = True
    got = _outcome(lambda: _client().account_address())
    assert got == ("ok", FAKE_OWNER)
    assert fake.console.app.jwt_requests == [WALLET_WIDE_PROBE]
    assert fake.console.app.user_me_reads == 0


def test_account_address_with_a_dseq_reads_the_status_token_issuer(fake) -> None:
    """A project-limited key given a dseq: after the probe's 403, a read-only status token
    naming that deployment, whose iss is the wallet. Never /v1/user/me."""
    got = _outcome(lambda: _client().account_address(DSEQ))
    assert got == ("ok", FAKE_OWNER)
    probe, body = fake.console.app.jwt_requests
    assert probe == WALLET_WIDE_PROBE
    assert _named(body) == [(int(DSEQ), _PROVIDER, ("status",), ("web",))]
    assert fake.console.app.user_me_reads == 0


# ── create_jwt(): granular, naming the dseq ─────────────────────────────────


def test_create_jwt_names_the_dseq_provider_and_service(fake) -> None:
    got = _outcome(lambda: _client().create_jwt(DSEQ, ttl=300, scope=["status"]))
    assert got == ("ok", GRANULAR_TOKEN)
    (body,) = fake.console.app.jwt_requests
    assert granular_names_its_deployments(body)
    assert _named(body) == [(int(DSEQ), _PROVIDER, ("status",), ("web",))]


def test_create_jwt_with_provider_names_the_dseq(fake) -> None:
    got = _outcome(lambda: _client().create_jwt_with_provider(DSEQ, _PROVIDER, scope=["logs"]))
    assert got == ("ok", GRANULAR_TOKEN)
    (body,) = fake.console.app.jwt_requests
    assert _named(body) == [(int(DSEQ), _PROVIDER, ("logs",), ("web",))]


@pytest.mark.parametrize("dseq", ["0", "", "abc", "-5", "1.5", None])
def test_there_is_no_dseq_less_jwt(fake, dseq) -> None:
    got = _outcome(lambda: _client().create_jwt(dseq, ttl=300, scope=["status"]))
    assert got[0] == "refused"
    assert "names the deployment" in got[1]
    assert fake.console.app.jwt_requests == [], "nothing may be sent for an unnamed grant"


def test_create_jwt_keeps_explicit_services_when_the_lease_reports_none(fake) -> None:
    """The caller named the service, so only the provider is read from the lease, even
    while the lease reports no service yet."""
    fake.console.app.lease_services = {}
    got = _outcome(lambda: _client().create_jwt(DSEQ, scope=["logs"], services=["worker"]))
    assert got == ("ok", GRANULAR_TOKEN)
    (body,) = fake.console.app.jwt_requests
    assert _named(body) == [(int(DSEQ), _PROVIDER, ("logs",), ("worker",))]


def test_create_jwt_fails_closed_when_no_service_can_be_named(fake) -> None:
    """Neither the caller nor the lease names a service: refused, nothing minted."""
    fake.console.app.lease_services = {}
    got = _outcome(lambda: _client().create_jwt(DSEQ, scope=["events"]))
    assert got[0] == "refused"
    assert "reports no service" in got[1]
    assert fake.console.app.jwt_requests == []


@pytest.mark.parametrize(
    "services",
    [[""], [None], ["web", ""], "web", [7]],
    ids=["empty", "none", "one-empty", "str", "int"],
)
def test_create_jwt_refuses_a_malformed_service_before_sending(fake, services) -> None:
    got = _outcome(
        lambda: _client().create_jwt_with_provider(
            DSEQ, _PROVIDER, scope=["shell"], services=services
        )
    )
    assert got[0] == "refused"
    assert got[1].startswith("ValueError: ")
    assert "service" in got[1]
    assert fake.console.app.jwt_requests == [], "nothing may be sent for a malformed grant"


# ── the two df-cicd paths, end to end on the fake ───────────────────────────


def test_close_end_to_end(fake) -> None:
    """akash_lease.py close(): bind the key to the wallet, then close the deployment."""

    def close():
        client = _client()
        acct = client.account_address()
        assert acct == FAKE_OWNER, f"the Console key belongs to {acct}, not {FAKE_OWNER}"
        client.close_deployment(DSEQ)
        return acct

    got = _outcome(close)
    assert got == ("ok", FAKE_OWNER)
    assert fake.console.app.closed == [DSEQ]
    assert fake.console.app.jwt_requests == [WALLET_WIDE_PROBE]


def test_gpu_check_end_to_end(fake, capfd) -> None:
    """akash_lease.py _lease_smi(): bind the key, open the lease shell, run nvidia-smi."""
    from just_akash.cli import _make_lease_shell

    def smi():
        client = _client()
        acct = client.account_address()
        assert acct == FAKE_OWNER, f"the Console key belongs to {acct}, not {FAKE_OWNER}"
        transport = _make_lease_shell(client, DSEQ)
        return transport.exec_shell_script(GPU_SMI_SCRIPT)

    got = _outcome(smi)
    out = capfd.readouterr().out
    assert got == ("ok", 0)
    assert SMI.decode() in out
    probe, *bodies = fake.console.app.jwt_requests
    assert probe == WALLET_WIDE_PROBE
    assert bodies, "the lease shell must have minted its JWT"
    assert all(granular_names_its_deployments(b) for b in bodies)
    assert {named for b in bodies for named in _named(b)} == {
        (int(DSEQ), _PROVIDER, ("shell",), ("web",))
    }


def test_the_jwt_bodies_are_json_round_trippable(fake) -> None:
    """The dseq goes over the wire as a JSON integer (the schema's type), not a string."""
    _outcome(lambda: _client().create_jwt(DSEQ, scope=["events"]))
    (body,) = fake.console.app.jwt_requests
    wire = json.loads(json.dumps(body))
    assert wire["data"]["leases"]["access"] == "granular"
    assert type(wire["data"]["leases"]["permissions"][0]["deployments"][0]["dseq"]) is int


def test_close_fails_closed_when_the_account_read_is_refused(fake, monkeypatch) -> None:
    """If a project-bound key may read neither its probe nor its account, the close stops
    with the cause named and closes nothing (GA condition on 1tm.15)."""
    import just_akash.api as api

    real = api.AkashConsoleAPI._request

    def refuse_me(self, method, path, *a, **kw):
        if method == "GET" and path.startswith("/v1/user/me"):
            raise api.AkashAPIError("API Error (403): project_forbidden", status=403)
        return real(self, method, path, *a, **kw)

    monkeypatch.setattr(api.AkashConsoleAPI, "_request", refuse_me)

    def close():
        client = _client()
        client.account_address()
        client.close_deployment(DSEQ)

    got = _outcome(close)
    assert got[0] == "refused"
    assert "project-bound key cannot read account" in got[1]
    assert fake.console.app.closed == []
