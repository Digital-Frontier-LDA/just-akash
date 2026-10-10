"""Real JIT wire capture remains data, never registration or create authority."""

import json

import pytest
from tests.test_github_jit import CONFIG, GitHub

from just_akash import github_jit as jit

REPO = "Borduas-Holdings/Blazing-Back"
WORKFLOW = REPO + "/.github/workflows/sentry-owned-producer.yml@refs/heads/main"
POLICY = jit.JitPolicy(
    "b" * 40, 37, 1071436278, REPO, (WORKFLOW,),
    non_reusable_workflow=True, source_workflow_revision="a" * 40,
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
            "request": github, "producer_workflow_revision": "a" * 40,
            "mint_receipt_path": str(path), "controller_operation_id": "sentry-123-2-build",
            "controller_run_id": "123", "controller_run_attempt": "2",
            "controller_source_revision": "c" * 40,
        }
        options.update(kwargs)
        return jit.mint_jit(POLICY, NAME, LABELS, TOKEN, **options)
    return run, github, path


def test_actual_post_observes_durable_unknown_and_nonsecret_exact_bindings(capture):
    run, github, path = capture
    def request(method, endpoint, token, body=None):
        if method == "POST":
            value = json.loads(path.read_bytes())
            assert value["state"] == "UNKNOWN"
            assert value["operation_id"] == "sentry-123-2-build"
            assert value["controller_claim"] == {"run_id": "123", "run_attempt": "2", "source_revision": "c" * 40, "authenticated": False}
            assert value["policy"]["repository_name"] == REPO
            assert value["policy"]["repository_id"] == POLICY.repository_id
            assert value["policy"]["group_id"] == body["runner_group_id"] == POLICY.group_id
            assert value["policy"]["revision"] == POLICY.revision
            assert value["policy"]["workflows"] == [WORKFLOW]
            assert value["producer_workflow_revision"] == "a" * 40
            assert value["runner_name"] == body["name"] == NAME
            assert value["labels"] == body["labels"] == list(LABELS)
            assert value["request_path"] == endpoint == POLICY.api_root + "/runners/generate-jitconfig"
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
