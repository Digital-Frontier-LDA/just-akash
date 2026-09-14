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

from just_akash import api

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "just_akash"

# Measured floor, not today's exact count: nine urlopen sites existed when this was
# written. A number well above zero is what proves the finder still finds.
_URLOPEN_POPULATION_FLOOR = 5


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


def package_urlopen_sites_missing_timeout() -> list[str]:
    """Concrete violations of 'every urlopen in just_akash/ is timeout-bounded'."""
    errors: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in urlopen_calls(path.read_text(encoding="utf-8")):
            if not any(keyword.arg == "timeout" for keyword in node.keywords):
                errors.append(f"{path.relative_to(REPO)}:{node.lineno}: urlopen without timeout=")
    return errors


def test_every_urlopen_in_the_package_passes_a_timeout() -> None:
    sites = [
        (path.relative_to(REPO), node.lineno)
        for path in sorted(PACKAGE.rglob("*.py"))
        for node in urlopen_calls(path.read_text(encoding="utf-8"))
    ]
    assert len(sites) >= _URLOPEN_POPULATION_FLOOR, (
        f"urlopen finder located only {len(sites)} site(s); a broken finder is not a "
        "clean result — widen it before trusting this ratchet"
    )
    assert not package_urlopen_sites_missing_timeout()


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
        client = api.AkashConsoleAPI(
            api_key="test-key", base_url=f"http://127.0.0.1:{endpoint.port}"
        )
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
