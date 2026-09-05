"""Native multi-key Console wallet discovery, ranking, and ownership routing."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal

from akash_lease_core import WalletCandidate, WalletPolicy, rank_wallets

from . import chain
from .api import AkashConsoleAPI, _extract_dseq


@dataclass(frozen=True)
class WalletClientSelection:
    client: AkashConsoleAPI
    account: str | None
    available_uact: int | None
    configured_keys: int
    distinct_accounts: int
    policy_version: str


def configured_api_keys() -> list[str]:
    """Configured Console keys, de-duplicated without ever logging their values."""

    pieces = re.split(r"[\n,;]", os.environ.get("AKASH_API_KEYS", ""))
    fallback = os.environ.get("AKASH_API_KEY", "").strip()
    if fallback:
        pieces.append(fallback)
    result: list[str] = []
    seen: set[str] = set()
    for piece in pieces:
        key = piece.strip()
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\\\)")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _one_line(text: str, limit: int = 300) -> str:
    """Bound a third-party exception to ONE printable line before it is logged.

    ⛔ CWE-117. These strings come from an HTTP client and can carry
    server-controlled bytes. In GitHub Actions a line beginning `::error::` (or
    `::add-mask::`, `::stop-commands::`) is a WORKFLOW COMMAND, not output — so
    an embedded newline lets a remote endpoint forge annotations, mask text, or
    switch command processing off entirely. ANSI escapes can additionally
    rewrite what a reader sees in the terminal.

    ⚠ FLATTENING IS THE FIX, not cosmetics. A workflow command is only honoured
    at the START of a line, so removing newlines removes the only way injected
    content can reach that position — and this became load-bearing when the
    failures started being joined one-per-line. `::` is left intact MID-string
    on purpose: rewriting it would corrupt legitimate text (IPv6 literals, C++
    scope, timestamps) while adding nothing once no newline can precede it. A
    leading `::` is still displaced, so the helper is safe for callers that do
    not prefix each entry the way this module does.
    """

    flattened = _ANSI_RE.sub("", text)
    flattened = _CONTROL_RE.sub("", flattened.replace("\r\n", " ").replace("\n", " "))
    flattened = flattened.replace("\r", " ").replace("\t", " ").strip()
    if flattened.startswith("::"):
        flattened = " " + flattened
    if len(flattened) > limit:
        flattened = flattened[: limit - 1] + "…"
    return flattened


def _redact_keys(message: str, keys: list[str]) -> str:
    """Strip any configured key that a third-party exception may have echoed back.

    ⛔ THIS MODULE'S CONTRACT IS THAT KEY VALUES ARE NEVER LOGGED — see
    `configured_api_keys`, "de-duplicated without ever logging their values". The
    failure reasons added alongside this function come from exceptions raised by an
    HTTP client, and a client that puts the request URL or an auth header into its
    message would carry a key straight into the run log, which is world-readable on
    a public Actions run. Reporting the cause must not cost the secret.

    ⛔ ONE PASS, LONGEST KEY FIRST — the ordering IS the security property.
    A `str.replace` per key in configuration order leaks (CWE-532): with keys
    `abc` and `abcdef`, the shorter runs first, rewrites an echoed `abcdef` to
    `***def`, and the longer key's suffix survives in a world-readable log
    while the redaction reports itself done. It needs only one configured key
    to be a prefix of another, and nothing prevents that.

    Fixed by construction rather than by reordering the loop. A single regex
    pass tries alternatives longest-first at each position and resumes AFTER
    the match, so no substitution can create or destroy another one — which a
    sequential loop cannot promise however it is ordered. Sorting on
    (-len, value) additionally makes the output independent of the order the
    keys were configured in, so the guarantee does not rest on caller habit.
    """
    ordered = sorted({k for k in keys if k}, key=lambda k: (-len(k), k))
    if not ordered:
        return message
    return re.sub("|".join(re.escape(k) for k in ordered), "***", message)


def _candidate_id(index: int) -> str:
    """Opaque in-process identity; never derive an identifier from a credential."""

    return f"wallet-{index}"


def _http_endpoint(endpoint: str) -> str:
    """Validate again at the urllib boundary, even though chain.rest_urls does too."""

    normalized = endpoint.rstrip("/")
    if urllib.parse.urlparse(normalized).scheme.lower() not in {"http", "https"}:
        raise RuntimeError("Akash LCD endpoint must use http or https")
    return normalized


def _default_credit_reader(account: str) -> int:
    """Height-pinned quorum of on-chain uact spend limits.

    A stale LCD can return a valid but obsolete grant, so max/first is not a
    safe funding oracle. Two independent endpoints must agree at one height.
    """

    endpoints = chain.rest_urls()
    if len(endpoints) == 1:
        return int(chain.deploy_credit(account).get("uact", 0))
    height = _chain_height(endpoints)
    target = height - 3
    with ThreadPoolExecutor(max_workers=len(endpoints)) as pool:
        readings = list(
            pool.map(lambda endpoint: _credit_at(endpoint, account, target), endpoints)
        )
    return _quorum_uact(readings)


def _chain_height(endpoints: list[str]) -> int:
    for endpoint in endpoints:
        url = f"{_http_endpoint(endpoint)}/cosmos/base/tendermint/v1beta1/blocks/latest"
        request = urllib.request.Request(  # noqa: S310 — configured http(s) LCD endpoints
            url, headers={"Accept": "application/json", "User-Agent": "just-akash-wallet/1.0"}
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                payload = json.loads(response.read().decode())
            return int(payload["block"]["header"]["height"])
        except Exception:  # noqa: BLE001,S112 — try the next independent LCD
            continue
    raise RuntimeError("no LCD endpoint could establish the current Akash block height")


def _credit_at(endpoint: str, account: str, height: int) -> int | None:
    url = f"{_http_endpoint(endpoint)}/cosmos/authz/v1beta1/grants/grantee/{account}"
    request = urllib.request.Request(  # noqa: S310 — configured http(s) LCD endpoints
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "just-akash-wallet/1.0",
            "x-cosmos-block-height": str(height),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            payload = json.loads(response.read().decode())
            echoed = response.headers.get("x-cosmos-block-height")
        if echoed is None or int(echoed) != height or not isinstance(payload, dict):
            return None
        return int(chain._sum_deposit_grants(payload).get("uact", 0))
    except Exception:  # noqa: BLE001 — an unprovable endpoint contributes no vote
        return None


def _quorum_uact(readings: list[int | None], quorum: int = 2) -> int:
    measured = [value for value in readings if value is not None]
    for value in measured:
        if measured.count(value) >= quorum:
            return value
    raise RuntimeError("no height-pinned LCD quorum for this Console wallet allowance")


def select_client_for_create(
    required_uact: int,
    *,
    client_factory: Callable[[str], AkashConsoleAPI] = AkashConsoleAPI,
    credit_reader: Callable[[str], int] = _default_credit_reader,
) -> WalletClientSelection:
    """Choose the richest distinct account able to fund a new deployment."""

    keys = configured_api_keys()
    if not keys:
        raise RuntimeError("AKASH_API_KEY or AKASH_API_KEYS must be set")
    if len(keys) == 1:
        return WalletClientSelection(client_factory(keys[0]), None, None, 1, 1, "single-wallet")

    clients: dict[str, AkashConsoleAPI] = {}
    candidates: list[WalletCandidate] = []
    failures: list[str] = []
    errors = 0
    for index, key in enumerate(keys):
        candidate_id = _candidate_id(index)
        client = client_factory(key)
        clients[candidate_id] = client
        try:
            account = client.account_address()
            available = credit_reader(account)
            candidates.append(
                WalletCandidate(
                    candidate_id=candidate_id,
                    account=account,
                    available_credit=Decimal(available),
                    denom="uact",
                )
            )
        except Exception as exc:  # noqa: BLE001 — one broken wallet must not hide healthy siblings
            errors += 1
            # ⛔ KEEP THE REASON. Counting the failure and discarding what it was
            # leaves the caller with "could not measure any of 3", which names the
            # symptom and hides every cause — auth, network, rate limit and a typo'd
            # key all render identically. MEASURED in Borduas-Holdings/blazing job
            # 101096063489: that line appeared six times and the run then classified
            # itself PROVIDER_CAPACITY, "a market/capacity condition, not a code
            # failure" — a verdict about the market reached without reading a wallet.
            failures.append(
                f"{candidate_id}: {_one_line(_redact_keys(f'{type(exc).__name__}: {exc}', keys))}"
            )

    result = rank_wallets(
        candidates,
        WalletPolicy(required_credit=Decimal(required_uact), denom="uact"),
    )
    if result.selected is None:
        if not candidates and errors:
            # One per line, as the PR describes. A single "; "-joined line put
            # every wallet's reason in one wall of text exactly when there are
            # most of them to read.
            raise RuntimeError(
                f"could not measure any of {len(keys)} configured Console wallets:\n  "
                + ";\n  ".join(failures)
            )
        richest = max((int(item.available_credit) for item in candidates), default=0)
        raise RuntimeError(
            "no Console wallet can fund this deployment: "
            f"required={required_uact} uact, richest_measured={richest} uact"
        )
    selected = result.selected
    return WalletClientSelection(
        client=clients[selected.candidate_id],
        account=selected.account,
        available_uact=int(selected.available_credit),
        configured_keys=len(keys),
        distinct_accounts=len({item.account for item in candidates}),
        policy_version=result.policy_version,
    )


def select_client_for_dseq(
    dseq: str,
    *,
    client_factory: Callable[[str], AkashConsoleAPI] = AkashConsoleAPI,
) -> AkashConsoleAPI:
    """Find the configured wallet that owns ``dseq`` by positive read-back."""

    keys = configured_api_keys()
    if not keys:
        raise RuntimeError("AKASH_API_KEY or AKASH_API_KEYS must be set")
    if len(keys) == 1:
        return client_factory(keys[0])
    for key in keys:
        client = client_factory(key)
        try:
            deployment = client.get_deployment(str(dseq))
        except RuntimeError:
            continue
        if isinstance(deployment, dict) and _extract_dseq(deployment) == str(dseq):
            return client
    raise RuntimeError(
        f"deployment {dseq} was not readable under any of {len(keys)} configured Console wallets"
    )
