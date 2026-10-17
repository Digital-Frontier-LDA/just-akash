"""Flynn openmix-h2l7: just_akash/lease_intent.py records WHY a canary lease exists.

A fake transport stands in for the network, so the request shapes, the modes and the secret
handling are what is tested. No test reaches Keycloak, Flynn or the chain.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from just_akash import lease_intent as LI

SECRET = "client-secret-0123456789abcdef"  # pragma: allowlist secret
TOKEN = "tok-from-keycloak"  # pragma: allowlist secret
CONSOLE2 = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"  # pragma: allowlist secret
IID = "11111111-2222-3333-4444-555555555555"


class Fake:
    """Records every request; answers by path."""

    def __init__(self, **codes):
        self.calls: list[dict] = []
        self.codes = {"token": 200, "create": 201, "bind": 200, "close": 200, "abandon": 200}
        self.codes.update(codes)
        self.chain: dict[str, dict] = {}

    def __call__(self, req):
        url = req.full_url
        body = req.data.decode() if req.data else ""
        self.calls.append({"url": url, "body": body, "headers": dict(req.header_items())})
        if url.endswith("/token"):
            code = self.codes["token"]
            return code, json.dumps({"access_token": TOKEN}).encode() if code == 200 else b""
        if "/akash/" in url or "/cosmos/" in url:
            for key, doc in self.chain.items():
                if key in url:
                    return 200, json.dumps(doc).encode()
            return 404, b""
        if url.endswith("/lease-intents"):
            code = self.codes["create"]
            return code, json.dumps({"id": IID, "state": "requested"}).encode()
        if url.endswith("/bind"):
            return self.codes["bind"], b"{}"
        if "/by-dseq/" in url:
            return self.codes["close"], b"{}"
        if url.endswith("/close"):
            return self.codes["abandon"], b"{}"
        return 500, b""

    def flynn(self):
        return [c for c in self.calls if "/api/v1/lease-intents" in c["url"]]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in (
        LI.SECRET_ENV,
        LI.MODE_ENV,
        "JUST_AKASH_INTENT_TOKEN_URL",
        "JUST_AKASH_INTENT_FLYNN_URL",
        "GITHUB_RUN_ID",
        "GITHUB_WORKFLOW",
        "GITHUB_ACTOR",
        "GITHUB_SERVER_URL",
        "GITHUB_REPOSITORY",
    ):
        monkeypatch.delenv(k, raising=False)


CREATE = [
    "create",
    "--owner",
    CONSOLE2,
    "--group-name",
    "just-akash-canary",
    "--reason",
    "persistent provider canary for onidc",
    "--bead",
    "openmix-h2l7",
]


def test_without_the_secret_every_command_is_inert_and_silent(capsys):
    fake = Fake()
    for argv in (
        CREATE,
        ["bind", "--id", IID, "--dseq", "1789100636167"],
        ["close", "--dseq", "1789100636167"],
        ["abandon", "--id", IID],
    ):
        assert LI.main(argv, transport=fake) == 0
    assert fake.calls == []
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_create_registers_a_capacity_intent_with_the_secret_off_argv_and_logs(monkeypatch, capsys):
    monkeypatch.setenv(LI.SECRET_ENV, SECRET)
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Digital-Frontier-LDA/just-akash")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_WORKFLOW", "Provider canary (persistent)")
    fake = Fake()
    assert LI.main(CREATE, transport=fake) == 0
    out = capsys.readouterr()
    assert out.out.strip() == IID
    token, create = fake.calls
    form = urllib.parse.parse_qs(token["body"])
    assert form["client_id"] == ["just-akash-lease-intent"]
    assert form["scope"] == ["flynn-lease-intent"] and form["client_secret"] == [SECRET]
    assert token["url"].startswith(
        "https://auth.console.digitalfrontier.so/realms/console-staging"
    )
    assert create["url"] == "https://api.flynn.digitalfrontier.so/api/v1/lease-intents"
    assert create["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(create["body"]) == {
        "repo": "Digital-Frontier-LDA/just-akash",
        "reason": "persistent provider canary for onidc",
        "charter": "CAPACITY",
        "wallet": "console-2",
        "max_hours": 720.0,
        "workflow": "Provider canary (persistent)",
        "run_url": "https://github.com/Digital-Frontier-LDA/just-akash/actions/runs/42",
        "bead": "openmix-h2l7",
        "group_name": "just-akash-canary",
    }
    assert SECRET not in out.out + out.err and TOKEN not in out.out + out.err


def test_an_account_that_is_not_ours_is_never_recorded_under_a_guessed_wallet(monkeypatch):
    monkeypatch.setenv(LI.SECRET_ENV, SECRET)
    fake = Fake()
    argv = list(CREATE)
    argv[argv.index(CONSOLE2)] = "akash1notours"
    assert LI.main(argv, transport=fake) == 0
    assert fake.flynn() == []


@pytest.mark.parametrize("var", ["JUST_AKASH_INTENT_TOKEN_URL", "JUST_AKASH_INTENT_FLYNN_URL"])
@pytest.mark.parametrize("url", ["http://evil.example/token", "file:///etc/passwd"])
def test_a_non_https_override_is_refused_before_any_request(monkeypatch, var, url):
    monkeypatch.setenv(LI.SECRET_ENV, SECRET)
    monkeypatch.setenv(var, url)
    fake = Fake()
    assert LI.main(CREATE, transport=fake) == 0  # warn: the canary goes on
    assert fake.calls == []
    monkeypatch.setenv(LI.MODE_ENV, "enforce")
    assert LI.main(CREATE, transport=fake) == 1
    assert fake.calls == []


def test_warn_mode_never_fails_the_caller_enforce_does(monkeypatch):
    monkeypatch.setenv(LI.SECRET_ENV, SECRET)
    assert LI.main(CREATE, transport=Fake(create=403)) == 0
    monkeypatch.setenv(LI.MODE_ENV, "enforce")
    assert LI.main(CREATE, transport=Fake(create=403)) == 1
    assert LI.main(CREATE, transport=Fake(token=401)) == 1


def test_enforce_without_the_secret_refuses_create_only(monkeypatch):
    monkeypatch.setenv(LI.MODE_ENV, "enforce")
    assert LI.main(CREATE, transport=Fake()) == 1
    assert LI.main(["close", "--dseq", "1789100636167"], transport=Fake()) == 0


def test_off_does_nothing(monkeypatch):
    monkeypatch.setenv(LI.SECRET_ENV, SECRET)
    monkeypatch.setenv(LI.MODE_ENV, "off")
    fake = Fake()
    assert LI.main(CREATE, transport=fake) == 0 and fake.calls == []


def test_bind_posts_the_dseq_and_refuses_a_non_dseq(monkeypatch):
    monkeypatch.setenv(LI.SECRET_ENV, SECRET)
    fake = Fake()
    assert LI.main(["bind", "--id", IID, "--dseq", "1789100636167"], transport=fake) == 0
    (bind,) = fake.flynn()
    assert bind["url"].endswith(f"/api/v1/lease-intents/{IID}/bind")
    assert json.loads(bind["body"]) == {"dseq": "1789100636167"}
    fake = Fake()
    monkeypatch.setenv(LI.MODE_ENV, "enforce")
    assert LI.main(["bind", "--id", IID, "--dseq", "01"], transport=fake) == 1
    urls = [c["url"] for c in fake.flynn()]
    # no bind is sent for a non-canonical dseq; the intent it was meant for is abandoned
    assert not any(u.endswith("/bind") for u in urls)
    assert urls == ["https://api.flynn.digitalfrontier.so/api/v1/lease-intents/" + IID + "/close"]


def test_abandon_closes_the_requested_intent_by_id(monkeypatch):
    monkeypatch.setenv(LI.SECRET_ENV, SECRET)
    fake = Fake()
    assert LI.main(["abandon", "--id", IID, "--reason", "onidc did not bid"], transport=fake) == 0
    (call,) = fake.flynn()
    assert call["url"].endswith(f"/api/v1/lease-intents/{IID}/close")
    assert json.loads(call["body"]) == {"reason": "onidc did not bid"}


def test_close_by_dseq_and_a_404_is_not_an_error(monkeypatch):
    monkeypatch.setenv(LI.SECRET_ENV, SECRET)
    monkeypatch.setenv(LI.MODE_ENV, "enforce")
    fake = Fake()
    assert LI.main(["close", "--dseq", "1789100636167"], transport=fake) == 0
    (call,) = fake.flynn()
    assert call["url"].endswith("/api/v1/lease-intents/by-dseq/1789100636167/close")
    assert LI.main(["close", "--dseq", "1789100636167"], transport=Fake(close=404)) == 0
    assert LI.main(["close", "--dseq", "1789100636167"], transport=Fake(close=500)) == 1


def test_close_quietly_is_inert_without_the_secret_and_never_raises(monkeypatch):
    fake = Fake()
    LI.close_quietly("1789100636167", "orphan closed", transport=fake)
    assert fake.calls == []
    monkeypatch.setenv(LI.SECRET_ENV, SECRET)

    def boom(req):
        raise LI.IntentError("request failed (URLError)")

    LI.close_quietly("1789100636167", "orphan closed", transport=boom)  # must not raise


# ── backfill: limits measured from the LEASE's chain creation ───────────────────────────────


def test_backfill_limits_are_age_plus_horizon_never_a_bare_horizon():
    blocks_800h = int(800 * 3600 / LI.BLOCK_TIME_S)
    hours, spend = LI.backfill_limits(
        created_height=1_000,
        latest_height=1_000 + blocks_800h,
        price_uact_per_block=2.0,
        horizon_h=720,
    )
    assert 1519 <= hours <= 1521  # ~800 h old + 720 h, rounded up
    per_day = LI.act_per_day(2.0)
    assert spend >= per_day * hours / 24.0  # never below what that rate implies
    assert spend == pytest.approx(per_day * hours / 24.0 * LI.SPEND_HEADROOM, abs=0.01)


def test_backfill_refuses_an_age_past_flynns_cap():
    blocks = int(2000 * 3600 / LI.BLOCK_TIME_S)
    with pytest.raises(LI.IntentError):
        LI.backfill_limits(created_height=1, latest_height=1 + blocks, price_uact_per_block=2.0)


def _chain(fake, dseq, *, created, latest, price="2", group="just-akash-canary.09a75b4f3236"):
    fake.chain[f"filters.dseq={dseq}"] = {
        "leases": [
            {
                "lease": {
                    "id": {"owner": CONSOLE2, "dseq": dseq, "gseq": 1, "oseq": 1},
                    "state": "active",
                    "price": {"denom": "uact", "amount": price},
                    "created_at": str(created),
                }
            }
        ]
    }
    fake.chain[f"id.dseq={dseq}"] = {
        "groups": [{"id": {"gseq": 1}, "group_spec": {"name": group}}]
    }
    fake.chain["blocks/latest"] = {"block": {"header": {"height": str(latest)}}}


def test_backfill_registers_and_binds_each_canary_with_chain_derived_limits(monkeypatch):
    monkeypatch.setenv(LI.SECRET_ENV, SECRET)
    fake = Fake()
    latest = 29_103_211
    _chain(fake, "1789100636167", created=28_572_385, latest=latest)
    rc = LI.main(
        [
            "backfill",
            "--owner",
            CONSOLE2,
            "--dseq",
            "1789100636167",
            "--reason",
            "just-akash persistent provider canary (backfill)",
        ],
        transport=fake,
    )
    assert rc == 0
    create, bind = fake.flynn()
    doc = json.loads(create["body"])
    age_h = (latest - 28_572_385) * LI.BLOCK_TIME_S / 3600
    assert doc["max_hours"] >= age_h + 720 and doc["charter"] == "CAPACITY"
    assert doc["wallet"] == "console-2" and doc["group_name"].startswith("just-akash-canary")
    assert doc["max_spend_act"] > 0
    assert json.loads(bind["body"]) == {"dseq": "1789100636167"}


def test_backfill_refuses_a_lease_that_is_not_a_canary(monkeypatch):
    monkeypatch.setenv(LI.SECRET_ENV, SECRET)
    monkeypatch.setenv(LI.MODE_ENV, "enforce")
    fake = Fake()
    _chain(fake, "1789116940502", created=28_575_154, latest=29_103_211, group="flynn-qwen3-coder")
    rc = LI.main(
        ["backfill", "--owner", CONSOLE2, "--dseq", "1789116940502", "--reason", "not a canary"],
        transport=fake,
    )
    assert rc == 1 and fake.flynn() == []


def test_a_backfill_whose_bind_fails_abandons_the_intent_it_created(monkeypatch):
    monkeypatch.setenv(LI.SECRET_ENV, SECRET)
    fake = Fake(bind=409)
    _chain(fake, "1789100636167", created=28_572_385, latest=29_103_211)
    LI.main(
        [
            "backfill",
            "--owner",
            CONSOLE2,
            "--dseq",
            "1789100636167",
            "--reason",
            "just-akash persistent provider canary (backfill)",
        ],
        transport=fake,
    )
    urls = [c["url"] for c in fake.flynn()]
    assert urls[-1].endswith(f"/api/v1/lease-intents/{IID}/close")  # abandoned


def test_the_close_plane_ends_the_intent_after_a_verified_close():
    """close_orphans calls the ledger AFTER the read-back proves the close, never before."""
    import inspect

    from just_akash import close_orphans

    src = inspect.getsource(close_orphans.run)
    assert src.index("TERMINAL_DEPLOYMENT_STATES") < src.index("lease_intent.close_quietly")


def test_a_failed_bind_abandons_the_intent_even_in_warn_mode(monkeypatch):
    """CodeRabbit on #457: a requested intent left behind can never be closed by dseq."""
    monkeypatch.setenv(LI.SECRET_ENV, SECRET)
    fake = Fake(bind=409)
    assert LI.main(["bind", "--id", IID, "--dseq", "1789100636167"], transport=fake) == 0
    urls = [c["url"] for c in fake.flynn()]
    assert urls[0].endswith("/bind") and urls[-1].endswith(f"/api/v1/lease-intents/{IID}/close")
    monkeypatch.setenv(LI.MODE_ENV, "enforce")
    assert LI.main(["bind", "--id", IID, "--dseq", "1789100636167"], transport=Fake(bind=409)) == 1


def test_a_redirect_is_never_followed_with_the_bearer_token():
    """CodeRabbit on #457: urllib's default handler would re-send Authorization to a target the
    https guard never checked, and a redirected 200 would read as success."""
    import http.server
    import threading

    seen: list[str] = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()

        def do_GET(self):  # noqa: N802
            seen.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = __import__("urllib.request").request.Request(
            f"http://127.0.0.1:{srv.server_port}/bind",
            data=b"{}",
            method="POST",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        status, _ = LI._urlopen(req)
    finally:
        srv.shutdown()
    assert status == 302 and seen == ["/bind"], "the redirect target must never be requested"
