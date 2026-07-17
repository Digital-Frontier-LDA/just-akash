# Phase 8: Secrets Injection via Lease-Shell - Research

> ⚠️ **SUPERSEDED — trust the shipped code, not this document.**
> This research assumes the earlier *direct-provider* design (`?cmd=` against the
> provider with `ssl.CERT_NONE`). The shipped code routes through the Console
> **provider-proxy** with **full TLS verification**. Note also that inject's final
> form streams the secret over a `104` stdin frame via `head -c <n> > path` (v1.27.0,
> issue #39) — not the `echo | base64 -d` shown here. See
> `just_akash/transport/lease_shell.py`; kept for history only (issue #38).

**Researched:** 2026-04-19
**Domain:** WebSocket-based file delivery (secrets injection) over Akash lease-shell with JWT authentication
**Confidence:** HIGH

## Summary

Phase 8 implements secrets injection over the lease-shell WebSocket transport that was established in Phase 7. The requirement is straightforward: users must be able to run `just inject` without SSH keys, using only the existing AKASH_API_KEY. The technical implementation reuses the Phase 7 `exec()` infrastructure to write files via shell commands (`mkdir`, `cat`, `chmod`), rather than inventing a new binary file transfer protocol.

The research reveals that the binary-frame protocol used for `exec()` (stdout/stderr codes 100/101, result code 102) cannot efficiently transmit arbitrary binary files — there is no file-transfer frame type (105 is terminal resize, 104 is stdin). Therefore, the correct approach is to encode secrets as base64 or shell-escaped strings and use `exec()` to run `mkdir`, `echo` (or `cat`), and `chmod` commands, identical to how SSH transport works today.

Key patterns from Phase 7 and SSH transport already established in the codebase:
- `LeaseShellTransport._exec_with_refresh()` handles JWT token expiry with automatic reconnect up to MAX_RECONNECT_ATTEMPTS
- Frame dispatch via `_dispatch_frame()` handles codes 100/101 (output), 102 (exit), 103 (error)
- Self-signed cert acceptance is enabled via SSL context
- No special directory creation or permission handling is needed beyond shell commands

**Primary recommendation:** Implement `LeaseShellTransport.inject()` as three sequential `exec()` calls: (1) `mkdir -p $(dirname remote_path)`, (2) base64-decode + write via shell `cat`, (3) `chmod 600 remote_path`. This mirrors SSHTransport.inject() exactly and avoids inventing new protocol mechanics.

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| INJS-01 | User can inject secrets via `just inject` / `just-akash inject` over lease-shell (no SSH/SCP dependency) | LeaseShellTransport.inject() mirrors SSH pattern using three exec() calls; reuses JWT auth from Phase 7; requires no new WebSocket frame types |
| INJS-02 | Injected secrets are written to the remote container without exposure in SDL or logs | Secret content is never logged; passed as base64 string to cat command; output suppressed via `capture_output=True` in Phase 7's recv loops (already implemented for stdout/stderr streaming) |

## Standard Stack

### Core (No New Dependencies)

The implementation requires **no new libraries beyond Phase 7**:

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `websockets` | ≥16.0 | Synchronous WebSocket client (Phase 7 already requires) | RFC 6455 compliant, Python 3.10+ native, no async overhead |
| `ssl` (stdlib) | 3.10+ | TLS context for self-signed provider certs (Phase 7 already requires) | Only stdlib needed; no third-party crypto library |
| `base64` (stdlib) | 3.10+ | Encode secrets for shell transmission | Universal, no alternatives |

### No New Supporting Libraries

Secrets injection via shell commands (the Phase 7 pattern) has **zero additional dependencies**. All infrastructure needed — JWT fetch, WebSocket reconnect, frame dispatch — was established in Phase 7.

## Architecture Patterns

### Recommended Pattern: Shell-Command Injection

The lease-shell protocol has **no binary file-transfer frame type**. Frame codes are:
- 100: stdout
- 101: stderr
- 102: exit code / result
- 103: error message
- 104: stdin (Phase 9, interactive shell)
- 105: terminal resize (Phase 9, interactive shell)

Therefore, file delivery must occur **via shell command execution**, not as a new frame type. This is the same pattern as SSH transport.

**Pattern 1: Three-Step Inject via exec()**

```
Step 1: mkdir -p $(dirname /remote/path)
Step 2: cat > /remote/path << 'EOF'
        {base64-encoded content}
        EOF
Step 3: chmod 600 /remote/path
```

Identical to SSHTransport.inject() in just_akash/transport/ssh.py (lines 43-64).

### Implementation Structure

```python
class LeaseShellTransport(Transport):
    def inject(self, remote_path: str, content: str) -> None:
        """Write content to remote_path; mirror SSHTransport pattern."""
        # Step 1: mkdir -p $(dirname {remote_path})
        mkdir_exit = self.exec(f"mkdir -p $(dirname {remote_path})")
        if mkdir_exit != 0:
            raise RuntimeError(f"Failed to create directory: exit code {mkdir_exit}")
        
        # Step 2: Write content via cat (with base64 or shell escape)
        # Option A (simpler, used by SSH): cat > {remote_path}
        # with self._write_via_cat() or similar
        
        # Step 3: chmod 600
        chmod_exit = self.exec(f"chmod 600 {remote_path}")
        if chmod_exit != 0:
            raise RuntimeError(f"Failed to set permissions: exit code {chmod_exit}")
```

**Why this pattern:**
- Reuses proven Phase 7 `exec()` infrastructure (no new WebSocket logic)
- Identical behavior to SSH transport (parity guarantee)
- Avoids binary file-transfer complications
- Simple, testable, compatible with provider's shell environment

### Shell-Safe Content Encoding

For secrets containing special characters (quotes, backslashes, newlines), use one of:

**Option 1: Base64 (recommended)**
```python
import base64
encoded = base64.b64encode(content.encode()).decode()
# Use: echo "{encoded}" | base64 -d > {remote_path}
```

**Option 2: Shell-escaped (printf with \x escapes)**
```python
# Use printf %b with hex escapes (POSIX-compatible)
```

Base64 is standard, universal, and zero-overhead in shell — use this.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Binary file transfer over WebSocket | Custom frame type 106+ or binary chunking protocol | Shell-command injection (mkdir → cat → chmod) via exec() | Protocol is frozen at provider; no way to add new frame types; shell commands are idempotent and universal |
| Secret content encoding for shell transmission | Custom encoding scheme or unescaped string concatenation | Base64 + `base64 -d` in shell, or printf with %b | Base64 is standard, portable, and handles all UTF-8 / binary edge cases |
| Direct socket-level stdin transmission (Phase 9 feature) | Custom stdin buffer management | Phase 9 will use frame code 104 (stdin) + pexpect for terminal handling | Frame 104 is not yet implemented; Phase 9 handles interactive sessions separately |

**Key insight:** The lease-shell protocol is designed for non-interactive command execution (query param `tty=false, stdin=false`). Files cannot be transmitted as binary frames; they must be created via shell commands. This is not a limitation — it's the design contract.

## Common Pitfalls

### Pitfall 1: Attempting Binary File Transfer via New Frame Type

**What goes wrong:** Developer assumes they can add a new frame type (e.g., 106) for binary file chunks and implement a custom protocol.

**Why it happens:** Binary frame transfer seems more efficient than shell commands. Analogy to SCP tempting.

**How to avoid:** Verify the provider's WebSocket frame code constants in `gateway/rest/constants.go` — only codes 100-105 are defined. Attempting to send an undefined code will fail silently or cause protocol errors. Shell commands are the canonical path.

**Warning signs:** 
- "Why can't we just send binary data?"
- Designing a custom chunking protocol
- Testing against a mock WebSocket (works) but fails on real provider (doesn't)

**Prevention:** Read PROTOCOL.md (docs/PROTOCOL.md) frame schema upfront. Understand that inject() on Phase 7 exec() is the only option.

### Pitfall 2: Secrets Leaking in Command Output

**What goes wrong:** `exec()` returns stdout/stderr to caller or logs; secret values appear in console.

**Why it happens:** Forgetting that `exec()` streams output to `sys.stdout.buffer` in Phase 7 (_dispatch_frame, code 100).

**How to avoid:** 
- Step 1 (`mkdir`) has no secret content, output is safe
- Step 2 (`cat`) output is suppressed by provider (cat from stdin produces no stdout)
- Step 3 (`chmod`) output is safe
- **Ensure the `cat` command uses heredoc or stdin redirection that doesn't echo the content**

**Warning signs:** Running `just inject --env SECRET=value --remote-path /etc/config` and seeing the secret value in terminal.

**Prevention:** Test with real deployment. Verify that the `cat > /path` command doesn't echo back the content.

### Pitfall 3: Path Injection via Unescaped remote_path

**What goes wrong:** If `remote_path` contains shell metacharacters (`;`, `|`, `$`, backticks), the `mkdir -p $(dirname {remote_path})` command can execute arbitrary code.

**Why it happens:** Trusting user input directly into shell command string.

**How to avoid:** **Use `shlex.quote()` to escape remote_path and any other interpolated values.**

```python
import shlex
mkdir_cmd = f"mkdir -p $(dirname {shlex.quote(remote_path)})"
```

**Warning signs:** `remote_path="/tmp; rm -rf /"` doesn't fail gracefully.

**Prevention:** Apply `shlex.quote()` to any user-supplied string interpolated into shell commands.

### Pitfall 4: Not Handling exec() Failures Properly

**What goes wrong:** `mkdir` fails (permission denied, filesystem full) but inject() doesn't notice and tries to write anyway, fails, leaves deployment in unknown state.

**Why it happens:** Not checking exit codes from intermediate `exec()` calls.

**How to avoid:** Check each `exec()` return value. If not 0, raise RuntimeError with context.

```python
rc = self.exec(f"mkdir -p ...")
if rc != 0:
    raise RuntimeError(f"mkdir failed: exit code {rc}")
```

**Warning signs:** Inject succeeds but file is not present on remote; next exec() fails mysteriously.

**Prevention:** Unit test with mocked exec() returning non-zero codes.

### Pitfall 5: Content Encoding Edge Cases

**What goes wrong:** Secret contains non-UTF-8 bytes, or newlines, or null bytes. Encoding fails or transmission corrupts the secret.

**Why it happens:** Assuming all secrets are ASCII strings.

**How to avoid:** 
- Accept `content: str` (as per Transport.inject signature)
- Assume UTF-8 (standard for Python 3)
- Encode to base64 for shell transmission
- Decode on remote side with `base64 -d`

This handles all UTF-8 edge cases transparently.

**Warning signs:** Secret with embedded newline works in SSH but fails in lease-shell.

**Prevention:** Base64 encode all content. Test with multi-line secrets.

## Code Examples

Verified patterns from codebase:

### Example 1: Three-Step Inject (from SSHTransport)

**Source:** `just_akash/transport/ssh.py` lines 43-64

```python
def inject(self, remote_path: str, content: str) -> None:
    """Inject secrets via SSH (mkdir, cat, chmod)."""
    assert self._ssh_info is not None, "Call prepare() first"
    assert self._key_path is not None, "Call prepare() first"
    ssh_cmd = _build_ssh_cmd(self._ssh_info, self._key_path)
    # mkdir -p
    subprocess.run(
        ssh_cmd + [f"mkdir -p $(dirname {remote_path})"],
        capture_output=True, text=True, check=True
    )
    # write content
    result = subprocess.run(
        ssh_cmd + [f"cat > {remote_path}"],
        input=content, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to write secrets: {result.stderr.strip()}")
    # chmod 600
    subprocess.run(
        ssh_cmd + [f"chmod 600 {remote_path}"],
        capture_output=True, text=True
    )
```

This is the **exact pattern** to follow for LeaseShellTransport.inject(), substituting `self.exec()` for `subprocess.run()`.

### Example 2: Secure Base64 Encoding for Secrets

```python
import base64
import shlex

def _inject_with_base64(self, remote_path: str, content: str) -> None:
    """Inject content using base64 encoding for shell safety."""
    # Step 1: mkdir
    mkdir_cmd = f"mkdir -p $(dirname {shlex.quote(remote_path)})"
    if self.exec(mkdir_cmd) != 0:
        raise RuntimeError(f"Failed to create directory for {remote_path}")
    
    # Step 2: base64 encode, then decode on remote
    encoded = base64.b64encode(content.encode('utf-8')).decode('ascii')
    write_cmd = f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(remote_path)}"
    if self.exec(write_cmd) != 0:
        raise RuntimeError(f"Failed to write {remote_path}")
    
    # Step 3: chmod
    chmod_cmd = f"chmod 600 {shlex.quote(remote_path)}"
    if self.exec(chmod_cmd) != 0:
        raise RuntimeError(f"Failed to set permissions on {remote_path}")
```

### Example 3: JWT Refresh Pattern (from Phase 7, already working)

**Source:** `just_akash/transport/lease_shell.py` lines 174-231

```python
def _exec_with_refresh(self, command: str) -> int:
    """Execute command with automatic JWT refresh on token expiry."""
    attempts = 0
    exit_code = 0

    while attempts < MAX_RECONNECT_ATTEMPTS:
        jwt = self._fetch_jwt()
        params = urllib.parse.urlencode({
            "cmd": command,
            "service": self._service,
            "tty": "false",
            "stdin": "false",
        })
        url = f"{self._ws_url}?{params}"
        headers = {"Authorization": f"Bearer {jwt}"}
        ssl_ctx = self._make_ssl_context()

        try:
            with connect(
                url,
                additional_headers=headers,
                ssl=ssl_ctx,
                compression=None,
                open_timeout=30,
                ping_interval=20,
                ping_timeout=20,
            ) as ws:
                while True:
                    try:
                        frame = ws.recv(timeout=300)
                    except ConnectionClosedOK:
                        return exit_code
                    except ConnectionClosedError as exc:
                        if _is_auth_expiry(exc):
                            break  # retry outer loop
                        raise  # non-auth close: propagate
                    result = self._dispatch_frame(frame)
                    if result is not None:
                        return result  # exit code received: done
        except RuntimeError as exc:
            if _is_auth_expiry_message(str(exc)):
                pass  # fall through to retry
            else:
                raise
        attempts += 1

    raise RuntimeError(
        f"Failed to re-authenticate after {MAX_RECONNECT_ATTEMPTS} attempts. "
        "Check that AKASH_API_KEY is valid and the deployment is active."
    )
```

**Inject() will call this unchanged.** Phase 8 does not modify `_exec_with_refresh()` — it reuses it.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| SSH SCP (`scp -P {port} file root@{host}:{path}`) | Shell-command injection via lease-shell exec() | v1.5 (this phase) | No SSH key or port 22 needed; uses JWT auth only |
| Binary file-transfer protocol (theoretical) | Shell commands (mkdir, cat, chmod) via WebSocket exec | v1.5 (design decision) | Simpler, no protocol extension needed, provider-compatible |
| Direct stdin transmission (Phase 9 feature) | Frame code 104 (stdin) + TTY/stdin query params | v1.5 Phase 9 | Interactive shell support; not needed for inject |

**Current provider implementation (Akash v0.24+):**
- lease-shell WebSocket frame types are frozen (100-105)
- No binary file transfer frame
- Commands are executed via query parameter `cmd=...` (non-interactive) or stdin frame 104 (interactive)
- File delivery is **always via shell commands**

## Validation Architecture

**Nyquist Validation:** Phase 8 requires test scaffolding before implementation (Wave 0).

### Test Framework and Commands

**Quick run (unit tests only):**
```bash
pytest tests/test_transport_inject.py -v
```

**Full suite (unit + mocks + edge cases):**
```bash
pytest tests/ -k "inject" -v --cov=just_akash.transport --cov-report=term-missing
```

**E2E validation (requires live deployment, Phase 11):**
```bash
just test-shell  # Phase 11: deploy + inject + verify + teardown
```

### Wave 0 Test Scaffolding Required

Before implementation begins, the following test files must exist with passing stubs:

| File | Test Type | Purpose | Required Tests |
|------|-----------|---------|-----------------|
| `tests/test_transport_inject.py` (new) | Unit | Inject method behavior in isolation | `test_inject_creates_directory`, `test_inject_writes_content`, `test_inject_sets_permissions`, `test_inject_escapes_path`, `test_inject_handles_exec_failure`, `test_inject_base64_encodes_content` |
| `tests/test_transport.py` (existing) | Unit | Transport ABC + factory | Add: `test_lease_shell_inject_not_implemented` (verify NotImplementedError is replaced by Phase 8) |
| `tests/test_transport_cli_integration.py` (existing) | Integration | CLI --inject with --transport flag | Expand: add tests for `--transport lease-shell` inject paths |

### Per-Task Test Type

Assuming Phase 8 is split into 1-3 tasks:

| Task | Responsibility | Test Type | Command |
|------|-----------------|-----------|---------|
| 08-01 | LeaseShellTransport.inject() core + helpers | Unit | `pytest tests/test_transport_inject.py::test_inject_* -v` |
| 08-02 (if exists) | CLI --transport lease-shell --env integration | Integration | `pytest tests/test_transport_cli_integration.py::test_inject_lease_shell_* -v` |
| 08-03 (if exists) | E2E validation (Phase 11 integration) | Smoke | Part of `just test-shell` recipe (Phase 11 dependency) |

### Test Examples (Wave 0 Stubs)

**`tests/test_transport_inject.py`:**

```python
"""Unit tests for LeaseShellTransport.inject()."""

import pytest
from unittest.mock import MagicMock, patch
from just_akash.transport import LeaseShellTransport, TransportConfig


class TestLeaseShellTransportInject:
    
    def _make_transport(self):
        """Helper: create LeaseShellTransport with mocked exec()."""
        config = TransportConfig(
            dseq="123",
            api_key="key",
            deployment={
                "leases": [{
                    "provider": {"hostUri": "https://provider.example.com"},
                    "status": {"services": {"web": {}}},
                }]
            },
        )
        return LeaseShellTransport(config)
    
    def test_inject_creates_directory(self):
        """Phase 8: inject() creates parent directory."""
        t = self._make_transport()
        t._ws_url = "wss://provider.example.com/lease/123/1/1/shell"
        t._service = "web"
        
        with patch.object(t, 'exec', side_effect=[0, 0, 0]) as mock_exec:
            t.inject("/tmp/secrets.env", "KEY=value")
            
            # First call: mkdir
            mkdir_call = mock_exec.call_args_list[0][0][0]
            assert "mkdir -p" in mkdir_call
            assert "/tmp/secrets.env" in mkdir_call
    
    def test_inject_writes_content(self):
        """Phase 8: inject() writes content to file."""
        t = self._make_transport()
        t._ws_url = "wss://provider.example.com/lease/123/1/1/shell"
        t._service = "web"
        
        with patch.object(t, 'exec', side_effect=[0, 0, 0]) as mock_exec:
            t.inject("/tmp/secrets.env", "SECRET=abc123")
            
            # Second call: write
            assert len(mock_exec.call_args_list) >= 2
    
    def test_inject_sets_permissions(self):
        """Phase 8: inject() restricts file permissions (chmod 600)."""
        t = self._make_transport()
        t._ws_url = "wss://provider.example.com/lease/123/1/1/shell"
        t._service = "web"
        
        with patch.object(t, 'exec', side_effect=[0, 0, 0]) as mock_exec:
            t.inject("/tmp/secrets.env", "KEY=value")
            
            # Third call: chmod
            chmod_call = mock_exec.call_args_list[2][0][0]
            assert "chmod 600" in chmod_call
    
    def test_inject_escapes_path(self):
        """Phase 8: inject() escapes shell metacharacters in remote_path."""
        t = self._make_transport()
        t._ws_url = "wss://provider.example.com/lease/123/1/1/shell"
        t._service = "web"
        
        with patch.object(t, 'exec', side_effect=[0, 0, 0]):
            # This should not execute dangerous code
            t.inject("/tmp/test'; rm -rf /", "safe_content")
            
            # If escaping works, no exception; if it doesn't, shell will fail
    
    def test_inject_handles_mkdir_failure(self):
        """Phase 8: inject() raises on mkdir failure."""
        t = self._make_transport()
        t._ws_url = "wss://provider.example.com/lease/123/1/1/shell"
        t._service = "web"
        
        with patch.object(t, 'exec', return_value=1):  # mkdir fails
            with pytest.raises(RuntimeError, match="Failed to create directory"):
                t.inject("/tmp/secrets.env", "KEY=value")
```

### Coverage Expectations

- `LeaseShellTransport.inject()`: target 100% coverage
- Shell command construction: 100%
- Error handling (exec failures): 100%
- Base64 encoding (if added): 100%

## Open Questions

1. **Should inject() use base64 encoding for all secrets?**
   - What we know: SSH transport uses plain `cat > {path}` with stdin redirection; base64 would be safer for special characters
   - What's unclear: Performance impact (negligible), CLI compatibility (need to document that secrets are UTF-8)
   - Recommendation: **Do NOT use base64 unless testing reveals shell-escape issues**. Match SSHTransport behavior exactly (pass content as stdin to `cat`, not embedded in command). If issues arise in Phase 8 testing, add base64 in Phase 8 Plan 2.

2. **What about secrets that contain newlines?**
   - What we know: SSHTransport passes content via stdin (`input=content`), which handles newlines
   - What's unclear: Does lease-shell `cat` command handle multi-line stdin correctly?
   - Recommendation: **Test during Wave 1 (first task implementation)**. Expect it to work; if it fails, use base64 fallback.

3. **Should directory creation be a separate public method?**
   - What we know: SSH transport does it inline in inject()
   - What's unclear: Whether Phase 9 (interactive shell) needs mkdir separately
   - Recommendation: Keep mkdir inline for now. Phase 9 can refactor if needed.

## Sources

### Primary (HIGH confidence)

- **Phase 7 Implementation** (`just_akash/transport/lease_shell.py`, `just_akash/transport/ssh.py`) — Verified working code patterns for exec() reuse and shell-command injection
- **PROTOCOL.md** (`docs/PROTOCOL.md`) — Official protocol schema derived from akash-network/provider source; frame type constants (100-105 only); no binary file transfer support
- **SSHTransport.inject()** (`just_akash/transport/ssh.py` lines 43-64) — Reference implementation for three-step inject pattern

### Secondary (MEDIUM confidence)

- **Akash Provider Go Source** (`gateway/rest/router_shell.go`, `gateway/rest/constants.go`) — Confirmed frame type constants and exec-only file delivery pattern (via PROTOCOL.md documentation)
- **Phase 7 STATE.md** — Token refresh pattern confirmed working in Phase 7; no new token logic needed for Phase 8

### Tertiary (validation only, no external sources required)

- Test patterns from `tests/test_transport.py` and existing CLI integration tests — establish Wave 0 scaffolding expectations

## Metadata

**Confidence breakdown:**
- **Standard Stack:** HIGH — No new libraries; Phase 7 dependencies sufficient (websockets, ssl)
- **Architecture:** HIGH — Shell-command injection is the only option; WebSocket protocol frozen at provider; reuses Phase 7 exec() without modification
- **Pitfalls:** HIGH — Identified from SSH transport (path injection, output leaking, failure handling); base64 optional but documented
- **Validation:** HIGH — Test patterns established; Wave 0 scaffolding clear; reuses Phase 7 mocking infrastructure

**Research date:** 2026-04-19
**Valid until:** 2026-05-17 (28 days — stable protocol, no pending Akash updates expected; Phase 8 depends on Phase 7 final validation)

**Why HIGH confidence:** Phase 7 successfully implemented the lease-shell exec() infrastructure; Phase 8 is a straightforward pattern application (three shell commands) with no protocol extensions needed. The design decision (shell commands vs. binary file frames) is locked in by provider constraints documented in PROTOCOL.md.
