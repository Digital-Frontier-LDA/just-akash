"""Every status list must derive from ``OrderStatus``. There is exactly one authority.

⛔ MEASURED 2026-08-29 — the same eight statuses were spelled in THREE places across two
repos, and one had already drifted:

    akash-lease-core  OrderStatus enum          8 members     <- the authority
    akash-lease-core  that enum's own docstring "SEVEN"       <- DRIFTED (says 7, has 8)
    just-akash        cli.py hardcoded tuple    8 strings     <- agreed by luck
    just-akash        summarise()               bare Counter  <- dropped absent statuses

The consequence was measurable in production. Audit run 33239504420 emitted, for three
owners of the same fleet:

    {"has_lease": 6, "excluded": 14}
    {"excluded": 36}      <- has_lease was 0, so the KEY VANISHED
    {}                    <- no decisions, or no read — indistinguishable

while the run reported "0 unreadable". The text output was correct the whole time,
because it iterated a hardcoded list under the comment "Print EVERY status, including the
zeros." ⇒ the HUMAN path carried the invariant and the MACHINE path, consumed by the
workflow with --json, did not.

⚠ ANTI-VACUITY. Asserting "summarise returns 8 keys" would pass on a function that
returns a hardcoded dict of 8 zeros and counts nothing. The limbs below pin the
RELATIONSHIP to the enum: every member present, counts still correct, and no second
spelling anywhere in the package.
"""

from __future__ import annotations

import re
from pathlib import Path

from akash_lease_core.orders import OrderStatus

from just_akash.unleased_orders import summarise

PKG = Path(__file__).resolve().parents[1] / "just_akash"


def test_summarise_reports_every_status_including_zeros():
    counts = summarise([])
    assert set(counts) == {s.value for s in OrderStatus}, (
        "summarise() must seed from OrderStatus so an absent status reads as 0, not as a "
        f"missing key; got {sorted(counts)}"
    )
    assert set(counts.values()) == {0}, "an empty decision set must be all zeros"


def test_summarise_still_counts(monkeypatch):
    """ANTI-VACUITY: seeding must not have replaced counting."""

    class _D:
        def __init__(self, s):
            self.status = s

    counts = summarise([_D(OrderStatus.EXCLUDED), _D(OrderStatus.EXCLUDED), _D(OrderStatus.HAS_LEASE)])
    assert counts["excluded"] == 2, f"counting broke: {counts}"
    assert counts["has_lease"] == 1
    assert counts["closeable"] == 0, "an unobserved status must be 0, not missing"
    assert set(counts) == {s.value for s in OrderStatus}


def test_no_second_spelling_of_the_status_list_in_this_package():
    """A retyped list is a place to drift. The enum is the only authority."""
    values = {s.value for s in OrderStatus}
    offenders = []
    for path in PKG.rglob("*.py"):
        src = path.read_text()
        for m in re.finditer(r"\(\s*((?:\s*\"[a-z_]+\"\s*,\s*){4,})\)", src):
            literals = set(re.findall(r'"([a-z_]+)"', m.group(1)))
            if len(literals & values) >= 4:
                offenders.append(f"{path.name}: {sorted(literals & values)}")
    assert not offenders, (
        "a status list is retyped here instead of derived from OrderStatus — that is the "
        f"drift this test exists to prevent: {offenders}"
    )
