"""End-to-end test for the `just-akash verify-closed` and `resolve-owner` CLI
subcommands via real subprocess invocation.

Director's 2026-09-09 review blocker required: 'Tests must exercise actual
CLI, not just a fake script returning chosen JSON.' The shell harness in
`test_runner_teardown_shell_probes.py` covers the workflow shell with a
fake `just-akash` transport; THIS file drives the actual CLI dispatch via
subprocess so the contract cannot silently drift from the production
parser / handler:

  - argparse wiring (default endpoints, --owner, --endpoint, --retries,
    --json).
  - The actual urllib chain fetcher inside the verify-closed handler
    (no test-only stub function injected into cli.py).
  - The HTTPS-scheme gate inside the verifier (a regression to HTTP
    would re-introduce the #952 false-close defect).
  - Exit-code path: closed=true → exit 0; anything else → exit 1.
  - Owner resolution via the wallet pool when --owner is omitted
    (uses a tiny driver script that patches _resolve_deployment_client
    and calls cli.main() — still the actual cli module, not a parallel
    helper).

Two parallel local HTTPS servers with self-signed certs simulate two
independent Akash chain endpoints. The CLI hits them with its real
urllib fetcher; each server returns the canned lease population for its
hostname. The verifier then consults both populations and reports
closure based on the agreement. The driver script disables SSL
verification (test-only) so the self-signed certs are trusted inside
the subprocess; production code never installs that override.
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import socket
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
PYTHON = sys.executable


class _IPv6HTTPServer(HTTPServer):
    """HTTPServer bound to an IPv6 loopback address (`::1`).

    The default HTTPServer uses `socket.AF_INET` and rejects IPv6
    literals. Tests need two distinct loopback hostnames to verify the
    verifier's hostname dedup is applied per-hostname, not per-port,
    so the second simulator binds here.
    """

    address_family = socket.AF_INET6


OWNER = "akash1" + "a" * 38
DSEQ = "1788952936722"

# ⇒ Self-signed cert generated once per test process. The driver disables
# SSL verification, so the cert only needs to parse, not validate against
# any authority. Generating per-process avoids stale-cert drift across
# test runs in long-lived dev shells.
_CERT_DIR = pathlib.Path("/tmp/_ja_test_certs")
_CERTFILE = _CERT_DIR / "cert.pem"
_KEYFILE = _CERT_DIR / "key.pem"


def _ensure_cert() -> None:
    _CERT_DIR.mkdir(parents=True, exist_ok=True)
    if _CERTFILE.exists() and _KEYFILE.exists():
        return
    # ⇒ Generate a fresh self-signed RSA cert via openssl. Avoids the
    # `cryptography` dependency and matches what `python -m http.server`
    # uses internally.
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(_KEYFILE),
            "-out",
            str(_CERTFILE),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )


# ── LOCAL HTTPS CHAIN ENDPOINT SIMULATOR ────────────────────────────────────


def _deployment_doc():
    return {
        "deployment": {"id": {"owner": OWNER, "dseq": DSEQ}, "state": "closed"},
        "escrow_account": {
            "id": {"scope": "deployment", "xid": f"{OWNER}/{DSEQ}"},
            "state": {"owner": OWNER, "state": "closed"},
        },
    }


class _ChainHandler(BaseHTTPRequestHandler):
    deployment_body: bytes
    paths: list[str]

    """Serve a single canned `/akash/market/v1beta5/leases/list` response."""

    response_body: bytes = b"{}"

    def do_GET(self):  # noqa: N802 — http.server protocol
        self.paths.append(self.path)
        # Exercise the actual CLI Request headers: public LCDs reject urllib defaults.
        if self.headers.get("User-Agent") != "just-akash-verify-closed/1.0":
            self.send_error(403, "Named verifier user agent required")
            return
        body = self.deployment_body if "/deployments/info?" in self.path else self.response_body
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args, **_kwargs):
        return  # silence stderr noise


def _make_https_server(
    response: dict, host: str = "127.0.0.1", deployment: dict | None = None
) -> tuple[HTTPServer, str]:
    """Start an HTTPS server on the given loopback host returning the canned response.

    The cert is generated for CN=localhost, but the driver disables SSL
    verification in the subprocess so any hostname is accepted. The host
    parameter exists so two simulators can claim two distinct origins —
    binding both to 127.0.0.1 would collapse them via the verifier's
    hostname dedup and silently weaken every test that expected
    two-source agreement. The pair used here is `127.0.0.1` (IPv4
    loopback) and `::1` (IPv6 loopback) — both universally bindable on
    macOS / Linux without root.
    """
    _ensure_cert()

    handler = type(
        "_H",
        (_ChainHandler,),
        {
            "response_body": json.dumps(response).encode("utf-8"),
            "deployment_body": json.dumps(
                deployment if deployment is not None else _deployment_doc()
            ).encode(),
            "paths": [],
        },
    )
    # ⇒ IPv6 needs AF_INET6; HTTPServer(('::1', 0)) fails on the
    # default (AF_INET) family. Tests use _IPv6HTTPServer for the
    # second simulator so the two endpoints occupy two distinct
    # hostnames (127.0.0.1 and ::1) and the verifier's hostname
    # dedup does NOT collapse them into one source.
    if ":" in host:
        server = _IPv6HTTPServer(("::1", 0), handler)
    else:
        server = HTTPServer((host, 0), handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(_CERTFILE), keyfile=str(_KEYFILE))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    addr = server.server_address
    # ⇒ IPv6 addresses are returned as (host, port, flowinfo, scopeid);
    # IPv4 addresses as (host, port). Unpack accordingly.
    if len(addr) == 4:
        bound_host, port = str(addr[0]), addr[1]
    else:
        bound_host, port = str(addr[0]), addr[1]
    # ⇒ IPv6 addresses need brackets in the URL.
    url = f"https://[{bound_host}]:{port}" if ":" in bound_host else f"https://{bound_host}:{port}"
    return server, url


def _lease_doc(state: str = "closed") -> dict:
    return {
        "leases": [
            {
                "lease": {
                    "id": {
                        "owner": OWNER,
                        "dseq": DSEQ,
                        "gseq": 1,
                        "oseq": 1,
                        "bseq": 0,
                        "provider": "akashprovider1xyz",
                    },
                    "state": state,
                    "price": {"denom": "uakt", "amount": "1000"},
                }
            }
        ],
        "pagination": {"next_key": ""},
    }


# ── DRIVER SCRIPT (test-only; disables SSL verification) ───────────────────
#
# The driver disables SSL verification BEFORE importing cli, so the
# verify-closed handler's `urllib.request.urlopen` uses an unverified
# context for the self-signed certs. This is a test-only path; production
# never installs this override.


def _driver_source(owner_resolve: bool = False) -> str:
    """Return Python source for a driver script.

    When `owner_resolve` is True, the driver patches
    `_resolve_deployment_client` to return a fake client. Otherwise it
    leaves the wallet-pool code path intact (and the parent test must
    supply --owner on the CLI).
    """
    base = (
        "import ssl\n"
        "ssl._create_default_https_context = ssl._create_unverified_context\n"
        "from unittest.mock import patch, MagicMock\n"
        "from just_akash import cli\n"
        f"OWNER = {OWNER!r}\n"
        f"DSEQ = {DSEQ!r}\n"
        "fake_client = MagicMock()\n"
        "fake_client.account_address = lambda: OWNER\n"
        "patcher = patch.object(cli, '_resolve_deployment_client',\n"
        "                       return_value=(fake_client, DSEQ))\n"
        "patcher.start()\n"
        "try:\n"
        "    cli.main()\n"
        "finally:\n"
        "    patcher.stop()\n"
    )
    if owner_resolve:
        return base
    # ⇒ When the parent passes --owner explicitly, no patch is needed.
    return (
        "import ssl\n"
        "ssl._create_default_https_context = ssl._create_unverified_context\n"
        "from just_akash import cli\n"
        "cli.main()\n"
    )


def _run_cli(argv: list[str]) -> subprocess.CompletedProcess:
    """Drive `python -m just_akash.cli <argv>` as a real subprocess.

    Uses a driver script that disables SSL verification so the
    self-signed test certs are trusted. Production code never installs
    that override.
    """
    driver = ROOT / "tests" / "_verify_closed_driver.py"
    driver.write_text(_driver_source())
    try:
        return subprocess.run(
            [PYTHON, str(driver), *argv],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        with contextlib.suppress(FileNotFoundError):
            driver.unlink()


# ── DEFAULT ENDPOINTS, BOTH CHANNELS AGREE TERMINAL ─────────────────────────


def test_cli_subprocess_two_agreeing_terminal_returns_closed_true_and_exit_zero():
    """The happy path: two chain simulators both report closed populations.

    Exercises actual cli.main() dispatch — argparse, the urllib HTTPS
    fetcher inside the handler, the verifier's HTTPS-scheme gate, the
    consensus() comparison, the JSON output, and the exit-code path.
    closed=true on agreement → exit 0.
    """
    doc = _lease_doc("closed")
    server_a, url_a = _make_https_server(doc, host="127.0.0.1")
    server_b, url_b = _make_https_server(doc, host="::1")
    try:
        result = _run_cli(
            [
                "verify-closed",
                "--dseq",
                DSEQ,
                "--owner",
                OWNER,
                "--endpoint",
                url_a,
                "--endpoint",
                url_b,
                "--retries",
                "3",
            ]
        )
    finally:
        server_a.shutdown()
        server_b.shutdown()

    assert result.returncode == 0, (
        f"closed populations on both endpoints must exit 0; got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout.strip())
    assert payload["closed"] is True, payload
    assert url_a in payload["sources"]
    assert url_b in payload["sources"]
    assert "agreeing terminal" in payload["reason"]


# ── DISAGREEMENT BETWEEN CHANNELS → NOT CLOSED, NON-ZERO ───────────────────


def test_cli_subprocess_endpoint_disagreement_exits_nonzero_with_closed_false():
    """The #952 false-close defect pinned at the CLI boundary.

    Endpoint A returns closed; endpoint B returns active. The verifier's
    consensus() returns None. closed=false in stdout AND non-zero exit
    code. A CLI that exits 0 here would re-introduce the false-GREEN
    defect — the destroy_text + Console_closed path this verifier exists
    to refuse.
    """
    server_a, url_a = _make_https_server(_lease_doc("closed"), host="127.0.0.1")
    server_b, url_b = _make_https_server(_lease_doc("active"), host="::1")
    try:
        result = _run_cli(
            [
                "verify-closed",
                "--dseq",
                DSEQ,
                "--owner",
                OWNER,
                "--endpoint",
                url_a,
                "--endpoint",
                url_b,
                "--retries",
                "1",
            ]
        )
    finally:
        server_a.shutdown()
        server_b.shutdown()

    payload = json.loads(result.stdout.strip())
    assert payload["closed"] is False, payload
    assert result.returncode != 0, (
        f"disagreement must exit non-zero; got {result.returncode} "
        f"(this is the typed-closed check pinning closed=False to a "
        f"non-zero exit, so stdout True alone cannot override)."
    )


# ── ACTIVE LEASE ON BOTH CHANNELS → NOT CLOSED ──────────────────────────────


# ── ACTIVE-STATE PROPAGATION RETRY ──────────────────────────────────────────


def test_cli_subprocess_active_first_then_closed_closes_via_retry():
    """Chain-propagation lag: first attempt reads active on both endpoints,
    later attempts read closed on both. With bounded retries + non-zero
    retry-sleep, the verifier must accept the LATER terminal consensus
    rather than the EARLIER active observation.

    This is the case the reviewer flagged 2026-09-09: a verifier that
    returns immediately on an active observation would report closed=false
    on a lease that is in fact mid-close. The fix in `_lease_verification.py`
    consumes the retry budget on active observations, so a later agreeing
    terminal reading wins.
    """
    # ⇒ Two-stage servers: each endpoint returns active on the FIRST
    # call, then closed on subsequent calls. retry_sleep_s is small so
    # the test finishes quickly.
    responses_per_endpoint: dict[str, list[dict]] = {
        "127.0.0.1": [_lease_doc("active"), _lease_doc("closed")],
        "::1": [_lease_doc("active"), _lease_doc("closed")],
    }
    request_counts = {"127.0.0.1": 0, "::1": 0}

    class _StageHandler(BaseHTTPRequestHandler):
        host_marker: str

        def do_GET(self):  # noqa: N802 — http.server protocol
            # ⇒ Read `host_marker` off the instance (set by _stage_server on
            # the per-host subclass), not the base class — otherwise every
            # request hits an empty-string key and the counts never advance.
            if "/deployments/info?" in self.path:
                body = json.dumps(_deployment_doc()).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
                return
            key = self.host_marker
            idx = request_counts[key]
            doc = responses_per_endpoint[key][min(idx, len(responses_per_endpoint[key]) - 1)]
            request_counts[key] = idx + 1
            body = json.dumps(doc).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args, **_kwargs):
            return

    def _stage_server(host: str) -> tuple[HTTPServer, str]:
        _ensure_cert()
        cls = type(f"_H_{host}", (_StageHandler,), {"host_marker": host})
        server = _IPv6HTTPServer(("::1", 0), cls) if ":" in host else HTTPServer((host, 0), cls)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(_CERTFILE), keyfile=str(_KEYFILE))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        addr = server.server_address
        if len(addr) == 4:
            bound, port = str(addr[0]), addr[1]
        else:
            bound, port = str(addr[0]), addr[1]
        url = f"https://[{bound}]:{port}" if ":" in bound else f"https://{bound}:{port}"
        return server, url

    server_a, url_a = _stage_server("127.0.0.1")
    server_b, url_b = _stage_server("::1")
    try:
        result = _run_cli(
            [
                "verify-closed",
                "--dseq",
                DSEQ,
                "--owner",
                OWNER,
                "--endpoint",
                url_a,
                "--endpoint",
                url_b,
                "--retries",
                "5",
                "--retry-sleep",
                "0.1",
            ]
        )
    finally:
        server_a.shutdown()
        server_b.shutdown()

    # ⇒ Both endpoints must have been consulted at least twice — once
    # for the active reading, once for the closed reading. A regression
    # that returned immediately on active would consult each endpoint
    # exactly once.
    assert request_counts["127.0.0.1"] >= 2, (
        f"endpoint 127.0.0.1 was consulted {request_counts['127.0.0.1']} times; "
        f"active-state retry must keep polling past the first active observation"
    )
    assert request_counts["::1"] >= 2, (
        f"endpoint ::1 was consulted {request_counts['::1']} times; "
        f"active-state retry must keep polling past the first active observation"
    )
    assert result.returncode == 0, (
        f"later agreeing terminal populations must close the lease; got rc={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout.strip())
    assert payload["closed"] is True, payload


# ── ACTIVE LEASE ON BOTH CHANNELS → NOT CLOSED ──────────────────────────────


def test_cli_subprocess_active_on_both_exits_nonzero():
    server_a, url_a = _make_https_server(_lease_doc("active"), host="127.0.0.1")
    server_b, url_b = _make_https_server(_lease_doc("active"), host="::1")
    try:
        result = _run_cli(
            [
                "verify-closed",
                "--dseq",
                DSEQ,
                "--owner",
                OWNER,
                "--endpoint",
                url_a,
                "--endpoint",
                url_b,
                "--retries",
                "1",
            ]
        )
    finally:
        server_a.shutdown()
        server_b.shutdown()

    payload = json.loads(result.stdout.strip())
    assert payload["closed"] is False, payload
    assert "active" in payload["reason"].lower()
    assert result.returncode != 0


# ── ONE ENDPOINT UNAVAILABLE → UNVERIFIED ───────────────────────────────────


def test_cli_subprocess_one_endpoint_unreachable_exits_nonzero():
    """An unreadable endpoint must NOT be reported as agreement.

    Start one server, close it before invocation. The verifier must
    return closed=false with sources empty / partial, and the CLI must
    exit non-zero. Destroy-text alone, or any single-side agreement,
    would re-introduce #952.
    """
    doc = _lease_doc("closed")
    server_a, url_a = _make_https_server(doc, host="127.0.0.1")
    server_b, url_b = _make_https_server(doc, host="::1")
    server_b.shutdown()  # ⇒ kill one endpoint before the CLI runs
    try:
        result = _run_cli(
            [
                "verify-closed",
                "--dseq",
                DSEQ,
                "--owner",
                OWNER,
                "--endpoint",
                url_a,
                "--endpoint",
                url_b,
                "--retries",
                "1",
            ]
        )
    finally:
        server_a.shutdown()

    payload = json.loads(result.stdout.strip())
    assert payload["closed"] is False, payload
    assert result.returncode != 0


# ── HOSTNAME DEDUP AT THE CLI BOUNDARY ──────────────────────────────────────


def test_cli_subprocess_hostname_dedup_collapses_same_host_different_port():
    """Pass the SAME hostname twice on different ports — they are ONE source.

    The CLI should consult one origin (the second occurrence is
    skipped). With only one usable source, the verifier cannot reach
    two-endpoint agreement and must return closed=false with a non-zero
    exit. A CLI that naively trusted `--endpoint` as a list of
    independent origins would silently double-count the same chain
    read and report closed=true over a single source.
    """
    doc = _lease_doc("closed")
    server_a, url_a = _make_https_server(doc)
    host, port_a = str(server_a.server_address[0]), server_a.server_address[1]
    try:
        # ⇒ Same hostname as server_a, different port. The CLI's hostname
        # dedup must collapse these into one source; only server_a is
        # actually consulted.
        fake_url_b = f"https://{host}:{port_a + 1}"
        result = _run_cli(
            [
                "verify-closed",
                "--dseq",
                DSEQ,
                "--owner",
                OWNER,
                "--endpoint",
                url_a,
                "--endpoint",
                fake_url_b,
                "--retries",
                "1",
            ]
        )
    finally:
        server_a.shutdown()

    payload = json.loads(result.stdout.strip())
    assert payload["closed"] is False, (
        f"hostname-dedup must collapse same-host endpoints into one source "
        f"and refuse to claim agreement; got {payload!r}"
    )
    assert result.returncode != 0


# ── HTTPS SCHEME GATE AT THE CLI BOUNDARY ───────────────────────────────────


def test_cli_subprocess_http_endpoint_is_refused_by_scheme_gate():
    """An HTTP endpoint must be silently skipped (scheme gate), leaving
    only one usable source. With one source, consensus cannot agree
    across two endpoints → closed=false. A regression that relaxed
    `parsed.scheme != "https"` to allow HTTP would re-introduce the
    MITM-and-claim-closed defect this gate exists to refuse.
    """
    doc = _lease_doc("closed")
    server_a, url_a = _make_https_server(doc, host="127.0.0.1")
    server_b, url_b = _make_https_server(doc, host="::1")
    try:
        # ⇒ Downgrade url_b to HTTP. The verifier's scheme check must
        # skip it; only server_a is consulted, so consensus() cannot
        # agree across two sources.
        http_b = url_b.replace("https://", "http://")
        result = _run_cli(
            [
                "verify-closed",
                "--dseq",
                DSEQ,
                "--owner",
                OWNER,
                "--endpoint",
                url_a,
                "--endpoint",
                http_b,
                "--retries",
                "1",
            ]
        )
    finally:
        server_a.shutdown()
        server_b.shutdown()

    payload = json.loads(result.stdout.strip())
    assert payload["closed"] is False, payload
    assert result.returncode != 0


# ── OWNER RESOLUTION VIA WALLET POOL ────────────────────────────────────────
#
# When --owner is omitted, cli.main() calls _resolve_deployment_client and
# reads client.account_address(). To exercise that path through the actual
# cli.main() (not a parallel helper), the driver script patches the
# resolve helper with a fake client and calls cli.main(). The driver is
# invoked as a subprocess so the patch is scoped and cannot leak.


def test_cli_subprocess_resolves_owner_via_wallet_pool_when_omitted():
    """When --owner is omitted, the CLI must consult the wallet pool and
    use the resolved account — not invent an owner, not require a
    mandatory input.

    The driver patches cli._resolve_deployment_client with a fake client
    whose account_address() returns OWNER. The chain endpoints (real
    HTTPS servers in the parent test) are wired to that owner.
    closed=true proves the owner derived from the wallet pool reached
    the verifier.
    """
    doc = _lease_doc("closed")
    server_a, url_a = _make_https_server(doc, host="127.0.0.1")
    server_b, url_b = _make_https_server(doc, host="::1")
    driver = ROOT / "tests" / "_verify_closed_owner_driver.py"
    driver.write_text(_driver_source(owner_resolve=True))
    try:
        result = subprocess.run(
            [
                PYTHON,
                str(driver),
                "verify-closed",
                "--dseq",
                DSEQ,
                "--endpoint",
                url_a,
                "--endpoint",
                url_b,
                "--retries",
                "2",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        with contextlib.suppress(FileNotFoundError):
            driver.unlink()
        server_a.shutdown()
        server_b.shutdown()

    assert result.returncode == 0, (
        f"wallet-pool-derived owner + agreeing populations must exit 0; "
        f"got {result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout.strip())
    assert payload["closed"] is True, payload


# ── RESOLVE-OWNER SUBCOMMAND EMITS THE DOCUMENTED SHAPE ────────────────────


def test_cli_subprocess_resolve_owner_emits_documented_shape():
    """resolve-owner is the read-only path the close step uses to capture
    the positively-owning account BEFORE destroy. The CLI must emit
    {owner, dseq, source: "wallet_pool"} on stdout and exit 0.
    """
    driver = ROOT / "tests" / "_resolve_owner_driver.py"
    driver.write_text(_driver_source(owner_resolve=True))
    try:
        result = subprocess.run(
            # ⇒ Pass the EXACT workflow argument vector — including
            # --json. A regression that drops --json from the workflow
            # while the parser still accepts it (or vice versa) is the
            # kind of drift this subprocess test is supposed to catch.
            [PYTHON, str(driver), "resolve-owner", "--dseq", DSEQ, "--json"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        with contextlib.suppress(FileNotFoundError):
            driver.unlink()

    assert result.returncode == 0, (
        f"resolve-owner must exit 0 on success; got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    payload = json.loads(result.stdout.strip())
    assert payload == {"owner": OWNER, "dseq": DSEQ, "source": "wallet_pool"}, payload


def test_cli_subprocess_resolve_owner_exits_nonzero_when_wallet_pool_returns_none():
    """resolve-owner exits 1 when no wallet in the pool claims the DSEQ —
    the workflow treats that as a hard failure to capture owner, NOT a
    proceed-anyway state.
    """
    driver = ROOT / "tests" / "_resolve_owner_fail_driver.py"
    driver.write_text(
        "import ssl\n"
        "ssl._create_default_https_context = ssl._create_unverified_context\n"
        "from unittest.mock import patch\n"
        "from just_akash import cli\n"
        "def _raise(_dseq):\n"
        "    raise RuntimeError('no wallet in pool claims that DSEQ')\n"
        "with patch.object(cli, '_resolve_deployment_client', side_effect=_raise):\n"
        "    cli.main()\n"
    )
    try:
        result = subprocess.run(
            [PYTHON, str(driver), "resolve-owner", "--dseq", DSEQ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        with contextlib.suppress(FileNotFoundError):
            driver.unlink()

    assert result.returncode != 0, (
        f"resolve-owner must exit non-zero when no owner is resolvable; "
        f"got {result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "no wallet" in result.stderr.lower()


def test_recorded_closed_leases_do_not_hide_open_deployment_escrow():
    """Sanitized 2026-09-09 wire observations: same closed leases, different escrow."""
    fixtures = json.loads((ROOT / "tests/fixtures/closure_wire.json").read_text())
    assert set(fixtures) == {"open_escrow", "closed"}
    for label, expected in (("open_escrow", False), ("closed", True)):
        sample = fixtures[label]
        owner = sample["deployment"]["deployment"]["id"]["owner"]
        server_a, url_a = _make_https_server(sample["leases"], "127.0.0.1", sample["deployment"])
        server_b, url_b = _make_https_server(sample["leases"], "::1", sample["deployment"])
        try:
            result = _run_cli(
                [
                    "verify-closed",
                    "--dseq",
                    "7",
                    "--owner",
                    owner,
                    "--endpoint",
                    url_a,
                    "--endpoint",
                    url_b,
                    "--retries",
                    "1",
                ]
            )
        finally:
            server_a.shutdown()
            server_b.shutdown()
        payload = json.loads(result.stdout)
        assert payload["closed"] is expected, payload
        assert result.returncode == (0 if expected else 1)
        for server in (server_a, server_b):
            paths = server.RequestHandlerClass.paths
            assert any("/leases/list?" in path for path in paths)
            if expected:
                assert any("/deployments/info?" in path for path in paths)
        assert any("/deployments/info?" in path for path in server_a.RequestHandlerClass.paths)
