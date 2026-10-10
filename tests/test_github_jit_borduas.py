"""Owned Blazing repository policies keep JIT credentials on the exact org route."""

import io
import json

import pytest

from just_akash import github_jit as jit
from tests.test_github_jit import LABELS, NAME, GitHub

REPOSITORIES = (
    ("Borduas-Holdings/blazing", 1074974924),
    ("Borduas-Holdings/Blazing-Back", 1071436278),
)
ROOT = "/orgs/Borduas-Holdings/actions"


def policy(repository, identity, **options):
    suffix = "refs/heads/main" if options.get("non_reusable_workflow") else "a" * 40
    return jit.JitPolicy(
        "b" * 40,
        37,
        identity,
        repository,
        (repository + "/.github/workflows/owned-build.yml@" + suffix,),
        **options,
    )


def github_for(pilot):
    github = GitHub()
    github.group["selected_workflows"] = list(pilot.workflows)
    github.pages[0]["repositories"] = [
        {"id": pilot.repository_id, "full_name": pilot.repository_name, "private": True}
    ]
    return github


@pytest.mark.parametrize("repository,identity", REPOSITORIES)
def test_owned_repository_mints_one_known_id_on_its_own_org_only(repository, identity):
    pilot = policy(repository, identity)
    github = github_for(pilot)
    handoff = jit.mint_jit(pilot, NAME, LABELS, "installation-fixture", request=github)
    assert handoff.runner_id == 789 and handoff.runner_name == NAME
    assert [row[0] for row in github.calls] == ["GET", "GET", "GET", "GET", "POST"]
    assert all(row[1].startswith(ROOT + "/") for row in github.calls)
    assert github.calls[-1][1] == ROOT + "/runners/generate-jitconfig"
    assert handoff.encoded_config not in repr(handoff)


@pytest.mark.parametrize(
    "repository,identity",
    [
        ("Borduas-Holdings/other", 1074974924),
        ("Borduas-Holdings/blazing", 1071436278),
        ("Borduas-Holdings/Blazing-Back", 1074974924),
        ("Borduas-Holdings/blazing", True),
        ("borduas-holdings/blazing", 1074974924),
        ("Borduas-Holdings/blazing/fork", 1074974924),
        ("unrelated/blazing", 1074974924),
    ],
)
def test_policy_rejects_foreign_repository_or_wrong_owned_identity(repository, identity):
    with pytest.raises(jit.JitHold):
        policy(repository, identity)


def test_owned_group_still_requires_one_exact_private_repository_before_mint():
    pilot = policy(*REPOSITORIES[0])
    github = github_for(pilot)
    github.pages[0]["repositories"][0]["private"] = False
    with pytest.raises(jit.JitHold):
        jit.mint_jit(pilot, NAME, LABELS, "installation-fixture", request=github)
    assert all(row[0] == "GET" for row in github.calls)


def test_owned_nonreusable_source_mismatch_refuses_before_every_request():
    pilot = policy(*REPOSITORIES[1], non_reusable_workflow=True, source_workflow_revision="c" * 40)
    github = github_for(pilot)
    with pytest.raises(jit.JitHold):
        jit.mint_jit(
            pilot,
            NAME,
            LABELS,
            "installation-fixture",
            request=github,
            producer_workflow_revision="d" * 40,
        )
    assert github.calls == []


def test_owned_post_failure_is_unknown_and_never_retried():
    pilot = policy(*REPOSITORIES[1])
    github = github_for(pilot)
    github.fail_post = True
    with pytest.raises(jit.JitMintUnknown):
        jit.mint_jit(pilot, NAME, LABELS, "installation-fixture", request=github)
    assert sum(row[0] == "POST" for row in github.calls) == 1


@pytest.mark.parametrize("method,status", [("GET", 200), ("POST", 201)])
def test_actual_fixed_origin_transport_accepts_owned_org_without_redirect(
    method, status, monkeypatch
):
    seen = []

    class Response(io.BytesIO):
        def __init__(self):
            super().__init__(json.dumps({"observed": True}).encode())
            self.status = status

    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            assert timeout == 20
            return Response()

    monkeypatch.setattr(jit.urllib.request, "build_opener", lambda handler: Opener())
    assert jit.github_request(
        method, ROOT + "/runners/generate-jitconfig", "installation-fixture"
    ) == {"observed": True}
    assert len(seen) == 1 and seen[0].full_url == jit.API + ROOT + "/runners/generate-jitconfig"


@pytest.mark.parametrize(
    "path",
    [
        "/orgs/other/actions/runners",
        ROOT + "/../runners",
        ROOT + "/runners%2foutside",
    ],
)
def test_owned_transport_rejects_foreign_or_escaping_route_before_opener(path, monkeypatch):
    monkeypatch.setattr(
        jit.urllib.request, "build_opener", lambda *a: pytest.fail("opener created")
    )
    with pytest.raises(jit.JitHold):
        jit.github_request("GET", path, "installation-fixture")
