"""Capacity probe — "will an N×GPU deployment actually place right now?" answered by a
real bid, not the provider's ``/status`` inventory.

A provider's aggregate ``/status`` collapses **rented / available / provisioning** into
one number, and a node mid-provision drops out of ``allocatable`` entirely (reads as
*non-existent*), so it cannot be trusted to say what will place. The chain's order/bid
market is the ground truth: create a throwaway deployment (an *order*) requesting the GPU
shape, see which providers bid, then **close it without ever creating a lease** — no
container runs, only the order's minimal deposit is briefly escrowed and refunded on
close.

This codifies the manual "deploy an alpine + GPU and see if it bids" probe that kept
getting rewritten as throwaway scripts, with the deterministic order-cleanup the ad-hoc
version kept getting wrong.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

# Minimal GPU order. alpine + `sleep` so that IF a lease were ever created the container
# is trivial — but the probe closes the order before accepting a bid, so nothing runs.
# Pricing is a generous per-block ceiling in uact (the Console credit denom) so that
# *price* is never the reason a provider declines — we want a pure capacity signal.
_PROBE_SDL = """\
version: "2.0"
services:
  probe:
    image: alpine:3.19
    command: ["sh", "-c", "sleep 30"]
    expose:
      - port: 80
        as: 80
        to: [{{ global: true }}]
profiles:
  compute:
    probe:
      resources:
        cpu: {{ units: 1 }}
        memory: {{ size: 1Gi }}
        gpu:
          units: {count}
          attributes: {{ vendor: {{ nvidia: {models} }} }}
        storage: [{{ size: 2Gi }}]
  placement:
    dcloud:
      pricing:
        probe: {{ denom: uact, amount: 1000000 }}
deployment:
  probe:
    dcloud: {{ profile: probe, count: 1 }}
"""


def build_probe_sdl(gpu_count: int, gpu_model: str | None) -> str:
    """Render a minimal GPU order SDL. ``gpu_model`` empty/None → any NVIDIA GPU
    (``nvidia: []``); otherwise pin the model (e.g. ``v100``, ``rtx4000ada``)."""
    if gpu_count < 1:
        raise ValueError("gpu_count must be >= 1")
    models = f"[{{ model: {gpu_model} }}]" if gpu_model else "[]"
    return _PROBE_SDL.format(count=gpu_count, models=models)


def capacity_probe(
    client: Any,
    gpu_count: int,
    gpu_model: str | None = None,
    *,
    wait_s: int = 45,
    poll_s: int = 5,
    provider: str | None = None,
    deposit: float = 0.5,
) -> dict[str, Any]:
    """Probe whether ``gpu_count``×``gpu_model`` GPUs will place on the market right now.

    Creates a throwaway order, polls for open bids up to ``wait_s`` seconds, then
    **always closes the order** (never creates a lease). Returns
    ``{placeable, bidders, gpu_count, gpu_model, dseq, waited_s}`` where ``bidders`` is a
    list of ``{provider, price_amount, price_denom}`` for each distinct provider that bid.
    ``provider`` filters the reported bidders to a single provider address (the order is
    still open to all — the filter is applied to the results).

    The order is closed in a ``finally`` so a mid-probe error never leaks an open order
    (the escrow-leak the ad-hoc scripts kept producing).
    """
    res = probe_order_sdl(
        client,
        build_probe_sdl(gpu_count, gpu_model),
        wait_s=wait_s,
        poll_s=poll_s,
        provider=provider,
        deposit=deposit,
    )
    return {"gpu_count": gpu_count, "gpu_model": gpu_model or "any-nvidia", **res}


def probe_order_sdl(
    client: Any,
    sdl: str,
    *,
    wait_s: int = 45,
    poll_s: int = 5,
    provider: str | None = None,
    deposit: float = 0.5,
) -> dict[str, Any]:
    """Order-only probe for an arbitrary SDL: create, watch bids, ALWAYS close.

    The shape-agnostic core of :func:`capacity_probe`, extracted so the
    bid-health probe (``bid_probe.py``) asks its question through the same
    create/poll/close-in-``finally`` path rather than a second copy of it. The
    cleanup discipline here is the whole reason this module exists — the ad-hoc
    scripts it replaced leaked escrow every time they raised mid-probe — so
    there must be exactly one implementation of it.

    Returns ``{placeable, bidders, dseq, waited_s}``. Never creates a lease.
    """
    from .api import _extract_bid_price, _extract_dseq, _extract_provider
    from .deploy import _is_open_bid

    # poll_s must advance the clock. At 0 the loop sleeps for nothing, `waited`
    # never reaches wait_s, and a genuinely un-bid order spins forever holding
    # an open deployment — the escrow leak this module exists to prevent.
    if poll_s <= 0 and wait_s > 0:
        raise ValueError("poll_s must be > 0 when wait_s > 0")

    dep = client.create_deployment(sdl, deposit=deposit)
    dseq = _extract_dseq(dep)
    bidders: list[dict[str, Any]] = []
    waited = 0
    try:
        if not dseq:
            raise RuntimeError(f"capacity probe: order was not created (response: {dep!r})")
        seen: set[str] = set()
        while True:
            bids = client.get_bids(dseq)
            for b in bids if isinstance(bids, list) else []:
                if not (isinstance(b, dict) and _is_open_bid(b)):
                    continue
                prov = _extract_provider(b)
                if not prov or prov in seen or (provider and prov != provider):
                    continue
                amount, denom = _extract_bid_price(b)
                seen.add(prov)
                bidders.append({"provider": prov, "price_amount": amount, "price_denom": denom})
            if bidders or waited >= wait_s:
                break
            time.sleep(poll_s)
            waited += poll_s
    finally:
        # Best-effort cleanup: always close the order, never let a close error mask the
        # probe result (or leak the order — this is the escrow-leak the ad-hoc scripts hit).
        if dseq:
            with contextlib.suppress(Exception):
                client.close_deployment(dseq)
    return {
        "placeable": bool(bidders),
        "bidders": bidders,
        "dseq": dseq,
        "waited_s": waited,
    }
