"""runner-pool.yml recovers the DSEQ of a deployment that a FAILED deploy left behind.

just-akash prints the "DSEQ:" summary only after a full success. A failure that happens
after create_deployment emits a typed diagnostic carrying the dseq instead, and the
workflow's awk reads it back so teardown has an identity to close. Review (CodeRabbit,
a1eab31): CHARTER_DEPOSIT_FAILED is emitted after the create too, so it must be read.

The awk program is taken from the workflow itself and run, not re-typed here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "runner-pool.yml"

pytestmark = pytest.mark.skipif(shutil.which("awk") is None, reason="needs awk")


def _awk_program() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"DSEQ=\$\(awk -F': \+' '\n(.*?)\n\s*' /tmp/ja\.log", text, re.S)
    assert match, "the DSEQ recovery awk was not found in runner-pool.yml"
    return match.group(1)


def _recover(log: str, tmp_path: Path) -> str:
    path = tmp_path / "ja.log"
    path.write_text(log, encoding="utf-8")
    out = subprocess.run(
        ["awk", "-F", ": +", _awk_program(), str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _event(code: str, dseq: str) -> str:
    return json.dumps(
        {"type": "akash_diag", "level": "error", "code": code, "message": "m", "dseq": dseq}
    )


@pytest.mark.parametrize("code", ["LEASE_CREATE_FAILED", "REDEPLOY_FAILED"])
def test_existing_post_create_codes_are_recovered(code, tmp_path):
    assert _recover(_event(code, "4242") + "\n", tmp_path) == "4242"


def test_a_charter_deposit_failure_dseq_is_recovered(tmp_path):
    log = "STEP 5: lease\n" + _event("CHARTER_DEPOSIT_FAILED", "777001") + "\n"
    assert _recover(log, tmp_path) == "777001"


def test_a_pre_create_code_recovers_nothing(tmp_path):
    assert _recover(_event("NO_BIDS_RECEIVED", "5") + "\n", tmp_path) == ""


def test_the_summary_line_still_wins(tmp_path):
    log = _event("CHARTER_DEPOSIT_FAILED", "1") + "\n  DSEQ: 99\n"
    assert _recover(log, tmp_path) == "99"
