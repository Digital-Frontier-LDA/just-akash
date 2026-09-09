"""Versioned identity is opt-in; legacy populations cannot become CI by inference."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from just_akash.workload_identity import (
    Identity,
    classify_groups,
    format_identity,
    parse_identity,
    transform_sdl,
)

REGISTER = {
    "borduas": "Borduas-Holdings/blazing",
    "dfci-infra-": "Borduas-Holdings/Blazing-Back",
    "just-akash-runner.": "Digital-Frontier-LDA/just-akash",
}


def identity(prefix="borduas", workload_class="ci-runner", group=1):
    if workload_class.startswith("ci-"):
        return Identity(prefix, REGISTER[prefix], workload_class, group, run=12345, attempt=2)
    return Identity(prefix, REGISTER[prefix], workload_class, group, release="abc123.v1_2")


@pytest.mark.parametrize("prefix", list(REGISTER))
@pytest.mark.parametrize(
    "workload_class", ["ci-runner", "ci-payload", "staging-payload", "prod-payload"]
)
def test_registered_namespace_round_trip(prefix, workload_class):
    original = identity(prefix, workload_class)
    name = format_identity(original, REGISTER)
    stem = prefix if prefix.endswith(("-", ".")) else prefix + "-"
    assert name.startswith(stem + "idv1-class-")
    assert parse_identity(name, REGISTER) == original
    assert classify_groups([name], REGISTER).held is False


def test_staging_expiry_is_explicit_and_preserved():
    original = replace(identity(workload_class="staging-payload"), expires=1800000000)
    assert parse_identity(format_identity(original, REGISTER), REGISTER) == original
    with pytest.raises(ValueError, match="only staging"):
        format_identity(replace(original, workload_class="prod-payload"), REGISTER)


@pytest.mark.parametrize(
    "change",
    [
        {"run": None},
        {"attempt": 0},
        {"group": True},
        {"owner": "someone/else"},
        {"prefix": "unknown"},
        {"release": "hidden"},
        {"expires": 1800000000},
        {"workload_class": "unknown"},
        {"run": -1},
        {"group": "1"},
    ],
)
def test_formatter_rejects_missing_or_conflicting_fields(change):
    with pytest.raises(ValueError):
        format_identity(replace(identity(), **change), REGISTER)


@pytest.mark.parametrize(
    "release", ["", "release-with-hyphens", "abc-expires-123", "CAPS", "../bad", "a" * 65]
)
def test_release_tokens_are_not_silently_normalized(release):
    with pytest.raises(ValueError, match="release token"):
        format_identity(
            replace(identity(workload_class="prod-payload"), release=release), REGISTER
        )


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "borduas-runner-run-1-end",
        "just-akash-runner.run-1-end",
        "borduas-idv2-class-ci-runner-g1-attempt-2-run-12345-end",
        "borduas-idv1-class-ci-runner-g1-attempt-2-run-12345-end-run-9-end",
        "borduas-idv1-class-ci-runner-g1-attempt-02-run-12345-end",
        "borduas-idv1-class-prod-payload-g1-release-abc-expires-1800000000",
        "borduas-idv1-class-staging-payload-g1-release-abc-release-def",
        "borduas-idv1-class-unknown-g1-release-abc",
        "stranger-idv1-class-ci-runner-g1-attempt-2-run-12345-end",
    ],
)
def test_unreadable_legacy_or_invalid_names_are_held(bad):
    assert parse_identity(bad, REGISTER) is None
    assert classify_groups([format_identity(identity(), REGISTER), bad], REGISTER).held


@pytest.mark.parametrize(
    "change",
    [
        {"attempt": 3},
        {"run": 88},
        {"workload_class": "ci-payload"},
        {"prefix": "dfci-infra-", "owner": REGISTER["dfci-infra-"]},
    ],
)
def test_group_lifecycle_disagreement_is_held(change):
    first = identity()
    second = replace(first, group=2, **change)
    assert classify_groups([format_identity(x, REGISTER) for x in (first, second)], REGISTER).held


def test_all_groups_retained_and_duplicates_rejected():
    names = [format_identity(identity(group=n), REGISTER) for n in (1, 2)]
    population = classify_groups(names, REGISTER)
    assert not population.held
    assert [i.group for i in population.identities] == [1, 2]
    assert classify_groups(names + names[:1], REGISTER).held
    assert classify_groups([], REGISTER).held


@pytest.mark.parametrize("register", [{}, {"borduas": "a/b", "borduas-": "c/d"}, {"bad/": "a/b"}])
def test_invalid_registry_is_a_configuration_error(register):
    with pytest.raises(ValueError):
        parse_identity("legacy", register)


def test_real_sdl_placement_and_reference_transformation_is_atomic():
    source = Path(__file__).resolve().parents[1] / "sdl/canary.yaml"
    document = yaml.safe_load(source.read_text())
    before = deepcopy(document)
    placements = document["profiles"]["placement"]
    assert len(placements) > 0
    mapping = {
        old: identity("just-akash-runner.", "staging-payload", group=n)
        for n, old in enumerate(placements, 1)
    }
    transformed = transform_sdl(document, mapping, REGISTER)
    assert document == before
    names = {format_identity(i, REGISTER) for i in mapping.values()}
    assert set(transformed["profiles"]["placement"]) == names
    assert transformed["services"] == before["services"]
    assert transformed["profiles"]["compute"] == before["profiles"]["compute"]
    references = {key for groups in transformed["deployment"].values() for key in groups}
    assert references == names
    assert list(transformed["profiles"]["placement"].values()) == list(placements.values())
    for service, groups in before["deployment"].items():
        assert transformed["deployment"][service] == {
            format_identity(mapping[key], REGISTER): value for key, value in groups.items()
        }
    with pytest.raises(ValueError, match="exactly"):
        transform_sdl(document, {}, REGISTER)
    broken = deepcopy(document)
    service = next(iter(broken["deployment"]))
    broken["deployment"][service]["missing"] = {}
    with pytest.raises(ValueError, match="reference"):
        transform_sdl(broken, mapping, REGISTER)


def test_multiple_sdl_groups_share_lifecycle_but_keep_distinct_ids():
    document = {
        "profiles": {"placement": {"old-a": {"attributes": {"region": "a"}}, "old-b": {}}},
        "deployment": {"api": {"old-a": {"count": 1}}, "worker": {"old-b": {"count": 2}}},
        "services": {"api": {"command": ["old-a"]}},
    }
    mapping = {"old-a": identity(group=1), "old-b": identity(group=2)}
    changed = transform_sdl(document, mapping, REGISTER)
    population = classify_groups(list(changed["profiles"]["placement"]), REGISTER)
    assert len(population.identities) == 2
    assert {i.group for i in population.identities} == {1, 2}
    assert changed["services"] == document["services"]
    with pytest.raises(ValueError, match="disagree"):
        transform_sdl(document, {"old-a": identity(), "old-b": identity()}, REGISTER)


@pytest.mark.parametrize("field,value", [("release", "different"), ("expires", 1800000001)])
def test_release_or_expiry_disagreement_is_held(field, value):
    first = replace(identity(workload_class="staging-payload"), expires=1800000000)
    second = replace(first, group=2, **{field: value})
    assert classify_groups([format_identity(i, REGISTER) for i in (first, second)], REGISTER).held
