#!/usr/bin/env python3
"""Inventory every recognized executable Akash close call in this repository.

The result is deliberately structural and line-number independent.  The checked-in
manifest freezes the call-site population: adding, removing, or moving a close into
a different callable requires reviewing the changed census in the same commit.
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from pathlib import Path

PYTHON_CALLEES = frozenset(
    {
        "close_all_deployments",
        "close_deployment",
        "destroy",
        "destroy_owned_deployment",
        "robust_destroy",
        "_destroy",
    }
)
_SHELL_CLOSE = re.compile(
    r'(?:\bjust-akash\b|\bjust\b|"?\$\{JA\[@\]\}"?)\s+'
    r"(?P<command>destroy(?:-all)?)\b"
)


class _Calls(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.sites: Counter[tuple[str, str, str, str]] = Counter()

    def _scope(self, node: ast.AST) -> None:
        self.scope.append(node.name)  # type: ignore[attr-defined]
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _scope
    visit_AsyncFunctionDef = _scope
    visit_ClassDef = _scope

    def visit_Call(self, node: ast.Call) -> None:
        callee = ""
        if isinstance(node.func, ast.Attribute):
            callee = node.func.attr
        elif isinstance(node.func, ast.Name):
            callee = node.func.id
        if callee in PYTHON_CALLEES:
            self.sites[("python", self.path, ".".join(self.scope), callee)] += 1
        self.generic_visit(node)


def census(root: Path) -> list[dict[str, str | int]]:
    sites: Counter[tuple[str, str, str, str]] = Counter()
    for source_root in (root / "just_akash", root / "canary"):
        if not source_root.is_dir():
            raise RuntimeError(f"close-site source root is missing: {source_root}")
        for path in sorted(source_root.rglob("*.py")):
            visitor = _Calls(path.relative_to(root).as_posix())
            visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
            sites.update(visitor.sites)

    shell_paths = [root / "Justfile"]
    for suffix in ("*.yml", "*.yaml", "*.sh"):
        shell_paths.extend((root / ".github").rglob(suffix))
    for path in sorted(set(shell_paths)):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for match in _SHELL_CLOSE.finditer(line):
                sites[
                    (
                        "shell",
                        path.relative_to(root).as_posix(),
                        "",
                        match.group("command"),
                    )
                ] += 1

    return [
        {"kind": kind, "path": path, "scope": scope, "callee": callee, "count": count}
        for (kind, path, scope, callee), count in sorted(sites.items())
    ]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    print(json.dumps(census(root), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
