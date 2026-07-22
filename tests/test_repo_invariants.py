"""Tests for .github/scripts/check_repo_invariants.py.

Each case below mirrors a regression that ACTUALLY happened on main — a direct
`secrets.AKASH_API_KEY` reintroduced twice after the SOPS migration, and a merge
that left two `## [1.37.0]` sections in non-descending order. The guard exists to
turn those from "someone notices later" into "CI says no".
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "check_repo_invariants.py"
_spec = importlib.util.spec_from_file_location("check_repo_invariants", _SCRIPT)
assert _spec and _spec.loader
inv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inv)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal repo skeleton that satisfies every invariant."""
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "actions").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  a:\n    steps:\n      - uses: ./.github/actions/sops-env\n"
        "        with:\n          age-key: ${{ secrets.SOPS_AGE_KEY }}\n"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "## [1.38.0] — 2026-07-22\n\n## [1.37.0] — 2026-07-21\n"
    )
    (tmp_path / "pyproject.toml").write_text('version = "1.38.0"\n')
    return tmp_path


class TestSecretsInvariant:
    def test_clean_repo_passes(self, repo):
        assert inv.check_secrets(repo) == []

    def test_catches_a_direct_secret_reference(self, repo):
        """The exact regression from #92: a step reading AKASH_API_KEY straight
        from GitHub secrets instead of through sops-env."""
        (repo / ".github" / "workflows" / "smoke.yml").write_text(
            "jobs:\n  a:\n    steps:\n      - env:\n"
            "          AKASH_API_KEY: ${{ secrets.AKASH_API_KEY }}\n        run: x\n"
        )
        problems = inv.check_secrets(repo)
        assert len(problems) == 1
        assert "AKASH_API_KEY" in problems[0]
        assert "smoke.yml:5" in problems[0]  # points at the offending line
        assert "sops-env" in problems[0]  # and says what to do instead

    def test_does_not_flag_the_detect_secrets_baseline_filename(self, repo):
        """`.secrets.baseline` is a FILENAME that appears in these workflows. A
        naive `grep secrets\\.` matches it and the guard cries wolf — so only
        references inside a ${{ }} expression count."""
        (repo / ".github" / "workflows" / "scan.yml").write_text(
            "jobs:\n  a:\n    steps:\n"
            "      - run: detect-secrets scan --exclude-files '.secrets.baseline'\n"
        )
        assert inv.check_secrets(repo) == []

    def test_github_token_is_allowed(self, repo):
        """Auto-provisioned per job, never stored by us — SOPS could not hold it
        even in principle, so forbidding it would buy nothing."""
        (repo / ".github" / "workflows" / "gh.yml").write_text(
            "jobs:\n  a:\n    steps:\n      - env:\n"
            "          T: ${{ secrets.GITHUB_TOKEN }}\n        run: gh pr list\n"
        )
        assert inv.check_secrets(repo) == []

    def test_scans_composite_actions_too(self, repo):
        """A secret can leak in via a composite action just as easily."""
        (repo / ".github" / "actions" / "bad").mkdir()
        (repo / ".github" / "actions" / "bad" / "action.yml").write_text(
            "runs:\n  steps:\n    - env:\n        K: ${{ secrets.SNEAKY }}\n"
        )
        assert any("SNEAKY" in p for p in inv.check_secrets(repo))

    def test_multiline_expression_is_still_matched(self, repo):
        (repo / ".github" / "workflows" / "m.yml").write_text(
            "jobs:\n  a:\n    if: ${{\n        secrets.HIDDEN != ''\n      }}\n"
        )
        assert any("HIDDEN" in p for p in inv.check_secrets(repo))


class TestChangelogInvariant:
    def test_clean_repo_passes(self, repo):
        assert inv.check_changelog(repo) == []

    def test_catches_a_duplicate_version_section(self, repo):
        """The merge bug: two sections claiming 1.37.0, the stray one below
        1.36.1 so the order no longer descends."""
        (repo / "CHANGELOG.md").write_text(
            "## [1.38.0]\n\n## [1.37.0]\n\n## [1.36.1]\n\n## [1.37.0]\n\n## [1.36.0]\n"
        )
        problems = inv.check_changelog(repo)
        assert any("duplicate section for 1.37.0" in p for p in problems)
        assert any("descend strictly" in p for p in problems)

    def test_catches_out_of_order_sections(self, repo):
        (repo / "CHANGELOG.md").write_text("## [1.37.0]\n\n## [1.38.0]\n")
        (repo / "pyproject.toml").write_text('version = "1.37.0"\n')
        assert any("descend strictly" in p for p in inv.check_changelog(repo))

    def test_catches_version_drift(self, repo):
        """A release that bumped the changelog but not the package (or vice
        versa) — the two must always agree."""
        (repo / "pyproject.toml").write_text('version = "1.37.0"\n')
        problems = inv.check_changelog(repo)
        assert len(problems) == 1
        assert "1.37.0" in problems[0] and "1.38.0" in problems[0]

    def test_missing_changelog_is_reported_not_crashed(self, repo):
        (repo / "CHANGELOG.md").unlink()
        assert inv.check_changelog(repo)  # a problem, not a traceback


class TestMain:
    def test_exit_zero_when_clean(self, repo, capsys):
        assert inv.main(["--root", str(repo)]) == 0
        assert "OK" in capsys.readouterr().out

    def test_exit_one_and_annotates_on_failure(self, repo, capsys):
        (repo / "pyproject.toml").write_text('version = "9.9.9"\n')
        assert inv.main(["--root", str(repo)]) == 1
        captured = capsys.readouterr()
        assert "FAILED" in captured.err
        # GitHub annotation so the failure lands on the PR, not just in the log.
        assert "::error::" in captured.out

    def test_the_real_repo_satisfies_its_own_invariants(self):
        """Guard the guard: if this repo ever violates the rules it enforces,
        this fails here rather than only in CI."""
        root = Path(__file__).resolve().parents[1]
        assert inv.check_secrets(root) == []
        assert inv.check_changelog(root) == []
