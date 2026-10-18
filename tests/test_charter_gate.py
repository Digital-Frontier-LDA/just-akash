"""AC1 (just-akash-3sq / openmix-pvr6.1): no lease of OURS without a lease charter.

"Ours" is Flynn's own attribution (app/background/leases.py, openmix 8943c379): a placement
name starting flynn- or fleet-, or exactly dcloud / akash. For those, a deploy with no
charter is REFUSED under JUST_AKASH_CHARTER=enforce -- before any create_deployment, which
is the first call that spends -- and WARNED under the default (warn: rollout is warn first,
then enforce). Every other caller (dfci-*, just-akash-*) is unchanged in both modes: same
create, same --deposit, no Flynn call.
"""

from __future__ import annotations

import json

import pytest

from just_akash import charter as charter_mod
from just_akash import deploy as deploy_mod
from tests import _charter_harness as harness

OURS = ["flynn-x", "fleet-serve-y", "dcloud", "akash"]
NOT_OURS = ["dfci-infra-r", "just-akash-runner"]
MODES = ["warn", "enforce"]


@pytest.fixture
def h(monkeypatch, tmp_path):
    hh = harness.start(monkeypatch, tmp_path)
    yield hh
    harness.stop(hh)


class _StopAtCreate(Exception):
    pass


def _stop_at_first_create(monkeypatch, h):
    """Let deploy() run for real up to its first create_deployment, record that call, then
    stop: AC1 is about whether and how the create is made, not about the auction after it."""
    AkashConsoleAPI = harness.REAL_CONSOLE_API
    real = AkashConsoleAPI.create_deployment

    def create(self, sdl_content, deposit=5.0):
        real(self, sdl_content, deposit=deposit)
        raise _StopAtCreate

    monkeypatch.setattr(AkashConsoleAPI, "create_deployment", create)


CAPACITY = {
    "kind": "capacity",
    "served_model_name": "qwen3-32b",
    "image_digest": "sha256:" + "a" * 64,
    "canary_prompt": "say ok",
    "canary_expect": "ok",
}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("key", OURS)
def test_ours_without_a_charter(monkeypatch, h, capsys, key, mode):
    monkeypatch.setenv("JUST_AKASH_CHARTER", mode)
    _stop_at_first_create(monkeypatch, h)
    sdl = h.write_sdl(key)
    if mode == "enforce":
        with pytest.raises(charter_mod.CharterMissing):
            deploy_mod.deploy(sdl_path=sdl, bid_wait=1, bid_wait_retry=1)
        assert h.app.created_deposits == [], "enforce must refuse BEFORE create_deployment"
        assert h.events == [], "nothing may run after the refusal (no credit probe, no Flynn)"
    else:
        with pytest.raises(_StopAtCreate):
            deploy_mod.deploy(sdl_path=sdl, bid_wait=1, bid_wait_retry=1)
        codes = [
            json.loads(line)["code"]
            for line in capsys.readouterr().err.splitlines()
            if line.startswith('{"type": "akash-diag"')
        ]
        assert "CHARTER_MISSING" in codes
        # warn proceeds on today's path: the --deposit default, unstamped, no Flynn call.
        assert h.app.created_deposits == [5.0]
        assert h.flynn.posts == []


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("key", NOT_OURS)
def test_other_callers_are_unchanged(monkeypatch, h, capsys, key, mode):
    monkeypatch.setenv("JUST_AKASH_CHARTER", mode)
    _stop_at_first_create(monkeypatch, h)
    with pytest.raises(_StopAtCreate):
        deploy_mod.deploy(sdl_path=h.write_sdl(key), bid_wait=1, bid_wait_retry=1, deposit=3.0)
    assert h.app.created_deposits == [3.0]
    assert h.flynn.posts == []
    assert "CHARTER_MISSING" not in capsys.readouterr().err
    (sdl,) = [d["sdl"] for d in h.app.deployments.values()]
    assert ".ch-" not in sdl


def test_the_default_mode_is_warn(monkeypatch):
    monkeypatch.delenv("JUST_AKASH_CHARTER", raising=False)
    assert charter_mod.mode() == "warn"


def test_an_unknown_mode_is_refused_not_read_as_warn(monkeypatch):
    """A typo (`enforced`) must not silently downgrade to warn and let uncharted leases out."""
    monkeypatch.setenv("JUST_AKASH_CHARTER", "enforced")
    with pytest.raises(RuntimeError, match="JUST_AKASH_CHARTER"):
        charter_mod.mode()


@pytest.mark.parametrize(
    "key,ours",
    [
        ("flynn-x", True),
        ("flynn-burst-a", True),
        ("fleet-serve-y", True),
        ("fleet-serve-y.ch-0123456789", True),
        ("dcloud", True),
        ("akash", True),
        ("dcloud-x", False),
        ("akash-x", False),
        ("dfci-infra-r", False),
        ("just-akash-runner", False),
        ("borduas", False),
        ("${PLACEMENT_KEY}", False),
    ],
)
def test_is_ours_placement_mirrors_flynn(key, ours):
    assert charter_mod.is_ours_placement(key) is ours


def test_a_charter_for_a_placement_flynn_does_not_attribute_is_refused(monkeypatch, h):
    """A charter on a just-akash-* lease could never be matched to it by name: Flynn reads
    that prefix as JUST_AKASH, not ours. Refuse rather than mint an orphan charter."""
    _stop_at_first_create(monkeypatch, h)
    charter = charter_mod.load_charter(h.write_charter())
    with pytest.raises(RuntimeError, match="not one Flynn attributes"):
        deploy_mod.deploy(
            sdl_path=h.write_sdl("just-akash-runner"),
            bid_wait=1,
            bid_wait_retry=1,
            charter=charter,
        )
    assert h.app.created_deposits == [] and h.flynn.posts == []


class TestCharterDocument:
    """Validated locally with Flynn's validate_charter rules, BEFORE any network call."""

    def test_a_valid_job_charter_loads(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps(harness.charter_doc()))
        c = charter_mod.load_charter(str(p))
        assert c is not None
        assert (c.bead, c.kind, c.max_hours, c.max_act) == ("openmix-pvr6.1", "job", 10.0, 50.0)
        assert c.flynn_payload()["bead"] == "openmix-pvr6.1"
        assert "wallet_label" not in c.flynn_payload()

    def test_every_problem_is_named(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"kind": "forever", "max_hours": -1}))
        with pytest.raises(charter_mod.CharterInvalid) as exc:
            charter_mod.load_charter(str(p))
        problems = exc.value.problems
        for needle in ("bead", "owner", "why", "kind", "max_hours", "max_act"):
            assert any(needle in pr for pr in problems), (needle, problems)

    def test_capacity_needs_its_canary_fields(self):
        with pytest.raises(charter_mod.CharterInvalid, match="image_digest"):
            charter_mod.Charter.from_dict(harness.charter_doc(kind="capacity"))

    def test_prod_pinned_is_capacity_only(self):
        with pytest.raises(charter_mod.CharterInvalid, match="prod_pinned"):
            charter_mod.Charter.from_dict(harness.charter_doc(prod_pinned=True))

    @pytest.mark.parametrize("value", ["false", "true", 0, 1, "no"])
    def test_prod_pinned_must_be_a_json_boolean(self, value):
        """Review (CodeRabbit, a1eab31): bool("false") is True, and prod_pinned turns auto
        top-up ON -- a string must be refused, never coerced."""
        doc = harness.charter_doc(prod_pinned=value, **CAPACITY)
        with pytest.raises(charter_mod.CharterInvalid, match="prod_pinned must be a JSON boolean"):
            charter_mod.Charter.from_dict(doc)

    @pytest.mark.parametrize("value,expected", [(None, False), (False, False), (True, True)])
    def test_prod_pinned_null_or_boolean_loads(self, value, expected):
        doc = harness.charter_doc(prod_pinned=value, **CAPACITY)
        assert charter_mod.Charter.from_dict(doc).prod_pinned is expected

    def test_max_act_below_the_console_minimum_is_refused(self):
        with pytest.raises(charter_mod.CharterInvalid, match="max_act"):
            charter_mod.Charter.from_dict(harness.charter_doc(max_act=0.2))

    def test_a_non_http_url_is_refused(self):
        with pytest.raises(charter_mod.CharterInvalid, match="endpoint_url"):
            charter_mod.Charter.from_dict(harness.charter_doc(endpoint_url="file:///etc/passwd"))

    def test_the_env_var_carries_a_charter(self, monkeypatch):
        monkeypatch.setenv("JUST_AKASH_CHARTER_JSON", json.dumps(harness.charter_doc()))
        c = charter_mod.load_charter(None)
        assert c is not None and c.bead == "openmix-pvr6.1"

    def test_no_file_and_no_env_is_no_charter(self, monkeypatch):
        monkeypatch.delenv("JUST_AKASH_CHARTER_JSON", raising=False)
        assert charter_mod.load_charter(None) is None


class TestWalletLabel:
    """Flynn binds by wallet LABEL (console-2), and just-akash only knows addresses."""

    def test_the_charter_field_is_used_with_one_key(self, monkeypatch):
        monkeypatch.delenv("JUST_AKASH_WALLET_LABELS", raising=False)
        c = charter_mod.Charter.from_dict(harness.charter_doc(wallet_label="console-2"))
        assert charter_mod.wallet_label_for(c, None) == "console-2"

    def test_the_charter_field_confirmed_by_the_map_is_used(self, monkeypatch):
        monkeypatch.setenv("JUST_AKASH_WALLET_LABELS", "akash1abc=console-2, akash1def=console-3")
        c = charter_mod.Charter.from_dict(harness.charter_doc(wallet_label="console-2"))
        assert charter_mod.wallet_label_for(c, "akash1abc", configured_keys=2) == "console-2"

    def test_a_charter_label_naming_another_wallet_is_refused(self, monkeypatch):
        """Review (fallback, decabfe): the pool picked console-9's wallet; binding the lease
        as console-2 would attach it to the wrong wallet or 4xx and leave it uncharted."""
        monkeypatch.setenv("JUST_AKASH_WALLET_LABELS", "akash1abc=console-9")
        c = charter_mod.Charter.from_dict(harness.charter_doc(wallet_label="console-2"))
        with pytest.raises(RuntimeError, match="not the selected wallet"):
            charter_mod.wallet_label_for(c, "akash1abc")

    def test_a_charter_label_that_cannot_be_confirmed_with_several_keys_is_refused(
        self, monkeypatch
    ):
        monkeypatch.delenv("JUST_AKASH_WALLET_LABELS", raising=False)
        c = charter_mod.Charter.from_dict(harness.charter_doc(wallet_label="console-2"))
        with pytest.raises(RuntimeError, match="cannot be confirmed"):
            charter_mod.wallet_label_for(c, "akash1abc", configured_keys=3)

    def test_deploy_resolves_the_label_of_the_selected_wallet(self, monkeypatch):
        from types import SimpleNamespace

        from just_akash import deploy as deploy_mod

        monkeypatch.setenv("JUST_AKASH_WALLET_LABELS", "akash1one=console-1,akash1two=console-2")
        c = charter_mod.Charter.from_dict(harness.charter_doc(wallet_label=None))
        picked = SimpleNamespace(account="akash1two", configured_keys=2)
        assert deploy_mod._charter_wallet_label(c, picked, client=None) == "console-2"
        static = charter_mod.Charter.from_dict(harness.charter_doc(wallet_label="console-1"))
        with pytest.raises(RuntimeError, match="not the selected wallet"):
            deploy_mod._charter_wallet_label(static, picked, client=None)

    def test_the_address_map_is_the_fallback(self, monkeypatch):
        monkeypatch.setenv("JUST_AKASH_WALLET_LABELS", "akash1abc=console-9, akash1def=console-3")
        c = charter_mod.Charter.from_dict(harness.charter_doc(wallet_label=None))
        assert charter_mod.wallet_label_for(c, "akash1def") == "console-3"

    def test_no_label_is_refused(self, monkeypatch):
        monkeypatch.delenv("JUST_AKASH_WALLET_LABELS", raising=False)
        c = charter_mod.Charter.from_dict(harness.charter_doc(wallet_label=None))
        with pytest.raises(RuntimeError, match="wallet label"):
            charter_mod.wallet_label_for(c, "akash1abc")


def test_the_cli_passes_the_charter_file_through(monkeypatch, tmp_path):
    import sys
    from unittest.mock import patch

    from just_akash import cli

    seen = {}

    def fake_deploy(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(deploy_mod, "deploy", fake_deploy)
    p = tmp_path / "c.json"
    p.write_text(json.dumps(harness.charter_doc()))
    sdl = tmp_path / "s.yaml"
    sdl.write_text(harness.sdl_text("fleet-serve-y"))
    argv = ["just-akash", "deploy", "--sdl", str(sdl), "--charter", str(p)]
    with patch.object(sys, "argv", argv), pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert seen["charter"].bead == "openmix-pvr6.1"
