#!/usr/bin/env python3
"""Expand `just-akash list --json` into the per-deployment DETAILS the canary needs.

WHY THIS MODULE EXISTS AT ALL. `list_deployments()` returns summary rows. The two things
that identify a canary — the service set (`leases[].status.services`) and the provider it
landed on (`leases[].id.provider`) — live on the DETAIL, which only `get_deployment(dseq)`
returns. Every other consumer in this repo already knows that: cleanup_stale and the smoke
sweep both list, then fetch a detail per dseq. canary/ensure.py originally did not, and read
`leases` off the summary rows, where that key is simply absent. The visible symptom was every
provider reporting `uri=(none)` while a healthy canary was serving metrics.

COMPLETENESS IS PART OF THE OUTPUT, not an afterthought. A detail fetch that fails leaves us
unable to tell "no canary here" from "could not look". Those two must never collapse into one
answer, because the first prompts a DEPLOY. Getting it wrong means opening a second lease on a
provider that already has one — and a canary matches no reaper (its service is named `canary`,
which no stale rule classifies), so that duplicate is not swept up later. It bills until a
human notices. So this writes an explicit `complete` flag and ensure.py refuses to report
anything missing when it is false. A late canary is recoverable; a leaked lease is not.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from just_akash.api import AkashConsoleAPI, _extract_dseq


def fetch(client, listing: list) -> tuple[list[dict], list[str]]:
    """(details, errors) for every active deployment in `listing`.

    Errors are COLLECTED, not raised. One unreadable deployment should not lose the
    readings for the others — but it must still be visible to the caller, which is why
    they come back rather than going to a log nobody reads.
    """
    details: list[dict] = []
    errors: list[str] = []
    for row in listing:
        if not isinstance(row, dict):
            # An error, NOT a skip — same rule as a row with no dseq. A dropped row makes a
            # live canary invisible, and invisible reads as "no canary", which spends money.
            errors.append(f"row is {type(row).__name__}, not an object")
            continue
        dseq = _extract_dseq(row)
        if not dseq:
            # No dseq means we cannot fetch it AND cannot name it. Count it as an error:
            # it is a row we failed to account for, and silently skipping rows is how a
            # live canary becomes invisible.
            errors.append(f"row without a dseq: {sorted(row)[:6]}")
            continue
        try:
            detail = client.get_deployment(dseq)
        except Exception as exc:  # noqa: BLE001 — collected above, never fatal here
            errors.append(f"{dseq}: {type(exc).__name__}: {exc}")
            continue
        if isinstance(detail, dict) and detail:
            details.append(detail)
        else:
            errors.append(f"{dseq}: empty detail response")
    return details, errors


def parse_listing(payload) -> list:
    """Rows out of a `list --json` payload, RAISING on an envelope we do not recognise.

    The distinction that matters is between an empty account and an unreadable answer. A bare
    `[]` is a real, recognisable "you have no deployments". A dict carrying none of the known
    keys is not an answer at all — and silently reading it as zero rows would produce a
    details document claiming `complete: true` with nothing in it, which tells ensure.py that
    every provider has lost its canary and authorises a deploy onto all three. The leases
    already there would keep billing, unswept, because a canary matches no stale rule.

    This is the same trade api.list_deployments makes, and for the same measured reason: its
    old fail-open `return []` had two downstream repos delete nothing while reporting
    "Nothing to do" against a wallet holding 15-27 deployments. Loud beats plausible whenever
    the next step spends money.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("deployments", "items", "results", "data"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
    raise ValueError(
        f"unrecognised `list --json` envelope ({type(payload).__name__}"
        + (f", keys {sorted(payload)[:6]}" if isinstance(payload, dict) else "")
        + "). Refusing to read this as an empty account: that would look like every "
        "provider having lost its canary and authorise a duplicate deploy onto each."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listing", required=True, help="`just-akash list --json` output")
    ap.add_argument("--out", required=True, help="Where to write the details document")
    a = ap.parse_args()

    # READING the file is inside the guard too, not just parsing it. A truncated or empty
    # listing.json raises from read_text/json.loads, and the workflow step runs under
    # `set -euo pipefail`, so a traceback here fails the step and the collection never runs —
    # losing the reachability reading on exactly the run where something is already wrong.
    # `git show BRANCH:file > out` creating a zero-byte file on a first run is the same trap
    # that killed the first live dispatch (see canary/_state.py).
    try:
        rows = parse_listing(json.loads(pathlib.Path(a.listing).read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        # Publish the incomplete verdict rather than exiting non-zero. `complete: false` is
        # exactly the right statement here, it stops any deploy, and it leaves the collector
        # free to run — an unreadable listing does not stop the canaries answering, and
        # losing that reading too would compound one blind spot into two.
        print(f"::warning::{exc}", file=sys.stderr)
        pathlib.Path(a.out).write_text(
            json.dumps({"complete": False, "deployments": []}, indent=2) + "\n",
            encoding="utf-8",
        )
        return 0

    # A named cause, not a KeyError traceback. This runs in CI where the key comes from a
    # SOPS-decrypted env file, so the realistic failure is that the decrypt step did not run
    # or exported a different name — and "KeyError: 'AKASH_API_KEY'" in a stack trace sends
    # people looking at this file instead of at that step.
    api_key = os.environ.get("AKASH_API_KEY")
    if not api_key:
        print(
            "Error: AKASH_API_KEY is not set. It is decrypted from secrets/ci.sops.env by "
            "the SOPS step in .github/workflows/provider-canary.yml; check that step ran.",
            file=sys.stderr,
        )
        return 2
    client = AkashConsoleAPI(api_key)
    details, errors = fetch(client, rows)

    for e in errors:
        print(f"  detail ERROR {e}", file=sys.stderr, flush=True)
    print(f"details: {len(details)} read, {len(errors)} failed, from {len(rows)} row(s)")

    pathlib.Path(a.out).write_text(
        json.dumps({"complete": not errors, "deployments": details}, indent=2) + "\n",
        encoding="utf-8",
    )
    # Exit 0 even with errors: the collector still has work to do, and ensure.py already
    # declines to deploy on an incomplete picture. Failing the job here would ALSO lose the
    # reachability measurement, which is the one thing that still works when the API is sick.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
