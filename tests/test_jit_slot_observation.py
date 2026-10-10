"""Observe the actual exact one-runner group, never infer delivery from a count."""

import copy

import pytest

from just_akash import github_jit as jit
from just_akash import jit_slot_observation as observe

REPO = "Borduas-Holdings/Blazing-Back"
POLICY = jit.JitPolicy(
    "b" * 40, 37, 1071436278, REPO, (REPO + "/.github/workflows/owned.yml@" + "a" * 40,)
)
NAME = "dfci-blazing-back-123-2-build-0"
LABELS = (NAME, "linux", "akash")
HANDOFF = jit.JitHandoff(789, NAME, 37, "b" * 40, LABELS, "synthetic-config-not-retained")


class GitHub:
    def __init__(self, policy=POLICY):
        self.policy = policy
        self.calls = []
        self.runner = {
            "id": 789,
            "name": NAME,
            "os": "linux",
            "status": "online",
            "busy": False,
            "labels": [
                {"id": 1, "name": NAME, "type": "custom"},
                {"id": 2, "name": "Linux", "type": "read-only"},
                {"id": 3, "name": "akash", "type": "custom"},
                {"id": 4, "name": "self-hosted", "type": "read-only"},
                {"id": 5, "name": "X64", "type": "read-only"},
            ],
        }
        self.group = {
            "id": 37,
            "visibility": "selected",
            "allows_public_repositories": False,
            "restricted_to_workflows": True,
            "selected_workflows": list(policy.workflows),
        }
        self.change = lambda method, path, document, count: None
        self.reads = {}

    def __call__(self, method, path, token, body=None):
        assert method == "GET" and body is None and token == "installation-fixture"
        self.calls.append((method, path))
        self.reads[path] = self.reads.get(path, 0) + 1
        if "/repositories?" in path:
            rows = (
                []
                if path.endswith("page=2")
                else [
                    {
                        "id": self.policy.repository_id,
                        "full_name": self.policy.repository_name,
                        "private": True,
                    }
                ]
            )
            doc = {"total_count": 1, "repositories": rows}
        elif "/runners?" in path:
            doc = {"total_count": 1, "runners": [] if path.endswith("page=2") else [self.runner]}
        elif path.endswith("/runners/789"):
            doc = self.runner
        else:
            doc = self.group
        doc = copy.deepcopy(doc)
        self.change(method, path, doc, self.reads[path])
        return doc


def ready(github, **options):
    return observe.observe_ready_jit_slot(
        POLICY, HANDOFF, "installation-fixture", deadline=100, request=github, **options
    )


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(observe.time, "monotonic", lambda: 10)


def test_exact_returned_id_is_observed_in_two_complete_groups_and_direct_readbacks():
    github = GitHub()
    result = ready(github)
    assert result.runner_id == 789 and result.runner_name == NAME and result.group_id == 37
    assert result.policy_revision == POLICY.revision
    assert result.observed_monotonic == 10
    assert len(github.calls) == 14
    assert sum("/runners?" in path for _, path in github.calls) == 4
    assert sum(path.endswith("/runners/789") for _, path in github.calls) == 2
    assert all(path.startswith("/orgs/Borduas-Holdings/actions/") for _, path in github.calls)
    assert "synthetic-config-not-retained" not in repr(result)


@pytest.mark.parametrize(
    "repository,identity",
    [("Borduas-Holdings/blazing", 1074974924), ("Digital-Frontier-LDA/df-grafana", 123)],
)
def test_other_supported_repository_observations_keep_the_validated_organization_route(
    repository, identity
):
    policy = jit.JitPolicy(
        "b" * 40,
        37,
        identity,
        repository,
        (repository + "/.github/workflows/owned.yml@" + "a" * 40,),
    )
    github = GitHub(policy)
    result = observe.observe_ready_jit_slot(
        policy, HANDOFF, "installation-fixture", deadline=100, request=github
    )
    assert result.runner_id == 789
    assert len(github.calls) == 14
    assert all(path.startswith(policy.api_root + "/") for _, path in github.calls)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", 790),
        ("id", True),
        ("name", "dfci-another"),
        ("os", "macos"),
        ("status", "offline"),
        ("busy", True),
        ("busy", 0),
    ],
)
def test_right_group_and_count_with_wrong_runner_or_unready_state_is_rejected(field, value):
    github = GitHub()
    github.runner[field] = value
    with pytest.raises(jit.JitHold):
        ready(github)


@pytest.mark.parametrize(
    "bad", ["missing", "extra-custom", "duplicate", "wrong-type", "unknown-platform", "id-bool"]
)
def test_requested_labels_are_complete_unique_and_extra_labels_only_known_defaults(bad):
    github = GitHub()
    rows = github.runner["labels"]
    if bad == "missing":
        rows.pop(0)
    elif bad == "extra-custom":
        rows.append({"id": 6, "name": "another-slot", "type": "custom"})
    elif bad == "duplicate":
        rows.append({"id": 6, "name": NAME.upper(), "type": "custom"})
    elif bad == "wrong-type":
        rows[0]["type"] = "untrusted"
    elif bad == "unknown-platform":
        rows.append({"id": 6, "name": "ARM64", "type": "read-only"})
    else:
        rows[0]["id"] = True
    with pytest.raises(jit.JitHold):
        ready(github)


@pytest.mark.parametrize(
    "bad",
    [
        "missing-first",
        "missing-terminal",
        "duplicate-terminal",
        "wrong-total",
        "total-bool",
        "wrong-group-policy",
        "direct-id",
        "drift",
    ],
)
def test_incomplete_population_policy_or_readback_movement_fails_closed(bad):
    github = GitHub()

    def change(method, path, doc, count):
        if "/runners?" in path:
            if bad == "missing-first" and path.endswith("page=1"):
                doc["runners"] = []
            elif bad == "missing-terminal" and path.endswith("page=2"):
                del doc["runners"]
            elif bad == "duplicate-terminal" and path.endswith("page=2"):
                doc["runners"] = [copy.deepcopy(github.runner)]
            elif bad in {"wrong-total", "total-bool"}:
                doc["total_count"] = 2 if bad == "wrong-total" else True
            elif bad == "drift" and count == 2 and path.endswith("page=1"):
                doc["runners"][0]["labels"][0]["id"] = 100
        elif path.endswith("/runners/789") and bad == "direct-id":
            doc["id"] = 790
        elif path.endswith("/runner-groups/37") and bad == "wrong-group-policy":
            doc["visibility"] = "all"

    github.change = change
    with pytest.raises(jit.JitHold):
        ready(github)


@pytest.mark.parametrize("deadline", [10, 9, float("inf"), float("nan"), True, 611, 10**1000])
def test_expired_or_unbounded_deadline_refuses_before_any_get(deadline):
    github = GitHub()
    with pytest.raises(jit.JitHold):
        observe.observe_ready_jit_slot(
            POLICY, HANDOFF, "installation-fixture", deadline=deadline, request=github
        )
    assert github.calls == []


def test_original_deadline_is_rechecked_after_each_response_without_reset(monkeypatch):
    github = GitHub()
    clock = iter([10, 10, 101])
    monkeypatch.setattr(observe.time, "monotonic", lambda: next(clock))
    with pytest.raises(jit.JitHold):
        ready(github)
    assert len(github.calls) == 1


def test_foreign_handoff_policy_is_rejected_before_any_get():
    github = GitHub()
    handoff = jit.JitHandoff(789, NAME, 38, "b" * 40, LABELS, "synthetic-config-not-retained")
    with pytest.raises(jit.JitHold):
        observe.observe_ready_jit_slot(
            POLICY, handoff, "installation-fixture", deadline=100, request=github
        )
    assert github.calls == []


def test_transport_failure_never_echoes_controller_token_or_response():
    def unavailable(*args):
        raise RuntimeError("synthetic-config-not-retained installation-fixture")

    with pytest.raises(jit.JitHold) as error:
        ready(unavailable)
    assert str(error.value) == "exact JIT slot readiness is unverified"


def test_case_ambiguous_declared_labels_refuse_before_any_get():
    github = GitHub()
    handoff = jit.JitHandoff(789, NAME, 37, "b" * 40, (NAME, "Linux", "linux"), "fixture")
    with pytest.raises(jit.JitHold):
        observe.observe_ready_jit_slot(
            POLICY, handoff, "installation-fixture", deadline=100, request=github
        )
    assert github.calls == []


def test_deadline_cannot_expire_between_last_read_and_return(monkeypatch):
    times = iter([10] * 29 + [100])
    monkeypatch.setattr(observe.time, "monotonic", lambda: next(times))
    github = GitHub()
    with pytest.raises(jit.JitHold):
        ready(github)
    assert len(github.calls) == 14
