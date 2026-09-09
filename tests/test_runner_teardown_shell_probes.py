"""Review harness: actual teardown YAML, isolated fake transport, desired outcomes.

Reproduces the close step from `.github/workflows/runner-teardown.yml` end-to-end
against a fake `just-akash` transport so the cases director measured at
`e06f52f` on 2026-09-09 (and the chain-verifier cases from the 2026-09-09
review blocker) are pinned as TEST cases here. The harness asserts the FIXED
behaviour:

  - `active`     chain reports state=active on BOTH endpoints — close step
                 MUST exit non-zero with closed != true. The destroy attempt
                 succeeded but the chain still shows an active lease, and
                 verify-closed returns closed=false.
  - `closed`     chain reports state=closed on BOTH endpoints (terminal
                 populations agree) — close step MUST exit 0 with closed=true.
                 The positive path.
  - `console_closed_chain_active` Console API reports state=closed but the
                 chain RPC still shows state=active (the two endpoints
                 disagree). The verifier MUST return closed=false and the
                 close step MUST exit non-zero. A single-channel Console
                 "closed" is not closure proof.
  - `destroy_closed_chain_unavailable` destroy returned "Deployment closed"
                 but the chain RPC was unavailable (couldn't read either
                 endpoint). The verifier MUST return closed=false (it needs
                 two agreeing endpoints to set closed=true) and the close
                 step MUST exit non-zero. Destroy text alone is not proof.
  - `agreeing_terminal` both endpoints agree on a terminal state across the
                 COMPLETE owner/dseq/gseq/oseq/bseq/provider identity map —
                 close step MUST exit 0 with closed=true.

The two controls (`active`, `closed`) pin what good looks like and must
remain green across any future fix. The three false-success cases pin the
FIXED behaviour — they FAIL against `e06f52f3` (which had no chain verifier)
and PASS once the close step invokes `just-akash verify-closed` for closure
proof. The flip is the fix landing.

No live I/O. The fake binary is a Python script that reads per-subcommand
(stdout, stderr, exitcode) from a per-test fixture file and writes to
TASK_CALLS so the test can verify which subcommands were actually invoked.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "runner-teardown.yml"


@pytest.mark.parametrize(
    "case",
    [
        "active",
        "closed",
        "console_closed_chain_active",
        "destroy_closed_chain_unavailable",
        "agreeing_terminal",
        "resolve_owner_unavailable",
        "verifier_false_nonzero",
        "verifier_true_nonzero",
    ],
)
def test_real_close_step(tmp_path, case):
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = [step for step in workflow["jobs"]["teardown"]["steps"] if step.get("id") == "close"]
    assert len(steps) == 1
    script = steps[0]["run"]
    needle = "JA=(uv run --with . just-akash)"
    assert script.count(needle) == 1

    # ⇒ Per-case fixture. Each subcommand the close step invokes has a
    # (stdout, stderr, exitcode) tuple the fake emits. The harness asserts
    # the FIXED behaviour: `closed` and `agreeing_terminal` exit 0 with
    # closed=true; the other three exit non-zero with closed != true.
    cases = {
        # CONTROL: chain reports active on both endpoints. Destroy
        # succeeds; verifier sees state=active, returns closed=false.
        "active": {
            "resolve-owner": [
                json.dumps({"owner": "akash1" + "a" * 38, "dseq": "7", "source": "wallet_pool"}),
                "",
                0,
            ],
            "destroy": ["destroyed", "", 0],
            "verify-closed": [
                '{"closed": false, "sources": ["a", "b"], "reason": "active lease on chain"}',
                "",
                0,
            ],
        },
        # CONTROL: chain reports closed on both endpoints (terminal
        # populations agree). Destroy succeeds; verifier returns closed=true.
        "closed": {
            "resolve-owner": [
                json.dumps({"owner": "akash1" + "a" * 38, "dseq": "7", "source": "wallet_pool"}),
                "",
                0,
            ],
            "destroy": ["destroyed", "", 0],
            "verify-closed": [
                '{"closed": true, "sources": ["a", "b"], "reason": "agreeing terminal states"}',
                "",
                0,
            ],
        },
        # FALSE-SUCCESS (was the #952 defect): Console API reports state=closed
        # but chain RPC still shows state=active. Verifier disagrees, returns
        # closed=false. close step must NOT report closed=true.
        "console_closed_chain_active": {
            "resolve-owner": [
                json.dumps({"owner": "akash1" + "a" * 38, "dseq": "7", "source": "wallet_pool"}),
                "",
                0,
            ],
            "destroy": ["destroyed", "", 0],
            "verify-closed": [
                '{"closed": false, "sources": ["a", "b"], "reason": "endpoints disagree"}',
                "",
                0,
            ],
        },
        # FALSE-SUCCESS: destroy text said "Deployment closed" but chain RPC
        # was unavailable. Verifier could not consult two endpoints, returns
        # closed=false. close step must NOT report closed=true.
        "destroy_closed_chain_unavailable": {
            "resolve-owner": [
                json.dumps({"owner": "akash1" + "a" * 38, "dseq": "7", "source": "wallet_pool"}),
                "",
                0,
            ],
            "destroy": ["Deployment closed", "", 0],
            "verify-closed": ['{"closed": false, "sources": [], "reason": "unverified"}', "", 0],
        },
        # POSITIVE: both endpoints return IDENTICAL terminal populations
        # across the complete owner/dseq/gseq/oseq/bseq/provider map. The
        # verifier's strongest signal — and the only one that closes the
        # lease.
        "agreeing_terminal": {
            "resolve-owner": [
                json.dumps({"owner": "akash1" + "a" * 38, "dseq": "7", "source": "wallet_pool"}),
                "",
                0,
            ],
            "destroy": ["destroyed", "", 0],
            "verify-closed": [
                '{"closed": true, "sources": ["a", "b"], "reason": "agreeing terminal states"}',
                "",
                0,
            ],
        },
        # BASH -e HARD-FAILURE GUARD: resolve-owner exits non-zero BEFORE
        # destroy runs. With bash `-e` enabled (the default, set -uo
        # pipefail does NOT disable it), an unguarded `${JA[@]} resolve-owner ...`
        # aborts the script BEFORE the OWNER_RC capture and BEFORE the
        # closed=unknown write — the harness reproduces this as
        # empty GITHUB_OUTPUT. The fix is `|| OWNER_RC=$?` so the failure
        # is captured and the script can branch. This case pins that fix.
        # destroy and verify-closed must NOT be invoked.
        "resolve_owner_unavailable": {
            "resolve-owner": ["", "no wallet in pool claims that DSEQ", 1],
        },
    }
    for name, value in (("verifier_false_nonzero", False), ("verifier_true_nonzero", True)):
        cases[name] = {
            **cases["closed"],
            "verify-closed": [json.dumps({"closed": value}), "verifier failed", 1],
        }
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(cases[case]))

    # ⇒ Per-test invocation log: the fake JA writes its argv here so the test
    # can assert which subcommands were actually invoked and in what order.
    calls = tmp_path / "calls.jsonl"

    # ⇒ Fake JA transport — pure stdout/stderr/exitcode emission, no file
    # writes. The shell does its own redirects; we just emit what just-akash
    # would emit and let the shell direct the streams.
    fake = tmp_path / "fake.py"
    fake.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        'with Path(os.environ["TASK_CALLS"]).open("a") as log:\n'
        '    log.write(json.dumps(sys.argv[1:]) + "\\n")\n'
        'fixture = json.loads(Path(os.environ["TASK_FIXTURE"]).read_text())\n'
        "sub = sys.argv[1]\n"
        "if sub not in fixture:\n"
        '    sys.stderr.write(f"unknown subcommand: {sub}\\n")\n'
        "    raise SystemExit(2)\n"
        "stdout, stderr, code = fixture[sub]\n"
        "sys.stdout.write(stdout)\n"
        "sys.stderr.write(stderr)\n"
        "raise SystemExit(code)\n"
    )

    # ⇒ Replace the one `JA=...` assignment with the fake binary, neutralise
    # sleep (both standalone `sleep N` lines and inline `[ ... ] || sleep 6`),
    # and rewrite the literal /tmp/... log paths to per-test paths so two
    # tests running in parallel cannot collide on the host filesystem.
    script = script.replace(
        needle,
        f"JA=({shlex.quote(sys.executable)} {shlex.quote(str(fake))})\nsleep() {{ :; }}",
    )
    for old in (
        "/tmp/destroy.log",
        "/tmp/status.json",
        "/tmp/status.err",
        "/tmp/verify.json",
        "/tmp/verify.err",
        "/tmp/owner.json",
        "/tmp/owner.err",
    ):
        if old in script:
            script = script.replace(old, shlex.quote(str(tmp_path / Path(old).name)))

    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        env={
            **os.environ,
            "DSEQ": "7",
            "WALLET_ADDRESS": "akash1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "TAG_PREFIX": "review",
            "GITHUB_OUTPUT": str(output),
            "TASK_CALLS": str(calls),
            "TASK_FIXTURE": str(fixture),
        },
        text=True,
        capture_output=True,
        timeout=10,
    )

    # ⇒ The close step must invoke resolve-owner, destroy, AND verify-closed;
    # a test that never reaches the verifier cannot be measuring what it
    # claims to. The order is also load-bearing: resolve-owner must run
    # BEFORE destroy (a Console 404 after destroy would blind the owner
    # lookup precisely when it matters most), and verify-closed must run
    # AFTER destroy (verifying before destroying would read the pre-destroy
    # state and report success on a lease the next action is about to take).
    # EXCEPTION: the `resolve_owner_unavailable` case exercises the bash
    # `-e` guard — destroy and verify-closed must NOT run when owner
    # resolution fails (the script aborts there, by design).
    invoked = [json.loads(line) for line in calls.read_text().splitlines()]
    subcommands = [call[0] for call in invoked]
    assert "resolve-owner" in subcommands, (
        f"{case}: resolve-owner subcommand never invoked; without capturing the "
        f"owner before destroy, verify-closed cannot be supplied --owner and "
        f"would have to resolve through a wallet pool that may now Console 404"
    )
    # ⇒ resolve-owner must be invoked with --json. The workflow emits JSON
    # and parses it via python3 — without --json the parser would reject
    # the flag (exit 2) and the OWNER capture would be empty. A
    # regression that drops --json from the workflow while the parser
    # still requires it (or vice versa) lands here.
    resolve_call = next(call for call in invoked if call[0] == "resolve-owner")
    assert "--json" in resolve_call, (
        f"{case}: resolve-owner was invoked without --json; the workflow "
        f"reads the owner via python3 JSON parsing on stdout, so the flag "
        f"is load-bearing. argv={resolve_call}"
    )
    assert "--dseq" in resolve_call, (
        f"{case}: resolve-owner was invoked without --dseq; argv={resolve_call}"
    )
    if case == "resolve_owner_unavailable":
        # ⇒ bash `-e` guard: when resolve-owner exits non-zero, the script
        # must abort AT THAT POINT. destroy and verify-closed must NOT
        # be invoked; the captured error must surface in GITHUB_OUTPUT
        # (closed=unknown) — not as an empty file the bash `-e` abort
        # would produce.
        assert "destroy" not in subcommands, (
            f"{case}: destroy must not run when owner resolution failed; "
            f"running destroy on a DSEQ whose owner is unknown is the #184 "
            f"class defect. subcommands={subcommands}"
        )
        assert "verify-closed" not in subcommands, (
            f"{case}: verify-closed must not run when owner resolution "
            f"failed; without an owner the verifier cannot scope its chain "
            f"reads and would either fail open or read another tenant's "
            f"lease. subcommands={subcommands}"
        )
        assert result.returncode != 0, (
            f"{case}: bash `-e` did not abort — the script must exit non-zero "
            f"so the runner step fails. got rc={result.returncode}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        output_text = output.read_text()
        assert output_text.strip() != "", (
            f"{case}: GITHUB_OUTPUT is EMPTY — bash `-e` aborted the script "
            f"BEFORE the closed=unknown write. The `|| OWNER_RC=$?` guard is "
            f"missing or mis-applied. output={output_text!r}"
        )
        reported = dict(line.split("=", 1) for line in output_text.splitlines())
        assert reported.get("closed") == "unknown", (
            f"{case}: bash `-e` abort path must write closed=unknown to "
            f"GITHUB_OUTPUT so the runner's environment block reflects the "
            f"real state. reported={reported!r}"
        )
        return  # ⇒ resolve_owner_unavailable does its own assertions

    assert "destroy" in subcommands, f"{case}: destroy subcommand never invoked"
    assert "verify-closed" in subcommands, f"{case}: verify-closed subcommand never invoked"
    assert subcommands.index("resolve-owner") < subcommands.index("destroy"), (
        f"{case}: resolve-owner must run BEFORE destroy; ordering was {subcommands}"
    )
    assert subcommands.index("destroy") < subcommands.index("verify-closed"), (
        f"{case}: destroy must run BEFORE verify-closed; ordering was {subcommands}"
    )
    # ⇒ The verify-closed call must carry the owner captured by resolve-owner.
    # A regression that drops the --owner handoff would force the verifier to
    # re-resolve through a wallet pool that may now Console 404 (the exact
    # situation resolve-owner exists to prevent).
    verify_call = next(call for call in invoked if call[0] == "verify-closed")
    assert "--owner" in verify_call, (
        f"{case}: verify-closed was invoked without --owner; the workflow is "
        f"trying to re-resolve owner through the wallet pool at verify-time, "
        f"which is exactly the post-destroy Console 404 case resolve-owner "
        f"exists to prevent. argv={verify_call}"
    )
    owner_idx = verify_call.index("--owner")
    assert owner_idx + 1 < len(verify_call), (
        f"{case}: --owner was passed without a value; argv={verify_call}"
    )
    captured_owner = verify_call[owner_idx + 1]
    assert captured_owner.startswith("akash1"), (
        f"{case}: --owner value does not look like an akash1 address: {captured_owner!r}"
    )

    reported = dict(line.split("=", 1) for line in output.read_text().splitlines())

    # ⇒ POSITIVE controls: close step exits 0 with closed=true written to
    # GITHUB_OUTPUT. These two pin what "the lease is closed" looks like.
    if case in ("closed", "agreeing_terminal"):
        assert result.returncode == 0, (
            f"{case}: positive control must exit 0; got {result.returncode}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        assert reported.get("closed") == "true", (
            f"{case}: positive control must report closed=true; got {reported.get('closed')!r}"
        )
    else:
        # ⇒ NEGATIVE cases: close step must NOT report success over an
        # unverified or active lease. Console-closed-alone, destroy-text-
        # alone, and chain-active are all the false-GREEN defects this
        # verifier exists to refuse.
        assert result.returncode != 0, (
            f"{case}: false success — close step exited 0 but the verifier did not "
            f"confirm closure across two endpoints.\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        assert reported.get("closed") == "unknown", (
            f"{case}: false success — close step wrote closed=true to GITHUB_OUTPUT "
            f"but the verifier did not confirm closure across two endpoints.\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
