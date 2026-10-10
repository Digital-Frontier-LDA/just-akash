"""Real JIT wire capture remains data, never registration or create authority."""

import json

import pytest

from just_akash import github_jit as jit
from tests.test_github_jit import CONFIG, GitHub

REPO = "Borduas-Holdings/Blazing-Back"
WORKFLOW = REPO + "/.github/workflows/sentry-owned-producer.yml@refs/heads/main"
POLICY = jit.JitPolicy(
    "b" * 40,
    37,
    1071436278,
    REPO,
    (WORKFLOW,),
    non_reusable_workflow=True,
    source_workflow_revision="a" * 40,
)
NAME = "dfci-sentry-123-2-build"
LABELS = (NAME, "linux", "akash")
TOKEN = "installation-fixture-never-persist"


@pytest.fixture
def capture(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "mint.json"
    github = GitHub()
    github.group["selected_workflows"] = [WORKFLOW]
    github.pages[0]["repositories"][0].update(id=POLICY.repository_id, full_name=REPO)
    github.result["runner"]["name"] = NAME

    def run(**kwargs):
        options = {
            "request": github,
            "producer_workflow_revision": "a" * 40,
            "mint_receipt_path": str(path),
            "controller_operation_id": "sentry-123-2-build",
            "controller_run_id": "123",
            "controller_run_attempt": "2",
            "controller_source_revision": "c" * 40,
        }
        options.update(kwargs)
        return jit.mint_jit(POLICY, NAME, LABELS, TOKEN, **options)

    return run, github, path


def test_actual_post_observes_durable_unknown_and_nonsecret_exact_bindings(capture):
    run, github, path = capture

    def request(method, endpoint, token, body=None):
        if method == "POST":
            assert isinstance(body, dict)
            value = json.loads(path.read_bytes())
            assert value["state"] == "UNKNOWN"
            assert value["operation_id"] == "sentry-123-2-build"
            assert value["controller_claim"] == {
                "run_id": "123",
                "run_attempt": "2",
                "source_revision": "c" * 40,
                "authenticated": False,
            }
            assert value["policy"]["repository_name"] == REPO
            assert value["policy"]["repository_id"] == POLICY.repository_id
            assert value["policy"]["group_id"] == body["runner_group_id"] == POLICY.group_id
            assert value["policy"]["revision"] == POLICY.revision
            assert value["policy"]["workflows"] == [WORKFLOW]
            assert value["producer_workflow_revision"] == "a" * 40
            assert value["runner_name"] == body["name"] == NAME
            assert value["labels"] == body["labels"] == list(LABELS)
            assert (
                value["request_path"]
                == endpoint
                == POLICY.api_root + "/runners/generate-jitconfig"
            )
            assert path.stat().st_mode & 0o777 == 0o600
        return github(method, endpoint, token, body)

    handoff = run(request=request)
    assert handoff.runner_id == 789 and handoff.encoded_config == CONFIG
    candidate = json.loads(path.with_suffix(".response.json").read_bytes())
    assert candidate["runner_id"] == 789 and candidate["runner_name"] == NAME
    assert candidate["registration_verified"] is candidate["publication_authority"] is False
    assert json.loads(path.read_bytes())["state"] == "UNKNOWN"
    text = path.read_text() + path.with_suffix(".response.json").read_text()
    assert TOKEN not in text and CONFIG not in text and "one-job-fixture" not in text
    calls = list(github.calls)
    with pytest.raises(jit.JitHold):
        run()
    assert github.calls == calls


def test_lost_post_ack_retains_unknown_and_refuses_remint_before_any_request(capture):
    run, github, path = capture
    github.fail_post = True
    with pytest.raises(jit.JitMintUnknown):
        run()
    assert json.loads(path.read_bytes())["state"] == "UNKNOWN"
    calls = list(github.calls)
    with pytest.raises(jit.JitHold):
        run()
    assert github.calls == calls and sum(call[0] == "POST" for call in calls) == 1


def test_malformed_ack_never_persists_private_config_or_permits_replacement(capture):
    run, github, path = capture
    github.result["encoded_jit_config"] = "invalid-config"
    with pytest.raises(jit.JitMintUnknown):
        run()
    assert path.exists() and not path.with_suffix(".response.json").exists()
    with pytest.raises(jit.JitHold):
        run()
    assert sum(call[0] == "POST" for call in github.calls) == 1


def test_source_refusal_never_installs_intent_or_posts(capture):
    run, github, path = capture
    with pytest.raises(jit.JitHold):
        run(producer_workflow_revision="d" * 40)
    assert not path.exists() and github.calls == []


@pytest.mark.parametrize(
    "error",
    [
        OSError("private-post-failure"),
        RuntimeError("private-post-failure"),
        TimeoutError("private-post-failure"),
    ],
)
def test_any_ambiguous_post_exception_retains_unknown_without_private_error_text(
    capture, error, capsys
):
    run, github, path = capture

    def request(method, endpoint, token, body=None):
        if method == "POST":
            github.calls.append((method, endpoint, token, body))
            raise error
        return github(method, endpoint, token, body)

    with pytest.raises(jit.JitMintUnknown) as outcome:
        run(request=request)
    assert "private-post-failure" not in str(outcome.value) + str(capsys.readouterr())
    assert json.loads(path.read_bytes())["state"] == "UNKNOWN"
    with pytest.raises(jit.JitHold):
        run()
    assert sum(call[0] == "POST" for call in github.calls) == 1


def test_process_interruption_after_request_never_allows_same_slot_remint(capture):
    run, github, path = capture

    def request(method, endpoint, token, body=None):
        if method == "POST":
            github.calls.append((method, endpoint, token, body))
            raise SystemExit(17)
        return github(method, endpoint, token, body)

    with pytest.raises(SystemExit):
        run(request=request)
    assert json.loads(path.read_bytes())["state"] == "UNKNOWN"
    calls = list(github.calls)
    with pytest.raises(jit.JitHold):
        run()
    assert github.calls == calls


def test_intent_directory_fsync_lost_ack_leaves_unknown_without_post(capture, monkeypatch):
    from just_akash import deployment_receipt as receipts

    run, github, path = capture
    original = receipts._fsync_parent

    def sync(parent):
        original(parent)
        if path.exists():
            raise OSError("private-directory-sync-failure")

    monkeypatch.setattr(receipts, "_fsync_parent", sync)
    with pytest.raises(jit.JitMintUnknown):
        run()
    assert json.loads(path.read_bytes())["state"] == "UNKNOWN"
    assert not any(call[0] == "POST" for call in github.calls)
    calls = list(github.calls)
    with pytest.raises(jit.JitHold):
        run()
    assert github.calls == calls


@pytest.mark.parametrize("installed", [False, True])
def test_candidate_write_failure_before_or_after_install_never_replays(
    capture, monkeypatch, installed
):
    from just_akash import jit_mint_receipt as receipts

    run, github, path = capture
    original = receipts._create_durable

    def persist(target, data):
        if target != path.with_suffix(".response.json"):
            return original(target, data)
        if installed:
            original(target, data)
        raise OSError("private-response-install-failure")

    monkeypatch.setattr(receipts, "_create_durable", persist)
    with pytest.raises(jit.JitMintUnknown):
        run()
    assert path.with_suffix(".response.json").exists() == installed
    assert json.loads(path.read_bytes())["state"] == "UNKNOWN"
    with pytest.raises(jit.JitHold):
        run()
    assert sum(call[0] == "POST" for call in github.calls) == 1


def test_existing_response_without_intent_refuses_before_every_request(capture):
    run, github, path = capture
    response = path.with_suffix(".response.json")
    response.write_text("retained-response")
    with pytest.raises(jit.JitHold):
        run()
    assert github.calls == [] and response.read_text() == "retained-response" and not path.exists()


def test_concurrent_slot_creation_is_never_overwritten_and_prevents_post(capture):
    run, github, path = capture

    def request(method, endpoint, token, body=None):
        result = github(method, endpoint, token, body)
        if github.group_reads == 2:
            path.write_text("other-operation")
        return result

    with pytest.raises(jit.JitMintUnknown):
        run(request=request)
    assert path.read_text() == "other-operation" and not any(
        call[0] == "POST" for call in github.calls
    )


def test_same_owner_replacement_after_post_is_retained_and_no_candidate_is_written(capture):
    run, github, path = capture

    def request(method, endpoint, token, body=None):
        if method == "POST":
            path.write_text("other-operation")
        return github(method, endpoint, token, body)

    with pytest.raises(jit.JitMintUnknown):
        run(request=request)
    assert (
        path.read_text() == "other-operation" and not path.with_suffix(".response.json").exists()
    )
    with pytest.raises(jit.JitHold):
        run()
    assert sum(call[0] == "POST" for call in github.calls) == 1


def test_group_refusal_leaves_no_intent_and_no_post(capture):
    run, github, path = capture
    github.group["allows_public_repositories"] = True
    with pytest.raises(jit.JitHold):
        run()
    assert not path.exists() and not any(call[0] == "POST" for call in github.calls)


@pytest.mark.parametrize(
    "key,value",
    [
        ("mint_receipt_path", None),
        ("controller_operation_id", None),
        ("controller_run_id", None),
        ("controller_run_attempt", None),
        ("controller_source_revision", None),
        ("controller_operation_id", "invalid\nclaim"),
        ("controller_run_id", "0123"),
        ("controller_run_attempt", "0"),
        ("controller_run_id", str(2**64)),
        ("controller_source_revision", "not-a-source"),
    ],
)
def test_incomplete_or_noncanonical_claims_refuse_before_policy_or_post(capture, key, value):
    run, github, path = capture
    with pytest.raises(jit.JitHold):
        run(**{key: value})
    assert github.calls == [] and not path.exists()


@pytest.mark.parametrize(
    "response",
    [None, [], {}, {"runner": {"id": True, "name": NAME}, "encoded_jit_config": CONFIG}],
)
def test_nonobject_or_missing_or_ambiguous_ack_keeps_unknown_and_never_reposts(capture, response):
    run, github, path = capture
    github.result = response
    with pytest.raises(jit.JitMintUnknown):
        run()
    assert json.loads(path.read_bytes())["state"] == "UNKNOWN"
    assert not path.with_suffix(".response.json").exists()
    with pytest.raises(jit.JitHold):
        run()
    assert sum(call[0] == "POST" for call in github.calls) == 1
