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

import concurrent.futures
import json
import os
import urllib.parse
import urllib.request
from typing import Any

# Companion to the default AKASH_NODE (akash-rpc.publicnode.com): the same provider's
# REST/LCD host. A public default matches how AKASH_NODE already defaults.
DEFAULT_REST_URL = "https://akash-rest.publicnode.com"

# Additional public LCDs, queried alongside the default when reading deploy credit.
#
# WHY: a single public LCD can lag, and a lagging node UNDER-reports a grant because
# it has not yet seen the newest deposit. Measured 2026-08-06 on one account:
#
#     api.akashnet.net        407.85 ACT   (expiration 2036-08-04)
#     akash-api.polkachu.com  407.85 ACT   (expiration 2036-08-04)
#     akash-rest.publicnode.com  246.19 ACT   (expiration 2036-07-14)  <- default
#
# The default was $161 behind and still serving an expired-and-replaced grant. Any
# caller gating on credit — `balance --check --min-usd`, the Prometheus credit gauge,
# a CI preflight — would report a funded account as short and take the failure path.
# In CI that means falling back to paid runners while the wallet is fine.
#
# Reconciled by MAX (see `deploy_credit`): staleness can only lose a deposit, never
# invent one, so the highest reading is the freshest.
DEFAULT_REST_FALLBACKS = (
    "https://api.akashnet.net",
    "https://akash-api.polkachu.com",
)

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


def rest_urls() -> list[str]:
    """Every LCD to consult, most-trusted first.

    An explicit ``AKASH_REST_URL`` is an operator decision and is honoured ALONE —
    silently querying other hosts would defeat the point of pinning one (an
    air-gapped or private LCD, a node under test). Only the default path fans out.
    """
    # `is not None`, NOT truthiness. An explicitly-set-but-empty AKASH_REST_URL is a
    # misconfiguration, and treating it as "not pinned" silently fans out to the public
    # defaults — the exact opposite of what someone pinning an air-gapped or private LCD
    # asked for. Defer to rest_url(), which raises on an empty value, so the two agree.
    pinned = os.environ.get("AKASH_REST_URL")
    if pinned is not None:
        return [rest_url()]
    return [DEFAULT_REST_URL, *DEFAULT_REST_FALLBACKS]


def _lcd_get(
    path: str, timeout: int = 15, base: str | None = None, height: int | None = None
) -> dict[str, Any]:
    """GET a Cosmos REST path and return parsed JSON. Raises RuntimeError on any
    transport/HTTP/parse failure, with the endpoint in the message so a dead LCD is
    obvious (and swappable via AKASH_REST_URL)."""
    url = f"{(base or rest_url()).rstrip('/')}{path}"
    headers = {"Accept": "application/json", "User-Agent": "just-akash-balance/1.0"}
    if height is not None:
        headers["x-cosmos-block-height"] = str(height)
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 — fixed base
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read().decode("utf-8")
            echoed = getattr(resp, "headers", {}).get("x-cosmos-block-height")
    except Exception as e:  # noqa: BLE001 — normalize every failure to one error type
        raise RuntimeError(f"chain query failed ({url}): {type(e).__name__}: {e}") from e
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"chain query returned non-JSON ({url}): {body[:200]}") from e
    if not isinstance(parsed, dict):
        raise RuntimeError(f"chain query returned unexpected shape ({url}): {type(parsed)}")
    if height is not None:
        try:
            if echoed is None or int(echoed) != height:
                raise RuntimeError(f"chain query did not echo pinned height ({url})")
        except (TypeError, ValueError) as e:
            raise RuntimeError(f"chain query returned invalid pinned height ({url})") from e
    return parsed


# The akash module's CURRENT REST version. v1beta3 is gone: every configured endpoint
# answers it with HTTP 501 "Not Implemented" while serving v1beta4 with a 200. That 501
# is what this repo recorded as "public LCD nodes don't serve akash-module queries" — it
# was a version mismatch, not a limitation of public nodes, and the difference matters:
# one closes the door on reading chain-native deployment state, the other is a URL edit.
#
# Verified 2026-08-12 against all three configured endpoints (publicnode, akashnet,
# polkachu): v1beta3 -> 501, v1beta4 -> 200.
_DEPLOYMENT_API = "/akash/deployment/v1beta4"


def deployment_group_names(owner: str, dseq: str) -> list[str]:
    """``group_spec.name`` for every group of one deployment, read from chain.

    This is the READ half of just_akash.provenance. The placement key an SDL declares
    becomes ``group_spec.name`` inside ``MsgCreateDeployment`` — author-controlled,
    written atomically, immutable afterwards — so reading it back is how a deployment
    proves WHO created it. Nothing else on chain does: just-akash's tags live in a local
    file, and the Console API exposes no tag at all.

    Without this, ownership could only be inferred from shape (service names, age), which
    is why `cleanup_stale --reap-runners` had to be an operator's assertion rather than a
    check, and why a suspected orphan could only be reported and never acted on. A sweep
    that reaps on shape alone once destroyed 14 third-party deployments.

    Returns [] when the deployment cannot be read — from every endpoint, or because it no
    longer exists. An empty list therefore means UNKNOWN, never "not ours", and a caller
    that destroys things must treat it as such.
    """
    if not owner or not dseq:
        return []
    path = f"{_DEPLOYMENT_API}/deployments/info?id.owner={owner}&id.dseq={dseq}"
    for base in rest_urls():
        try:
            data = _lcd_get(path, base=base)
        except RuntimeError:
            continue  # one dead or lagging endpoint must not answer for the whole chain
        groups = data.get("groups")
        if not isinstance(groups, list) or not groups:
            continue
        # ALL-OR-NOTHING per response. A partial parse — three groups, two readable —
        # would claim ownership from incomplete evidence, and the caller uses this to
        # decide whether to DESTROY. Half an answer is not a weaker proof, it is a
        # different deployment's proof. So an unnamed group makes the whole response
        # unreadable and we try the next endpoint, which may simply be healthier.
        names: list[str] = []
        for g in groups:
            name = (g.get("group_spec") or {}).get("name") if isinstance(g, dict) else None
            if not isinstance(name, str) or not name:
                names = []
                break
            names.append(name)
        if names:
            return names
    return []


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
    per_endpoint: list[dict[str, int]] = []
    errors: list[str] = []
    bases = rest_urls()

    def _one(base: str) -> tuple[str, dict[str, int] | None, str]:
        try:
            data = _lcd_get(f"/cosmos/authz/v1beta1/grants/grantee/{address}", base=base)
        except RuntimeError as e:  # one dead LCD must not sink the reading
            return base, None, str(e)
        return base, _sum_deposit_grants(data), ""

    # CONCURRENT, because the timeouts add up. Queried in sequence, three dead endpoints
    # at the 15s _lcd_get timeout block for ~45s — and this call sits in front of every
    # deploy, so a slow reading looks like a hung CI job. Fanning out costs one thread
    # each and bounds the wait at the slowest single endpoint.
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(bases))) as pool:
        for base, summed, err in pool.map(_one, bases):
            if summed is None:
                errors.append(f"{base}: {err}")
            else:
                per_endpoint.append(summed)
    if not per_endpoint:
        raise RuntimeError(
            "no LCD endpoint could be reached for deploy credit: " + "; ".join(errors)
        )
    # MAX per denom: a lagging node under-reports, so the largest reading is the
    # freshest. Never min/first — that lets one stale node declare a funded account
    # empty, which is the whole reason this fans out.
    totals: dict[str, int] = {}
    for reading in per_endpoint:
        for denom, amt in reading.items():
            totals[denom] = max(totals.get(denom, 0), amt)
    return totals


def granted_uact(
    address: str, *, quorum: tuple[str, ...] | None = None, height: int | None = None
) -> int | None:
    """Canonical uact accessor for callers that need an explicit quorum contract.

    ``deploy_credit`` remains the backwards-compatible rich result. This narrow API
    returns only plural ``spend_limits[uact]`` and never converts an unreadable grant
    into zero. The optional arguments are retained as the integration seam for the
    pinned-height reader used by CI selectors.
    """
    bases = list(quorum or tuple(rest_urls()))
    if not bases:
        return None
    if height is None:
        try:
            tip = int(
                _lcd_get("/cosmos/base/tendermint/v1beta1/blocks/latest", base=bases[0])["block"][
                    "header"
                ]["height"]
            )
        except (RuntimeError, KeyError, TypeError, ValueError):
            return None
        height = tip - 3
    if height <= 0:
        return None
    readings: list[int] = []
    for base in bases:
        try:
            value = _sum_deposit_grants(
                _lcd_get(
                    f"/cosmos/authz/v1beta1/grants/grantee/{address}", base=base, height=height
                )
            ).get("uact")
        except RuntimeError:
            continue
        if value is not None:
            readings.append(value)
    if not readings:
        return None
    counts = {value: readings.count(value) for value in set(readings)}
    agreeing = [value for value, count in counts.items() if count >= 2]
    return max(agreeing) if agreeing else None


def free_uact(granted_uact_value: int) -> int:
    """Free deploy credit in uact, derived from the DepositAuthorization spend_limit.

    ⭐ Fix for #169: ``spend_limits`` is ALREADY NET of locked escrow. The Cosmos
    authz module decrements ``spend_limits`` as the grantee uses escrow, so the
    value the chain returns is the *remaining* allowance, NOT the gross grant.
    Subtracting a separately-measured ``locked_uact`` double-subtracts and clamps
    to 0 — so a 90 ACT account with 346 ACT in escrow reads ``free_uact = 0``,
    firing the low-credit alarm permanently on a funded wallet.

    The OLD expression ``max(granted_uact - locked_uact, 0)`` is wrong. The
    correct expression is the spend_limit value itself (clamped to 0 defensively):

    - **Two independent payload-level disproofs (from the issue):**
      1. ``locked > granted`` is routine on real accounts
         (e.g. akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee: granted=90.23 ACT,
         locked=346.43 ACT). A gross grant could not allow more escrow to be
         locked than was ever granted. ``spend_limits`` must be net.
      2. ``spend_limits`` falls in exact 5 ACT steps as deployments are created
         (measured: ``25.670005 -> 15.670001`` = -10.000004 on 2 deposits,
         ``15.670001 -> 10.662414`` = -5.007587 on 1 deposit). A deposit's
         escrow cost is 5 ACT; only a *remaining allowance* decreases by that
         exact amount. A gross grant does not move when a deposit is taken.

    - **Where this is used:** ``cli.py:999`` (deploy-credit-check path) and
      ``cli.py:1097`` (wallet-balance path). Both sites previously computed
      ``free_uact = max(granted_uact - locked_uact, 0)`` — the bug. They now
      call this helper.

    ``locked_in_escrow_uact`` is still useful as a DISPLAY field (how much is
    parked in escrow right now) — keep emitting it in payloads. It is just
    not a subtrahend of free credit.
    """
    if granted_uact_value < 0:
        return 0
    return granted_uact_value


def _sum_deposit_grants(data: dict[str, Any]) -> dict[str, int]:
    """Sum uact spend_limits across DepositAuthorization grants in one LCD payload."""
    totals: dict[str, int] = {}
    for grant in data.get("grants", []) or []:
        auth = grant.get("authorization", {})
        if auth.get("@type") != _DEPOSIT_AUTH_TYPE:
            continue
        # The chain carries a singular `spend_limit` uakt decoy alongside the
        # plural DepositAuthorization allowance. Never fall back to the singular
        # field: treating it as a list reports zero deploy credit for a funded
        # Console AUTHZ grantee.
        limits = auth.get("spend_limits")
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
