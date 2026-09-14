"""No Akash lifecycle check may go green on a result it did not measure (blazing#1183).

blazing#1183 asks for this ratchet in each repo that runs Akash lifecycle code. This is the
just-akash copy of the scanner merged in blazing#1194, with its own population and baseline.
The shape it stops, seen in both repos:

* a close verifier that printed "closure is UNKNOWN" and passed (blazing#1178);
* a conservation assert that mapped UNMEASURED to exit 0 (blazing#1179);
* four paid-cleanup call sites here that no test could tell from their removal (#358);
* a create-time owner lookup that read a Console outage as "not the owner" and HELD a paid
  lease open while reporting a clean verdict (#363).

This ratchet fails on a NEW occurrence of any of the four shapes in an Akash lifecycle
file:

  R1  a broad `except` (bare, `Exception`, `BaseException`) whose body neither logs,
      re-raises, exits nor uses the exception it bound;
  R2  `|| true`, `continue-on-error`, or `if-no-files-found: ignore` on a step whose
      output is a closure / lease / receipt verdict;
  R3  an exit-code translation that turns a documented UNKNOWN / UNMEASURED / UNVERIFIED
      outcome into success;
  R4  a verifier whose acceptance threshold is below its own observer count, or below two.

Existing occurrences are in `BASELINE`, each with a reason. The baseline is EXACT: a new hit
fails, and so does an entry that no longer matches.

⛔ ENTRIES ARE KEYED BY CONTENT, NOT BY LINE. A key is the rule, the file, the enclosing scope
(the Python function, or the workflow's job/step) and a hash of the offending line with its
whitespace and Python comments normalised away. So inserting lines above an entry changes
nothing, while editing the offending line itself is a NEW hit plus a STALE entry, which is the
re-read this ratchet exists to force. Identical lines in one scope are COUNTED, never collapsed:
each entry carries how many occurrences it covers. Line numbers appear only in messages.

⛔ LIFECYCLE FILES ARE DERIVED BY CONTENT, NOT LISTED. A file is in scope when it handles an
Akash deployment: it names a DSEQ, invokes or imports just-akash / akash-lease-core, calls
the Console deployments or leases API, or runs `akash tx deployment`. A renamed or new
teardown script is therefore in scope without anyone remembering to add it. `MIN_*` floors
make sure the derivation cannot quietly go blind.

⛔ FAIL CLOSED ON WHAT CANNOT BE READ.
* An `except` naming something the scanner cannot resolve to a narrow exception counts as
  broad.
* A `continue-on-error` holding an expression counts as true.
* Aliased imports are resolved: `import logging as lg`, `from sys import exit as bye`,
  `from builtins import Exception as E`.

Known blind spots, named so nobody reads more into a green run than it proves:
* Shell is read line by line, not parsed. R3 sees `case` arms and `then … fi` blocks that
  mention the unmeasured vocabulary. It does not see a translation spread across helper
  functions.
* R4 reads Python observer counts (`len(<observers>) <op> N`). A quorum computed in shell
  or `jq` is not seen.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import importlib
import io
import re
import sys
import textwrap
import tokenize
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (".github", "just_akash", "canary", "scripts")
SUFFIXES = {".py", ".sh", ".yml", ".yaml"}

LIFECYCLE_MARKER = re.compile(
    r"dseq|just[-_]akash|akash[-_]lease[-_]core|console-api\.akash|/v1/deployments|/v1/leases"
    r"|akash\s+tx\s+deployment",
    re.IGNORECASE,
)

# Measured on 2026-09-14 at 7d1ba709: 71 candidate files, of which 60 are lifecycle
# (14 workflows, 43 Python, 3 shell). This package IS the lifecycle tool, so most of it is in
# scope. Floors sit below the counts, so an ordinary deletion does not trip them but a
# derivation that stops matching does.
MIN_SCAN_CANDIDATES = 60
MIN_LIFECYCLE_FILES = 50
MIN_LIFECYCLE_WORKFLOWS = 11
MIN_LIFECYCLE_PYTHON = 35

VERDICT_WORDS = re.compile(
    r"clos(?:e|ed|es|ing|ure)|lease|receipt|dseq|teardown|tear down|destroy|reap|leak"
    r"|conservation|verif|assert",
    re.IGNORECASE,
)
# Case-insensitive: this repo publishes its unmeasured outcomes as lower-case step outputs
# (`deregister_failed=unmeasured`). Comment lines are blanked before shell is read.
UNMEASURED_WORDS = re.compile(r"\b(?:UNKNOWN|UNMEASURED|UNVERIFIED)\b", re.IGNORECASE)
# `found` is what blazing's quorum helper counts; kept so a port of that helper is read.
OBSERVER_NAME = re.compile(
    r"seen|found|observ|endpoint|source|answer|agree|quorum|snapshot", re.IGNORECASE
)

LOG_METHODS = {
    "debug",
    "info",
    "warning",
    "warn",
    "error",
    "exception",
    "critical",
    "log",
    "print_exc",
}
EXIT_CALLS = {"sys.exit", "os._exit", "exit", "quit"}


@dataclass(frozen=True, order=True)
class Hit:
    rule: str
    where: str  # "path:line", for messages only
    detail: str = ""
    scope: str = ""  # the enclosing function, or the workflow job/step
    text: str = ""  # the offending line, normalised

    @property
    def path(self) -> str:
        return self.where.rsplit(":", 1)[0]

    @property
    def key(self) -> tuple[str, str, str, str]:
        digest = hashlib.sha256(self.text.encode()).hexdigest()[:12]
        return (self.rule, self.path, self.scope, digest)


# ── population ─────────────────────────────────────────────────────────────────────────


def scan_candidates(root: Path = REPO_ROOT) -> list[Path]:
    """Every source, shell and workflow file under the scan roots, before content derivation."""
    candidates: list[Path] = []
    for top in SCAN_ROOTS:
        base = root / top
        if base.is_dir():
            candidates.extend(
                path
                for path in sorted(base.rglob("*"))
                if path.suffix in SUFFIXES and path.is_file() and "__pycache__" not in path.parts
            )
    return candidates


def lifecycle_files(root: Path = REPO_ROOT) -> list[Path]:
    """Files that handle an Akash deployment, plus any file that runs one of those scripts.

    The second clause matters: `akash-leak-monitor.yml` names no DSEQ itself. It runs
    `scripts/akash_leak_monitor.py` and translates that script's exit code, and the
    translation is exactly what R3 reads.
    """
    texts = {
        path: path.read_text(encoding="utf-8", errors="replace") for path in scan_candidates(root)
    }
    found = {path for path, text in texts.items() if LIFECYCLE_MARKER.search(text)}
    while True:
        scripts = {p.relative_to(root).as_posix() for p in found if p.suffix in (".py", ".sh")}
        callers = {
            p for p, text in texts.items() if p not in found and any(s in text for s in scripts)
        }
        # And the other direction: a quorum a verifier LOADS is part of the verifier. The chain
        # quorum helper in blazing (scripts/akash_observer_independence.py) carries no DSEQ; its
        # consumers load it from their own directory, by file name or by module name.
        loaded = {
            p
            for p in texts
            if p not in found
            and p.suffix == ".py"
            and p.name != "__init__.py"
            and any(
                f.suffix == ".py"
                and f.parent == p.parent
                and (
                    f'with_name("{p.name}")' in texts[f]
                    or re.search(
                        rf"^\\s*(?:from|import)\\s+{re.escape(p.stem)}\\b", texts[f], re.M
                    )
                )
                for f in found
            )
        }
        if not callers | loaded:
            return sorted(found)
        found |= callers | loaded


# ── R1: a broad except that swallows ───────────────────────────────────────────────────


class _Aliases(ast.NodeVisitor):
    """Resolve local names to dotted origins: `import logging as lg` → lg = logging."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names[alias.asname or alias.name.split(".")[0]] = (
                alias.name if alias.asname else alias.name.split(".")[0]
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.names[alias.asname or alias.name] = f"{node.module or ''}.{alias.name}"


def _dotted(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value, aliases)
        return f"{inner}.{node.attr}" if inner else None
    return None


def _builtin_exception(name: str) -> type | None:
    value = getattr(builtins, name.removeprefix("builtins."), None)
    return value if isinstance(value, type) and issubclass(value, BaseException) else None


def _is_broad(handler_type: ast.AST | None, aliases: dict[str, str], module: ast.Module) -> bool:
    if handler_type is None:
        return True
    if isinstance(handler_type, ast.Tuple):
        return any(_is_broad(element, aliases, module) for element in handler_type.elts)
    dotted = _dotted(handler_type, aliases)
    if dotted is None:
        return True  # a call, subscript, …: cannot be read, so it is broad
    exc = (
        _builtin_exception(dotted) if "." not in dotted or dotted.startswith("builtins.") else None
    )
    if exc is not None:
        return exc in (Exception, BaseException)
    if "." in dotted:
        module_name, _, leaf = dotted.rpartition(".")
        # A standard-library name is resolved for real: `socket.gaierror` is a narrow class
        # although its name is lower-case, and `from errno import ENOENT` is not a class at all.
        if module_name.split(".")[0] in sys.stdlib_module_names:
            try:
                value = getattr(importlib.import_module(module_name), leaf)
            except (ImportError, AttributeError):
                return True  # cannot be read: fail closed
            if isinstance(value, type) and issubclass(value, BaseException):
                return value in (Exception, BaseException)
            return True
        # Otherwise only the spelling is available. `akash_lease_core.ChainError` is a class;
        # `from errors import RETRYABLE` may be a tuple holding Exception, so it fails closed.
        return not (leaf[:1].isupper() and not leaf.isupper())
    local_classes = {n.name for n in module.body if isinstance(n, ast.ClassDef)}
    if dotted in local_classes:
        return False
    assigned = [
        n.value
        for n in ast.walk(module)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == dotted for t in n.targets)
    ]
    if assigned and all(isinstance(v, (ast.Tuple, ast.Name, ast.Attribute)) for v in assigned):
        return any(_is_broad(v, aliases, module) for v in assigned)
    return True  # an unresolvable name fails closed


def _handles(handler: ast.ExceptHandler, aliases: dict[str, str]) -> bool:
    for node in (n for stmt in handler.body for n in ast.walk(stmt)):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Name) and handler.name and node.id == handler.name:
            return True
        if isinstance(node, ast.Call):
            dotted = _dotted(node.func, aliases) or ""
            leaf = dotted.rsplit(".", 1)[-1]
            if dotted in {"print", "builtins.print"}:
                return True
            if dotted in EXIT_CALLS:
                # `sys.exit(0)` in a handler is the swallow in its loudest disguise: it ends
                # the process GREEN. Only an exit that is readably non-zero handles.
                code = node.args[0] if node.args else None
                if isinstance(code, ast.Constant) and code.value not in (0, None, False):
                    return True
                continue
            if isinstance(node.func, ast.Attribute) and leaf in LOG_METHODS:
                return True
            if dotted.startswith(("logging.", "warnings.", "traceback.")) or dotted.endswith(
                "stderr.write"
            ):
                return True
    return False


def python_swallows(source: str, path: str, line_offset: int = 0) -> list[Hit]:
    module = ast.parse(source)
    visitor = _Aliases()
    visitor.visit(module)
    return [
        Hit("R1", f"{path}:{node.lineno + line_offset}", "broad except swallows")
        for node in ast.walk(module)
        if isinstance(node, ast.ExceptHandler)
        and _is_broad(node.type, visitor.names, module)
        and not _handles(node, visitor.names)
    ]


_TEXT_EXCEPT = re.compile(
    r"^(?P<indent>\s*)except\b(?P<type>[^:]*?)(?:\s+as\s+(?P<name>\w+))?\s*:(?P<rest>.*)$"
)
_TEXT_HANDLED = re.compile(
    r"\braise\b|\bprint\(|\blog(?:ger|ging)?\.|\bwarn|stderr|\bexit\(|::(?:error|warning)"
)


def embedded_python_swallows(text: str, path: str) -> list[Hit]:
    """R1 for Python embedded in workflow `run:` blocks and shell heredocs, read as text."""
    hits: list[Hit] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _TEXT_EXCEPT.match(line)
        if not match:
            continue
        kind = match["type"].strip()
        if kind and not re.search(r"\b(?:Base)?Exception\b", kind):
            continue
        body = [match["rest"]]
        indent = len(match["indent"])
        for following in lines[index + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            body.append(following)
        joined = "\n".join(body)
        if _TEXT_HANDLED.search(joined) or (
            match["name"] and re.search(rf"\b{match['name']}\b", joined)
        ):
            continue
        hits.append(Hit("R1", f"{path}:{index + 1}", "broad except swallows"))
    return hits


# ── R2 / R3 on workflows and shell ─────────────────────────────────────────────────────


def _mapping(node: yaml.Node | None) -> dict[str, yaml.Node]:
    if not isinstance(node, yaml.MappingNode):
        return {}
    return {key.value: value for key, value in node.value if isinstance(key, yaml.ScalarNode)}


def _run_lines(scalar: yaml.ScalarNode, text_lines: list[str]) -> list[tuple[int, str]]:
    """(1-based file line, content) for each line of a `run:` scalar, verified against the file."""
    value = scalar.value
    if scalar.style in ("|", ">"):
        first = scalar.start_mark.line + 1
        out, cursor = [], first
        for content in value.splitlines():
            if content.strip():
                while cursor < len(text_lines) and content.strip() not in text_lines[cursor]:
                    cursor += 1
                if cursor >= len(text_lines):
                    raise AssertionError(f"could not place run line {content!r}")  # fail closed
            out.append((cursor + 1, content))
            cursor += 1 if content.strip() else 0
        return out
    return [(scalar.start_mark.line + 1, value)]


_OR_TRUE = re.compile(r"\|\|\s*(?:true|:)(?:\s|;|$|\))")


def _truthy_continue(node: yaml.Node | None) -> bool:
    return isinstance(node, yaml.ScalarNode) and node.value.strip().lower() not in ("false", "")


def shell_translations(lines: list[tuple[int, str]], path: str) -> list[Hit]:
    """R3: a `case` arm or `then` block that names an unmeasured outcome and ends in success."""
    hits: list[Hit] = []
    # Comment lines are blanked, not removed, so indices still map to file lines.
    texts = ["" if content.lstrip().startswith("#") else content for _, content in lines]
    for index, content in enumerate(texts):
        case = re.match(r'\s*case\s+"?\$\{?(\w+)\}?"?\s+in\b', content)
        if case:
            variable = case[1]
            end = next(
                (j for j in range(index + 1, len(texts)) if re.match(r"\s*esac\b", texts[j])),
                len(texts) - 1,
            )
            after = "\n".join(texts[end : end + 6])
            propagates = re.search(rf'exit\s+"?\$\{{?{variable}\}}?"?', after) is not None
            arm_start = None
            for j in range(index + 1, end + 1):
                if arm_start is None and re.match(
                    r"""\s*[\w"'*.$-]+(?:\|[\w"'*.$-]+)*\)""", texts[j]
                ):
                    arm_start = j
                if arm_start is not None and (";;" in texts[j] or j == end):
                    arm = "\n".join(texts[arm_start : j + 1])
                    arm_start = None
                    if not UNMEASURED_WORDS.search(arm):
                        continue
                    exits_nonzero = re.search(r'\bexit\s+(?:[1-9]|"?\$)', arm) or re.search(
                        rf"\b{variable}=[1-9]", arm
                    )
                    succeeds = re.search(r"\bexit\s+0\b", arm) or re.search(
                        rf"\b{variable}=0\b", arm
                    )
                    if succeeds or (not exits_nonzero and not propagates):
                        hits.append(Hit("R3", f"{path}:{lines[j][0]}", "unmeasured arm exits 0"))
        if re.search(r"\bthen\b", content):
            block = []
            for j in range(index, len(texts)):
                block.append(texts[j])
                if j > index and re.match(r"\s*(?:fi|elif|else)\b", texts[j]):
                    break
            joined = "\n".join(block)
            if UNMEASURED_WORDS.search(joined) and re.search(r"\bexit\s+0\b", joined):
                hits.append(Hit("R3", f"{path}:{lines[index][0]}", "unmeasured branch exits 0"))
    return hits


def workflow_hits(path: Path, lifecycle_paths: set[str]) -> list[Hit]:
    rel = path.relative_to(REPO_ROOT).as_posix() if path.is_relative_to(REPO_ROOT) else path.name
    text = path.read_text(encoding="utf-8", errors="replace")
    return workflow_text_hits(text, rel, lifecycle_paths)


def workflow_text_hits(text: str, rel: str, lifecycle_paths: set[str]) -> list[Hit]:
    text_lines = text.splitlines()
    root = yaml.compose(text)
    hits: list[Hit] = []
    for job_node in _mapping(_mapping(root).get("jobs")).values():
        job = _mapping(job_node)
        steps = job.get("steps")
        step_nodes = steps.value if isinstance(steps, yaml.SequenceNode) else []
        job_is_verdict = False
        for step_node in step_nodes:
            step = _mapping(step_node)
            label = " ".join(
                step[k].value for k in ("name", "id") if isinstance(step.get(k), yaml.ScalarNode)
            )
            run = step.get("run")
            run_lines = _run_lines(run, text_lines) if isinstance(run, yaml.ScalarNode) else []
            run_text = "\n".join(content for _, content in run_lines)
            invokes_lifecycle = any(p in run_text for p in lifecycle_paths)
            verdict = bool(VERDICT_WORDS.search(label)) or invokes_lifecycle
            job_is_verdict = job_is_verdict or verdict
            with_ = _mapping(step.get("with"))
            if verdict and _truthy_continue(step.get("continue-on-error")):
                line = step["continue-on-error"].start_mark.line + 1
                hits.append(Hit("R2", f"{rel}:{line}", "continue-on-error on a verdict step"))
            ignore = with_.get("if-no-files-found")
            if (
                verdict
                and isinstance(ignore, yaml.ScalarNode)
                and ignore.value.strip() == "ignore"
            ):
                hits.append(
                    Hit("R2", f"{rel}:{ignore.start_mark.line + 1}", "if-no-files-found: ignore")
                )
            if verdict:
                hits.extend(
                    Hit("R2", f"{rel}:{line}", "|| true on a verdict step")
                    for line, content in run_lines
                    if _OR_TRUE.search(content) and not content.lstrip().startswith("#")
                )
            hits.extend(shell_translations(run_lines, rel))
            hits.extend(_relocate(run_text, run_lines, rel))
        uses = job.get("uses")
        job_is_verdict = job_is_verdict or (
            isinstance(uses, yaml.ScalarNode) and any(p in uses.value for p in lifecycle_paths)
        )
        if job_is_verdict and _truthy_continue(job.get("continue-on-error")):
            line = job["continue-on-error"].start_mark.line + 1
            hits.append(
                Hit("R2", f"{rel}:{line}", "continue-on-error on a job with a verdict step")
            )
    return hits


def _relocate(run_text: str, run_lines: list[tuple[int, str]], rel: str) -> list[Hit]:
    """Embedded-Python R1 hits, renumbered from run-block lines to file lines."""
    return [
        Hit(hit.rule, f"{rel}:{run_lines[int(hit.where.rsplit(':', 1)[1]) - 1][0]}", hit.detail)
        for hit in embedded_python_swallows(run_text, "run")
    ]


def shell_hits(text: str, rel: str) -> list[Hit]:
    lines = list(enumerate(text.splitlines(), start=1))
    hits = [
        Hit("R2", f"{rel}:{line}", "|| true in a lifecycle script")
        for line, content in lines
        if _OR_TRUE.search(content)
        and not content.lstrip().startswith("#")
        and VERDICT_WORDS.search(content)
    ]
    hits.extend(shell_translations(lines, rel))
    hits.extend(embedded_python_swallows(text, rel))
    return hits


# ── R3 / R4 on Python ──────────────────────────────────────────────────────────────────


def _mentions_unmeasured(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and UNMEASURED_WORDS.search(
            child.id.upper().replace("_", " ")
        ):
            return True
        if isinstance(child, ast.Attribute) and UNMEASURED_WORDS.search(
            child.attr.upper().replace("_", " ")
        ):
            return True
        if (
            isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and UNMEASURED_WORDS.search(child.value)
        ):
            return True
    return False


def _is_zero(node: ast.AST | None, constants: Mapping[str, object]) -> bool:
    if isinstance(node, ast.Constant):
        return node.value == 0 and not isinstance(node.value, bool)
    if isinstance(node, ast.Name):
        return constants.get(node.id) == 0
    return False


def python_translations(module: ast.Module, aliases: dict[str, str], rel: str) -> list[Hit]:
    constants = {
        target.id: node.value.value
        for node in module.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    hits: list[Hit] = []
    for node in ast.walk(module):
        if isinstance(node, ast.Assign) and _is_zero(node.value, {}):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id.startswith("EXIT")
                    and _mentions_unmeasured(target)
                ):
                    hits.append(Hit("R3", f"{rel}:{node.lineno}", f"{target.id} = 0"))
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if key is not None and _mentions_unmeasured(key) and _is_zero(value, constants):
                    hits.append(Hit("R3", f"{rel}:{key.lineno}", "unmeasured key maps to 0"))
        if isinstance(node, ast.If) and _mentions_unmeasured(node.test):
            for stmt in (n for s in node.body for n in ast.walk(s)):
                exits_zero = isinstance(stmt, ast.Return) and _is_zero(stmt.value, constants)
                if isinstance(stmt, ast.Call) and (
                    _dotted(stmt.func, aliases) or ""
                ) in EXIT_CALLS | {f"{m}.exit" for m in ("sys",)}:
                    exits_zero = (
                        exits_zero
                        or (bool(stmt.args) and _is_zero(stmt.args[0], constants))
                        or not stmt.args
                    )
                if exits_zero:
                    line = stmt.lineno if isinstance(stmt, (ast.Return, ast.Call)) else node.lineno
                    hits.append(Hit("R3", f"{rel}:{line}", "unmeasured branch returns 0"))
    return hits


def _observer_len(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "len"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and OBSERVER_NAME.search(node.args[0].id)
    ):
        return node.args[0].id
    return None


QUORUM_KEYWORDS = {"need", "required", "quorum", "min_observers"}


def python_quorums(module: ast.Module, rel: str) -> list[Hit]:
    """R4: a verifier may not accept fewer observers than it counts, nor fewer than two.

    Three readings of the same rule:
    * thresholds on one observer collection (`len(seen) == 2` to stop, `len(seen) < 2` to
      refuse) must agree, and none may be below two. Parameter defaults count as constants,
      so `need: int = 1` is read;
    * a call passing a quorum keyword (`need=`, `required=`, …) below two;
    * a function that gathers observers from an `*observations` call and counts nothing.
    """
    constants = {
        target.id: node.value.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, int)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    hits: list[Hit] = []
    for call in (n for n in ast.walk(module) if isinstance(n, ast.Call)):
        for keyword in call.keywords:
            value = keyword.value.value if isinstance(keyword.value, ast.Constant) else None
            if (
                keyword.arg in QUORUM_KEYWORDS
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value < 2
            ):
                hits.append(Hit("R4", f"{rel}:{call.lineno}", f"{keyword.arg}={value}"))
    for function in (
        n for n in ast.walk(module) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        scope = dict(constants)
        positional = function.args.posonlyargs + function.args.args
        defaults = zip(
            positional[len(positional) - len(function.args.defaults) :],
            function.args.defaults,
            strict=True,
        )
        kw_defaults = zip(function.args.kwonlyargs, function.args.kw_defaults, strict=True)
        for arg, default in (*defaults, *kw_defaults):
            if isinstance(default, ast.Constant) and isinstance(default.value, int):
                scope[arg.arg] = default.value
                # A quorum parameter whose default is below two: `quorum: int = 1` lets every
                # caller that omits it succeed on one observer, however the count is taken.
                if arg.arg in QUORUM_KEYWORDS and default.value < 2:
                    hits.append(
                        Hit(
                            "R4",
                            f"{rel}:{default.lineno}",
                            f"{arg.arg} defaults to {default.value}",
                        )
                    )
        thresholds: dict[str, list[tuple[int, int]]] = {}
        for compare in (
            n for n in ast.walk(function) if isinstance(n, ast.Compare) and len(n.ops) == 1
        ):
            name = _observer_len(compare.left)
            right = compare.comparators[0]
            if name is None or not isinstance(right, (ast.Constant, ast.Name)):
                continue
            value = right.value if isinstance(right, ast.Constant) else scope.get(right.id)
            if not isinstance(value, int) or isinstance(value, bool):
                continue
            # Normalise to "the number of observers this comparison treats as enough".
            op = type(compare.ops[0])
            needed = {
                ast.Lt: value,
                ast.GtE: value,
                ast.Eq: value,
                ast.Gt: value + 1,
                ast.LtE: value + 1,
            }.get(op)
            if needed is not None:
                thresholds.setdefault(name, []).append((needed, compare.lineno))
        for name, found in thresholds.items():
            count = max(needed for needed, _ in found)
            hits.extend(
                Hit("R4", f"{rel}:{line}", f"len({name}) accepts {needed} of {count}")
                for needed, line in found
                if needed < 2 or needed < count
            )
        for assign in (
            n
            for n in ast.walk(function)
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
        ):
            call = assign.value
            if not isinstance(call, ast.Call):
                continue
            leaf = (_dotted(call.func, {}) or "").rsplit(".", 1)[-1]
            # Counted anywhere in the function is enough: a consumer may validate observations
            # into a derived list (`seen`) and count that instead.
            if leaf.lower().endswith("observations") and not thresholds:
                hits.append(
                    Hit(
                        "R4",
                        f"{rel}:{assign.lineno}",
                        f"observers from {leaf}() are never counted",
                    )
                )
    return hits


def python_hits(text: str, rel: str) -> list[Hit]:
    module = ast.parse(text)
    visitor = _Aliases()
    visitor.visit(module)
    return (
        python_swallows(text, rel)
        + python_translations(module, visitor.names, rel)
        + python_quorums(module, rel)
    )


# ── survey ─────────────────────────────────────────────────────────────────────────────


def invocation_spellings(root: Path, lifecycle_paths: set[str]) -> set[str]:
    """How a workflow runs lifecycle code here: rarely by path.

    `python -m just_akash.test_shell_e2e` names a module, and `uv run just-akash destroy` names
    the console script. Both are read from the tree rather than listed: dotted names from the
    lifecycle paths, console-script names from `[project.scripts]` in pyproject.toml.
    """
    spellings = {path[:-3].replace("/", ".") for path in lifecycle_paths if path.endswith(".py")}
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        section = re.search(
            r"^\[project\.scripts\]\n((?:[^\[\n].*\n|\n)*)", pyproject.read_text(), re.M
        )
        for name in re.findall(r"^([\w.-]+)\s*=", section[1] if section else "", re.M):
            spellings.add(
                f"{name} "
            )  # a trailing space, so `just-akash` does not match `just-akash-x`
    return spellings


# ── content keys ───────────────────────────────────────────────────────────────────────


def _normalise(line: str, python: bool) -> str:
    """The offending line without layout: whitespace collapsed, and Python comments dropped."""
    if python:
        try:
            tokens = [
                token.string
                for token in tokenize.generate_tokens(io.StringIO(line.strip() + "\n").readline)
                if token.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE)
            ]
            return " ".join(t for t in tokens if t)
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass  # a line that does not tokenise alone is kept as text
    return " ".join(line.split())


def _python_scopes(text: str) -> list[tuple[int, int, str]]:
    """(first line, last line, qualified name) for every function and class."""
    scopes: list[tuple[int, int, str]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                scopes.append((child.lineno, child.end_lineno or child.lineno, name))
                visit(child, f"{name}.")
            else:
                visit(child, prefix)

    visit(ast.parse(text), "")
    return scopes


def _workflow_scopes(text: str) -> list[tuple[int, int, str]]:
    """(first line, last line, "job" or "job/step") for every job and step."""
    scopes: list[tuple[int, int, str]] = []
    jobs = yaml.compose(text)
    jobs_node = _mapping(jobs).get("jobs")
    if not isinstance(jobs_node, yaml.MappingNode):
        return scopes
    for key, job_node in jobs_node.value:
        job_id = key.value
        scopes.append((job_node.start_mark.line + 1, job_node.end_mark.line, job_id))
        steps = _mapping(job_node).get("steps")
        for index, step_node in enumerate(
            steps.value if isinstance(steps, yaml.SequenceNode) else []
        ):
            step = _mapping(step_node)
            label = next(
                (
                    step[k].value
                    for k in ("name", "id")
                    if isinstance(step.get(k), yaml.ScalarNode)
                ),
                f"#{index}",
            )
            scopes.append(
                (step_node.start_mark.line + 1, step_node.end_mark.line, f"{job_id}/{label}")
            )
    return scopes


def _innermost(scopes: list[tuple[int, int, str]], line: int) -> str:
    containing = [(last - first, name) for first, last, name in scopes if first <= line <= last]
    return min(containing)[1] if containing else "<file>"


def keyed(hits: list[Hit], text: str, rel: str) -> list[Hit]:
    """The same hits, each given the scope and normalised text its baseline key is built from.

    For a Python R1 hit the offending "line" is the whole handler, `except …:` plus its body,
    unparsed (so comments and layout drop out). Keying on the `except Exception:` line alone
    would make every such handler in one function the same entry, and an edit to what one of
    them swallows would change no key.
    """
    python = rel.endswith(".py")
    scopes = (
        _python_scopes(text)
        if python
        else _workflow_scopes(text)
        if rel.startswith(".github/workflows/")
        else []
    )
    handlers = (
        {n.lineno: n for n in ast.walk(ast.parse(text)) if isinstance(n, ast.ExceptHandler)}
        if python
        else {}
    )
    lines = text.splitlines()
    out = []
    for hit in hits:
        line = int(hit.where.rsplit(":", 1)[1])
        handler = handlers.get(line) if hit.rule == "R1" else None
        if handler is not None:
            normalised = " ".join(ast.unparse(handler).split())
        else:
            offending = lines[line - 1] if 0 < line <= len(lines) else ""
            normalised = _normalise(offending, python)
        out.append(Hit(hit.rule, hit.where, hit.detail, _innermost(scopes, line), normalised))
    return out


def survey(root: Path = REPO_ROOT) -> tuple[list[Path], list[Hit]]:
    files = lifecycle_files(root)
    lifecycle_paths = {p.relative_to(root).as_posix() for p in files if p.suffix in (".py", ".sh")}
    lifecycle_paths |= invocation_spellings(root, lifecycle_paths)
    hits: list[Hit] = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py":
            found = python_hits(text, rel)
        elif path.suffix == ".sh":
            found = shell_hits(text, rel)
        elif rel.startswith(".github/workflows/"):
            found = workflow_text_hits(text, rel, lifecycle_paths)
        else:
            found = []
        # A set removes a site reported twice at the SAME line; identical lines elsewhere stay.
        hits.extend(keyed(sorted(set(found)), text, rel))
    return files, sorted(hits)


def unexplained(hits: list[Hit], baseline: Mapping[tuple[str, str, str, str], tuple[int, str]]):
    """(hits beyond what the baseline covers, baseline entries with fewer hits than they claim)."""
    current = Counter(hit.key for hit in hits)
    new = [
        hit
        for key, count in current.items()
        if count > baseline.get(key, (0, ""))[0]
        for hit in [h for h in hits if h.key == key][baseline.get(key, (0, ""))[0] :]
    ]
    stale = [
        (key, count, reason)
        for key, (count, reason) in baseline.items()
        if current.get(key, 0) < count
    ]
    return new, stale


# ── baseline ───────────────────────────────────────────────────────────────────────────

# Measured on 2026-09-14 at 7d1ba709: 75 hits (R1 40, R2 29, R3 3, R4 3) under 74 keys. Each
# entry says why the site is not a false green today, or names a defect tracked in an issue.
# The comment above each entry is the start of its normalised offending text, for finding it.
# Each key line carries `# pragma: allowlist secret`: its 12-hex digest is a content hash, and
# detect-secrets reads any short hex run as a possible credential.
# ⛔ SHRINK-ONLY: delete an entry when its site is fixed. Adding one is a reviewed decision,
# not a way to make this test pass.
BASELINE: dict[tuple[str, str, str, str], tuple[int, str]] = {
    # R1 — broad except that swallows
    # except Exception: return (UNKNOWN, lived)
    (
        "R1",
        "canary/closure.py",
        "attribute_detailed",
        "4d49026ddf73",  # pragma: allowlist secret
    ): (
        1,
        "an unexpected shape is attributed UNKNOWN, never a false blame",
    ),
    # except Exception: return (UNKNOWN, None)
    (
        "R1",
        "canary/closure.py",
        "attribute_detailed",
        "54c0f40f79d8",  # pragma: allowlist secret
    ): (
        1,
        "an unreadable chain is attributed UNKNOWN, never a cause",
    ),
    # except Exception: block = None
    (
        "R1",
        "canary/closure.py",
        "attribute_detailed",
        "e22e90f0a54b",  # pragma: allowlist secret
    ): (
        1,
        "a missing settlement block falls back to the escrow-only verdict",
    ),
    # except Exception: result = UNKNOWN
    ("R1", "canary/collect.py", "merge", "bb8daa36833e"): (  # pragma: allowlist secret
        1,
        "a broken attributor records cause UNKNOWN for that lease",
    ),
    # except Exception: pass
    ("R1", "just_akash/_diagnostics.py", "emit", "a2ad58d9f50d"): (  # pragma: allowlist secret
        1,
        "the diagnostics emitter itself; writing to stderr is what failed",
    ),
    # except Exception: pass
    (
        "R1",
        "just_akash/_e2e.py",
        "_confirm_settled_single_reader",
        "a2ad58d9f50d",  # pragma: allowlist secret
    ): (
        1,
        "a failed probe leaves got_open False, so the audit returns None and fails closed",
    ),
    # except Exception: snapshot = None
    (
        "R1",
        "just_akash/_lease_verification.py",
        "consensus",
        "f9a071386b9c",  # pragma: allowlist secret
    ): (
        1,
        "an endpoint that errors abstains; consensus refuses without two agreeing snapshots",
    ),
    # except Exception: return False
    (
        "R1",
        "just_akash/_lease_verification.py",
        "deployment_closed",
        "7bea3bdfca89",  # pragma: allowlist secret
    ): (
        1,
        "an unreadable deployment reads as not closed (False), the safe direction",
    ),
    # except Exception: _FEATURE_CAP_MS = {}
    (
        "R1",
        "just_akash/analyze_telemetry.py",
        "<file>",
        "d911204aafec",  # pragma: allowlist secret
    ): (
        1,
        "module-import fallback for standalone telemetry analysis; no lease verdict",
    ),
    # except Exception: continue
    (
        "R1",
        "just_akash/chain.py",
        "_corroborated_deployment_group_names",
        "af10980773d7",  # pragma: allowlist secret
    ): (
        1,
        "a failed source contributes no authority to the corroborated group names",
    ),
    # except Exception: return None
    (
        "R1",
        "just_akash/chain.py",
        "_owner_close_evidence.fetch",
        "eaf9362805e8",  # pragma: allowlist secret
    ): (
        1,
        "a transport failure abstains from the height-pinned read",
    ),
    # except Exception: return None
    (
        "R1",
        "just_akash/chain.py",
        "_read_source_document",
        "eaf9362805e8",  # pragma: allowlist secret
    ): (
        1,
        "an unavailable trust path contributes no vote",
    ),
    # except Exception: unresolved.append(position) continue
    (
        "R1",
        "just_akash/cleanup_stale.py",
        "_resolve_distinct_accounts",
        "242e4308cd07",  # pragma: allowlist secret
    ): (
        1,
        "an unidentifiable key is appended to `unresolved` and reported",
    ),
    # except Exception: address = ''
    (
        "R1",
        "just_akash/cli.py",
        "_warn_if_listing_degraded",
        "e2953895fa08",  # pragma: allowlist secret
    ): (
        1,
        "no address means no corroboration; corroborate_listing classifies the unreadable source",
    ),
    # except Exception: owner = ''
    (
        "R1",
        "just_akash/deploy.py",
        "_report_suspected_orphans",
        "548e8c096c38",  # pragma: allowlist secret
    ): (
        1,
        "orphan diagnosis on an already-failing create; owner degrades to empty",
    ),
    # except Exception: names = []
    (
        "R1",
        "just_akash/deploy.py",
        "_report_suspected_orphans",
        "ee5c87462490",  # pragma: allowlist secret
    ): (
        1,
        "orphan diagnosis weakens its claim; the create failure is still raised",
    ),
    # except Exception: return None
    (
        "R1",
        "just_akash/orphan_detect.py",
        "active_leases_for",
        "eaf9362805e8",  # pragma: allowlist secret
    ): (
        1,
        "a read failure returns None (UNKNOWN), never 'no leases'",
    ),
    # except Exception: return None
    (
        "R1",
        "just_akash/orphan_detect.py",
        "live_orders_for",
        "eaf9362805e8",  # pragma: allowlist secret
    ): (
        1,
        "a read failure returns None (UNKNOWN), never 'no orders'",
    ),
    # except Exception: return None
    (
        "R1",
        "just_akash/runner_probe.py",
        "_pod_started",
        "eaf9362805e8",  # pragma: allowlist secret
    ): (
        1,
        "a read error returns None (unknown), never 'no pod'",
    ),
    # except Exception: runs = []
    (
        "R1",
        "just_akash/runner_probe.py",
        "_run_noop_job",
        "4851c1bbcf1b",  # pragma: allowlist secret
    ): (
        1,
        "an unparseable run listing keeps the probe waiting until its deadline",
    ),
    # except Exception: return False
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_availability_ready",
        "7bea3bdfca89",  # pragma: allowlist secret
    ): (
        1,
        "readiness unreadable counts as not ready, the safe direction",
    ),
    # except Exception: return None
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_dead_state",
        "eaf9362805e8",  # pragma: allowlist secret
    ): (
        1,
        "state unreadable returns None; a transient read error is not treated as dead",
    ),
    # except Exception: continue
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_delete_resolved_provider_smoke_receipts",
        "af10980773d7",  # pragma: allowlist secret
    ): (1, "an unreadable receipt is kept on disk for the upload step"),
    # except Exception: receipt, receipt_dseq = (None, None)
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_deploy",
        "255c018c294b",  # pragma: allowlist secret
    ): (
        1,
        "DEFECT (#376): a failed receipt read or reconciliation is swallowed unlogged",
    ),
    # except Exception: pass
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_deploy",
        "a2ad58d9f50d",  # pragma: allowlist secret
    ): (
        1,
        "DEFECT (#376): interrupt-path cleanup errors are swallowed unlogged",
    ),
    # except Exception: return False
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_exec_works",
        "7bea3bdfca89",  # pragma: allowlist secret
    ): (
        1,
        "exec probe failure reports 'unreachable' in the diagnostic",
    ),
    # except Exception: pass
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_explain_deploy_failed",
        "a2ad58d9f50d",  # pragma: allowlist secret
    ): (
        1,
        "best-effort evidence printing on an already-failed deploy",
    ),
    # except Exception: return None
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_ingress_uri",
        "eaf9362805e8",  # pragma: allowlist secret
    ): (
        1,
        "no ingress URI yet returns None, which keeps waiting",
    ),
    # except Exception: pass
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_observe_after_cap",
        "a2ad58d9f50d",  # pragma: allowlist secret
    ): (
        1,
        "post-cap diagnostic probe; the timeout verdict is already recorded",
    ),
    # except Exception: return 'unknown'
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_pkg_version",
        "c96db9388dd9",  # pragma: allowlist secret
    ): (
        1,
        "telemetry version label degrades to 'unknown'",
    ),
    # except Exception: return 'unreachable'
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_probe_in_pod_marker",
        "fcca9498f9d9",  # pragma: allowlist secret
    ): (
        1,
        "in-pod marker probe reports 'unreachable' as its own diagnostic value",
    ),
    # except Exception: avail = None
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_record_ingress_timeout",
        "b465c434c100",  # pragma: allowlist secret
    ): (
        1,
        "diagnostic evidence for an ingress timeout; the verdict never changes",
    ),
    # except Exception: info = {}
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_record_no_bid_evidence",
        "3e1b56a528ff",  # pragma: allowlist secret
    ): (
        1,
        "best-effort no-bid evidence; provider info degrades to empty",
    ),
    # except Exception: dead = False
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_record_ready_timeout",
        "45caeacc1ef1",  # pragma: allowlist secret
    ): (
        1,
        "diagnostic evidence for a ready timeout; the verdict never changes",
    ),
    # except Exception: avail = None
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_record_ready_timeout",
        "b465c434c100",  # pragma: allowlist secret
    ): (
        1,
        "diagnostic evidence for a ready timeout; the verdict never changes",
    ),
    # except Exception: avail = None
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_record_update_timeout",
        "b465c434c100",  # pragma: allowlist secret
    ): (
        1,
        "diagnostic evidence for an update timeout; the timeout verdict stands",
    ),
    # except Exception: return None
    (
        "R1",
        "just_akash/smoke_providers.py",
        "_service_availability",
        "eaf9362805e8",  # pragma: allowlist secret
    ): (
        1,
        "availability unreadable returns None, which keeps waiting",
    ),
    # except Exception: return False
    (
        "R1",
        "just_akash/transport/lease_shell.py",
        "LeaseShellTransport._send_resize",
        "7bea3bdfca89",  # pragma: allowlist secret
    ): (1, "terminal resize is best-effort and returns False; not a lease verdict"),
    # except Exception: continue
    (
        "R1",
        "just_akash/wallet_pool.py",
        "_chain_height",
        "af10980773d7",  # pragma: allowlist secret
    ): (
        1,
        "LCD failover; every endpoint failing raises RuntimeError",
    ),
    # except Exception: return None
    (
        "R1",
        "just_akash/wallet_pool.py",
        "_credit_at",
        "eaf9362805e8",  # pragma: allowlist secret
    ): (
        1,
        "an unprovable endpoint abstains; _quorum_uact requires two agreeing readings",
    ),
    # R2 — || true / continue-on-error / if-no-files-found: ignore on a verdict step
    # if-no-files-found: ignore
    (
        "R2",
        ".github/workflows/ci.yml",
        "e2e-secrets/Preserve unresolved deployment receipt",
        "d4f17979db7f",  # pragma: allowlist secret
    ): (1, "a verified closure deletes the receipt, so absence is the success case"),
    # if-no-files-found: ignore
    (
        "R2",
        ".github/workflows/ci.yml",
        "e2e-shell/Preserve unresolved lease-shell deployment receipt",
        "d4f17979db7f",  # pragma: allowlist secret
    ): (1, "a verified closure deletes the receipt, so absence is the success case"),
    # owner="$(python3 -c 'import json;print(json.load(open("credit.json")).…
    (
        "R2",
        ".github/workflows/provider-canary.yml",
        "canary/Collect from inside every deployment",
        "294057ef03aa",  # pragma: allowlist secret
    ): (1, "empty owner degrades attribution to cause=unknown, handled by the next `if`"),
    # sed 's/^/ /' orphan-scan.err | tail -5 || true
    (
        "R2",
        ".github/workflows/provider-canary.yml",
        "canary/Collect from inside every deployment",
        "fcc98e63aadc",  # pragma: allowlist secret
    ): (1, "prints the orphan-scan stderr tail inside a ::warning branch"),
    # uv run just-akash tag --dseq "$dseq" --name "canary-${provider}" || tr…
    (
        "R2",
        ".github/workflows/provider-canary.yml",
        "canary/Deploy canaries for providers missing one",
        "3c79a14f4e9b",  # pragma: allowlist secret
    ): (1, "the tag is a local convenience; canary identity is read off the deployment"),
    # status=$(python -c "import json,sys;print(json.load(open('credit.json'…
    (
        "R2",
        ".github/workflows/provider-canary.yml",
        "canary/Deploy canaries for providers missing one",
        "6568cd459338",  # pragma: allowlist secret
    ): (1, "empty status from an unreadable credit.json fails != OK, which skips creation"),
    # dseq=$(grep -oE 'dseq[ =:]+[0-9]+' "deploy-${provider}.log" | grep -oE…
    (
        "R2",
        ".github/workflows/provider-canary.yml",
        "canary/Deploy canaries for providers missing one",
        "f89c8c101706",  # pragma: allowlist secret
    ): (1, "empty dseq from the deploy log is handled by the next `if`"),
    # continue-on-error: true
    (
        "R2",
        ".github/workflows/provider-canary.yml",
        "canary/Re-resolve targets after any deploy",
        "9ff6534d73ee",  # pragma: allowlist secret
    ): (1, "re-resolve after deploy; a failure leaves the first resolve's targets.json"),
    # cat credit.json 2>/dev/null || true
    (
        "R2",
        ".github/workflows/provider-canary.yml",
        "canary/Read deploy credit",
        "0ef5ef2a6909",  # pragma: allowlist secret
    ): (
        1,
        "prints credit.json into the log; the gate reads the file below",
    ),
    # timeout 120 uv run just-akash balance --json || true
    (
        "R2",
        ".github/workflows/provider-canary.yml",
        "canary/Read deploy credit",
        "7fdc3c5699ef",  # pragma: allowlist secret
    ): (
        1,
        "full balance printed for the log only",
    ),
    # [ -s credit.err ] && cat credit.err || true
    (
        "R2",
        ".github/workflows/provider-canary.yml",
        "canary/Read deploy credit",
        "ce92d9193896",  # pragma: allowlist secret
    ): (
        1,
        "prints credit.err into the log only",
    ),
    # echo "missing: $(cut -f1 missing.txt | tr '\n' ' ' || true)"
    (
        "R2",
        ".github/workflows/provider-canary.yml",
        "canary/Resolve which providers still have a live canary",
        "e7a2d2405555",  # pragma: allowlist secret
    ): (1, "echo of the missing-provider list for the log only"),
    # python3 -m just_akash.analyze_telemetry accrued.jsonl --shim-survey ||…
    (
        "R2",
        ".github/workflows/provider-smoke.yml",
        "report/Aggregate + latency gate",
        "a4ce1a73097f",  # pragma: allowlist secret
    ): (1, "advisory shim survey; the SLO gate below still runs"),
    # if-no-files-found: ignore
    (
        "R2",
        ".github/workflows/provider-smoke.yml",
        "smoke/Preserve unresolved provider-smoke deployment receipts",
        "d4f17979db7f",  # pragma: allowlist secret
    ): (1, "a verified closure deletes the receipts, so absence is the success case"),
    # continue-on-error: true
    (
        "R2",
        ".github/workflows/provider-smoke.yml",
        "smoke/Snapshot deploy credit",
        "9ff6534d73ee",  # pragma: allowlist secret
    ): (1, "credit snapshot is observational; the accrue render uses the last snapshot"),
    # WALLET=$(awk -F': +' '/^[[:space:]]*Wallet:/{print $2; exit}' /tmp/ja.…
    (
        "R2",
        ".github/workflows/runner-pool.yml",
        "pool/Provision (retry down candidates, tag before waiting)",
        "0f2a0cb14a3d",  # pragma: allowlist secret
    ): (1, "awk on a missing log; wallet is published only for a DSEQ round (#348)"),
    # "${JA[@]}" tag --dseq "$DSEQ" --name "${TAG_PREFIX}-${RUN_ID}" || true
    (
        "R2",
        ".github/workflows/runner-pool.yml",
        "pool/Provision (retry down candidates, tag before waiting)",
        "20d2c8273401",  # pragma: allowlist secret
    ): (1, "tag before the destroy retry loop; the destroy is what is checked"),
    # VERDICT_MINTED=$(printf '%s' "$VERDICT_RESP" | sed -n 's/.*"token" *: …
    (
        "R2",
        ".github/workflows/runner-pool.yml",
        "pool/Provision (retry down candidates, tag before waiting)",
        "2cd988bbbefb",  # pragma: allowlist secret
    ): (1, "token extraction for masking; an empty token is refused by the guard below"),
    # WALLET_UACT=$(awk '/^[[:space:]]*Wallet available:/{print $(NF-1); exi…
    (
        "R2",
        ".github/workflows/runner-pool.yml",
        "pool/Provision (retry down candidates, tag before waiting)",
        "45e6a922369c",  # pragma: allowlist secret
    ): (1, "wallet-available figure for the log; not a gate"),
    # PROVIDER=$(awk -F': +' '/^[[:space:]]*Provider:/{print $2; exit}' /tmp…
    (
        "R2",
        ".github/workflows/runner-pool.yml",
        "pool/Provision (retry down candidates, tag before waiting)",
        "49ddce72bb8f",  # pragma: allowlist secret
    ): (1, "awk on a missing log; empty PROVIDER closes the DSEQ below"),
    # GATE_DEAD=$(printf '%s\n' "$RUNNER_VERSIONS" | grep -cxF -- "null" || …
    (
        "R2",
        ".github/workflows/runner-pool.yml",
        "pool/Provision (retry down candidates, tag before waiting)",
        "58f543325b69",  # pragma: allowlist secret
    ): (1, "`grep -c` exits 1 on zero matches while printing 0; the count is gated"),
    # --bid-wait-retry 120 "${SELECT_ARGS[@]}" "${PROV_ARGS[@]}" 2>&1 | tee …
    (
        "R2",
        ".github/workflows/runner-pool.yml",
        "pool/Provision (retry down candidates, tag before waiting)",
        "76a6828d2ce6",  # pragma: allowlist secret
    ): (1, "deploy outcome is read from the log; DSEQ and LEASE_CREATE_FAILED recovery below"),
    # ONLINE=$(printf '%s\n' "$RUNNER_IDS" | grep -c . || true)
    (
        "R2",
        ".github/workflows/runner-pool.yml",
        "pool/Provision (retry down candidates, tag before waiting)",
        "7e97ecd1cba8",  # pragma: allowlist secret
    ): (1, "`grep -c` exits 1 on zero matches while printing 0; the count is gated"),
    # VERDICT_RESP=$(gh api --method POST "orgs/${ORG}/actions/runners/regis…
    (
        "R2",
        ".github/workflows/runner-pool.yml",
        "pool/Provision (retry down candidates, tag before waiting)",
        "83245f3e0d2e",  # pragma: allowlist secret
    ): (1, "captures the token-mint response; the guard below classifies it"),
    # GATE_WRONG=$(printf '%s\n' "$RUNNER_VERSIONS" | grep -vxF -- "null" | …
    (
        "R2",
        ".github/workflows/runner-pool.yml",
        "pool/Provision (retry down candidates, tag before waiting)",
        "cbec588f7541",  # pragma: allowlist secret
    ): (1, "`grep -c` exits 1 on zero matches while printing 0; the count is gated"),
    # BAD_PAGES=$(printf '%s' "$RUNNER_PAGES" | jq -r 'select(has("runners")…
    (
        "R2",
        ".github/workflows/runner-pool.yml",
        "pool/Provision (retry down candidates, tag before waiting)",
        "f11f90afd4b3",  # pragma: allowlist secret
    ): (1, "`grep -c` exits 1 on zero matches while printing 0; the count is gated"),
    # ' /tmp/ja.log || true)
    (
        "R2",
        ".github/workflows/runner-pool.yml",
        "pool/Provision (retry down candidates, tag before waiting)",
        "febe634fb7db",  # pragma: allowlist secret
    ): (1, "awk on a missing log yields empty DSEQ, the no-deployment branch"),
    # sed -e 's/^/ /' /tmp/owner.err >&2 || true
    (
        "R2",
        ".github/workflows/runner-teardown.yml",
        "teardown/Close the lease",
        "071b2a82f956",  # pragma: allowlist secret
    ): (
        2,
        "indents resolve-owner stderr inside a branch that exits 1",
    ),
    # R3 — an unmeasured outcome translated to success
    # if [ ! -f missing.txt ]; then
    (
        "R3",
        ".github/workflows/provider-canary.yml",
        "canary/Say so if canaries are missing and we did not deploy",
        "7e3fc57c5f73",  # pragma: allowlist secret
    ): (1, "missing.txt absent is a named ::error; the step continues so collect still publishes"),
    # if [ "$GH_RC" -ne 0 ]; then
    (
        "R3",
        ".github/workflows/runner-teardown.yml",
        "teardown/De-register offline runners for this label",
        "5137207716fb",  # pragma: allowlist secret
    ): (
        1,
        "registrations only: published as outputs deregistered and deregister_failed=unmeasured",
    ),
    # if [ "$IDS_RC" -ne 0 ]; then
    (
        "R3",
        ".github/workflows/runner-teardown.yml",
        "teardown/De-register offline runners for this label",
        "664d14642554",  # pragma: allowlist secret
    ): (
        1,
        "registrations only: published as output deregister_failed=unmeasured, plus a ::warning",
    ),
    # R4 — observer counts
    # if len ( seen ) == 1 :
    (
        "R4",
        "just_akash/test_shell_e2e.py",
        "_dseq_from_deploy_log",
        "48f6bd5cecac",  # pragma: allowlist secret
    ): (
        1,
        "`seen` is a set of DSEQs found in a log, not chain observers",
    ),
    # observations = build_observations ( owner , state , height )
    (
        "R4",
        "just_akash/unleased_orders.py",
        "audit_owner",
        "0ef278995075",  # pragma: allowlist secret
    ): (
        1,
        "order observations from one chain read, not independent observers",
    ),
    # if len ( endpoints ) == 1 :
    (
        "R4",
        "just_akash/wallet_pool.py",
        "_default_credit_reader",
        "fe211630d77e",  # pragma: allowlist secret
    ): (
        1,
        "an explicit AKASH_REST_URL is honoured alone by design (chain.rest_urls)",
    ),
}
MIN_BASELINE_HITS = 50


def test_the_lifecycle_population_has_not_gone_blind() -> None:
    candidates = scan_candidates()
    assert len(candidates) >= MIN_SCAN_CANDIDATES, "the scan roots moved; nothing is being read"
    files = lifecycle_files()
    workflows = [p for p in files if p.suffix in (".yml", ".yaml")]
    python = [p for p in files if p.suffix == ".py"]
    assert len(files) >= MIN_LIFECYCLE_FILES, [p.name for p in files]
    assert len(workflows) >= MIN_LIFECYCLE_WORKFLOWS, [p.name for p in workflows]
    assert len(python) >= MIN_LIFECYCLE_PYTHON, [p.name for p in python]


def test_no_lifecycle_check_goes_green_on_an_unmeasured_result() -> None:
    _files, hits = survey()
    assert len(hits) >= MIN_BASELINE_HITS, (
        "the rules stopped matching anything; the scanner is blind"
    )
    new, stale = unexplained(hits, BASELINE)
    assert not new, (
        "NEW lifecycle site(s) that can go green on an unmeasured result. Log, re-raise or fail "
        "instead; see this file's docstring for each rule:\n  "
        + "\n  ".join(f"{h.rule} {h.where} [{h.scope}]: {h.detail}: {h.text}" for h in new)
        + "\n  Keys: "
        + ", ".join(repr(h.key) for h in new)
    )
    assert not stale, (
        "STALE baseline entries: the offending line was fixed, edited or removed. Delete the "
        "entry, or re-key it after re-reading the site:\n  "
        + "\n  ".join(f"{key} x{count}: {reason}" for key, count, reason in stale)
    )


def test_every_baseline_entry_is_justified() -> None:
    assert len(BASELINE) >= MIN_BASELINE_HITS // 2, len(BASELINE)
    assert all(count >= 1 and len(reason.split()) >= 4 for count, reason in BASELINE.values())


# ── fixtures: each rule goes red on its shape and stays clean on the near miss ─────────

_PY_RED = {
    "R1 except Exception: pass": "try:\n    close()\nexcept Exception:\n    pass\n",
    "R1 bare except: continue": (
        "for e in ENDPOINTS:\n    try:\n        read(e)\n    except:\n        continue\n"
    ),
    "R1 aliased Exception": (
        "from builtins import Exception as E\ntry:\n    close()\nexcept E:\n    pass\n"
    ),
    "R1 tuple containing Exception": "try:\n    close()\nexcept (OSError, Exception):\n    pass\n",
    "R1 unresolvable handler fails closed": (
        "try:\n    close()\nexcept errors_from_somewhere():\n    pass\n"
    ),
    "R1 bound name unused": "try:\n    close()\nexcept Exception as exc:\n    result = None\n",
    "R1 exit(0) in a handler": (
        "import sys\ntry:\n    close()\nexcept Exception:\n    sys.exit(0)\n"
    ),
    "R1 imported constant tuple fails closed": (
        "from errors import RETRYABLE\ntry:\n    close()\nexcept RETRYABLE:\n    pass\n"
    ),
    "R3 EXIT_UNMEASURED = 0": "EXIT_OK = 0\nEXIT_UNMEASURED = 0\n",
    "R3 dict maps UNVERIFIED to 0": (
        "CLOSED, UNVERIFIED = 'CLOSED', 'UNVERIFIED'\nEXIT = {CLOSED: 0, UNVERIFIED: 0}\n"
    ),
    "R3 branch returns 0": (
        "def main():\n    if verdict == UNKNOWN:\n        return 0\n    return 1\n"
    ),
    "R3 aliased exit(0)": (
        "from sys import exit as bye\ndef main():\n    if verdict == UNMEASURED:\n        bye(0)\n"
    ),
    "R4 accepts one observer": (
        "def consensus(endpoints):\n    seen = []\n    for e in endpoints:\n"
        "        seen.append(e)\n        if len(seen) == 2:\n            break\n"
        "    if len(seen) < 1:\n        return None\n    return seen[0]\n"
    ),
    "R4 default quorum of one": (
        "def gather(endpoints, need=1):\n    found = []\n    for e in endpoints:\n"
        "        found.append(e)\n        if len(found) == need:\n            break\n"
        "    return found\n"
    ),
    "R4 quorum parameter defaults to one": (
        "def quorum_value(readings, quorum: int = 1):\n"
        "    for value in readings:\n"
        "        if readings.count(value) >= quorum:\n"
        "            return value\n"
    ),
    "R4 caller passes need=1": (
        "observations = independent_observations(ENDPOINTS, get, read, need=1)\n"
    ),
    "R4 observers never counted": (
        "def latest():\n"
        "    observations = _independence.independent_observations(ENDPOINTS, get, read)\n"
        "    return observations[0].data\n"
    ),
    "R4 threshold below two": (
        "def verify(sources):\n    if len(sources) >= 1:\n        return True\n    return False\n"
    ),
}
_PY_CONTROL = {
    "R1 except Exception: log": (
        "import logging as lg\ntry:\n    close()\nexcept Exception:\n    lg.warning('x')\n"
    ),
    "R1 narrow except": (
        "import urllib.error\ntry:\n    close()\nexcept (ValueError, urllib.error.URLError):\n"
        "    pass\n"
    ),
    "R1 re-raise": "try:\n    close()\nexcept Exception:\n    raise\n",
    "R1 carries the exception": (
        "try:\n    close()\nexcept Exception as exc:\n    failures.append(exc)\n"
    ),
    "R1 local exception class": (
        "class Unmeasured(Exception):\n    pass\ntry:\n    close()\nexcept Unmeasured:\n    pass\n"
    ),
    "R1 exit(2) in a handler": (
        "import sys\ntry:\n    close()\nexcept Exception:\n    sys.exit(2)\n"
    ),
    "R1 lower-case stdlib exception class": (
        "import socket\ntry:\n    close()\nexcept socket.gaierror:\n    pass\n"
    ),
    "R1 imported exception class": (
        "from urllib.error import HTTPError\ntry:\n    close()\nexcept HTTPError:\n    pass\n"
    ),
    "R3 EXIT_UNMEASURED = 2": "EXIT_OK = 0\nEXIT_UNMEASURED = 2\n",
    "R3 dict maps UNVERIFIED to 2": (
        "CLOSED, UNVERIFIED = 'CLOSED', 'UNVERIFIED'\nEXIT = {CLOSED: 0, UNVERIFIED: 2}\n"
    ),
    "R3 branch returns 2": (
        "def main():\n    if verdict == UNKNOWN:\n        return 2\n    return 0\n"
    ),
    "R4 quorum parameter defaults to two": (
        "def quorum_value(readings, quorum: int = 2):\n"
        "    for value in readings:\n"
        "        if readings.count(value) >= quorum:\n"
        "            return value\n"
    ),
    "R4 default quorum of two": (
        "def gather(endpoints, need=2):\n    found = []\n    for e in endpoints:\n"
        "        found.append(e)\n        if len(found) == need:\n            break\n"
        "    return found\n"
    ),
    "R4 observers counted": (
        "def latest():\n"
        "    observations = _independence.independent_observations(ENDPOINTS, get, read)\n"
        "    if len(observations) < 2:\n        raise Unmeasured('x')\n"
        "    return observations[0].data\n"
    ),
    "R4 two agreeing observers": (
        "def consensus(endpoints):\n    seen = []\n    for e in endpoints:\n"
        "        seen.append(e)\n        if len(seen) == 2:\n            break\n"
        "    if len(seen) < 2:\n        return None\n    return seen[0]\n"
    ),
}


def _python_rules(source: str) -> set[str]:
    return {hit.rule for hit in python_hits(source, "fixture.py")}


@pytest.mark.parametrize("label", sorted(_PY_RED))
def test_a_python_fixture_goes_red(label: str) -> None:
    assert label.split()[0] in _python_rules(_PY_RED[label]), label


@pytest.mark.parametrize("label", sorted(_PY_CONTROL))
def test_a_python_near_miss_stays_clean(label: str) -> None:
    assert _python_rules(_PY_CONTROL[label]) == set(), label


def _workflow(step: str, job_extra: str = "") -> str:
    return (
        textwrap.dedent(
            """\
        on: push
        jobs:
          j:
            runs-on: ubuntu-latest
        """
        )
        + job_extra
        + "    steps:\n"
        + textwrap.indent(textwrap.dedent(step), "      ")
    )


_WF_RED = {
    "R2 continue-on-error on a verdict step": (
        "- name: Verify the pool lease actually closed\n  continue-on-error: true\n  run: echo x\n"
    ),
    "R2 continue-on-error expression fails closed": (
        "- name: Assert this run left no pool behind\n  continue-on-error: ${{ inputs.soft }}\n"
        "  run: echo x\n"
    ),
    "R2 || true on a verdict step": (
        "- name: Close deployment\n  run: |\n    just-akash destroy --dseq 1 || true\n"
    ),
    "R2 step runs a lifecycle script": (
        "- name: Check\n  run: |\n    python3 -m just_akash.verify_closed --dseq 1 || :\n"
    ),
    "R2 if-no-files-found: ignore on a receipt upload": (
        "- name: Preserve unresolved deployment receipt\n  uses: actions/upload-artifact@v4\n"
        "  with:\n    path: receipt.json\n    if-no-files-found: ignore\n"
    ),
    "R3 case arm UNMEASURED exits 0": (
        '- name: Measure\n  run: |\n    rc=0\n    python3 x.py || rc=$?\n    case "$rc" in\n'
        '      0) ;;\n      2) echo "::warning::UNMEASURED" ;;\n    esac\n'
    ),
    "R3 lower-case unmeasured output exits 0": (
        "- name: Deregister\n"
        "  run: |\n"
        '    if [ "$RC" -ne 0 ]; then\n'
        '      echo "closed=unmeasured" >> "$GITHUB_OUTPUT"\n'
        "      exit 0\n"
        "    fi\n"
    ),
    "R3 then-block UNKNOWN exits 0": (
        '- name: Measure\n  run: |\n    if [ "$rc" -eq 2 ]; then\n'
        '      echo "closure is UNKNOWN"\n      exit 0\n    fi\n'
    ),
    "R1 embedded python swallows": (
        "- name: Probe\n  run: |\n    python3 - <<'PY'\n    try:\n        read()\n"
        "    except Exception:\n        pass\n    PY\n"
    ),
}
_WF_CONTROL = {
    "R2 continue-on-error on a diagnostics step": (
        "- name: Upload diagnostics\n  continue-on-error: true\n  run: echo x\n"
    ),
    "R2 continue-on-error false": (
        "- name: Verify the pool lease actually closed\n  continue-on-error: false\n"
        "  run: echo x\n"
    ),
    "R2 || true in a comment": (
        "- name: Close deployment\n  run: |\n    # never write || true here\n"
        "    just-akash destroy --dseq 1\n"
    ),
    "R2 if-no-files-found: error": (
        "- name: Preserve unresolved deployment receipt\n  uses: actions/upload-artifact@v4\n"
        "  with:\n    path: receipt.json\n    if-no-files-found: error\n"
    ),
    "R3 case arm UNMEASURED propagates": (
        '- name: Measure\n  run: |\n    rc=0\n    python3 x.py || rc=$?\n    case "$rc" in\n'
        '      0) ;;\n      2) echo "::error::UNMEASURED" ;;\n    esac\n    exit "$rc"\n'
    ),
    "R3 then-block UNKNOWN exits 2": (
        '- name: Measure\n  run: |\n    if [ "$rc" -eq 2 ]; then\n'
        '      echo "closure is UNKNOWN"\n      exit 2\n    fi\n'
    ),
    "R3 UNKNOWN only in a comment": (
        '- name: Measure\n  run: |\n    # UNKNOWN is not zero\n    if [ -z "$x" ]; then\n'
        "      exit 0\n    fi\n"
    ),
    "R1 embedded python logs": (
        "- name: Probe\n  run: |\n    python3 - <<'PY'\n    try:\n        read()\n"
        "    except Exception as e:\n        print(e)\n    PY\n"
    ),
}


def _workflow_rules(step: str, job_extra: str = "") -> set[str]:
    lifecycle = {"just_akash.verify_closed"}
    return {
        hit.rule
        for hit in workflow_text_hits(_workflow(step, job_extra), "fixture.yml", lifecycle)
    }


@pytest.mark.parametrize("label", sorted(_WF_RED))
def test_a_workflow_fixture_goes_red(label: str) -> None:
    assert label.split()[0] in _workflow_rules(_WF_RED[label]), label


@pytest.mark.parametrize("label", sorted(_WF_CONTROL))
def test_a_workflow_near_miss_stays_clean(label: str) -> None:
    assert _workflow_rules(_WF_CONTROL[label]) == set(), label


def test_job_level_continue_on_error_over_a_verdict_step_goes_red() -> None:
    step = "- name: Verify the pool lease actually closed\n  run: echo x\n"
    assert _workflow_rules(step, "    continue-on-error: true\n") == {"R2"}
    assert (
        _workflow_rules("- name: Build\n  run: echo x\n", "    continue-on-error: true\n") == set()
    )


def test_shell_script_rules() -> None:
    red = '#!/bin/bash\njust-akash destroy --dseq "$D" || true\n'
    control = "#!/bin/bash\nsed -e 's/^/  /' err.txt >&2 || true\n"
    assert {hit.rule for hit in shell_hits(red, "fixture.sh")} == {"R2"}
    assert shell_hits(control, "fixture.sh") == []


def test_a_new_site_in_a_real_lifecycle_file_is_seen() -> None:
    """The rules applied to a real lifecycle file: one appended swallow is one new hit."""
    real = REPO_ROOT / "just_akash/_lease_verification.py"
    text = real.read_text(encoding="utf-8")
    before = python_hits(text, "just_akash/_lease_verification.py")
    after = python_hits(
        text + "\ntry:\n    consensus()\nexcept Exception:\n    pass\n",
        "just_akash/_lease_verification.py",
    )
    added = set(after) - set(before)
    assert len(after) == len(before) + 1
    assert len(added) == 1, added
    assert next(iter(added)).rule == "R1", added


# ── content keys: stable under insertion, sensitive to the offending line, never collapsed ─


def _tree(tmp_path: Path, workflow: str, module: str) -> Path:
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / "just_akash").mkdir()
    (tmp_path / ".github/workflows/teardown.yml").write_text(workflow)
    (tmp_path / "just_akash/closer.py").write_text(module)
    return tmp_path


_KEYED_WORKFLOW = (
    "on: push\n"
    "jobs:\n"
    "  teardown:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - name: Close the lease\n"
    "        run: |\n"
    "          just-akash destroy --dseq 1 || true\n"
)
_KEYED_MODULE = (
    "def close(dseq):\n    try:\n        destroy(dseq)\n    except Exception:\n        pass\n"
)


def test_inserting_lines_above_an_entry_changes_no_key(tmp_path: Path) -> None:
    before = survey(_tree(tmp_path / "a", _KEYED_WORKFLOW, _KEYED_MODULE))[1]
    shifted_workflow = _KEYED_WORKFLOW.replace("jobs:\n", "# one\n# two\n# three\njobs:\n")
    shifted_module = "# one\n# two\n\n\n" + _KEYED_MODULE
    after = survey(_tree(tmp_path / "b", shifted_workflow, shifted_module))[1]
    assert len(before) == 2, before
    assert [h.where for h in before] != [h.where for h in after], "the fixture must move lines"
    assert Counter(h.key for h in before) == Counter(h.key for h in after)
    baseline = {key: (1, "a baselined site") for key in (h.key for h in before)}
    assert unexplained(after, baseline) == ([], [])


def test_editing_the_offending_line_is_a_new_hit_and_a_stale_entry(tmp_path: Path) -> None:
    before = survey(_tree(tmp_path / "a", _KEYED_WORKFLOW, _KEYED_MODULE))[1]
    edited = _KEYED_WORKFLOW.replace("destroy --dseq 1 || true", "destroy --dseq 2 || true")
    after = survey(_tree(tmp_path / "b", edited, _KEYED_MODULE))[1]
    baseline = {key: (1, "a baselined site") for key in (h.key for h in before)}
    new, stale = unexplained(after, baseline)
    assert [h.rule for h in new] == ["R2"], new
    assert len(stale) == 1 and stale[0][0][0] == "R2", stale


def test_a_python_comment_edit_on_the_offending_line_changes_no_key(tmp_path: Path) -> None:
    before = survey(_tree(tmp_path / "a", _KEYED_WORKFLOW, _KEYED_MODULE))[1]
    commented = _KEYED_MODULE.replace("except Exception:", "except Exception:  # noqa: BLE001")
    after = survey(_tree(tmp_path / "b", _KEYED_WORKFLOW, commented))[1]
    assert len(after) == 2, after
    assert Counter(h.key for h in before) == Counter(h.key for h in after)


def test_identical_lines_in_one_step_are_counted_not_collapsed(tmp_path: Path) -> None:
    doubled = _KEYED_WORKFLOW + "          just-akash destroy --dseq 1 || true\n"
    hits = [h for h in survey(_tree(tmp_path, doubled, _KEYED_MODULE))[1] if h.rule == "R2"]
    assert len(hits) == 2, hits
    assert len({h.key for h in hits}) == 1
    one = {hits[0].key: (1, "only one occurrence baselined")}
    new, stale = unexplained(hits, one)
    assert len(new) == 1 and stale == [], (new, stale)
    assert unexplained(hits, {hits[0].key: (2, "both occurrences baselined")}) == ([], [])


def test_changing_what_a_handler_swallows_is_a_new_hit_and_a_stale_entry(tmp_path: Path) -> None:
    before = survey(_tree(tmp_path / "a", _KEYED_WORKFLOW, _KEYED_MODULE))[1]
    edited = _KEYED_MODULE.replace("        pass\n", "        return None\n")
    after = survey(_tree(tmp_path / "b", _KEYED_WORKFLOW, edited))[1]
    baseline = {key: (1, "a baselined site") for key in (h.key for h in before)}
    new, stale = unexplained(after, baseline)
    assert [h.rule for h in new] == ["R1"], new
    assert len(stale) == 1 and stale[0][0][0] == "R1", stale
