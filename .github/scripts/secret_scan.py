#!/usr/bin/env python3
"""Secret Scan helpers for .github/workflows/secrets.yml (openmix-g442m).

The Gitleaks job used to run ``gitleaks detect --source .`` on a fetch-depth 0
checkout. With no ``--log-opts``, gitleaks runs ``git log -p --all``, so every
pull request scanned every branch and tag, and one abandoned branch turned
every PR red. Each event now scans only what it adds, and a daily run scans
everything and reports to ONE issue instead of reddening PRs.

Subcommands, each run as its own workflow step:

``range``
    Print the ``--log-opts`` for this event: the PR or merge-group range, the
    pushed range (main's own history when ``before`` is zero or unfetchable),
    or ``--all`` on a schedule. ``--diff-merges=remerge`` shows what a merge
    commit adds beyond git's own automatic merge, so a secret written into a
    conflict resolution is scanned, while an update-branch merge of main
    re-scans nothing main already holds. A missing or unknown SHA is an error,
    never a silent empty scan.

``pin-config``
    Replace the working copy's ``.gitleaks.toml`` and ``.gitleaksignore`` with
    the base's. gitleaks reads both from ``--source``, so without this a pull
    request could exempt its own range.

``escape-guard``
    Fail when the range ADDS an inline escape for gitleaks or TruffleHog,
    changes a gitleaks exemption file together with anything else, or adds
    an unaudited ``.secrets.baseline`` entry.

``baseline-equal``
    The detect-secrets job's comparison of the committed baseline with a fresh
    scan, ignoring the audit fields a fresh scan never carries.

``refs``
    In the scanning job: the branch and tag counts the API lists (taken BEFORE
    the checkout) and the refs the checkout actually holds.

``report``
    Read the daily scan's JSON report and fail closed: no report, a report
    that does not parse, a scan that did not succeed, or a scan job whose
    checkout held fewer refs than the API listed. Otherwise write the issue
    body and say whether any finding is reachable from main.

Run: ``python3 .github/scripts/secret_scan.py <subcommand> --help``
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ZERO_SHA = "0" * 40

# The files gitleaks reads its exemptions from. A change to one of them must be
# reviewed on its own, never alongside the code it would exempt. NOT
# .secrets.baseline: the detect-secrets job compares the baseline with the PR's
# own tree, so a baseline update must land WITH the code it covers (all 36
# baseline changes on main to 2026-10-30 did).
IGNORE_FILES = (".gitleaks.toml", ".gitleaksignore")

# Inline escapes that blind the BACKSTOP scanners, gitleaks and TruffleHog;
# none was ever added on main. Not the detect-secrets pragma: it blinds only
# detect-secrets (gitleaks and TruffleHog still scan that line), it is a
# per-line deliberate act, and main adds it routinely (144 lines in 33 of 348
# commits to 2026-10-30). Spelled in pieces so this file does not itself add
# the strings the guard refuses.
_ESCAPE = re.compile("|".join(("gitleaks" + ":allow", "trufflehog" + ":ignore")))

BASELINE = ".secrets.baseline"
# Fields a fresh `detect-secrets scan` never writes: an audit adds them.
_AUDIT_FIELDS = ("is_secret", "is_verified")

MAX_REFS_SHOWN = 10
MAX_REF_LEN = 100


class ScanError(Exception):
    """A condition under which the scan must fail rather than pass."""


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise ScanError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def _is_commit(sha: str, cwd: Path | None) -> bool:
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        return False
    p = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=cwd, capture_output=True
    )
    return p.returncode == 0


def _need(sha: str, what: str, cwd: Path | None) -> str:
    if not _is_commit(sha, cwd):
        raise ScanError(f"{what} {sha!r} is not a commit in this checkout")
    return sha


def scan_range(
    event: str,
    *,
    base: str = "",
    head: str = "",
    before: str = "",
    sha: str = "",
    cwd: Path | None = None,
) -> tuple[str, int | None]:
    """(git revision arguments for this event, commits in the range or None for --all)."""
    if event in ("schedule", "workflow_dispatch"):
        return "--all", None
    if event in ("pull_request", "merge_group"):
        rev = f"{_need(base, 'base', cwd)}..{_need(head, 'head', cwd)}"
    elif event == "push":
        head = _need(sha, "sha", cwd)
        # A new ref or a force push leaves `before` zero or unfetchable: scan
        # main's own history then, never every branch.
        rev = f"{before}..{head}" if before != ZERO_SHA and _is_commit(before, cwd) else head
    else:
        raise ScanError(f"no scan range is defined for event {event!r}")
    return rev, int(git("rev-list", "--count", rev, cwd=cwd).strip())


def log_opts(rev: str) -> str:
    return f"--full-history --diff-merges=remerge {rev}"


def pin_config(base: str, cwd: Path) -> list[str]:
    """Overwrite the exemption files with the base's (or remove them); returns what was done."""
    done = []
    for name in (".gitleaks.toml", ".gitleaksignore"):
        p = subprocess.run(
            ["git", "show", f"{base}:{name}"], cwd=cwd, capture_output=True, text=True
        )
        target = cwd / name
        if p.returncode == 0:
            target.write_text(p.stdout)
            done.append(f"{name}: from base")
        elif target.exists():
            target.unlink()
            done.append(f"{name}: removed (absent on base)")
        else:
            done.append(f"{name}: absent on base and head")
    return done


def escape_violations(rev: str, cwd: Path | None = None) -> list[str]:
    """Escapes ADDED in the range, and ignore-file changes mixed with other paths."""
    out = []
    patch = git(
        "log", "-p", "-U0", "--diff-merges=remerge", "--format=", "--no-color", rev, cwd=cwd
    )
    path = "?"
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+") and _ESCAPE.search(line):
            out.append(f"{path}: adds an inline scanner escape: {line[1:].strip()[:120]}")
    names = git("log", "--name-only", "--diff-merges=remerge", "--format=", rev, cwd=cwd).split()
    touched = set(names)
    ignores = sorted(touched & set(IGNORE_FILES))
    if ignores and touched - set(IGNORE_FILES):
        out.append(
            f"{', '.join(ignores)} changed together with other files: an exemption "
            "change must be its own pull request"
        )
    return out


def _baseline_entries(text: str | None) -> dict[tuple[str, str, str], dict]:
    """{(filename, hashed_secret, type): entry} of a baseline; line numbers are not identity."""
    if not text:
        return {}
    try:
        results = json.loads(text).get("results") or {}
    except (json.JSONDecodeError, AttributeError) as e:
        raise ScanError(f"{BASELINE} does not parse: {e}") from e
    return {
        (fname, str(e.get("hashed_secret")), str(e.get("type"))): e
        for fname, entries in results.items()
        for e in entries or []
    }


def _show(rev: str, path: str, cwd: Path | None) -> str | None:
    p = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=cwd, capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def baseline_violations(base: str, head: str, cwd: Path | None = None) -> list[str]:
    """Entries the head's .secrets.baseline adds over the base's must be audited as
    not-a-secret (`"is_secret": false`). Otherwise a PR regenerates the baseline over
    its own secret and the detect-secrets job goes green: the accidental path, one
    bulk command, no per-secret act."""
    old = _baseline_entries(_show(base, BASELINE, cwd))
    new = _baseline_entries(_show(head, BASELINE, cwd))
    return [
        f"{BASELINE}: {fname} gains an unaudited {kind} entry; run "
        f"`detect-secrets audit {BASELINE}` and mark it, or remove the secret"
        for (fname, hashed, kind), e in sorted(new.items())
        if (fname, hashed, kind) not in old and e.get("is_secret") is not False
    ]


def _without_audit(results: dict) -> str:
    return json.dumps(
        {
            fname: [{k: v for k, v in e.items() if k not in _AUDIT_FIELDS} for e in entries]
            for fname, entries in results.items()
        },
        sort_keys=True,
    )


def baseline_equal(committed: Path, fresh: Path) -> bool:
    """The detect-secrets gate: a fresh scan finds exactly what the baseline records.
    Audit fields are dropped first, or an audited baseline could never match."""
    old = json.loads(committed.read_text()).get("results", {})
    new = json.loads(fresh.read_text()).get("results", {})
    return _without_audit(old) == _without_audit(new)


def refs_in_checkout(cwd: Path | None = None) -> dict[str, int]:
    def count(*patterns: str) -> int:
        refs = git("for-each-ref", "--format=%(refname)", *patterns, cwd=cwd).split()
        return len([r for r in refs if not r.endswith("/HEAD")])

    return {"branches": count("refs/remotes/origin"), "tags": count("refs/tags")}


def _refs_containing(commit: str, cwd: Path | None) -> list[str]:
    p = subprocess.run(
        [
            "git",
            "for-each-ref",
            "--contains",
            commit,
            "--format=%(refname)",
            "refs/remotes",
            "refs/tags",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    refs = [r for r in p.stdout.split() if not r.endswith("/HEAD")]
    return [r.replace("`", "")[:MAX_REF_LEN] for r in refs]


def report(
    report_path: Path,
    scan_result: str,
    *,
    main_ref: str,
    refs_path: Path,
    cwd: Path | None = None,
) -> tuple[str, int, int]:
    """(issue body, findings, findings reachable from main). Raises ScanError to fail closed."""
    if scan_result != "success":
        raise ScanError(f"the scan job ended {scan_result!r}, not 'success'")
    if not report_path.is_file():
        raise ScanError(f"no scan report at {report_path}")
    try:
        findings = json.loads(report_path.read_text())
    except json.JSONDecodeError as e:
        raise ScanError(f"the scan report does not parse: {e}") from e
    if not isinstance(findings, list):
        raise ScanError("the scan report is not a JSON array of findings")
    # The SCAN job's own counts (refs.json, written next to gl.json): this job's
    # checkout says nothing about what the scan saw.
    try:
        counts = json.loads(refs_path.read_text())
        api, scanned = counts["api"], counts["scanned"]
        short = [k for k in ("branches", "tags") if int(scanned[k]) < int(api[k])]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        raise ScanError(f"no usable ref counts from the scan job at {refs_path}: {e}") from e
    if short:
        raise ScanError(
            "the scan's checkout held fewer refs than the API listed ("
            + ", ".join(f"{k} {scanned[k]} < {api[k]}" for k in short)
            + "): the fetch narrowed the scan"
        )
    rows, on_main = [], 0
    for f in findings:
        commit = str(f.get("Commit", ""))
        reachable = (
            bool(commit)
            and subprocess.run(
                ["git", "merge-base", "--is-ancestor", commit, main_ref],
                cwd=cwd,
                capture_output=True,
            ).returncode
            == 0
        )
        on_main += reachable
        refs = _refs_containing(commit, cwd) if commit else []
        shown = ", ".join(f"`{r}`" for r in refs[:MAX_REFS_SHOWN])
        if len(refs) > MAX_REFS_SHOWN:
            shown += f" (+{len(refs) - MAX_REFS_SHOWN} more)"
        rows.append(
            f"| `{str(f.get('RuleID', ''))[:60]}` | `{str(f.get('Fingerprint', ''))[:200]}` "
            f"| {'**yes**' if reachable else 'no'} | {shown or '—'} |"
        )
    body = [
        "Daily all-branch secret scan (`.github/workflows/secrets.yml`, openmix-g442m).",
        "",
        f"**{len(findings)}** finding(s) on non-PR refs; {on_main} reachable from main. "
        "Match text is never shown. Rotate the credential first, then remove the commit "
        "or ignore the fingerprint in a pull request of its own.",
        "",
        "| Rule | Fingerprint | On main | Refs containing the commit |",
        "|---|---|---|---|",
        *rows,
    ]
    return "\n".join(body) + "\n", len(findings), on_main


def _output(**kv: object) -> None:
    lines = "".join(f"{k}={v}\n" for k, v in kv.items())
    dest = os.environ.get("GITHUB_OUTPUT")
    if dest:
        with open(dest, "a") as fh:
            fh.write(lines)
    sys.stdout.write(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("range")
    r.add_argument("--event", required=True)
    for name in ("base", "head", "before", "sha"):
        r.add_argument(f"--{name}", default="")
    p = sub.add_parser("pin-config")
    p.add_argument("--base", required=True)
    e = sub.add_parser("escape-guard")
    e.add_argument("--rev", required=True)
    be = sub.add_parser("baseline-equal")
    be.add_argument("committed", type=Path)
    be.add_argument("fresh", type=Path)
    rf = sub.add_parser("refs")
    rf.add_argument("--api-branches", required=True, type=int)
    rf.add_argument("--api-tags", required=True, type=int)
    rf.add_argument("--out", required=True, type=Path)
    rp = sub.add_parser("report")
    rp.add_argument("--report", required=True, type=Path)
    rp.add_argument("--scan-result", required=True)
    rp.add_argument("--main-ref", default="origin/main")
    rp.add_argument("--refs", required=True, type=Path)
    rp.add_argument("--body-out", required=True, type=Path)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "range":
            rev, n = scan_range(a.event, base=a.base, head=a.head, before=a.before, sha=a.sha)
            _output(
                rev=rev,
                log_opts=log_opts(rev),
                commits="all" if n is None else n,
                empty="true" if n == 0 else "false",
            )
        elif a.cmd == "pin-config":
            for line in pin_config(_need(a.base, "base", None), Path.cwd()):
                print(line)
        elif a.cmd == "escape-guard":
            bad = escape_violations(a.rev)
            if ".." in a.rev:  # a range with a base: its baseline additions must be audited
                bad += baseline_violations(*a.rev.split("..", 1))
            for b in bad:
                print(f"::error::{b}")
            if bad:
                return 1
            print(
                "escape guard: no gitleaks/TruffleHog escape added, no mixed exemption "
                "change, no unaudited baseline entry"
            )
        elif a.cmd == "baseline-equal":
            if not baseline_equal(a.committed, a.fresh):
                print("::error::detect-secrets found new secrets. Update .secrets.baseline")
                return 1
            print("No new secrets detected")
        elif a.cmd == "refs":
            # Every repository has a default branch, so 0 can only be a count that
            # failed; a 0 would make the narrowed-scan check pass vacuously. Tags
            # can be 0 legitimately, so they are not floored.
            if a.api_branches < 1:
                raise ScanError(
                    f"the API listed {a.api_branches} branches: the count failed, "
                    "and the narrowed-scan check would pass vacuously"
                )
            refs = {
                "api": {"branches": a.api_branches, "tags": a.api_tags},
                "scanned": refs_in_checkout(),
            }
            a.out.write_text(json.dumps(refs) + "\n")
            print(json.dumps(refs))
        else:
            body, n, on_main = report(
                a.report, a.scan_result, main_ref=a.main_ref, refs_path=a.refs
            )
            a.body_out.write_text(body)
            _output(findings=n, on_main=on_main)
    except ScanError as err:
        print(f"::error::{err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
