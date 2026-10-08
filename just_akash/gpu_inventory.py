"""V100 inventory — who on the WHOLE network can place which V100 shape, by real bids.

openmix-67w3 AC3. ``bid_probe`` pins every order to OUR providers (an unpinned
order loses the 20-bid race to everyone else), so it cannot answer the
question this does: one UNPINNED order per shape — units x VRAM x form
factor — open to every provider, waiting the full window for every bidder,
then closed. It never creates a lease; the bid set is the datum, and the only
cost is each order's deposit, escrowed and refunded on close.

Why bids and not ``/status``: a provider's aggregate inventory cannot say how
the GPUs sit on nodes, and Akash places one replica on ONE node. A bid on
``units: N`` means some node of that provider has N free GPUs of that shape, so
the largest N a provider bids on is its free GPUs per node — the number the
Console API's ``/v1/gpu`` totals do not expose (2026-10-08: 14 V100s on the
whole network, per-node layout unknown).

Shapes go to 8 because the operator asked whether 8x V100 nodes exist for
GLM / DeepSeek; a units:8 bid is the only proof one does.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from .capacity import probe_order_sdl


@dataclass(frozen=True)
class Shape:
    units: int
    ram: str  # Akash gpu attribute, e.g. "16Gi"
    interface: str  # Akash gpu attribute: "sxm" | "pcie"


V100_SHAPES: tuple[Shape, ...] = tuple(
    Shape(units, ram, interface)
    for interface in ("sxm", "pcie")
    for ram in ("16Gi", "32Gi")
    for units in (1, 2, 4, 8)
)

# No placement attributes, on purpose: this survey must be open to every
# provider. Pricing is a generous ceiling in uact so price never decides a bid.
_SDL = """\
---
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
          units: {units}
          attributes:
            vendor:
              nvidia:
                - {{ model: v100, ram: {ram}, interface: {interface} }}
        storage: [{{ size: 2Gi }}]
  placement:
    dcloud:
      pricing:
        probe: {{ denom: uact, amount: 1000000 }}
deployment:
  probe:
    dcloud: {{ profile: probe, count: 1 }}
"""


def build_inventory_sdl(shape: Shape) -> str:
    if shape.units < 1:
        raise ValueError("units must be >= 1")
    return _SDL.format(units=shape.units, ram=shape.ram, interface=shape.interface)


def run_inventory(
    client: Any,
    *,
    shapes: tuple[Shape, ...] | list[Shape] = V100_SHAPES,
    wait_s: int = 60,
    poll_s: int = 5,
    deposit: float = 0.5,
) -> list[dict[str, Any]]:
    """One order per shape, every bidder kept, every order closed (in ``probe_order_sdl``).

    A failed order is recorded with its error, never raised: one bad shape must
    not abort the survey and leave the rest unasked.
    """
    records: list[dict[str, Any]] = []
    for idx, shape in enumerate(shapes):
        rec: dict[str, Any] = {
            "units": shape.units,
            "ram": shape.ram,
            "interface": shape.interface,
            "ts": time.time(),
        }
        try:
            res = probe_order_sdl(
                client,
                build_inventory_sdl(shape),
                wait_s=wait_s,
                poll_s=poll_s,
                deposit=deposit,
                collect_all=True,
            )
            rec.update(dseq=res.get("dseq"), bidders=res.get("bidders") or [], error="")
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            rec.update(dseq=None, bidders=[], error=f"{type(exc).__name__}: {exc}"[:300])
        records.append(rec)
        print(
            f"  [{idx + 1}/{len(shapes)}] {shape.units}x v100 {shape.ram} {shape.interface}: "
            f"{len(rec['bidders'])} bidder(s){' ERROR ' + rec['error'] if rec['error'] else ''}"
        )
    return records


def _price(amount: Any) -> float | None:
    try:
        return float(amount)
    except (TypeError, ValueError):
        return None


def summarize(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per (provider, VRAM, form factor): the largest units it bid on = free GPUs per node."""
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for rec in records:
        for b in rec.get("bidders") or []:
            key = (b["provider"], rec["ram"], rec["interface"])
            cur = best.get(key)
            if cur is None or rec["units"] > cur["max_gpus_per_node"]:
                best[key] = {
                    "provider": b["provider"],
                    "ram": rec["ram"],
                    "interface": rec["interface"],
                    "max_gpus_per_node": rec["units"],
                    "bid_uact_per_block": _price(b.get("price_amount")),
                    "bid_denom": b.get("price_denom"),
                    "dseq": rec.get("dseq"),
                }
    return sorted(best.values(), key=lambda r: (r["provider"], r["ram"], r["interface"]))


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Unpinned V100 inventory survey (orders only).")
    ap.add_argument("--wait", type=int, default=60, help="seconds to collect bids per order")
    ap.add_argument("--deposit", type=float, default=0.5, help="order deposit (ACT)")
    ap.add_argument("--json-out", default="", help="write records + summary here")
    ap.add_argument("--dry-run", action="store_true", help="print the shapes and exit")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.dry_run:
        for s in V100_SHAPES:
            print(f"{s.units}x v100 {s.ram} {s.interface}")
        return 0
    api_key = os.environ.get("AKASH_API_KEY", "").strip()
    if not api_key:
        print("ERROR: AKASH_API_KEY is not set", file=sys.stderr)
        return 1

    from .api import AkashConsoleAPI

    records = run_inventory(AkashConsoleAPI(api_key), wait_s=args.wait, deposit=args.deposit)
    rows = summarize(records)
    print(f"\nINVENTORY: {len(rows)} provider x shape rows")
    for r in rows:
        print(
            f"  {r['provider']} v100 {r['ram']} {r['interface']}: "
            f"{r['max_gpus_per_node']} GPU/node, "
            f"bid {r['bid_uact_per_block']} {r['bid_denom']}/block"
        )
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"records": records, "summary": rows}, fh, indent=2)
        print(f"wrote {args.json_out}")
    # A failed ORDER is data; only a survey where nothing could be asked is a failed run.
    return 1 if records and all(r["error"] for r in records) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
