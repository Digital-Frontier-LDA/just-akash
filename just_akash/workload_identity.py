"""Opt-in idv1 identity substrate; no producer or reclaimer uses this module yet.

The register is supplied explicitly by the caller. Legacy names and malformed/mixed
populations are held, never inferred to be CI. Parsing identity is not authorization
to close: current run/attempt state or explicit retirement authority remains required.
Release tokens use lowercase letters, digits, dots and underscores (no hyphens), so
field delimiters cannot be smuggled into an opaque release identifier.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass

CLASSES = frozenset({"ci-runner", "ci-payload", "staging-payload", "prod-payload"})
_NUMBER = r"[1-9][0-9]{0,31}"
_RELEASE = r"[a-z0-9][a-z0-9._]{0,63}"


@dataclass(frozen=True)
class Identity:
    prefix: str
    owner: str
    workload_class: str
    group: int
    run: int | None = None
    attempt: int | None = None
    release: str | None = None
    expires: int | None = None

    @property
    def lifecycle(self) -> tuple:
        return (
            self.prefix,
            self.owner,
            self.workload_class,
            self.run,
            self.attempt,
            self.release,
            self.expires,
        )


@dataclass(frozen=True)
class Population:
    identities: tuple[Identity, ...] = ()
    held: bool = True
    reason: str = "unclassified"


def _register(register: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(register, Mapping) or not register:
        raise ValueError("an explicit nonempty ownership register is required")
    stems = {}
    for prefix, owner in register.items():
        if not isinstance(prefix, str) or not re.fullmatch(
            r"[a-z0-9]+(?:[.-][a-z0-9]+)*[.-]?", prefix
        ):
            raise ValueError("invalid registered prefix")
        if not isinstance(owner, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", owner
        ):
            raise ValueError("invalid registered repository")
        stem = prefix if prefix.endswith(("-", ".")) else prefix + "-"
        if stem in stems:
            raise ValueError("ambiguous registered namespace delimiter")
        stems[stem] = prefix
    return stems


def _number(value: object) -> str:
    if type(value) is not int or not re.fullmatch(_NUMBER, str(value)):
        raise ValueError("identity numbers must be positive canonical integers")
    return str(value)


def format_identity(identity: Identity, register: Mapping[str, str]) -> str:
    _register(register)
    if register.get(identity.prefix) != identity.owner:
        raise ValueError("identity does not match registered ownership")
    if identity.workload_class not in CLASSES:
        raise ValueError("unknown workload class")
    stem = identity.prefix if identity.prefix.endswith(("-", ".")) else identity.prefix + "-"
    name = f"{stem}idv1-class-{identity.workload_class}-g{_number(identity.group)}"
    if identity.workload_class.startswith("ci-"):
        if identity.release is not None or identity.expires is not None:
            raise ValueError("CI requires run/attempt and cannot carry release or expiry")
        return f"{name}-attempt-{_number(identity.attempt)}-run-{_number(identity.run)}-end"
    if identity.run is not None or identity.attempt is not None:
        raise ValueError("payload releases cannot carry CI lifecycle fields")
    if not isinstance(identity.release, str) or not re.fullmatch(_RELEASE, identity.release):
        raise ValueError("invalid release token")
    name += f"-release-{identity.release}"
    if identity.expires is not None:
        if identity.workload_class != "staging-payload":
            raise ValueError("only staging may carry explicit expiry")
        name += f"-expires-{_number(identity.expires)}"
    return name


def parse_identity(name: object, register: Mapping[str, str]) -> Identity | None:
    stems = _register(register)
    if not isinstance(name, str):
        return None
    matches = [prefix for stem, prefix in stems.items() if name.startswith(stem + "idv1-class-")]
    if len(matches) != 1:
        return None
    prefix = matches[0]
    stem = prefix if prefix.endswith(("-", ".")) else prefix + "-"
    tail = name[len(stem + "idv1-class-") :]
    match = re.fullmatch(
        rf"(?P<class>ci-runner|ci-payload)-g(?P<group>{_NUMBER})-attempt-(?P<attempt>{_NUMBER})-run-(?P<run>{_NUMBER})-end"
        rf"|(?P<payload>staging-payload|prod-payload)-g(?P<pgroup>{_NUMBER})-release-(?P<release>{_RELEASE})(?:-expires-(?P<expires>{_NUMBER}))?",
        tail,
    )
    if match is None:
        return None
    fields = match.groupdict()
    identity = Identity(
        prefix,
        register[prefix],
        fields["class"] or fields["payload"],
        int(fields["group"] or fields["pgroup"]),
        run=int(fields["run"]) if fields["run"] else None,
        attempt=int(fields["attempt"]) if fields["attempt"] else None,
        release=fields["release"],
        expires=int(fields["expires"]) if fields["expires"] else None,
    )
    try:
        return identity if format_identity(identity, register) == name else None
    except ValueError:
        return None


def classify_groups(names: object, register: Mapping[str, str]) -> Population:
    _register(register)
    if not isinstance(names, (list, tuple)) or not names:
        return Population(reason="missing or unreadable groups")
    parsed = tuple(parse_identity(name, register) for name in names)
    if any(identity is None for identity in parsed):
        return Population(reason="legacy, unknown or malformed identity")
    identities = tuple(identity for identity in parsed if identity is not None)
    if len({identity.lifecycle for identity in identities}) != 1:
        return Population(reason="conflicting lifecycle identities")
    if len({identity.group for identity in identities}) != len(identities):
        return Population(reason="duplicate group identity")
    return Population(
        identities=identities,
        held=False,
        reason="classified; retirement requires separate authority",
    )


def transform_sdl(
    document: dict, identities: Mapping[str, Identity], register: Mapping[str, str]
) -> dict:
    """Return a copy with every placement definition and service reference renamed.

    Accepts a parsed SDL, not a shell template. Caller must identify every placement
    explicitly; partial, unused, unknown or conflicting groups are rejected atomically.
    Service names, resource profiles, provider attributes and embedded scripts stay intact.
    """
    _register(register)
    try:
        placements = document["profiles"]["placement"]
        services = document["deployment"]
    except (KeyError, TypeError) as exc:
        raise ValueError("missing SDL placement structure") from exc
    if not isinstance(placements, dict) or not placements or set(placements) != set(identities):
        raise ValueError("identity mapping must cover exactly every placement")
    if not isinstance(services, dict) or not services:
        raise ValueError("missing SDL deployment references")
    names = {old: format_identity(identity, register) for old, identity in identities.items()}
    if classify_groups(list(names.values()), register).held:
        raise ValueError("SDL groups disagree or repeat an identity")
    referenced = set()
    for groups in services.values():
        if not isinstance(groups, dict) or not groups or not set(groups) <= set(placements):
            raise ValueError("unknown or unreadable SDL deployment placement reference")
        referenced.update(groups)
    if referenced != set(placements):
        raise ValueError("unused SDL placement definition")
    result = deepcopy(document)
    result["profiles"]["placement"] = {
        names[key]: value for key, value in result["profiles"]["placement"].items()
    }
    result["deployment"] = {
        service: {names[key]: value for key, value in groups.items()}
        for service, groups in result["deployment"].items()
    }
    return result
