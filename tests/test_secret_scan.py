"""Plant tests for .github/scripts/secret_scan.py and the Secret Scan workflow (openmix-g442m).

Each scenario builds a scratch git repo, plants a fake-but-matching token, and asserts what the
scoped scan (P1-P6) or the daily report (P7) does with it. Tokens are generated at run time, so
no secret-shaped literal is committed here, and the inline escapes are spelled in pieces, so this
file does not itself add what the escape guard refuses.

The scans need the gitleaks binary. Without it those tests skip, unless
SECRET_SCAN_REQUIRE_GITLEAKS=1 (the `secret-scan-plants` job, which installs the pinned
binary): there a missing binary is a failure.
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import shutil
import string
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = ROOT / ".github" / "scripts" / "secret_scan.py"
_spec = importlib.util.spec_from_file_location("secret_scan", _SCRIPT)
assert _spec and _spec.loader
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)

GITLEAKS = shutil.which("gitleaks")
needs_gitleaks = pytest.mark.skipif(
    GITLEAKS is None and os.environ.get("SECRET_SCAN_REQUIRE_GITLEAKS") != "1",
    reason="gitleaks not installed (the secret-scan-plants job runs these)",
)

ALLOW = "gitleaks" + ":allow"
IGNORE = "trufflehog" + ":ignore"
PRAGMA = "pragma" + ":"

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "plant",
    "GIT_AUTHOR_EMAIL": "plant@example.invalid",
    "GIT_COMMITTER_NAME": "plant",
    "GIT_COMMITTER_EMAIL": "plant@example.invalid",
}


def token(seed: int) -> str:
    """A GitHub-PAT-shaped string (gitleaks rule github-pat), never a real credential."""
    r = random.Random(seed)
    return "ghp_" + "".join(r.choice(string.ascii_letters + string.digits) for _ in range(36))


class Repo:
    def __init__(self, path: Path):
        self.path = path
        self.scans = 0
        path.mkdir()
        self.git("init", "-q", "-b", "main")
        self.commit({"README.md": "base\n"}, "base")

    def git(self, *args: str, check: bool = True) -> str:
        p = subprocess.run(["git", *args], cwd=self.path, capture_output=True, text=True, env=_ENV)
        if check and p.returncode:
            raise AssertionError(f"git {args}: {p.stderr}")
        return p.stdout.strip()

    def commit(self, files: dict[str, str], msg: str) -> str:
        for name, text in files.items():
            (self.path / name).write_text(text)
            self.git("add", name)
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def sha(self, ref: str = "HEAD") -> str:
        return self.git("rev-parse", ref)

    def scan(self, rev: str, *, merges: str = "--diff-merges=remerge") -> int:
        """gitleaks over `rev` with the workflow's options: 1 = leaks found, 0 = clean.

        The scanner must also have succeeded: its JSON report holds findings iff rc is 1, so
        an rc of 0 from a scan that errored or saw nothing cannot pass as "clean"."""
        assert GITLEAKS, "gitleaks is required here (SECRET_SCAN_REQUIRE_GITLEAKS=1)"
        opts = (
            ss.log_opts(rev)
            if merges == "--diff-merges=remerge"
            else f"--full-history {merges} {rev}"
        )
        out = self.path.parent / f"scan-{self.scans}.json"
        self.scans += 1
        p = subprocess.run(
            [
                GITLEAKS,
                "detect",
                "--source",
                str(self.path),
                "--no-banner",
                "--redact",
                "--exit-code",
                "1",
                "--report-format",
                "json",
                "--report-path",
                str(out),
                f"--log-opts={opts}",
            ],
            capture_output=True,
            text=True,
        )
        assert p.returncode in (0, 1), p.stderr
        findings = json.loads(out.read_text())
        assert isinstance(findings, list) and (len(findings) > 0) == (p.returncode == 1), (
            p.returncode,
            findings,
            p.stderr,
        )
        return p.returncode

    def report(self, rev: str, out: Path) -> list[dict]:
        assert GITLEAKS
        subprocess.run(
            [
                GITLEAKS,
                "detect",
                "--source",
                str(self.path),
                "--no-banner",
                "--redact",
                "--exit-code",
                "0",
                "--report-format",
                "json",
                "--report-path",
                str(out),
                f"--log-opts={ss.log_opts(rev)}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(out.read_text())


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "r")


def _pr(repo: Repo) -> tuple[str, str]:
    """A PR: branch `feat` off main; returns (base, head)."""
    return repo.sha("main"), repo.sha("feat")


# ── the range for each event ───────────────────────────────────────────────────────────────


def test_a_pr_scans_base_to_head(repo):
    base = repo.sha()
    repo.git("checkout", "-q", "-b", "feat")
    head = repo.commit({"a.py": "x = 1\n"}, "feat")
    rev, n = ss.scan_range("pull_request", base=base, head=head, cwd=repo.path)
    assert (rev, n) == (f"{base}..{head}", 1)
    assert ss.log_opts(rev) == f"--full-history --diff-merges=remerge {base}..{head}"


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_a_missing_or_unknown_sha_fails_instead_of_scanning_nothing(repo, event):
    with pytest.raises(ss.ScanError):
        ss.scan_range(event, base="", head=repo.sha(), cwd=repo.path)
    with pytest.raises(ss.ScanError):
        ss.scan_range(event, base=repo.sha(), head="ab" * 20, cwd=repo.path)


@pytest.mark.parametrize("before", [ss.ZERO_SHA, "cd" * 20], ids=["new-ref", "unfetchable"])
def test_a_push_without_a_usable_before_scans_mains_own_history(repo, before):
    head = repo.commit({"a.py": "x = 1\n"}, "next")
    rev, n = ss.scan_range("push", before=before, sha=head, cwd=repo.path)
    assert (rev, n) == (head, 2)


def test_a_push_scans_before_to_sha(repo):
    before = repo.sha()
    head = repo.commit({"a.py": "x = 1\n"}, "next")
    assert ss.scan_range("push", before=before, sha=head, cwd=repo.path) == (
        f"{before}..{head}",
        1,
    )


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_the_daily_scan_covers_every_ref(event):
    assert ss.scan_range(event) == ("--all", None)


def test_an_unknown_event_is_refused():
    with pytest.raises(ss.ScanError):
        ss.scan_range("issue_comment")


# ── P1-P4, P6: what the scoped scan sees ─────────────────────────────────────────────────


@needs_gitleaks
def test_p1_a_token_in_the_pr_range_is_red(repo):
    repo.git("checkout", "-q", "-b", "feat")
    repo.commit({"cfg.py": f'key = "{token(1)}"\n'}, "add key")
    base, head = _pr(repo)
    assert repo.scan(f"{base}..{head}") == 1


@needs_gitleaks
def test_p2_a_token_on_another_branch_does_not_red_the_pr(repo, tmp_path):
    repo.git("checkout", "-q", "-b", "stale")
    repo.commit({"old.py": f'key = "{token(2)}"\n'}, "abandoned")
    repo.git("checkout", "-q", "main")
    repo.git("checkout", "-q", "-b", "feat")
    repo.commit({"a.py": "x = 1\n"}, "clean work")
    base, head = _pr(repo)
    assert repo.scan(f"{base}..{head}") == 0
    assert repo.scan("--all") == 1, "control: the daily scan does see it"
    for b in ("main", "stale", "feat"):
        repo.git("update-ref", f"refs/remotes/origin/{b}", repo.sha(b))
    found = repo.report("--all", tmp_path / "gl.json")
    body, n, on_main = ss.report(
        tmp_path / "gl.json",
        "success",
        main_ref="refs/remotes/origin/main",
        refs_path=_refs(tmp_path, 3),
        cwd=repo.path,
    )
    assert (n, on_main) == (len(found), 0) and n >= 1
    assert "`refs/remotes/origin/stale`" in body and token(2) not in body


@needs_gitleaks
def test_p3_a_token_written_into_a_conflict_resolution_is_red(repo):
    repo.commit({"f.py": "a = 1\n"}, "f")
    repo.git("checkout", "-q", "-b", "feat")
    repo.commit({"f.py": "a = 10\n"}, "feat side")
    repo.git("checkout", "-q", "main")
    repo.commit({"f.py": "a = 11\n"}, "main side")
    base = repo.sha("main")
    repo.git("checkout", "-q", "feat")
    repo.git("merge", "-q", "main", check=False)  # conflicts by construction
    repo.commit({"f.py": f'a = 12\nkey = "{token(3)}"\n'}, "merge main")
    head = repo.sha()
    assert repo.scan(f"{base}..{head}", merges="--no-diff-merges") == 0, (
        "control: without a merge diff the resolution is invisible (the gap before g442m)"
    )
    assert repo.scan(f"{base}..{head}") == 1


@needs_gitleaks
def test_p4_updating_from_a_main_that_holds_an_ignored_finding_stays_green(repo, tmp_path):
    fork = repo.sha()
    repo.commit({"vendor.py": f'key = "{token(4)}"\n'}, "known finding on main")
    found = repo.report("main", tmp_path / "main.json")
    assert len(found) == 1
    repo.commit({".gitleaksignore": found[0]["Fingerprint"] + "\n"}, "ignore it, reviewed")
    assert repo.scan(repo.sha()) == 0, "control: main itself is clean once ignored"
    repo.git("checkout", "-q", "-b", "feat", fork)
    repo.commit({"a.py": "x = 1\n"}, "work")
    repo.git("merge", "-q", "--no-edit", "main")  # update-branch: a clean merge
    base, head = _pr(repo)
    assert repo.scan(f"{base}..{head}") == 0
    assert repo.scan(f"{base}..{head}", merges="-m") == 1, (
        "control: -m re-scans main's delta under the merge commit, a new fingerprint"
    )


@needs_gitleaks
def test_p6_a_pr_cannot_ignore_its_own_token(repo, tmp_path):
    base = repo.sha()
    repo.git("checkout", "-q", "-b", "feat")
    repo.commit({"cfg.py": f'key = "{token(6)}"\n'}, "add key")
    head = repo.sha()
    fp = repo.report(f"{base}..{head}", tmp_path / "pr.json")[0]["Fingerprint"]
    head = repo.commit({".gitleaksignore": fp + "\n"}, "exempt it")
    assert repo.scan(f"{base}..{head}") == 0, "control: the PR's own ignore file hides it"
    assert any("changed together" in v for v in ss.escape_violations(f"{base}..{head}", repo.path))
    assert ss.pin_config(base, repo.path) == [
        ".gitleaks.toml: absent on base and head",
        ".gitleaksignore: removed (absent on base)",
    ]
    assert repo.scan(f"{base}..{head}") == 1


def test_pin_config_writes_the_bases_files(repo):
    base = repo.commit(
        {".gitleaks.toml": "title = 'base'\n", ".gitleaksignore": "fp-base\n"}, "cfg"
    )
    repo.git("checkout", "-q", "-b", "feat")
    repo.commit({".gitleaks.toml": "title = 'head'\n", ".gitleaksignore": "fp-head\n"}, "loosen")
    ss.pin_config(base, repo.path)
    assert (repo.path / ".gitleaks.toml").read_text() == "title = 'base'\n"
    assert (repo.path / ".gitleaksignore").read_text() == "fp-base\n"


# ── P5: the escape guard ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line", [f"k = 'v'  # {ALLOW}", f"k = 'v'  # {IGNORE}"], ids=["P5a-gitleaks", "P5b-trufflehog"]
)
def test_p5_an_added_inline_escape_is_red(repo, line):
    base = repo.sha()
    head = repo.commit({"a.py": line + "\n"}, "escape")
    bad = ss.escape_violations(f"{base}..{head}", repo.path)
    assert len(bad) == 1 and "inline scanner escape" in bad[0]


PRAGMAS = {
    "P5c-pragma": [f"k = 'v'  # {PRAGMA} allowlist secret"],
    "P5d-nextline": [f"# {PRAGMA} allowlist nextline secret", "k = 'v'"],
    "P5e-no-space": [f"k = 'v'  # {PRAGMA}allowlist secret"],
    "P5f-whitelist": [f"k = 'v'  # {PRAGMA} whitelist secret"],
}


@pytest.mark.parametrize("lines", PRAGMAS.values(), ids=PRAGMAS.keys())
def test_p5_a_detect_secrets_pragma_passes_the_guard(repo, lines):
    """The pragma escapes detect-secrets only, which the baseline audit (P8) already holds,
    and main carries it on many lines: refusing it would red ordinary PRs."""
    base = repo.sha()
    head = repo.commit({"a.py": "\n".join(lines) + "\n"}, "pragma")
    assert ss.escape_violations(f"{base}..{head}", repo.path) == []


@needs_gitleaks
def test_p5i_a_pragma_does_not_hide_a_token_from_gitleaks(repo):
    base = repo.sha()
    head = repo.commit({"a.py": f'key = "{token(5)}"  # {PRAGMA} allowlist secret\n'}, "pragma")
    assert ss.escape_violations(f"{base}..{head}", repo.path) == []
    assert repo.scan(f"{base}..{head}") == 1


def test_p5j_an_added_line_that_starts_with_plus_plus_is_content_not_a_header(repo):
    """`++ x  # allow` is "+++ x …" in the patch: the guard must not take it for a header."""
    base = repo.sha()
    head = repo.commit({"a.py": "x = 1\n", "b.txt": f"++ key = 'v'  # {ALLOW}\n"}, "plus-plus")
    bad = ss.escape_violations(f"{base}..{head}", repo.path)
    assert len(bad) == 1 and bad[0].startswith("b.txt: adds an inline scanner escape")


def test_p5k_a_file_header_is_not_scanned_as_content(repo):
    """A path holding the escape text appears only as a "+++ b/<path>" header line."""
    base = repo.sha()
    head = repo.commit({f"{ALLOW}.py": "x = 1\n"}, "odd name")
    patch = repo.git("log", "-p", "--format=", f"{base}..{head}")
    assert f"+++ b/{ALLOW}.py" in patch, "control: the header carries the escape text"
    assert ss.escape_violations(f"{base}..{head}", repo.path) == []


def test_p5g_no_escape_passes_the_guard(repo):
    base = repo.sha()
    head = repo.commit({"a.py": "k = 'v'\n"}, "plain")
    assert ss.escape_violations(f"{base}..{head}", repo.path) == []


def test_p5h_removing_an_escape_passes_the_guard(repo):
    repo.commit({"a.py": f"k = 'v'  # {ALLOW}\n"}, "old escape")
    base = repo.sha()
    head = repo.commit({"a.py": "k = 'v'\n"}, "drop it")
    assert ss.escape_violations(f"{base}..{head}", repo.path) == []


@pytest.mark.parametrize("ignore_file", ss.IGNORE_FILES)
def test_p5_an_exemption_change_mixed_with_code_is_red_alone_is_not(repo, ignore_file):
    base = repo.sha()
    alone = repo.commit({ignore_file: "x\n"}, "exemption only")
    assert ss.escape_violations(f"{base}..{alone}", repo.path) == []
    mixed = repo.commit({"a.py": "x = 1\n"}, "and code")
    bad = ss.escape_violations(f"{base}..{mixed}", repo.path)
    assert len(bad) == 1 and "its own pull request" in bad[0]


def test_a_secrets_baseline_update_may_land_with_its_code(repo):
    """detect-secrets compares .secrets.baseline with the PR's tree: they cannot be split."""
    base = repo.sha()
    head = repo.commit({".secrets.baseline": "{}\n", "a.py": "x = 1\n"}, "fixture + baseline")
    assert ss.escape_violations(f"{base}..{head}", repo.path) == []
    assert ss.baseline_violations(base, head, repo.path) == []


# ── P8: a baseline cannot exempt a PR's own secret by regeneration ────────────────────


def _baseline(**entries: dict) -> str:
    """{filename: entry}, one detect-secrets result per file."""
    return json.dumps({"version": "1.5.0", "results": {f: [e] for f, e in entries.items()}})


_ENTRY = {"type": "Secret Keyword", "hashed_secret": "ab" * 20, "line_number": 3}


def test_p8_an_unaudited_baseline_entry_is_red(repo, monkeypatch, capsys):
    base = repo.commit({".secrets.baseline": _baseline()}, "empty baseline")
    head = repo.commit(
        {".secrets.baseline": _baseline(**{"cfg.py": _ENTRY}), "cfg.py": "x = 1\n"},
        "regenerate over a new secret",
    )
    bad = ss.baseline_violations(base, head, repo.path)
    assert len(bad) == 1 and "cfg.py gains an unaudited Secret Keyword" in bad[0]
    monkeypatch.chdir(repo.path)  # the CLI runs in the workflow's checkout
    assert ss.main(["escape-guard", "--rev", f"{base}..{head}"]) == 1
    assert "::error::.secrets.baseline: cfg.py gains" in capsys.readouterr().out


def test_p8_an_entry_marked_not_a_secret_passes(repo):
    base = repo.commit({".secrets.baseline": _baseline()}, "empty baseline")
    head = repo.commit(
        {".secrets.baseline": _baseline(**{"cfg.py": {**_ENTRY, "is_secret": False}})},
        "audited fixture",
    )
    assert ss.baseline_violations(base, head, repo.path) == []


@pytest.mark.parametrize("verdict", [True, None], ids=["marked-secret", "null"])
def test_p8_only_an_explicit_false_counts_as_audited(repo, verdict):
    base = repo.commit({".secrets.baseline": _baseline()}, "empty baseline")
    head = repo.commit(
        {".secrets.baseline": _baseline(**{"cfg.py": {**_ENTRY, "is_secret": verdict}})}, "x"
    )
    assert len(ss.baseline_violations(base, head, repo.path)) == 1


def test_p8_line_number_churn_on_a_known_entry_passes(repo):
    base = repo.commit({".secrets.baseline": _baseline(**{"cfg.py": _ENTRY})}, "known")
    head = repo.commit(
        {".secrets.baseline": _baseline(**{"cfg.py": {**_ENTRY, "line_number": 9}})}, "moved"
    )
    assert ss.baseline_violations(base, head, repo.path) == []


def test_p8_a_baseline_that_does_not_parse_fails_closed(repo):
    base = repo.sha()
    head = repo.commit({".secrets.baseline": "not json"}, "broken")
    with pytest.raises(ss.ScanError, match="does not parse"):
        ss.baseline_violations(base, head, repo.path)


def test_baseline_equal_ignores_audit_fields_only(tmp_path):
    committed, fresh = tmp_path / "committed", tmp_path / "fresh"
    committed.write_text(_baseline(**{"a.py": {**_ENTRY, "is_secret": False}}))
    fresh.write_text(_baseline(**{"a.py": _ENTRY}))
    assert ss.baseline_equal(committed, fresh)
    fresh.write_text(_baseline(**{"a.py": _ENTRY, "b.py": _ENTRY}))
    assert not ss.baseline_equal(committed, fresh), "control: a new finding still differs"
    assert ss.main(["baseline-equal", str(committed), str(fresh)]) == 1


# ── P7: the daily report fails closed ─────────────────────────────────────────────────


def _origin(repo: Repo) -> None:
    repo.git("update-ref", "refs/remotes/origin/main", repo.sha("main"))


def _refs(tmp_path: Path, branches: int, tags: int = 0, *, api: int | None = None) -> Path:
    """refs.json as the scan job writes it: the API's counts and its own checkout's."""
    out = tmp_path / "refs.json"
    listed = {"branches": branches if api is None else api, "tags": tags}
    out.write_text(json.dumps({"api": listed, "scanned": {"branches": branches, "tags": tags}}))
    return out


def test_p7_a_scan_that_did_not_succeed_fails(repo, tmp_path):
    (tmp_path / "gl.json").write_text("[]")
    for result in ("failure", "cancelled", "skipped", ""):
        with pytest.raises(ss.ScanError, match="not 'success'"):
            ss.report(
                tmp_path / "gl.json",
                result,
                main_ref="main",
                refs_path=_refs(tmp_path, 0),
                cwd=repo.path,
            )


@pytest.mark.parametrize(
    "content, match",
    [(None, "no scan report"), ("not json", "does not parse"), ('{"a": 1}', "not a JSON array")],
    ids=["missing", "unparseable", "not-a-list"],
)
def test_p7_a_missing_or_bad_report_fails(repo, tmp_path, content, match):
    if content is not None:
        (tmp_path / "gl.json").write_text(content)
    with pytest.raises(ss.ScanError, match=match):
        ss.report(
            tmp_path / "gl.json",
            "success",
            main_ref="main",
            refs_path=_refs(tmp_path, 0),
            cwd=repo.path,
        )


def test_p7_a_scan_that_saw_fewer_refs_than_the_api_lists_fails(repo, tmp_path):
    (tmp_path / "gl.json").write_text("[]")
    ok = _refs(tmp_path, 2, 1)
    assert ss.report(tmp_path / "gl.json", "success", main_ref="main", refs_path=ok)[1:] == (0, 0)
    with pytest.raises(ss.ScanError, match="branches 2 < 3.*narrowed"):
        ss.report(
            tmp_path / "gl.json", "success", main_ref="main", refs_path=_refs(tmp_path, 2, api=3)
        )


@pytest.mark.parametrize(
    "content",
    [None, "not json", '{"api": {"branches": 1, "tags": 0}}', '{"api": 1, "scanned": 1}'],
    ids=["missing", "unparseable", "no-scanned", "wrong-shape"],
)
def test_p7_missing_or_bad_ref_counts_fail(tmp_path, content):
    (tmp_path / "gl.json").write_text("[]")
    refs = tmp_path / "refs.json"
    if content is not None:
        refs.write_text(content)
    with pytest.raises(ss.ScanError, match="no usable ref counts"):
        ss.report(tmp_path / "gl.json", "success", main_ref="main", refs_path=refs)


def test_the_refs_cli_counts_the_checkout_it_runs_in(repo, tmp_path, monkeypatch):
    _origin(repo)
    repo.git("update-ref", "refs/remotes/origin/HEAD", repo.sha())
    repo.git("tag", "v1")
    repo.git("update-ref", "refs/remotes/pull/7/head", repo.sha())
    assert ss.refs_in_checkout(repo.path) == {"branches": 1, "tags": 1}, (
        "origin/HEAD and closed-PR heads are not branches the API lists"
    )
    monkeypatch.chdir(repo.path)
    out = tmp_path / "refs.json"
    assert ss.main(["refs", "--api-branches", "1", "--api-tags", "1", "--out", str(out)]) == 0
    counts = json.loads(out.read_text())
    assert counts["api"] == {"branches": 1, "tags": 1}
    assert counts["scanned"]["branches"] == 1


def test_p7_a_finding_on_main_is_counted_as_on_main(repo, tmp_path):
    _origin(repo)
    finding = {
        "RuleID": "github-pat",
        "Fingerprint": f"{repo.sha()}:a.py:github-pat:1",
        "Commit": repo.sha(),
    }
    (tmp_path / "gl.json").write_text(json.dumps([finding]))
    body, n, on_main = ss.report(
        tmp_path / "gl.json",
        "success",
        main_ref="main",
        refs_path=_refs(tmp_path, 1),
        cwd=repo.path,
    )
    assert (n, on_main) == (1, 1) and "**yes**" in body


def test_p7_the_cli_exits_1_and_writes_no_body_when_the_report_is_missing(tmp_path, capsys):
    out = tmp_path / "body.md"
    rc = ss.main(
        [
            "report",
            "--report",
            str(tmp_path / "none.json"),
            "--scan-result",
            "success",
            "--refs",
            str(tmp_path / "refs.json"),
            "--body-out",
            str(out),
        ]
    )
    assert rc == 1 and not out.exists()
    assert "::error::no scan report" in capsys.readouterr().out


# ── the workflow wires it as designed ─────────────────────────────────────────────────


def _wf() -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "secrets.yml").read_text())


def test_every_gitleaks_scan_is_given_its_range():
    text = (ROOT / ".github" / "workflows" / "secrets.yml").read_text()
    runs = [ln for ln in text.splitlines() if "gitleaks detect" in ln]
    assert len(runs) == 2
    joined = text[text.index("Run gitleaks (this event") :]
    assert joined.count('--log-opts="$LOG_OPTS"') >= 2, "no scan may fall back to --all"


def test_only_the_report_job_may_write():
    wf = _wf()
    assert wf["permissions"] == {"contents": "read"}
    writers = {
        n
        for n, j in wf["jobs"].items()
        if any(v == "write" for v in (j.get("permissions") or {}).values())
    }
    assert writers == {"report"}
    assert wf["jobs"]["report"]["permissions"] == {"contents": "read", "issues": "write"}


def test_the_report_runs_after_any_scan_outcome_on_main_only():
    cond = _wf()["jobs"]["report"]["if"]
    assert "always()" in cond and "github.ref == 'refs/heads/main'" in cond


def test_exemptions_come_from_the_base_on_pull_requests_and_merge_groups():
    steps = {s.get("name"): s for s in _wf()["jobs"]["gitleaks"]["steps"]}
    pin = steps["Exemptions from the base"]
    assert "pull_request" in pin["if"] and "merge_group" in pin["if"]
    names = list(steps)
    assert names.index("Exemptions from the base") < names.index(
        "Run gitleaks (this event's range)"
    )


def test_the_daily_scan_runs_every_day():
    on = _wf()[True]  # YAML 1.1 reads the bare key `on` as a boolean
    assert on["schedule"] == [{"cron": "0 8 * * *"}] and "workflow_dispatch" in on


def test_a_daily_run_and_a_push_to_main_never_share_a_concurrency_group():
    """On schedule/dispatch github.ref is refs/heads/main too: without the event in the
    group, a push to main cancels the daily scan (cancel-in-progress) or vice versa."""
    assert "github.event_name" in _wf()["concurrency"]["group"]


def test_the_report_reads_the_scan_jobs_own_ref_counts():
    gl = _wf()["jobs"]["gitleaks"]["steps"]
    names = [s.get("name") for s in gl]
    api = names.index("Branch and tag counts the API lists (daily scan)")
    assert api < names.index("Checkout"), "the API is asked before anything is fetched"
    fetch = gl[names.index("Fetch closed-PR heads (daily scan)")]["run"]
    assert "secret_scan.py refs" in fetch and "--out refs.json" in fetch
    kept = gl[names.index("Keep the report")]["with"]["path"]
    assert "refs.json" in kept and "gl.json" in kept
    rep_steps = {s.get("name"): s for s in _wf()["jobs"]["report"]["steps"]}
    assert "--refs refs.json" in rep_steps["Read the report (fails closed)"]["run"]


def test_the_refs_cli_refuses_a_zero_branch_count(tmp_path, capsys):
    """A repository always has a branch: 0 is a failed count, and would pass any scan."""
    out = tmp_path / "refs.json"
    assert ss.main(["refs", "--api-branches", "0", "--api-tags", "0", "--out", str(out)]) == 1
    assert "count failed" in capsys.readouterr().out and not out.exists()


def _api_count_step() -> dict:
    steps = {s.get("name"): s for s in _wf()["jobs"]["gitleaks"]["steps"]}
    return steps["Branch and tag counts the API lists (daily scan)"]


@pytest.mark.parametrize("failing", ["branches", "tags"])
def test_a_failed_api_count_fails_the_step_instead_of_counting_0(tmp_path, failing):
    """The step body runs as Actions runs it when no shell is given (`bash -e`), with a
    fake `gh` that fails for one endpoint: the step must go red and export nothing."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    gh = bin_ / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        f'case "$2" in *{failing}*) echo "HTTP 502" >&2; exit 1;; esac\n'
        "printf 'main\\nfeat\\n'\n"
    )
    gh.chmod(0o755)
    env_file = tmp_path / "github_env"
    env_file.write_text("")
    step = _api_count_step()
    assert "shell" not in step, "the test runs the default shell; update it with the step"
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}
    env |= {"GITHUB_ENV": str(env_file), "GITHUB_REPOSITORY": "o/r"}
    p = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True)
    assert p.returncode != 0
    assert env_file.read_text() == "", "a failed count must not reach the environment"


def test_a_successful_api_count_exports_both_counts(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "gh").write_text("#!/bin/sh\nprintf 'main\\nfeat\\n'\n")
    (bin_ / "gh").chmod(0o755)
    env_file = tmp_path / "github_env"
    env_file.write_text("")
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}
    env |= {"GITHUB_ENV": str(env_file), "GITHUB_REPOSITORY": "o/r"}
    subprocess.run(["bash", "-e", "-c", _api_count_step()["run"]], env=env, check=True)
    lines = [ln.replace(" ", "") for ln in env_file.read_text().splitlines()]
    assert lines == ["API_BRANCHES=2", "API_TAGS=2"]
