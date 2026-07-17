"""Dynamically-generated throwaway credentials for tests.

detect-secrets anchors on static secret-looking *literals* in source (a
``api_key = "..."`` / ``secret = "..."`` assignment). Generating these at runtime
means there is no literal for it to flag, so the values stay out of
``.secrets.baseline`` — which stops the baseline churning every time an unrelated
edit shifts a test line (it did on #39, #61, #64). See issue #38 item 3.

The values are only used within a single process/run and never persisted, so a
fresh random each call is fine; where a test asserts equality it just captures the
returned value rather than comparing to a literal.
"""

from __future__ import annotations

import secrets


def fake_api_key(label: str = "test") -> str:
    """A throwaway API key for test configs — non-literal, so the scanner ignores it."""
    return f"{label}-key-{secrets.token_hex(4)}"


def fake_secret(label: str = "secret") -> str:
    """A throwaway secret VALUE (e.g. an injected env value) for tests."""
    return f"{label}-{secrets.token_hex(8)}"
