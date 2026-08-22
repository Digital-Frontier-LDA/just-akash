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
from .api import AkashConsoleAPI


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
        except Exception:  # noqa: BLE001 — one broken wallet must not hide healthy siblings
            errors += 1

    result = rank_wallets(
        candidates,
        WalletPolicy(required_credit=Decimal(required_uact), denom="uact"),
    )
    if result.selected is None:
        if not candidates and errors:
            raise RuntimeError(f"could not measure any of {len(keys)} configured Console wallets")
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
        if isinstance(deployment, dict):
            return client
    raise RuntimeError(
        f"deployment {dseq} was not readable under any of {len(keys)} configured Console wallets"
    )
