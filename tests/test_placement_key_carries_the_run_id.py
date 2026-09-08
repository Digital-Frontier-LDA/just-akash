"""Every pool deployment must be attributable to the run that created it.

⛔ THIS IS THE INVARIANT THAT MAKES A LEAKED POOL IMPOSSIBLE TO CREATE rather than merely
detectable. `placement-key` says WHOSE a deployment is. Until the run id rides with it,
nothing on chain says WHETHER IT IS STILL NEEDED, so every reaper falls back to age — and
age is a proxy. Measured on one shared wallet 2026-09-08: 112 of 128 active deployments
carried no run reference, and a consumer's 6h floor left 69 of 83 pools
protected-and-unreapable at any instant, because a floor used as the PRIMARY rule
guarantees a standing population of `floor × leak-rate`.

⚠ THE SCRIPT UNDER TEST IS READ OUT OF THE SHIPPED WORKFLOW, never retyped here. A test
against a copy passes while the workflow does something else — which is how a guard becomes
decoration.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github/workflows/runner-pool.yml"

# The stamping block, lifted verbatim from the workflow and wrapped in a function so the
# real shell — not an approximation of it — decides each case.
_HARNESS = """
stamp() {{
  PLACEMENT_KEY="$1"; GH_RUN_ID="$2"
{body}
  echo "${{PLACEMENT_KEY}}"
}}
stamp "$1" "$2"
"""


def _stamp_body() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    m = re.search(
        r"^(\s*)case \"\$\{PLACEMENT_KEY\}\" in\n\s*\*-run-\[0-9\]\*\).*?^\1esac\n",
        text,
        re.M | re.S,
    )
    assert m, (
        "the attribution stamp is not present in runner-pool.yml. Without it a pool can be "
        "created that no sweeper can prove is closable, and detection becomes a race."
    )
    block = m.group(0)
    # also take the shape re-assertion that follows it
    rest = text[m.end() :]
    m2 = re.search(r"^(\s*)case \"\$\{PLACEMENT_KEY\}\" in\n.*?^\1esac\n", rest, re.M | re.S)
    assert m2, "the post-stamp shape re-assertion is missing"
    return block + m2.group(0)


def _run(key: str, run_id: str) -> tuple[int, str]:
    script = _HARNESS.format(body=_stamp_body())
    p = subprocess.run(["bash", "-c", script, "bash", key, run_id], capture_output=True, text=True)
    return p.returncode, p.stdout.strip()


def test_the_block_was_actually_found() -> None:
    """POPULATION FLOOR — an empty extraction would make every case below vacuously pass."""
    body = _stamp_body()
    assert "-run-${GH_RUN_ID:-}-end" in body
    assert "*-run-[0-9]*" in body


def test_an_unattributed_key_gets_the_run_id() -> None:
    rc, out = _run("borduas-pool", "34228480597")
    assert rc == 0
    assert out == "borduas-pool-run-34228480597-end"


def test_stamping_is_idempotent_for_callers_already_doing_it() -> None:
    """⛔ THE LOAD-BEARING CASE. blazing#962 stamps caller-side and Blazing-Back has shipped
    `dfci-infra-runner-run-<id>-end` for months. Double-stamping gives them a name their own
    sweeper's `run-(\\d+)` no longer matches — so a callee that "improved" attribution would
    silently DISABLE it for the two consumers already doing it right."""
    for already in ("borduas-runner-run-34228480597-end", "dfci-infra-runner-run-999-end"):
        rc, out = _run(already, "34228480597")
        assert rc == 0
        assert out == already, f"double-stamped {already} -> {out}"


def test_a_non_numeric_run_id_is_refused() -> None:
    """It is interpolated into the SDL heredoc, so a surprising value does not produce a bad
    key — it produces a different document."""
    for bad in ("abc", "1; rm -rf /", "", "12 34"):
        rc, _ = _run("borduas-pool", bad)
        assert rc == 2, f"accepted run id {bad!r}"


def test_the_shape_is_reasserted_after_the_stamp() -> None:
    body = _stamp_body()
    assert body.count('case "${PLACEMENT_KEY}" in') >= 2, (
        "the shape check must run AFTER the stamp; a future change to the stamp must not be "
        "able to restructure the SDL document"
    )


def test_the_docs_no_longer_claim_tag_prefix_carries_the_run_id() -> None:
    """⛔ `tag-prefix` is documented at the top of runner-pool.yml as the reaping key, and it
    reaches nothing: `just-akash tag` writes .tags.json inside the INSTALLED PACKAGE
    DIRECTORY (api.py:31), destroyed by every fresh `uvx` install. Prose asserting a
    capability the code does not have is how a consumer built a sweeper that had never
    reclaimed a lease and reported it was working as designed. See #311."""
    text = WORKFLOW.read_text(encoding="utf-8")
    head = text[: text.index("name: Akash runner pool")]
    assert "so a sweeper can reap this run's lease" not in head, (
        "the tag-prefix docs still claim it carries the run id to a sweeper; the placement "
        "key does that now, and the tag does not reach the chain at all"
    )
