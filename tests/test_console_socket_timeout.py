"""Console HTTP calls are bounded by a socket timeout (#368).

⛔ THE DEFECT THIS PINS. ``AkashConsoleAPI._request`` opened the Console endpoint with
``urlopen(req)`` and no ``timeout``: a socket that connects and then never sends a byte —
the stalled-endpoint shape — raised nothing at all, so a Console lookup or close could
hang until the GitHub job timeout. The #366 retry budgets bound attempts and backoff,
not wall time; an unbounded attempt makes any budget of attempts unbounded in the wall
clock too.

Two legs, deliberately different instruments:

  structural   every ``urlopen(`` in ``just_akash/`` passes ``timeout=`` — judged on the
               parsed AST and on the ACTION (a urlopen call), not on a string that
               moves; the population is asserted non-empty first, because a finder that
               reports zero sites has never once meant clean in this codebase.

  behavioural  a REAL local socket that accepts the connection and then never answers:
               the client must raise ``TimeoutError`` in bounded wall time. That is the
               transport class of failure — UNKNOWN outcome, exactly like a dropped
               connection — and NOT an ``AkashAPIError``, which would claim the server
               returned an HTTP verdict it never sent.
"""

from __future__ import annotations

import ast
import contextlib
import socket
import threading
import time
from pathlib import Path

import pytest

from just_akash import api

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "just_akash"

# Measured floor, not today's exact count: nine urlopen sites existed when this was
# written. A number well above zero is what proves the finder still finds.
_URLOPEN_POPULATION_FLOOR = 5

# Sentinel for "the timeout argument is not a literal the rule can judge".
_NOT_A_LITERAL = object()


def _call_callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def urlopen_calls(source: str) -> list[ast.Call]:
    tree = ast.parse(source)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_callee_name(node) == "urlopen"
    ]


def _urlopen_import_names(tree: ast.Module) -> set[str]:
    """Names a `from urllib.request import urlopen [as x]` binds in this module."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("urllib"):
            for alias in node.names:
                if alias.name == "urlopen":
                    names.add(alias.asname or "urlopen")
    return names


def urlopen_rule_errors(source: str, where: str) -> list[str]:
    """Concrete violations of 'urlopen is called directly, with a real timeout'.

    Fail-closed by design (review of #369, M2/M3): a name-keyed call finder is
    escaped by ANY alias, so the rule pins the REFERENCE, not the call. Every
    reference to urlopen — an ``ast.Attribute`` with attr ``urlopen`` (that is
    also ``urllib.request.urlopen`` reached through a module alias), or a
    ``Name`` bound by ``from urllib.request import urlopen [as x]`` — must be
    the ``func`` of a Call that carries a usable ``timeout=``. Anything else —
    a function-local alias, ``functools.partial``, a callback argument — is the
    binding itself, flagged where it is created.

    A usable ``timeout=`` is a non-``None`` value that is not a non-positive
    (or boolean) literal. Parameterized names (``timeout=timeout``) pass by
    design: a static rule cannot resolve them, and the sites that use them
    thread a required argument, not a default.
    """
    tree = ast.parse(source)
    imported = _urlopen_import_names(tree)
    called: dict[int, ast.Call] = {
        id(call.func): call for call in ast.walk(tree) if isinstance(call, ast.Call)
    }
    errors: list[str] = []
    for node in ast.walk(tree):
        # isinstance branches, not an `is_reference` boolean: the branches NARROW
        # node to Attribute | Name (both carry .lineno) for the error paths below.
        if isinstance(node, ast.Attribute):
            if node.attr != "urlopen":
                continue
        elif isinstance(node, ast.Name):
            if node.id not in imported:
                continue
        else:
            continue
        call = called.get(id(node))
        if call is None:
            errors.append(f"{where}:{node.lineno}: urlopen aliased or passed around, not called")
            continue
        keyword = next((kw for kw in call.keywords if kw.arg == "timeout"), None)
        if keyword is None:
            errors.append(f"{where}:{call.lineno}: urlopen without timeout=")
            continue
        # ⚠ `-1` parses as UnaryOp(USub, Constant(1)), NOT Constant(-1): the
        # signed literal must be flattened before it can be judged. The operand
        # is narrowed to a real number BEFORE negating, so no type: ignore.
        literal: object = _NOT_A_LITERAL
        value = keyword.value
        if isinstance(value, ast.Constant):
            literal = value.value
        elif (
            isinstance(value, ast.UnaryOp)
            and isinstance(value.op, (ast.UAdd, ast.USub))
            and isinstance(value.operand, ast.Constant)
        ):
            operand = value.operand.value
            if isinstance(operand, (int, float)) and not isinstance(operand, bool):
                literal = -operand if isinstance(value.op, ast.USub) else operand
        if (
            literal is None
            or isinstance(literal, bool)
            or (isinstance(literal, (int, float)) and literal <= 0)
        ):
            errors.append(
                f"{where}:{call.lineno}: timeout={literal!r} is not a bound — None "
                "means no timeout and a non-positive number fails every socket"
            )
    return errors


def package_urlopen_rule_errors() -> list[str]:
    errors: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = str(path.relative_to(REPO))
        errors.extend(urlopen_rule_errors(path.read_text(encoding="utf-8"), rel))
    return errors


def test_every_urlopen_in_the_package_is_called_directly_with_a_real_timeout() -> None:
    sites = [
        (path.relative_to(REPO), node.lineno)
        for path in sorted(PACKAGE.rglob("*.py"))
        for node in urlopen_calls(path.read_text(encoding="utf-8"))
    ]
    assert len(sites) >= _URLOPEN_POPULATION_FLOOR, (
        f"urlopen finder located only {len(sites)} site(s); a broken finder is not a "
        "clean result — widen it before trusting this ratchet"
    )
    assert not package_urlopen_rule_errors(), package_urlopen_rule_errors()


@pytest.mark.parametrize(
    ("source", "why"),
    [
        pytest.param("urllib.request.urlopen(req)", "no timeout at all", id="no-timeout"),
        pytest.param(
            "urllib.request.urlopen(req, timeout=None)", "None is the unbounded case", id="none"
        ),
        pytest.param("urllib.request.urlopen(req, timeout=0)", "zero never fires", id="zero"),
        pytest.param("urllib.request.urlopen(req, timeout=-1)", "negative", id="negative"),
        pytest.param("urllib.request.urlopen(req, timeout=True)", "bool", id="bool"),
        pytest.param(
            "from urllib.request import urlopen as u\nu(req)",
            "from-import alias without timeout",
            id="from-import-no-timeout",
        ),
        pytest.param(
            "def f(request):\n"
            "    fetch = urllib.request.urlopen\n"
            "    return fetch(request, timeout=5)",
            "function-local alias is the binding DEV2's mutation used",
            id="function-local-alias",
        ),
        pytest.param(
            "from urllib.request import urlopen\nhandler = urlopen",
            "urlopen passed around as a callback",
            id="passed-as-value",
        ),
    ],
)
def test_the_ratchet_rejects_unbounded_and_aliased_urlopen(source: str, why: str) -> None:
    assert urlopen_rule_errors(source, "synthetic.py"), f"expected a violation: {why}"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("urllib.request.urlopen(req, timeout=15)", id="literal"),
        pytest.param(
            "import urllib.request as _urlrequest\n_urlrequest.urlopen(req, timeout=15)",
            id="module-alias",
        ),
        pytest.param(
            "from urllib.request import urlopen as u\nu(req, timeout=15)", id="from-import"
        ),
        pytest.param("urllib.request.urlopen(req, timeout=timeout)", id="parameterized"),
        pytest.param(
            "urllib.request.urlopen(req, timeout=CONSOLE_HTTP_TIMEOUT)", id="constant-by-name"
        ),
    ],
)
def test_the_ratchet_accepts_every_bounded_direct_call(source: str) -> None:
    assert not urlopen_rule_errors(source, "synthetic.py")


def test_the_console_urlopen_reads_the_one_module_constant() -> None:
    """'One constant' is about the CALL SITE, not a convention comment: the Console
    urlopen's timeout argument must be the constant's name, so shrinking the constant
    (the behavioural test below) actually reaches the socket."""
    source = (REPO / "just_akash" / "api.py").read_text(encoding="utf-8")
    calls = urlopen_calls(source)
    assert calls, "no urlopen call left in api.py — the Console client moved; re-pin it"
    assert len(calls) == 1, f"expected the single Console choke point, found {len(calls)}"
    timeout_kw = next((kw for kw in calls[0].keywords if kw.arg == "timeout"), None)
    assert timeout_kw is not None, "the Console urlopen lost its timeout= again"
    assert isinstance(timeout_kw.value, ast.Name), (
        "the Console timeout must come from CONSOLE_HTTP_TIMEOUT by name, not a literal "
        f"(got {ast.dump(timeout_kw.value)})"
    )
    assert timeout_kw.value.id == "CONSOLE_HTTP_TIMEOUT"


def test_the_console_timeout_stays_above_the_measured_slow_answer_envelope() -> None:
    """The 180s is a floor pinned to MEASUREMENTS, not a taste (review M6): this
    repo has recorded a committed-then-500 arriving 103s into a request and
    Cloudflare's own 524 at 125s (deploy.py `_report_suspected_orphans`). A
    constant inside that envelope would convert answers we currently receive
    and can classify into unknown-outcome timeouts."""
    assert api.CONSOLE_HTTP_TIMEOUT > 125, (
        f"CONSOLE_HTTP_TIMEOUT={api.CONSOLE_HTTP_TIMEOUT} cuts inside the measured "
        "103s/125s slow-answer envelope; raise it or re-measure"
    )


class _StalledEndpoint:
    """A local TCP endpoint that ACCEPTS and never answers.

    This is the issue's exact shape: connect succeeds, the request is sent, and no
    byte ever comes back. No fake urlopen — the real client, the real socket layer,
    and (after #368) the real timeout have to cooperate to end this.
    """

    def __init__(self) -> None:
        self._listener = socket.socket()
        self._held: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept_and_stall, daemon=True)

    def _accept_and_stall(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        self._held.append(conn)  # hold it open; never write, never close

    def __enter__(self) -> _StalledEndpoint:
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for conn in self._held:
            with contextlib.suppress(OSError):
                conn.close()
        with contextlib.suppress(OSError):
            self._listener.close()


def test_a_stalled_console_endpoint_raises_TimeoutError_not_a_hang(monkeypatch) -> None:
    monkeypatch.setattr(api, "CONSOLE_HTTP_TIMEOUT", 0.5)
    with _StalledEndpoint() as endpoint:
        # ⚠ Key passed POSITIONALLY, never as `api_key="…"`: detect-secrets'
        # KeywordDetector flags a secret-named keyword followed by a string
        # literal, and the repo's Secret Scan compares against a fixed baseline
        # (positionally, like every other test in this suite).
        client = api.AkashConsoleAPI("test-key", base_url=f"http://127.0.0.1:{endpoint.port}")
        started = time.monotonic()
        # ⛔ RUN THE CALL IN A WORKER WITH A JOIN DEADLINE, not inline. Inline, a
        # regression that drops the timeout again would HANG the suite forever —
        # the defect this file pins, reproduced in CI. Bounded here, the same
        # regression is a red assertion in ~5s; leaving the `with` block closes
        # the held socket, which unblocks (and retires) a still-stuck worker.
        outcome: list[BaseException] = []

        def call() -> None:
            try:
                client._request("GET", "/deployments")
            except BaseException as e:  # re-classified by the assertions below
                outcome.append(e)

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        worker.join(5)
        elapsed = time.monotonic() - started
        assert not worker.is_alive(), (
            f"the call is still running after {elapsed:.1f}s — bounded only on paper; "
            "this is the #368 hang reproduced"
        )
    assert elapsed < 5, f"unexpected wall time: {elapsed:.1f}s"
    assert outcome, "worker finished without raising anything"
    raised = outcome[0]
    assert isinstance(raised, TimeoutError), f"expected TimeoutError, got {raised!r}"
    assert not isinstance(raised, api.AkashAPIError), (
        "a stalled socket has NO server verdict; surfacing an HTTP-shaped error would "
        "claim knowledge the endpoint never sent"
    )
