# Phase 7: Lease-Shell Exec — Research

> ⚠️ **SUPERSEDED — trust the shipped code, not this document.**
> This research describes an earlier *direct-provider* design — `wss://{provider}/…?cmd=…`
> with self-signed certs accepted via `ssl.CERT_NONE`. The shipped implementation
> **reversed** it: exec/inject route through the Console **provider-proxy** with **full
> TLS verification**, and the command rides the proxy URL's `cmd2=` (or a stdin frame),
> not a `?cmd=` query against the provider. For the real design see
> `just_akash/transport/lease_shell.py`; this file is kept for history only (issue #38).

**Researched:** 2026-04-19
**Domain:** WebSocket binary framing, Akash JWT authentication, Python async-to-sync bridge
**Confidence:** HIGH (PROTOCOL.md is authoritative; websockets API verified locally; JWT endpoint confirmed from Console source)

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| LSHL-02 | Lease-shell WebSocket connection authenticates using the existing AKASH_API_KEY | Console API has a `/v1/create-jwt-token` endpoint that accepts the existing `x-api-key` and returns a short-lived JWT |
| LSHL-03 | Token expiry during long sessions triggers automatic re-authentication without dropping the user session | JWT TTL is only 30 seconds by default (server default); strategy: pre-emptive refresh via background thread before expiry, then reconnect with new token |
| EXEC-01 | User can execute a remote command via `just exec` / `just-akash exec` using lease-shell | `LeaseShellTransport.exec()` connects via `wss://{provider}/lease/{dseq}/1/1/shell?cmd=...&service=...`, receives frames 100/101/102 |
| EXEC-02 | Remote command exit code is propagated as the CLI exit code | Frame type 102 payload: 4-byte little-endian int32 (validate live — may also be JSON) |
| EXEC-03 | Remote stdout and stderr are streamed to local output | Frame types 100 (stdout) and 101 (stderr) — write directly to `sys.stdout.buffer` and `sys.stderr.buffer`, flush immediately |

</phase_requirements>

---

## Summary

Phase 7 implements `LeaseShellTransport.exec()` — the first live method in the previously stub-only class. The implementation bridges three well-understood pieces: the Akash binary frame WebSocket protocol (documented in `docs/PROTOCOL.md` from real provider source), the `websockets>=16.0` synchronous threading client, and the Console API's JWT token endpoint.

The most significant architectural finding is that **the Console API exposes a `/v1/create-jwt-token` endpoint** (confirmed from `akash-network/console` source) that accepts the existing `AKASH_API_KEY` (`x-api-key` header) and returns a signed JWT token in a `{ data: { token: "..." } }` response. This means `just-akash` does NOT need to implement secp256k1 signing or manage wallet keys — it delegates token issuance entirely to the managed wallet infrastructure already used by the project.

The second critical finding is the **JWT default TTL of 30 seconds** (discovered in `provider-jwt-token.service.ts`). LSHL-03 mandates that long-running commands survive token expiry. The recommended strategy is a background thread that re-issues a token before expiry and re-opens a new WebSocket, then replays the original command if the server has already closed the first connection.

**Primary recommendation:** Use `websockets.sync.client.connect()` (threading, not asyncio) to keep `LeaseShellTransport.exec()` synchronous; poll frames in a `while True` loop; implement token refresh by requesting a new JWT from the Console API before each connection and pre-empting expiry with a background timer refresh.

---

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| websockets | >=16.0 | WebSocket client (sync + async) | Already in pyproject.toml; provides `websockets.sync.client` for synchronous use |
| urllib.request | stdlib | Console API HTTP calls | Already used in `api.py`; zero new dependencies |
| ssl | stdlib | TLS context for provider connections | Provider certs are self-signed; `ssl.CERT_NONE` required for Phase 7 |
| struct | stdlib | Frame encoding (resize) | Used for terminal resize frame packing |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| threading | stdlib | Background token refresh timer | Keep long sessions alive without asyncio complexity |
| sys | stdlib | `sys.stdout.buffer` / `sys.stderr.buffer` | Binary output streaming without encoding issues |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `websockets.sync.client` | `websockets.asyncio.client` + `asyncio.run()` | asyncio requires changing `exec()` return type or nesting loops; sync is cleaner for synchronous Transport ABC |
| Console API `/v1/create-jwt-token` | Direct secp256k1 wallet signing | Would require `eth-keys` or `cryptography` dependency and wallet key management; the Console API handles this with the existing API key |

**Installation:** All dependencies are already in `pyproject.toml`. No new packages required.

---

## Architecture Patterns

### Recommended Project Structure

The implementation lives entirely in `just_akash/transport/lease_shell.py`. No new files are needed for Phase 7.

```
just_akash/
├── transport/
│   ├── base.py           # TransportConfig, Transport ABC (unchanged)
│   ├── __init__.py       # make_transport factory (unchanged)
│   ├── ssh.py            # SSHTransport (unchanged)
│   └── lease_shell.py    # IMPLEMENT: prepare(), exec(), validate()
└── api.py                # AkashConsoleAPI._request() (unchanged — reuse for JWT fetch)
```

### Pattern 1: Console API JWT Token Fetch

**What:** POST to `/v1/create-jwt-token` using the existing `AKASH_API_KEY` to obtain a short-lived JWT bearer token.
**When to use:** Before every WebSocket connection (and on refresh).

```python
# Source: confirmed from akash-network/console apps/api/src/provider/routes/jwt-token/jwt-token.router.ts
# and apps/api/src/provider/http-schemas/jwt-token.schema.ts

def _fetch_jwt(self, dseq: str, provider_address: str) -> str:
    """Fetch a short-lived JWT from Console API. TTL defaults to 30s server-side."""
    import urllib.request, json
    url = f"{self._config.console_url}/v1/create-jwt-token"
    payload = json.dumps({
        "data": {
            "ttl": 120,  # request longer TTL (server may cap at 30s; validate live)
            "leases": {
                dseq: {"access": "full"}
            }
        }
    }).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "x-api-key": self._config.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())
    # Response shape: { "data": { "token": "<JWT>" } }
    return body["data"]["token"]
```

**Confidence:** MEDIUM — endpoint path and response schema confirmed from source; exact `leases` field format and TTL cap need live validation.

### Pattern 2: Provider Address Extraction

**What:** Extract `hostUri` from deployment data to construct the WebSocket URL.
**When to use:** Inside `prepare()`.

```python
# Source: docs/PROTOCOL.md — Provider Address Discovery section
def _extract_provider_url(self) -> tuple[str, str]:
    """Returns (provider_hostUri, service_name) from deployment."""
    leases = self._config.deployment.get("leases", [])
    if not leases:
        raise RuntimeError(f"No leases found for deployment {self._config.dseq}")
    lease = leases[0]
    provider = lease.get("provider", {})
    host_uri = provider.get("hostUri") or provider.get("host_uri")
    if not host_uri:
        raise RuntimeError("Provider hostUri not found in deployment lease data")
    # Convert https:// → wss://, http:// → ws://
    ws_base = host_uri.replace("https://", "wss://").replace("http://", "ws://")
    service = self._config.service_name or _infer_service(self._config.deployment)
    return ws_base, service
```

**Confidence:** HIGH — path `deployment["leases"][0]["provider"]["hostUri"]` confirmed by PROTOCOL.md.

### Pattern 3: Synchronous WebSocket Exec Loop

**What:** Connect to provider WebSocket, dispatch binary frames by type code, collect exit code.
**When to use:** Inside `exec()`.

```python
# Source: websockets.sync.client — verified locally against websockets 15.0.1/16.0
# Source: docs/PROTOCOL.md — Message Schema and Python Implementation Notes sections
import ssl
import sys
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError

def exec(self, command: str) -> int:
    ws_base, service = self._extract_provider_url()
    dseq = self._config.dseq

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE  # providers use self-signed certs

    import urllib.parse
    params = urllib.parse.urlencode({
        "cmd": command,
        "service": service,
        "tty": "false",
        "stdin": "false",
    })
    url = f"{ws_base}/lease/{dseq}/1/1/shell?{params}"

    jwt = self._fetch_jwt(dseq, "")
    headers = {"Authorization": f"Bearer {jwt}"}

    exit_code = 0
    with connect(url, additional_headers=headers, ssl=ssl_ctx, open_timeout=30) as ws:
        while True:
            try:
                frame = ws.recv(timeout=300)  # 5-minute command timeout
            except (ConnectionClosedOK, ConnectionClosedError):
                break
            if isinstance(frame, bytes) and len(frame) >= 1:
                code = frame[0]
                payload = frame[1:]
                if code == 100:  # stdout
                    sys.stdout.buffer.write(payload)
                    sys.stdout.buffer.flush()
                elif code == 101:  # stderr
                    sys.stderr.buffer.write(payload)
                    sys.stderr.buffer.flush()
                elif code == 102:  # result (exit code)
                    if len(payload) >= 4:
                        exit_code = int.from_bytes(payload[:4], "little")
                    break
                elif code == 103:  # failure
                    msg = payload.decode("utf-8", errors="replace")
                    raise RuntimeError(f"Provider error: {msg}")
    return exit_code
```

**Confidence:** HIGH for frame dispatch logic (matches PROTOCOL.md exactly). MEDIUM for result frame payload format (PROTOCOL.md notes it "may be JSON" — needs live validation).

### Pattern 4: Token Refresh Strategy (LSHL-03)

**What:** Pre-emptive JWT refresh before expiry during long commands.
**When to use:** Any command that may run longer than the JWT TTL.

The JWT TTL is 30 seconds by default. Strategy options:

1. **Re-connect on expiry:** If the server closes the WebSocket due to token expiry (connection closed with a specific code), catch `ConnectionClosedError`, re-fetch a JWT, and re-open the connection. The provider re-runs the command from scratch — only viable for idempotent commands.

2. **Pre-emptive reconnect (preferred):** Use a background `threading.Timer` to trigger a token refresh at `TTL - 5` seconds. When fired, fetch a new token and keep it ready. If the WebSocket is still open, the current frame loop continues — the token is only used on the next connection. This is only meaningful if the connection persists (which requires the provider to accept token refresh in-band or via reconnect).

3. **Single-use token with long TTL:** Request `ttl=3600` (1 hour) from the Console API. If the server respects the TTL request, this eliminates the refresh problem entirely for typical command durations. **This is the recommended approach for Phase 7** — validate the max server-side TTL live, and fall back to strategy 1 if the server caps TTL.

```python
# Source: pattern derived from docs/PROTOCOL.md "Critical: JWT tokens expire" note
# and provider-jwt-token.service.ts default TTL of 30s

# Phase 7: request a long TTL; catch auth errors and re-authenticate
TTL_REQUEST_SECONDS = 3600  # 1 hour; server may cap — validate live

def _fetch_jwt(self, dseq: str, provider: str, ttl: int = TTL_REQUEST_SECONDS) -> str:
    # ... as above ...
```

### Anti-Patterns to Avoid

- **Buffering stdout/stderr as text:** Always use `sys.stdout.buffer.write(bytes)` not `print(text)`. Frame payloads are raw bytes from the container process — they may contain non-UTF-8 bytes (e.g., terminal escape codes).
- **Using `asyncio.run()` inside `exec()`:** The Transport ABC `exec()` is a synchronous method. Using `asyncio.run()` in a synchronous context will fail if an event loop is already running. Use `websockets.sync.client` instead.
- **Assuming frame 102 payload is always 4-byte LE int32:** The PROTOCOL.md notes it "may be JSON or protobuf". Write a parser that first attempts `int.from_bytes(payload[:4], 'little')` and falls back to `json.loads(payload)["exit_code"]`.
- **Hardcoding gseq/oseq as 1/1:** This holds for all standard single-group deployments. But use `deployment["leases"][0]` to extract `gseq`/`oseq` if the lease data exposes them.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| WebSocket framing, ping/pong, close handshake | Custom socket code | `websockets.sync.client` | RFC 6455 compliance, fragmentation, ping/pong keepalive |
| TLS cert handling | Custom SSL verification | `ssl.create_default_context()` with `CERT_NONE` | Standard Python ssl module; adding CA cert support later is trivial |
| JWT token generation from wallet key | secp256k1 signing in Python | Console API `/v1/create-jwt-token` | The project already uses managed wallets — no key material in the client |
| Base64 or protobuf parsing for result frame | Custom codec | stdlib `int.from_bytes()` + `json.loads()` fallback | Sufficient for the two known payload formats |

**Key insight:** The Console API's `/v1/create-jwt-token` endpoint is the correct authentication path for just-akash. Direct wallet-based JWT signing (as used by `provider-services` CLI) would require adding `eth-keys` or `cryptography` dependencies and handling key material the project intentionally avoids.

---

## Common Pitfalls

### Pitfall 1: JWT TTL is 30 Seconds by Default

**What goes wrong:** The WebSocket connection drops mid-command with an auth error or close code 4001/4003 after 30 seconds.
**Why it happens:** The Console API's `JWT_TOKEN_TTL_IN_SECONDS = 30` constant (confirmed from source). The `ttl` request field may allow a longer TTL — this must be validated against a live provider.
**How to avoid:** Request `ttl=3600` in the token request. If the server respects it, long commands work without refresh. If the server caps TTL, implement connection re-establishment.
**Warning signs:** Commands completing instantly (< 30s) pass tests but long-running commands (builds, downloads) fail.

### Pitfall 2: Provider TLS with Self-Signed Certificates

**What goes wrong:** `ssl.SSLCertVerificationError` on connection to provider.
**Why it happens:** Akash providers use self-signed TLS certificates. Python's default SSL context rejects them.
**How to avoid:** Use `ssl_ctx.check_hostname = False; ssl_ctx.verify_mode = ssl.CERT_NONE` for Phase 7. Document as a known limitation; Phase 8+ can add CA cert loading from deployment data.
**Warning signs:** Connection fails immediately before any frames are received.

### Pitfall 3: Frame 102 Payload Encoding is Unknown Until Live Validation

**What goes wrong:** Exit code comes back as 0 for all commands, or parsing throws an exception.
**Why it happens:** PROTOCOL.md notes the payload "may be 4-byte LE int32, or JSON `{"exit_code": N}`". The actual encoding depends on provider version.
**How to avoid:** Implement a try-both parser:
```python
try:
    exit_code = int.from_bytes(payload[:4], "little") if len(payload) >= 4 else 0
except Exception:
    exit_code = json.loads(payload).get("exit_code", 0)
```
**Warning signs:** Non-zero exits from a known-failing command always return 0.

### Pitfall 4: `hostUri` JSON Path May Vary

**What goes wrong:** `KeyError` or `None` when extracting provider URL from deployment data.
**Why it happens:** The JSON shape of `client.get_deployment(dseq)` may differ between deployments with and without active leases, or across Console API versions.
**How to avoid:** Write a robust extractor that checks multiple paths: `deployment["leases"][0]["provider"]["hostUri"]` and `deployment["leases"][0]["provider"]["host_uri"]`, with a clear `RuntimeError` if neither exists. Mirror the defensive style of `_extract_ssh_info()` in `api.py`.
**Warning signs:** `prepare()` raises `KeyError` during live testing but not in unit tests.

### Pitfall 5: Websocket Compression Interferes with Binary Frames

**What goes wrong:** Binary frame payloads arrive garbled when compression is enabled.
**Why it happens:** `websockets.sync.client.connect()` enables `permessage-deflate` compression by default (`compression="deflate"`). Most providers handle this, but some may not negotiate it correctly.
**How to avoid:** Disable compression for Phase 7: pass `compression=None` to `connect()`. Re-enable in Phase 8 after confirming provider compatibility.
**Warning signs:** Frame `code` byte reads as unexpected value; payload is shorter than expected.

### Pitfall 6: `service_name` Must Match SDL

**What goes wrong:** Provider returns a `103 Failure` frame with "service not found" immediately after connection.
**Why it happens:** The `service` query parameter must exactly match the SDL service name (e.g., `"web"`, `"app"`, `"gpu-test"`).
**How to avoid:** Infer the service name from deployment data if not specified in `TransportConfig.service_name`. Provide a helper `_infer_service()` that reads `deployment["leases"][0]["status"]["services"]` keys.
**Warning signs:** Immediate failure frame (code 103) with no stdout/stderr.

---

## Code Examples

Verified patterns from official/primary sources:

### websockets.sync.client.connect() with Auth Headers

```python
# Source: websockets.sync.client — verified locally (websockets 15.0.1/16.0 API identical)
import ssl
from websockets.sync.client import connect

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

with connect(
    "wss://provider.example.com:8443/lease/12345/1/1/shell?cmd=echo+hello&service=web&tty=false&stdin=false",
    additional_headers={"Authorization": "Bearer <JWT>"},
    ssl=ssl_ctx,
    compression=None,           # disable for Phase 7
    open_timeout=30,
    ping_interval=20,
    ping_timeout=20,
) as ws:
    while True:
        try:
            frame = ws.recv(timeout=300)
        except Exception:
            break
        # frame is bytes when server sends binary frames
        if isinstance(frame, bytes):
            code = frame[0]
            payload = frame[1:]
            # dispatch by code ...
```

### Binary Frame Dispatch

```python
# Source: docs/PROTOCOL.md — Python Implementation Notes
import sys, json

def _dispatch_frame(frame: bytes) -> int | None:
    """Dispatch a binary frame; return exit code for code=102, None otherwise."""
    code = frame[0]
    payload = frame[1:]
    if code == 100:   # stdout
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    elif code == 101: # stderr
        sys.stderr.buffer.write(payload)
        sys.stderr.buffer.flush()
    elif code == 102: # result
        try:
            return int.from_bytes(payload[:4], "little") if len(payload) >= 4 else 0
        except (ValueError, OverflowError):
            return json.loads(payload).get("exit_code", 0)
    elif code == 103: # failure
        raise RuntimeError(payload.decode("utf-8", errors="replace"))
    return None
```

### Console API JWT Token Request

```python
# Source: akash-network/console — provider/routes/jwt-token/jwt-token.router.ts
#         and provider/http-schemas/jwt-token.schema.ts (confirmed from source)
import json, urllib.request

def _fetch_jwt(console_url: str, api_key: str, dseq: str, ttl: int = 3600) -> str:
    """Request a JWT from the Console API managed-wallet endpoint."""
    body = json.dumps({
        "data": {
            "ttl": ttl,
            "leases": {dseq: {"access": "full"}},
        }
    }).encode()
    req = urllib.request.Request(
        f"{console_url}/v1/create-jwt-token",
        data=body,
        headers={
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return data["data"]["token"]  # { data: { token: "..." } }
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| mTLS certificate auth | JWT bearer token (AEP-64) | Mainnet 14, Oct 2025 | No wallet/cert management; Console API issues tokens via API key |
| `websockets.legacy` API (`websockets.connect`) | `websockets.asyncio.client` + `websockets.sync.client` | websockets 13.0 (2023) | sync client available; `additional_headers` (not `extra_headers`) is the current param name |

**Deprecated/outdated:**
- `extra_headers` kwarg: replaced by `additional_headers` in websockets 13+. PROTOCOL.md uses `additional_headers` correctly.
- `websockets.connect()` (legacy): use `websockets.sync.client.connect()` or `websockets.asyncio.client.connect()` explicitly.

---

## Validation Architecture

### Test Framework

- **Framework:** pytest (already used across the project)
- **Quick run:** `python -m pytest tests/test_transport.py tests/test_lease_shell_exec.py -v`
- **Full suite:** `python -m pytest` (runs all 406+ tests with coverage)
- **Coverage check:** `python -m pytest --cov=just_akash --cov-report=term-missing`

### Wave 0 Test Scaffolding (must exist before implementation)

The following test file must be created as empty scaffolding before any implementation code is written:

```
tests/test_lease_shell_exec.py   # new file for Phase 7
```

This file should define the test class structure with `pytest.raises(NotImplementedError)` stubs that will be replaced as implementation progresses.

### Per-Task Test Types

| Task | File | Test Type | What to Test |
|------|------|-----------|--------------|
| Wave 0: test scaffold | `tests/test_lease_shell_exec.py` | unit | Stubs raise NotImplementedError (validates scaffold works) |
| JWT fetch helper | `tests/test_lease_shell_exec.py` | unit (mock urllib) | Happy path returns token string; HTTP error raises RuntimeError |
| Provider URL extraction | `tests/test_lease_shell_exec.py` | unit | Correct URL construction from deployment fixture; missing hostUri raises RuntimeError |
| Binary frame dispatch | `tests/test_lease_shell_exec.py` | unit | Code 100 → stdout; 101 → stderr; 102 → returns exit code; 103 → raises RuntimeError |
| `exec()` end-to-end | `tests/test_lease_shell_exec.py` | unit (mock websockets) | Full exec() with mocked WebSocket returning frames 100+101+102; exit code propagated |
| Token refresh / error path | `tests/test_lease_shell_exec.py` | unit (mock urllib) | HTTP 401 on JWT fetch raises RuntimeError cleanly |
| `prepare()` validation | `tests/test_lease_shell_exec.py` | unit | Raises RuntimeError when no leases; sets `_ws_url` and `_service` |
| CLI integration | `tests/test_transport_cli_integration.py` | integration (extend existing) | `exec --transport lease-shell` now exits with correct code instead of 1 |
| Live smoke test | manual / `just test` | smoke | `just-akash exec --transport lease-shell --dseq <N> "echo hello"` returns 0 |

### Mocking Strategy

Use `unittest.mock.patch` to mock:
- `just_akash.transport.lease_shell.urllib.request.urlopen` — for JWT fetch tests
- `just_akash.transport.lease_shell.connect` — for exec WebSocket tests

Create a `FakeWebSocket` helper class in the test file:

```python
class FakeWebSocket:
    def __init__(self, frames):
        self._frames = iter(frames)
    def recv(self, timeout=None):
        try:
            return next(self._frames)
        except StopIteration:
            from websockets.exceptions import ConnectionClosedOK
            raise ConnectionClosedOK(None, None)
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def close(self): pass
```

---

## Open Questions

1. **Exact TTL the Console API will honour**
   - What we know: Default server TTL is 30s; request schema accepts `ttl: positiveInt`
   - What's unclear: Whether the server caps `ttl` at some maximum (e.g., 1800s, 3600s)
   - Recommendation: Request `ttl=3600` in Phase 7 tests; if 403/400 returned, fall back to 300s; implement re-connection on `ConnectionClosedError`

2. **Exact `leases` field format accepted by `/v1/create-jwt-token`**
   - What we know: Schema is `Record<string, any>` with string dseq keys
   - What's unclear: Whether the value must be `{"access": "full"}` or another shape (e.g., `{"access": "granular", "permissions": [...]}`)
   - Recommendation: Start with `{"access": "full"}`; if rejected, try `{}` or `{"access": "granular"}`

3. **Frame 102 payload encoding (4-byte LE int32 vs JSON)**
   - What we know: PROTOCOL.md documents both possibilities; Go source uses protobuf internally
   - What's unclear: Which encoding the current provider binary produces
   - Recommendation: Implement dual-parse (try LE int32 first, fallback to JSON); log raw payload in debug mode

4. **`hostUri` JSON path in deployment data**
   - What we know: PROTOCOL.md says `deployment["leases"][0]["provider"]["hostUri"]`
   - What's unclear: Whether all Console API deployment responses include this field, or if it requires a lease status to be "active"
   - Recommendation: Write `_extract_provider_url()` defensively; unit test with a fixture that mirrors real Console API response shape; validate live in smoke test

5. **gseq/oseq values for multi-replica or multi-group deployments**
   - What we know: PROTOCOL.md says "usually 1/1"
   - What's unclear: How to determine correct values for multi-group SDLs
   - Recommendation: Hardcode 1/1 for Phase 7 (single-group deployments only); add extraction logic in Phase 8+

---

## Sources

### Primary (HIGH confidence)

- `docs/PROTOCOL.md` (this repo) — WebSocket endpoint URL, binary frame codes 100-105, Python code patterns, connection lifecycle
- `just_akash/transport/base.py`, `lease_shell.py`, `ssh.py` — Transport ABC contract, stub to be implemented
- `websockets.sync.client.connect` — verified locally (`python3 -c "import inspect; from websockets.sync.client import connect; print(inspect.signature(connect))"`) — `additional_headers`, `ssl`, `compression=None`, `open_timeout` params confirmed
- `akash-network/console` — `apps/api/src/provider/routes/jwt-token/jwt-token.router.ts` — `/v1/create-jwt-token` endpoint confirmed; POST, bearer-or-api-key auth
- `akash-network/console` — `apps/api/src/provider/http-schemas/jwt-token.schema.ts` — `{ttl: positiveInt, leases: Record<string,any>}` request; `{token: string}` response
- `akash-network/console` — `apps/api/src/provider/services/provider-jwt-token/provider-jwt-token.service.ts` — `JWT_TOKEN_TTL_IN_SECONDS = 30` default confirmed

### Secondary (MEDIUM confidence)

- AEP-64 (akash.network/roadmap/aep-64/) — JWT claims structure (iss, exp, nbf, iat, version, leases), ES256K signing algorithm, Mainnet 14 activation
- `akash-network/console` — `apps/provider-proxy/src/routes/proxyProviderRequest.ts` — proxy does NOT handle WebSocket connections; client must connect directly to provider

### Tertiary (LOW confidence — needs live validation)

- JWT `leases` field exact format for shell scope: inferred from `getGranularLeases()` helper in service file; exact value accepted by `/v1/create-jwt-token` not confirmed against live API
- Server-side TTL cap: unknown; `ttl=3600` assumed viable; needs live test

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — websockets API verified locally; urllib used throughout api.py
- Architecture (frame dispatch): HIGH — matches PROTOCOL.md exactly
- JWT endpoint: HIGH path/method/schema; MEDIUM for exact leases field value and TTL cap
- Token refresh strategy: MEDIUM — TTL confirmed at 30s; reconnect strategy sound but untested
- Pitfalls: HIGH for TLS/binary output/compression; MEDIUM for frame 102 encoding (live validation needed)

**Research date:** 2026-04-19
**Valid until:** 2026-05-19 (30 days — websockets API stable; JWT endpoint from source so stable unless Console deploys a breaking change)
