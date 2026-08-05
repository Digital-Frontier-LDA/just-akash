#!/usr/bin/env python3
"""Decide which providers still have a live canary, and where to reach it.

SEPARATION OF CONCERNS, ON PURPOSE. This module only READS and DECIDES; it never
deploys. The money-spending action (`just-akash deploy`) stays in the workflow where it is
visible in the run log and bounded by an explicit cap. A bug in here can therefore mislabel
a canary as missing, but it cannot silently open leases in a loop.

Input is the JSON from `just-akash list --json` plus a provider->wallet map. Output is the
targets file the collector consumes, and the list of providers needing a deploy.

WHY MATCH ON A TAG AND NOT ON THE SDL OR IMAGE. `tag` gives each canary a stable name
(`canary-<provider>`) that survives redeploys and is visible in `list`. Matching on image
would also match the smoke probes (same base images), and matching on SDL is not something
`list` reports.

The tag does NOT protect the lease from the reapers, and an earlier version of this
docstring wrongly said it did. `cleanup_stale` and the smoke sweep classify by SERVICE SET
({probe} after 1h, {backtest} after 48h, {} left alone) — never by tag. The canary survives
because its service is named `canary` and matches no stale rule. See sdl/canary.yaml.
"""

from __future__ import annotations

import argparse
import json
import pathlib

TAG_PREFIX = "canary-"

# Provider ADDRESS -> friendly name. These are the same three addresses already carried in
# AKASH_PROVIDERS (secrets/ci.sops.env, and .env.example), the same ones df-grafana's
# akash-external-smoke rules label_replace into cluster names, and the same ones the
# autobidder's per-cluster dashboards pin. They are public provider addresses, not our
# wallet, which is why they are committed rather than injected.
#
# WHY MAP HERE INSTEAD OF ASKING FOR A NEW CONFIG VARIABLE. An earlier revision took a
# CANARY_PROVIDER_WALLETS variable listing exactly this. That was duplicated config — a
# second copy of AKASH_PROVIDERS that could silently drift out of step with the providers
# the smoke test actually exercises, so the canary and the smoke could end up measuring
# different fleets while both looked configured. It was also badly named: it reads like
# OUR wallets, when every entry is a PROVIDER's address. We spend from AKASH_API_KEY, one
# Console-API wallet, the same one the smoke test has always deployed with.
PROVIDER_NAMES = {
    "akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z": "alphavps",
    "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk": "onidc",
    "akash1z9nr23cgweu45g2jktfx95v7g2xp8qlsa3ys2x": "hetzner_hel",
}


def name_for(address: str) -> str:
    """Friendly name for a provider address, falling back to a truncated address.

    The fallback matters: adding a fourth provider to AKASH_PROVIDERS must not silently
    drop it from the canary. It gets an ugly label until someone adds it above, which is a
    visible prompt rather than a silent omission.

    PREFIX **AND** SUFFIX, not a plain truncation. Every Akash address starts `akash1`, so
    address[:12] leaves only six distinguishing characters — two unknown providers could
    collide on one label. That would be silently destructive rather than merely ugly:
    `plan()` and the targets file are keyed by this name, so two providers would fold into
    one entry and one of them would go unwatched. That is precisely the outcome the
    fallback exists to prevent. The trailing characters of a bech32 address include its
    checksum, so prefix+suffix is effectively unique.
    """
    if address in PROVIDER_NAMES:
        return PROVIDER_NAMES[address]
    return f"{address[:10]}..{address[-6:]}" if len(address) > 18 else address


def providers_from_env(akash_providers: str) -> list[tuple[str, str]]:
    """[(name, address)] from an AKASH_PROVIDERS-style comma-separated list.

    DE-DUPLICATED, on both the address and the resolved name, because a duplicate here is
    not harmless. `missing` would carry the provider twice, the deploy loop would run twice
    for it, and we would open TWO leases on one provider — paying twice to watch the same
    thing, and publishing two entries that overwrite each other. AKASH_PROVIDERS is a
    hand-maintained comma-separated string in a SOPS file, so a repeated address is an
    ordinary copy-paste slip rather than an exotic input.

    Name-level de-duplication is the belt to that braces: two distinct addresses resolving
    to one label would silently fold together downstream, since plan() and the targets file
    are keyed by name. First occurrence wins so the order stays predictable.
    """
    out: list[tuple[str, str]] = []
    seen_addr: set[str] = set()
    seen_name: set[str] = set()
    for addr in (a.strip() for a in akash_providers.split(",")):
        if not addr or addr in seen_addr:
            continue
        name = name_for(addr)
        if name in seen_name:
            continue
        seen_addr.add(addr)
        seen_name.add(name)
        out.append((name, addr))
    return out


def canary_tag(provider: str) -> str:
    return f"{TAG_PREFIX}{provider}"


def _as_list(payload) -> list:
    """`list --json` may return a bare list or an object wrapping one."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("deployments", "items", "results", "data"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
    return []


def _is_live(dep: dict) -> bool:
    """Treat only an explicitly-closed deployment as dead.

    Fail OPEN on an unknown/missing state: mislabelling a live canary as missing would
    make the workflow deploy a SECOND one on that provider, and two canaries racing to
    report the same provider is worse than a late redeploy.
    """
    for key in ("state", "status"):
        v = dep.get(key)
        if isinstance(v, str) and v.strip().lower() in ("closed", "destroyed", "inactive"):
            return False
    esc = dep.get("escrow")
    if isinstance(esc, dict):
        st = esc.get("state")
        if isinstance(st, str) and st.strip().lower() == "closed":
            return False
    return True


def ingress_uri(dep: dict) -> str | None:
    """Provider-assigned ingress host, same traversal as smoke_providers._ingress_uri:
    leases[].status.services[].uris[0]. Bare host[:port], served over plain http."""
    for lease in dep.get("leases") or []:
        if not isinstance(lease, dict):
            continue
        status = lease.get("status")
        services = status.get("services") if isinstance(status, dict) else None
        for svc in services.values() if isinstance(services, dict) else []:
            uris = svc.get("uris") if isinstance(svc, dict) else None
            if isinstance(uris, list) and uris:
                return str(uris[0])
    return None


def plan(listing, providers: list[str], prev_targets: dict | None = None) -> tuple[dict, list]:
    """Return (targets, providers_needing_deploy).

    A provider whose canary is live but has no ingress yet keeps its PREVIOUS uri rather
    than being blanked: ingress routes can take a moment to propagate after a redeploy,
    and dropping the uri would make the collector report the provider unreachable — a
    self-inflicted outage in the signal.
    """
    prev = prev_targets or {}
    by_tag: dict[str, dict] = {}
    for dep in _as_list(listing):
        if not isinstance(dep, dict):
            continue
        name = dep.get("name") or dep.get("tag") or dep.get("label")
        if isinstance(name, str) and name.startswith(TAG_PREFIX) and _is_live(dep):
            by_tag[name] = dep

    targets: dict = {}
    missing: list[str] = []
    for provider in providers:
        dep = by_tag.get(canary_tag(provider))
        if dep is None:
            missing.append(provider)
            # Keep the last known target so the collector still records the outage
            # against the right endpoint instead of losing the provider entirely.
            if provider in prev:
                targets[provider] = prev[provider]
            continue
        dseq = str(dep.get("dseq") or dep.get("id") or "")
        uri = ingress_uri(dep) or (prev.get(provider, {}) or {}).get("uri", "")
        targets[provider] = {"uri": uri, "dseq": dseq}
    return targets, missing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listing", required=True, help="`just-akash list --json` output file")
    ap.add_argument(
        "--akash-providers",
        required=True,
        help="AKASH_PROVIDERS value: comma-separated provider ADDRESSES. Deliberately the "
        "same variable the smoke test uses, so the two cannot drift apart.",
    )
    ap.add_argument("--targets", required=True, help="Targets file to read+write")
    ap.add_argument(
        "--missing-out",
        help="Write providers needing a deploy as 'name<TAB>address', one per line",
    )
    a = ap.parse_args()

    listing = json.loads(pathlib.Path(a.listing).read_text(encoding="utf-8"))
    tp = pathlib.Path(a.targets)
    prev = json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else {}

    pairs = providers_from_env(a.akash_providers)
    names = [n for n, _ in pairs]
    addr_of = dict(pairs)

    targets, missing = plan(listing, names, prev)
    tp.write_text(json.dumps(targets, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if a.missing_out:
        lines = [f"{n}\t{addr_of[n]}" for n in missing]
        pathlib.Path(a.missing_out).write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )
    for n in names:
        t = targets.get(n, {})
        print(
            f"{n:14} dseq={t.get('dseq', '-'):>12} uri={t.get('uri', '') or '(none)'}"
            f"{'  NEEDS DEPLOY' if n in missing else ''}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
