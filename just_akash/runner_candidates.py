"""Order the provider candidates for an ephemeral GitHub-runner lease.

Moved here from a CI repo's scripts/ directory: which providers may host a runner is
Akash knowledge, not workflow glue, and keeping it in one consumer meant the other
consumer had no concept of `runner_deny` at all.

WHY THIS IS NOT JUST A LIST

blazing passes providers as two flat comma-separated secrets and tries them in
order. That loses two facts that Blazing-Back learned by losing CI runs to them,
and it is the reason a failure gets misread as "the marketplace had no capacity".

  1. A provider can be HEALTHY, BID, WIN — and never schedule the runner pod.
     Recorded for three providers at BOTH 16Gi/30Gi and 32Gi/30Gi, so it is not a
     memory-size problem; ephemeral storage and port-80 global ingress are the
     live hypotheses. A lease like that is worse than no bid: it consumes the
     attempt, holds escrow, and stalls until the timeout. One such provider was
     traced to an 1800s stall.

  2. just-akash picks the CHEAPEST bid within a tier. So an unproven provider
     that undercuts the proven one CAPTURES the runner and kills it — measured
     at ~24 uact vs ~27 uact. Ordering by price alone actively selects for the
     broken host. Proven hosts must be a strictly earlier tier, not merely
     "preferred".

Hence three markers, taken from the fleet's own curation file:

    runner_host: true   PROVEN to bring the runner online (measured, ~30s)
    runner_deny: true   leases but never schedules the runner pod — NEVER try it
    ci_only:     true   reputable third-party; fine for a throwaway CI runner,
                        never for customer workloads

WHAT THIS DELIBERATELY DOES NOT CONTAIN

No addresses. The caller supplies them. A shared default provider list is how one
fleet's operational trust decision silently becomes everyone's, and the markers
are only meaningful against a specific fleet's measurements.

POLARITY

An unparseable providers document is a hard error, not an empty list. Returning
[] would fall through to just-akash's built-in defaults and quietly ignore every
runner_deny the operator recorded — re-selecting the exact providers known to
strand the runner.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Providers with no explicit ordering sort after those that have one.
DEFAULT_PRIORITY = 10_000


class ProviderSpecError(ValueError):
    """The providers document is unusable. Never silently degrades to a default."""


def parse_providers(raw: str) -> list[dict]:
    """Accept the JSON array form, or a bare comma/newline-separated address list.

    The flat form exists so an existing caller passing AKASH_PROVIDERS keeps
    working; every entry is then unmarked, which means unproven — NOT denied.
    """
    text = (raw or "").strip()
    if not text:
        return []

    if text.startswith("["):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ProviderSpecError(f"providers is not valid JSON: {e}") from e
        if not isinstance(doc, list):
            raise ProviderSpecError("providers JSON must be an array")
        return [_normalise(entry, i) for i, entry in enumerate(doc)]

    out = []
    for tok in text.replace(",", "\n").split("\n"):
        tok = tok.strip()
        if tok:
            out.append(_normalise({"address": tok}, len(out)))
    return out


def _normalise(entry, index: int) -> dict:
    if isinstance(entry, str):
        entry = {"address": entry}
    if not isinstance(entry, dict):
        raise ProviderSpecError(f"providers[{index}] must be an object or an address string")

    addr = entry.get("address")
    if not isinstance(addr, str) or not addr.startswith("akash1"):
        # A typo'd address silently never bids, which reads as a market outage.
        raise ProviderSpecError(f"providers[{index}].address is not an akash1… address: {addr!r}")

    prio = entry.get("failover_priority", DEFAULT_PRIORITY)
    try:
        prio = int(prio)
    except (TypeError, ValueError):
        raise ProviderSpecError(
            f"providers[{index}].failover_priority must be an integer, got {prio!r}"
        ) from None

    if entry.get("runner_host") and entry.get("runner_deny"):
        # Contradictory markers must not be resolved silently — whichever way we
        # guessed would either strand the runner or discard a proven host.
        raise ProviderSpecError(f"providers[{index}] ({addr}) is both runner_host and runner_deny")

    return {
        "address": addr,
        "runner_host": bool(entry.get("runner_host")),
        "runner_deny": bool(entry.get("runner_deny")),
        "ci_only": bool(entry.get("ci_only")),
        "failover_priority": prio,
        "name": entry.get("name") or addr,
    }


def select_candidates(providers: list[dict]) -> tuple[list[dict], list[dict]]:
    """(ordered candidates, excluded). Proven hosts strictly first.

    Ordering is host-first, THEN priority — not priority with a host tiebreak.
    just-akash takes the cheapest bid inside whatever set it is given, so mixing
    an unproven provider into the same tier as a proven one lets price decide,
    and price is exactly what selects the broken host.
    """
    # `.get` rather than `[...]`: this must be total over any provider mapping,
    # normalised or not. An ABSENT marker means "not marked" — unproven, but not
    # denied — which is the same semantic the flat address list carries.
    denied = [p for p in providers if p.get("runner_deny")]
    usable = [p for p in providers if not p.get("runner_deny")]
    ordered = sorted(
        usable,
        key=lambda p: (
            not p.get("runner_host"),
            int(p.get("failover_priority", DEFAULT_PRIORITY)),
            p.get("address", ""),
        ),
    )
    return ordered, denied


def proven_host_count(providers: list[dict]) -> int:
    """How many providers are PROVEN runner hosts, ignoring denied ones.

    This is the readiness number: with fewer than 3, a single silent provider
    takes the whole pool down, which is what sends CI onto billed runners.
    """
    return sum(1 for p in providers if p.get("runner_host") and not p.get("runner_deny"))


def render_report(ordered: list[dict], denied: list[dict], min_hosts: int = 3) -> list[str]:
    lines: list[str] = []
    hosts = [p for p in ordered if p.get("runner_host")]
    lines.append(
        f"candidates: {len(ordered)} usable ({len(hosts)} proven runner_host), "
        f"{len(denied)} excluded by runner_deny"
    )
    for p in ordered:
        mark = "HOST " if p.get("runner_host") else "     "
        extra = " ci_only" if p.get("ci_only") else ""
        lines.append(
            f"  {mark} {p.get('failover_priority', DEFAULT_PRIORITY):>5}  "
            f"{p.get('name') or p.get('address')}{extra}"
        )
    for p in denied:
        lines.append(
            f"  DENY        {p.get('name') or p.get('address')} — leases but does not "
            "schedule the runner pod"
        )

    if not ordered:
        lines.append(
            "::error title=No eligible runner provider::Every supplied provider is "
            "runner_deny. This is a configuration result, NOT a market outage — "
            "no order will be created and no provider will be asked to bid."
        )
    elif len(hosts) == 0:
        lines.append(
            "::warning title=No PROVEN runner host::None of the candidates is "
            "runner_host. The lease may succeed and still never schedule the runner "
            "pod — the failure that reads as a 30-minute stall."
        )
    elif len(hosts) < min_hosts:
        lines.append(
            f"::warning title=Runner pool is one provider deep::{len(hosts)} proven "
            f"runner_host(s), want >= {min_hosts}. While this holds, a single silent "
            "provider takes the whole pool down and CI falls back to billed runners."
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--providers", default=os.environ.get("AKASH_PROVIDERS_SPEC", ""))
    ap.add_argument("--min-hosts", type=int, default=3)
    ap.add_argument("--github-output", action="store_true")
    args = ap.parse_args(argv)

    try:
        providers = parse_providers(args.providers)
    except ProviderSpecError as e:
        # Hard error: falling through to just-akash's defaults would ignore every
        # runner_deny the operator recorded.
        print(f"::error title=Bad provider spec::{e}", file=sys.stderr)
        return 2

    ordered, denied = select_candidates(providers)
    for line in render_report(ordered, denied, args.min_hosts):
        print(line)

    out = os.environ.get("GITHUB_OUTPUT")
    if args.github_output and out:
        with open(out, "a") as fh:
            fh.write("candidates=" + ",".join(p["address"] for p in ordered) + "\n")
            fh.write(f"proven_hosts={proven_host_count(providers)}\n")
            fh.write(f"denied={len(denied)}\n")

    # Only an empty candidate list is fatal — there is nothing to try.
    return 1 if providers and not ordered else 0


if __name__ == "__main__":
    raise SystemExit(main())
