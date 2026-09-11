"""A pin in ANOTHER repository cannot be checked by local git — so nothing was checking these.

`test_runner_pool_workflow.py::test_the_nested_teardown_pin_is_reachable_from_main` guards the
INTERNAL pin (`runner-pool.yml` -> `runner-teardown.yml`) with `merge-base --is-ancestor`, and it
does so well: it materialises the object, DEEPENS `main` first — a depth-1 `main` makes the walk
answer NO for a genuine ancestor, a false ORPHAN report — and fails under CI rather than skipping.
⇒ That guard stays authoritative for the internal pin. This file does not touch it.

⛔ BUT IT READS ONE HARD-CODED PIN, and this repository has three::

    runner-pool.yml:1430        just-akash/runner-teardown.yml@…          guarded there
    reap-stale-runners.yml      akash-github-runner/…-reaper.yml@…        guarded HERE
    runner-conformance.yml      akash-github-runner/…-conformance.yml@…   guarded HERE

⚠ AND THE LOCAL TECHNIQUE CANNOT BE EXTENDED TO THEM. `merge-base` runs against THIS checkout;
ancestry inside `akash-github-runner` is unanswerable from here without cloning a second
repository. That is the whole reason this file uses the forge instead — reaching a pin that lives
elsewhere, NOT replacing a working local check.

★ WHY IT MATTERS: an orphaned cross-repo pin fails the same way as just-akash#309 — GitHub Actions
cannot resolve the `uses:` while building the job graph, so every caller dies as a STARTUP FAILURE:
a run with ZERO jobs, no logs, no annotation, rendering as a grey X indistinguishable from a blip.

⛔ FETCHABILITY IS NOT THE PREDICATE. `refs/pull/*/head` is retained indefinitely, so a
squash-orphaned commit keeps returning HTTP 200 from the contents/raw APIs **forever** — measured
on `d5e64da8`, the pin whose orphaning caused #309, which still fetches today. Ancestry is what
Actions requires: `compare/{default}...{sha}` with `identical` or `behind`.

⛔ AND A 404 IS AMBIGUOUS ACROSS REPOSITORIES, which is why every repo gets a positive control
first. A repo-scoped token reads a repository it cannot see as `404 {"message": "Not Found"}` —
byte-identical to a missing object. ⇒ Deciding from the 404 alone reports a credential fault as a
broken pin, and the remedy that suggests is a re-pin, which is the outage. The internal pin never
had this problem: a repo-scoped token always reads its own repository.

⚠ Every non-404 failure is UNMEASURED, never a negative. A 403 secondary rate limit keys on
request RATE, not quota, and reporting it as "not an ancestor" would name a healthy pin as
orphaned — again suggesting the re-pin that causes the outage.
"""

from __future__ import annotations

import email.message
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

THIS_REPO = ("Digital-Frontier-LDA", "just-akash")

_PIN = re.compile(
    r"(?P<owner>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9._-]+)"
    r"/(?P<path>\.github/workflows/[A-Za-z0-9._-]+\.ya?ml)"
    r"@(?P<sha>[0-9a-f]{40})"
)

ANCESTOR_STATUSES = frozenset({"identical", "behind"})
RETRYABLE = frozenset({403, 429, 500, 502, 503, 504})
ATTEMPTS = 3

# ⚠ Measured 2026-09-11: 3 pins across 2 repositories in this tree. The floor is on the
# discovered population, because a moved directory or a changed extension yields an empty scan
# and "every cross-repo pin is an ancestor" then reports a clean audit of nothing.
MIN_WORKFLOW_FILES = 8
MIN_CROSS_REPO_PINS = 2


def _hdrs() -> email.message.Message:
    """A real `Message`, not `{}`: `_api` reads `Retry-After` off it and pyright rejects a dict."""
    return email.message.Message()


class Unauthorised(RuntimeError):
    """The credential cannot see the repository — nothing is known about its pins."""


class Unmeasured(RuntimeError):
    """The API would not answer. NOT a verdict about any pin."""


def _token() -> str | None:
    for name in ("GH_PIN_AUDIT_TOKEN", "GH_RUNNER_PAT", "GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value
    return None


def _api(path: str) -> dict:
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "just-akash-cross-repo-pin-ancestry/1.0",
            **({"Authorization": f"Bearer {_token()}"} if _token() else {}),
        },
    )
    last = ""
    for attempt in range(ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise Unauthorised(f"404 for {path}") from exc
            last = f"HTTP {exc.code} for {path}"
            if exc.code not in RETRYABLE or attempt == ATTEMPTS - 1:
                raise Unmeasured(last) from exc
            delay = float(exc.headers.get("Retry-After") or 0) or 2.0 * (2**attempt)
            time.sleep(min(delay, 30.0))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc} for {path}"
            if attempt == ATTEMPTS - 1:
                raise Unmeasured(last) from exc
            time.sleep(2.0 * (2**attempt))
    raise Unmeasured(last or f"exhausted {ATTEMPTS} attempts for {path}")


def _workflow_files() -> list[pathlib.Path]:
    return sorted(WORKFLOWS.glob("*.y*ml"))


def _cross_repo_pins() -> list[tuple[str, str, str, str]]:
    """(owner, repo, path, sha) for pins into a DIFFERENT repository.

    ⚠ Comment lines are excluded: `runner-teardown.yml` documents its own `uses:` line in a
    header comment, and auditing documentation as if it were a call makes the guard fail on prose.

    ⇒ Self-pins are excluded too — deliberately. They are covered by the local `merge-base` guard
    in `test_runner_pool_workflow.py`, which is strictly better for them: it runs on PRs and
    distinguishes "not yet on main" from "orphaned", which an API ancestry check cannot.
    """
    found: dict[tuple[str, str, str, str], None] = {}
    for workflow in _workflow_files():
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("#"):
                continue
            for match in _PIN.finditer(line):
                key = (match["owner"], match["repo"], match["path"], match["sha"])
                if (key[0], key[1]) != THIS_REPO:
                    found[key] = None
    return list(found)


_PROBE: dict[tuple[str, str], str] = {}


def _assert_credential_sees(owner: str, repo: str) -> str:
    """⛔ POSITIVE CONTROL, DERIVED. `compare/{default}...{default}` must be `identical`.

    Derived from the artefact rather than a hard-coded ancestor SHA, so it cannot go stale — and
    it is the only thing separating "this object does not exist" from "this token cannot see this
    repository", which the API reports identically.

    ⚠ One probe per REPOSITORY, not per pin: the control costs two calls, and repeating it per pin
    multiplies this guard's request RATE, which is what reaches the secondary limit.
    """
    cached = _PROBE.get((owner, repo))
    if cached is not None:
        return cached
    try:
        branch = str(_api(f"/repos/{owner}/{repo}").get("default_branch") or "main")
        control = str(
            _api(f"/repos/{owner}/{repo}/compare/{branch}...{branch}").get("status") or ""
        )
    except Unmeasured as exc:
        pytest.fail(
            f"UNMEASURED for {owner}/{repo}: {exc}. ⛔ NOT a verdict about any pin — the API "
            f"would not answer after {ATTEMPTS} attempts. ⚠ Do NOT re-pin on this: a wrong pin "
            f"bump is what took every caller down in #309."
        )
    except Unauthorised as exc:
        pytest.fail(
            f"UNAUTHORISED for {owner}/{repo}: {exc}. ⇒ This says NOTHING about the pins. A "
            f"repo-scoped GITHUB_TOKEN reads a cross-repo API as 404, byte-identical to a missing "
            f"object, so provision a token that can read {owner}/{repo}. ⚠ Do NOT 'fix' this by "
            f"treating 404 as a bad pin — that reports a credential fault as an outage."
        )
    assert control == "identical", (
        f"the derived control for {owner}/{repo} returned {control!r}, expected 'identical'. "
        f"Comparing a branch with itself must be identical; if it is not, this instrument is "
        f"broken and its verdicts about the pins below mean nothing."
    )
    _PROBE[(owner, repo)] = branch
    return branch


def test_workflow_file_floor() -> None:
    """FILE-population floor. A moved directory returns no files, the pin scan comes back empty,
    and a clean audit of nothing is indistinguishable from a clean audit."""
    files = _workflow_files()
    assert len(files) >= MIN_WORKFLOW_FILES, (
        f"scanned {len(files)} workflow files under {WORKFLOWS}, floor is {MIN_WORKFLOW_FILES} — "
        f"the directory or the glob moved, and every assertion below is then vacuous."
    )


def test_there_are_cross_repo_pins_to_check() -> None:
    """MATCH-population floor, a different failure from an empty glob with the same clean zero."""
    sites = _cross_repo_pins()
    assert len(sites) >= MIN_CROSS_REPO_PINS, (
        f"found {len(sites)} cross-repo workflow pins, floor is {MIN_CROSS_REPO_PINS}. Either the "
        f"detector broke (fix it) or the last cross-repo pin was removed (then this file can go). "
        f"⚠ Do not lower this to 0 — a zero here is indistinguishable from a broken rule."
    )


@pytest.mark.parametrize(
    "owner,repo,path,sha",
    _cross_repo_pins(),
    ids=[f"{r}/{p.split('/')[-1]}@{s[:8]}" for _, r, p, s in _cross_repo_pins()],
)
def test_each_cross_repo_pin_is_an_ancestor(owner: str, repo: str, path: str, sha: str) -> None:
    if not _token():
        pytest.skip(
            "no cross-repo credential; tracked by test_the_cross_repo_credential_is_configured"
        )
    branch = _assert_credential_sees(owner, repo)
    try:
        status = str(_api(f"/repos/{owner}/{repo}/compare/{branch}...{sha}").get("status") or "")
    except Unmeasured as exc:
        pytest.fail(
            f"UNMEASURED for {owner}/{repo}@{sha[:8]}: {exc}. ⛔ NOT a verdict about this pin — "
            f"the control for this repo succeeded, so the credential is fine. ⚠ Do NOT re-pin."
        )
    except Unauthorised:
        pytest.fail(
            f"{owner}/{repo}@{sha[:8]} returned 404 while the derived control for the SAME "
            f"repository succeeded. ⇒ The credential can read this repo, so this is the OBJECT: "
            f"the commit does not exist — a typo in a hand-edited SHA, or a force-push. Every run "
            f"calling it startup-fails with ZERO jobs and no logs."
        )
    assert status in ANCESTOR_STATUSES, (
        f"{owner}/{repo}/{path}@{sha} is {status!r} — NOT an ancestor of {branch}. It probably "
        f"still FETCHES (a squash-merged PR head lives in refs/pull forever, HTTP 200), "
        f"but Actions will refuse to resolve it and every caller startup-fails with zero jobs — "
        f"just-akash#309 exactly. ⇒ Re-pin to a commit on {branch}."
    )


# ⛔ A REAL COMMIT THAT IS NOT AN ANCESTOR — the known-negative, in the repo actually pinned.
# akash-github-runner PR#75's head. Measured 2026-09-11: the contents API returns it (200) and
# `compare main...e8e7b4f6` is `diverged`. ⇒ Fetchable forever via refs/pull, and unusable.
# Do NOT swap this for an impossible SHA to make the test hermetic: an impossible SHA exercises
# the 404 path, which is the branch that already worked. This one exercises the branch that
# actually caused #309.
ORPHANED_FIXTURE = (
    "Digital-Frontier-LDA",
    "akash-github-runner",
    # pragma: allowlist secret -- a PUBLIC git commit SHA on akash-github-runner (PR#75 head),
    # not a credential. detect-secrets classifies any 40-char hex as a Hex High Entropy String.
    # Allowlisting the LINE follows the idiom already used in runner-conformance.yml:36.
    "e8e7b4f644b4c9e5c35bc23a5afd69a2ee58c8e2",  # pragma: allowlist secret
)


def test_the_predicate_can_actually_fail() -> None:
    """KNOWN-NEGATIVE. A fetchable-but-orphaned commit must be reported as a non-ancestor.

    ⚠ Without this, a predicate that silently returned `behind` for everything would make every
    assertion above vacuous — the failure mode this whole file is about.
    """
    if not _token():
        pytest.skip(
            "no cross-repo credential; tracked by test_the_cross_repo_credential_is_configured"
        )
    owner, repo, sha = ORPHANED_FIXTURE
    branch = _assert_credential_sees(owner, repo)
    try:
        status = str(_api(f"/repos/{owner}/{repo}/compare/{branch}...{sha}").get("status") or "")
    except Unauthorised:
        # ⛔ MUST NOT SKIP. A skip here is the control quietly doing nothing, which is the exact
        # defect this file exists to catch — and it already fired once: an early draft carried a
        # FABRICATED 40-hex fixture (a real short hash padded with invented digits), the lookup
        # 404'd, this branch skipped, and the suite reported 8 passed / 1 skipped as though the
        # known-negative had run. A control that can silently abstain is not a control.
        pytest.fail(
            f"the known-orphaned fixture {owner}/{repo}@{sha[:8]} is not resolvable (404) while "
            f"the control for the same repository succeeded. ⇒ The SHA is wrong or the commit was "
            f"removed. Pick another REAL non-ancestor commit — a merged PR head works, because "
            f"refs/pull retention keeps it fetchable — and do NOT replace it with an impossible "
            f"SHA, which only exercises the 404 path that already worked."
        )
    assert status not in ANCESTOR_STATUSES, (
        f"the known-orphaned fixture {sha[:8]} reported {status!r}, which this file treats as a "
        f"PASS. Either it reached {branch} since (pick another non-ancestor commit) or the "
        f"predicate stopped discriminating — in which case every assertion above is vacuous."
    )


class TestAnUnanswerableRequestIsNotAVerdict:
    """⛔ CLASSIFICATION, PINNED — HERMETIC, so it can be mutated without touching the network.

    ⚠ Mutation-testing anything that hits an external API poisons its own matrix: a mutation run
    inside a throttled window returns VOID, not negative, and reads exactly like a guard that
    failed to catch it. Measured in blazing#1106, where three runs in a minute tripped the
    SECONDARY limit while the core budget sat at 4513/5000.
    """

    @staticmethod
    def _raise(code: int):
        def _open(*_a, **_k):
            raise urllib.error.HTTPError(
                url="https://api.github.com/x", code=code, msg="stub", hdrs={}, fp=None
            )

        return _open

    def test_a_403_is_UNMEASURED_not_a_missing_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(urllib.request, "urlopen", self._raise(403))
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        with pytest.raises(Unmeasured):
            _api("/repos/x/y")

    def test_a_404_is_still_UNAUTHORISED(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """COUNTERWEIGHT. Widening the classification must not swallow the 404 case — that one IS
        actionable, and collapsing the two loses the credential diagnosis."""
        monkeypatch.setattr(urllib.request, "urlopen", self._raise(404))
        with pytest.raises(Unauthorised):
            _api("/repos/x/y")

    def test_a_retryable_code_is_actually_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []

        def _open(*_a, **_k):
            calls.append(1)
            raise urllib.error.HTTPError(
                url="https://api.github.com/x", code=403, msg="s", hdrs={}, fp=None
            )

        monkeypatch.setattr(urllib.request, "urlopen", _open)
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        with pytest.raises(Unmeasured):
            _api("/repos/x/y")
        # ⛔ THE LITERAL FLOOR IS NOT REDUNDANT. Asserting only `len(calls) == ATTEMPTS` compares
        # the behaviour against the very constant that controls it, so `ATTEMPTS = 1` satisfies it
        # and the retry disappears with the test still green — caught by mutating exactly that in
        # blazing#1106. A retry means MORE THAN ONE attempt: a claim about the world.
        assert ATTEMPTS >= 2, f"ATTEMPTS is {ATTEMPTS}; a single attempt is not a retry"
        assert len(calls) == ATTEMPTS, f"tried {len(calls)} times, expected {ATTEMPTS}"

    def test_a_non_retryable_code_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """401 is a settled answer, not a busy one. Retrying it burns budget against the very
        limit this class exists to survive."""
        calls: list[int] = []

        def _open(*_a, **_k):
            calls.append(1)
            raise urllib.error.HTTPError(
                url="https://api.github.com/x", code=401, msg="s", hdrs={}, fp=None
            )

        monkeypatch.setattr(urllib.request, "urlopen", _open)
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        with pytest.raises(Unmeasured):
            _api("/repos/x/y")
        assert len(calls) == 1, f"tried {len(calls)} times; 401 is settled and must not be retried"


# ⛔ THE PRECONDITION IS RATCHETED, NOT ASSERTED — and the distinction is the whole design.
#
# The ancestry legs above need a credential that can read ANOTHER repository. This repo has no
# such secret wired into `Run unit tests` (it passes no env at all), and the only cross-org token
# present is `GH_RUNNER_PAT` — an org-admin PAT. ⇒ Handing that to `pytest tests/`, which runs
# arbitrary test code, is a privilege decision for this repository's maintainers, not something a
# guard should quietly arrange for itself. This file does not ask for it.
#
# ⚠ SO WHY NOT JUST FAIL UNDER CI, AS THE SIBLING GUARDS DO? Because `Run unit tests` gates every
# PR here. A guard that cannot pass without a secret that does not exist would redden every
# unrelated PR until someone provisioned it — and the predictable outcome is that the guard is
# deleted, not that the token appears. That is worse than not having it.
#
# ⇒ AND A BARE SKIP WOULD BE THE ABSTENTION-AS-SUCCESS DEFECT. So the skip is paired with this:
# a strict xfail on the PRECONDITION. It reports XFAIL on every run — visible and counted, never
# silent — and the moment a credential IS configured it starts passing, which `strict=True` turns
# into a FAILURE demanding this marker be removed and the guard switched on.
#
# ⇒ Net effect: nobody's PR is blocked, the gap is never invisible, and the fix is self-announcing.
@pytest.mark.xfail(
    strict=True,
    reason=(
        "No cross-repo credential is wired into the unit-test job, so the ancestry legs above "
        "SKIP. When one is provisioned this test passes, strict=True turns that into a failure, "
        "and whoever sees it should delete this marker — the guard is then live."
    ),
)
def test_the_cross_repo_credential_is_configured() -> None:
    assert _token(), (
        "no GH_PIN_AUDIT_TOKEN / GH_RUNNER_PAT / GITHUB_TOKEN visible to the tests. ⚠ A "
        "repo-scoped GITHUB_TOKEN is NOT sufficient: it reads a cross-repo API as 404, "
        "byte-identical to a missing object, which would report a healthy pin as orphaned."
    )
