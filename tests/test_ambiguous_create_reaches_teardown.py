"""A later ambiguous create must not be masked by the earlier, closed lease (#348).

Round 1 creates dseq 1001 and proves it closed. Round 2's create returns NO_DSEQ_RETURNED:
the POST may have committed a second deployment with no identity. The pool keeps 1001
as the last KNOWN lease and flags create_ambiguous. Teardown must close and prove 1001, and
still fail HELD with CREATE_OUTCOME_AMBIGUOUS, because closing what is known cannot prove
that nothing else exists.

This runs the REAL provision block, resolves the REAL caller `with:` wiring in
runner-pool.yml, and runs the REAL teardown close step with those inputs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from tests.test_owner_less_teardown_is_held import GROUP, run_close
from tests.test_runner_pool_outcome_is_monotonic import OWNER, deploys, last, run_step

POOL = Path(__file__).resolve().parents[1] / ".github/workflows/runner-pool.yml"
NO_DSEQ = json.dumps({"type": "akash-diag", "level": "error", "code": "NO_DSEQ_RETURNED"})

_PLAIN = re.compile(r"\$\{\{ needs\.pool\.outputs\.([a-z_]+) \}\}")
_GATED = re.compile(
    r"\$\{\{ needs\.pool\.outputs\.([a-z_]+) == '([a-z-]+)' "
    r"&& needs\.pool\.outputs\.([a-z_]+) \|\| '' \}\}"
)


def caller_inputs(pool_outputs: dict[str, str]) -> dict[str, str]:
    """Resolve runner-pool.yml's teardown `with:` against the pool's outputs. Only the two
    expression shapes this boundary has used are understood; anything else fails."""
    doc = yaml.safe_load(POOL.read_text(encoding="utf-8"))
    resolved = {}
    for name, expr in doc["jobs"]["teardown"]["with"].items():
        expr = str(expr)
        if m := _PLAIN.fullmatch(expr):
            resolved[name] = pool_outputs.get(m.group(1), "")
        elif m := _GATED.fullmatch(expr):
            ok = pool_outputs.get(m.group(1), "") == m.group(2)
            resolved[name] = pool_outputs.get(m.group(3), "") if ok else ""
        elif "needs.pool.outputs" not in expr:
            resolved[name] = expr
        else:
            raise AssertionError(f"unrecognised teardown input expression for {name}: {expr}")
    return resolved


def test_a_later_ambiguous_create_closes_the_known_lease_and_still_holds(tmp_path: Path) -> None:
    pool_dir, teardown_dir = tmp_path / "pool", tmp_path / "teardown"
    pool_dir.mkdir()
    teardown_dir.mkdir()
    pool = run_step(
        pool_dir,
        {
            "owner": OWNER,
            "verify": {"1001": "closed"},
            "destroy": {"1001": "ok"},
            "rounds": [{"dseq": "1001"}, {"text": NO_DSEQ}],
        },
    )
    assert pool["rc"] != 0 and deploys(pool) == 2, pool["calls"]
    assert last(pool, "failure_reason") == "CREATE_OUTCOME_AMBIGUOUS", pool["writes"]
    outputs = {k: v for k, v in pool["writes"]}  # last write wins per key
    outputs["deployment_group"] = GROUP  # published by the render step, not this block

    inputs = caller_inputs(outputs)
    rc, calls, out, log = run_close(
        teardown_dir,
        wallet=inputs.get("wallet-address", ""),
        group=inputs.get("deployment-group", ""),
        dseq=inputs.get("dseq", ""),
        deployment_outcome=inputs.get("deployment-outcome", "no-deployment"),
        create_ambiguous=inputs.get("create-ambiguous", ""),
    )

    def called(sub: str) -> list[list[str]]:
        return [c for c in calls if c[0] == sub and "1001" in c]

    assert called("destroy"), f"the known lease 1001 was never closed: {calls}"
    assert called("verify-closed"), f"the known lease 1001 was never proven closed: {calls}"
    assert out.get("closed") == ["true"], out
    assert rc != 0, "an ambiguous later create must never read as a clean teardown"
    assert out.get("held_reason") == ["CREATE_OUTCOME_AMBIGUOUS"], (out, log[-1500:])
