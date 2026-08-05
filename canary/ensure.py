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
would also match the smoke probes (same base images) and matching on SDL is not something
`list` reports. The tag is also what keeps `cleanup-stale` and the smoke startup sweep from
reaping this lease — see the warning in sdl/canary.yaml.
"""

from __future__ import annotations

import argparse
import json
import pathlib

TAG_PREFIX = "canary-"


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
    ap.add_argument("--providers", required=True, help="Comma-separated provider names")
    ap.add_argument("--targets", required=True, help="Targets file to read+write")
    ap.add_argument("--missing-out", help="Write providers needing a deploy, one per line")
    a = ap.parse_args()

    listing = json.loads(pathlib.Path(a.listing).read_text(encoding="utf-8"))
    tp = pathlib.Path(a.targets)
    prev = json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else {}
    providers = [p.strip() for p in a.providers.split(",") if p.strip()]

    targets, missing = plan(listing, providers, prev)
    tp.write_text(json.dumps(targets, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if a.missing_out:
        pathlib.Path(a.missing_out).write_text("\n".join(missing) + ("\n" if missing else ""),
                                               encoding="utf-8")
    for p in providers:
        t = targets.get(p, {})
        print(f"{p:14} dseq={t.get('dseq','-'):>12} uri={t.get('uri','') or '(none)'}"
              f"{'  NEEDS DEPLOY' if p in missing else ''}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
