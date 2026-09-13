"""just-akash#346: both auction call sites in `deploy()` feed the group's request to the core.

Driven through the REAL `deploy()` up to `create_lease`, so the provider it chose is the
observable — not whether a helper was called. The fleets are adversarial: the CHEAPEST
bidder is always one that CANNOT fit the correct aggregate, so a call site that forgets the
profile, drops `count`, or aggregates across groups picks it, and a correct one does not.

  site A  deploy.py `auction[collection]` — bids present when the collection window closes
  site B  deploy.py `auction[fallback]`   — no bid at first; bidders arrive in the fallback window
"""

from __future__ import annotations

import hashlib
import logging

import pytest
from akash_lease_core import from_provider_status

from just_akash import deploy as deploy_mod
from just_akash import wallet_pool

CHEAP, DEAR = "akash1cheap", "akash1dear"

# BLOCK style, as every SDL in sdl/ is written. `stamp_run` does not rewrite a flow-style
# `deployment` reference (`group: {profile: x, count: 2}`), leaving an SDL that names a
# placement group that does not exist; that is a separate defect, not exercised here.
ONE_GROUP = """\
version: "2.0"
services:
  app: {image: nginx, expose: [{port: 80, as: 80, to: [{global: true}]}]}
profiles:
  compute:
    replica:
      resources:
        cpu: {units: 1}
        memory: {size: 1Mi}
        storage: {size: 1Mi}
  placement:
    just-akash-callsite:
      pricing: {replica: {denom: uact, amount: 100}}
deployment:
  app:
    just-akash-callsite:
      profile: replica
      count: 2
"""


def _status(cpu_millicores: int, gpu: int = 0) -> dict:
    node = {
        "allocatable": {"cpu": 100_000, "memory": 10**12, "storage_ephemeral": 10**12, "gpu": 8},
        "available": {
            "cpu": cpu_millicores,
            "memory": 10**11,
            "storage_ephemeral": 10**11,
            "gpu": gpu,
        },
    }
    return {"cluster": {"inventory": {"available": {"nodes": [node]}}}}


def _bid(provider: str, amount: str, gseq: int = 1) -> dict:
    return {
        "bid": {
            "id": {"provider": provider, "gseq": gseq},
            "price": {"denom": "uakt", "amount": amount},
            "state": "open",
        }
    }


class _Chose(Exception):
    """Raised by create_lease: selection is complete and this is the winner."""

    def __init__(self, provider: str) -> None:
        super().__init__(provider)
        self.provider = provider


class _Clock:
    """`time.sleep` advances `time.time` — the bid loop's 5s polls cost nothing."""

    def __init__(self, real) -> None:
        self.now = real.time()

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def run_deploy(monkeypatch, tmp_path):
    submitted: list[str] = []

    def run(
        *,
        sdl: str,
        capacity: dict,
        bids: list,
        bids_after_first_poll: bool = False,
        gpu_variant: str | None = None,
        preferred: list[str] | None = None,
        **deploy_kwargs,
    ) -> str:
        polls = {"n": 0}

        class _Client:
            def account_address(self):
                return "akash1fake"

            def create_deployment(self, sdl_content, deposit=5.0):
                submitted.append(sdl_content)
                return {"dseq": "1234567890", "manifest": "{}"}

            def get_bids(self, dseq):
                polls["n"] += 1
                return [] if (bids_after_first_poll and polls["n"] == 1) else list(bids)

            def create_lease(self, *, provider, **_):
                raise _Chose(provider)

            def get_provider(self, address):
                return {"host_uri": "https://provider.example:8443"}

            def close_deployment(self, dseq):
                return {"ok": True}

            def list_deployments(self, active_only=True):
                return []

        class _Wallet:
            client = _Client()
            name = "FAKE"
            configured_keys = 1
            slot = "AKASH_CONSOLE"

        clock = _Clock(deploy_mod.time)
        monkeypatch.setattr(deploy_mod.time, "time", clock.time)
        monkeypatch.setattr(deploy_mod.time, "sleep", clock.sleep)
        monkeypatch.setattr(wallet_pool, "select_client_for_create", lambda *a, **k: _Wallet())
        monkeypatch.setattr(
            deploy_mod,
            "capacity_by_provider",
            lambda addresses, **k: {a: capacity[a] for a in addresses if a in capacity},
        )
        monkeypatch.delenv("AKASH_PROVIDERS", raising=False)
        monkeypatch.delenv("AKASH_PROVIDERS_BACKUP", raising=False)
        if preferred:
            monkeypatch.setenv("AKASH_PROVIDERS", ",".join(preferred))
        path = tmp_path / "deploy.yaml"
        path.write_text(sdl)
        if gpu_variant is not None:
            (tmp_path / "deploy-gpu.yaml").write_text(gpu_variant)
        with pytest.raises(_Chose) as chose:
            deploy_mod.deploy(
                sdl_path=str(path),
                bid_wait=2,
                bid_wait_retry=30,
                select="emptiest",
                **deploy_kwargs,
            )
        return chose.value.provider

    run.submitted = submitted
    return run


# CHEAP has 1500m: enough for ONE replica (1000m), not for the group (count 2 → 2000m).
FIT_SPLIT = {
    CHEAP: from_provider_status(_status(1500)),
    DEAR: from_provider_status(_status(4000)),
}


def test_site_A_rejects_the_cheap_provider_that_cannot_host_the_group(run_deploy, caplog) -> None:
    caplog.set_level(logging.INFO)
    chosen = run_deploy(
        sdl=ONE_GROUP, capacity=FIT_SPLIT, bids=[_bid(CHEAP, "1"), _bid(DEAR, "9")]
    )
    assert chosen == DEAR, "site A: the group needs 2000m and CHEAP has 1500m"
    assert "auction[collection] REQUEST_PROFILE gseq=1:cpu_millicores=2000" in caplog.text
    assert "auction[fallback]" not in caplog.text, "site A must decide this fleet on its own"


def test_site_B_fallback_also_rejects_the_provider_that_cannot_host_the_group(
    run_deploy, caplog
) -> None:
    """No bid in the collection window, so site A collects and site B decides. The fallback
    ranks by arrival (CHEAP sorts first on a tie); only the fit check can exclude it."""
    caplog.set_level(logging.INFO)
    chosen = run_deploy(
        sdl=ONE_GROUP,
        capacity=FIT_SPLIT,
        bids=[_bid(CHEAP, "1"), _bid(DEAR, "9")],
        bids_after_first_poll=True,
    )
    assert "auction[fallback] REQUEST_PROFILE gseq=1:cpu_millicores=2000" in caplog.text, (
        "fixture must drive the decision through site B"
    )
    assert chosen == DEAR, "site B: the group needs 2000m and CHEAP has 1500m"


TWO_GROUPS = ONE_GROUP.replace(
    "    just-akash-callsite:\n      pricing: {replica: {denom: uact, amount: 100}}\n",
    "    just-akash-callsite:\n      pricing: {replica: {denom: uact, amount: 100}}\n"
    "    just-akash-second:\n      pricing: {replica: {denom: uact, amount: 100}}\n",
).replace(
    "    just-akash-callsite:\n      profile: replica\n      count: 2\n",
    "    just-akash-callsite:\n      profile: replica\n      count: 2\n"
    "    just-akash-second:\n      profile: replica\n      count: 2\n",
)


def test_each_bid_is_fit_against_its_own_group_not_the_whole_deployment(run_deploy) -> None:
    """Both groups need 2000m; together 4000m. DEAR has 3000m: it fits the group it bid on
    and must win. An across-group aggregate would reject it too."""
    assert TWO_GROUPS.count("just-akash-second") == 2, (
        "fixture edit did not apply to both sections"
    )
    capacity = {
        CHEAP: from_provider_status(_status(1500)),
        DEAR: from_provider_status(_status(3000)),
    }
    chosen = run_deploy(
        sdl=TWO_GROUPS, capacity=capacity, bids=[_bid(CHEAP, "1"), _bid(DEAR, "9")]
    )
    assert chosen == DEAR


def test_the_profile_comes_from_the_gpu_variant_that_is_submitted(run_deploy) -> None:
    """`--gpu` swaps in `<stem>-gpu.yaml` BEFORE transformation. The profile must describe
    that submitted SDL (a GPU per replica), not the caller's CPU-only `--sdl`."""
    gpu_sdl = ONE_GROUP.replace(
        "        storage: {size: 1Mi}\n",
        "        storage: {size: 1Mi}\n"
        "        gpu: {units: 1, attributes: {vendor: {nvidia: []}}}\n",
    )
    assert gpu_sdl != ONE_GROUP
    capacity = {
        CHEAP: from_provider_status(_status(4000, gpu=0)),
        DEAR: from_provider_status(_status(4000, gpu=4)),
    }
    chosen = run_deploy(
        sdl=ONE_GROUP,
        gpu_variant=gpu_sdl,
        gpu=True,
        capacity=capacity,
        bids=[_bid(CHEAP, "1"), _bid(DEAR, "9")],
    )
    assert chosen == DEAR, "the submitted group requests 2 GPUs; CHEAP has none"


def test_the_profile_is_derived_from_the_exact_submitted_bytes(run_deploy, caplog) -> None:
    """⚠ ARTEFACT IDENTITY, not effect. Every `_prepare_sdl_content` transform (run-scoped
    placement key, image, SSH key, env) is resource-neutral, so deriving from the
    untransformed file yields the SAME numbers and no selection can tell. What differs is the
    artefact: the logged digest must be the sha256 of what `create_deployment` received."""
    caplog.set_level(logging.INFO)
    run_deploy(sdl=ONE_GROUP, capacity=FIT_SPLIT, bids=[_bid(CHEAP, "1"), _bid(DEAR, "9")])
    assert len(run_deploy.submitted) == 1
    submitted = run_deploy.submitted[0]
    assert submitted != ONE_GROUP, "precondition: the run-scoped placement key changed the bytes"
    digest = hashlib.sha256(submitted.encode("utf-8")).hexdigest()[:12]
    assert f"sdl_sha256={digest}" in caplog.text


def test_an_underivable_request_falls_back_to_cheapest_and_logs_why(run_deploy, caplog) -> None:
    """No profile can be derived (a template placeholder). #47's defined behaviour applies:
    cheapest, with the degradation named — and the reason is logged at the call site."""
    caplog.set_level(logging.INFO)
    templated = ONE_GROUP.replace("cpu: {units: 1}", "cpu: {units: '{{CPU}}'}")
    assert templated != ONE_GROUP
    chosen = run_deploy(
        sdl=templated, capacity=FIT_SPLIT, bids=[_bid(CHEAP, "1"), _bid(DEAR, "9")]
    )
    assert chosen == CHEAP
    assert "auction[collection] REQUEST_PROFILE unavailable" in caplog.text


def test_anti_affinity_through_deploy_needs_the_profile(run_deploy) -> None:
    """⛔ The production failure pinning #47 without #346 produced, measured: three
    placements on one provider. Both bidders fit and tie on room; the one already used
    (CHEAP) must step aside. Without the profile, EMPTIEST falls back to cheapest and the
    spread term never runs, so CHEAP is chosen again."""
    tied = {
        CHEAP: from_provider_status(_status(4000)),
        DEAR: from_provider_status(_status(4000)),
    }
    chosen = run_deploy(
        sdl=ONE_GROUP,
        capacity=tied,
        bids=[_bid(CHEAP, "1"), _bid(DEAR, "9")],
        preferred=[CHEAP, DEAR],
        already_selected=[CHEAP],
    )
    assert chosen == DEAR, "anti-affinity: CHEAP was already selected this round"


# ── gseq-less bids (goal keeper ruling on just-akash#346) ────────────────────────────────


def _bid_without_gseq(provider: str, amount: str) -> dict:
    return {
        "bid": {
            "id": {"provider": provider},
            "price": {"denom": "uakt", "amount": amount},
            "state": "open",
        }
    }


@pytest.fixture
def observations(monkeypatch):
    """Every BidObservation `_select_auction_bid` builds — the auction's view of each bid."""
    seen: list = []
    real = deploy_mod.BidObservation

    def record(*args, **kwargs):
        obs = real(*args, **kwargs)
        seen.append(obs)
        return obs

    monkeypatch.setattr(deploy_mod, "BidObservation", record)
    return seen


def _submitted_profiles(sdl: str):
    from just_akash.provenance import stamp_run
    from just_akash.request_profile import derive_resource_profiles

    return derive_resource_profiles(stamp_run(sdl, "abc123def456")[0])


def _auction(bids, derived):
    from akash_lease_core.auction import PreferredSelection

    return deploy_mod._select_auction_bid(
        bids,
        preferred=[CHEAP, DEAR],
        backup=[],
        collection_window_seconds=10,
        capacity_by_provider=FIT_SPLIT,
        preferred_selection=PreferredSelection.EMPTIEST,
        resource_profiles=derived.profiles,
        placement_group_count=derived.placement_group_count,
    )


def test_reproduction_a_gseqless_bid_no_longer_bypasses_the_fit_check() -> None:
    """The measured reproduction, committed. Before the ruling the second row selected
    akash1cheap with `emptiest_request_profile_unavailable_fell_back_to_cheapest`."""
    derived = _submitted_profiles(ONE_GROUP)
    for label, cheap in (
        ("CHEAP names gseq=1", _bid(CHEAP, "1")),
        ("CHEAP has NO gseq", _bid_without_gseq(CHEAP, "1")),
    ):
        _raw, result = _auction([cheap, _bid(DEAR, "9")], derived)
        assert result.selected is not None, label
        assert result.selected.provider == DEAR, label
        assert result.selection_reason == "emptiest_preferred", label


def test_a_one_group_gseqless_bid_is_observed_as_group_1_and_fit_checked(observations) -> None:
    """(a) One placement group, profile derived: the gseq-less CHEAP bid is observed as
    gseq 1 WITH the group's profile, rejected for insufficient capacity, and DEAR wins."""
    derived = _submitted_profiles(ONE_GROUP)
    assert derived.placement_group_count == 1 and derived.profiles
    _raw, result = _auction([_bid_without_gseq(CHEAP, "1"), _bid(DEAR, "9")], derived)
    cheap_obs = [o for o in observations if o.provider == CHEAP]
    assert len(cheap_obs) == 1
    assert cheap_obs[0].gseq == 1
    assert cheap_obs[0].resource_profile == derived.profiles[1]
    assert [(r.provider, r.reason.value) for r in result.rejected] == [
        (CHEAP, "insufficient_capacity")
    ]
    assert result.selected is not None and result.selected.provider == DEAR
    assert result.selection_reason == "emptiest_preferred"


def test_a_one_group_gseqless_bid_is_NOT_normalized_when_no_profile_was_derived(
    observations,
) -> None:
    """(a, no-profile variant) One group, but the request is unreadable: nothing to fit,
    so there is no reason to assign a group. gseq stays None and the fallback applies."""
    templated = ONE_GROUP.replace("cpu: {units: 1}", "cpu: {units: '{{CPU}}'}")
    assert templated != ONE_GROUP
    derived = _submitted_profiles(templated)
    assert derived.placement_group_count == 1 and not derived.profiles
    _raw, result = _auction([_bid_without_gseq(CHEAP, "1"), _bid(DEAR, "9")], derived)
    cheap_obs = [o for o in observations if o.provider == CHEAP]
    assert len(cheap_obs) == 1
    assert cheap_obs[0].gseq is None
    assert cheap_obs[0].resource_profile is None
    assert result.selection_reason == "emptiest_request_profile_unavailable_fell_back_to_cheapest"


def test_a_multi_group_gseqless_bid_is_never_silently_assigned_group_1(
    observations, run_deploy, caplog
) -> None:
    """(b) ⛔ Two groups: which group a gseq-less bid is for is unknowable. It must stay
    gseq None with NO profile, the whole auction takes the core's explicit fallback, and the
    call site logs how many bids did not say."""
    derived = _submitted_profiles(TWO_GROUPS)
    assert derived.placement_group_count == 2 and len(derived.profiles) == 2
    _raw, result = _auction([_bid_without_gseq(CHEAP, "1"), _bid(DEAR, "9")], derived)
    cheap_obs = [o for o in observations if o.provider == CHEAP]
    assert len(cheap_obs) == 1
    assert cheap_obs[0].gseq is None, "a multi-group gseq-less bid was assigned a group"
    assert cheap_obs[0].resource_profile is None
    assert result.selection_reason == "emptiest_request_profile_unavailable_fell_back_to_cheapest"

    caplog.set_level(logging.INFO)
    run_deploy(
        sdl=TWO_GROUPS,
        capacity=FIT_SPLIT,
        bids=[_bid_without_gseq(CHEAP, "1"), _bid(DEAR, "9")],
    )
    assert "gseqless_bids=1" in caplog.text
