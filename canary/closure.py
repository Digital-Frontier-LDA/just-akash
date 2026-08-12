#!/usr/bin/env python3
"""Attribute a CLOSED canary deployment to whoever actually closed it.

WHY THIS EXISTS
---------------
`akash_canary_lease_replacements_total` counts one thing: "the dseq I was watching
changed". That is a fact about OUR tracking, not about the provider. It goes up when a
provider evicts the deployment, and it goes up identically when one of our own jobs
closes it and `ensure.py` deploys a replacement. df-grafana's critical rule read that
number as "this provider does not keep customer deployments alive" and paged three
providers on 2026-08-11 — while the chain said every one of those closures was
`MsgCloseDeployment` signed by our own wallet, memo `akash console`.

Same defect as the 2026-08-07 canary incident, one metric along: a rule may not assert
a cause the collector cannot observe. The fix is not to soften the alert text, it is to
MEASURE the cause, so the critical rule can keep its teeth and point them at the right
target.

WHAT THE CHAIN ACTUALLY DISTINGUISHES
-------------------------------------
Closing a deployment and losing a lease are different messages with different signers:

  * ``MsgCloseDeployment``  — deployment module, signed by the OWNER. Only we can send
    this. It is the whole deployment going away because we asked.
  * ``MsgCloseBid``         — market module, carries a ``provider`` in its id and is
    signed by the PROVIDER. This is the provider dropping the lease, i.e. the actual
    "provider does not keep customer deployments alive" event.
  * no close message at all, escrow ``overdrawn`` — the deposit ran out and the chain
    settled it. Nobody closed anything; we underfunded it.

Matching is on the message NAME, never the versioned type URL. The type carries the
module version (`/akash.deployment.v1beta4.MsgCloseDeployment`), and pinning that would
turn the next chain upgrade into silent misattribution — every closure would fall through
to "unknown" while the rule that consumes it went quietly blind. The name has been stable
across every version this tool has seen.

NEVER RAISES. An unreadable LCD must degrade to "unknown" and be counted as such, because
the alternative — booking an unattributable closure as somebody's fault — is exactly the
bug this module was written to remove. `unknown` is a published counter, not a swallowed
error: df-grafana pages when it climbs, since a blind critical rule is the failure mode
that let the last incident run for a day.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from just_akash.chain import _lcd_get

# Verdicts. `open` is not an error: it means the deployment we stopped tracking is still
# alive on chain, which is the duplicate-canary case ensure.py warns about (two live
# canaries on one provider, the older one orphaned because no reaper collects it).
SELF = "self"
PROVIDER = "provider"
LAPSED = "lapsed"
OPEN = "open"
UNKNOWN = "unknown"

VERDICTS = (SELF, PROVIDER, LAPSED, OPEN, UNKNOWN)

# Message NAMES, deliberately unversioned — see the module docstring.
_OWNER_CLOSE = "MsgCloseDeployment"
_PROVIDER_CLOSE = "MsgCloseBid"


def _msg_name(type_url: object) -> str:
    """Bare message name from a protobuf type URL, version and package stripped."""
    return str(type_url).rsplit(".", 1)[-1] if isinstance(type_url, str) else ""


def _escrow_state(info: dict) -> tuple[str, str | None]:
    """(escrow_state, settled_at) tolerating both escrow_account shapes.

    publicnode returns ``escrow_account.state`` as a nested object carrying
    ``{state, funds, transferred, settled_at}``; other LCDs flatten it so ``state`` is
    the bare string and its siblings sit on the account. Reading only one shape would
    yield None on the other and send every closure to `unknown` — a whole-signal outage
    caused by an endpoint swap, which is the kind of thing that must not be possible.
    """
    acct = info.get("escrow_account")
    if not isinstance(acct, dict):
        return "", None
    inner = acct.get("state")
    if isinstance(inner, dict):
        state = inner.get("state")
        settled = inner.get("settled_at")
    else:
        state = inner
        settled = acct.get("settled_at")
    return (state or "").strip().lower(), (str(settled) if settled is not None else None)


def _closes_this_deployment(msg: dict, owner: str, dseq: str) -> str | None:
    """Verdict if `msg` closes exactly (owner, dseq), else None.

    BOTH halves of the identity are checked, once, before the message type is looked at.
    A deployment is identified by the PAIR (owner, dseq) — dseq is a per-owner sequence,
    not a global one — so matching a close on dseq alone would let another account's
    `MsgCloseBid` carrying the same dseq land on our provider's eviction counter. That is
    a fabricated provider fault, which is the one outcome this module exists to prevent.
    Raised independently by CodeRabbit and Copilot on #143, and they were right: the
    owner check had been applied to the owner-close branch only.

    Checking identity first also keeps this honest about blocks: a block carries the whole
    chain's traffic, and our canary closes have been observed sharing one with unrelated
    deployments of ours. Matching on message type alone would attribute whichever close
    landed first.
    """
    ident = msg.get("id")
    if not isinstance(ident, dict):
        return None
    if str(ident.get("dseq") or "") != str(dseq) or str(ident.get("owner") or "") != owner:
        return None
    name = _msg_name(msg.get("@type"))
    if name == _OWNER_CLOSE:
        return SELF
    if name == _PROVIDER_CLOSE and ident.get("provider"):
        return PROVIDER
    return None


def classify(info: dict, block: dict | None, owner: str, dseq: str) -> str:
    """Attribute one deployment's end, from its info document and its settlement block.

    PURE — no network, so the decision table is testable against fixtures rather than
    against a live chain whose history moves.

    Order is deliberate. An explicit close message is authoritative and is checked first;
    `overdrawn` only means "ran out" when nobody sent one, because a deployment closed by
    its owner also stops being funded and would otherwise read as lapsed.
    """
    deployment = info.get("deployment")
    state = ""
    if isinstance(deployment, dict):
        state = str(deployment.get("state") or "").strip().lower()
    if state and state != "closed":
        return OPEN

    for tx in (block or {}).get("txs") or []:
        if not isinstance(tx, dict):
            continue
        body = tx.get("body")
        for msg in (body.get("messages") if isinstance(body, dict) else None) or []:
            if not isinstance(msg, dict):
                continue
            verdict = _closes_this_deployment(msg, owner, dseq)
            if verdict:
                return verdict

    escrow_state, _ = _escrow_state(info)
    if escrow_state == "overdrawn":
        return LAPSED
    return UNKNOWN


def _fetch_info(owner: str, dseq: str) -> dict:
    return _lcd_get(f"/akash/deployment/v1beta4/deployments/info?id.owner={owner}&id.dseq={dseq}")


def _fetch_block(height: str) -> dict:
    return _lcd_get(f"/cosmos/tx/v1beta1/txs/block/{height}")


def _block_time(block: dict | None) -> datetime | None:
    """The settling block's timestamp, or None if the shape is not what we expect."""
    if not isinstance(block, dict):
        return None
    raw = ((block.get("block") or {}).get("header") or {}).get("time")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # Cosmos emits RFC3339 with a trailing Z and often nanosecond precision, which
        # fromisoformat rejects before 3.11 and still dislikes at >6 digits.
        text = raw.replace("Z", "+00:00")
        if "." in text:
            head, _, tail = text.partition(".")
            frac, sign, off = tail.partition("+") if "+" in tail else tail.partition("-")
            text = f"{head}.{frac[:6]}{sign}{off}" if sign else f"{head}.{frac[:6]}"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def lifetime_hours(dseq: str, block: dict | None) -> float | None:
    """EXACT lease lifetime in hours, or None if either end is unreadable.

    WHY THIS IS NOT MEASURED FROM akash_canary_uptime_seconds, which looks like the obvious
    source. That gauge is reported from INSIDE the lease and only reaches us on a successful
    scrape, so the last value before a lease dies is a LOWER BOUND sampled at the collection
    cadence — 30 minutes on a good day, and observed at 60-90 when GitHub's scheduler slips.
    Reading it as the lifetime is wrong by up to an hour and a half.

    That error is not academic. On 2026-08-12 two such lower bounds (13.79h and 13.69h) were
    read as failure times, their closeness taken as evidence of a fixed timer, and a whole
    hypothesis built on it. The chain then gave the real numbers for the same population --
    11.91h, 12.06h, 14.23h -- a 2.3h spread with no timer in it at all.

    Both ends of this measurement come from the chain and neither depends on our cadence:
      * the START is encoded in the dseq itself, which the Console API mints as epoch
        MILLISECONDS (a live deployment reads 1786498654528 -> 2026-08-12T05:37:34Z);
      * the END is the header time of the block the escrow settled in.
    """
    if not dseq:
        return None
    end = _block_time(block)
    if end is None:
        return None
    try:
        start = datetime.fromtimestamp(int(dseq) / 1000, timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    hours = (end - start).total_seconds() / 3600.0
    # A negative or absurd span means the dseq is not the millisecond timestamp this assumes
    # (a chain upgrade, or a deployment created by something that mints dseq differently).
    # Publish nothing rather than a number that would be read as a measurement.
    if hours < 0 or hours > 24 * 365:
        return None
    return hours


def attribute_detailed(
    owner: str,
    dseq: str,
    *,
    fetch_info: Callable[[str, str], dict] = _fetch_info,
    fetch_block: Callable[[str], dict] = _fetch_block,
) -> tuple[str, float | None]:
    """(who ended it, how long it lived in hours). Never raises.

    Both come out of the SAME two LCD reads `attribute()` already performed, so measuring the
    lifetime costs no extra requests -- the settling block was being fetched anyway to read
    who signed the close.
    """
    if not owner or not dseq:
        return UNKNOWN, None
    try:
        info = fetch_info(owner, dseq)
    except Exception:  # noqa: BLE001 — an unreadable chain is `unknown`, not a crash
        return UNKNOWN, None
    if not isinstance(info, dict):
        return UNKNOWN, None

    _, settled_at = _escrow_state(info)
    block: dict | None = None
    if settled_at:
        try:
            fetched = fetch_block(settled_at)
            block = fetched if isinstance(fetched, dict) else None
        except Exception:  # noqa: BLE001 — fall through to the escrow-only verdict
            block = None
    lived = lifetime_hours(str(dseq), block)
    try:
        return classify(info, block, owner, dseq), lived
    except Exception:  # noqa: BLE001 — a surprise shape is unknown, never a false blame
        return UNKNOWN, lived


def attribute(
    owner: str,
    dseq: str,
    *,
    fetch_info: Callable[[str, str], dict] = _fetch_info,
    fetch_block: Callable[[str], dict] = _fetch_block,
) -> str:
    """Who ended (owner, dseq). One of VERDICTS; never raises.

    Two LCD reads, and only when a lease replacement was actually observed — a handful of
    requests a day, not per collection. Kept as the cause-only façade over
    attribute_detailed() so callers that do not want the lifetime are unaffected.
    """
    return attribute_detailed(owner, dseq, fetch_info=fetch_info, fetch_block=fetch_block)[0]
