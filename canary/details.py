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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listing", required=True, help="`just-akash list --json` output")
    ap.add_argument("--out", required=True, help="Where to write the details document")
    a = ap.parse_args()

    payload = json.loads(pathlib.Path(a.listing).read_text(encoding="utf-8"))
    # Same envelope tolerance as ensure._as_list: `list --json` has returned both a bare
    # list and an object wrapping one, and this must not care which.
    rows = payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        for key in ("deployments", "items", "results", "data"):
            v = payload.get(key)
            if isinstance(v, list):
                rows = v
                break

    client = AkashConsoleAPI(os.environ["AKASH_API_KEY"])
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
