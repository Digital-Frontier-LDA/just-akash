#!/usr/bin/env python3
"""Publish orphaned-escrow findings as METRICS, so a human can be paged about them.

WHY THIS EXISTS
---------------
`just-akash orphan-scan` already finds deployments that hold escrow and satisfy nothing, and
it prints them. A printed line is not a signal: df-grafana scrapes a `.prom` file off the
telemetry branch and cannot alert on a workflow log, so today an orphan is discovered only if
somebody happens to open the run. Two onidc deployments held ~$4.00 for five days on exactly
that basis.

The fleet quorum settled the shape of this (2026-08-11, unanimous across five reviewers):

  * reclamation stays ALERT-ONLY, never automatic. A wrong close destroys a live canary, and
    nothing here closes anything -- this module only measures.
  * the per-orphan series is labelled by `dseq`, NOT by provider. `orphan_detect.py`'s
    DeploymentVerdict carries a dseq and has no provider field at all, so a provider label
    would need attribution wiring that does not exist. dseq is already there, is unique per
    orphan, is the actionable handle (`tx deployment close --dseq X`), and SELF-EXPIRES: a
    reaped orphan simply stops appearing, so no stale series lingers at a nonzero value. That
    last property is why three reviewers' demand for an explicit zero-emit guard evaporated.
  * the SCALAR is what pages; the per-dseq gauge is what the human reads afterwards.

A DEGRADED SCAN MUST NOT READ AS ZERO ORPHANS
---------------------------------------------
`FleetReport.is_degraded` means the fleet could not be fully enumerated -- an endpoint refused,
a page was truncated. The orphan count from such a scan is a floor, not a measurement, and
publishing `akash_canary_orphans_total 0` from it would be a false all-clear of exactly the
kind this repo keeps finding: a green number standing in for an unasked question.

So `akash_canary_orphans_total` is published ONLY when the scan was complete. When it is
degraded the series is ABSENT and `akash_canary_orphan_scan_degraded` is 1, which df-grafana
can alert on with absent()/== 1. Absence is loud here by design; a zero would be quiet.

Usage:  just-akash orphan-scan --json | python3 -m canary.orphans >> canary-metrics.prom
"""

from __future__ import annotations

import json
import sys

_ESCAPE = str.maketrans({"\\": "\\\\", '"': '\\"', "\n": "\\n"})


def _label(value: str) -> str:
    """Escape a label value for the exposition format."""
    return str(value).translate(_ESCAPE)


def _uact(value: object) -> int:
    """Escrow as an int, or 0 if the field is not a number.

    Used by BOTH the per-orphan series and the total. Guarding only the per-orphan parse was
    the first version of this file, and its own self-test crashed the renderer on a
    non-numeric escrow -- which in production would have taken out the whole exposition and,
    with it, every orphan signal. A malformed field must cost one number, not the file.
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def render(report: dict) -> str:
    """Prometheus exposition for one orphan-scan report.

    Pure: takes the parsed `orphan-scan --json` document and returns text. No chain, no
    filesystem -- so the decision about what to publish is testable against fixtures rather
    than against a live wallet whose contents move.
    """
    lines: list[str] = []
    add = lines.append

    degraded = bool(report.get("degraded"))
    reasons = report.get("degraded_reasons") or []

    add(
        "# HELP akash_canary_orphan_scan_degraded 1 when the fleet could not be fully "
        "enumerated, so the orphan count below is a FLOOR and not a measurement. The count "
        "is withheld entirely in that case -- a zero from a half-read fleet is a false "
        "all-clear."
    )
    add("# TYPE akash_canary_orphan_scan_degraded gauge")
    add(f"akash_canary_orphan_scan_degraded {1 if degraded else 0}")

    if degraded:
        # Deliberately no orphans_total here. See the module docstring: an absent series is
        # loud (df-grafana can alert on absent()), a zero is quiet and wrong.
        add(
            "# NOTE: akash_canary_orphans_total withheld -- scan degraded: "
            + _label("; ".join(str(r) for r in reasons)[:200])
        )
        return "\n".join(lines) + "\n"

    deployments = report.get("deployments") or []
    orphans = [
        d
        for d in deployments
        if isinstance(d, dict) and str(d.get("classification", "")).upper() == "ORPHANED"
    ]

    add(
        "# HELP akash_canary_orphans_total Deployments holding escrow that satisfy nothing. "
        "This is the paging signal. Reclamation is deliberately NOT automatic -- a wrong "
        "close destroys a live canary -- so this exists to put a human on it."
    )
    add("# TYPE akash_canary_orphans_total gauge")
    add(f"akash_canary_orphans_total {len(orphans)}")

    add(
        "# HELP akash_canary_orphan_escrow_uact Escrow held by each orphaned deployment, in "
        "uact (micro-USD: 2000000 = $2.00). Labelled by dseq because that is the handle "
        "needed to close it and because a reaped orphan stops appearing, so no stale series "
        "is left behind at a nonzero value."
    )
    add("# TYPE akash_canary_orphan_escrow_uact gauge")
    for d in sorted(orphans, key=lambda x: str(x.get("dseq", ""))):
        dseq = str(d.get("dseq", "")).strip()
        if not dseq:
            # A verdict with no dseq cannot be acted on and must not become an unlabelled
            # series that silently aggregates with the next one.
            continue
        escrow = _uact(d.get("escrow_uact"))
        add(f'akash_canary_orphan_escrow_uact{{dseq="{_label(dseq)}"}} {escrow}')

    add(
        "# HELP akash_canary_orphan_escrow_uact_total Total escrow held by orphans, in uact. "
        "The per-dseq series above is for the human; this is for a spend threshold."
    )
    add("# TYPE akash_canary_orphan_escrow_uact_total gauge")
    total = report.get("orphaned_escrow_uact")
    if not isinstance(total, int):
        total = sum(_uact(d.get("escrow_uact")) for d in orphans)
    add(f"akash_canary_orphan_escrow_uact_total {total}")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    raw = sys.stdin.read()
    try:
        report = json.loads(raw)
    except ValueError as e:
        # Fail LOUD. Emitting an empty exposition here would silently replace the orphan
        # signal with nothing, which reads downstream as "no orphans".
        print(
            f"canary.orphans: stdin was not the JSON from `orphan-scan --json` ({e})",
            file=sys.stderr,
        )
        print(f"  first 200 bytes: {raw[:200]!r}", file=sys.stderr)
        return 1
    if not isinstance(report, dict):
        print(
            f"canary.orphans: expected a JSON object, got {type(report).__name__}", file=sys.stderr
        )
        return 1
    sys.stdout.write(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
