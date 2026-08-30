"""`list` / `destroy-all` / `_resolve_deployment` / wallet-pool picker previously printed
clean output for an empty Console listing (#208). This is the helper that turns the
`⚠ DEGRADED` warning that `lease-status` already carries into a primitive the four CLI
siblings can call.

Why these tests exist as their own file:
  - `test_lease_status_corroboration.py` pins the underlying `corroborate_listing()`
    decision and the chain reader. The CLI sites are a different layer: they own
    *whether to call corroboration at all*. Without this layer's tests, an edit that
    removes the call (e.g. a refactor that bypasses `_warn_if_listing_degraded`) re-opens
    #208 silently — the `lease-status` test would still pass.
  - The four call sites (`cli.py:108, 145, 1054, 1779`) used to differ in three small
    ways (one is wallet-pool, one is JSON-or-table, one is destructive-by-default). The
    helper collapses those differences to a single decision: "did the listing come
    back empty? then warn." If a future caller wants different behaviour, they opt out
    by not calling the helper — that opt-out is what tests should pin, not paper over.
"""

from __future__ import annotations

from just_akash import cli


class _FakeClient:
    """Just enough of AkashConsoleAPI for the helper. The helper calls
    `account_address()` only when the listing is empty; if that raises, it degrades
    to the unconfirmed path (None chain count)."""

    def __init__(self, address: str = "akash1test", raise_addr: bool = False):
        """Hold the address to return (or raise the address call on demand)."""
        self._address = address
        self._raise = raise_addr

    def account_address(self) -> str:
        """Return the fake address, or simulate a primary-source failure."""
        if self._raise:
            raise RuntimeError("primary source unreadable")
        return self._address


def test_non_empty_listing_emits_no_warning(monkeypatch, capsys):
    """The corroboration check is only meaningful when the listing came back empty.
    A populated listing is its own evidence — no chain round-trip needed."""
    called = {"chain": 0}

    def _count(_addr):
        """Stub chain counter; record the call to assert no round-trip happens."""
        called["chain"] += 1
        return 7

    monkeypatch.setattr("just_akash.chain.active_deployment_count", _count)
    cli._warn_if_listing_degraded(_FakeClient(), [{"dseq": "1"}, {"dseq": "2"}])
    out = capsys.readouterr()
    assert "⚠ DEGRADED" not in out.err
    assert called["chain"] == 0, "non-empty listing must not pay for the chain round-trip"


def test_empty_listing_with_chain_zero_is_clean(monkeypatch, capsys):
    """Both sources agree the fleet is empty → no warning. This is the one case the
    pre-fix code happened to get right; pin it so a regression that 'always warns' is
    caught."""
    monkeypatch.setattr("just_akash.chain.active_deployment_count", lambda _a: 0)
    cli._warn_if_listing_degraded(_FakeClient(), [])
    out = capsys.readouterr()
    assert "⚠ DEGRADED" not in out.err, (
        f"a fleet confirmed empty by both sources must not warn (got: {out.err!r})"
    )


def test_empty_listing_with_chain_active_emits_mismatch_warning(monkeypatch, capsys):
    """The exact case #208 reports: Console returns []; chain reports 21 ACTIVE.
    The warning must surface BOTH numbers AND the owner address so an operator can
    act without re-running the command."""
    monkeypatch.setattr("just_akash.chain.active_deployment_count", lambda _a: 21)
    cli._warn_if_listing_degraded(_FakeClient("akash1cklqa…"), [])
    out = capsys.readouterr()
    assert "⚠ DEGRADED" in out.err
    assert "21 ACTIVE" in out.err
    assert "akash1cklqa" in out.err


def test_empty_listing_with_unreadable_chain_emits_unconfirmed_warning(monkeypatch, capsys):
    """A primary source we can't read is its own class — NOT a clean fleet, NOT a
    confirmed mismatch. Pinning this prevents the 'treat None like 0' regression."""
    monkeypatch.setattr("just_akash.chain.active_deployment_count", lambda _a: None)
    cli._warn_if_listing_degraded(_FakeClient(), [])
    out = capsys.readouterr()
    assert "⚠ DEGRADED" in out.err
    assert "UNCONFIRMED" in out.err


def test_unreadable_account_address_emits_unconfirmed_warning(monkeypatch, capsys):
    """If we can't even get the wallet address, we can't corroborate at all.
    The helper must still warn — just with the UNCONFIRMED framing rather than
    pretending the chain read returned a number."""
    monkeypatch.setattr("just_akash.chain.active_deployment_count", lambda _a: 99)
    cli._warn_if_listing_degraded(_FakeClient(raise_addr=True), [])
    out = capsys.readouterr()
    assert "⚠ DEGRADED" in out.err
    assert "UNCONFIRMED" in out.err


def test_decision_is_a_pure_function_of_three_args(monkeypatch, capsys):
    """The helper delegates the actual decision to `chain.corroborate_listing` —
    pinning the delegation here catches accidental re-implementation."""
    seen = {}

    def _fake_corroborate(listing_is_empty, chain_active, address=""):
        """Stub corroboration; record all 3 args to assert pure delegation."""
        seen["listing_is_empty"] = listing_is_empty
        seen["chain_active"] = chain_active
        seen["address"] = address
        return ["stub"]

    monkeypatch.setattr("just_akash.chain.active_deployment_count", lambda _a: 3)
    monkeypatch.setattr("just_akash.chain.corroborate_listing", _fake_corroborate)
    cli._warn_if_listing_degraded(_FakeClient("akash1abc"), [])
    assert seen == {"listing_is_empty": True, "chain_active": 3, "address": "akash1abc"}, seen
    out = capsys.readouterr()
    assert "stub" in out.err


# ── the four call sites stay wired ─────────────────────────────────────────


def test_four_call_sites_each_call_the_helper():
    """A regression that drops the call at any one site re-opens #208 for that command.
    The four sites are: `_resolve_deployment`, `_resolve_deployment_client` (wallet-pool),
    `list`, `destroy-all`. Search the file for `_warn_if_listing_degraded(client, ...)`
    and assert there are exactly four of them — the helper definition itself doesn't
    match because its first arg is the type-annotated `client` followed by a newline +
    docstring, not the runtime call pattern."""
    import re
    from pathlib import Path

    src = Path(cli.__file__).read_text()
    # Count runtime call sites — they look like `_warn_if_listing_degraded(client, …)`
    # at indented (non-def) positions. The `def` itself starts with `_warn_if_listing_degraded(`
    # too, but the character preceding the call name distinguishes them: a call site is
    # preceded by whitespace (indent); the def is preceded by `def `.
    calls = re.findall(r"(?<!def )_warn_if_listing_degraded\(client,", src)
    assert len(calls) == 4, (
        f"expected 4 call sites wired to the helper, found {len(calls)}: {calls}. "
        f"Either a site was dropped (re-opens #208) or a new site was added without "
        f"a corresponding test case."
    )
