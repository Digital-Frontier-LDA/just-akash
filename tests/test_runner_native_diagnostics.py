"""Private server failures cannot echo remote status/body/CLI data into diagnostics."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
ECHO = "UNTRUSTED_RESPONSECANARY"


def body():
    doc = yaml.safe_load((ROOT / ".github/workflows/runner-pool.yml").read_text())
    return next(s["run"] for s in doc["jobs"]["pool"]["steps"] if s.get("id") == "provision")


def execute(tmp_path, script, *, native, response="", extra=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_RESPONSE"\n')
    gh.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", script],
        env={
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "RUNNER_NATIVE_PULL_READER": "true" if native else "false",
            "RUNNER_NATIVE_REPOSITORY_SCOPE": "false",
            "RUNNER_COLLECTION": "repos/Borduas-Holdings/blazing/actions/runners",
            "CREATED_DSEQ": "",
            "UNCLASSIFIED_ATTEMPT": "0",
            "GITHUB_OUTPUT": str(tmp_path / "output"),
            "FAKE_RESPONSE": response,
            **(extra or {}),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result


@pytest.mark.parametrize("native", [True, False])
def test_mint_http_status_word_cannot_echo_in_private_output(tmp_path, native):
    script = body()
    start = script.index('RESP=$(gh api --method POST "${RUNNER_COLLECTION}/registration-token"')
    end = script.index('if [ "${RUNNER_NATIVE_REPOSITORY_SCOPE:-false}" = true ]', start)
    result = execute(
        tmp_path,
        "set -uo pipefail\nRC=0\nattempt=1\n" + script[start:end],
        native=native,
        response=f'HTTP/2.0 {ECHO}\n{{"token":"{ECHO}","message":"{ECHO}"}}',
    )
    assert result.returncode == 1
    if native:
        assert ECHO not in result.stdout + result.stderr
        assert "none received" in result.stdout
    else:
        assert ECHO in result.stdout


@pytest.mark.parametrize("native", [True, False])
def test_verdict_http_status_word_cannot_echo_in_private_output(tmp_path, native):
    script = body()
    start = script.index("VERDICT_RESP=$(gh api --method POST")
    end = script.index('echo "failure_reason=RUNNER_NEVER_REGISTERED"', start)
    result = execute(
        tmp_path,
        "set -uo pipefail\nVERDICT_RC=0\n" + script[start:end],
        native=native,
        response=f'HTTP/2.0 {ECHO}\n{{"token":"{ECHO}","message":"{ECHO}"}}',
    )
    assert result.returncode == 1
    if native:
        assert ECHO not in result.stdout + result.stderr
        assert "no status" in result.stdout
    else:
        assert ECHO in result.stdout


@pytest.mark.parametrize("native", [True, False])
def test_bad_listing_page_never_echoes_private_remote_message(tmp_path, native):
    script = body()
    start = script.index('if [ "${BAD_PAGES:-0}" -ne 0 ]; then')
    end = script.index(
        'if [ "${RUNNER_NATIVE_PULL_READER:-false}" = true ]; then',
        script.index("continue", start),
    )
    # Close the enclosing GH_RC conditional in the extracted actual block.
    block = script[start:end].rsplit("fi", 1)[0]
    result = execute(
        tmp_path,
        "set -uo pipefail\nsleep() { :; }\nfor i in 1; do\n" + block + "\ndone",
        native=native,
        extra={"BAD_PAGES": "1", "RUNNER_PAGES": json.dumps({"message": ECHO})},
    )
    assert result.returncode == 0, result.stderr
    assert (ECHO not in result.stdout + result.stderr) if native else ECHO in result.stdout


@pytest.mark.parametrize("native", [True, False])
def test_private_listing_cli_failure_never_echoes_remote_stderr(tmp_path, native):
    script = body()
    start = script.index('if [ "$GH_RC" -ne 0 ]; then', script.index("RUNNER_VERSIONS=$(printf"))
    end = script.index("API_OK=1", start)
    result = execute(
        tmp_path,
        "set -uo pipefail\nsleep() { :; }\ntail() { printf '%s' '"
        + ECHO
        + "'; }\nfor i in 1; do\n"
        + script[start:end]
        + "\ndone",
        native=native,
        extra={"GH_RC": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert (ECHO not in result.stdout + result.stderr) if native else ECHO in result.stdout


@pytest.mark.parametrize(
    "patch",
    [
        {"version": ECHO},
        {"id": ECHO},
        {"labels": ECHO},
        {"busy": None},
        {"status": ECHO},
    ],
)
def test_private_population_rejects_malformed_and_version_echo_before_projection(tmp_path, patch):
    script = body()
    start = script.index(
        'if [ "${RUNNER_NATIVE_PULL_READER:-false}" = true ]; then',
        script.index("BAD_PAGES=$(printf"),
    )
    start = script.index('if [ "${RUNNER_NATIVE_PULL_READER:-false}" = true ]; then', start + 1)
    end = script.index("RUNNER_IDS=$(printf", start)
    row = {
        "id": 701,
        "name": "just-akash-podman-images-123-2-abc",
        "status": "online",
        "busy": False,
        "labels": [{"name": "podman-images-123-2"}],
        "version": "2.337.0",
    } | patch
    result = execute(
        tmp_path,
        "set -uo pipefail\nsleep() { :; }\nfor i in 1; do\n"
        + script[start:end]
        + "\nprintf projection-ran\ndone",
        native=True,
        extra={"RUNNER_PAGES": json.dumps({"total_count": 1, "runners": [row]})},
    )
    assert result.returncode == 0, result.stderr
    assert ECHO not in result.stdout + result.stderr
    assert "Private runner population was not verified" in result.stdout
    assert "projection-ran" not in result.stdout


@pytest.mark.parametrize(
    "pages",
    [
        [{"total_count": 1, "runners": []}],
        [{"total_count": 0, "runners": []}, {"total_count": 1, "runners": []}],
        [{"message": ECHO}],
    ],
)
def test_private_population_refuses_incomplete_or_error_pages(tmp_path, pages):
    script = body()
    start = script.index(
        'if [ "${RUNNER_NATIVE_PULL_READER:-false}" = true ]; then',
        script.index("BAD_PAGES=$(printf"),
    )
    start = script.index('if [ "${RUNNER_NATIVE_PULL_READER:-false}" = true ]; then', start + 1)
    end = script.index("RUNNER_IDS=$(printf", start)
    result = execute(
        tmp_path,
        "set -uo pipefail\nsleep() { :; }\nfor i in 1; do\n"
        + script[start:end]
        + "\nprintf projection-ran\ndone",
        native=True,
        extra={"RUNNER_PAGES": "\n".join(json.dumps(p) for p in pages)},
    )
    assert result.returncode == 0, result.stderr
    assert ECHO not in result.stdout + result.stderr
    assert "projection-ran" not in result.stdout


def test_private_empty_complete_population_allows_projection(tmp_path):
    script = body()
    start = script.index(
        'if [ "${RUNNER_NATIVE_PULL_READER:-false}" = true ]; then',
        script.index("BAD_PAGES=$(printf"),
    )
    start = script.index('if [ "${RUNNER_NATIVE_PULL_READER:-false}" = true ]; then', start + 1)
    end = script.index("RUNNER_IDS=$(printf", start)
    result = execute(
        tmp_path,
        "set -uo pipefail\nsleep() { :; }\nfor i in 1; do\n"
        + script[start:end]
        + "\nprintf projection-ran\ndone",
        native=True,
        extra={"RUNNER_PAGES": json.dumps({"total_count": 0, "runners": []})},
    )
    assert result.returncode == 0, result.stderr
    assert "projection-ran" in result.stdout


@pytest.mark.parametrize("fail_at", [1, 2])
def test_private_repository_identity_change_around_poll_holds_known_lease(tmp_path, fail_at):
    script = body()
    start = script.index(
        'if [ "${RUNNER_NATIVE_REPOSITORY_SCOPE:-false}" = true ]'
        " && ! verify_private_repository_identity;",
        script.index("# An error page"),
    )
    end = script.index('if [ "$GH_RC" -eq 0 ]; then', start)
    prefix = (
        "set -uo pipefail\nchecks=0\nverify_private_repository_identity() { "
        'checks=$((checks+1)); [ "$checks" -ne ' + str(fail_at) + " ]; }\n"
    )
    result = execute(
        tmp_path,
        prefix + script[start:end] + "\nprintf accepted-poll",
        native=True,
        response='{"total_count":0,"runners":[]}',
        extra={"RUNNER_NATIVE_REPOSITORY_SCOPE": "true", "CREATED_DSEQ": "1791227916291"},
    )
    assert result.returncode == 1, result.stderr
    assert "accepted-poll" not in result.stdout and "lease retained" in result.stdout
    output = (tmp_path / "output").read_text()
    assert "NATIVE_READER_REPOSITORY_UNQUALIFIED" in output
    assert "deployment_outcome=no-deployment" not in output


@pytest.mark.parametrize(
    "document", [{}, {"message": ECHO}, {"token": ECHO}, {"token": "A" * 4097}]
)
def test_private_success_status_without_bounded_token_is_not_write_authority(tmp_path, document):
    script = body()
    start = script.index("VERDICT_RC=0")
    end = script.index('echo "failure_reason=RUNNER_NEVER_REGISTERED"', start)
    result = execute(
        tmp_path,
        "set -uo pipefail\n" + script[start:end],
        native=True,
        response="HTTP/2.0 201 Created\n" + json.dumps(document),
    )
    assert result.returncode == 1
    assert "could not be re-checked" in result.stdout
    assert ECHO not in result.stdout + result.stderr
    assert "::add-mask::" not in result.stdout
