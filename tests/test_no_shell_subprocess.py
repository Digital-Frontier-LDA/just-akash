"""No command in just_akash runs through a shell (#371).

The e2e `run()` helpers in test_lifecycle and test_secrets_e2e executed f-string commands with
`shell=True`. The values they interpolate (DSEQs, provider-returned data, temp paths) come from
external answers, in CI jobs that hold wallet API credentials. #348 had already converted 18 e2e
subprocess sites to argv; these two helpers remained.

Two legs:
  structural   every subprocess call under just_akash/ either omits `shell=` or passes the
               literal False, and os.system/os.popen are never called. Judged on parsed Call
               nodes. The baseline of allowed exceptions is EMPTY and shrink-only.
  behavioural  a DSEQ-like value `1; touch <tmp>/pwned` passed through each real helper
               arrives as ONE literal argument and runs nothing.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from just_akash import test_lifecycle, test_secrets_e2e

PACKAGE = Path(__file__).resolve().parents[1] / "just_akash"
SUBPROCESS_CALLS = {
    "run",
    "Popen",
    "call",
    "check_call",
    "check_output",
    "getoutput",
    "getstatusoutput",
}
SHELL_FUNCTIONS = {("os", "system"), ("os", "popen")}
# file:line entries that must keep a shell, each with a written justification. Shrink-only.
ALLOWED_SHELL: dict[str, str] = {}


def _resolver(tree: ast.AST):
    """Map every spelling in this module to (module, function).

    Import aliases are resolved wherever they appear, including inside functions:
    `import subprocess as sp`, `from subprocess import run as r` and `from os import system`
    all count, so an alias cannot take a shell call out of view.
    """
    modules = {"subprocess": "subprocess", "os": "os"}
    names: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("subprocess", "os"):
                    modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in ("subprocess", "os"):
            for alias in node.names:
                names[alias.asname or alias.name] = (node.module, alias.name)

    def resolve(expr: ast.AST) -> tuple[str, str] | None:
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            module = modules.get(expr.value.id)
            return (module, expr.attr) if module else None
        if isinstance(expr, ast.Name):
            return names.get(expr.id)
        return None

    return resolve


def _shell_sites() -> tuple[list[str], int]:
    violations, subprocess_calls = [], 0
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        resolve = _resolver(tree)
        called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        for node in ast.walk(tree):
            where = f"{path.relative_to(PACKAGE.parent)}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, (ast.Attribute, ast.Name)) and id(node) not in called:
                target = resolve(node)
                if target in SHELL_FUNCTIONS or (
                    target and target[0] == "subprocess" and target[1] in SUBPROCESS_CALLS
                ):
                    # Fail closed on references that are not direct calls (`r = subprocess.run`,
                    # callbacks, partials): the keyword cannot be checked through them.
                    violations.append(
                        f"{where} references {target[0]}.{target[1]} without calling it"
                    )
                continue
            if not isinstance(node, ast.Call):
                continue
            target = resolve(node.func)
            if target in SHELL_FUNCTIONS:
                violations.append(f"{where} calls {target[0]}.{target[1]} (always a shell)")
                continue
            if not target or target[0] != "subprocess" or target[1] not in SUBPROCESS_CALLS:
                continue
            subprocess_calls += 1
            if target[1] in {"getoutput", "getstatusoutput"}:
                violations.append(f"{where} calls subprocess.{target[1]} (always a shell)")
            for keyword in node.keywords:
                if keyword.arg == "shell" and not (
                    isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                ):
                    violations.append(f"{where} passes shell={ast.unparse(keyword.value)}")
    return violations, subprocess_calls


def test_the_ratchet_sees_aliased_spellings(tmp_path, monkeypatch) -> None:
    """Every spelling the review named must be caught, and a clean module must stay clean."""
    import tests.test_no_shell_subprocess as module

    package = tmp_path / "just_akash"
    package.mkdir()
    (package / "clean.py").write_text(
        "import subprocess\nsubprocess.run(['true'], shell=False)\n" * 20, encoding="utf-8"
    )
    spellings = {
        "from_run.py": "from subprocess import run\nrun('true', shell=True)\n",
        "from_run_as.py": "from subprocess import run as r\nr('true', shell=True)\n",
        "module_alias.py": "import subprocess as sp\nsp.run('true', shell=True)\n",
        "from_os_system.py": "from os import system\nsystem('true')\n",
        "os_alias.py": "import os as o\no.popen('true')\n",
        "value_alias.py": (
            "import subprocess\n"
            "def f():\n    r = subprocess.run\n    return r('true', shell=True)\n"
        ),
        "local_import.py": (
            "def f():\n    from subprocess import Popen as P\n    return P('true', shell=True)\n"
        ),
    }
    for name, body in spellings.items():
        (package / name).write_text(body, encoding="utf-8")
    monkeypatch.setattr(module, "PACKAGE", package)

    violations, _calls = module._shell_sites()
    flagged = {v.split(":", 1)[0].split("/")[-1] for v in violations}
    assert flagged == set(spellings), (flagged, violations)


def test_no_subprocess_call_under_just_akash_runs_through_a_shell() -> None:
    violations, subprocess_calls = _shell_sites()
    # A floor on the population: a walker that finds no subprocess calls has proven nothing.
    assert subprocess_calls >= 20, (
        f"only {subprocess_calls} subprocess calls found; the walk is blind"
    )
    unexpected = [v for v in violations if v.split(" ", 1)[0] not in ALLOWED_SHELL]
    assert not unexpected, "commands run through a shell:\n  " + "\n  ".join(unexpected)
    stale = [site for site in ALLOWED_SHELL if not any(v.startswith(site) for v in violations)]
    assert not stale, f"allowed shell sites no longer exist; shrink the baseline: {stale}"


@pytest.mark.parametrize(
    "module", [test_lifecycle, test_secrets_e2e], ids=["lifecycle", "secrets"]
)
def test_an_injected_dseq_is_one_literal_argument_and_runs_nothing(module, tmp_path) -> None:
    marker = tmp_path / "pwned"
    payload = f"1; touch {marker}"
    echo = "import sys; print(sys.argv[1])"

    result = module.run([sys.executable, "-c", echo, payload], timeout=30, input_text="")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == payload, "the value must arrive as one literal argument"
    assert not marker.exists(), "the payload ran as a command"


@pytest.mark.parametrize(
    "module", [test_lifecycle, test_secrets_e2e], ids=["lifecycle", "secrets"]
)
def test_a_command_string_is_refused(module, tmp_path) -> None:
    marker = tmp_path / "pwned"
    with pytest.raises(TypeError, match="argv list"):
        command: object = f"echo 1; touch {marker}"
        module.run(command)  # a string, as the old helper took
    assert not marker.exists()
