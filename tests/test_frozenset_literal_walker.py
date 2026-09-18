"""Pin the splat-expansion behaviour and fail-loud contract of
`tests._frozenset_literal`.

The cross-repo check that produced [just-akash#393](https://github.com/Digital-Frontier-LDA/just-akash/issues/393)
needed a frozenset walker. The naive `isinstance(elt, ast.Name)` shape silently
dropped every `*SPLAT` member; the trap-case test below pins the corrected
behaviour so the next person who uses the walker cannot reintroduce the
undercount.

The walker is also pinned to **fail loudly** on any member shape it does not
recognise — a partial result would be the same bug this module fixes, only
narrower. The shape-raises test below pins that contract.

Mutant coverage:
  - drop the `isinstance(elt, ast.Starred)` branch → 5 trap tests fail with
    the exact "got 17, expected 20" undercount.
  - drop a shape-raise (e.g., silent-skip on Comprehension) → shape-raise
    tests fail because the walker returns a partial set.
  - silently swallow errors in `resolve_frozensets_in_module` → fail-loud
    tests fail because no exception is raised.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests._frozenset_literal import resolve_frozenset, resolve_frozensets_in_module


def _call_in(src: str) -> tuple[ast.Call, ast.Module]:
    """Parse `src` and return the LAST top-level Assign's value as a Call, plus the module.

    Tests use this rather than `tree.body[-1].value` because pyright sees
    `body[i]` as the union type `stmt` (not all subclasses have `.value`),
    and reaching for `.value` without a narrowing check is structurally the
    same bug class the walker exists to fix — a caller that hands the walker
    the wrong node shape and proceeds without checking. Pinning the narrowing
    here mirrors what the walker does for its own inputs.
    """
    tree = ast.parse(src)
    last = tree.body[-1]
    assert isinstance(last, ast.Assign), (
        f"expected last top-level stmt to be an Assign; got {type(last).__name__}"
    )
    value = last.value
    assert isinstance(value, ast.Call), (
        f"expected last Assign value to be a Call; got {type(value).__name__}"
    )
    return value, tree


# ---------------------------------------------------------------------------
# Splat-handling controls — the original bug
# ---------------------------------------------------------------------------


def test_frozenset_with_splat_in_the_middle_resolves_all_members() -> None:
    """The trap. 5 distinct members across Constant + Starred + Constant.

    Naive walker checking `isinstance(elt, ast.Name)` returns 3, missing `*B`.
    """
    src = """
B = frozenset({"B1", "B2"})
X = frozenset({"A", *B, "C"})
"""
    parsed = resolve_frozensets_in_module(src)
    assert parsed["X"] == {"A", "B1", "B2", "C"}, (
        f"splat in middle of set literal dropped members; got {parsed['X']}. "
        f"A walker that handles only `ast.Name` (not `ast.Starred(Name)`) "
        f"returns {{'A', 'C'}} here — that is the undercount trap."
    )


def test_frozenset_without_splat_resolves_all_members() -> None:
    """Regression guard: bare-Constant set still works after the Starred branch is added."""
    src = """
X = frozenset({"A", "B", "C"})
"""
    parsed = resolve_frozensets_in_module(src)
    assert parsed["X"] == {"A", "B", "C"}


def test_splat_at_the_end_of_set_literal_resolves_all_members() -> None:
    """Starred at the trailing position: still expands `*B`."""
    src = """
B = frozenset({"B1", "B2"})
X = frozenset({"A", *B})
"""
    parsed = resolve_frozensets_in_module(src)
    assert parsed["X"] == {"A", "B1", "B2"}


def test_splat_at_the_start_of_set_literal_resolves_all_members() -> None:
    """Starred at the leading position: still expands `*A`."""
    src = """
A = frozenset({"A1", "A2"})
X = frozenset({*A, "B"})
"""
    parsed = resolve_frozensets_in_module(src)
    assert parsed["X"] == {"A1", "A2", "B"}


def test_splatted_binding_can_itself_be_a_frozenset() -> None:
    """Recursive resolution: `*B` where `B = frozenset({...})`."""
    src = """
B = frozenset({"B1", "B2"})
X = frozenset({"A", *B})
"""
    parsed = resolve_frozensets_in_module(src)
    assert parsed["X"] == {"A", "B1", "B2"}


def test_walker_resolves_a_splat_plus_literal_sample_to_twenty_members() -> None:
    """The walker resolves a parser sample with a splat + literal mix to 20 unique members.

    This test is a property of the WALKER plus the SAMPLE, not a claim about
    any live cross-repo contract. The sample in `tests/fixtures/frozenset_with_splat_sample.py`
    is a frozen parser sample (its docstring explains). The live contract check
    is #393's membership test, which fetches the producer file at the branch
    head and walks it; it is intentionally a separate test from this one.

    If this test ever fails, the walker regressed — the sample did not change
    without an explicit update here. The 20-member count is the splat-expansion
    control: 17 literals + 3 splat members from the splatted binding. A walker
    that drops the splat returns 17 (which is the original defect this module
    exists to prevent).
    """
    fixture = Path(__file__).parent / "fixtures" / "frozenset_with_splat_sample.py"
    src = fixture.read_text()
    parsed = resolve_frozensets_in_module(src)
    sample_set = parsed["ACCEPTED_ZERO_FAILURE_REASONS"]
    assert len(sample_set) == 20, (
        f"the walker must resolve the splat-plus-literal sample to 20 unique "
        f"members. Got {len(sample_set)}: {sorted(sample_set)}. "
        f"If this is 17, the Starred(Name) handling regressed — the splat is "
        f"being silently dropped. (This is a walker-property assertion; it is "
        f"NOT a claim about any cross-repo contract.)"
    )
    assert sample_set == {
        "NO_ELIGIBLE_BIDDER",
        "RUNNER_PAT_INVALID",
        "RUNNER_PAT_MISSING",
        "WALLET_TX_CONTENTION",
        "WALLET_UNDERFUNDED",
        "RUNNER_SDL_TEMPLATE_MISSING",
        "RUNNER_SDL_UNRENDERED",
        "RUNNER_TOKEN_UNMINTED",
        "PROVIDER_OFFLINE",
        "PROVIDER_INVALID_VERSION",
        "PROVIDER_NO_CAPACITY",
        "PROVIDER_NO_BID",
        "PROVIDER_STATUS_QUERY_FAILED",
        "PROVIDER_UNKNOWN",
        "NO_BIDS_RECEIVED",
        "BIDS_MALFORMED",
        "BIDS_STALE",
        "BIDS_FOREIGN_ONLY",
        "SDL_ERROR",
        "CONFIG_ERROR",
    }


def test_just_akash_code_enum_walks_clean() -> None:
    """Sanity: a module with no frozenset()/set() bindings returns an empty dict.

    Confirms the walker doesn't crash on a class body and gives the next
    reader a worked example of how the walker handles a module with no
    target bindings.
    """
    src = """
class Code:
    WALLET_INSUFFICIENT_CREDIT = "WALLET_INSUFFICIENT_CREDIT"
    WALLET_LOW_CREDIT = "WALLET_LOW_CREDIT"
"""
    assert resolve_frozensets_in_module(src) == {}


# ---------------------------------------------------------------------------
# Refusal cases — guard the call shape
# ---------------------------------------------------------------------------


def test_resolve_frozenset_raises_on_non_frozenset_call() -> None:
    """The walker refuses to silently walk the wrong call shape."""
    call, tree = _call_in("X = list({'A', 'B'})")
    with pytest.raises(ValueError, match="expected frozenset"):
        resolve_frozenset(call, tree)


def test_resolve_frozenset_raises_on_set_comprehension() -> None:
    """Set comprehensions are not frozenset literals — raise, do not silently return partial."""
    call, tree = _call_in("X = frozenset(x for x in ['A', 'B'])")
    with pytest.raises(TypeError, match="set literal"):
        resolve_frozenset(call, tree)


# ---------------------------------------------------------------------------
# Fail-loud contract — unrecognised member shapes raise
# ---------------------------------------------------------------------------


def test_walker_raises_on_function_call_splat() -> None:
    """`*some_function()` cannot be statically resolved. Raise, do not drop."""
    src = """
def some_function():
    return ["B1", "B2"]
X = frozenset({"A", *some_function()})
"""
    call, tree = _call_in(src)
    with pytest.raises(TypeError, match="unhandled splat shape"):
        resolve_frozenset(call, tree)


def test_walker_raises_on_generator_splat() -> None:
    """`*(x for x in y)` cannot be statically resolved. Raise."""
    src = """
X = frozenset({"A", *(x for x in ["B1", "B2"])})
"""
    call, tree = _call_in(src)
    with pytest.raises(TypeError, match="unhandled splat shape"):
        resolve_frozenset(call, tree)


def test_walker_raises_on_fstring_member() -> None:
    """f-string members cannot be statically resolved to a string. Raise."""
    src = """
X = frozenset({"A", f"B{x}"})
"""
    call, tree = _call_in(src)
    with pytest.raises(TypeError, match="unhandled element shape"):
        resolve_frozenset(call, tree)


def test_walker_raises_on_ifexp_member() -> None:
    """Ternary `a if cond else b` is not a constant. Raise."""
    src = """
X = frozenset({"A", "B" if True else "C"})
"""
    call, tree = _call_in(src)
    with pytest.raises(TypeError, match="unhandled element shape"):
        resolve_frozenset(call, tree)


def test_walker_raises_on_non_string_constant_member() -> None:
    """Integer/float/bool Constants are not in our contract shape. Raise."""
    src = """
X = frozenset({"A", 42})
"""
    call, tree = _call_in(src)
    with pytest.raises(TypeError, match="non-string Constant"):
        resolve_frozenset(call, tree)


def test_walker_raises_on_undereferenceable_splat_name() -> None:
    """`*SOMETHING_NOT_DEFINED` cannot resolve. Raise, do not return partial."""
    src = """
X = frozenset({"A", *SOMETHING_NOT_DEFINED})
"""
    call, tree = _call_in(src)
    with pytest.raises(NameError, match="no module-level binding"):
        resolve_frozenset(call, tree)


def test_walker_raises_when_splat_binding_is_a_function_call() -> None:
    """`*build_list` where `build_list = other_function()` — binding is a Call, not a frozenset.

    Statically we cannot know what `other_function()` returns. Raise.
    """
    src = """
def other_function():
    return ["A", "B"]
build_list = other_function()
X = frozenset({"C", *build_list})
"""
    call, tree = _call_in(src)
    with pytest.raises(TypeError, match="Call to"):
        resolve_frozenset(call, tree)


def test_walker_raises_when_splat_binding_is_a_list_comprehension() -> None:
    """`*LST` where `LST = [x for x in y]` — list comprehensions are not statically resolvable."""
    src = """
LST = [x for x in ["B1", "B2"]]
X = frozenset({"A", *LST})
"""
    call, tree = _call_in(src)
    with pytest.raises(TypeError, match="not a frozenset"):
        resolve_frozenset(call, tree)


def test_resolve_frozensets_in_module_does_not_silently_skip_failed_bindings() -> None:
    """If one binding in a module fails to resolve, the whole walk propagates.

    `resolve_frozensets_in_module` does NOT silently skip — the fail-loud
    contract is part of correctness. A partial result is the bug.
    """
    src = """
Y = frozenset({"A", *MISSING})
X = frozenset({"B", "C"})
"""
    with pytest.raises(NameError, match="no module-level binding"):
        resolve_frozensets_in_module(src)
