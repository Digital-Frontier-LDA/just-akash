"""The escrow deposit is the only leak bound that needs no actor, and its own input
description said it was something else.

⛔ `required-deposit-usd` is passed to `just-akash deploy --deposit` and NOWHERE ELSE. There
is no balance precondition in runner-pool.yml. The description said "free credit needed
before attempting a deploy" — naming a check that does not exist — so a caller reading the
contract would believe lowering it weakens a safety gate. It does not: it shortens how long a
LEAKED pool keeps billing. Prose stating a rule the code does not implement is the most
recurrent defect across these repos, and here it sat in the one place callers actually read.

⇒ WHY IT MATTERS. Akash closes a deployment when escrow is exhausted, so the deposit is a
self-terminating TTL requiring no reaper, no event, no credential and no correct code — the
only bound that holds when every other layer fails.

MEASURED on chain 2026-09-09 (zero auth, repeatable by anyone): blazing's active stamped
pools each held funds 1,988,192 uact against a lease rate of 69 uact/block = 28,814 blocks
≈ 48.0h, identical across every one. The longest run they provision for is ~2h, so an
undetected leak bills for ~24x the job it served. The chain's own floor is
`min_deposits` = 500,000 uact ≈ 12.1h at that rate.
"""

from __future__ import annotations

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
POOL = ROOT / ".github/workflows/runner-pool.yml"
INPUT = "required-deposit-usd"


def _doc() -> dict:
    return yaml.safe_load(POOL.read_text(encoding="utf-8"))


def _input_spec() -> dict:
    doc = _doc()
    on = doc[True] if True in doc else doc["on"]
    return on["workflow_call"]["inputs"][INPUT]


def test_the_input_still_exists() -> None:
    """POPULATION FLOOR — a renamed input makes every assertion below vacuous."""
    spec = _input_spec()
    assert spec.get("description"), f"{INPUT} has no description to check"


def test_the_deposit_is_used_ONLY_as_the_escrow_deposit() -> None:
    """⇒ The claim the description now makes, checked against the code rather than trusted.

    If a balance precondition is ever added, this test fails and the description must be
    updated with it — which is the point: the contract and the use stay in step.
    """
    text = POOL.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    uses = re.findall(
        rf"\$\{{?REQUIRED_DEPOSIT_USD\}}?|\$\{{\{{\s*inputs\.{re.escape(INPUT)}", code
    )
    assert uses, f"{INPUT} is never used; the description describes nothing"
    # every use of the shell variable must be the --deposit flag
    for m in re.finditer(r"^.*\$\{?REQUIRED_DEPOSIT_USD\}?.*$", code, re.M):
        line = m.group(0)
        if "REQUIRED_DEPOSIT_USD:" in line:
            continue  # the env binding itself
        assert "--deposit" in line, (
            f"{INPUT} is used for something other than --deposit, so the description is now "
            f"wrong: {line.strip()!r}"
        )


def test_the_description_does_not_claim_a_precondition_that_does_not_exist() -> None:
    """⛔ THE ORIGINAL DEFECT. "Free credit needed before attempting a deploy" describes a
    gate runner-pool.yml does not have. A caller who believes it will not touch this value,
    which is exactly the value that decides a leaked pool's lifetime."""
    desc = str(_input_spec()["description"]).lower()
    for phrase in ("free credit needed", "credit needed before", "before attempting a deploy"):
        assert phrase not in desc, (
            f"the description still claims a precondition this workflow does not implement "
            f"({phrase!r}); it is passed only to --deposit"
        )


def test_the_description_says_what_the_value_actually_governs() -> None:
    """⇒ And the positive half, so the test above cannot be satisfied by deleting the
    description entirely."""
    desc = str(_input_spec()["description"]).lower()
    assert "deposit" in desc, f"the description must name what it is: {desc!r}"
    assert any(w in desc for w in ("lifetime", "escrow exhaustion", "how long")), (
        f"the description must say that this bounds a leaked pool's lifetime, which is the "
        f"fact a caller needs in order to size it: {desc!r}"
    )
