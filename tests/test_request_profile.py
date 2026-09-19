"""`derive_resource_profiles`: count × resources, summed PER gseq, or no profile at all.

Each assertion names the observed aggregate, not that a helper ran. The numbers are
chosen so the per-replica shape, the across-group total and the correct per-group total
are all DIFFERENT — a derivation that is wrong in any of those ways cannot pass by
coincidence.
"""

from __future__ import annotations

import pytest
from akash_lease_core import (
    CapacityFit,
    NodeCapacity,
    ProviderCapacity,
    ReplicaProfile,
    ResourceProfile,
)

from just_akash.request_profile import attach_profile, derive_resource_profiles

GI = 1024**3
MI = 1024**2

SMALL = ReplicaProfile(cpu_millicores=500, memory_bytes=512 * MI, storage_bytes=GI)
BIG = ReplicaProfile(
    cpu_millicores=2000,
    memory_bytes=4 * GI,
    storage_bytes=15 * GI,
    gpu_count=1,
)

TWO_GROUPS = """\
version: "2.0"
services:
  web: {image: nginx}
  worker: {image: busybox}
profiles:
  compute:
    small:
      resources:
        cpu: {units: 0.5}
        memory: {size: 512Mi}
        storage: {size: 1Gi}
    big:
      resources:
        cpu: {units: 2}
        memory: {size: 4Gi}
        storage:
          - size: 10Gi
          - size: 5Gi
        gpu: {units: 1}
  placement:
    just-akash-a: {pricing: {small: {denom: uact, amount: 1}}}
    just-akash-b: {pricing: {big: {denom: uact, amount: 1}}}
deployment:
  web:
    just-akash-a: {profile: small, count: 3}
  worker:
    just-akash-b: {profile: big, count: 2}
"""


def test_each_group_is_count_times_its_replica_shape() -> None:
    derived = derive_resource_profiles(TWO_GROUPS)
    assert derived.unavailable_reason is None
    assert derived.profiles == {
        1: ResourceProfile(
            cpu_millicores=1500,
            memory_bytes=3 * 512 * MI,
            storage_bytes=3 * GI,
            replicas=(SMALL,) * 3,
        ),
        2: ResourceProfile(
            cpu_millicores=4000,
            memory_bytes=8 * GI,
            storage_bytes=2 * 15 * GI,
            gpu_count=2,
            replicas=(BIG,) * 2,
        ),
    }


def test_groups_are_not_aggregated_across_gseqs() -> None:
    """⛔ The failure this guards: one deployment-wide total applied to every group."""
    profiles = derive_resource_profiles(TWO_GROUPS).profiles
    assert profiles[1].cpu_millicores == 1500, "group 1 must not carry group 2's CPU"
    assert profiles[1].gpu_count == 0, "group 1 requested no GPU"


def test_two_services_in_one_group_sum_into_that_group() -> None:
    sdl = TWO_GROUPS.replace(
        "  worker:\n    just-akash-b: {profile: big, count: 2}\n",
        "  worker:\n    just-akash-a: {profile: small, count: 1}\n",
    ).replace("    just-akash-b: {pricing: {big: {denom: uact, amount: 1}}}\n", "")
    derived = derive_resource_profiles(sdl)
    assert derived.unavailable_reason is None
    assert derived.profiles == {
        1: ResourceProfile(
            cpu_millicores=2000,
            memory_bytes=4 * 512 * MI,
            storage_bytes=4 * GI,
            replicas=(SMALL,) * 4,
        )
    }


def test_replica_shapes_allow_a_group_to_fit_across_two_nodes() -> None:
    """The aggregate is 4 CPU, but it is two 2-CPU replicas rather than one 4-CPU pod."""
    profile = derive_resource_profiles(TWO_GROUPS).profiles[2]
    provider = ProviderCapacity.from_totals(
        cpu=(4000, 8000),
        memory=(8 * GI, 16 * GI),
        storage=(30 * GI, 60 * GI),
        gpu=(2, 4),
        node_capacities=(
            NodeCapacity(
                cpu_millicores_available=2000,
                memory_bytes_available=4 * GI,
                storage_bytes_available=15 * GI,
                gpu_count_available=1,
            ),
            NodeCapacity(
                cpu_millicores_available=2000,
                memory_bytes_available=4 * GI,
                storage_bytes_available=15 * GI,
                gpu_count_available=1,
            ),
        ),
    )

    assert profile.replicas == (BIG, BIG)
    assert provider.fit(profile) is CapacityFit.FIT


def test_unbounded_replica_population_is_refused_before_allocation() -> None:
    target = "{profile: small, count: 3}"
    assert TWO_GROUPS.count(target) == 1
    derived = derive_resource_profiles(
        TWO_GROUPS.replace(target, "{profile: small, count: 100001}")
    )
    assert derived.profiles == {}
    assert "replica_derivation_limit" in (derived.unavailable_reason or "")


def test_replica_population_limit_is_cumulative_across_groups() -> None:
    first = "{profile: small, count: 3}"
    second = "{profile: big, count: 2}"
    assert TWO_GROUPS.count(first) == TWO_GROUPS.count(second) == 1
    sdl = TWO_GROUPS.replace(first, "{profile: small, count: 60000}").replace(
        second, "{profile: big, count: 60000}"
    )

    derived = derive_resource_profiles(sdl)

    assert derived.profiles == {}
    assert "total_replica_derivation_limit" in (derived.unavailable_reason or "")


@pytest.mark.parametrize(
    ("units", "millicores"),
    [(1, 1000), ("0.1", 100), ("250m", 250), (2.5, 2500)],
)
def test_cpu_units(units: object, millicores: int) -> None:
    sdl = TWO_GROUPS.replace("cpu: {units: 0.5}", f"cpu: {{units: {units!r}}}".replace("'", '"'))
    assert derive_resource_profiles(sdl).profiles[1].cpu_millicores == 3 * millicores


# ── NO PROFILE: every one of these must be a stated reason, never a guessed number ──


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        ("template placeholder", "cpu: {units: 0.5}", "cpu: {units: '{{CPU}}'}"),
        ("unknown size unit", "memory: {size: 512Mi}", "memory: {size: 512Zb}"),
        ("missing count", "{profile: small, count: 3}", "{profile: small}"),
        ("bool count", "{profile: small, count: 3}", "{profile: small, count: true}"),
        ("zero count", "{profile: small, count: 3}", "{profile: small, count: 0}"),
        ("undeclared compute profile", "{profile: small, count: 3}", "{profile: nope, count: 3}"),
        ("fractional millicore", "cpu: {units: 0.5}", "cpu: {units: 0.0005}"),
    ],
)
def test_unreadable_input_yields_no_profile_and_a_reason(label: str, old: str, new: str) -> None:
    assert TWO_GROUPS.count(old) == 1, f"fixture edit for {label!r} did not apply exactly once"
    derived = derive_resource_profiles(TWO_GROUPS.replace(old, new))
    assert derived.profiles == {}, label
    assert derived.unavailable_reason, label
    assert "unavailable" in derived.describe()


def test_deployment_order_disagreeing_with_placement_order_yields_no_profile() -> None:
    """gseq is the placement index (deployment_receipt.py's rule). If deployment references
    the groups in another order, which group is gseq 1 is not something to guess."""
    swapped = TWO_GROUPS.replace(
        "    just-akash-a: {pricing: {small: {denom: uact, amount: 1}}}\n"
        "    just-akash-b: {pricing: {big: {denom: uact, amount: 1}}}\n",
        "    just-akash-b: {pricing: {big: {denom: uact, amount: 1}}}\n"
        "    just-akash-a: {pricing: {small: {denom: uact, amount: 1}}}\n",
    )
    assert swapped != TWO_GROUPS
    derived = derive_resource_profiles(swapped)
    assert derived.profiles == {}
    assert "order" in (derived.unavailable_reason or "")


def test_not_yaml_yields_no_profile() -> None:
    derived = derive_resource_profiles("version: '2.0'\n")
    assert derived.profiles == {} and derived.unavailable_reason


def test_a_bid_with_no_gseq_never_receives_a_profile() -> None:
    """⛔ akash-lease-core#47 rejects a profiled bid with no group as unbound; the
    consumer must not be the one that creates that bid."""
    profiles = derive_resource_profiles(TWO_GROUPS).profiles
    assert attach_profile(profiles, None) is None
    assert attach_profile(profiles, 2) == profiles[2]
    assert attach_profile(profiles, 9) is None
    assert attach_profile({}, 1) is None


def test_the_log_line_names_the_artefact_digest() -> None:
    derived = derive_resource_profiles(TWO_GROUPS)
    assert derived.sdl_sha256[:12] in derived.describe()
    assert "gseq=1:cpu_millicores=1500" in derived.describe()
