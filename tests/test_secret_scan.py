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
        """gitleaks over `rev` with the workflow's options: 1 = leaks found, 0 = clean."""
        assert GITLEAKS, "gitleaks is required here (SECRET_SCAN_REQUIRE_GITLEAKS=1)"
        opts = (
            ss.log_opts(rev)
            if merges == "--diff-merges=remerge"
            else f"--full-history {merges} {rev}"
        )
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
                f"--log-opts={opts}",
            ],
            capture_output=True,
            text=True,
        )
        assert p.returncode in (0, 1), p.stderr
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
        refs_expected=3,
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
    "lines",
    [
        [f"k = 'v'  # {ALLOW}"],
        [f"k = 'v'  # {IGNORE}"],
        [f"k = 'v'  # {PRAGMA} allowlist secret"],
        [f"# {PRAGMA} allowlist nextline secret", "k = 'v'"],
        [f"k = 'v'  # {PRAGMA}allowlist secret"],
        [f"k = 'v'  # {PRAGMA} whitelist secret"],
    ],
    ids=[
        "P5a-gitleaks",
        "P5b-trufflehog",
        "P5c-pragma",
        "P5d-nextline",
        "P5e-no-space",
        "P5f-whitelist",
    ],
)
def test_p5_an_added_inline_escape_is_red(repo, lines):
    base = repo.sha()
    head = repo.commit({"a.py": "\n".join(lines) + "\n"}, "escape")
    bad = ss.escape_violations(f"{base}..{head}", repo.path)
    assert len(bad) == 1 and "inline scanner escape" in bad[0]


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


# ── P7: the daily report fails closed ─────────────────────────────────────────────────


def _origin(repo: Repo) -> None:
    repo.git("update-ref", "refs/remotes/origin/main", repo.sha("main"))


def test_p7_a_scan_that_did_not_succeed_fails(repo, tmp_path):
    (tmp_path / "gl.json").write_text("[]")
    for result in ("failure", "cancelled", "skipped", ""):
        with pytest.raises(ss.ScanError, match="not 'success'"):
            ss.report(
                tmp_path / "gl.json", result, main_ref="main", refs_expected=0, cwd=repo.path
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
        ss.report(tmp_path / "gl.json", "success", main_ref="main", refs_expected=0, cwd=repo.path)


def test_p7_fewer_refs_than_the_repo_has_fails(repo, tmp_path):
    _origin(repo)
    (tmp_path / "gl.json").write_text("[]")
    assert ss.report(
        tmp_path / "gl.json", "success", main_ref="main", refs_expected=1, cwd=repo.path
    )[1:] == (0, 0)
    with pytest.raises(ss.ScanError, match="narrowed"):
        ss.report(tmp_path / "gl.json", "success", main_ref="main", refs_expected=2, cwd=repo.path)


def test_p7_a_finding_on_main_is_counted_as_on_main(repo, tmp_path):
    _origin(repo)
    finding = {
        "RuleID": "github-pat",
        "Fingerprint": f"{repo.sha()}:a.py:github-pat:1",
        "Commit": repo.sha(),
    }
    (tmp_path / "gl.json").write_text(json.dumps([finding]))
    body, n, on_main = ss.report(
        tmp_path / "gl.json", "success", main_ref="main", refs_expected=1, cwd=repo.path
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
            "--refs-expected",
            "0",
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
