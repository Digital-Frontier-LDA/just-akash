"""The provision step's creation outcome is monotonic across retry rounds (#347).

These tests run the REAL `provision` step script end to end: every round, the deploy
call, the destroy loops and the runner poll. The existing outcome tests read the script
as text, and the one test that executes anything runs two lines with DSEQ injected, so
none of them can see a second round. That is how a later round could publish
no-deployment over an earlier created lease.

Only the external commands are stubbed: `uv` (standing in for `just-akash deploy`,
`destroy`, `tag` and `verify-closed`), `gh`, `sleep` and `date`. The script is
otherwise run unchanged, apart from pointing its literal `/tmp/` paths at a
directory per test (asserted complete below), because other checkouts on the same
host run these tests concurrently.

The published outputs are read as the ORDERED list of writes to $GITHUB_OUTPUT, and
every assertion about "the final value" is paired with a check that no write after
`created` (or after a non-empty `dseq`) ever downgrades it. That second check holds
whether GitHub keeps the first write of a key or the last, so these tests do not
depend on how duplicate keys are resolved.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github/workflows/runner-pool.yml"

OWNER = "akash1" + "o" * 38
PROVIDER_A = "akash1" + "a" * 38
PROVIDER_B = "akash1" + "b" * 38
LABEL = "pool-label"

UV_STUB = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
sub = args[args.index("just-akash") + 1]
state = os.environ["STUB_STATE"]
scenario = json.load(open(os.environ["STUB_SCENARIO"]))
def arg(name):
    return args[args.index(name) + 1] if name in args else ""
with open(os.path.join(state, "calls.log"), "a") as log:
    log.write(f"{sub} dseq={arg('--dseq')}\n")
if sub == "deploy":
    path = os.path.join(state, "round")
    n = int(open(path).read()) if os.path.exists(path) else 0
    open(path, "w").write(str(n + 1))
    rounds = scenario["rounds"]
    r = rounds[n] if n < len(rounds) else {"text": "unclassified failure"}
    if r.get("dseq"):
        print(f"DSEQ: {r['dseq']}")
    if r.get("provider"):
        print(f"Provider: {r['provider']}")
    # just-akash prints Wallet only when it has one; "wallet": null models that.
    if r.get("dseq") and r.get("wallet", scenario.get("owner")) is not None:
        print(f"Wallet: {r.get('wallet', scenario.get('owner'))}")
    if r.get("text"):
        print(r["text"])
    sys.exit(0)
if sub == "destroy":
    ok = scenario.get("destroy", {}).get(arg("--dseq"), "fail") == "ok"
    print(f"Deployment {arg('--dseq')} destroyed." if ok else "Error: close failed")
    sys.exit(0 if ok else 1)
if sub == "verify-closed":
    verdict = scenario.get("verify", {}).get(arg("--dseq"), "open")
    modes = {
        "closed": ({"closed": True, "reason": "terminal"}, 0),
        "open": ({"closed": False, "reason": "active"}, 1),
        "disagree": ({"closed": False, "reason": "endpoints disagree"}, 1),
        "rc0-not-closed": ({"closed": False, "reason": "active"}, 0),
        "closed-but-rc1": ({"closed": True, "reason": "terminal"}, 1),
        "string-true": ({"closed": "True", "reason": "terminal"}, 0),
    }
    if verdict == "unreadable":
        sys.exit(1)
    body, rc = modes[verdict]
    print(json.dumps(body)); sys.exit(rc)
sys.exit(0)
"""

GH_STUB = r"""#!/usr/bin/env python3
import json, os, sys
sc = json.load(open(os.environ["STUB_SCENARIO"]))
if "registration-token" in " ".join(sys.argv[1:]):
    code = int(sc.get("token_status", 201))
    print(f"HTTP/2.0 {code} stub\n\n" + ('{"token": "stub-token"}' if code == 201 else "{}"))
    sys.exit(0 if code == 201 else 1)
if sc.get("gh_fail"):
    sys.stderr.write("HTTP 503\n"); sys.exit(1)
runners = []
for i in range(int(sc.get("online", 0))):
    row = {"id": i + 1, "status": "online", "labels": [{"name": os.environ["RUNNER_LABEL"]}]}
    if sc.get("version", "2.330.0") is not None:
        row["version"] = sc.get("version", "2.330.0")
    runners.append(row)
print(json.dumps({"runners": runners}))
"""

DATE_STUB = r"""#!/bin/bash
f="$STUB_STATE/clock"; n=$(cat "$f" 2>/dev/null || echo 1000000); echo $((n + 1)) > "$f"; echo "$n"
"""


def provision_script() -> str:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    matches = [s for s in doc["jobs"]["pool"]["steps"] if s.get("id") == "provision"]
    assert len(matches) == 1, f"expected exactly one provision step, found {len(matches)}"
    return matches[0]["run"]


def test_the_harness_runs_the_whole_provision_block_not_a_fragment() -> None:
    """An extraction that silently grabbed a fragment would still run, and still pass."""
    run = provision_script()
    assert run.count('for attempt in $(seq 1 "$MAX_ATTEMPTS"); do') == 1, "retry loop missing"
    assert run.count('"${JA[@]}" deploy --sdl') == 1, "deploy call missing"
    assert run.count("lease_is_terminal() {") == 1, "terminal-proof predicate missing"
    assert run.lstrip().startswith("set -uo pipefail"), (
        "extraction did not start at the block head"
    )


def modern_bash() -> str:
    """GitHub's runners use bash 5. macOS /bin/bash is 3.2, where an EMPTY array expansion
    such as "${SELECT_ARGS[@]}" is an "unbound variable" error under set -u. The step
    swallows that with `|| true`, so the deploy never runs and every round looks like an
    unclassified failure. A harness failing that way still lets refusal tests pass."""
    bash = shutil.which("bash")
    assert bash, "no bash on PATH"
    version = subprocess.run(
        [bash, "-c", 'echo "${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert (int(version[0]), int(version[1])) >= (4, 4), f"{bash} is bash {version}; need >= 4.4"
    return bash


def run_step(
    tmp_path: Path, scenario: dict, script: str | None = None, env_extra: dict | None = None
) -> dict:
    """Execute the provision step once; return exit code, ordered writes and calls."""
    work = tmp_path / "work"
    stubs = tmp_path / "bin"
    state = tmp_path / "state"
    for d in (work, stubs, state):
        d.mkdir()
    for name, body in (
        ("uv", UV_STUB),
        ("gh", GH_STUB),
        ("date", DATE_STUB),
        ("sleep", "#!/bin/bash\nexit 0\n"),
    ):
        path = stubs / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    source = script if script is not None else provision_script()
    # Check the SOURCE, not the result: the replacement directory can itself contain
    # "/tmp" (macOS TemporaryDirectory is .../T/tmpXXXX), which made a check on the
    # rebased text fail for a reason unrelated to the script.
    assert source.count("/tmp") == source.count("/tmp/") > 0, "a /tmp path would escape the rebase"
    rebased = source.replace("/tmp/", f"{work}/")
    (tmp_path / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "STUB_STATE": str(state),
        "STUB_SCENARIO": str(tmp_path / "scenario.json"),
        "GITHUB_OUTPUT": str(output),
        "AKASH_API_KEY": "stub",  # pragma: allowlist secret
        "AKASH_API_KEYS": "",
        "GH_TOKEN": "stub",
        "PREFERRED_CANDIDATES_CSV": f"{PROVIDER_A},{PROVIDER_B}",
        "FALLBACK_CANDIDATES_CSV": "",
        "STANDING_EXCLUDED_CSV": "",
        "ORG": "org",
        "RUNNER_LABEL": LABEL,
        "POOL_SIZE": "1",
        "MIN_POOL_SIZE": "1",
        "TAG_PREFIX": "pool",
        "RUN_ID": "7",
        "MAX_ATTEMPTS": "3",
        "RUNNER_WAIT_TRIES": "1",
        "EXPECTED_RUNNER_VERSION": "",
        "REQUIRED_DEPOSIT_USD": "5",
        "PROVIDER_SELECT": "",
        "DEPLOYMENT_GROUP": "group",
    }
    env.update(env_extra or {})
    # GitHub runs `run:` steps as `bash -e {0}`; `set -uo pipefail` does not undo -e.
    proc = subprocess.run(
        [modern_bash(), "-e", "-c", rebased],
        env=env,
        text=True,
        capture_output=True,
        cwd=tmp_path,
        timeout=60,
    )
    writes = []
    for line in output.read_text(encoding="utf-8").splitlines():
        if "=" in line and "<<" not in line:
            key, value = line.split("=", 1)
            writes.append((key, value))
    calls_file = state / "calls.log"
    calls = calls_file.read_text().splitlines() if calls_file.exists() else []
    return {
        "rc": proc.returncode,
        "writes": writes,
        "calls": calls,
        "log": proc.stdout + proc.stderr,
    }


def last(result: dict, key: str) -> str | None:
    values = [v for k, v in result["writes"] if k == key]
    return values[-1] if values else None


def assert_monotonic(result: dict) -> None:
    """Holds under first-write-wins AND last-write-wins: nothing downgrades."""
    outcomes = [v for k, v in result["writes"] if k == "deployment_outcome"]
    if "created" in outcomes:
        after = outcomes[outcomes.index("created") :]
        assert set(after) == {"created"}, f"outcome downgraded after created: {outcomes}"
    dseqs = [v for k, v in result["writes"] if k == "dseq"]
    first_real = next((i for i, v in enumerate(dseqs) if v), None)
    if first_real is not None:
        assert all(dseqs[first_real:]), f"an empty dseq followed a real one: {dseqs}"
    identity_pairs(result)


def identity_pairs(result: dict) -> list[tuple[str, str]]:
    """dseq and wallet_address are one record: every dseq write is followed by the
    owner of that same round before any other identity write, and no owner is
    written without its dseq. Returns the published (dseq, owner) pairs in order."""
    ident = [(k, v) for k, v in result["writes"] if k in ("dseq", "wallet_address")]
    keys = [k for k, _ in ident]
    assert keys == ["dseq", "wallet_address"] * (len(keys) // 2), (
        f"dseq and owner were not published as pairs: {ident}"
    )
    return [(ident[i][1], ident[i + 1][1]) for i in range(0, len(ident), 2)]


def deploys(result: dict) -> int:
    return sum(1 for c in result["calls"] if c.startswith("deploy "))


# ── positive control: the harness reaches the loop and a real success ─────────────


def test_control_a_healthy_first_round_publishes_created(tmp_path: Path) -> None:
    """⛔ Precondition for every refusal below. If this fails, they may pass without
    the harness ever reaching the deploy loop."""
    r = run_step(
        tmp_path,
        {"owner": OWNER, "online": 1, "rounds": [{"dseq": "1001", "provider": PROVIDER_A}]},
    )
    assert r["rc"] == 0, r["log"][-2000:]
    assert deploys(r) == 1
    assert last(r, "deployment_outcome") == "created"
    assert last(r, "dseq") == "1001"
    assert last(r, "provision_healthy") == "true"
    assert_monotonic(r)


# ── S1: a later no-broadcast branch never downgrades created ─────────────────────


def test_s1_created_then_a_later_402_still_publishes_created(tmp_path: Path) -> None:
    r = run_step(
        tmp_path,
        {
            "owner": OWNER,
            "verify": {"1001": "closed"},
            "destroy": {"1001": "ok"},
            "rounds": [
                {"dseq": "1001"},  # orphan: no provider
                {"text": "PaymentRequiredError: HTTP 402"},
            ],  # round 2: no DSEQ
        },
    )
    assert deploys(r) == 2, r["calls"]
    assert r["rc"] != 0
    assert last(r, "deployment_outcome") == "created", r["writes"]
    assert last(r, "dseq") == "1001"
    assert_monotonic(r)


def test_s1b_created_then_no_eligible_provider_still_publishes_created(tmp_path: Path) -> None:
    """Both candidates get excluded by post-provider discards; the next round has no bidder."""
    r = run_step(
        tmp_path,
        {
            "owner": OWNER,
            "online": 0,
            "verify": {"1001": "closed", "1002": "closed"},
            "destroy": {"1001": "ok", "1002": "ok"},
            "rounds": [
                {"dseq": "1001", "provider": PROVIDER_A},
                {"dseq": "1002", "provider": PROVIDER_B},
            ],
        },
    )
    assert deploys(r) == 2, r["calls"]
    assert last(r, "failure_reason") == "NO_ELIGIBLE_BIDDER", r["writes"]
    assert last(r, "deployment_outcome") == "created"
    assert_monotonic(r)


# ── S2: an unproven close stops the loop and keeps the lease's identity ─────────


@pytest.mark.parametrize(
    "verdict",
    ["open", "unreadable", "disagree", "rc0-not-closed", "closed-but-rc1", "string-true"],
)
def test_s2_unproven_orphan_close_stops_before_another_deploy(
    tmp_path: Path, verdict: str
) -> None:
    r = run_step(
        tmp_path,
        {
            "owner": OWNER,
            "verify": {"1001": verdict},
            "rounds": [{"dseq": "1001"}, {"dseq": "1002", "provider": PROVIDER_A}],
        },
    )
    assert deploys(r) == 1, f"a second deploy followed an unproven close: {r['calls']}"
    assert r["rc"] != 0
    assert last(r, "failure_reason") == "LEASE_CLOSE_UNVERIFIED"
    assert last(r, "deployment_outcome") == "created"
    assert last(r, "dseq") == "1001"
    assert_monotonic(r)


def test_s2_unproven_post_provider_close_stops_before_another_deploy(tmp_path: Path) -> None:
    """The post-provider retry edge: fewer runners online than min-pool-size."""
    r = run_step(
        tmp_path,
        {
            "owner": OWNER,
            "online": 0,
            "destroy": {"1001": "ok"},  # destroy SAYS it worked...
            "verify": {"1001": "open"},  # ...but the chain disagrees
            "rounds": [
                {"dseq": "1001", "provider": PROVIDER_A},
                {"dseq": "1002", "provider": PROVIDER_B},
            ],
        },
    )
    assert deploys(r) == 1, f"a second deploy followed an unproven close: {r['calls']}"
    assert last(r, "failure_reason") == "LEASE_CLOSE_UNVERIFIED"
    assert last(r, "dseq") == "1001"
    assert_monotonic(r)


def _empty_version_projection(tmp_path: Path) -> dict:
    """jq that drops only the version projection, so ids and versions disagree."""
    wrap = tmp_path / "jq-wrap"
    wrap.mkdir()
    real = shutil.which("jq")
    assert real, "jq is required to reach the empty-projection edge"
    jq = wrap / "jq"
    jq.write_text(
        "#!/bin/bash\n"
        'case "$*" in *".version"*) cat >/dev/null; exit 0 ;; esac\n'
        f'exec {real} "$@"\n',
        encoding="utf-8",
    )
    jq.chmod(0o755)
    return {"PATH": f"{wrap}:{tmp_path / 'bin'}:{os.environ['PATH']}"}


VERSION_EDGES = {
    "empty-version-projection": ({}, "version projection empty"),
    "dead-runner-version": ({"version": None}, "Landing gate: registered runners are not running"),
}


@pytest.mark.parametrize("edge", sorted(VERSION_EDGES))
@pytest.mark.parametrize("verdict", ["open", "closed"])
def test_s2_version_gate_retry_edges_go_through_the_proof(
    tmp_path: Path, edge: str, verdict: str
) -> None:
    """The two landing-gate discards, reached through the real poll: an unproven close
    stops at one deploy; a proven close continues to the next attempt."""
    extra, warning = VERSION_EDGES[edge]
    env = _empty_version_projection(tmp_path) if edge == "empty-version-projection" else None
    r = run_step(
        tmp_path,
        {
            "owner": OWNER,
            "online": 1,
            **extra,
            "destroy": {"1001": "ok"},
            "verify": {"1001": verdict},
            "rounds": [
                {"dseq": "1001", "provider": PROVIDER_A},
                {"dseq": "1002", "provider": PROVIDER_B},
            ],
        },
        env_extra=env,
    )
    assert warning in r["log"], r["log"][-2000:]
    assert "verify-closed dseq=1001" in r["calls"], r["calls"]
    if verdict == "open":
        assert deploys(r) == 1, f"a second deploy followed an unproven close: {r['calls']}"
        assert last(r, "failure_reason") == "LEASE_CLOSE_UNVERIFIED"
        assert last(r, "dseq") == "1001"
    else:
        assert deploys(r) == 2, r["calls"]
        assert last(r, "dseq") == "1002"
    assert last(r, "deployment_outcome") == "created"
    assert_monotonic(r)


# ── S3 (must stay green): a proven close does allow the next attempt ─────────────


def test_s3_a_proven_close_permits_the_next_attempt(tmp_path: Path) -> None:
    """Guards against a fix that simply stops every retry. The second deploy must come
    from lease_is_terminal returning proven, not from bypassing it."""
    r = run_step(
        tmp_path,
        {
            "owner": OWNER,
            "online": 1,
            "verify": {"1001": "closed"},
            "destroy": {"1001": "ok"},
            "rounds": [{"dseq": "1001"}, {"dseq": "1002", "provider": PROVIDER_A}],
        },
    )
    assert "verify-closed dseq=1001" in r["calls"], "S3 must pass through the proof"
    assert deploys(r) == 2, r["calls"]
    assert r["rc"] == 0, r["log"][-2000:]
    assert last(r, "dseq") == "1002"
    assert last(r, "deployment_outcome") == "created"
    assert_monotonic(r)


# ── S4: the published dseq is the unproven lease's, never a later empty value ────


def test_s4_a_later_empty_round_never_blanks_the_published_dseq(tmp_path: Path) -> None:
    r = run_step(
        tmp_path,
        {
            "owner": OWNER,
            "verify": {"1001": "closed"},
            "destroy": {"1001": "ok"},
            "rounds": [{"dseq": "1001"}, {"text": "unclassified"}, {"text": "unclassified"}],
        },
    )
    assert deploys(r) == 3, r["calls"]
    assert last(r, "dseq") == "1001", r["writes"]
    assert last(r, "deployment_outcome") == "created"
    assert_monotonic(r)


def test_s5a_an_empty_round_preserves_the_published_dseq_owner_pair(
    tmp_path: Path,
) -> None:
    """#348: teardown passes --expected-owner only for a non-empty wallet_address."""
    r = run_step(
        tmp_path,
        {
            "owner": OWNER,
            "verify": {"1001": "closed"},
            "destroy": {"1001": "ok"},
            "rounds": [{"dseq": "1001"}, {"text": "unclassified"}],
        },
    )
    assert deploys(r) == 3, r["calls"]
    assert identity_pairs(r) == [("1001", OWNER)], r["writes"]
    assert_monotonic(r)


def test_s5b_a_new_dseq_without_a_wallet_never_keeps_the_old_owner_and_stops(
    tmp_path: Path,
) -> None:
    """A later round that prints DSEQ but no Wallet must not inherit the previous
    round's owner, and must stop before any close or another deploy."""
    r = run_step(
        tmp_path,
        {
            "owner": OWNER,
            "verify": {"1001": "closed"},
            "destroy": {"1001": "ok"},
            "rounds": [
                {"dseq": "1001"},
                {"dseq": "1002", "provider": PROVIDER_A, "wallet": None},
                {"dseq": "1003", "provider": PROVIDER_B},
            ],
        },
    )
    assert r["rc"] != 0
    assert deploys(r) == 2, f"a deploy followed an unreadable owner: {r['calls']}"
    assert identity_pairs(r) == [("1001", OWNER), ("1002", "")], r["writes"]
    assert last(r, "failure_reason") == "LEASE_OWNER_UNREADABLE", r["writes"]
    assert last(r, "deployment_outcome") == "created"
    assert not [c for c in r["calls"] if c.endswith("dseq=1002") and not c.startswith("deploy")], (
        f"an owner-less lease was acted on: {r['calls']}"
    )
    assert_monotonic(r)


# ── S6: every exit reached after a lease was created keeps created ───────────────


@pytest.mark.parametrize(
    ("scenario_extra", "reason"),
    [
        ({"gh_fail": True, "online": 1}, "GITHUB_API_UNAVAILABLE"),
        ({"token_status": 401}, "RUNNER_PAT_INVALID"),
        ({"token_status": 429}, "GITHUB_API_UNAVAILABLE"),
        ({"token_status": 500}, "INDETERMINATE"),
        ({"token_status": 201}, "RUNNER_NEVER_REGISTERED"),
    ],
    ids=[
        "listing-unreadable",
        "verdict-401",
        "verdict-429",
        "verdict-unknown",
        "never-registered",
    ],
)
def test_s6_a_post_create_exit_never_publishes_no_deployment(
    tmp_path: Path, scenario_extra: dict, reason: str
) -> None:
    """The removed #345 ratchet pinned "no post-submit no-deployment write" as text.
    This runs each post-create failure exit instead, so a write inserted on any of
    these paths is observed, whatever its wording or position."""
    r = run_step(
        tmp_path,
        {
            "owner": OWNER,
            "destroy": {"1001": "ok"},
            "verify": {"1001": "closed"},
            "rounds": [{"dseq": "1001", "provider": PROVIDER_A}],
            **scenario_extra,
        },
        env_extra={"MAX_ATTEMPTS": "1"},
    )
    assert r["rc"] != 0
    assert deploys(r) == 1, r["calls"]
    assert last(r, "failure_reason") == reason, r["writes"]
    outcomes = [v for k, v in r["writes"] if k == "deployment_outcome"]
    assert "no-deployment" not in outcomes and outcomes[-1] == "created", outcomes
    assert last(r, "dseq") == "1001"
    assert last(r, "wallet_address") == OWNER
    assert_monotonic(r)


def test_s6_created_then_wallet_tx_contention_never_publishes_no_deployment(
    tmp_path: Path,
) -> None:
    """The one post-create exit S6's single-round shape cannot reach: contention is
    classified before SAW_BID, so it needs an earlier created (and proven-closed) lease."""
    r = run_step(
        tmp_path,
        {
            "owner": OWNER,
            "destroy": {"1001": "ok"},
            "verify": {"1001": "closed"},
            "rounds": [{"dseq": "1001"}, {"text": "account sequence mismatch"}],
        },
        env_extra={"MAX_ATTEMPTS": "2"},
    )
    assert r["rc"] != 0
    assert deploys(r) == 2, r["calls"]
    assert last(r, "failure_reason") == "WALLET_TX_CONTENTION", r["writes"]
    outcomes = [v for k, v in r["writes"] if k == "deployment_outcome"]
    assert "no-deployment" not in outcomes and outcomes[-1] == "created", outcomes
    assert identity_pairs(r) == [("1001", OWNER)], r["writes"]
    assert_monotonic(r)


# ── #347 acceptance: no-deployment still means something, unknown stays unknown ──


@pytest.mark.parametrize(
    ("env_extra", "rounds", "expected_deploys", "expected_reason"),
    [
        ({}, [{"text": "PaymentRequiredError: HTTP 402"}], 1, "WALLET_UNDERFUNDED"),
        ({"STANDING_EXCLUDED_CSV": f"{PROVIDER_A},{PROVIDER_B}"}, [], 0, "NO_ELIGIBLE_BIDDER"),
        ({"PROVIDER_SELECT": "not-a-policy"}, [], 0, None),
    ],
    ids=["authoritative-payment-refusal", "no-eligible-provider", "invalid-provider-policy"],
)
def test_every_known_no_broadcast_branch_still_publishes_no_deployment(
    tmp_path: Path, env_extra: dict, rounds: list, expected_deploys: int, expected_reason
) -> None:
    """Replaces #345's text checks with the real branches: each is reached by running
    the step, and each publishes no-deployment only because no DSEQ ever existed."""
    r = run_step(tmp_path, {"owner": OWNER, "rounds": rounds}, env_extra=env_extra)
    assert r["rc"] != 0
    assert deploys(r) == expected_deploys, r["calls"]
    assert last(r, "deployment_outcome") == "no-deployment", r["writes"]
    assert last(r, "failure_reason") == expected_reason
    assert_monotonic(r)


@pytest.mark.parametrize(
    "rounds",
    [[{"text": "Insufficient balance"}], [{"text": "x"}, {"text": "y"}, {"text": "z"}]],
    ids=["generic-insufficient-wording", "unclassified-until-exhausted"],
)
def test_unknown_evidence_never_becomes_no_deployment(tmp_path: Path, rounds: list) -> None:
    r = run_step(tmp_path, {"owner": OWNER, "rounds": rounds})
    assert deploys(r) == len(rounds)
    assert last(r, "deployment_outcome") == "unknown", r["writes"]


@pytest.mark.parametrize(
    "evidence",
    [
        '{"type":"akash-diag","level":"error","code":"NO_DSEQ_RETURNED"}',
        '{"type":"akash-diag","level":"error","code":"DEPLOY_CREATE_FAILED"}',
        "No DSEQ returned from API",
    ],
)
def test_ambiguous_create_stops_before_a_second_non_idempotent_request(
    tmp_path: Path, evidence: str
) -> None:
    r = run_step(
        tmp_path,
        {"owner": OWNER, "rounds": [{"text": evidence}, {"dseq": "1002"}]},
    )
    assert r["rc"] != 0
    assert deploys(r) == 1, r["calls"]
    assert last(r, "deployment_outcome") == "unknown", r["writes"]
    assert last(r, "failure_reason") == "CREATE_OUTCOME_AMBIGUOUS", r["writes"]
    assert last(r, "dseq") is None


# ── the call site: every retry edge goes through the one proof ───────────────────


def test_every_retry_edge_after_a_destroy_calls_lease_is_terminal() -> None:
    """A correct predicate that one edge bypasses is the call-site gap. Every attempt-loop
    `continue` that follows a destroy must be preceded by the proof, and so must the
    orphan close. Derived from the script, not from a list of line numbers."""
    lines = provision_script().splitlines()
    destroy_loops = [
        i for i, line in enumerate(lines) if line.strip().startswith("for _try in 1 2 3; do")
    ]
    assert len(destroy_loops) >= 4, "destroy loops disappeared; re-derive this test"
    guarded, unguarded, exits = [], [], []
    for start in destroy_loops:
        end = next(i for i in range(start, len(lines)) if lines[i].strip() == "done")
        # The first control transfer after the loop decides the path. A plain `exit 1`
        # keeps this round's DSEQ and created; a retry needs the proof before it.
        follow = next(
            i
            for i in range(end + 1, len(lines))
            if "continue" in lines[i] or "exit 1" in lines[i] or "lease_is_terminal" in lines[i]
        )
        if "lease_is_terminal" in lines[follow]:
            guarded.append(start)
        elif "exit 1" in lines[follow]:
            exits.append(start)
        else:
            unguarded.append(start)
    assert not unguarded, f"destroy loops that reach a retry without the proof: {unguarded}"
    assert len(guarded) == 4, f"expected the four retry edges from #347, found {len(guarded)}"
    assert len(exits) == 1, f"expected one destroy that exits directly, found {len(exits)}"
