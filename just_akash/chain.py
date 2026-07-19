"""Read-only Akash chain queries over a Cosmos REST (LCD) endpoint.

The Console API this tool normally talks to exposes NO balance endpoint (see
``smoke_providers`` — the only credit signal it has is a 402 on deploy). But the
credit *is* on-chain: Console holds the real funds in a managed depositor wallet
and grants each account an escrow ``DepositAuthorization`` whose ``spend_limits``
is the remaining deploy credit. That grant, and the account's liquid bank balance,
are both plain public-chain state, so we read them straight from a public LCD with
stdlib HTTP — no ``akash`` binary, no secret, nothing spent.

``AKASH_REST_URL`` overrides the endpoint; the default is the same provider that
backs the default ``AKASH_NODE`` RPC.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any

# Companion to the default AKASH_NODE (akash-rpc.publicnode.com): the same provider's
# REST/LCD host. A public default matches how AKASH_NODE already defaults.
DEFAULT_REST_URL = "https://akash-rest.publicnode.com"

# Akash's own escrow authorization type (custom, not a generic cosmos SendAuthorization).
_DEPOSIT_AUTH_TYPE = "/akash.escrow.v1.DepositAuthorization"

# Human labels for the denoms we expect. Both are 6-decimal ("micro") units.
# uact = Akash Credit Token, the USD-pegged Console credit; uakt = AKT.
_DENOM_META = {
    "uact": {"label": "ACT", "decimals": 6, "usd_pegged": True},
    "uakt": {"label": "AKT", "decimals": 6, "usd_pegged": False},
}


def rest_url() -> str:
    """The LCD base URL (no trailing slash), from env or the public default.

    Restricted to http/https so a crafted ``AKASH_REST_URL`` (e.g. ``file://``)
    can't point ``urllib`` at a local resource — this is what justifies the
    ``# noqa: S310`` on the ``urlopen`` calls below. Raises RuntimeError on any
    other scheme.
    """
    url = os.environ.get("AKASH_REST_URL", DEFAULT_REST_URL).rstrip("/")
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise RuntimeError(
            f"AKASH_REST_URL must use an http/https scheme; got {scheme!r} from {url!r}"
        )
    return url


def _lcd_get(path: str, timeout: int = 15) -> dict[str, Any]:
    """GET a Cosmos REST path and return parsed JSON. Raises RuntimeError on any
    transport/HTTP/parse failure, with the endpoint in the message so a dead LCD is
    obvious (and swappable via AKASH_REST_URL)."""
    url = f"{rest_url()}{path}"
    req = urllib.request.Request(  # noqa: S310 — url built from a fixed https base
        url, headers={"Accept": "application/json", "User-Agent": "just-akash-balance/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read().decode("utf-8")
    except Exception as e:  # noqa: BLE001 — normalize every failure to one error type
        raise RuntimeError(f"chain query failed ({url}): {type(e).__name__}: {e}") from e
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"chain query returned non-JSON ({url}): {body[:200]}") from e
    if not isinstance(parsed, dict):
        raise RuntimeError(f"chain query returned unexpected shape ({url}): {type(parsed)}")
    return parsed


def _coins_map(coins: list[dict[str, Any]]) -> dict[str, int]:
    """Sum a list of {denom, amount} into {denom: int_amount}. Amounts arrive as
    integer strings; some nodes append a decimal suffix (``"170623558.000…"``), so
    the integer part is parsed directly — never via float(), which would silently
    round large micro-unit balances."""
    out: dict[str, int] = {}
    for c in coins or []:
        denom = c.get("denom")
        raw = c.get("amount")
        if not denom or raw is None:
            continue
        try:
            amt = int(str(raw).split(".", 1)[0])  # drop any ".000…" suffix, parse as int
        except (TypeError, ValueError):
            continue
        out[denom] = out.get(denom, 0) + amt
    return out


def deploy_credit(address: str) -> dict[str, int]:
    """Remaining Console deploy credit for ``address``, as {denom: micro_amount}.

    Reads every escrow ``DepositAuthorization`` granted TO this account and sums
    their ``spend_limits``. An empty result means no credit grant exists (a fresh or
    fully-drained account). This is the authoritative "wallet balance" the Console
    API can't give us."""
    data = _lcd_get(f"/cosmos/authz/v1beta1/grants/grantee/{address}")
    totals: dict[str, int] = {}
    for grant in data.get("grants", []) or []:
        auth = grant.get("authorization", {})
        if auth.get("@type") != _DEPOSIT_AUTH_TYPE:
            continue
        # Newer chains report a list under "spend_limits"; tolerate a single
        # "spend_limit" object too. A zero-amount uakt entry rides alongside the real
        # uact limit — _coins_map keeps it, and formatting drops zero denoms.
        limits = auth.get("spend_limits")
        if limits is None and isinstance(auth.get("spend_limit"), dict):
            limits = [auth["spend_limit"]]
        for denom, amt in _coins_map(limits or []).items():
            totals[denom] = totals.get(denom, 0) + amt
    return totals


def credit_grant_detail(address: str) -> dict[str, Any] | None:
    """The escrow DepositAuthorization granted to ``address`` (granter + expiration),
    or None if there is none. Diagnostic detail for the wallet report — which managed
    wallet funds this account, and when the authorization lapses."""
    data = _lcd_get(f"/cosmos/authz/v1beta1/grants/grantee/{address}")
    for grant in data.get("grants", []) or []:
        if grant.get("authorization", {}).get("@type") == _DEPOSIT_AUTH_TYPE:
            return {
                "granter": grant.get("granter"),
                "grantee": grant.get("grantee"),
                "expiration": grant.get("expiration"),
            }
    return None


def bank_balances(address: str) -> dict[str, int]:
    """Liquid on-chain balance for ``address`` as {denom: micro_amount}. Usually empty
    for a Console-managed account (funds live as the credit grant, not liquid AKT)."""
    data = _lcd_get(f"/cosmos/bank/v1beta1/balances/{address}")
    return _coins_map(data.get("balances", []))


def format_amount(denom: str, micro: int) -> str:
    """Render a micro-unit amount as e.g. '170.62 ACT'. Unknown denoms pass through
    with their raw denom so nothing is silently mislabeled."""
    meta = _DENOM_META.get(denom)
    if not meta:
        return f"{micro} {denom}"
    value = micro / (10 ** meta["decimals"])
    return f"{value:,.2f} {meta['label']}"


def usd_estimate(denom: str, micro: int) -> float | None:
    """USD estimate for a USD-pegged denom (uact ≈ $1/ACT), else None. Never guesses a
    price for AKT — that floats — so callers only show '$' when it's actually pegged."""
    meta = _DENOM_META.get(denom)
    if not meta or not meta.get("usd_pegged"):
        return None
    return round(micro / (10 ** meta["decimals"]), 2)


def describe_coins(coins: dict[str, int]) -> list[dict[str, Any]]:
    """Turn {denom: micro} into display rows for the CLI/JSON, dropping zero amounts
    (a DepositAuthorization carries a 0-uakt entry beside the real uact limit). Sorted
    largest-first so the meaningful balance leads."""
    rows = [
        {
            "denom": denom,
            "micro": micro,
            "display": format_amount(denom, micro),
            "usd_estimate": usd_estimate(denom, micro),
        }
        for denom, micro in coins.items()
        if micro > 0
    ]
    rows.sort(key=lambda r: r["micro"], reverse=True)
    return rows
