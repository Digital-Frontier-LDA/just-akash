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


class TestWorkflowVarsInvariant:
    """The outage this encodes: provider-canary.yml gated its deploy step on
    vars.CANARY_AUTODEPLOY, which was never set. An unset repo variable is the empty
    string and '' is falsy, so the step silently never ran and every scheduled run still
    reported success -- for weeks, with no canary deployed anywhere (#129/#132)."""

    def test_clean_repo_passes(self, repo):
        assert inv.check_workflow_vars(repo) == []

    def test_catches_an_undeclared_variable(self, repo):
        (repo / ".github" / "workflows" / "canary.yml").write_text(
            "on:\n  schedule: [{cron: '0 * * * *'}]\n"
            "jobs:\n  a:\n    steps:\n      - name: Deploy\n"
            "        if: ${{ vars.SOME_NEW_FLAG == 'true' }}\n        run: x\n"
        )
        problems = inv.check_workflow_vars(repo)
        assert len(problems) == 1
        assert "SOME_NEW_FLAG" in problems[0]
        assert "canary.yml:7" in problems[0]  # points at the offending line
        # and says WHY it is dangerous, not just that it is undeclared
        assert "reports success" in problems[0]

    def test_a_declared_variable_is_allowed(self, repo):
        name = next(iter(inv.DECLARED_VARS))
        (repo / ".github" / "workflows" / "canary.yml").write_text(
            "jobs:\n  a:\n    steps:\n"
            f"      - if: ${{{{ vars.{name} == 'true' }}}}\n        run: x\n"
        )
        assert inv.check_workflow_vars(repo) == []

    def test_a_stale_declaration_is_caught_only_when_checking_this_repo(self, repo):
        """The two arms are different kinds of statement. "no undeclared reference" holds
        for any tree; "every declaration is used" is about THIS repo's table. Demanding the
        second of a synthetic fixture is how a general checker acquires a private dependency
        on one repo's content -- which is what the pre-existing TestMain fixture caught."""
        assert inv.check_workflow_vars(repo) == []
        strict = inv.check_workflow_vars(repo, declarations_must_be_used=True)
        assert strict and "no workflow reads it any more" in strict[0]

    def test_prose_beside_a_real_expression_is_not_reported(self, repo):
        """The fixture above has no expression at all, so it never exercises the
        line-reporting loop. THIS one does: the same name appears in a real ${{ }} and in a
        run: line below it. Before the fix that yielded TWO problems, one pointing at the
        prose -- a check that cries wolf on documentation is a check people learn to skip.
        Raised by CodeRabbit on #132."""
        (repo / ".github" / "workflows" / "canary.yml").write_text(
            "jobs:\n  a:\n    steps:\n"
            "      - if: ${{ vars.SOME_NEW_FLAG == 'true' }}\n"
            "        run: echo 'set vars.SOME_NEW_FLAG to enable'\n"
        )
        problems = inv.check_workflow_vars(repo)
        assert len(problems) == 1, problems
        assert "canary.yml:4" in problems[0]  # the expression, not the prose on line 5

    def test_a_multiline_expression_is_still_reported(self, repo):
        """The precise pass is per-line, so a ${{ }} spanning lines matches nothing. It must
        fall back to the substring scan rather than drop the finding -- losing a real
        violation is worse than locating it imprecisely."""
        (repo / ".github" / "workflows" / "wrap.yml").write_text(
            "jobs:\n  a:\n    steps:\n      - if: >-\n"
            "          ${{\n            vars.WRAPPED_FLAG == 'true'\n          }}\n"
            "        run: x\n"
        )
        problems = inv.check_workflow_vars(repo)
        assert problems and "WRAPPED_FLAG" in problems[0]

    def test_a_reference_outside_an_expression_is_not_a_reference(self, repo):
        """`vars.X` in prose or a run: line is not an Actions expression, and flagging it
        would train people to ignore this check."""
        (repo / ".github" / "workflows" / "doc.yml").write_text(
            "jobs:\n  a:\n    steps:\n      - run: echo 'set vars.NOT_A_REF to enable'\n"
        )
        assert inv.check_workflow_vars(repo) == []


_GUARD = (
    "      - name: Refuse to run from a non-default ref\n"
    "        env:\n"
    "          REF: ${{ github.ref }}\n"
    "          DEFAULT: ${{ github.event.repository.default_branch }}\n"
    "        run: |\n"
    "          set -euo pipefail\n"
    '          if [ "$REF" != "refs/heads/$DEFAULT" ]; then\n'
    '            echo "::error::refusing $REF"\n'
    "            exit 1\n"
    "          fi\n"
)
_CHECKOUT = "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
_SOPS = (
    "      - name: Load SOPS secrets\n"
    "        uses: ./.github/actions/sops-env\n"
    "        with:\n          age-key: ${{ secrets.SOPS_AGE_KEY }}\n"
)


def _dispatch_workflow(steps: str, on: str = "on:\n  workflow_dispatch:\n") -> str:
    return f"{on}jobs:\n  close:\n    runs-on: ubuntu-latest\n    steps:\n{steps}"


class TestDispatchRefGuard:
    """openmix-17uy: six workflow_dispatch workflows loaded sops-env (the shared Console
    wallet key) with no ref check, so a repo writer could dispatch a branch whose code
    closes or spends on that wallet. The guard from auto-topup-audit.yml must come first."""

    def _write(self, repo, text, name="close-orphans.yml"):
        (repo / ".github" / "workflows" / name).write_text(text)

    def test_clean_repo_passes(self, repo):
        assert inv.check_dispatch_ref_guard(repo) == []

    def test_an_unguarded_dispatch_job_is_caught(self, repo):
        self._write(repo, _dispatch_workflow(_CHECKOUT + _SOPS))
        problems = inv.check_dispatch_ref_guard(repo)
        assert len(problems) == 1, problems
        assert "close-orphans.yml:4" in problems[0]  # the job's line
        assert "'close'" in problems[0] and "'Load SOPS secrets'" in problems[0]

    def test_a_guarded_job_passes(self, repo):
        self._write(repo, _dispatch_workflow(_GUARD + _CHECKOUT + _SOPS))
        assert inv.check_dispatch_ref_guard(repo) == []

    @pytest.mark.parametrize(
        "on",
        [
            "on: workflow_dispatch\n",
            "on: [push, workflow_dispatch]\n",
            "on:\n  schedule: [{cron: '0 * * * *'}]\n  workflow_dispatch:\n    inputs: {}\n",
        ],
    )
    def test_every_trigger_spelling_counts_as_dispatchable(self, repo, on):
        self._write(repo, _dispatch_workflow(_CHECKOUT + _SOPS, on=on))
        assert len(inv.check_dispatch_ref_guard(repo)) == 1

    def test_a_workflow_without_dispatch_is_out_of_scope(self, repo):
        """push and pull_request run the trusted workflow of their event; workflow_call
        runs the caller's. Only a dispatch lets the dispatcher choose the ref."""
        self._write(repo, _dispatch_workflow(_CHECKOUT + _SOPS, on="on: [push]\n"))
        assert inv.check_dispatch_ref_guard(repo) == []

    def test_a_yaml_extension_is_scanned(self, repo):
        self._write(repo, _dispatch_workflow(_CHECKOUT + _SOPS), name="x.yaml")
        assert len(inv.check_dispatch_ref_guard(repo)) == 1

    def test_the_guard_must_come_before_checkout(self, repo):
        """After checkout the step list is fixed, but ./.github/actions/sops-env itself
        is the dispatched ref's code. Refuse before anything from the ref is on disk."""
        self._write(repo, _dispatch_workflow(_CHECKOUT + _GUARD + _SOPS))
        assert len(inv.check_dispatch_ref_guard(repo)) == 1

    def test_ref_name_is_not_the_full_ref(self, repo):
        """A tag named main has ref_name main. Only github.ref tells it apart."""
        self._write(
            repo,
            _dispatch_workflow(
                _GUARD.replace("github.ref }}", "github.ref_name }}") + _CHECKOUT + _SOPS
            ),
        )
        assert len(inv.check_dispatch_ref_guard(repo)) == 1

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda g: g.replace("            exit 1\n", "            echo still going\n"),
            lambda g: g.replace(
                "        env:\n", "        continue-on-error: true\n        env:\n"
            ),
            lambda g: g.replace(
                "        env:\n", "        if: github.event_name == 'push'\n        env:\n"
            ),
            lambda g: g.replace("          set -euo", "          exit 0\n          set -euo"),
            lambda g: g.replace('$DEFAULT" ]', '$DEFAULT" ] || true'),
        ],
        ids=["no-exit-1", "continue-on-error", "conditional", "exit-0", "or-true"],
    )
    def test_a_guard_that_can_be_passed_over_does_not_count(self, repo, mutate):
        self._write(repo, _dispatch_workflow(mutate(_GUARD) + _CHECKOUT + _SOPS))
        assert len(inv.check_dispatch_ref_guard(repo)) == 1

    def test_the_schedule_pass_is_allowed(self, repo):
        """A schedule event has no repository payload (default_branch is empty) and GitHub
        runs it only from the default branch, so the guard may pass it on event_name."""
        guard = _GUARD.replace(
            "        run: |\n",
            "          EVENT: ${{ github.event_name }}\n        run: |\n",
        ).replace(
            "          set -euo pipefail\n",
            '          set -euo pipefail\n          if [ "$EVENT" = "schedule" ]; then\n'
            '            echo "schedule"\n            exit 0\n          fi\n',
        )
        self._write(repo, _dispatch_workflow(guard + _CHECKOUT + _SOPS))
        assert inv.check_dispatch_ref_guard(repo) == []

    def test_an_always_step_after_the_guard_is_caught(self, repo):
        sops = _SOPS.replace("        uses:", "        if: always()\n        uses:")
        self._write(repo, _dispatch_workflow(_GUARD + _CHECKOUT + sops))
        problems = inv.check_dispatch_ref_guard(repo)
        assert len(problems) == 1 and "always()" in problems[0]

    def test_every_sops_job_needs_its_own_guard(self, repo):
        """provider-smoke's report job runs if: always() after the smoke job, so the
        smoke job's guard does not stop it."""
        text = _dispatch_workflow(_GUARD + _CHECKOUT + _SOPS) + (
            "  report:\n    if: always()\n    runs-on: ubuntu-latest\n    steps:\n"
            + _CHECKOUT
            + _SOPS
        )
        self._write(repo, text)
        problems = inv.check_dispatch_ref_guard(repo)
        assert len(problems) == 1 and "'report'" in problems[0]

    def test_an_unverified_environment_is_not_an_exemption(self, repo):
        """An environment's name does not show it has required reviewers."""
        text = _dispatch_workflow(_CHECKOUT + _SOPS).replace(
            "    runs-on:", "    environment: production\n    runs-on:"
        )
        self._write(repo, text)
        assert len(inv.check_dispatch_ref_guard(repo)) == 1


def _real_guards() -> list[tuple[str, str, dict]]:
    """(workflow, job, guard step) for every guarded sops-env job in this repo."""
    import yaml

    root = Path(__file__).resolve().parents[1]
    found = []
    for path in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if inv.is_ref_guard(step):
                    found.append((path.name, job_id, step))
    return found


class TestRealGuardsBehave:
    """The structural check reads the YAML. This RUNS each guard's shell with the values
    GitHub would give it, so a guard that parses right but decides wrong still fails."""

    def test_the_guards_are_found(self):
        jobs = {(w, j) for w, j, _ in _real_guards()}
        # The two pre-existing guards plus the seven jobs openmix-17uy guarded.
        for want in [
            ("auto-topup-audit.yml", "audit"),
            ("close-orphans.yml", "close"),
            ("cleanup-stale.yml", "cleanup"),
            ("bid-probe.yml", "probe"),
            ("lease-intent-backfill.yml", "backfill"),
            ("provider-canary.yml", "canary"),
            ("provider-smoke.yml", "smoke"),
            ("provider-smoke.yml", "report"),
        ]:
            assert want in jobs, (want, sorted(jobs))

    @pytest.mark.parametrize(
        ("ref", "default", "event", "ok"),
        [
            ("refs/heads/main", "main", "workflow_dispatch", True),
            ("refs/heads/evil", "main", "workflow_dispatch", False),
            ("refs/tags/main", "main", "workflow_dispatch", False),
            ("refs/heads/main", "", "workflow_dispatch", False),
        ],
        ids=["default-branch", "other-branch", "tag-named-main", "no-default"],
    )
    def test_each_guard_decides_on_the_full_ref(self, tmp_path, ref, default, event, ok):
        import subprocess

        for workflow, job, step in _real_guards():
            env = {"PATH": "/usr/bin:/bin"}
            for name, value in step["env"].items():
                value = value.replace("${{ github.ref }}", ref)
                value = value.replace("${{ github.event.repository.default_branch }}", default)
                value = value.replace("${{ github.event_name }}", event)
                env[name] = value
            result = subprocess.run(
                ["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True
            )
            assert (result.returncode == 0) is ok, (workflow, job, result.stdout)


class TestAnnotations:
    def test_annotation_carries_file_and_line(self, repo, capsys):
        """`::error::` alone puts the annotation at the top of the run. The comment beside it
        has always claimed it lands on the offending line; file=/line= makes that true."""
        (repo / ".github" / "workflows" / "canary.yml").write_text(
            "jobs:\n  a:\n    steps:\n"
            "      - if: ${{ vars.UNDECLARED_ONE == 'true' }}\n        run: x\n"
        )
        assert inv.main(["--root", str(repo)]) == 1
        out = capsys.readouterr().out
        assert "::error file=.github/workflows/canary.yml,line=4::" in out


class TestMain:
    def test_exit_zero_when_clean(self, repo, capsys):
        assert inv.main(["--root", str(repo)]) == 0
        assert "OK" in capsys.readouterr().out

    def test_success_line_does_not_claim_unchecked_coverage(self, repo, capsys):
        """Non-strict runs skip the dead-declaration arm, so the summary must not say the
        declarations were all referenced. Reporting coverage nobody verified is the exact
        defect this checker exists to prevent -- it must not commit it in its own output.
        Raised by CodeRabbit on #132."""
        assert inv.main(["--root", str(repo)]) == 0
        assert "usage unchecked" in capsys.readouterr().out

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
        # Strict here: this IS the repo the table describes.
        assert inv.check_workflow_vars(root, declarations_must_be_used=True) == []
        assert inv.check_changelog(root) == []
        assert inv.check_dispatch_ref_guard(root) == []
