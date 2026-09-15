"""runner-pool renders a MINTED registration token into the runner SDL, never the org PAT (#383).

The SDL goes to the Akash Console API (create_deployment receives it whole) and to the
lease-winning provider (the manifest). It used to carry `ACCESS_TOKEN=${GH_RUNNER_PAT}`. The
render step now mints a ~1h registration token with the PAT and renders `RUNNER_TOKEN=<minted>`,
which the pinned df-akash-runner image reads before ACCESS_TOKEN.

Three legs, per the issue's acceptance:
  * the REAL mint code path, executed with `gh` stubbed, fixtures defaulting to 201;
  * structure: no PAT in the SDL; the token is masked before use and never an output or env;
  * the whole mint -> runners-online window is timed, and the sum stays under the TTL.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "runner-pool.yml"
DOC = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
POOL = DOC["jobs"]["pool"]
SDL_PATH = "/tmp/runner-sdl.yaml"
TOKEN = "AREGISTRATIONTOKENXYZ"
PAT = "ghp_THE_ORG_RUNNER_PAT"

# A GitHub registration token is valid for one hour.
REGISTRATION_TOKEN_TTL_MINUTES = 60
WINDOW_MARGIN_MINUTES = 10


def _step(step_id: str, doc: dict = DOC) -> dict:
    matches = [s for s in doc["jobs"]["pool"]["steps"] if s.get("id") == step_id]
    assert len(matches) == 1, f"expected one pool step with id {step_id!r}, found {len(matches)}"
    return matches[0]


def _code(body: str) -> str:
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))


def _response(status: int | None, token: str | None = TOKEN) -> str:
    """What `gh api -i` prints: status line, headers, blank line, body."""
    if status is None:
        return "error connecting to api.github.com"
    reason = {200: "OK", 201: "Created", 202: "Accepted", 204: "No Content"}.get(status, "Error")
    body = (
        f'{{"token":"{token}","expires_at":"2026-09-15T09:00:00Z"}}'
        if token
        else '{"message":"x"}'
    )
    if status == 204:
        body = ""
    return f"HTTP/2.0 {status} {reason}\r\nContent-Type: application/json\r\n\r\n{body}"


def _run_render(
    tmp_path: Path, status: int | None = 201, token: str | None = TOKEN, rc: int | None = None
):
    """Execute the REAL render step, with only `gh` stubbed and the SDL path moved into tmp."""
    render = _step("render")["run"]
    assert render.count(SDL_PATH) == 2, (
        "the harness relocates exactly the write and the redacted echo"
    )
    sdl = tmp_path / "runner-sdl.yaml"
    script = tmp_path / "render.sh"
    script.write_text(render.replace(SDL_PATH, str(sdl)), encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "gh-calls"
    (fake_bin / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'printf "%s" "$FAKE_RESP"\nexit "$FAKE_RC"\n',
        encoding="utf-8",
    )
    (fake_bin / "gh").chmod(0o755)
    if rc is None:
        rc = 0 if status is not None and 200 <= status < 300 else 1
    output = tmp_path / "output"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_RESP": _response(status, token),
        "FAKE_RC": str(rc),
        "GH_TOKEN": PAT,
        "ORG": "testorg",
        "RUNNER_LABEL": "pool-label",
        "POOL_SIZE": "2",
        "CPU": "1",
        "MEMORY": "1Gi",
        "STORAGE": "1Gi",
        "EPHEMERAL": "true",
        "PLACEMENT_KEY": "borduas-runner",
        "GH_RUN_ID": "42",
        "GITHUB_OUTPUT": str(output),
    }
    # Actions runs a `run:` step as `bash -e {0}`.
    proc = subprocess.run(
        ["bash", "-e", str(script)], env=env, capture_output=True, text=True, timeout=30
    )
    assert "unexpected EOF" not in proc.stderr, proc.stderr
    return (
        proc,
        sdl.read_text(encoding="utf-8") if sdl.exists() else None,
        output.read_text(encoding="utf-8") if output.exists() else "",
        calls.read_text(encoding="utf-8").splitlines() if calls.exists() else [],
    )


# --------------------------------------------------------------------------- the real mint path


def test_a_201_mint_renders_the_minted_token_and_never_the_pat(tmp_path):
    proc, sdl, output, calls = _run_render(tmp_path, status=201)

    assert proc.returncode == 0, proc.stderr
    assert calls == ["api --method POST orgs/testorg/actions/runners/registration-token -i"], calls
    assert sdl is not None, "no SDL was written"
    runner_env = yaml.safe_load(sdl)["services"]["runner"]["env"]
    assert f"RUNNER_TOKEN={TOKEN}" in runner_env, runner_env
    assert not any(e.startswith("ACCESS_TOKEN=") for e in runner_env), runner_env
    assert PAT not in sdl, "the org PAT reached the rendered SDL"
    assert TOKEN not in output and PAT not in output, "a credential was written to GITHUB_OUTPUT"
    # Masked before any use: the only place the raw token appears in the log is the mask itself.
    log = proc.stdout + proc.stderr
    assert f"::add-mask::{TOKEN}" in log
    assert log.count(TOKEN) == 1, "the token was printed somewhere other than its ::add-mask::"


def test_a_200_mint_is_accepted(tmp_path):
    proc, sdl, _, _ = _run_render(tmp_path, status=200)

    assert proc.returncode == 0, proc.stderr
    assert sdl is not None and f"RUNNER_TOKEN={TOKEN}" in sdl


@pytest.mark.parametrize(
    ("status", "token", "rc"),
    [
        (202, TOKEN, 0),
        (204, None, 0),
        (401, None, 1),
        (403, None, 1),
        (404, None, 1),
        (422, None, 1),
        (201, None, 0),
        (None, None, 1),
    ],
    ids=["202", "204", "401", "403", "404", "422", "201-without-token", "no-response"],
)
def test_anything_but_a_200_or_201_with_a_token_refuses_before_rendering(
    tmp_path, status, token, rc
):
    proc, sdl, output, _ = _run_render(tmp_path, status=status, token=token, rc=rc)

    assert proc.returncode == 1, (proc.returncode, proc.stderr)
    assert "failure_reason=RUNNER_TOKEN_UNMINTED" in output, output
    assert sdl is None, "an SDL was rendered without a minted token"
    assert "::error title=Runner registration token not minted" in proc.stdout + proc.stderr
    assert PAT not in proc.stdout + proc.stderr


# --------------------------------------------------------------------------- structure


def _sdl_heredoc(render_run: str) -> str:
    code = _code(render_run)
    start = code.index(f"cat > {SDL_PATH} <<SDL\n")
    end = code.index("\nSDL\n", start)
    return code[start:end]


def test_the_sdl_carries_the_minted_token_and_no_pat():
    sdl = _sdl_heredoc(_step("render")["run"])

    assert sdl.count("RUNNER_TOKEN=${RUNNER_TOKEN}") == 1, sdl
    for forbidden in ("GH_RUNNER_PAT", "ACCESS_TOKEN", "GH_TOKEN"):
        assert forbidden not in sdl, f"{forbidden} is in the rendered runner SDL"


def test_the_token_is_masked_before_its_first_use_and_never_published():
    """Before the mask the token may only be ASSIGNED and TESTED, never emitted; and no
    GITHUB_OUTPUT or GITHUB_ENV write carries it."""
    lines = _code(_step("render")["run"]).splitlines()
    mask = [i for i, line in enumerate(lines) if 'echo "::add-mask::${RUNNER_TOKEN}"' in line]
    assert len(mask) == 1, mask
    before = [line.strip() for line in lines[: mask[0]] if "RUNNER_TOKEN" in line]
    assert len(before) == 2 and before[0] == 'RUNNER_TOKEN=""', before
    assert (
        before[1].startswith('if [[ "$RESP" =~ ')
        and 'RUNNER_TOKEN="${BASH_REMATCH[1]}"' in before[1]
    )
    heredoc = [
        i for i, line in enumerate(lines) if line.strip().startswith("cat > /tmp/runner-sdl.yaml")
    ]
    assert len(heredoc) == 1 and mask[0] < heredoc[0], "the SDL was written before the mask"
    writes = [line for line in lines if "GITHUB_OUTPUT" in line or "GITHUB_ENV" in line]
    assert len(writes) >= 2, writes
    token_var = re.compile(r"\$\{?RUNNER_TOKEN\}?(?![A-Z_])")
    assert not any(token_var.search(line) for line in writes), writes


def _steps_expression_env_violations(doc: dict) -> list[str]:
    violations = []
    for where, env in [("workflow", doc.get("env") or {})] + [
        (f"job {name}", job.get("env") or {}) for name, job in doc["jobs"].items()
    ]:
        violations += [
            f"{where} env {key}" for key, value in env.items() if "steps." in str(value)
        ]
    for name, job in doc["jobs"].items():
        violations += [
            f"job {name} output {key}"
            for key, value in (job.get("outputs") or {}).items()
            if re.search(r"(?i)token", key)
            or re.search(r"steps\.\w+\.outputs\.\w*token", str(value), re.I)
        ]
    return violations


def test_no_steps_value_is_hoisted_into_job_or_workflow_env_or_a_token_output():
    assert len(DOC["jobs"]) >= 2
    assert _steps_expression_env_violations(DOC) == []


def test_the_env_leg_fires_when_the_token_is_hoisted_to_job_env():
    mutated = copy.deepcopy(DOC)
    mutated["jobs"]["pool"]["env"] = {"RUNNER_TOKEN": "${{ steps.render.outputs.runner_token }}"}
    assert _steps_expression_env_violations(mutated), (
        "hoisting the token to job env went unnoticed"
    )


def test_the_pat_preflight_still_owns_the_pat_failure_reasons():
    """blazing's cleanup reads RUNNER_PAT_* as "nothing was deployed" because only the preflight
    emits it (Borduas-Holdings/blazing tests/test_runner_registration_cleanup.py). The mint's
    refusal must be a different reason, and the job output must still lead with the preflight."""
    render_code = _code(_step("render")["run"])
    assert "RUNNER_PAT_" not in render_code
    output = POOL["outputs"]["failure_reason"]
    assert output.index("steps.pat.outputs.failure_reason") < output.index(
        "steps.render.outputs.failure_reason"
    )


# --------------------------------------------------------------------------- the TTL window


def _window_violations(doc: dict) -> list[str]:
    steps = doc["jobs"]["pool"]["steps"]
    mint = [
        i
        for i, s in enumerate(steps)
        if "registration-token" in _code(s.get("run") or "")
        and "RUNNER_TOKEN=" in _code(s.get("run") or "")
    ]
    online = [i for i, s in enumerate(steps) if "WAIT_DEADLINE=" in _code(s.get("run") or "")]
    if len(mint) != 1 or len(online) != 1 or mint[0] > online[0]:
        return [f"cannot locate the window: mint steps {mint}, runners-online steps {online}"]
    window = steps[mint[0] : online[0] + 1]
    violations = [
        f"step {s.get('id') or s.get('name')!r} in the token window has no timeout-minutes"
        for s in window
        if not isinstance(s.get("timeout-minutes"), int)
    ]
    total = sum(s.get("timeout-minutes") or 0 for s in window)
    bound = REGISTRATION_TOKEN_TTL_MINUTES - WINDOW_MARGIN_MINUTES
    if total > bound:
        violations.append(f"the token window allows {total} min, over {bound} (TTL minus margin)")
    return violations


def test_every_step_from_the_mint_to_runners_online_is_timed_inside_the_ttl():
    steps = POOL["steps"]
    assert len(steps) >= 5
    assert _window_violations(DOC) == []


def test_the_window_leg_fires_on_an_untimed_middle_step():
    mutated = copy.deepcopy(DOC)
    steps = mutated["jobs"]["pool"]["steps"]
    render_at = steps.index(_step("render", mutated))
    steps.insert(render_at + 1, {"name": "untimed", "run": "true"})
    assert any("has no timeout-minutes" in v for v in _window_violations(mutated))


def test_the_window_leg_fires_when_the_sum_exceeds_the_bound():
    mutated = copy.deepcopy(DOC)
    _step("provision", mutated)["timeout-minutes"] = 60
    assert any("over 50" in v for v in _window_violations(mutated))
