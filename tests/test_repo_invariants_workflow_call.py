"""The workflow_call exemption must stay narrow.

Widening it would quietly undo the invariant it lives inside: v1.35.0 moved every CI
secret into SOPS, and two later PRs re-added a direct `secrets.AKASH_API_KEY`. The
exemption exists for ONE reason — a reusable workflow's secrets are supplied by the
CALLING repo (its Akash account, its runner PAT) and cannot live in this repo's SOPS
file even in principle. Anything else is still a violation.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "_inv", ROOT / ".github/scripts/check_repo_invariants.py"
)
inv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inv)

REUSABLE = """
on:
  workflow_call:
    secrets:
      AKASH_API_KEY:
        required: true
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - env:
          K: ${{ secrets.AKASH_API_KEY }}
        run: echo hi
"""


def test_a_declared_call_secret_is_exempt():
    """Supplied by the caller; this repo must never hold it."""
    assert inv.declared_call_secrets(REUSABLE) == {"AKASH_API_KEY"}


def test_an_undeclared_secret_is_not_exempt():
    """It would have to come from THIS repo, so SOPS still applies."""
    assert "OTHER" not in inv.declared_call_secrets(REUSABLE)


def test_a_non_reusable_workflow_gets_no_exemption():
    """No workflow_call block means every secret is this repo's own."""
    plain = "on:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
    assert inv.declared_call_secrets(plain) == set()


def test_a_workflow_call_without_secrets_grants_nothing():
    assert (
        inv.declared_call_secrets(
            "on:\n  workflow_call:\n    inputs:\n      a:\n        type: string\n"
        )
        == set()
    )


def test_malformed_yaml_grants_no_exemption():
    """A parse failure must fail CLOSED. Returning a permissive set on bad input
    would let a syntax error switch the guard off."""
    assert inv.declared_call_secrets("on: [[[") == set()


def test_the_repo_itself_is_clean():
    assert inv.check_secrets(ROOT) == []
