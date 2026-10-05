"""Task-local logging boundary for credential-bearing tenant SDL.

This changes diagnostics, never request data, retries, create receipts or delivery.
Public operations retain their existing messages. Do not identify secrets by value:
remote responses may escape or transform a credential before echoing it.
"""

from __future__ import annotations

import inspect
import math
import re
from contextlib import suppress
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any, cast

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from .address import is_canonical_akash_address

_ACTIVE: ContextVar[bool] = ContextVar("akash_confidential_sdl", default=False)
_READER_KEYS = ("DOCKERHUB_PULL_USERNAME", "DOCKERHUB_PULL_TOKEN")
# These phrases already control existing create, lease and credit decisions.
_RETRY_MARKERS = (
    "already exists",
    "no longer open",
    "no lease for deployment",
    "jwt has invalid claims",
    "insufficient credit",
    "insufficient balance",
    "payment required",
    "http 402",
    "status 402",
    "status_code=402",
    "(402)",
)


def active() -> bool:
    return _ACTIVE.get()


def credential_content(content: str) -> bool:
    """Inspect YAML nodes without constructing objects or losing duplicate keys.

    JSON manifests are YAML too. Parse failures are confidential: a malformed private
    document must not expose its contents through a parser error before submission.
    The visited set bounds aliases, including cycles; every unique node is inspected.
    """
    try:
        root = yaml.compose(content, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError):
        return True
    pending = [root] if root is not None else []
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, MappingNode):
            for key, value in node.value:
                if isinstance(key, ScalarNode) and key.value == "credentials":
                    return True
                pending.extend((key, value))
        elif isinstance(node, SequenceNode):
            pending.extend(node.value)
        elif isinstance(node, ScalarNode) and any(
            node.value.partition("=")[0] == key for key in _READER_KEYS
        ):
            return True
    return False


def canonical_dseq(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return False
    text = str(value)
    return re.fullmatch(r"[1-9][0-9]{0,19}", text) is not None and int(text) <= 2**64 - 1


def display(value: object, kind: str) -> str:
    """Private diagnostics expose only structurally verified lifecycle metadata.

    This never rewrites the transport or auction data. A malformed remote field
    may echo any part of a payload; printable string/type alone is insufficient.
    """
    if not active():
        return str(value)
    valid = False
    if kind == "address_list":
        return (
            str([display(item, "address") for item in value])
            if isinstance(value, list)
            else "<withheld>"
        )
    if kind == "number":
        valid = type(value) is int or type(value) is float and math.isfinite(value)
    elif kind == "bool":
        valid = type(value) is bool
    elif kind == "dseq":
        valid = canonical_dseq(value)
    elif kind == "address":
        valid = isinstance(value, str) and is_canonical_akash_address(value)
    elif kind == "state":
        valid = isinstance(value, str) and value in {"open", "active", "closed", "lost", "?"}
    elif kind == "denom":
        valid = isinstance(value, str) and value in {"uakt", "uact"}
    return str(value) if valid else "<withheld>"


def diagnostic_context(context: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in context.items():
        if key in {"dseq", "provider", "account"}:
            kind = "dseq" if key == "dseq" else "address"
            result[key] = display(value, kind)
        elif (
            isinstance(value, bool)
            or value is None
            or type(value) is int
            or type(value) is float
            and math.isfinite(value)
        ):
            result[key] = value
        else:
            result[key] = "<withheld>"
    return result


def protect_content(content: str) -> None:
    """Check the actual read/transformed bytes, not only the preflight file snapshot."""
    if credential_content(content):
        _ACTIVE.set(True)


def request_is_confidential(data: Any) -> bool:
    if active():
        return True
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "credentials":
                return True
            if key in ("sdl", "manifest") and isinstance(value, str) and credential_content(value):
                return True
            if request_is_confidential(value):
                return True
    elif isinstance(data, list):
        return any(request_is_confidential(value) for value in data)
    return False


def withheld_error(error: object) -> str:
    """Only fixed classifications may cross the private error boundary."""
    message = str(error)
    if message == (
        "Receipt-bound create already exists; reconcile the recorded operation before retrying"
    ):
        return message  # Fixed local refusal, not upstream prose or replay authority.
    text = message.lower()
    if "non-retryable create outcome ambiguous" in text:
        return "NON-RETRYABLE CREATE OUTCOME AMBIGUOUS: credential-bearing operation failed"
    markers = [marker for marker in _RETRY_MARKERS if marker in text]
    prefix = "Connection error: " if text.startswith("connection error:") else ""
    detail = "; ".join(markers) if markers else "credential-bearing operation failed"
    return f"{prefix}{detail} (details withheld)"


def error_text(error: object) -> str:
    if not active():
        return str(error)
    if isinstance(error, Exception):
        # A structured error name/body may be interpolated separately after its
        # message. Sanitize the object too, before any such diagnostic is emitted.
        sanitize_exception(error)
    return withheld_error(error)


def sanitize_exception(error: Exception) -> None:
    """Preserve the original exception class while removing echo-bearing fields."""
    error.args = (withheld_error(error),)
    if isinstance(error, OSError):
        # OSError.__str__ may use these slots instead of args. Keep errno, but not
        # untrusted strerror/filename echoes (including a socket exception's reason).
        error.strerror = "details withheld" if error.errno is not None else None
        error.filename = None
        error.filename2 = None
    # These optional fields belong to concrete exception subclasses. Keep this
    # helper independent of api.py (which imports it), and guard every access.
    mutable = cast(Any, error)
    if hasattr(error, "body"):
        mutable.body = ""
    if hasattr(error, "error_name") and mutable.error_name != "origin_response_timeout":
        mutable.error_name = ""
    error.__cause__ = None
    error.__context__ = None
    if hasattr(error, "__notes__"):
        mutable.__notes__ = []


def sdl_operation(function):
    """Scope the complete lifecycle, including read-back/cleanup error diagnostics.

    The final transformed SDL is checked again by the preparation decorator. Context
    tokens reset even on failure, so a later public operation cannot inherit this mode.
    This reads only the explicit SDL path; it never reads a dotenv or ambient token.
    """
    signature = inspect.signature(function)

    @wraps(function)
    def scoped(*args, **kwargs):
        values = signature.bind(*args, **kwargs).arguments
        private = active()
        path = values.get("sdl_path")
        if path is not None:
            source = Path(path)
            if values.get("gpu"):
                variant = source.with_name(f"{source.stem}-gpu{source.suffix}")
                if variant.exists():
                    source = variant
            # Preserve the operation's existing missing/unreadable-file error.
            with suppress(OSError):
                private |= credential_content(source.read_text())
        private |= any(
            isinstance(value, str) and value.partition("=")[0] in _READER_KEYS
            for value in values.get("env_vars") or []
        )
        token = _ACTIVE.set(private)
        try:
            return function(*args, **kwargs)
        except Exception as error:
            if active():
                sanitize_exception(error)
                raise error from None
            raise
        finally:
            _ACTIVE.reset(token)

    return scoped
