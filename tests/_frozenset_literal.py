"""Resolve a frozenset/set literal from its AST — splat-aware, fail-loud.

The naive walker `isinstance(elt, ast.Name)` for the splat branch is **wrong**:
a splat inside a collection literal — `frozenset({A, *B, C})` or `[{A, *B}]` —
appears in Python's AST as `Starred(Name('B'))`, not bare `Name('B')`. A walker
that handles only `Constant` and `Name` silently drops every splatted member
and reports a smaller set than the source actually contains.

Measured 2026-09-18: the just-akash ↔ blazing cross-repo check walked
`ACCEPTED_ZERO_FAILURE_REASONS = frozenset({..., *PER_ATTEMPT_MINT_REASONS, ...})`
and returned 17 unique members when the source had 20 — the 3 splatted members
were silently dropped because the splat branch never fired.

This module is the splat-aware replacement. It is the precondition for any
derive-the-list membership test that has to mirror the source of truth:
a buggy walker encodes its undercount as the spec, and the next person who
uses it is misled the same way.

## Fail-loud contract

A walker that silently drops a shape it does not understand is the **same**
bug as the one this module fixes, only narrower. Every member shape we don't
recognize raises — we never return a partial set with a missing member.

Recognised member shapes inside `frozenset({...})`:
  - `Constant(value=str)`              literal string member
  - `Constant(value=<non-str>)`        raise (frozensets of non-strings are not
                                        a contract-binding shape; flag for review)
  - `Starred(value=Name(id=X))`        splat of a module-level binding
  - `Starred(<other shape>)`           raise (function-call splat, comprehension
                                        splat, conditional splat, f-string splat)
  - `Name(id=X)`                       bare-name reference to a module-level
                                        binding (uncommon in frozenset literals;
                                        tolerated)
  - `Comprehension`                    raise (set comprehension, not a literal)
  - `IfExp`                            raise (ternary, not a literal)
  - `JoinedStr`                        raise (f-string, not a literal)
  - `Call`                             raise (function call, not a literal)

Recognised binding shapes for the splatted name:
  - `frozenset({...})` / `set({...})`  recurse and union the members
  - `({...})` / `[...]` / `(...)`      sequence of `Constant(str)` literals
  - anything else                      raise (function calls, comprehensions,
                                        imports, attribute access, etc.)

Tested by `tests/test_frozenset_literal_walker.py` with controls for every
shape that could regress, including the loud-failure cases.

The module is threaded through resolution because Python's AST does not carry
parent pointers. `_resolve_binding` cannot walk up without help, so the
caller (`resolve_frozensets_in_module`) passes the parsed Module through.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable


def resolve_frozenset(call_node: ast.Call, module: ast.Module) -> set[str]:
    """Resolve the unique members of a frozenset({...}) / set({...}) call.

    Raises on any member shape it does not recognise. Never silently drops
    a member — partial results are the bug we are fixing.
    """
    if not isinstance(call_node, ast.Call):
        raise TypeError(
            f"expected ast.Call wrapping frozenset()/set(); got {type(call_node).__name__}"
        )
    fn_name = getattr(call_node.func, "id", None)
    if fn_name not in {"frozenset", "set"}:
        raise ValueError(f"expected frozenset()/set(); got call to {fn_name!r}")

    args = call_node.args
    if len(args) != 1:
        raise ValueError(f"expected one positional argument (the set literal); got {len(args)}")

    literal = args[0]
    if not isinstance(literal, ast.Set):
        raise TypeError(
            f"expected a set literal {{...}}; got {type(literal).__name__}. "
            f"This walker does not handle set comprehensions; convert to a "
            f"frozenset literal of constants first."
        )

    members: set[str] = set()
    for elt in literal.elts:
        members.update(_resolve_member(elt, module))
    return members


def _resolve_member(elt: ast.expr, module: ast.Module) -> set[str]:
    """Resolve one member of a set literal to its string values.

    Raises on any unrecognised shape.
    """
    if isinstance(elt, ast.Constant):
        if isinstance(elt.value, str):
            return {elt.value}
        # Non-string constant — not in our contract shape. Raise loud.
        raise TypeError(
            f"set-literal member is a non-string Constant "
            f"({type(elt.value).__name__} = {elt.value!r}). "
            f"This walker only handles string members — the contract we "
            f"derive from cross-repo Python sources is a string allow-list. "
            f"Convert or filter before walking."
        )

    if isinstance(elt, ast.Starred):
        inner = elt.value
        if isinstance(inner, ast.Name):
            return set(_resolve_binding(inner.id, module))
        # Any other splat shape is a silent-drop trap. Raise.
        raise TypeError(
            f"unhandled splat shape inside set literal: {ast.dump(elt)}. "
            f"`*f(...)`, `*(x for x in y)`, `*(a if c else b)`, and f-string "
            f"splats are NOT supported. The walker cannot statically "
            f"resolve dynamic splats; convert to a frozenset literal of "
            f"constants first."
        )

    if isinstance(elt, ast.Name):
        # Bare-Name inside a set literal is unusual but tolerated.
        return set(_resolve_binding(elt.id, module))

    # Anything else: comprehension, IfExp, JoinedStr (f-string), Call, etc.
    # All of these would be silently dropped by a partial walker. Raise.
    raise TypeError(
        f"unhandled element shape inside set literal: {ast.dump(elt)}. "
        f"Only string Constants, Starred(Name) splats, and bare Name "
        f"references are supported."
    )


def _resolve_binding(name: str, module: ast.Module) -> Iterable[str]:
    """Walk the module-level statements to find the binding for `name`.

    Raises if the binding does not exist, is not a recognised shape, or
    recursively contains an unrecognised shape.
    """
    for stmt in module.body:
        if isinstance(stmt, ast.Assign):
            target = stmt.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                value = stmt.value
                if isinstance(value, ast.Call):
                    fn_name = getattr(value.func, "id", None)
                    if fn_name in {"frozenset", "set"}:
                        return resolve_frozenset(value, module)
                    # Other Call shapes (function calls, constructor calls) —
                    # not resolvable by static AST walk. Raise.
                    raise TypeError(
                        f"binding {name!r} is a Call to {fn_name!r}, not "
                        f"frozenset()/set(). This walker only resolves static "
                        f"frozenset/set bindings."
                    )
                if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
                    out: list[str] = []
                    for elt in value.elts:
                        # Each element of a sequence binding must also be a
                        # string Constant — anything else raises. Sequences
                        # do not get the splat treatment; only set literals do.
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            out.append(elt.value)
                        else:
                            raise TypeError(
                                f"sequence binding {name!r} contains a "
                                f"non-string-Constant element: {ast.dump(elt)}"
                            )
                    return out
                raise TypeError(
                    f"binding {name!r} is not a frozenset()/set() call or a "
                    f"sequence of string literals; got {type(value).__name__}. "
                    f"This walker only resolves static frozenset/set bindings "
                    f"or sequences of string literals."
                )
    raise NameError(f"no module-level binding for {name!r}")


def resolve_frozensets_in_module(source: str) -> dict[str, set[str]]:
    """Convenience: parse `source` and return every frozenset()/set() binding.

    Returns a dict {binding_name: set_of_members}. **Propagates all errors** —
    does NOT silently skip bindings that fail to resolve. The fail-loud
    contract is part of the module's correctness story: a partial result
    would re-introduce the silent-drop bug this walker fixes.

    Use the caller-facing `resolve_frozenset` directly if you want to handle
    a specific binding and catch errors around it.
    """
    tree = ast.parse(source)
    out: dict[str, set[str]] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            target = stmt.targets[0]
            if isinstance(target, ast.Name) and isinstance(stmt.value, ast.Call):
                fn_name = getattr(stmt.value.func, "id", None)
                if fn_name in {"frozenset", "set"}:
                    out[target.id] = resolve_frozenset(stmt.value, tree)
    return out
