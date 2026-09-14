"""Nested self-pins: a workflow here calling another workflow HERE by full path and ref.

⛔ WHY THE PIN EXISTS AT ALL. `uses: ./.github/workflows/runner-teardown.yml` resolves in the
CALLER's tree, so every cross-repo consumer of `runner-pool.yml` died as a startup failure with
zero jobs (just-akash#247, measured 2026-09-03 on Borduas-Holdings/blazing; akash-github-runner#149
was the same bug one repo over). #248 replaced it with the full path at a pinned SHA.

⛔ WHY A PINNED SELF-CALL NEEDS TWO RULES, AND WHICH EVENT OWNS EACH (just-akash#364).
A pin into a PR's own branch looks fine for the whole life of the PR and is orphaned by the squash
merge: #348 → 16be7deb left `runner-pool.yml` pinning 1ad3913b, and the old guard only went red on
main afterwards, because it let any ancestor of HEAD through — and a PR-only commit always is one.

    pull_request   every pin must be an ancestor of the BASE branch (no HEAD escape). Identity
                   against the BASE's copy applies only when the PR itself changes the called
                   file or the pin line; an unrelated PR skips it with a note naming main's
                   pending repin, so one teardown-editing merge does not turn every open PR red.
    anything else  every pin must be an ancestor of main, and the pinned file must be
                   byte-identical to the working copy. When a merged change edits the called
                   file this goes red on main by design and names the one-line repin.

Identity and base-ancestry cannot BOTH hold inside one squash-merged PR that edits the called
file — no base commit carries the new bytes until after the merge — which is why the identity
comparison target moves with the event instead of forcing the bump into the same PR.

⚠ A SHALLOW CLONE REPORTS GOOD PINS AS ORPHANED. `merge-base --is-ancestor` answers NO when the
parent history is simply absent, so ancestry is never decided before the base is fetched
explicitly and the repository is proven not shallow.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

SELF_REPO = "Digital-Frontier-LDA/just-akash"
_SELF_USES = re.compile(
    re.escape(SELF_REPO) + r"/(?P<path>\.github/workflows/[A-Za-z0-9._-]+\.ya?ml)@(?P<ref>[^\s@]+)"
)


@dataclass(frozen=True)
class SelfPin:
    workflow: str
    job: str
    path: str
    ref: str


def discover_self_pins(workflows: dict[str, object]) -> list[SelfPin]:
    """Every `uses:` in every parsed workflow that calls this repository by full path.

    Parsed, not grepped: comments that quote a `uses:` line are not pins.
    """
    pins: list[SelfPin] = []
    for name, doc in sorted(workflows.items()):
        jobs = (doc or {}).get("jobs") if isinstance(doc, dict) else None
        for job_name, job in (jobs or {}).items():
            if not isinstance(job, dict):
                continue
            candidates = [job.get("uses")]
            steps = [step for step in job.get("steps") or [] if isinstance(step, dict)]
            candidates += [step.get("uses") for step in steps]
            for uses in candidates:
                match = _SELF_USES.fullmatch(str(uses)) if uses else None
                if match:
                    pins.append(SelfPin(name, str(job_name), match["path"], match["ref"]))
    return pins


def pin_format_violations(pins: list[SelfPin]) -> list[str]:
    """Every self-pin names an immutable 40-hex commit: never a branch, never a movable tag."""
    return [
        f"{pin.workflow}:{pin.job} pins {pin.path}@{pin.ref}, not a 40-hex commit SHA. A branch "
        "or tag can move under every caller; pin the exact commit."
        for pin in pins
        if not re.fullmatch(r"[0-9a-f]{40}", pin.ref)
    ]


def git(root: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, timeout=180, check=check
    )  # bytes: identity is asserted on BYTES, and decoding would hide a line-ending change


def pull_request_base() -> str | None:
    """The base branch when this is a pull_request run, else None. Set by Actions only then."""
    return os.environ.get("GITHUB_BASE_REF") or None


def base_branch() -> str:
    """The branch every pin is judged against: the PR's base on a pull request, else main."""
    return pull_request_base() or "main"


def identity_target(reference: str) -> str | None:
    """What the pinned file must equal: the BASE copy on a pull request, else the working copy."""
    return reference if pull_request_base() else None


def reference_ref(root: Path, branch: str) -> str:
    """The ref ancestry and base identity are decided against: the BASE branch, never HEAD."""
    for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
        if git(root, "rev-parse", "--verify", "--quiet", ref).returncode == 0:
            return ref
    raise LookupError(f"no ref for branch {branch!r}")


def resolve_commit(root: Path, ref: str) -> str | None:
    done = git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return done.stdout.decode().strip() if done.returncode == 0 else None


def reachability_violations(root: Path, pins: list[SelfPin], base: str) -> list[str]:
    """Pins that are not ancestors of `base`. There is no escape for ancestors of HEAD."""
    violations = []
    for pin in pins:
        where = f"{pin.workflow}:{pin.job}"
        commit = resolve_commit(root, pin.ref)
        if commit is None:
            violations.append(f"{where} pins {pin.ref[:12]}, which does not resolve")
        elif git(root, "merge-base", "--is-ancestor", commit, base).returncode != 0:
            violations.append(
                f"{where} pins {pin.ref[:12]}, which is not an ancestor of {base}. A commit "
                "that exists only on this branch is orphaned by a squash merge, and every "
                "caller then dies as a startup failure with zero jobs. Pin a commit already "
                f"on {base}."
            )
    return violations


def identity_violations(root: Path, pins: list[SelfPin], against: str | None) -> list[str]:
    """Pins whose called file differs from `against` (a ref), or from the working copy if None."""
    violations = []
    for pin in pins:
        where = f"{pin.workflow}:{pin.job}"
        pinned = git(root, "show", f"{pin.ref}:{pin.path}")
        if pinned.returncode != 0:
            violations.append(f"{where}: {pin.path} unreadable at {pin.ref[:12]}")
            continue
        if against is None:
            current = (root / pin.path).read_bytes()
        else:
            shown = git(root, "show", f"{against}:{pin.path}")
            current = shown.stdout if shown.returncode == 0 else None
        if pinned.stdout == current:
            continue
        if against is None:
            head = resolve_commit(root, "HEAD") or "<this commit>"
            violations.append(
                f"{where} calls {pin.path} as it was at {pin.ref[:12]}, which differs from "
                "the working copy, so callers run a STALE copy. Repin in a follow-up change: "
                f"`uses: {SELF_REPO}/{pin.path}@{head}`"
            )
        else:
            violations.append(
                f"{where} calls {pin.path} as it was at {pin.ref[:12]}, which differs from "
                f"{against}. This PR changes {pin.path} or its pin: keep the pin at the base copy "
                "and repin in a follow-up after merge."
            )
    return violations


def _is_shallow(root: Path) -> bool:
    return git(root, "rev-parse", "--is-shallow-repository").stdout.strip() == b"true"


class ShallowHistory(RuntimeError):
    """Ancestry cannot be decided: the history it needs is not present."""


def prepare_history(root: Path, branch: str, pins: list[SelfPin]) -> str:
    """Fetch the base explicitly, prove the repository is not shallow, materialise every pin.

    Returns the reference ref. Raises ShallowHistory rather than let a partial history report
    a genuine ancestor as orphaned.
    """
    git(root, "fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    if _is_shallow(root) and git(root, "fetch", "--quiet", "--unshallow", "origin").returncode:
        git(root, "fetch", "--quiet", "--deepen=100000", "origin")
    if _is_shallow(root):
        raise ShallowHistory("still shallow after --unshallow; ancestry is undecidable")
    for pin in pins:
        # ⚠ NO --depth HERE: a depth-limited fetch re-shallows the repository just proven full.
        if resolve_commit(root, pin.ref) is None and re.fullmatch(r"[0-9a-f]{40}", pin.ref):
            git(root, "fetch", "--quiet", "origin", pin.ref)
    return reference_ref(root, branch)


def pr_touches_pin(root: Path, pin: SelfPin, base: str) -> bool:
    """Whether this PR changes the called file, or adds or moves this pin, relative to `base`."""
    fork = git(root, "merge-base", base, "HEAD")
    if fork.returncode != 0:
        return True  # undecidable: check identity rather than skip it
    start = fork.stdout.decode().strip()
    changed = git(root, "diff", "--name-only", start, "HEAD").stdout.decode().split()
    if pin.path in changed:
        return True
    shown = git(root, "show", f"{start}:.github/workflows/{pin.workflow}")
    if shown.returncode != 0:
        return True  # the workflow is new in this PR
    return pin not in discover_self_pins({pin.workflow: yaml.safe_load(shown.stdout)})


def identity_scope(root: Path, pins: list[SelfPin], base: str) -> tuple[list[SelfPin], list[str]]:
    """(pins whose identity is checked, notes for the ones skipped).

    Off a pull request every pin is checked. On one, only pins this PR touches: after a squash
    merge that edited the called file, main is red until its repin lands, and an unrelated PR
    must not inherit that red. Reachability is NOT scoped this way; it applies to every PR.
    """
    if not pull_request_base():
        return list(pins), []
    checked, notes = [], []
    for pin in pins:
        if pr_touches_pin(root, pin, base):
            checked.append(pin)
            continue
        pinned = git(root, "show", f"{pin.ref}:{pin.path}")
        current = git(root, "show", f"{base}:{pin.path}")
        drift = (
            pinned.returncode != 0 or current.returncode != 0 or pinned.stdout != current.stdout
        )
        notes.append(
            f"identity not checked for {pin.workflow}:{pin.job}: this PR does not change "
            f"{pin.path} or its pin."
            + (
                f" {base} has a PENDING REPIN: its pin {pin.ref[:12]} differs from its own "
                f"{pin.path}; merge that repin to turn main green."
                if drift
                else ""
            )
        )
    return checked, notes
