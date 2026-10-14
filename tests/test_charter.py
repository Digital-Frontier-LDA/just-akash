"""openmix-pvr6.1: the lease charter gate, the charter_id placement stamp and the JOB runtime
limit. Nothing here touches a network: Flynn is a recording opener, the Console a fake client."""

from __future__ import annotations

import io
import json
import math
import urllib.error
from typing import Any, cast

import pytest
import yaml

from just_akash import charter as C
from just_akash import deploy as deploy_mod
from just_akash import provenance as P
from just_akash import wallet_pool
from just_akash.api import AkashConsoleAPI
from just_akash.wallet_pool import WalletClientSelection

CONSOLE_2 = "akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee"
CONSOLE_3 = "akash14n4rkmz64rn0tey0r5g07l8q5x0fh2h4hu44kt"
KEY = "test-flynn-key-value-0123456789"
CID = "ch-0123456789"

JOB = {
    "bead": "openmix-pvr6.1",
    "owner": "dev2",
    "why": "unit test",
    "kind": "job",
    "max_hours": 2.5,
    "max_act": 4,
}
CAPACITY = {
    **JOB,
    "kind": "capacity",
    "served_model_name": "qwen",
    "image_digest": "sha256:" + "a" * 64,
    "canary_prompt": "2+2?",
    "canary_expect": "4",
}


def sdl_for(key: str, service: str = "app") -> str:
    return f"""---
version: "2.0"
services:
  {service}:
    image: nginx:1.27
    expose:
      - port: 80
        as: 80
        to:
          - global: true
profiles:
  compute:
    {service}:
      resources:
        cpu:
          units: 0.5
        memory:
          size: 512Mi
        storage:
          size: 1Gi
  placement:
    {key}:
      pricing:
        {service}:
          denom: uact
          amount: 1000
deployment:
  {service}:
    {key}:
      profile: {service}
      count: 1
"""


# ── a recording Flynn ────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._raw = json.dumps(body).encode()

    def read(self):
        return self._raw

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Flynn:
    """Answers the charter API from a script; records every request (URL, headers, body)."""

    def __init__(self, create=(201, {"charter_id": CID}), bind=(200, {"ok": True})):
        self.calls: list[dict] = []
        self.create, self.bind = create, bind

    def __call__(self, req, timeout):
        self.calls.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "headers": {k.lower(): v for k, v in req.header_items()},
                "body": json.loads(req.data.decode()),
            }
        )
        status, body = self.bind if req.full_url.endswith("/bind") else self.create
        if isinstance(status, Exception):
            raise status
        if status >= 400:
            raise urllib.error.HTTPError(
                req.full_url, status, "err", cast(Any, {}), io.BytesIO(json.dumps(body).encode())
            )
        return _Resp(status, body)


ENV = {"FLYNN_CHARTER_KEY": KEY}


# ── validation ───────────────────────────────────────────────────────────────────────────


def test_a_valid_job_and_capacity_charter_pass():
    assert C.validate_charter(JOB)["max_hours"] == 2.5
    assert C.validate_charter(CAPACITY)["kind"] == "capacity"


def test_every_problem_is_named_at_once():
    with pytest.raises(C.CharterInvalid) as e:
        C.validate_charter({"kind": "forever"})
    text = str(e.value)
    for k in ("bead", "owner", "why", "kind", "max_hours", "max_act"):
        assert k in text


@pytest.mark.parametrize("bad", [0, -1, math.inf, -math.inf, math.nan, "x", None, True, 1e9])
def test_max_hours_must_be_finite_positive_and_capped(bad):
    with pytest.raises(C.CharterInvalid, match="max_hours"):
        C.validate_charter({**JOB, "max_hours": bad})


@pytest.mark.parametrize("bad", [0, -2, math.inf, math.nan, "lots", None, False])
def test_max_act_must_be_finite_positive(bad):
    with pytest.raises(C.CharterInvalid, match="max_act"):
        C.validate_charter({**JOB, "max_act": bad})


@pytest.mark.parametrize(
    "missing", ["served_model_name", "image_digest", "canary_prompt", "canary_expect"]
)
def test_capacity_needs_its_init_checks(missing):
    with pytest.raises(C.CharterInvalid, match=missing):
        C.validate_charter({**CAPACITY, missing: ""})


@pytest.mark.parametrize(
    "digest", ["sha256:abc", "sha256:" + "A" * 64, "a" * 64, "md5:" + "a" * 64]
)
def test_capacity_digest_must_be_sha256_64_hex(digest):
    with pytest.raises(C.CharterInvalid, match="image_digest"):
        C.validate_charter({**CAPACITY, "image_digest": digest})


def test_prod_pinned_is_capacity_only_and_bad_regex_is_refused():
    with pytest.raises(C.CharterInvalid, match="prod_pinned"):
        C.validate_charter({**JOB, "prod_pinned": True})
    with pytest.raises(C.CharterInvalid, match="regex"):
        C.validate_charter({**CAPACITY, "canary_expect": "("})


def test_load_charter_from_json_file_or_mapping(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps(JOB))
    assert C.load_charter(str(f)) == JOB
    assert C.load_charter(json.dumps(JOB)) == JOB
    assert C.load_charter(JOB) == JOB
    with pytest.raises(C.CharterInvalid):
        C.load_charter("{not json")
    with pytest.raises(C.CharterInvalid):
        C.load_charter(str(tmp_path / "missing.json"))
    with pytest.raises(C.CharterInvalid):
        C.load_charter("[1, 2]")


def test_mode_defaults_to_warn_and_a_typo_is_refused():
    assert C.resolve_mode({}) == "warn"
    assert C.resolve_mode({"JUST_AKASH_CHARTER": " Enforce "}) == "enforce"
    with pytest.raises(C.CharterRefused):
        C.resolve_mode({"JUST_AKASH_CHARTER": "enforced"})


# ── scope: OUR names only ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key,ours",
    [
        ("flynn-fleet-x", True),
        ("flynn-burst-1", True),
        ("fleet-serve-qwen", True),
        ("dcloud", True),
        ("akash", True),
        ("dcloud-x", False),
        ("akash-node", False),
        ("dfci-infra-runner", False),
        ("dfci-ci", False),
        ("just-akash-runner", False),
        ("borduas", False),
        ("myflynn-x", False),
    ],
)
def test_is_ours(key, ours):
    assert P.is_ours(key) is ours


@pytest.mark.parametrize("key", ["dfci-infra-runner", "just-akash-canary", "borduas", "other"])
@pytest.mark.parametrize("mode", ["warn", "enforce", "off", "garbage"])
@pytest.mark.parametrize("given", [None, JOB])
def test_other_callers_are_unchanged_in_every_mode(key, mode, given):
    flynn = Flynn()
    plan = C.gate(sdl_for(key), given, env={**ENV, "JUST_AKASH_CHARTER": mode}, opener=flynn)
    assert plan.ours is False and plan.charter_id is None and plan.runtime_limit_hours is None
    assert flynn.calls == []


def test_a_templated_key_is_not_ours():
    assert (
        C.gate(sdl_for("${PLACEMENT_KEY}"), None, env={"JUST_AKASH_CHARTER": "enforce"}).ours
        is False
    )


# ── the gate, ours ───────────────────────────────────────────────────────────────────────


def test_enforce_without_a_charter_refuses():
    with pytest.raises(C.CharterMissing):
        C.gate(sdl_for("fleet-x"), None, env={"JUST_AKASH_CHARTER": "enforce"})


def test_warn_without_a_charter_logs_and_goes_on(caplog):
    plan = C.gate(sdl_for("dcloud"), None, env={})
    assert plan.ours and plan.mode == "warn" and plan.charter_id is None
    assert "no --charter" in caplog.text


def test_off_does_nothing():
    flynn = Flynn()
    plan = C.gate(sdl_for("fleet-x"), JOB, env={**ENV, "JUST_AKASH_CHARTER": "off"}, opener=flynn)
    assert plan.charter_id is None and plan.runtime_limit_hours is None and flynn.calls == []


def test_the_charter_is_posted_with_the_bearer_key_and_never_leaks_it(caplog):
    flynn = Flynn()
    plan = C.gate(sdl_for("fleet-x"), json.dumps(JOB), env=ENV, opener=flynn)
    assert plan.charter_id == CID and plan.runtime_limit_hours == 3
    (call,) = flynn.calls
    assert call["url"] == "https://api.flynn.digitalfrontier.so/api/v1/leases/charters"
    assert call["method"] == "POST"
    assert call["headers"]["authorization"] == f"Bearer {KEY}"
    assert call["body"]["bead"] == "openmix-pvr6.1" and call["body"]["kind"] == "job"
    assert KEY not in caplog.text


def test_the_canary_key_is_the_fallback_and_the_url_is_overridable():
    flynn = Flynn()
    env = {"FLYNN_CANARY_KEY": "canary-k", "FLYNN_API_URL": "https://staging.example/"}
    C.create_remote(C.validate_charter(JOB), env=env, opener=flynn)
    assert flynn.calls[0]["headers"]["authorization"] == "Bearer canary-k"
    assert flynn.calls[0]["url"] == "https://staging.example/api/v1/leases/charters"


def test_a_plain_http_flynn_url_is_refused_before_the_key_is_sent():
    flynn = Flynn()
    with pytest.raises(C.CharterAPIError, match="https"):
        C.create_remote(JOB, env={**ENV, "FLYNN_API_URL": "http://api.example"}, opener=flynn)
    assert flynn.calls == []


@pytest.mark.parametrize(
    "flynn,match",
    [
        (Flynn(create=(422, {"detail": ["kind must be exactly 'job'"]})), "HTTP 422: kind"),
        (Flynn(create=(500, {})), "HTTP 500"),
        (Flynn(create=(200, {"charter_id": CID})), "expected 201"),
        (Flynn(create=(201, {"charter_id": "ch-x; rm"})), "valid charter_id"),
        (Flynn(create=(urllib.error.URLError("down"), {})), "URLError"),
    ],
)
def test_a_charter_flynn_did_not_record_fails(flynn, match):
    with pytest.raises(C.CharterAPIError, match=match) as e:
        C.create_remote(JOB, env=ENV, opener=flynn)
    assert KEY not in str(e.value)


def test_no_key_fails_without_a_request():
    flynn = Flynn()
    with pytest.raises(C.CharterAPIError, match="FLYNN_CHARTER_KEY"):
        C.create_remote(JOB, env={}, opener=flynn)
    assert flynn.calls == []


def test_enforce_refuses_a_failed_post_and_warn_goes_on():
    env = {**ENV, "JUST_AKASH_CHARTER": "enforce"}
    with pytest.raises(C.CharterRefused, match="not recorded"):
        C.gate(sdl_for("fleet-x"), JOB, env=env, opener=Flynn(create=(500, {})))
    plan = C.gate(sdl_for("fleet-x"), JOB, env=ENV, opener=Flynn(create=(500, {})))
    assert plan.charter_id is None
    # the runtime limit does not depend on Flynn: a valid job charter still bounds the lease
    assert plan.runtime_limit_hours == 3


def test_enforce_refuses_an_invalid_charter_without_posting():
    flynn = Flynn()
    with pytest.raises(C.CharterRefused, match="invalid charter"):
        C.gate(
            sdl_for("fleet-x"),
            {**JOB, "max_act": math.inf},
            env={**ENV, "JUST_AKASH_CHARTER": "enforce"},
            opener=flynn,
        )
    assert flynn.calls == []


@pytest.mark.parametrize("hours,want", [(0.1, 1), (1, 1), (2.5, 3), (47.01, 48), (48, 48)])
def test_job_runtime_limit_is_ceil_max_hours(hours, want):
    assert C.runtime_limit_for(C.validate_charter({**JOB, "max_hours": hours})) == want


def test_capacity_has_no_runtime_limit():
    plan = C.gate(sdl_for("fleet-x"), CAPACITY, env=ENV, opener=Flynn())
    assert plan.charter_id == CID and plan.runtime_limit_hours is None


def test_a_job_longer_than_48h_is_refused_in_enforce_and_unbounded_in_warn():
    flynn = Flynn()
    with pytest.raises(C.CharterRefused, match="48 h"):
        C.gate(
            sdl_for("fleet-x"),
            {**JOB, "max_hours": 49},
            env={**ENV, "JUST_AKASH_CHARTER": "enforce"},
            opener=flynn,
        )
    assert flynn.calls == []
    plan = C.gate(sdl_for("fleet-x"), {**JOB, "max_hours": 49}, env=ENV, opener=Flynn())
    assert plan.charter_id == CID and plan.runtime_limit_hours is None


def test_bind_posts_label_and_dseq():
    flynn = Flynn()
    C.bind_remote(CID, wallet="console-2", dseq="123", env=ENV, opener=flynn)
    (call,) = flynn.calls
    assert call["url"].endswith(f"/api/v1/leases/charters/{CID}/bind")
    assert call["body"] == {"wallet": "console-2", "dseq": "123"}
    with pytest.raises(C.CharterAPIError, match="409"):
        C.bind_remote(CID, wallet="console-2", dseq="1", env=ENV, opener=Flynn(bind=(409, {})))
    with pytest.raises(C.CharterAPIError):
        C.bind_remote("../x", wallet="console-2", dseq="1", env=ENV, opener=Flynn())


def test_owner_labels():
    assert C.owner_label(CONSOLE_2) == "console-2"
    assert C.owner_label(CONSOLE_3) == "console-3"
    assert C.owner_label("akash1someoneelse") is None and C.owner_label(None) is None


# ── runtime limit read-back ──────────────────────────────────────────────────────────────


class _Settings:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def get_deployment_settings(self, dseq):
        self.calls += 1
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


def test_an_exact_int_reads_back():
    C.verify_runtime_limit(_Settings({"runtimeLimitHours": 3}), "1", 3, sleep=lambda s: None)


@pytest.mark.parametrize(
    "answer",
    [
        {"runtimeLimitHours": 4},
        {"runtimeLimitHours": "3"},
        {"runtimeLimitHours": None},
        {"runtimeLimitHours": 3.0},
        {"autoTopUpEnabled": True},
        RuntimeError("API Error (500)"),
    ],
)
def test_anything_else_is_a_refusal(answer):
    with pytest.raises(RuntimeError, match="runtime limit reads back"):
        C.verify_runtime_limit(_Settings(answer), "1", 3, sleep=lambda s: None)


def test_true_is_not_one():
    with pytest.raises(RuntimeError):
        C.verify_runtime_limit(
            _Settings({"runtimeLimitHours": True}), "1", 1, sleep=lambda s: None
        )


def test_a_missing_row_is_polled_then_refused():
    s, slept = _Settings({}), []
    with pytest.raises(RuntimeError, match="no settings row"):
        C.verify_runtime_limit(s, "1", 3, tries=3, sleep=slept.append)
    assert s.calls == 3 and len(slept) == 2
    late = _Settings({}, {"runtimeLimitHours": 3})
    C.verify_runtime_limit(late, "1", 3, sleep=lambda s: None)
    assert late.calls == 2


def test_create_deployment_sends_runtime_limit_only_when_asked():
    sent = []

    class _Api(AkashConsoleAPI):
        def _request(self, method, endpoint, data=None):
            sent.append(data)
            return {"data": {"dseq": "1"}}

    api = _Api("k")
    api.create_deployment("sdl", deposit=5.0)
    api.create_deployment("sdl", deposit=5.0, runtime_limit_hours=3)
    assert "runtimeLimitHours" not in sent[0]["data"]
    assert sent[1]["data"]["runtimeLimitHours"] == 3
    for bad in (0, 49, 2.5, True):
        with pytest.raises(ValueError):
            api.create_deployment("sdl", runtime_limit_hours=cast(Any, bad))
    assert len(sent) == 2


# ── stamp_charter ────────────────────────────────────────────────────────────────────────


def _group_names(text: str) -> tuple[set, set]:
    doc = yaml.safe_load(text)
    placement = set(doc["profiles"]["placement"])
    refs = {k for svc in doc["deployment"].values() for k in svc}
    return placement, refs


def test_the_stamped_group_name_carries_the_charter_id():
    out, keys = P.stamp_charter(sdl_for("fleet-serve-qwen"), CID)
    assert keys == [f"fleet-serve-qwen.{CID}"]
    assert _group_names(out) == ({keys[0]}, {keys[0]})
    assert P.charter_id_of(keys[0]) == CID
    assert keys[0].startswith("fleet-")


def test_a_service_named_like_its_placement_keeps_its_name():
    out, keys = P.stamp_charter(sdl_for("flynn-fleet-embed", service="flynn-fleet-embed"), CID)
    doc = yaml.safe_load(out)
    assert set(doc["services"]) == {"flynn-fleet-embed"}
    assert set(doc["profiles"]["compute"]) == {"flynn-fleet-embed"}
    assert set(doc["deployment"]) == {"flynn-fleet-embed"}
    assert _group_names(out) == ({keys[0]}, {keys[0]})


def test_a_long_key_is_truncated_to_62_keeping_prefix_and_id():
    key = "flynn-fleet-" + "x" * 70
    _, (stamped,) = P.stamp_charter(sdl_for(key), CID)
    assert len(stamped) == P.MAX_PLACEMENT_KEY_LEN
    assert stamped.startswith("flynn-fleet-") and stamped.endswith(f".{CID}")


def test_a_restamp_replaces_the_id_instead_of_stacking():
    once, _ = P.stamp_charter(sdl_for("fleet-x"), CID)
    twice, keys = P.stamp_charter(once, "ch-abcdefabcd")
    assert keys == ["fleet-x.ch-abcdefabcd"] and CID not in twice


@pytest.mark.parametrize("key", ["dcloud", "akash", "dfci-infra-x", "just-akash-runner", "other"])
def test_exact_names_and_other_callers_are_not_stamped(key):
    text = sdl_for(key)
    assert P.stamp_charter(text, CID) == (text, [])


def test_never_produces_the_sibling_prefix():
    for key in ("flynn-a", "fleet-b"):
        _, (k,) = P.stamp_charter(sdl_for(key), CID)
        assert not k.startswith(P.SIBLING_REAPED_PREFIX)


def test_a_block_scalar_that_looks_like_an_sdl_is_left_alone():
    text = sdl_for("fleet-x").replace(
        "    image: nginx:1.27\n",
        "    image: nginx:1.27\n    args:\n      - |\n"
        "        deployment:\n          app:\n            fleet-x:\n",
    )
    out, _ = P.stamp_charter(text, CID)
    assert "            fleet-x:\n" in out


def test_an_invalid_charter_id_is_refused():
    with pytest.raises(ValueError):
        P.stamp_charter(sdl_for("fleet-x"), "ch-../../x")


def test_an_unreferenced_placement_is_an_incomplete_stamp():
    text = sdl_for("fleet-x").replace("    fleet-x:\n      profile", "    fleet-y:\n      profile")
    with pytest.raises(ValueError, match="incomplete"):
        P.stamp_charter(text, CID)


# ── deploy(): the gate sits before any spend ─────────────────────────────────────────────


class _Stop(Exception):
    pass


class _Client:
    def __init__(self, events, *, create_ok=False, readback=None, account=CONSOLE_2):
        self.events, self.create_ok, self.readback, self.account = (
            events,
            create_ok,
            readback,
            account,
        )
        self.created: list[tuple[str, dict]] = []

    def account_address(self):
        return self.account

    def create_deployment(self, sdl_content, deposit=5.0, **kw):
        self.events.append("create")
        self.created.append((sdl_content, kw))
        if not self.create_ok:
            raise _Stop()
        return {"dseq": "4242", "manifest": ""}

    def get_deployment_settings(self, dseq):
        self.events.append("settings")
        return self.readback

    def close_deployment(self, dseq):
        self.events.append(f"close:{dseq}")
        return {}

    def get_bids(self, dseq):
        self.events.append("bids")
        raise AssertionError("no bid may be read before the runtime limit is verified")

    def list_deployments(self, active_only=True):
        return []


@pytest.fixture
def harness(tmp_path, monkeypatch):
    events: list[str] = []
    flynn = Flynn()
    for k in ("JUST_AKASH_CHARTER", "FLYNN_API_URL", "FLYNN_CANARY_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FLYNN_CHARTER_KEY", KEY)
    monkeypatch.setattr(
        C, "_default_opener", lambda req, t: (events.append("flynn"), flynn(req, t))[1]
    )
    monkeypatch.setattr(
        deploy_mod, "_check_wallet_credit", lambda *a, **k: events.append("credit")
    )
    state = {"client": _Client(events)}

    def select(*a, **k):
        events.append("select")
        c = state["client"]
        return WalletClientSelection(cast(Any, c), c.account, 10**9, 1, 1, "v1", False)

    monkeypatch.setattr(wallet_pool, "select_client_for_create", select)

    def run(key, charter=None, **client_kw):
        if client_kw:
            state["client"] = _Client(events, **client_kw)
        p = tmp_path / "deploy.yaml"
        p.write_text(sdl_for(key))
        return deploy_mod.deploy(sdl_path=str(p), bid_wait=2, bid_wait_retry=3, charter=charter)

    run.events, run.flynn, run.state = events, flynn, state
    return run


def test_enforce_ours_without_charter_creates_nothing(harness, monkeypatch):
    monkeypatch.setenv("JUST_AKASH_CHARTER", "enforce")
    with pytest.raises(C.CharterMissing):
        harness("fleet-serve-x")
    assert harness.state["client"].created == []
    assert harness.events == []  # not even a wallet selection or a credit probe


def test_enforce_ours_with_a_failing_charter_post_creates_nothing(harness, monkeypatch):
    monkeypatch.setenv("JUST_AKASH_CHARTER", "enforce")
    harness.flynn.create = (503, {})
    with pytest.raises(C.CharterRefused):
        harness("dcloud", charter=JOB)
    assert harness.state["client"].created == [] and harness.events == ["flynn"]


def test_enforce_ours_with_an_unknown_owner_creates_nothing(harness, monkeypatch):
    monkeypatch.setenv("JUST_AKASH_CHARTER", "enforce")
    with pytest.raises(C.CharterRefused, match="owner wallets"):
        harness("fleet-x", charter=JOB, account="akash1notours")
    assert harness.state["client"].created == []


@pytest.mark.parametrize("mode", ["warn", "enforce", "off"])
@pytest.mark.parametrize("key", ["dfci-infra-runner", "just-akash-runner"])
def test_other_callers_reach_create_unchanged(harness, monkeypatch, mode, key):
    monkeypatch.setenv("JUST_AKASH_CHARTER", mode)
    with pytest.raises(_Stop):
        harness(key)
    ((sent, kw),) = harness.state["client"].created
    assert kw == {} and "flynn" not in harness.events and ".ch-" not in sent


def test_warn_ours_without_charter_still_creates(harness):
    with pytest.raises(_Stop):
        harness("fleet-x")
    assert harness.events[-1] == "create" and "flynn" not in harness.events


def test_the_charter_is_posted_before_the_wallet_and_the_create_is_stamped(harness):
    with pytest.raises(_Stop):
        harness("fleet-serve-x", charter=json.dumps(JOB))
    assert harness.events == ["flynn", "select", "credit", "create"]
    ((sent, kw),) = harness.state["client"].created
    assert kw == {"runtime_limit_hours": 3}
    assert _group_names(sent) == ({f"fleet-serve-x.{CID}"}, {f"fleet-serve-x.{CID}"})


def test_a_runtime_limit_that_does_not_read_back_closes_the_order_unleased(harness):
    with pytest.raises(RuntimeError, match="runtime limit not verified"):
        harness("fleet-x", charter=JOB, create_ok=True, readback={"runtimeLimitHours": 48})
    assert harness.events[-3:] == ["create", "settings", "close:4242"]
    assert "bids" not in harness.events


def test_a_capacity_charter_sends_no_runtime_limit(harness):
    with pytest.raises(_Stop):
        harness("fleet-x", charter=CAPACITY)
    ((_, kw),) = harness.state["client"].created
    assert kw == {}


@pytest.mark.parametrize("argv,expect", [([], None), (["--charter", "c.json"], "c.json")])
def test_the_cli_flag_reaches_deploy(monkeypatch, argv, expect):
    import sys
    from unittest.mock import patch

    from just_akash import cli

    monkeypatch.setattr(sys, "argv", ["just-akash", "deploy", *argv])
    with patch("just_akash.deploy.deploy") as d, pytest.raises(SystemExit):
        cli.main()
    assert d.call_args.kwargs["charter"] == expect


def test_deploy_main_takes_the_flag_too(monkeypatch):
    import sys
    from unittest.mock import patch

    monkeypatch.setattr(sys, "argv", ["deploy", "--charter", '{"a": 1}'])
    with patch.object(deploy_mod, "deploy") as d, pytest.raises(SystemExit):
        deploy_mod.deploy_main()
    assert d.call_args.kwargs["charter"] == '{"a": 1}'
