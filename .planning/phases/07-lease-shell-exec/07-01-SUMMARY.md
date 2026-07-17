---
phase: 07-lease-shell-exec
plan: 01
type: execute
completed_date: 2026-04-19
duration_minutes: 4
tasks_completed: 2
tests_added: 34
test_suite_status: 443 passed, 0 failed
key_files:
  - just_akash/api.py
  - just_akash/transport/lease_shell.py
  - tests/test_lease_shell_exec.py
  - tests/test_transport.py
  - tests/test_transport_cli_integration.py
key_decisions:
  - Synchronous websockets client (websockets.sync.client) for non-interactive exec
  - Self-signed cert acceptance via custom SSL context (Phase 7 only, defer validation to Phase 8)
  - JWT TTL default 3600s with server fallback (Phase 8 token refresh deferred)
  - Host URI snake_case fallback (hostUri OR host_uri) for API compatibility
subsystem: transport/lease-shell
tags:
  - websocket
  - jwt
  - sync-client
  - frames
  - phase-7-exec
---

# Phase 7 Plan 1: Lease-Shell Exec Implementation Summary

> ⚠️ **SUPERSEDED — trust the shipped code, not this document.**
> Parts of this summary reference the earlier *direct-provider* design (`?cmd=` with
> `ssl.CERT_NONE`). The shipped code routes through the Console **provider-proxy** with
> **full TLS verification**. See `just_akash/transport/lease_shell.py` (issue #38).

JWT authentication + synchronous WebSocket command execution over lease-shell transport. Replaces Phase 6 stub with full exec() + prepare() implementation. 34 unit tests cover JWT fetch, provider URL extraction, frame dispatch, and end-to-end happy path.

## Overview

Completed implementation of `AkashConsoleAPI.create_jwt()` and full `LeaseShellTransport` (prepare + exec) to enable non-interactive command execution on Akash provider containers via WebSocket. Uses synchronous websockets client (NOT asyncio), accepts self-signed provider certificates, and dispatches binary frames to stdout/stderr/exit-code channels.

**Fulfills requirements:** EXEC-01, EXEC-02, EXEC-03, LSHL-02

## Tasks Completed

### Task 1: Implement AkashConsoleAPI.create_jwt() and LeaseShellTransport

**Status:** COMPLETE

Added `create_jwt(dseq, ttl=3600)` method to AkashConsoleAPI that:
- POSTs to `/v1/create-jwt-token` with lease access scope
- Parses JWT from response `{"data": {"token": "..."}}`
- Raises RuntimeError on HTTP error or missing token field

Replaced LeaseShellTransport Phase 6 stub with full implementation:
- **prepare()** — Extracts provider hostUri and service name from deployment; sets `_ws_url` and `_service`
- **exec(command)** — Connects via websockets.sync.client with JWT bearer token; dispatches frames (100=stdout, 101=stderr, 102=exit code, 103=failure); returns exit code
- **_extract_provider_url()** — Parses deployment lease data; handles hostUri/host_uri snake_case fallback; converts https://→wss://, http://→ws://
- **_dispatch_frame()** — Routes frame codes to sys.stdout.buffer/sys.stderr.buffer; parses exit code as LE int32 or JSON fallback; raises on code 103
- **_make_ssl_context()** — Accepts self-signed provider certs (Phase 7 only)
- **validate()** — Returns True if deployment has active lease with provider hostUri
- **inject() / connect()** — Still raise NotImplementedError (Phase 8-9)

**Commit:** `729121b` — feat(07-01): implement AkashConsoleAPI.create_jwt() and LeaseShellTransport.prepare()+exec()

### Task 2: Write Unit Tests

**Status:** COMPLETE

Created `tests/test_lease_shell_exec.py` with 34 passing test functions:

**JWT Fetch (3 tests)**
- Happy path: _fetch_jwt() calls create_jwt and returns token
- Default TTL handling (3600s)
- Error propagation from create_jwt()

**Provider URL Extraction (7 tests)**
- Happy path with camelCase hostUri
- HTTPS → WSS / HTTP → WS protocol conversion
- Snake_case host_uri fallback
- Error on missing leases, missing hostUri, missing service name

**Frame Dispatch (11 tests)**
- Code 100: stdout to sys.stdout.buffer
- Code 101: stderr to sys.stderr.buffer
- Code 102: exit code as LE int32
- Code 102: fallback to JSON when < 4 bytes
- Code 102: default to 0 on parse failure
- Code 103: RuntimeError with provider message
- Code 103: UTF-8 decode error handling
- Unknown codes return None
- Empty / non-bytes input handling

**exec() Happy Path (6 tests)**
- Stdout capture and return exit code 0
- Both stdout and stderr capture
- Non-zero exit code propagation (127, 137)
- JWT in Authorization header
- compression=None in connect kwargs
- Auto-prepare when _ws_url not yet set

**validate() (5 tests)**
- True with hostUri present
- True with host_uri snake_case
- False with empty leases
- False with missing hostUri
- False with non-dict provider

**NotImplementedError (2 tests)**
- inject() raises with Phase 8 message
- connect() raises with Phase 9 message

**Commit:** `aaa26e5` — test(07-01): add comprehensive unit tests for lease-shell exec and update phase 6 tests

## Files Modified/Created

### New Files
- **tests/test_lease_shell_exec.py** — 34 unit tests (470 lines)

### Modified Files
- **just_akash/api.py** — Added create_jwt() method (45 lines)
- **just_akash/transport/lease_shell.py** — Replaced stub with full Phase 7 implementation (226 lines)
- **tests/test_transport.py** — Updated Phase 6 stub tests to Phase 7 expectations (8 tests refactored)
- **tests/test_transport_cli_integration.py** — Updated TestLeaseShellStubBehaviour for Phase 7 (5 tests refactored)

## Deviations from Plan

None — plan executed exactly as written. No auto-fixes needed.

## Verification Results

All success criteria met:

- [x] `AkashConsoleAPI.create_jwt(dseq, ttl)` exists in just_akash/api.py and uses `self._request("POST", "/v1/create-jwt-token", ...)`
- [x] `LeaseShellTransport.prepare()` sets `_ws_url` and `_service`; raises RuntimeError on missing lease/hostUri
- [x] `LeaseShellTransport.exec()` uses `websockets.sync.client.connect()` (sync, not asyncio); dispatches frames 100/101/102/103; returns integer exit code
- [x] `_dispatch_frame()` writes to `sys.stdout.buffer`/`sys.stderr.buffer` (not print/text mode)
- [x] 34 unit tests pass in `tests/test_lease_shell_exec.py` (exceeds 12 minimum)
- [x] Full test suite passes: **443 tests green, 0 failed, no regressions**
- [x] No SSH key or SSH infrastructure required for lease-shell exec path
- [x] No asyncio imports
- [x] compression=None present in websockets.sync.client.connect() call
- [x] All must-have artifacts satisfied (see artifacts section below)

## Must-Have Artifacts

All artifacts verified present:

**AkashConsoleAPI.create_jwt** (in just_akash/api.py)
```
def create_jwt(self, dseq: str, ttl: int = 3600) -> str:
    """Request a short-lived JWT for lease-shell auth..."""
    response = self._request(
        "POST",
        "/v1/create-jwt-token",
        {"data": {"ttl": ttl, "leases": {dseq: {"access": "full"}}}},
    )
    # Response parsing with fallback logic
```

**LeaseShellTransport.prepare() + exec()** (in just_akash/transport/lease_shell.py)
- prepare(): 10 lines, extracts provider URL + service name
- exec(): 32 lines, full WebSocket handshake, frame loop, exit code return
- Total file: 226 lines, well above minimum 80-line requirement

**Unit Tests** (in tests/test_lease_shell_exec.py)
- 34 test functions across 6 test classes
- 470 lines, covers all required scenarios
- FakeWebSocket helper for mocking

## Key Links Verified

From plan's must_haves.key_links:

| From | To | Via | Status |
|------|----|----|--------|
| just_akash/transport/lease_shell.py | just_akash/api.py | AkashConsoleAPI.create_jwt() call inside LeaseShellTransport._fetch_jwt() | ✓ Present (line 51) |
| just_akash/transport/lease_shell.py | wss://{provider}/lease/{dseq}/1/1/shell | websockets.sync.client.connect() in exec() | ✓ Present (line 186) |
| just_akash/transport/lease_shell.py | sys.stdout.buffer / sys.stderr.buffer | binary frame dispatch on codes 100/101 | ✓ Present (lines 126-129) |

## Test Coverage

Lease-shell module coverage: **91%** (123 statements, 11 missed)
- Missed: _get_api_client caching (lines 45-50), _infer_service edge cases (lines 69, 72, 96), _dispatch_frame unused paths (135-136, 199-200, 226)
- All critical paths covered by unit tests

Full suite: **74% overall coverage**, 443 tests

## Decisions Made

1. **Synchronous websockets client** — Use websockets.sync.client.connect() not asyncio. Rationale: Transport.exec() is synchronous interface; asyncio incompatible with existing v1.4 SSH behavior.

2. **Self-signed cert acceptance** — Custom SSL context with check_hostname=False, CERT_NONE. Rationale: Phase 7 deployments use self-signed certs; validation deferred to Phase 8 cert pinning.

3. **JWT TTL default 3600s** — Request 3600s but provider may cap at 30s. Rationale: Phase 8 handles token refresh on 30s cap; Phase 7 works either way.

4. **Host URI fallback** — Check both hostUri (camelCase) and host_uri (snake_case). Rationale: API inconsistency; both formats seen in real deployments.

5. **Frame code logic** — Code 102 tries LE int32, falls back to JSON, defaults to 0. Rationale: Handles multiple server implementations without strict format.

## What's NOT Included (Deferred)

- **Token refresh on TTL expiry** (Phase 8, LSHL-03) — Current impl gets single token, disconnects; reconnect triggers new token
- **Interactive shell** (Phase 9, LSHL-04) — inject() and connect() still raise NotImplementedError
- **Cert validation** (Phase 8+) — Self-signed acceptance OK for Phase 7; pinning deferred
- **Timeout/retry logic** (Phase 7.5) — Current impl has 300s recv timeout, no reconnect retry

## Blocked By / Blocking

- **Blocked by:** Phase 6 Plan 2 (Protocol discovery via docs/PROTOCOL.md) — COMPLETE ✓
- **Blocking:** Phase 7 Plan 2 (Token refresh LSHL-03) — Ready to start
- **Blocking:** Phase 7 Plan 3 (Error handling + timeout retry) — Ready to start
- **Blocking:** Phase 8 (inject() implementation) — Awaits Phase 7 completion

## Performance & Quality

| Metric | Value |
|--------|-------|
| Execution time | 3.8 minutes |
| Tasks completed | 2/2 |
| Tests written | 34 new |
| Test suite | 443 passed, 0 failed |
| Code coverage (lease_shell.py) | 91% |
| Commits | 2 atomic commits |
| Deviations | 0 (no auto-fixes needed) |

## Next Steps

1. **Phase 7 Plan 2** — Token refresh (LSHL-03): Handle 30s TTL cap, reconnect with new token
2. **Phase 7 Plan 3** — Error handling: Timeout retry, provider error messages, graceful degradation
3. **Phase 8** — File inject via WebSocket: Reuse exec() infrastructure
4. **Phase 9** — Interactive shell: Full duplex stdin/stdout/stderr streaming

---

**Executor:** Claude Sonnet 4.6  
**Start:** 2026-04-19T11:12:55Z  
**End:** 2026-04-19T11:16:33Z  
**Duration:** ~4 minutes
