#!/usr/bin/env python3
"""Decide which providers still have a live canary, and where to reach it.

SEPARATION OF CONCERNS, ON PURPOSE. This module only READS and DECIDES; it never
deploys. The money-spending action (`just-akash deploy`) stays in the workflow where it is
visible in the run log and bounded by an explicit cap. A bug in here can therefore mislabel
a canary as missing, but it cannot silently open leases in a loop.

Input is the DETAILS document from canary/details.py plus a provider->wallet map. Output is
the targets file the collector consumes, and the list of providers needing a deploy.

HOW A CANARY IS IDENTIFIED, AND WHY NOT BY TAG. A deployment is our canary for provider P
when its service set is exactly {canary} and its lease is held by P. Both facts come off the
deployment itself, so they are readable from any machine at any time.

This started out matching `just-akash tag` names (`canary-<provider>`) and that was simply
broken. Tags are stored in `.tags.json` in the working copy — local state, never on chain and
never in the API. A GitHub runner is destroyed after every job, so the tag written by the
deploy step no longer existed by the next run, `plan()` matched nothing, and all three
providers reported NEEDS DEPLOY forever. Under CANARY_AUTODEPLOY that is not a cosmetic bug:
it opens three fresh leases every thirty minutes, none of which any reaper collects.

The service set is the right key because it is what the reapers themselves classify on
({probe} after 1h, {backtest} after 48h, {} left alone). Matching on the same property the
sweepers use means "what survives" and "what we recognise" cannot drift apart. Matching on
image would collide with the smoke probes, which share base images.
"""

from __future__ import annotations

import argparse
import json
import pathlib

from canary._state import load_json_mapping

# The sole service in sdl/canary.yaml. Deliberately a name no other SDL in this repo uses,
# so the service set alone identifies a canary and no reaper's stale rule matches it.
CANARY_SERVICE = "canary"

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
    "akash1aaul837r7en7hpk9wv2svg8u78fdq0t2j2e82z": "alphavps",  # pragma: allowlist secret
    "akash1hgulk6aekakqzc0v6wukrd3dy9n90f5gkl4ezk": "onidc",  # pragma: allowlist secret
    "akash1z9nr23cgweu45g2jktfx95v7g2xp8qlsa3ys2x": "hetzner_hel",  # pragma: allowlist secret
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


def _as_list(payload) -> list:
    """Deployments out of a details document, a bare list, or a list-style envelope."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("deployments", "items", "results", "data"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
    return []


def is_complete(payload) -> bool:
    """Whether the details document claims to have read EVERY deployment.

    Absent flag means complete, so a hand-written fixture or a bare list still works. Only
    an explicit `false` — which canary/details.py writes when a fetch failed — turns off
    deploying.
    """
    return not (isinstance(payload, dict) and payload.get("complete") is False)


def service_names(dep: dict) -> set[str]:
    """Service names the provider reports running, from leases[].status.services.

    Same traversal as smoke_providers._deployment_service_names, guarding every hop for the
    same reason: a malformed provider response must read as "no services", not raise and
    take the run down. Empty means CANNOT CLASSIFY (down, or still starting) — never "not a
    canary". Callers must keep those apart; see plan().
    """
    names: set[str] = set()
    for lease in dep.get("leases") or []:
        if not isinstance(lease, dict):
            continue
        status = lease.get("status")
        services = status.get("services") if isinstance(status, dict) else None
        if isinstance(services, dict):
            names.update(str(k) for k in services)
    return names


def lease_provider(dep: dict) -> str | None:
    """Provider address holding the lease, from leases[].id.provider.

    Mirrors just_akash.api._extract_lease_provider rather than importing it, because this
    module is deliberately importable without the API package — plan() is pure, and its
    tests run on a fixture, not a client.
    """
    for lease in dep.get("leases") or []:
        if not isinstance(lease, dict):
            continue
        lease_id = lease.get("id")
        if isinstance(lease_id, dict):
            provider = lease_id.get("provider")
            if isinstance(provider, str) and provider:
                return provider
    return None


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


def _dseq_of(dep: dict) -> str:
    return str(dep.get("dseq") or dep.get("id") or "")


def _newest(deps: list[dict]) -> dict:
    """The most recently created of several deployments.

    dseqs are minted as millisecond epochs (see smoke_providers._probe_age_seconds), so the
    largest is the newest. A non-numeric dseq sorts oldest rather than raising — this runs on
    whatever the API returned, and a surprise here must not abort the run.
    """

    def key(d: dict) -> int:
        try:
            return int(_dseq_of(d))
        except (TypeError, ValueError):
            return -1

    return max(deps, key=key)


def plan(
    details, providers: list[tuple[str, str]], prev_targets: dict | None = None
) -> tuple[dict, list, list]:
    """Return (targets, providers_needing_deploy, notes).

    `providers` is [(name, address)]. Matching is on the DEPLOYMENT, never on local state:
    service set exactly {canary}, lease held by that provider's address.

    A provider whose canary is live but has no ingress yet keeps its PREVIOUS uri rather
    than being blanked: ingress routes can take a moment to propagate after a redeploy,
    and dropping the uri would make the collector report the provider unreachable — a
    self-inflicted outage in the signal.

    THREE OUTCOMES, NOT TWO. "has a canary", "has none", and "cannot tell" are distinct, and
    only the middle one may trigger a deploy:

      * A deployment whose provider is ours but whose service set is EMPTY is unclassifiable
        — the provider has not reported services yet, which is exactly how a canary looks in
        the minutes after it is created. Reading that as "no canary" is how you deploy a
        second one on top of the first.
      * An incomplete details document (a failed API read) is unclassifiable for the same
        reason, everywhere at once.

    Both suppress the deploy for the providers they touch and say so in `notes`. The cost of
    being wrong is asymmetric: waiting 30 minutes for the next run is free, while a duplicate
    lease matches no reaper and bills until a human spots it.
    """
    prev = prev_targets or {}
    notes: list[str] = []
    complete = is_complete(details)
    if not complete:
        notes.append(
            "details are INCOMPLETE (a deployment could not be read) - not reporting any "
            "provider as missing, because 'could not look' is not 'nothing there'."
        )

    name_of_addr = {addr: name for name, addr in providers}
    found: dict[str, list[dict]] = {}
    unclassifiable: set[str] = set()
    for dep in _as_list(details):
        if not isinstance(dep, dict) or not _is_live(dep):
            continue
        provider_name = name_of_addr.get(lease_provider(dep) or "")
        if provider_name is None:
            continue  # someone else's lease, or no lease yet — not ours to reason about
        services = service_names(dep)
        if services == {CANARY_SERVICE}:
            found.setdefault(provider_name, []).append(dep)
        elif not services:
            unclassifiable.add(provider_name)

    targets: dict = {}
    missing: list[str] = []
    for provider, _addr in providers:
        deps = found.get(provider) or []
        if len(deps) > 1:
            # Not self-correcting: canaries match no stale rule, so the loser of this race
            # runs until someone closes it by hand. Name the dseqs so that is a one-liner.
            notes.append(
                f"{provider}: {len(deps)} live canaries "
                f"({', '.join(sorted(_dseq_of(d) for d in deps))}) - watching the newest; "
                "close the others by hand, no reaper will."
            )
        if not deps:
            if provider in unclassifiable:
                notes.append(
                    f"{provider}: a lease here reports no services yet - cannot tell a "
                    "starting canary from none, so NOT deploying this run."
                )
            elif complete:
                missing.append(provider)
            # Keep the last known target so the collector still records the outage
            # against the right endpoint instead of losing the provider entirely.
            if provider in prev:
                targets[provider] = prev[provider]
            continue
        dep = _newest(deps)
        uri = ingress_uri(dep) or (prev.get(provider, {}) or {}).get("uri", "")
        targets[provider] = {"uri": uri, "dseq": _dseq_of(dep)}
    return targets, missing, notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--details",
        required=True,
        help="canary/details.py output. NOT `list --json`: the summary rows carry no "
        "leases, so neither the service set nor the provider is readable from them.",
    )
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

    details = json.loads(pathlib.Path(a.details).read_text(encoding="utf-8"))
    tp = pathlib.Path(a.targets)
    prev = load_json_mapping(tp)

    pairs = providers_from_env(a.akash_providers)
    names = [n for n, _ in pairs]
    addr_of = dict(pairs)

    targets, missing, notes = plan(details, pairs, prev)
    for n in notes:
        print(f"  note: {n}", flush=True)
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
