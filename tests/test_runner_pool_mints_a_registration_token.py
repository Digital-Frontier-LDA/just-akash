"""runner-pool deploys with a registration token MINTED PER ATTEMPT, never the org PAT (#383).

The SDL goes to the Akash Console API (create_deployment receives it whole) and to the
lease-winning provider (the manifest). It used to carry `ACCESS_TOKEN=${GH_RUNNER_PAT}`. Now the
render step writes a placeholder, `RUNNER_TOKEN=@@RUNNER_TOKEN@@`, and holds no credential; each
provision attempt mints a ~1h registration token immediately before its deploy, fills the
placeholder into a per-attempt SDL, and deletes that file once the deploy returns.

Per attempt, not once (DEV1's review of #384): a failed attempt costs ~21-22 min, so a single mint
would expire by attempt 3, and a step cap short enough to prevent that kills a legitimate retry
mid-close. The window that must fit the TTL is one attempt's mint -> deploy -> runners online.

Legs:
  * the REAL mint-and-deploy fragment of the provision step, executed with `gh` and the
    just-akash CLI stubbed, fixtures defaulting to 201;
  * structure: no PAT in the SDL or the render step; masked before use; never an output or env;
  * the per-attempt window: the mint sits inside the attempt loop before the deploy, and
    bid windows + the clamped runners-online wait fit under TTL minus margin.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "runner-pool.yml"
DOC = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
POOL = DOC["jobs"]["pool"]
TEMPLATE = "/tmp/runner-sdl.yaml"
MINTED = "/tmp/runner-sdl.minted.yaml"
PLACEHOLDER = "@@RUNNER_TOKEN@@"
TOKEN = "AREGISTRATIONTOKENXYZ"
PAT = "ghp_THE_ORG_RUNNER_PAT"

# A GitHub registration token is valid for one hour.
REGISTRATION_TOKEN_TTL_MINUTES = 60
WINDOW_MARGIN_MINUTES = 10

# Indentation as YAML parses the `run:` block scalar: the attempt loop body is two spaces in.
MINT_START = (
    '  RC=0\n  RESP=$(gh api --method POST "orgs/${ORG}/actions/runners/registration-token"'
)
MINT_END = f"  rm -f {MINTED}\n"


def _step(step_id: str, doc: dict = DOC) -> dict:
    matches = [s for s in doc["jobs"]["pool"]["steps"] if s.get("id") == step_id]
    assert len(matches) == 1, f"expected one pool step with id {step_id!r}, found {len(matches)}"
    return matches[0]


def _code(body: str) -> str:
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))


def _mint_fragment(provision_run: str) -> str:
    """The attempt's mint -> per-attempt SDL -> deploy -> delete, exactly as the step has it."""
    assert provision_run.count(MINT_START) == 1, "the per-attempt mint is not where expected"
    assert provision_run.count(MINT_END) == 1
    start = provision_run.index(MINT_START)
    return provision_run[start : provision_run.index(MINT_END) + len(MINT_END)]


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


def _run_attempt(tmp_path: Path, status=201, token=TOKEN, rc=None, placeholder=True):
    """Execute the REAL fragment with `gh` and the deploy CLI stubbed and the /tmp paths moved."""
    template = tmp_path / "runner-sdl.yaml"
    line = f"RUNNER_TOKEN={PLACEHOLDER}" if placeholder else "RUNNER_TOKEN=missing"
    if placeholder is not None:  # None: the render step's template is missing altogether
        template.write_text(
            'version: "2.0"\nservices:\n  runner:\n    env:\n'
            f"      - {line}\n      - ORG_NAME=testorg\n"
        )
    minted = tmp_path / "runner-sdl.minted.yaml"
    deployed = tmp_path / "deployed-sdl"
    fragment = _mint_fragment(_step("provision")["run"])
    fragment = fragment.replace(TEMPLATE, str(template)).replace(MINTED, str(minted))
    fragment = fragment.replace("/tmp/ja.log", str(tmp_path / "ja.log"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "gh-calls"
    (fake_bin / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'printf "%s" "$FAKE_RESP"\nexit "$FAKE_RC"\n',
        encoding="utf-8",
    )
    # The deploy stub copies the SDL it was handed, so the test sees exactly what would be sent.
    (fake_bin / "fake-ja").write_text(
        "#!/usr/bin/env bash\n"
        'while [ $# -gt 0 ]; do if [ "$1" = --sdl ]; then shift; '
        f'cp "$1" "{deployed}"; printf "%s\\n" "$1" > "{deployed}.path"; fi; shift; done\n'
        'echo "DSEQ: 12345"\n',
        encoding="utf-8",
    )
    for tool in ("gh", "fake-ja"):
        (fake_bin / tool).chmod(0o755)
    if rc is None:
        rc = 0 if status is not None and 200 <= status < 300 else 1
    output = tmp_path / "output"
    script = tmp_path / "attempt.sh"
    script.write_text(
        "set -uo pipefail\n"
        "attempt=1\nCREATED_DSEQ=\nREQUIRED_DEPOSIT_USD=5\n"
        "SELECT_ARGS=()\nPROV_ARGS=(--provider p)\n"
        "JA=(fake-ja)\n" + fragment,
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_RESP": _response(status, token),
        "FAKE_RC": str(rc),
        "GH_TOKEN": PAT,
        "ORG": "testorg",
        "GITHUB_OUTPUT": str(output),
    }
    # Actions runs a `run:` step as `bash -e {0}`.
    proc = subprocess.run(
        ["bash", "-e", str(script)], env=env, capture_output=True, text=True, timeout=30
    )
    assert "unexpected EOF" not in proc.stderr, proc.stderr
    path_file = tmp_path / "deployed-sdl.path"
    return SimpleNamespace(
        proc=proc,
        deployed=deployed.read_text(encoding="utf-8") if deployed.exists() else None,
        deployed_path=path_file.read_text().strip() if path_file.exists() else None,
        minted_left=minted.exists(),
        template=template.read_text(encoding="utf-8") if template.exists() else "",
        output=output.read_text(encoding="utf-8") if output.exists() else "",
        calls=calls.read_text(encoding="utf-8").splitlines() if calls.exists() else [],
        minted_path=str(minted),
    )


# --------------------------------------------------------------------------- the real mint path


def test_a_201_mint_deploys_the_minted_token_and_never_the_pat(tmp_path):
    r = _run_attempt(tmp_path, status=201)

    assert r.proc.returncode == 0, r.proc.stderr
    assert r.calls == ["api --method POST orgs/testorg/actions/runners/registration-token -i"]
    assert r.deployed is not None and r.deployed_path == r.minted_path, "deploy got another SDL"
    runner_env = yaml.safe_load(r.deployed)["services"]["runner"]["env"]
    assert f"RUNNER_TOKEN={TOKEN}" in runner_env, runner_env
    assert PLACEHOLDER not in r.deployed and PAT not in r.deployed
    assert not r.minted_left, "the per-attempt SDL carrying the token outlived the deploy"
    assert PLACEHOLDER in r.template and TOKEN not in r.template, (
        "the shared template got the token"
    )
    assert TOKEN not in r.output and PAT not in r.output, (
        "a credential was written to GITHUB_OUTPUT"
    )
    log = r.proc.stdout + r.proc.stderr
    assert f"::add-mask::{TOKEN}" in log
    assert log.count(TOKEN) == 1, "the token was printed somewhere other than its ::add-mask::"


def test_a_200_mint_is_accepted(tmp_path):
    r = _run_attempt(tmp_path, status=200)

    assert r.proc.returncode == 0, r.proc.stderr
    assert r.deployed is not None and f"RUNNER_TOKEN={TOKEN}" in r.deployed


@pytest.mark.parametrize(
    ("status", "token", "rc", "placeholder"),
    [
        (202, TOKEN, 0, True),
        (204, None, 0, True),
        (401, None, 1, True),
        (403, None, 1, True),
        (404, None, 1, True),
        (422, None, 1, True),
        (201, None, 0, True),
        (201, "AB&CD", 0, True),
        (201, TOKEN, 1, True),
        (None, None, 1, True),
        (201, TOKEN, 0, False),
        (201, TOKEN, 0, None),
    ],
    ids=[
        "202",
        "204",
        "401",
        "403",
        "404",
        "422",
        "201-without-token",
        "201-non-alphanumeric-token",
        "201-but-gh-exited-nonzero",
        "no-response",
        "template-without-placeholder",
        "template-missing",
    ],
)
def test_anything_but_a_usable_mint_refuses_before_that_attempt_deploys(
    tmp_path, status, token, rc, placeholder
):
    r = _run_attempt(tmp_path, status=status, token=token, rc=rc, placeholder=placeholder)

    assert r.proc.returncode == 1, (r.proc.returncode, r.proc.stderr)
    assert "failure_reason=RUNNER_TOKEN_UNMINTED" in r.output, r.output
    assert "deployment_outcome=no-deployment" in r.output, (
        "a refused first attempt deployed nothing"
    )
    assert r.deployed is None, "the deploy ran without a usable minted token"
    assert "::error title=Runner registration token not minted" in r.proc.stdout + r.proc.stderr
    assert PAT not in r.proc.stdout + r.proc.stderr


# --------------------------------------------------------------------------- structure


def _sdl_heredoc(render_run: str) -> str:
    code = _code(render_run)
    start = code.index(f"cat > {TEMPLATE} <<SDL\n")
    return code[start : code.index("\nSDL\n", start)]


def test_the_rendered_sdl_carries_only_the_placeholder_and_no_pat():
    render = _step("render")
    sdl = _sdl_heredoc(render["run"])

    assert sdl.count(f"RUNNER_TOKEN={PLACEHOLDER}") == 1, sdl
    for forbidden in ("GH_RUNNER_PAT", "ACCESS_TOKEN", "GH_TOKEN", "${RUNNER_TOKEN}"):
        assert forbidden not in sdl, f"{forbidden} is in the rendered runner SDL"
    env = render.get("env") or {}
    assert len(env) >= 5, env
    assert not any("GH_RUNNER_PAT" in str(v) for v in env.values()), "render still holds the PAT"


def test_the_token_is_masked_before_its_first_use_and_never_published():
    lines = _code(_mint_fragment(_step("provision")["run"])).splitlines()
    mask = [i for i, line in enumerate(lines) if 'echo "::add-mask::${RUNNER_TOKEN}"' in line]
    assert len(mask) == 1, mask
    before = [line.strip() for line in lines[: mask[0]] if "RUNNER_TOKEN" in line]
    assert len(before) == 2 and before[0] == 'RUNNER_TOKEN=""', before
    assert before[1].startswith('if [[ "$RESP" =~ ')
    assert 'RUNNER_TOKEN="${BASH_REMATCH[1]}"' in before[1]
    token_var = re.compile(r"\$\{?RUNNER_TOKEN\}?(?![A-Z_])")
    code = _code(_step("provision")["run"])
    writes = [
        line for line in code.splitlines() if "GITHUB_OUTPUT" in line or "GITHUB_ENV" in line
    ]
    assert len(writes) >= 10, len(writes)
    assert not any(token_var.search(line) for line in writes)


def _steps_expression_env_violations(doc: dict) -> list[str]:
    violations = []
    for where, env in [("workflow", doc.get("env") or {})] + [
        (f"job {name}", job.get("env") or {}) for name, job in doc["jobs"].items()
    ]:
        violations += [
            f"{where} env {key}" for key, value in env.items() if "steps." in str(value)
        ]
    for name, job in doc["jobs"].items():
        for key, value in (job.get("outputs") or {}).items():
            if re.search(r"(?i)token", key) or re.search(
                r"steps\.\w+\.outputs\.\w*token", str(value), re.I
            ):
                violations.append(f"job {name} output {key}")
    return violations


def test_no_steps_value_is_hoisted_into_job_or_workflow_env_or_a_token_output():
    assert len(DOC["jobs"]) >= 2
    assert _steps_expression_env_violations(DOC) == []


def test_the_env_leg_fires_when_the_token_is_hoisted_to_job_env():
    mutated = copy.deepcopy(DOC)
    mutated["jobs"]["pool"]["env"] = {
        "RUNNER_TOKEN": "${{ steps.provision.outputs.runner_token }}"
    }
    assert _steps_expression_env_violations(mutated), (
        "hoisting the token to job env went unnoticed"
    )


def test_the_pat_preflight_still_owns_the_pat_failure_reasons():
    """blazing's cleanup reads RUNNER_PAT_* as "nothing was deployed" because only the preflight
    emits it (Borduas-Holdings/blazing tests/test_runner_registration_cleanup.py). The mint's
    refusal is a different reason, and the job output still leads with the preflight."""
    fragment = _code(_mint_fragment(_step("provision")["run"]))
    assert "RUNNER_PAT_" not in fragment
    assert "RUNNER_TOKEN_UNMINTED" in fragment
    output = POOL["outputs"]["failure_reason"]
    assert output.startswith("${{ steps.pat.outputs.failure_reason ||"), output


# -------------------------------------------------------------------------- the per-attempt window


def _window_violations(provision_run: str) -> list[str]:
    code = _code(provision_run)
    violations = []
    loop = code.find("for attempt in $(seq 1")
    mint = code.find(
        'RESP=$(gh api --method POST "orgs/${ORG}/actions/runners/registration-token"'
    )
    deploys = [m.start() for m in re.finditer(r'"\$\{JA\[@\]\}" deploy ', code)]
    if loop < 0 or mint < 0 or len(deploys) != 1:
        return [f"cannot locate the attempt: loop {loop}, mint {mint}, deploys {deploys}"]
    if not loop < mint < deploys[0]:
        violations.append(
            "the registration token is not minted inside the attempt loop before its deploy"
        )
    deploy_end = code.index("\n", code.index("\n", deploys[0]) + 1)
    deploy_line = code[deploys[0] : deploy_end]
    if f"--sdl {MINTED}" not in deploy_line:
        violations.append("the deploy is not handed the per-attempt minted SDL")
    bid = [int(v) for v in re.findall(r"--bid-wait(?:-retry)? (\d+)", deploy_line)]
    clamp = re.search(
        r'\[ "\$RUNNER_WAIT_TRIES" -le (\d+) \] \|\| \{.*?RUNNER_WAIT_TRIES=(\d+); \}', code
    )
    if len(bid) != 2:
        return [*violations, f"cannot read the bid windows from the deploy: {deploy_line!r}"]
    if clamp is None or clamp.group(1) != clamp.group(2):
        return [
            *violations,
            "runner-wait-tries is not clamped, so the runners-online wait is unbounded",
        ]
    window = sum(bid) + int(clamp.group(1)) * 5
    bound = (REGISTRATION_TOKEN_TTL_MINUTES - WINDOW_MARGIN_MINUTES) * 60
    if window > bound:
        violations.append(
            f"one attempt's mint -> runners-online window allows {window} s, over {bound} s"
        )
    return violations


def test_each_attempt_mints_its_own_token_and_its_window_fits_the_ttl():
    assert _window_violations(_step("provision")["run"]) == []


def test_the_window_leg_fires_when_the_mint_moves_before_the_attempt_loop():
    run = _step("provision")["run"]
    fragment = _mint_fragment(run)
    loop = "for attempt in $(seq 1"
    assert run.count(loop) == 1
    moved = run.replace(fragment, "").replace(loop, fragment + loop, 1)
    assert any("not minted inside the attempt loop" in v for v in _window_violations(moved))


def test_the_window_leg_fires_when_the_wait_clamp_is_raised_or_removed():
    run = _step("provision")["run"]
    raised = run.replace("-le 480 ] ||", "-le 600 ] ||").replace(
        "RUNNER_WAIT_TRIES=480; }", "RUNNER_WAIT_TRIES=600; }"
    )
    assert any("over 3000 s" in v for v in _window_violations(raised))
    clamp_line = next(line for line in run.splitlines() if '[ "$RUNNER_WAIT_TRIES" -le ' in line)
    removed = run.replace(clamp_line + "\n", "")
    assert any("not clamped" in v for v in _window_violations(removed))
