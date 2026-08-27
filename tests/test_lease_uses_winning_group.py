"""The lease must be created against the group that actually WON the bid.

⛔ THE DEFECT THIS PINS. `AkashConsoleAPI.create_lease` declares `gseq: int = 1`, and for
the whole life of the deploy path every caller took that default. While an order carries
one group that is invisibly correct. The moment an order is SPLIT into groups — which is
the change that roughly doubles the bid rate, 74.9% of 191 orders versus 36.6% of 303 —
a bid won on group 2 produced a lease against group 1.

⚠ WHY THE CHECKS ARE STRUCTURAL. `deploy()` is a long function that reaches Console over
the network at several points; driving it end-to-end in a unit test would test the mocks.
So the wiring is judged on the parsed AST instead, and the rules below are written to be
about the ACTION (a `create_lease` call, a `_redeploy_and_reselect` unpack) rather than
about the name `gseq` — a finder keyed to the string under change disarms itself the
moment that string moves, and reports success for having found nothing.

Every check asserts its own population is non-empty first. `sites == 0` has never once
meant clean in this codebase.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from just_akash.api import _extract_gseq

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "just_akash" / "deploy.py"


# ---------------------------------------------------------------- the checker
# Kept as a function over SOURCE TEXT, not over the live file, so the same rules can be
# run against a historical revision. A control that cannot re-run the real check is not
# a control.
def wiring_errors(source: str) -> list[str]:
    """Concrete violations of 'the lease uses the winning group'."""

    tree = ast.parse(source)
    errors: list[str] = []

    # --- site 1: the create_lease call ------------------------------------------------
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "create_lease"
    ]
    if not calls:
        return ["no create_lease call found at all (the checker is aimed at nothing)"]

    for call in calls:
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        if "gseq" not in kw:
            errors.append(
                f"create_lease at line {call.lineno} passes no group — it takes the "
                "gseq=1 default and leases group 1 whatever won"
            )
        elif isinstance(kw["gseq"], ast.Constant):
            errors.append(
                f"create_lease at line {call.lineno} passes the literal "
                f"{kw['gseq'].value!r}; a literal cannot be the winning bid's group"
            )

    # --- site 2: the re-deploy path must rebind whatever the lease call reads ---------
    # A re-created order is a NEW order. If the helper's result does not refresh the
    # group, the second attempt leases the right provider against the first order's
    # group. Find the unpack by the CALL, never by the variable name.
    lease_names = {
        kw["gseq"].id
        for call in calls
        for kw in [{k.arg: k.value for k in call.keywords if k.arg}]
        if isinstance(kw.get("gseq"), ast.Name)
    }
    unpacks = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
        and n.value.func.id == "_redeploy_and_reselect"
    ]
    if unpacks and lease_names:
        for node in unpacks:
            target = node.targets[0]
            bound = {e.id for e in ast.walk(target) if isinstance(e, ast.Name)}
            missing = lease_names - bound
            if missing:
                errors.append(
                    f"the re-deploy unpack at line {node.lineno} does not rebind "
                    f"{sorted(missing)}: after a re-created order the lease would use "
                    "the PREVIOUS order's group"
                )

    # --- site 3: the group must travel with the provider on EVERY reselection -----
    # ⛔ THE LIMB THAT WAS MISSING, and its absence cost a real defect. The first version
    # checked the initial selection and the re-deploy path and called that complete. It
    # missed the STALE-BID RETRY, which picks a different bid on the SAME order: it
    # rebound `provider` from `next_bid` and left the group on the previous bid — the
    # very defect this file exists to prevent, surviving one path over. CodeRabbit caught
    # it on review; this rule is why it cannot return.
    #
    # ⚠ SCOPED TO THE FUNCTION THAT LEASES. A rule over every `_extract_provider` call in
    # the module reads providers out of logging helpers and bid-filtering comprehensions
    # too (`b`, `item`) and would demand a group for bids that never reach a lease — a
    # false-positive machine, and the pressure to silence it would take the real rule
    # with it.
    def _arg(call: ast.Call) -> str | None:
        return call.args[0].id if call.args and isinstance(call.args[0], ast.Name) else None

    def _calls(node: ast.AST, fn: str) -> set[str]:
        return {
            a
            for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == fn
            for a in [_arg(c)]
            if a is not None
        }

    holder = None
    for fn_node in ast.walk(tree):
        if not isinstance(fn_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # innermost wins: keep the smallest span that still contains the call
        if any(c is calls[0] for c in ast.walk(fn_node)) and (
            holder is None or fn_node.lineno > holder.lineno
        ):
            holder = fn_node
    if holder is None:
        errors.append("the create_lease call sits in no function (cannot scope limb 3)")
        return errors

    # A provider BINDING: an assignment to `provider` / `*_provider` whose value reads a
    # bid. These are the ones the lease consumes, directly or via the re-deploy return.
    bindings: dict[str, int] = {}
    for n in ast.walk(holder):
        if not isinstance(n, ast.Assign):
            continue
        names = [t.id for t in n.targets if isinstance(t, ast.Name)]
        if not any(t == "provider" or t.endswith("_provider") for t in names):
            continue
        for bid_var in _calls(n.value, "_extract_provider"):
            bindings.setdefault(bid_var, n.lineno)

    if not bindings:
        errors.append(
            "no provider is bound from a bid in the leasing function (limb 3 is aimed at nothing)"
        )
    groups = _calls(holder, "_extract_gseq")
    for bid_var in sorted(set(bindings) - groups):
        errors.append(
            f"line {bindings[bid_var]} binds a provider from {bid_var!r} but never reads "
            "its group: a reselection that changes the provider without changing the "
            "group leases the new provider against the old group"
        )

    return errors


# ---------------------------------------------------------------- the extractor
@pytest.mark.parametrize(
    "bid,expected",
    [
        ({"bid": {"id": {"provider": "akash1x", "gseq": 3}}}, 3),  # nested Console shape
        ({"id": {"provider": "akash1x", "gseq": 2}}, 2),  # flattened shape
        ({"gseq": "5"}, 5),  # string-typed, as Console sometimes sends
        ({"bid": {"id": {"provider": "akash1x"}}}, None),  # shape omits it
        ({}, None),
        ("not-a-dict", None),
    ],
)
def test_extract_gseq_reads_every_shape(bid, expected):
    assert _extract_gseq(bid) == expected


def test_unreadable_group_is_none_not_one():
    """None means NOT SUPPLIED. Returning 1 here would rebuild the bug one level down,
    where the caller can no longer tell a real group-1 bid from an unreadable shape."""

    assert _extract_gseq({"bid": {"id": {}}}) is None
    assert _extract_gseq({"gseq": "not-a-number"}) is None


def test_extract_gseq_never_returns_a_string():
    """The core rejects a str with ValueError, and BidObservation construction is inside
    a `except (TypeError, ValueError): continue` — so a string here would not raise, it
    would silently DROP the bid from the auction."""

    assert _extract_gseq({"gseq": "7"}) == 7
    assert isinstance(_extract_gseq({"gseq": "7"}), int)


# ---------------------------------------------------------------- the wiring
def test_lease_is_created_against_the_winning_group():
    errors = wiring_errors(DEPLOY.read_text())
    assert not errors, f"deploy.py leases the wrong group: {errors}"


def test_removing_the_group_argument_is_a_red_mutation():
    """BOTH DIRECTIONS, without depending on git history.

    ⚠ The first version of this control compared against `HEAD:just_akash/deploy.py`.
    That is a control that DISARMS ITSELF: the moment this change is committed, HEAD
    contains the fix, the "before" source passes, and the assertion inverts. A control
    whose meaning depends on when it runs is worse than none, because it fails loudly
    for the wrong reason and invites deletion.

    Mutating the live source is stable under commits, rebases, and relocation."""

    source = DEPLOY.read_text()
    assert not wiring_errors(source), "not green before mutation"

    mutated, n = re.subn(r"\n\s*gseq=\w+,", "", source, count=1)
    assert n == 1, "the mutation matched nothing — the anchor has moved, fix the anchor"
    assert mutated != source

    errors = wiring_errors(mutated)
    assert errors, "dropping the group argument did not fail the check"
    assert any("passes no group" in e for e in errors), errors


def test_failing_to_rebind_after_a_redeploy_is_a_red_mutation():
    """The second limb, mutated on its own. Both sites must be independently load-bearing
    — a check that only ever fails because of limb 1 is one check wearing two names.

    ⚠ The mutation DERIVES the variable it removes from the parsed source rather than
    hardcoding an adjacency. The first attempt matched `price_denom, X = _redeploy…` on
    one line; `ruff format` then wrapped the call into a parenthesised continuation and
    the regex silently matched nothing — a mutation that no longer mutates makes its
    test pass for the wrong reason."""

    source = DEPLOY.read_text()
    tree = ast.parse(source)

    # The name the lease call reads — whatever it is called today.
    names = {
        k.value.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "create_lease"
        for k in n.keywords
        if k.arg == "gseq" and isinstance(k.value, ast.Name)
    }
    assert len(names) == 1, f"expected exactly one group variable, found {names}"
    name = names.pop()

    mutated, n = re.subn(rf",\s*{re.escape(name)}\b(?=\s*=)", "", source, count=1)
    assert n == 1, f"the mutation did not remove {name!r} from the re-deploy unpack"
    assert mutated != source
    # The mutation must not have broken the OTHER limb, or this test would pass on
    # limb 1's error and prove nothing about limb 2.
    assert ast.parse(mutated) is not None

    errors = wiring_errors(mutated)
    assert errors, "leaving the group stale across a re-created order did not fail"
    assert any("re-deploy unpack" in e for e in errors), errors


def test_dropping_the_group_from_a_reselection_path_is_a_red_mutation():
    """Limb 3's own control — the one that was missing when the defect got through.

    Removes the group refresh from a reselection path while LEAVING the provider
    refresh in place. That is precisely the shape CodeRabbit caught on review: the
    provider changes, the group does not, and the lease is struck against the group
    of a bid that no longer applies."""

    source = DEPLOY.read_text()
    assert not wiring_errors(source), "not green before mutation"

    # Find a bid variable that BOTH extractors read, and blind the group side of it.
    mutated, n = re.subn(r"\n[ \t]*\w+ = _extract_gseq\(next_bid\)[^\n]*", "", source, count=1)
    assert n == 1, "the reselection anchor has moved — fix the anchor, not the assertion"
    assert mutated != source

    errors = wiring_errors(mutated)
    assert errors, "a provider-without-group reselection did not fail the check"
    assert any("never reads" in e for e in errors), errors
