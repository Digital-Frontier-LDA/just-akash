"""Aggregate resource request per Akash group, derived from the SUBMITTED SDL.

akash-lease-core#47 made `PreferredSelection.EMPTIEST` request-aware and #49 made its
per-node fit proof exact: a bid is ranked on whether the provider can place every
replica in the group. The core only knows that population if the consumer supplies it
as `BidObservation.resource_profile`. Without one, EMPTIEST degrades to cheapest and
says so (`emptiest_request_profile_unavailable_fell_back_to_cheapest`).

⛔ MEASURED, and the reason this module exists: pinning that core WITHOUT supplying a
profile turned every `--select emptiest` deploy into cheapest, and collapsed the
anti-affinity spread onto one provider — three sequential placements chose
`['akash1lisbon', 'akash1lisbon', 'akash1lisbon']`.

⛔ RETAIN BOTH `count × resources` TOTALS AND EVERY REPLICA SHAPE PER GROUP. Totals
prove aggregate capacity while the population proves per-node placement. A group of
four 2-CPU replicas asks for 8 CPUs, but it may fit across four nodes even when no one
node has 8 CPUs.

⛔ PER GROUP, NOT PER DEPLOYMENT. Akash auctions each group separately and a bid names
its `gseq`. Summing across groups would reject a provider that can host the group it
actually bid on.

gseq ORDER uses the rule `deployment_receipt.py` already enforces: gseq is the 1-based
index of `profiles.placement` names in document order, and the `deployment:` section
must first-reference groups in that SAME order. An SDL that disagrees is not guessed at.

⚠ NO PROFILE IS A RESULT, NOT A DEFAULT. Anything this cannot read exactly — a
template placeholder, an unknown unit, a missing count, disagreeing group order —
yields no profiles and a stated reason. A guessed or zeroed profile would let the fit
check pass or reject providers on a number nobody requested.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import yaml
from akash_lease_core import ReplicaProfile, ResourceProfile

_SIZE_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]*)$")
_SIZE_UNITS = {
    "": 1,
    "k": 1000,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "E": 1000**6,
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Ei": 1024**6,
}

# Building one exact shape per replica is required by the core's bin-packing proof.
# Refuse an adversarial SDL before allocating an unbounded tuple; the shared solver
# itself explores at most 100,000 canonical states, so a larger input cannot earn a
# positive placement verdict through this synchronous adapter.
MAX_REPLICA_POPULATION = 100_000


class _Unreadable(ValueError):
    """A value this module refuses to interpret."""


@dataclass(frozen=True)
class DerivedProfiles:
    """`profiles` maps gseq → aggregate request. Empty iff `unavailable_reason` is set."""

    profiles: dict[int, ResourceProfile] = field(default_factory=dict)
    unavailable_reason: str | None = None
    sdl_sha256: str = ""
    # Declared `profiles.placement` groups, recorded as soon as that section parses —
    # even when derivation then fails — so "one group" and "a profile was derived" stay
    # two separate facts. None = the placement section itself was unreadable.
    placement_group_count: int | None = None

    def describe(self, gseqless_bids: int | None = None) -> str:
        """One log line naming the artefact the profile was derived from, and how many
        bids in this round did not say which group they are for."""
        source = f"sdl_sha256={self.sdl_sha256[:12]}"
        if gseqless_bids is not None:
            source += f" gseqless_bids={gseqless_bids}"
        if self.unavailable_reason is not None:
            return f"REQUEST_PROFILE unavailable reason={self.unavailable_reason} {source}"
        groups = " ".join(
            f"gseq={gseq}:cpu_millicores={p.cpu_millicores},memory_bytes={p.memory_bytes},"
            f"storage_bytes={p.storage_bytes},gpu_count={p.gpu_count}"
            for gseq, p in sorted(self.profiles.items())
        )
        return f"REQUEST_PROFILE {groups} {source}"


def _mapping(value: object, where: str) -> dict:
    if not isinstance(value, dict):
        raise _Unreadable(f"{where} is not a mapping")
    return value


def _cpu_millicores(value: object) -> int:
    if isinstance(value, bool):
        raise _Unreadable("cpu units is a bool")
    text = str(value).strip() if isinstance(value, (int, float, str)) else None
    if not text:
        raise _Unreadable("cpu units missing or not scalar")
    try:
        millis = Decimal(text[:-1]) if text.endswith("m") else Decimal(text) * 1000
    except InvalidOperation as exc:
        raise _Unreadable(f"cpu units {text!r} is not a number") from exc
    if millis < 0 or millis != millis.to_integral_value():
        raise _Unreadable(f"cpu units {text!r} is not a whole number of millicores")
    return int(millis)


def _size_bytes(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _Unreadable(f"{where} size is not a scalar size")
    match = _SIZE_RE.match(str(value).strip())
    if match is None or match.group("unit") not in _SIZE_UNITS:
        raise _Unreadable(f"{where} size {value!r} has an unrecognised unit")
    total = Decimal(match.group("num")) * _SIZE_UNITS[match.group("unit")]
    if total != total.to_integral_value():
        raise _Unreadable(f"{where} size {value!r} is not a whole number of bytes")
    return int(total)


def _storage_bytes(value: object) -> int:
    if value is None:
        return 0
    volumes = value if isinstance(value, list) else [value]
    total = 0
    for volume in volumes:
        total += _size_bytes(_mapping(volume, "storage volume").get("size"), "storage")
    return total


def _gpu_count(resources: dict) -> int:
    gpu = resources.get("gpu")
    if gpu is None:
        return 0
    units = _mapping(gpu, "gpu").get("units", 0)
    if isinstance(units, bool) or not isinstance(units, (int, str)) or not str(units).isdigit():
        raise _Unreadable(f"gpu units {units!r} is not a whole number")
    return int(units)


def _replica_request(compute: dict, profile_name: object) -> tuple[int, int, int, int]:
    if not isinstance(profile_name, str) or profile_name not in compute:
        raise _Unreadable(f"compute profile {profile_name!r} is not declared")
    resources = _mapping(
        _mapping(compute[profile_name], "compute profile").get("resources"), "resources"
    )
    return (
        _cpu_millicores(_mapping(resources.get("cpu"), "cpu").get("units")),
        _size_bytes(_mapping(resources.get("memory"), "memory").get("size"), "memory"),
        _storage_bytes(resources.get("storage")),
        _gpu_count(resources),
    )


def derive_resource_profiles(sdl_text: str) -> DerivedProfiles:
    """Derive one aggregate `ResourceProfile` per gseq from the exact SDL text submitted."""
    digest = hashlib.sha256(sdl_text.encode("utf-8")).hexdigest()
    placement_group_count: int | None = None
    try:
        document = _mapping(yaml.safe_load(sdl_text), "SDL")
        profiles_section = _mapping(document.get("profiles"), "profiles")
        compute = _mapping(profiles_section.get("compute"), "profiles.compute")
        placement_names = list(_mapping(profiles_section.get("placement"), "profiles.placement"))
        placement_group_count = len(placement_names)
        deployment = _mapping(document.get("deployment"), "deployment")

        totals: dict[str, list[int]] = {}
        replicas: dict[str, list[ReplicaProfile]] = {}
        total_population = 0
        for service, groups in deployment.items():
            for group_name, spec in _mapping(groups, f"deployment.{service}").items():
                spec = _mapping(spec, f"deployment.{service}.{group_name}")
                count = spec.get("count")
                if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                    raise _Unreadable(
                        f"deployment.{service}.{group_name}.count is not a positive integer"
                    )
                replica = _replica_request(compute, spec.get("profile"))
                running = totals.setdefault(group_name, [0, 0, 0, 0])
                for index, amount in enumerate(replica):
                    running[index] += count * amount
                if total_population + count > MAX_REPLICA_POPULATION:
                    raise _Unreadable(
                        f"deployment exceeds the {MAX_REPLICA_POPULATION} total replica "
                        "derivation limit"
                    )
                total_population += count
                population = replicas.setdefault(group_name, [])
                shape = ReplicaProfile(
                    cpu_millicores=replica[0],
                    memory_bytes=replica[1],
                    storage_bytes=replica[2],
                    gpu_count=replica[3],
                )
                population.extend([shape] * count)

        if list(totals) != placement_names:
            raise _Unreadable(
                "deployment groups do not reference profiles.placement in the same order, "
                "so gseq cannot be assigned"
            )
        derived: dict[int, ResourceProfile] = {}
        for gseq, name in enumerate(placement_names, start=1):
            cpu, memory, storage, gpu = totals[name]
            derived[gseq] = ResourceProfile(
                cpu_millicores=cpu,
                memory_bytes=memory,
                storage_bytes=storage,
                gpu_count=gpu,
                replicas=tuple(replicas[name]),
            )
    except (_Unreadable, ValueError, yaml.YAMLError, TypeError) as exc:
        reason = re.sub(r"\s+", "_", str(exc).strip())[:120] or type(exc).__name__
        return DerivedProfiles(
            unavailable_reason=reason,
            sdl_sha256=digest,
            placement_group_count=placement_group_count,
        )
    return DerivedProfiles(
        profiles=derived, sdl_sha256=digest, placement_group_count=placement_group_count
    )


def attach_profile(profiles: dict[int, ResourceProfile] | None, gseq: int | None) -> Any:
    """The profile for a bid's group, or None. Never a profile for a bid with no gseq:
    the core rejects that as `resource_profile_group_unbound`."""
    if not profiles or gseq is None:
        return None
    return profiles.get(gseq)


def observed_gseq(
    extracted: int | None,
    profiles: dict[int, ResourceProfile] | None,
    placement_group_count: int | None,
) -> int | None:
    """The group a bid is OBSERVED for in the auction.

    A bid that names its gseq keeps it. A bid that does not is observed as gseq 1 ONLY when
    the submitted SDL declares exactly one placement group AND a profile was derived for
    it. With one group there is no other group it can be for, and the lease path already
    uses 1 for that bid (`_extract_gseq(...) or 1`), so auction and lease agree.

    ⛔ Measured without this: in a one-group deployment a gseq-less bid carried no profile,
    skipped the fit check, and won — while also downgrading the WHOLE auction to cheapest
    (`profiles_complete` false in akash-lease-core `auction.py:844`).

    ⛔ Two or more groups: still None. Which group a gseq-less bid is for is not knowable,
    so it gets no profile and the core's explicit fallback applies. This is not
    `_extract_gseq` guessing 1 (api.py:760): extraction still returns None, and the
    caller applies its default only where the SDL proves there is a single group.
    """
    if extracted is not None:
        return extracted
    if placement_group_count == 1 and profiles:
        return 1
    return None
