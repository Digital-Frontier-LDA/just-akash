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
    script = 'set -uo pipefail\nSAW_BID=1\nSAW_SEQ_CONTENTION=0\n' + _verdict_block()
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
    [("401", "RUNNER_PAT_INVALID"), ("403", "GITHUB_API_UNAVAILABLE"), ("429", "GITHUB_API_UNAVAILABLE")],
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
    """No new vocabulary: a caller keying on failure_reason must not meet a surprise."""
    doc = yaml.safe_load(WORKFLOW.read_text())
    declared = str(((doc.get("on") or doc.get(True))["workflow_call"]["outputs"])["failure_reason"])
    emitted = {
        line.split("failure_reason=")[1].split('"')[0]
        for line in _verdict_block().splitlines()
        if "failure_reason=" in line
    }
    missing = sorted(r for r in emitted if r not in declared)
    assert not missing, f"emitted but never declared to callers: {missing}"
