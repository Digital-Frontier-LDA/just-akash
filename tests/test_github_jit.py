"""Effect-test the exact policy verification -> JIT mutation boundary."""

import base64
import copy
import io
import json
import urllib.error
from dataclasses import replace

import pytest

from just_akash import github_jit as jit

REPO = "Digital-Frontier-LDA/df-grafana"
WORKFLOW = REPO + "/.github/workflows/ci-observability.yml@" + "a" * 40
POLICY = jit.JitPolicy("b" * 40, 37, 123, REPO, (WORKFLOW,))
NAME = "dfci-grafana-123-1-rules-0"
LABELS = (NAME, "linux", "akash")
CONFIG = base64.b64encode(
    json.dumps(
        {
            ".runner": base64.b64encode(b'{"agentId":789}').decode(),
            ".credentials": base64.b64encode(b"one-job-fixture").decode(),
        }
    ).encode()
).decode()


class GitHub:
    def __init__(self):
        self.calls = []
        self.group = {
            "id": 37,
            "visibility": "selected",
            "allows_public_repositories": False,
            "restricted_to_workflows": True,
            "selected_workflows": [WORKFLOW],
        }
        self.pages = [
            {"total_count": 1, "repositories": [{"id": 123, "full_name": REPO, "private": True}]},
            {"total_count": 1, "repositories": []},
        ]
        self.result = {"runner": {"id": 789, "name": NAME}, "encoded_jit_config": CONFIG}
        self.group_reads = 0
        self.change_group = False
        self.fail_post = False

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path, token, body))
        if method == "POST":
            if self.fail_post:
                raise jit.JitMintUnknown("fixture")
            return copy.deepcopy(self.result)
        if "/repositories?" in path:
            page = int(path.rsplit("=", 1)[1])
            return copy.deepcopy(self.pages[min(page - 1, len(self.pages) - 1)])
        self.group_reads += 1
        result = copy.deepcopy(self.group)
        if self.change_group and self.group_reads == 2:
            result["visibility"] = "all"
        return result


def mint(github):
    return jit.mint_jit(POLICY, NAME, LABELS, "installation-fixture", request=github)


def branch_policy():
    return jit.JitPolicy(
        "b" * 40,
        37,
        123,
        REPO,
        (REPO + "/.github/workflows/ci-observability.yml@refs/heads/main",),
        non_reusable_workflow=True,
        source_workflow_revision="a" * 40,
    )


def test_non_reusable_group_branch_and_separate_source_binding_at_actual_mint():
    github = GitHub()
    policy = branch_policy()
    github.group["selected_workflows"] = list(policy.workflows)
    handoff = jit.mint_jit(
        policy,
        NAME,
        LABELS,
        "installation-fixture",
        request=github,
        producer_workflow_revision="a" * 40,
    )
    assert handoff.runner_id == 789
    assert sum(call[0] == "POST" for call in github.calls) == 1


def master_policy():
    return replace(
        branch_policy(),
        workflows=(REPO + "/.github/workflows/ci-observability.yml@refs/heads/master",),
        source_workflow_branch="master",
    )


def test_master_default_branch_mints_with_exact_group_and_source():
    github = GitHub()
    policy = master_policy()
    github.group["selected_workflows"] = list(policy.workflows)
    handoff = jit.mint_jit(
        policy,
        NAME,
        LABELS,
        "installation-fixture",
        request=github,
        producer_workflow_revision="a" * 40,
    )
    assert handoff.runner_id == 789
    assert sum(call[0] == "POST" for call in github.calls) == 1


@pytest.mark.parametrize(
    "branch", ["feature", "refs/heads/master", "", None, False, "master\n", "deploy-env\n"]
)
def test_only_reviewed_default_branch_names_are_accepted(branch):
    with pytest.raises(jit.JitHold):
        replace(master_policy(), source_workflow_branch=branch)


def deploy_env_policy():
    return replace(
        branch_policy(),
        workflows=(REPO + "/.github/workflows/ci-observability.yml@refs/heads/deploy-env",),
        source_workflow_branch="deploy-env",
    )


def test_deploy_env_binding_mints_only_after_exact_group_and_source_checks():
    github = GitHub()
    policy = deploy_env_policy()
    github.group["selected_workflows"] = list(policy.workflows)
    handoff = jit.mint_jit(
        policy,
        NAME,
        LABELS,
        "installation-fixture",
        request=github,
        producer_workflow_revision="a" * 40,
    )
    assert handoff.runner_id == 789
    assert sum(call[0] == "POST" for call in github.calls) == 1


@pytest.mark.parametrize("selected", [branch_policy(), master_policy()])
def test_other_branch_groups_cannot_receive_deploy_env_qualified_runner(selected):
    github = GitHub()
    github.group["selected_workflows"] = list(selected.workflows)
    with pytest.raises(jit.JitHold, match="runner group differs"):
        jit.mint_jit(
            deploy_env_policy(),
            NAME,
            LABELS,
            "installation-fixture",
            request=github,
            producer_workflow_revision="a" * 40,
        )
    assert not any(call[0] == "POST" for call in github.calls)


def test_deploy_env_branch_does_not_replace_approved_producer_source():
    github = GitHub()
    github.group["selected_workflows"] = list(deploy_env_policy().workflows)
    with pytest.raises(jit.JitHold):
        jit.mint_jit(
            deploy_env_policy(),
            NAME,
            LABELS,
            "installation-fixture",
            request=github,
            producer_workflow_revision="c" * 40,
        )
    assert github.calls == []


def test_deploy_env_cannot_broaden_reusable_workflow_admission():
    with pytest.raises(jit.JitHold):
        replace(POLICY, source_workflow_branch="deploy-env")


def test_main_group_cannot_receive_a_master_qualified_runner():
    github = GitHub()
    github.group["selected_workflows"] = list(branch_policy().workflows)
    with pytest.raises(jit.JitHold, match="runner group differs"):
        jit.mint_jit(
            master_policy(),
            NAME,
            LABELS,
            "installation-fixture",
            request=github,
            producer_workflow_revision="a" * 40,
        )
    assert not any(call[0] == "POST" for call in github.calls)


def test_branch_field_cannot_broaden_reusable_workflow_admission():
    with pytest.raises(jit.JitHold):
        replace(POLICY, source_workflow_branch="master")


def test_master_group_still_requires_exact_approved_source():
    github = GitHub()
    github.group["selected_workflows"] = list(master_policy().workflows)
    with pytest.raises(jit.JitHold):
        jit.mint_jit(
            master_policy(),
            NAME,
            LABELS,
            "installation-fixture",
            request=github,
            producer_workflow_revision="c" * 40,
        )
    assert github.calls == []


@pytest.mark.parametrize("revision", [None, "c" * 40, "main", False])
def test_branch_group_does_not_replace_approved_producer_source(revision):
    github = GitHub()
    with pytest.raises(jit.JitHold):
        jit.mint_jit(
            branch_policy(),
            NAME,
            LABELS,
            "installation-fixture",
            request=github,
            producer_workflow_revision=revision,
        )
    assert not github.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("workflows", (WORKFLOW,)),
        ("workflows", (REPO + "/.github/workflows/ci.yml@refs/heads/other",)),
        ("source_workflow_revision", None),
        ("source_workflow_revision", "main"),
        ("non_reusable_workflow", 1),
    ],
)
def test_non_reusable_policy_rejects_unsupported_or_missing_bindings(field, value):
    with pytest.raises(jit.JitHold):
        replace(branch_policy(), **{field: value})


def test_old_sha_group_is_held_for_a_non_reusable_branch_policy():
    github = GitHub()
    with pytest.raises(jit.JitHold):
        jit.mint_jit(
            branch_policy(),
            NAME,
            LABELS,
            "installation-fixture",
            request=github,
            producer_workflow_revision="a" * 40,
        )
    assert not any(call[0] == "POST" for call in github.calls)


def test_exact_verified_group_bound_at_real_mutation_once_and_secret_repr_redacted():
    github = GitHub()
    handoff = mint(github)
    assert handoff.runner_id == 789
    assert handoff.group_id == POLICY.group_id
    assert handoff.policy_revision == POLICY.revision
    assert handoff.encoded_config == CONFIG
    assert CONFIG not in repr(handoff)
    assert github.calls[-1] == (
        "POST",
        jit._ROOT + "/runners/generate-jitconfig",
        "installation-fixture",
        {"name": NAME, "runner_group_id": 37, "labels": list(LABELS), "work_folder": "_work"},
    )
    assert [call[0] for call in github.calls] == ["GET"] * 4 + ["POST"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", 38),
        ("id", True),
        ("visibility", "all"),
        ("visibility", "private"),
        ("allows_public_repositories", True),
        ("allows_public_repositories", 0),
        ("restricted_to_workflows", False),
        ("restricted_to_workflows", 1),
        ("selected_workflows", []),
        ("selected_workflows", [WORKFLOW, WORKFLOW]),
        ("selected_workflows", [WORKFLOW.replace("a" * 40, "refs/heads/main")]),
        ("selected_workflows", None),
        ("selected_workflows", [False]),
    ],
)
def test_policy_mismatch_removes_jit_create_effect(field, value):
    github = GitHub()
    github.group[field] = value
    with pytest.raises(jit.JitHold):
        mint(github)
    assert not any(call[0] == "POST" for call in github.calls)


@pytest.mark.parametrize(
    "pages",
    [
        [{"total_count": 0, "repositories": []}],
        [{"total_count": 1, "repositories": []}],
        [{"total_count": True, "repositories": []}],
        [{"total_count": -1, "repositories": []}],
        [{"total_count": 1001, "repositories": []}],
        [{"total_count": 1, "repositories": None}],
        [{"total_count": 1, "repositories": [{}]}],
        [{"total_count": 1, "repositories": [None]}],
        [{"total_count": 1, "repositories": [{"id": True}]}],
        [{"total_count": 1, "repositories": [{"id": 123, "full_name": REPO, "private": False}]}],
        [{"total_count": 1, "repositories": [{"id": 123, "full_name": None, "private": True}]}],
        [{"total_count": 0, "repositories": [{"id": 123, "full_name": REPO, "private": True}]}],
        [
            {"total_count": 1, "repositories": [{"id": 124, "full_name": REPO, "private": True}]},
            {"total_count": 1, "repositories": []},
        ],
        [
            {
                "total_count": 1,
                "repositories": [{"id": 123, "full_name": REPO + "-other", "private": True}],
            },
            {"total_count": 1, "repositories": []},
        ],
    ],
)
def test_incomplete_or_foreign_population_cannot_mint(pages):
    github = GitHub()
    github.pages = pages
    with pytest.raises(jit.JitHold):
        mint(github)
    assert not any(call[0] == "POST" for call in github.calls)


def test_duplicate_page_and_changing_total_are_not_a_complete_census():
    for changed in (False, True):
        github = GitHub()
        github.pages[1] = copy.deepcopy(github.pages[0])
        if changed:
            github.pages[1]["total_count"] = 2
        with pytest.raises(jit.JitHold):
            mint(github)
        assert not any(call[0] == "POST" for call in github.calls)


def test_policy_change_during_repository_observation_holds_before_post():
    github = GitHub()
    github.change_group = True
    with pytest.raises(jit.JitHold, match="changed"):
        mint(github)
    assert github.calls[-1][0] == "GET"


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", "main"),
        ("revision", None),
        ("group_id", True),
        ("group_id", 0),
        ("repository_id", -1),
        ("repository_name", "Other/repo"),
        ("repository_name", None),
        ("workflows", []),
        ("workflows", ()),
        ("workflows", (False,)),
        ("workflows", (WORKFLOW, WORKFLOW)),
        ("workflows", (WORKFLOW.replace("a" * 40, "refs/heads/main"),)),
        ("workflows", (WORKFLOW.replace("df-grafana", "other"),)),
    ],
)
def test_noncanonical_policy_data_holds(field, value):
    with pytest.raises(jit.JitHold):
        replace(POLICY, **{field: value})


@pytest.mark.parametrize(
    "name,labels,token",
    [
        ("", LABELS, "fixture"),
        ("sentinel-host", LABELS, "fixture"),
        (None, LABELS, "fixture"),
        (NAME, (), "fixture"),
        (NAME, list(LABELS), "fixture"),
        (NAME, (NAME, NAME), "fixture"),
        (NAME, ("other",), "fixture"),
        (NAME, (NAME, "bad\nlabel"), "fixture"),
        (NAME, (NAME, True), "fixture"),
        (NAME, LABELS, ""),
        (NAME, LABELS, "bad\ntoken"),
    ],
)
def test_invalid_delivery_slot_never_queries_or_mints(name, labels, token):
    github = GitHub()
    with pytest.raises(jit.JitHold):
        jit.mint_jit(POLICY, name, labels, token, request=github)
    assert not github.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("runner", None),
        ("runner", {"id": True, "name": NAME}),
        ("runner", {"id": 789, "name": "foreign"}),
        ("encoded_jit_config", ""),
        ("encoded_jit_config", "not base64"),
        ("encoded_jit_config", False),
        ("encoded_jit_config", base64.b64encode(b"{}").decode()),
        ("encoded_jit_config", base64.b64encode(b"[]").decode()),
        ("encoded_jit_config", base64.b64encode(b'{"x":false}').decode()),
        ("encoded_jit_config", base64.b64encode(b'{"x":""}').decode()),
        ("encoded_jit_config", base64.b64encode(b'{"x":"a","x":"b"}').decode()),
    ],
)
def test_response_corruption_is_unknown_not_safe_to_retry(field, value):
    github = GitHub()
    github.result[field] = value
    with pytest.raises(jit.JitMintUnknown) as error:
        mint(github)
    assert sum(call[0] == "POST" for call in github.calls) == 1
    assert "fixture" not in str(error.value)
    assert CONFIG not in str(error.value)


def test_unknown_post_is_not_retried_or_presented_as_a_handoff():
    github = GitHub()
    github.fail_post = True
    with pytest.raises(jit.JitMintUnknown):
        mint(github)
    assert sum(call[0] == "POST" for call in github.calls) == 1


@pytest.mark.parametrize(
    "method,path,token",
    [
        ("GET", "https://evil.example", "fixture"),
        ("GET", "/app", "fixture"),
        ("DELETE", jit._ROOT + "/runners/1", "fixture"),
        ("GET", jit._ROOT + "/../runners", "fixture"),
        ("GET", jit._ROOT + "/runners#fragment", "fixture"),
        ("GET", jit._ROOT + "/runners%0a", "fixture"),
        ("GET", jit._ROOT + "/runners", "bad\r\ntoken"),
        ("GET", jit._ROOT + "/runners", ""),
    ],
)
def test_invalid_transport_arguments_never_reach_network(method, path, token, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid request reached transport")

    monkeypatch.setattr(jit.urllib.request, "build_opener", forbidden)
    with pytest.raises(jit.JitHold):
        jit.github_request(method, path, token)


class Response(io.BytesIO):
    status = 200


def test_fixed_origin_no_redirect_and_exact_mint_transport_body(monkeypatch):
    observed = []

    class Opener:
        def open(self, req, timeout):
            observed.append((req, timeout))
            result = Response(b'{"ok":true}')
            result.status = 201
            return result

    def build(handler):
        assert handler.redirect_request(None, None, 302, "", {}, "https://evil.example") is None
        return Opener()

    monkeypatch.setattr(jit.urllib.request, "build_opener", build)
    assert jit.github_request(
        "POST", jit._ROOT + "/runners/generate-jitconfig", "fixture", {"runner_group_id": 37}
    ) == {"ok": True}
    req, timeout = observed[0]
    assert req.full_url == jit.API + jit._ROOT + "/runners/generate-jitconfig"
    assert json.loads(req.data) == {"runner_group_id": 37}
    assert timeout == 20


@pytest.mark.parametrize(
    "raw,status",
    [
        (b"{}", 302),
        (b"[]", 200),
        (b'{"x":1,"x":2}', 200),
        (b'{"x":NaN}', 200),
        (b"not-json", 200),
        (b"x" * (jit._MAX_BYTES + 1), 200),
    ],
)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_bad_transport_response_is_redacted_and_post_unknown(raw, status, method, monkeypatch):
    class Opener:
        def open(self, req, timeout):
            response = Response(raw)
            response.status = 201 if method == "POST" and status == 200 else status
            return response

    monkeypatch.setattr(jit.urllib.request, "build_opener", lambda _: Opener())
    error_type = jit.JitMintUnknown if method == "POST" else jit.JitHold
    with pytest.raises(error_type) as error:
        jit.github_request(method, jit._ROOT + "/runners", "installation-fixture")
    assert "installation-fixture" not in str(error.value)
    assert "not-json" not in str(error.value)


@pytest.mark.parametrize("failure", [TimeoutError(), urllib.error.URLError("secret-body")])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_transport_failure_is_bounded_redacted_and_not_retried(failure, method, monkeypatch):
    calls = []

    class Opener:
        def open(self, req, timeout):
            calls.append(req)
            raise failure

    monkeypatch.setattr(jit.urllib.request, "build_opener", lambda _: Opener())
    error_type = jit.JitMintUnknown if method == "POST" else jit.JitHold
    with pytest.raises(error_type) as error:
        jit.github_request(method, jit._ROOT + "/runners", "fixture")
    assert len(calls) == 1
    assert "secret-body" not in str(error.value)


def test_readonly_probe_is_not_a_mint_receipt():
    github = GitHub()
    assert jit.verify_group_policy(POLICY, "fixture", request=github) is None
    assert all(call[0] == "GET" for call in github.calls)
    github.group["visibility"] = "all"
    with pytest.raises(jit.JitHold):
        mint(github)
    assert all(call[0] == "GET" for call in github.calls)


@pytest.mark.parametrize("policy,token", [(None, "fixture"), (POLICY, ""), (POLICY, "bad\ntoken")])
def test_invalid_readonly_probe_never_requests(policy, token):
    github = GitHub()
    with pytest.raises(jit.JitHold):
        jit.verify_group_policy(policy, token, request=github)
    assert not github.calls


def test_noncanonical_base64_padding_is_unknown():
    github = GitHub()
    github.result["encoded_jit_config"] = "eyJ4IjoiYWIifR=="
    with pytest.raises(jit.JitMintUnknown):
        mint(github)
    assert sum(call[0] == "POST" for call in github.calls) == 1


@pytest.mark.parametrize(
    "filename,content",
    [
        (".runner", "not base64!"),
        (".runner", "Zh=="),
        ("../.runner", "Zg=="),
        ("/tmp/.runner", "Zg=="),
        (".", "Zg=="),
        ("..", "Zg=="),
        ("path/.runner", "Zg=="),
    ],
)
def test_nested_jit_file_corruption_or_root_escape_is_unknown(filename, content):
    github = GitHub()
    github.result["encoded_jit_config"] = base64.b64encode(
        json.dumps({filename: content}).encode()
    ).decode()
    with pytest.raises(jit.JitMintUnknown):
        mint(github)
    assert sum(call[0] == "POST" for call in github.calls) == 1
