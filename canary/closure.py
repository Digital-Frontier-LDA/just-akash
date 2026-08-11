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

    The dseq check is what keeps this honest: a block carries every chain's traffic, and
    our canary closes have been observed sharing a block with unrelated deployments of
    ours. Matching on message type alone would attribute whichever close landed first.
    """
    ident = msg.get("id")
    if not isinstance(ident, dict):
        return None
    if str(ident.get("dseq") or "") != str(dseq):
        return None
    name = _msg_name(msg.get("@type"))
    if name == _OWNER_CLOSE and str(ident.get("owner") or "") == owner:
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


def attribute(
    owner: str,
    dseq: str,
    *,
    fetch_info: Callable[[str, str], dict] = _fetch_info,
    fetch_block: Callable[[str], dict] = _fetch_block,
) -> str:
    """Who ended (owner, dseq). One of VERDICTS; never raises.

    Two LCD reads, and only when a lease replacement was actually observed — a handful of
    requests a day, not per collection.
    """
    if not owner or not dseq:
        return UNKNOWN
    try:
        info = fetch_info(owner, dseq)
    except Exception:  # noqa: BLE001 — an unreadable chain is `unknown`, not a crash
        return UNKNOWN
    if not isinstance(info, dict):
        return UNKNOWN

    _, settled_at = _escrow_state(info)
    block: dict | None = None
    if settled_at:
        try:
            fetched = fetch_block(settled_at)
            block = fetched if isinstance(fetched, dict) else None
        except Exception:  # noqa: BLE001 — fall through to the escrow-only verdict
            block = None
    try:
        return classify(info, block, owner, dseq)
    except Exception:  # noqa: BLE001 — a surprise shape is unknown, never a false blame
        return UNKNOWN
