"""The legacy Default group must positively restrict credential-bearing jobs."""

import copy
import io
import os
import subprocess
import traceback
import urllib.error
from http.client import HTTPMessage
from unittest.mock import Mock

import pytest

from just_akash import runner_image as image

ROOT = "/orgs/Borduas-Holdings/actions/runner-groups"
GROUP = {
    "id": 1,
    "default": True,
    "visibility": "selected",
    "allows_public_repositories": False,
    "restricted_to_workflows": False,
    "selected_workflows": [],
}
REPO = {"id": 1074974924, "full_name": "Borduas-Holdings/blazing", "private": True}


def policy(monkeypatch, *, groups=None, repos=None, mutate=None):
    groups = copy.deepcopy(groups if groups is not None else [GROUP])
    repos = copy.deepcopy(repos if repos is not None else [REPO])
    calls = []

    def request(path):
        calls.append(path)
        if "/repositories?" in path:
            doc = {
                "total_count": len(repos),
                "repositories": [] if path.endswith("page=2") else repos,
            }
        elif "?per_page=" in path:
            doc = {
                "total_count": len(groups),
                "runner_groups": [] if path.endswith("page=2") else groups,
            }
        else:
            doc = groups[0]
        doc = copy.deepcopy(doc)
        if mutate:
            mutate(path, doc, calls)
        return doc

    monkeypatch.setattr(image, "_native_group_request", request)
    return calls


@pytest.mark.parametrize("both", [False, True])
def test_only_measured_private_repositories_can_receive_a_native_job(monkeypatch, both):
    repos = [REPO]
    if both:
        repos.append(
            {"id": 1071436278, "full_name": "Borduas-Holdings/Blazing-Back", "private": True}
        )
    calls = policy(monkeypatch, repos=repos)
    image.verify_native_reader_group()
    assert len(calls) == 8
    assert calls[-1] == ROOT + "/1"
    assert not any("visible_to_repository" in call for call in calls)


@pytest.mark.parametrize(
    "patch",
    [
        {"visibility": "all"},
        {"visibility": "private"},
        {"allows_public_repositories": True},
        {"allows_public_repositories": None},
        {"restricted_to_workflows": True},
        {"restricted_to_workflows": None},
        {"selected_workflows": ["unqualified"]},
        {"default": False},
        {"default": "true"},
        {"id": True},
        {"id": 0},
        {"id": 2**64},
    ],
)
def test_missing_or_broad_default_authority_is_held(monkeypatch, patch):
    policy(monkeypatch, groups=[GROUP | patch])
    with pytest.raises(ValueError, match="Native reader"):
        image.verify_native_reader_group()


@pytest.mark.parametrize("groups", [[], [GROUP, GROUP], [GROUP, GROUP | {"id": 2}]])
def test_default_group_must_be_unique_and_complete(monkeypatch, groups):
    policy(monkeypatch, groups=groups)
    with pytest.raises(ValueError):
        image.verify_native_reader_group()


@pytest.mark.parametrize(
    "repos",
    [
        [],
        [REPO | {"private": False}],
        [REPO | {"private": None}],
        [REPO | {"id": True}],
        [REPO | {"id": 1}],
        [REPO | {"full_name": "Other/blazing"}],
        [REPO | {"full_name": None}],
        [REPO, REPO],
        [{"id": 1071436278, "full_name": "Borduas-Holdings/Blazing-Back", "private": True}],
        [REPO, {"id": 42, "full_name": "Other/private", "private": True}],
    ],
)
def test_foreign_public_duplicate_or_missing_caller_access_is_held(monkeypatch, repos):
    policy(monkeypatch, repos=repos)
    with pytest.raises(ValueError):
        image.verify_native_reader_group()


@pytest.mark.parametrize("population", ["runner_groups", "repositories"])
@pytest.mark.parametrize(
    "failure", ["changed-total", "missing-first", "nonterminal", "boolean-total"]
)
def test_full_population_requires_stable_totals_and_terminal_page(
    monkeypatch, population, failure
):
    def mutate(path, doc, calls):
        if population not in doc:
            return
        if failure == "changed-total" and path.endswith("page=2"):
            doc["total_count"] = 2
        if failure == "missing-first" and path.endswith("page=1"):
            doc[population] = []
        if failure == "nonterminal" and path.endswith("page=2"):
            doc[population] = [REPO if population == "repositories" else GROUP]
        if failure == "boolean-total":
            doc["total_count"] = True

    policy(monkeypatch, mutate=mutate)
    with pytest.raises(ValueError):
        image.verify_native_reader_group()


def test_policy_change_during_population_read_is_held(monkeypatch):
    def mutate(path, doc, calls):
        if path == ROOT + "/1" and calls.count(path) == 2:
            doc["visibility"] = "all"

    policy(monkeypatch, mutate=mutate)
    with pytest.raises(ValueError):
        image.verify_native_reader_group()


def test_same_count_access_change_between_complete_reads_is_held(monkeypatch):
    repo2 = {"id": 1071436278, "full_name": "Borduas-Holdings/Blazing-Back", "private": True}

    def mutate(path, doc, calls):
        if "/repositories?" in path and path.endswith("page=1") and calls.count(path) == 2:
            doc["repositories"] = [repo2]

    policy(monkeypatch, mutate=mutate)
    with pytest.raises(ValueError, match="changed during observation"):
        image.verify_native_reader_group()


def test_unknown_group_urls_never_select_the_bearer_destination(monkeypatch):
    def mutate(path, doc, calls):
        if path == ROOT + "/1":
            doc["repositories_url"] = "https://hostile.invalid/leak"

    calls = policy(monkeypatch, mutate=mutate)
    image.verify_native_reader_group()
    assert all(call.startswith(ROOT) for call in calls)


@pytest.mark.parametrize(
    "failure", ["403", "timeout", "oserror", "invalid-json", "duplicate", "oversize"]
)
def test_hosted_http_reads_never_expose_tokens_responses_or_error_chains(
    monkeypatch, capsys, failure
):
    token = "synthetic-github-token"
    monkeypatch.setenv("GH_TOKEN", token)
    response = Mock()
    response.status = 200
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = {
        "invalid-json": token.encode(),
        "duplicate": b'{"id":1,"id":2}',
        "oversize": b"x" * (1024 * 1024 + 1),
    }.get(failure, b"{}")

    def send(request, *, timeout):
        assert request.full_url == "https://api.github.com" + ROOT + "/1"
        assert request.get_method() == "GET"
        assert request.get_header("Authorization") == "Bearer " + token
        assert timeout == 20 and token not in request.full_url
        if failure == "403":
            raise urllib.error.HTTPError(
                request.full_url, 403, token, HTTPMessage(), io.BytesIO(token.encode())
            )
        if failure == "timeout":
            raise TimeoutError(token)
        if failure == "oserror":
            raise OSError(token)
        return response

    monkeypatch.setattr(image.urllib.request, "build_opener", lambda handler: Mock(open=send))
    with pytest.raises(ValueError, match="authority was not verified") as caught:
        image._native_group_request(ROOT + "/1")
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert token not in "".join(traceback.format_exception(caught.value))
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


def test_redirect_handler_refuses_credential_forwarding():
    assert (
        image._GroupNoRedirect().redirect_request(
            image.urllib.request.Request("https://api.github.com" + ROOT + "/1"),
            io.BytesIO(),
            302,
            "",
            HTTPMessage(),
            "https://hostile.invalid",
        )
        is None
    )


@pytest.mark.parametrize(
    "path", ["https://hostile.invalid", ROOT + "/1?leak=fixture", ROOT + "/0"]
)
def test_nonfixed_http_paths_are_refused_before_transport(monkeypatch, path):
    monkeypatch.setenv("GH_TOKEN", "synthetic-github-token")
    sender = Mock(side_effect=AssertionError("must not open transport"))
    monkeypatch.setattr(image.urllib.request, "build_opener", sender)
    with pytest.raises(ValueError):
        image._native_group_request(path)
    sender.assert_not_called()


def test_group_gate_precedes_sops_and_runs_again_before_credential_injection(
    tmp_path, monkeypatch, capsys
):
    from tests.test_runner_native_reader import decrypt, invoke, scope, template

    scope(monkeypatch)
    cipher, calls = decrypt(tmp_path, monkeypatch)
    path = template(tmp_path)
    before = path.read_bytes()
    gate = Mock(side_effect=image.NativeReaderGroupError("held fixed group"))
    monkeypatch.setattr(image, "verify_native_reader_group", gate)
    assert invoke(monkeypatch, path, cipher) == 1
    assert calls == [] and path.read_bytes() == before
    output = capsys.readouterr().out
    assert "::add-mask::" not in output
    assert "NATIVE_READER_GROUP_UNQUALIFIED" in output
    gate.reset_mock(side_effect=True)
    gate.side_effect = [None, image.NativeReaderGroupError("policy changed before injection")]
    assert invoke(monkeypatch, path, cipher) == 1
    assert len(calls) == 1 and path.read_bytes() == before
    assert gate.call_count == 2


def test_mint_gate_is_native_only_and_preserves_monotonic_create_attribution():
    from pathlib import Path

    import yaml

    doc = yaml.safe_load(
        (Path(__file__).parents[1] / ".github/workflows/runner-pool.yml").read_text()
    )
    step = next(s for s in doc["jobs"]["pool"]["steps"] if s.get("id") == "provision")
    assert step["env"]["RUNNER_NATIVE_PULL_READER"] == "${{ inputs.runner-native-pull-reader }}"
    script = step["run"]
    marker = 'if [ "${RUNNER_NATIVE_PULL_READER:-false}" = true ]'
    assert script.index(marker) < script.index("${RUNNER_COLLECTION}/registration-token")
    refusal = script[script.index(marker) : script.index("RC=0", script.index(marker))]
    assert '"$CREATED_DSEQ"' in refusal and '"$UNCLASSIFIED_ATTEMPT"' in refusal
    assert "NATIVE_READER_GROUP_UNQUALIFIED" in refusal and "exit 1" in refusal


@pytest.mark.parametrize("created,unknown", [("", "0"), ("42", "0"), ("", "1")])
def test_actual_mint_guard_fails_before_mint_and_keeps_prior_create_authority(
    tmp_path, created, unknown
):
    from pathlib import Path

    import yaml

    root = Path(__file__).parents[1]
    doc = yaml.safe_load((root / ".github/workflows/runner-pool.yml").read_text())
    script = next(s for s in doc["jobs"]["pool"]["steps"] if s.get("id") == "provision")["run"]
    marker = 'if [ "${RUNNER_NATIVE_PULL_READER:-false}" = true ]'
    guard = script[script.index(marker) : script.index("RC=0", script.index(marker))]
    output = tmp_path / "outcome"
    output.touch()
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", guard + "echo should-not-mint"],
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(root),
            "RUNNER_NATIVE_PULL_READER": "true",
            "CREATED_DSEQ": created,
            "UNCLASSIFIED_ATTEMPT": unknown,
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1 and "should-not-mint" not in result.stdout
    assert (
        "failure_reason=NATIVE_READER_GROUP_UNQUALIFIED"  # pragma: allowlist secret
        in output.read_text()
    )
    assert ("deployment_outcome=no-deployment" in output.read_text()) == (
        not created and unknown == "0"
    )
    assert "Traceback" not in result.stderr


def test_actual_mint_guard_can_pass_a_positive_server_observation(tmp_path):
    from pathlib import Path

    import yaml

    root = Path(__file__).parents[1]
    doc = yaml.safe_load((root / ".github/workflows/runner-pool.yml").read_text())
    script = next(s for s in doc["jobs"]["pool"]["steps"] if s.get("id") == "provision")["run"]
    marker = 'if [ "${RUNNER_NATIVE_PULL_READER:-false}" = true ]'
    guard = script[script.index(marker) : script.index("RC=0", script.index(marker))]
    # Replace only the server read at its import boundary; execute the real
    # heredoc/conditional and ensure a positive observation reaches the next phase.
    guard = guard.replace(
        "from just_akash.runner_image import verify_native_reader_admission",
        "def verify_native_reader_admission():\n    return None",
    )
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", guard + "echo reached-next-phase"],
        env={"PATH": os.environ["PATH"], "RUNNER_NATIVE_PULL_READER": "true"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0 and "reached-next-phase" in result.stdout
    assert not result.stderr


@pytest.mark.parametrize("partial", [False, True])
def test_malformed_group_transport_withholds_partial_body_and_status_line(monkeypatch, partial):
    from http.client import BadStatusLine, IncompleteRead

    monkeypatch.setenv("GH_TOKEN", "PATCANARY")
    failure = (
        IncompleteRead(b'{"metadata":"PARTIALRESPONSECANARY"}', 100)
        if partial
        else BadStatusLine("PARTIALRESPONSECANARY")
    )
    opener = Mock()
    if partial:
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.side_effect = failure
        opener.open.return_value = response
    else:
        opener.open.side_effect = failure
    monkeypatch.setattr(image.urllib.request, "build_opener", Mock(return_value=opener))
    with pytest.raises(image.NativeReaderGroupError) as caught:
        image._native_group_request(ROOT + "?per_page=100&page=1")
    rendered = "".join(traceback.format_exception(caught.value))
    assert "PARTIALRESPONSECANARY" not in rendered and "PATCANARY" not in rendered
    assert caught.value.__context__ is None
