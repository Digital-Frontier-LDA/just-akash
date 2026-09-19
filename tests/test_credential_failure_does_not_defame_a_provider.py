"""A credential that dies mid-run must not be recorded as a provider's fault.

`RUNNER_NEVER_REGISTERED` is the only verdict in this workflow that writes a durable
accusation about a third party: `runner_candidates.py` documents `runner_deny` as
"leases but never schedules the runner pod -- NEVER try it". A provider carrying it is
excluded from selection permanently.

⛔ THE SAME OBSERVABLE HAS TWO CAUSES. The runner container mints its registration token
with the SAME `GH_RUNNER_PAT` the pool polls with. If that credential dies during the
~15-minute window, the container 403s and crash-loops while our polls keep succeeding
and keep reporting zero online. From the pool's vantage that is byte-identical to a
provider that leased and never scheduled -- and the provider is the one that gets the
record.

⚠ THE REPO ALREADY GUARDS THE OTHER CASES, and this module is deliberately narrow
because of that. The preflight fails fast on a PAT that is invalid BEFORE provisioning;
the poll loop refuses to fold an unreadable listing into "zero runners" and reports
GITHUB_API_UNAVAILABLE instead, whose declared purpose is that folding it into
RUNNER_NEVER_REGISTERED "would runner_deny providers for our own rate limit". Neither
covers a credential that stops working DURING the window while the listing still reads.
The check is one-shot; the failure is not.

⇒ The fix is one request at the one instant the two causes are still distinguishable.

⛔ THESE TESTS ASSERT ON THE MARKER, NEVER ON THE EXIT CODE. Every branch here exits 1,
so an exit-code assertion passes with the fix deleted.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "runner-pool.yml"
START = 'if [ "$SAW_BID" = "1" ]; then'


def _verdict_block() -> str:
    """The real if/elif/else verdict, extracted verbatim and executed below."""
    lines = WORKFLOW.read_text().splitlines(True)
    start = next((i for i, ln in enumerate(lines) if ln.strip() == START), None)
    assert start is not None, "the verdict block moved -- this file is now blind"
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].strip() == "fi" and (len(lines[i]) - len(lines[i].lstrip())) == indent
        ),
        None,
    )
    assert end is not None, "no matching `fi` -- extraction would run a fragment"
    block = "".join(lines[start : end + 1])
    assert "RUNNER_NEVER_REGISTERED" in block, "extracted the wrong block"
    return block


def _run_verdict(http_status: str | None) -> dict[str, str]:
    """Execute the verdict with a stubbed `gh` returning `http_status`.

    Returns the parsed $GITHUB_OUTPUT. `None` stubs a `gh` that fails to produce a
    status line at all.
    """
    tmp = tempfile.mkdtemp()
    bin_ = os.path.join(tmp, "bin")
    os.makedirs(bin_)
    body = (
        f"printf 'HTTP/2.0 {http_status} x\\n\\n'\nexit 0\n"
        if http_status
        else "printf ''\nexit 1\n"
    )
    stub = os.path.join(bin_, "gh")
    with open(stub, "w") as fh:
        fh.write("#!/bin/sh\n" + body)
    os.chmod(stub, 0o755)

    out_file = os.path.join(tmp, "gh_output")
    open(out_file, "w").close()
    script = "set -uo pipefail\nSAW_BID=1\nSAW_SEQ_CONTENTION=0\n" + _verdict_block()
    subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "PATH": bin_ + os.pathsep + os.environ["PATH"],
            "GITHUB_OUTPUT": out_file,
            "ORG": "some-org",
            "MAX_ATTEMPTS": "3",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    parsed = {}
    for line in Path(out_file).read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            parsed[k] = v
    return parsed


def test_the_block_is_actually_extracted() -> None:
    """Non-vacuity: if extraction silently returned nothing, every test below passes."""
    block = _verdict_block()
    assert "SAW_BID" in block and block.count("failure_reason=") >= 3, block[:200]


# --------------------------------------------------------------------------- #
# KNOWN BAD: the credential is dead at verdict time. No provider may be named.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status,expected",
    [
        ("401", "RUNNER_PAT_INVALID"),
        ("403", "GITHUB_API_UNAVAILABLE"),
        ("429", "GITHUB_API_UNAVAILABLE"),
    ],
)
def test_a_dead_credential_is_never_reported_as_a_provider_failure(status, expected) -> None:
    out = _run_verdict(status)
    assert out.get("failure_reason") != "RUNNER_NEVER_REGISTERED", (
        f"HTTP {status} at verdict time was recorded as a PROVIDER failure. That value "
        f"makes the provider a runner_deny candidate for our own credential dying."
    )
    assert out.get("failure_reason") == expected, out


def test_an_unreadable_credential_check_does_not_license_the_accusation() -> None:
    """Unknown is neither innocent nor guilty.

    An unreadable check cannot CLEAR the credential, so it must not license naming a
    provider — the same reason runner_probe.py marks the ambiguous case `unknown`
    rather than `runner_deny`.
    """
    out = _run_verdict(None)
    assert out.get("failure_reason") == "INDETERMINATE", out
    assert out.get("failure_reason") != "RUNNER_NEVER_REGISTERED", out


# --------------------------------------------------------------------------- #
# KNOWN GOOD: a live credential must NOT suppress a real provider verdict.
# --------------------------------------------------------------------------- #


def test_a_live_credential_still_names_the_provider(tmp_path) -> None:
    """A guard that always exonerated would pass every test above and blind the fleet."""
    out = _run_verdict("200")
    assert out.get("failure_reason") == "RUNNER_NEVER_REGISTERED", (
        f"a healthy credential must leave the provider verdict intact, got {out}"
    )


def test_every_reason_this_block_emits_is_already_declared_as_an_output() -> None:
    """No new vocabulary: a caller keying on failure_reason must not meet a surprise.

    Three emission shapes exist in the verdict block:

    1. Literal `failure_reason=<CODE>` — every literal MUST appear in the
       declared `workflow_call.outputs.failure_reason` description.
    2. Dynamic `failure_reason=$<VAR>` — bounded by a `case` statement
       whose arms list every Code enum member AND whose default branch
       emits a literal that IS in the declared set. An arbitrary string
       from a corrupted log file or a future producer must NOT escape
       into the output contract.
    3. Default branch — must emit a literal in the declared set (the
       bucket PROVIDER_CAPACITY is the documented one).

    The Code enum (just_akash/_diagnostics.py) is the closed set of valid
    codes. The cross-repo constraint — every Code must also be in
    blazing's ACCEPTED_ZERO_FAILURE_REASONS or its named-exclusion list —
    is checked at PR-open time, not here. The test's job is the local
    contract: no emission escapes into the declared output set.
    """
    import re

    from just_akash._diagnostics import Code

    doc = yaml.safe_load(WORKFLOW.read_text())
    declared = str(
        ((doc.get("on") or doc.get(True))["workflow_call"]["outputs"])["failure_reason"]
    )
    code_enum = {v for k, v in vars(Code).items() if not k.startswith("_") and isinstance(v, str)}

    block = _verdict_block()
    static_emitted: set[str] = set()
    dynamic_emitted: set[str] = set()
    for line in block.splitlines():
        if "failure_reason=" not in line:
            continue
        rest = line.split("failure_reason=", 1)[1].split('"')[0]
        if rest.startswith("$"):
            dynamic_emitted.add(rest)
        else:
            static_emitted.add(rest)

    missing = sorted(r for r in static_emitted if r not in declared)
    assert not missing, (
        f"literal emissions not in the declared output set: {missing}. "
        f"Every `failure_reason=<CODE>` literal in the verdict block must "
        f"appear in workflow_call.outputs.failure_reason so callers can "
        f"key on the value."
    )

    # Dynamic emissions must be bounded by a `case` statement whose arms
    # list every Code enum member AND whose default branch emits a literal
    # in the declared set. Without the case-guard, an arbitrary string
    # from /tmp/ja.log would escape into the output contract.
    for var in sorted(dynamic_emitted):
        case_match = re.search(
            rf'case "{re.escape(var)}" in(.*?)\besac\b',
            block,
            re.DOTALL,
        )
        assert case_match, (
            f"dynamic emission {var} has no `case` guard in the verdict "
            f"block. Any value read from /tmp/ja.log (or future producer) "
            f"must be validated against the Code enum before emission."
        )
        arm = case_match.group(1)
        missing_codes = sorted(c for c in code_enum if c not in arm)
        assert not missing_codes, (
            f"{var}'s case-guard is missing Code enum members: "
            f"{missing_codes}. A new Code added to just_akash._diagnostics "
            f"without a matching arm here would silently fall through to "
            f"the bucket — exactly the swallow this PR demoted."
        )
        # The default branch (`*)`) must emit a literal in the declared set.
        default_match = re.search(r"\*\)\s*\n(.*?)\n\s*;;", arm, re.DOTALL)
        assert default_match, f"{var}'s case-guard has no `*)` default branch"
        # Strip bash `# comments` so a documentation comment naming
        # `failure_reason=` does not trip the literal-extraction below.
        default_body = re.sub(r"#[^\n]*", "", default_match.group(1))
        default_literal = re.search(r"failure_reason=([A-Z_]+)", default_body)
        assert default_literal, (
            f"{var}'s default branch must emit a literal "
            f"`failure_reason=<CODE>`; got: {default_body.strip()!r}"
        )
        assert default_literal.group(1) in declared, (
            f"{var}'s default fallback ({default_literal.group(1)}) is "
            f"not in the declared output set. PROVIDER_CAPACITY is the "
            f"documented bucket and is in the declared set."
        )


def test_post_diag_is_bounded_by_the_code_enum_with_a_declared_fallback() -> None:
    """The post-loop verdict variable must be validated against the Code enum.

    A separate test from the general declared-output guard because the
    failure shape is different: the Code enum can grow (new members
    added in just_akash._diagnostics.py) and the cross-repo constraint
    needs to be visible at PR-open time, not just at runtime.

    The post-loop case was previously `case "$POST_DIAG"` (this attempt's
    diag read); #389 follow-up made it `case "$EFFECTIVE_DIAG"` so a
    preserved LAST_KNOWN_DIAG from a wiped log can still surface. The
    enum-bounding contract is the same: every Code member must appear in
    the arm, with PROVIDER_CAPACITY as the default fallback.
    """
    import re

    from just_akash._diagnostics import Code

    block = _verdict_block()
    code_enum = {v for k, v in vars(Code).items() if not k.startswith("_") and isinstance(v, str)}

    case_match = re.search(r'case "\$EFFECTIVE_DIAG" in(.*?)\besac\b', block, re.DOTALL)
    assert case_match, (
        "EFFECTIVE_DIAG must be guarded by a `case` statement listing every "
        "Code enum member, with PROVIDER_CAPACITY as the default fallback."
    )
    arm = case_match.group(1)
    for code in sorted(code_enum):
        assert code in arm, (
            f"Code.{code} is missing from EFFECTIVE_DIAG's case-guard. A "
            f"future deploy.py emission of this code would silently "
            f"fall through to PROVIDER_CAPACITY."
        )


def test_diag_code_in_the_inner_loop_is_also_bounded_by_the_code_enum() -> None:
    """The inner-loop DIAG_CODE site must NOT emit an unvalidated value.

    Two emit sites exist for akash-diag codes: the inner-loop branch
    (line ~1354) and the post-loop verdict branch (line ~1783). Both
    read from `diag_last_code /tmp/ja.log` and emit
    `failure_reason=$VAR`. The post-loop site is bounded by the
    test_post_diag_is_bounded_by_the_code_enum_with_a_declared_fallback
    test; the inner-loop site must also be bounded.

    The inner-loop case-guard's default branch (`*)`) MUST NOT emit a
    `failure_reason=` line — an unknown value at attempt N should be
    treated as unclassified and re-tried at attempt N+1, the same as
    the no-DIAG_CODE branch. The bucket is reserved for the post-loop
    verdict (no more attempts remaining) where the producer truly had
    nothing else to say.
    """
    import re

    from just_akash._diagnostics import Code

    code_enum = {v for k, v in vars(Code).items() if not k.startswith("_") and isinstance(v, str)}
    workflow_text = WORKFLOW.read_text(encoding="utf-8")

    case_match = re.search(r'case "\$DIAG_CODE" in(.*?)\besac\b', workflow_text, re.DOTALL)
    assert case_match, (
        "DIAG_CODE in the inner-loop must be guarded by a `case` "
        "statement listing every Code enum member, with no `failure_reason=` "
        "in the default branch."
    )
    arm = case_match.group(1)
    for code in sorted(code_enum):
        assert code in arm, (
            f"Code.{code} is missing from DIAG_CODE's case-guard. An "
            f"unknown value would currently fall through to the raw-output "
            f"branch with no failure_reason — but a future Code added to "
            f"the enum without an arm here would NOT be guarded."
        )

    default_match = re.search(r"\*\)\s*\n(.*?)\n\s*;;", arm, re.DOTALL)
    assert default_match, "DIAG_CODE's case-guard has no `*)` default branch"
    # Strip bash `# comments` so a documentation comment naming
    # `failure_reason=` does not trip the assertion below.
    default_body = re.sub(r"#[^\n]*", "", default_match.group(1))
    assert "failure_reason=" not in default_body, (
        "DIAG_CODE's default branch must NOT emit failure_reason — an "
        "unknown value at attempt N re-tries at N+1; the bucket belongs "
        "only to the post-loop verdict (no attempts remaining)."
    )
