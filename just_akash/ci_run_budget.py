"""CI-only create proxy. No direct Console fallback, retry, or budget defaults.

The server, not this client, owns the shared run/attempt counter and durable
operation journal. An ambiguous response is terminal and must be reconciled.
This binds cooperative SDK callers; possession of a Console key is not sandboxed.
"""

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation, localcontext

UNIT = "CONSOLE_DEPOSIT_USD_REQUEST"
CREATE_PATH = "/v1/akash-ci-budget/creates"
MAX_RESPONSE = 524288
MAX_MANIFEST = 262144
MAX_SDL = 262144
MAX_USD_MICROS = 9223372036854775807  # Server signed-64 counter, not an Akash/uact conversion.
TIMEOUT = 180.0
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CODES = frozenset(
    {
        "CI_BUDGET_CONFIG_REQUIRED",
        "CI_BUDGET_INVALID_INTENT",
        "CI_BUDGET_OIDC_REFUSED",
        "CI_BUDGET_OPERATION_REPLAY",
        "CI_BUDGET_EXHAUSTED",
        "CI_BUDGET_OUTCOME_UNKNOWN",
        "CI_BUDGET_POLICY_UNAVAILABLE",
        "CI_BUDGET_RESPONSE_REFUSED",
        "CI_BUDGET_DIRECT_MUTATION_REFUSED",
    }
)


class CIRunBudgetError(RuntimeError):
    """Terminal closed-code error: no raw response, payload or credential fields."""

    _is_non_retryable = True

    def __init__(self, code):
        self.code = (
            code if isinstance(code, str) and code in _CODES else "CI_BUDGET_OUTCOME_UNKNOWN"
        )
        super().__init__(self.code)


def ci_required(client=None):
    return getattr(client, "_ci_run_budget_required", False) is True or (
        os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
    )


def canonical_deposit(value):
    """Exact decimal interpretation, never rounding or converting to uact."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise CIRunBudgetError("CI_BUDGET_INVALID_INTENT")
    text = str(value)
    if len(text) > 64:
        raise CIRunBudgetError("CI_BUDGET_INVALID_INTENT")
    try:
        amount = Decimal(text)
        if not amount.is_finite() or amount <= 0 or amount.adjusted() > 18:
            raise ValueError
        with localcontext() as context:
            context.prec = 64
            amount = amount.normalize()
            if amount * 1000000 > MAX_USD_MICROS:
                raise ValueError
        if amount.as_tuple().exponent < -6:
            raise ValueError
        return format(amount, "f")
    except (InvalidOperation, ValueError):
        raise CIRunBudgetError("CI_BUDGET_INVALID_INTENT") from None


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise CIRunBudgetError("CI_BUDGET_OUTCOME_UNKNOWN")


def _exchange(request, cap):
    """One request; bounded body; error bodies are neither read nor disclosed."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            if response.status != 200:
                raise CIRunBudgetError("CI_BUDGET_OUTCOME_UNKNOWN")
            raw = response.read(cap + 1)
            if len(raw) > cap:
                raise CIRunBudgetError("CI_BUDGET_RESPONSE_REFUSED")
            return _json(raw)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        code = {
            409: "CI_BUDGET_OPERATION_REPLAY",
            402: "CI_BUDGET_EXHAUSTED",
            503: "CI_BUDGET_POLICY_UNAVAILABLE",
        }.get(status, "CI_BUDGET_OUTCOME_UNKNOWN")
        raise CIRunBudgetError(code) from None
    except CIRunBudgetError:
        raise
    except Exception:
        raise CIRunBudgetError("CI_BUDGET_OUTCOME_UNKNOWN") from None


def _token(audience):
    endpoint = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    credential = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    parsed = urllib.parse.urlsplit(endpoint)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not host.endswith(".actions.githubusercontent.com")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
        or not _credential(credential)
    ):
        raise CIRunBudgetError("CI_BUDGET_OIDC_REFUSED")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if any(key == "audience" for key, _value in query):
        raise CIRunBudgetError("CI_BUDGET_OIDC_REFUSED")
    url = urllib.parse.urlunsplit(
        parsed._replace(query=urllib.parse.urlencode(query + [("audience", audience)]))
    )
    request = urllib.request.Request(  # noqa: S310 -- validated Actions HTTPS origin
        url, headers={"Authorization": f"Bearer {credential}"}, method="GET"
    )
    try:
        result = _exchange(request, 16384)
        if not isinstance(result, dict) or set(result) != {"value"}:
            raise ValueError
        token = result["value"]
        if not _credential(token):
            raise ValueError
        return token
    except Exception:
        raise CIRunBudgetError("CI_BUDGET_OIDC_REFUSED") from None


def _credential(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 8192
        and all(33 <= ord(character) <= 126 for character in value)
    )


def request_digest(body):
    intent = {key: body[key] for key in ("operation_id", "account_id", "deposit_usd")}
    intent["sdl_sha256"] = hashlib.sha256(body["sdl_content"].encode("utf-8")).hexdigest()
    encoded = json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


class CIRunBudgetClient:
    def __init__(self, *, exchange=None, token_source=None):
        self._exchange = exchange or _exchange
        self._token_source = token_source or _token

    def create(self, sdl_content, deposit, operation_id):
        """Exactly one budget POST. Replay/unknown never authorizes a new create."""
        try:
            origin = os.environ.get("AKASH_CI_BUDGET_URL", "")
            audience = os.environ.get("AKASH_CI_BUDGET_AUDIENCE", "")
            account = os.environ.get("AKASH_CI_BUDGET_ACCOUNT_ID", "")
            parsed = urllib.parse.urlsplit(origin)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 443)
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
                or not _credential(audience)
                or not _IDENTIFIER.fullmatch(account)
            ):
                raise CIRunBudgetError("CI_BUDGET_CONFIG_REQUIRED")
            if (
                not isinstance(operation_id, str)
                or not _IDENTIFIER.fullmatch(operation_id)
                or not isinstance(sdl_content, str)
                or not 0 < len(sdl_content.encode("utf-8")) <= MAX_SDL
            ):
                raise CIRunBudgetError("CI_BUDGET_INVALID_INTENT")
            body = {
                "operation_id": operation_id,
                "account_id": account,
                "sdl_content": sdl_content,
                "deposit_usd": canonical_deposit(deposit),
            }
            token = self._token_source(audience)
            if not _credential(token):
                raise CIRunBudgetError("CI_BUDGET_OIDC_REFUSED")
            request = urllib.request.Request(  # noqa: S310 -- validated configured HTTPS origin
                origin.rstrip("/") + CREATE_PATH,
                data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            result = self._exchange(request, MAX_RESPONSE)
            if not isinstance(result, dict) or set(result) != {
                "dseq",
                "manifest",
                "operation_id",
                "account_id",
                "deposit_usd",
                "unit",
                "request_digest",
            }:
                raise ValueError
            if (
                not isinstance(result["dseq"], str)
                or not re.fullmatch(r"[1-9][0-9]{0,19}", result["dseq"])
                or int(result["dseq"]) >= 2**64
                or not isinstance(result["manifest"], str)
                or not 0 < len(result["manifest"].encode("utf-8")) <= MAX_MANIFEST
                or result["operation_id"] != operation_id
                or result["account_id"] != account
                or result["deposit_usd"] != body["deposit_usd"]
                or result["unit"] != UNIT
                or result["request_digest"] != request_digest(body)
            ):
                raise ValueError
            return {"dseq": result["dseq"], "manifest": result["manifest"]}
        except CIRunBudgetError:
            raise
        except Exception:
            raise CIRunBudgetError("CI_BUDGET_RESPONSE_REFUSED") from None
