"""Native multi-key Console wallet discovery, ranking, and ownership routing."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from akash_lease_core import WalletCandidate, WalletPolicy, rank_wallets

from . import chain
from ._confidential import canonical_dseq, display
from .api import AkashConsoleAPI, ListingTruncated, _extract_dseq, _extract_lease_provider
from .chain import ChainCorroborationUnreachable
from .owner_lookup import (
    OWNER_LOOKUP_DEADLINE_SECONDS,
    OWNER_LOOKUP_UNREACHABLE,
    Deadline,
    OwnerLookupUnresolved,
    ask,
    lookup_owner,  # noqa: F401 - importable from wallet_pool since #374
    unresolved_verdict,
)


@dataclass(frozen=True)
class WalletClientSelection:
    client: AkashConsoleAPI
    account: str | None
    available_uact: int | None
    configured_keys: int
    distinct_accounts: int
    policy_version: str
    # openmix-wxs8: True when this selection was made quiet-aware (opt-in), so the caller
    # must re-check the chosen wallet (confirm_quiet_or_reselect) right before the create tx.
    contention_aware: bool = False


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


# ⛔ THE ST BRANCH WAS DEAD, AND `_CONTROL_RE` HID IT. In a raw string `\\\\` is
# two source backslashes, so the regex demanded ESC + TWO literal backslashes;
# the OSC String Terminator is ESC + ONE. That alternative therefore never
# matched anything. It looked like it was providing the guarantee while
# `_CONTROL_RE` — which runs second and covers ESC — quietly did the work, so
# every test passed. Reorder those two lines, or narrow `_CONTROL_RE` to spare a
# C1 range, and the hole opens with the suite still green. Hence the tests that
# exercise this pattern ALONE: a test that only calls `_one_line` cannot tell
# "the ANSI pattern matched" from "the control-character sweep cleaned up after
# it", which is exactly how this survived a mutation check.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
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


# ── openmix-wxs8: contention-aware wallet selection ─────────────────────────────────────
# A funded wallet is not a free one. Measured 2026-10-07: one funded Console wallet carried an
# owned CI runner controller that created a deployment every 1-4 minutes (15/15 slots), and the
# richest wallet held 66 deployments with fresh creates. Ranking on funding alone sends a fleet
# create straight into another creator's window. An OPT-IN multi-key create (deploy
# --quiet-wallet / AKASH_QUIET_WALLET=1) therefore also requires the wallet to be QUIET:
#   * no deployment CREATED within FLEET_QUIET_MINUTES (default 10). A dseq is the creation time
#     in epoch-ms, so any state counts — a NO_BID create that already closed is still a create;
#   * no deployment still BIDDING (active, no lease).
# ⚠ One Console read cannot see a create that opened and closed between two reads, and the
#   Console listing can lag the chain. Quiet is evidence, not a lock. The re-check right before
#   the create tx (confirm_quiet_or_reselect) narrows the window; a real lock both creators
#   honour, or a dedicated wallet, is the only closure (openmix-qctk).
# ⛔ OFF BY DEFAULT. The multi-key callers include CI runners (df-cicd akash-runner-ci,
#   df-akash-gate, akash-github-runner's pool handoff) that create on these wallets every few
#   minutes; default-on, CI would refuse because of its OWN creates. Funding-only ranking stays
#   the default for every caller.
# ⚠ The Console lists OLDEST-first (measured 2026-10-08), so the newest rows are on the last
#   page and the listing cannot stop early on age. It is capped at QUIET_LIST_MAX_PAGES; a
#   listing still going past the cap is TRUNCATED and the wallet counts as busy.
QUIET_MINUTES_ENV = "FLEET_QUIET_MINUTES"
QUIET_WALLET_ENV = "AKASH_QUIET_WALLET"
DEFAULT_QUIET_MINUTES = 10.0
QUIET_LIST_MAX_PAGES = 5
# A dseq is EITHER Console's creation epoch-ms (~1.79e12) OR, from the Akash CLI default, a
# BLOCK HEIGHT (~2.5e7). Read as ms, a height is always decades old, which would fail OPEN. A
# canonical dseq below this bound is a height, aged against the chain head at ~6 s/block; an
# unreadable head makes its age UNKNOWN, which is busy.
HEIGHT_DSEQ_BELOW = 10**12
SECONDS_PER_BLOCK = 6.0
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _quiet_minutes() -> float:
    raw = os.environ.get(QUIET_MINUTES_ENV, "").strip()
    if not raw:
        return DEFAULT_QUIET_MINUTES
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{QUIET_MINUTES_ENV} must be a number of minutes") from exc
    if not (value >= 0 and value < float("inf")):
        raise RuntimeError(f"{QUIET_MINUTES_ENV} must be a finite non-negative number of minutes")
    return value


def _quiet_from_env() -> bool:
    raw = os.environ.get(QUIET_WALLET_ENV, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    # A typo must not silently mean "off" for an opt-in safety check.
    raise RuntimeError(f"{QUIET_WALLET_ENV} must be one of 1/true/yes/on or 0/false/no/off")


def wallet_contention(
    client: AkashConsoleAPI,
    *,
    now_ms: float,
    quiet_minutes: float,
    keys: list[str] | None = None,
    height_reader: Callable[[], int | None] = chain.latest_height,
) -> list[str]:
    """Why this wallet is NOT quiet; empty means quiet. Fails CLOSED: an unreadable or
    truncated listing, an unparseable dseq, or a block-height dseq whose age cannot be read
    is a reason, never silence (an empty Console page is not proof of an idle wallet).
    The chain head is read at most once, and only when a block-height dseq is present."""

    try:
        rows = client.list_deployments(active_only=False, max_pages=QUIET_LIST_MAX_PAGES)
    except ListingTruncated:
        return [
            f"listing truncated after {QUIET_LIST_MAX_PAGES} page(s): its newest rows (last, "
            "the Console lists oldest-first) were not read"
        ]
    except Exception as exc:  # noqa: BLE001 — unreadable = not provably quiet
        text = _redact_keys(f"{type(exc).__name__}: {exc}", keys or [])
        return [f"listing unavailable ({_one_line(text, 160)})"]
    window_ms = quiet_minutes * 60_000
    reasons: list[str] = []
    head: list[int | None] = []  # read lazily, once
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        dseq = _extract_dseq(row)
        # canonical_dseq, not str.isdigit: isdigit accepts Unicode digits ("²") that int()
        # then rejects with an uncaught ValueError.
        if dseq is None or not canonical_dseq(dseq):
            reasons.append(f"unparseable dseq {display(dseq, 'dseq')}")
        else:
            value = int(dseq)
            age_ms: float | None
            if value < HEIGHT_DSEQ_BELOW:
                if not head:
                    head.append(height_reader())
                height = head[0]
                age_ms = None if height is None else (height - value) * SECONDS_PER_BLOCK * 1000
            else:
                age_ms = now_ms - value
            if age_ms is None:
                reasons.append(
                    f"age unknown for block-height dseq {display(dseq, 'dseq')} "
                    "(chain head unreadable)"
                )
                continue
            if age_ms < window_ms:
                reasons.append(
                    f"recent create {display(dseq, 'dseq')} {max(age_ms, 0) / 60_000:.1f}m ago"
                )
                continue
        dep = row.get("deployment", row)
        state = str(dep.get("state", "")) if isinstance(dep, dict) else ""
        if state == "active" and not _extract_lease_provider(row):
            reasons.append(f"bidding {display(dseq, 'dseq')} (active, no lease)")
    return reasons


def select_client_for_create(
    required_uact: int,
    *,
    client_factory: Callable[[str], AkashConsoleAPI] = AkashConsoleAPI,
    credit_reader: Callable[[str], int] = _default_credit_reader,
    quiet: bool | None = None,
    clock: Callable[[], float] = time.time,
) -> WalletClientSelection:
    """Choose a distinct account able to fund a new deployment.

    One key: that key, unchanged (no probe). Several keys: the richest FUNDED wallet. With
    ``quiet`` (or AKASH_QUIET_WALLET=1; None reads the env), only a funded wallet that is also
    QUIET (see the block above), refusing with a per-wallet reason when none qualifies."""

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

    contention_aware = _quiet_from_env() if quiet is None else quiet
    busy: dict[str, list[str]] = {}
    if contention_aware:
        now_ms = clock() * 1000
        quiet_minutes = _quiet_minutes()
        for item in candidates:
            if item.available_credit < required_uact:
                continue  # unfunded: rank_wallets refuses it anyway; no listing read needed
            reasons = wallet_contention(
                clients[item.candidate_id], now_ms=now_ms, quiet_minutes=quiet_minutes, keys=keys
            )
            if reasons:
                busy[item.candidate_id] = reasons
    ranked = [item for item in candidates if item.candidate_id not in busy]

    result = rank_wallets(
        ranked,
        WalletPolicy(required_credit=Decimal(required_uact), denom="uact"),
    )
    if result.selected is None and busy:
        lines = []
        for item in candidates:
            if item.candidate_id in busy:
                why = "; ".join(busy[item.candidate_id][:5])
                more = len(busy[item.candidate_id]) - 5
                why += f"; +{more} more" if more > 0 else ""
            else:
                why = f"unfunded ({int(item.available_credit)} < {required_uact} uact)"
            lines.append(f"{item.candidate_id} {display(item.account, 'address')}: {why}")
        lines.extend(failures)
        raise RuntimeError(
            f"no funded AND quiet Console wallet (quiet = no create within "
            f"{_quiet_minutes():g} min, nothing bidding; requested by --quiet-wallet / "
            f"{QUIET_WALLET_ENV}):\n  " + ";\n  ".join(lines)
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
        policy_version=result.policy_version + ("+quiet" if contention_aware else ""),
        contention_aware=contention_aware,
    )


def confirm_quiet_or_reselect(
    selection: WalletClientSelection,
    required_uact: int,
    *,
    client_factory: Callable[[str], AkashConsoleAPI] = AkashConsoleAPI,
    credit_reader: Callable[[str], int] = _default_credit_reader,
    clock: Callable[[], float] = time.time,
) -> WalletClientSelection:
    """The pre-create re-check (openmix-wxs8). Still quiet: the same selection. Busy since it
    was chosen: re-rank ONCE over the whole pool (which re-reads every wallet) and return that,
    or raise the per-wallet refusal. Never loops, never races."""

    if not getattr(selection, "contention_aware", False):
        return selection
    reasons = wallet_contention(
        selection.client,
        now_ms=clock() * 1000,
        quiet_minutes=_quiet_minutes(),
        keys=configured_api_keys(),
    )
    if not reasons:
        return selection
    return select_client_for_create(
        required_uact,
        client_factory=client_factory,
        credit_reader=credit_reader,
        quiet=True,
        clock=clock,
    )


def select_client_for_dseq(
    dseq: str,
    *,
    client_factory: Callable[[str], AkashConsoleAPI] = AkashConsoleAPI,
) -> AkashConsoleAPI:
    """Find the configured wallet that positively owns ``dseq`` by read-back.

    Every candidate key is read against the DSEQ, including the only
    candidate when the pool holds exactly one key. The previous one-key
    fastpath returned the client without calling ``get_deployment`` —
    the wallet then claimed positive ownership of a DSEQ it had never
    actually read, and the verify-closed chain verification would have
    proceeded against an unproven owner. With a single-key pool that
    misconfigures (or rotates the key out from under the deployment),
    every read in the close step would still pass through `account_address()`
    and produce a syntactically valid `akash1...` address for a wallet
    that has no business touching this lease.
    """

    keys = configured_api_keys()
    if not keys:
        raise RuntimeError("AKASH_API_KEY or AKASH_API_KEYS must be set")
    # ⛔ A READ THAT FAILED IN TRANSPORT IS NOT "THIS WALLET CANNOT READ IT" (#367). An API outage
    # used to skip every wallet and report the lease unreadable by all of them.
    # ⛔ AND THE PHASE IS WALL-CLOCK BOUNDED (#370): every wallet and every attempt shares one
    # Deadline; on expiry the typed UNREACHABLE records the attempts each wallet made, by
    # POSITION — key material never enters a message.
    unknown = False
    deadline = Deadline()
    attempts: list[int] = []
    for position, key in enumerate(keys):
        if deadline.expired:
            raise OwnerLookupUnresolved(
                "OWNER_LOOKUP_UNREACHABLE",
                f"deployment {dseq} could not be read: the owner-lookup wall-clock deadline "
                f"({OWNER_LOOKUP_DEADLINE_SECONDS:.0f}s) expired with {len(attempts)} of "
                f"{len(keys)} configured wallet(s) tried (attempts per wallet position: "
                f"{attempts}); ownership is unproven, not disproven",
            )
        client = client_factory(key)
        made = 0

        def _read(client: AkashConsoleAPI = client) -> Any:
            nonlocal made
            made += 1
            return client.get_deployment(str(dseq))

        # ⭐ PER-WALLET SHARE of the remaining budget (remaining / untried): a
        # dripping first wallet must not starve a later wallet that can read it.
        share = Deadline(deadline.remaining() / (len(keys) - position))
        kind, deployment = ask(_read, deadline=share)
        attempts.append(made)
        if kind == "unknown":
            unknown = True
        elif (
            kind == "answered"
            and isinstance(deployment, dict)
            and _extract_dseq(deployment) == str(dseq)
        ):
            return client
    if unknown:
        raise OwnerLookupUnresolved(
            "OWNER_LOOKUP_UNREACHABLE",
            f"deployment {dseq} could not be read: at least one configured Console wallet did "
            f"not answer within its retry budget (attempts per wallet position: {attempts}), "
            "so ownership is unproven, not disproven",
        )
    raise RuntimeError(
        f"deployment {dseq} was not readable under any of {len(keys)} configured Console wallets"
    )


def _raw_client_for_bound_owner(
    dseq: str,
    expected_owner: str,
    expected_group: str,
    *,
    client_factory: Callable[[str], AkashConsoleAPI] = AkashConsoleAPI,
) -> AkashConsoleAPI:
    """Select the private signer behind exact owner-bound containment.

    Console maps a credential to the create-time owner but is not a chain vote. Two
    registered trust paths must agree on the exact singleton ``gseq=1`` group. State is
    intentionally absent before destroy; it belongs to post-destroy closure verification.
    Callers receive either a read-only view or an evidence-gated closer, never this client.
    """
    if not re.fullmatch(r"[1-9][0-9]{0,19}", dseq) or int(dseq) > 2**64 - 1:
        raise RuntimeError("dseq must be canonical positive uint64 ASCII decimal")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", expected_group):
        raise RuntimeError("expected group must be a non-empty canonical group name")
    if not re.fullmatch(r"akash1[a-z0-9]{38,58}", expected_owner):
        raise RuntimeError("expected owner is not a canonical Akash account shape")
    keys = configured_api_keys()
    if not keys:
        raise RuntimeError("AKASH_API_KEY or AKASH_API_KEYS must be set")
    # ⛔ AN OUTAGE IS NOT "NOT THE OWNER" (#367). `except RuntimeError: continue` skipped every
    # credential during a Console connection-reset window and reported an owner mismatch. Only an
    # address MATCH selects a signer; a transport failure is UNKNOWN (retried within a budget).
    # ⛔ AND THE PHASE IS WALL-CLOCK BOUNDED (#370): one Deadline across every credential and
    # attempt; expiry raises the typed UNREACHABLE with attempts per credential POSITION.
    matching = []
    kinds: set[str] = set()
    deadline = Deadline()
    attempts: list[int] = []
    for position, key in enumerate(keys):
        if deadline.expired:
            raise OwnerLookupUnresolved(
                "OWNER_LOOKUP_UNREACHABLE",
                f"the owner-lookup wall-clock deadline ({OWNER_LOOKUP_DEADLINE_SECONDS:.0f}s) "
                f"expired with {len(attempts)} of {len(keys)} configured credential(s) tried "
                f"(attempts per credential position: {attempts}); ownership is unproven, "
                "not disproven",
            )
        client = client_factory(key)
        made = 0

        def _mint(client: AkashConsoleAPI = client) -> str:
            nonlocal made
            made += 1
            return client.account_address()

        # ⭐ PER-CREDENTIAL SHARE of the remaining budget (remaining / untried, this
        # key included): one dripping FIRST key must not starve a later key that
        # would answer. The share is itself a Deadline, so the step-wide ceiling
        # (OWNER_LOOKUP_DEADLINE_AT) still binds through min().
        share = Deadline(deadline.remaining() / (len(keys) - position))
        # ask() + the kind mapping lookup_owner() applies, with the attempt counted.
        kind, owner = ask(_mint, deadline=share)
        attempts.append(made)
        if kind == "answered":
            kind = "address"
        if kind == "address" and owner == expected_owner:
            matching.append(client)
            break
        kinds.add(kind)
    if not matching:
        verdict = unresolved_verdict(kinds)
        raise OwnerLookupUnresolved(
            verdict,
            "expected owner was not reported by any configured Console credential"
            if verdict == "NO_CREDENTIAL_MATCHES_OWNER"
            else "no configured Console credential was proven to be the expected owner "
            + (
                "(a credential lookup did not answer within its retry budget; attempts "
                f"per credential position: {attempts}; ownership is unproven, not disproven)"
                if verdict == "OWNER_LOOKUP_UNREACHABLE"
                else "(every credential lookup failed for a non-transport reason)"
            ),
        )
    try:
        names = chain.corroborated_deployment_group_names(expected_owner, dseq, expected_group)
    except ChainCorroborationUnreachable as unreachable:
        # ⛔ REPUBLISH AS A CONSUMER-RESOLVABLE VERDICT (#404). The chain module
        # deliberately does not import owner_lookup's verdict table (it would
        # couple a read-only chain helper to the Console-aware typology); the
        # translation lives at the destructive boundary instead, where the
        # caller already has the lexicon and the exit-code mapping. The
        # resolved owner at this point is "no configured Console credential was
        # proven to be the expected owner": the consensus half read, the other
        # half did not, and the gate has the receipt of which source failed.
        sources = ", ".join(f"{sid}: {reason}" for sid, reason in unreachable.unreachable.items())
        raise OwnerLookupUnresolved(
            OWNER_LOOKUP_UNREACHABLE,
            f"chain corroboration did not prove the expected group because one or "
            f"more registered sources did not answer within its budget ({sources})",
        ) from unreachable
    if names != [expected_group]:
        raise RuntimeError("owner-bound containment did not prove exact gseq=1 singleton")
    return matching[0]


@dataclass(frozen=True)
class BoundOwnerContainment:
    """Read-only owner result; it deliberately carries no Console client or close method."""

    owner: str

    def account_address(self) -> str:
        return self.owner


def select_client_for_bound_owner(
    dseq: str,
    expected_owner: str,
    expected_group: str,
    *,
    client_factory: Callable[[str], AkashConsoleAPI] = AkashConsoleAPI,
) -> BoundOwnerContainment:
    """Return read-only containment evidence without exposing a mutating client."""
    _raw_client_for_bound_owner(
        dseq, expected_owner, expected_group, client_factory=client_factory
    )
    return BoundOwnerContainment(expected_owner)


def authorize_client_for_bound_owner(
    dseq: str,
    expected_owner: str,
    expected_group: str,
    *,
    client_factory: Callable[[str], AkashConsoleAPI] = AkashConsoleAPI,
) -> tuple[_AuthorizedBoundOwnerCloser, dict]:
    """Return an evidence-gated closer, never the raw mutating client."""
    client = _raw_client_for_bound_owner(
        dseq, expected_owner, expected_group, client_factory=client_factory
    )
    evidence = chain.owner_close_evidence(expected_owner, dseq, expected_group)
    if evidence is None:
        raise RuntimeError("owner-bound containment is not fresh/finalized destructive authority")
    return _AuthorizedBoundOwnerCloser(client, evidence), evidence


def owner_evidence_is_unexpired(evidence: dict, *, now: datetime | None = None) -> bool:
    """The final local gate immediately before the mutating send."""
    try:
        expiry = datetime.fromisoformat(evidence["expires_at"])
    except (KeyError, TypeError, ValueError):
        return False
    current = now or datetime.now(timezone.utc)
    return expiry.tzinfo is not None and current.tzinfo is not None and expiry > current


@dataclass
class _AuthorizedBoundOwnerCloser:
    """The only owner-bound close handle; expiry is checked at its network boundary."""

    _client: AkashConsoleAPI
    evidence: dict
    _clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def close_deployment(self, dseq: str) -> dict:
        if str(self.evidence.get("dseq")) != str(dseq):
            raise RuntimeError("owner authority evidence is bound to a different dseq")
        if not owner_evidence_is_unexpired(self.evidence, now=self._clock()):
            raise RuntimeError("owner authority evidence expired at close send boundary")
        return self._client.close_deployment(dseq)
