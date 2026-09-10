"""Unit tests for just_akash/_lease_verification.py — the shared closure contract.

Director's review blocker on 2026-09-09 required: 'Tests must cover real CLI
args/default URL and owning-account call-site, not only fake verify command
stdout.' The shell harness tests the workflow's close step end-to-end with a
fake `just-akash` transport; THIS file exercises the verifier contract
directly with stubbed chain endpoints so the wiring (default endpoints,
hostname dedup, owner-scoped pagination, two-endpoint agreement) cannot
silently drift.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "_lease_verification", ROOT / "just_akash" / "_lease_verification.py"
)
assert SPEC is not None
verifier = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(verifier)


OWNER = "akash1" + "a" * 38
DSEQ = "1788952936722"


def _closed_deployment():
    return {
        "deployment": {"id": {"owner": OWNER, "dseq": DSEQ}, "state": "closed"},
        "escrow_account": {
            "id": {"scope": "deployment", "xid": f"{OWNER}/{DSEQ}"},
            "state": {"owner": OWNER, "state": "closed"},
        },
    }


def _terminal_lease(state: str = "closed") -> dict:
    """One complete Akash leases/list page with one terminal-state lease."""
    return {
        "leases": [
            {
                "lease": {
                    "id": {
                        "owner": OWNER,
                        "dseq": DSEQ,
                        "gseq": 1,
                        "oseq": 1,
                        "bseq": 0,
                        "provider": "akashprovider1xyz",
                    },
                    "state": state,
                    "price": {"denom": "uakt", "amount": "1000"},
                }
            }
        ],
        "pagination": {"next_key": ""},
    }


def _active_lease() -> dict:
    return _terminal_lease(state="active")


PROVIDER_2 = "akashprovider2abc"
PROVIDER_3 = "akashprovider3def"


def _lease(provider: str, state: str, *, gseq: int = 1, oseq: int = 1, bseq: int = 0) -> dict:
    """A single Akash lease row, ready for embedding in a `leases` page."""
    return {
        "lease": {
            "id": {
                "owner": OWNER,
                "dseq": DSEQ,
                "gseq": gseq,
                "oseq": oseq,
                "bseq": bseq,
                "provider": provider,
            },
            "state": state,
            "price": {"denom": "uakt", "amount": "1000"},
        }
    }


def _multi_lease_page(leases: list[dict], *, next_key: str = "") -> dict:
    """A leases/list page containing N rows, one per provider (or state shape)."""
    return {"leases": leases, "pagination": {"next_key": next_key}}


def _stub_get(responses: dict):
    """Return a `get` callable that maps URL prefixes to canned responses."""

    def _get(url: str):
        if "/deployments/info?" in url:
            return _closed_deployment()
        for prefix, doc in responses.items():
            if prefix in url:
                return doc
        raise AssertionError(f"unexpected URL: {url}")

    return _get


# ── DEFAULT ENDPOINTS (issue 1) ─────────────────────────────────────────────


def test_default_endpoints_are_pol_kachu_and_cosmos_directory_akash():
    """Reuse the proven Akash REST defaults from the blazing shared verifier."""
    assert verifier.DEFAULT_ENDPOINTS == (
        "https://akash-api.polkachu.com",
        "https://rest.cosmos.directory/akash",
    )


# ── HOSTNAME DEDUP (issue 6) ────────────────────────────────────────────────


def test_same_hostname_different_port_is_one_source():
    """`akash-api.polkachu.com` and `akash-api.polkachu.com:443` are one origin.

    A dedup that compares full netloc would treat them as two sources, and a
    test that pinned two agreeing chain populations on this case would silently
    pass while the production code consults one endpoint twice — exactly the
    population-agreement defect this verifier exists to refuse.
    """
    responses = {
        "akash-api.polkachu.com": _terminal_lease(),
        "rest.cosmos.directory/akash": _terminal_lease(),
    }
    snap, sources = verifier.consensus(
        DSEQ,
        OWNER,
        [
            "https://akash-api.polkachu.com",
            "https://akash-api.polkachu.com:443",
            "https://rest.cosmos.directory/akash",
        ],
        _stub_get(responses),
    )
    # ⇒ The two polkachu variants collapse to one source; cosmos.directory is
    # the second. Sources is therefore 2, not 3, and the populations agree.
    assert snap is not None, "two distinct origins consulted, populations agree"
    assert len(sources) == 2, f"expected 2 sources after hostname dedup; got {len(sources)}"


def test_dedup_is_case_insensitive_on_hostname():
    """Mixed-case hostnames should still be treated as one origin."""
    responses = {"akash-api.polkachu.com": _terminal_lease()}
    snap, sources = verifier.consensus(
        DSEQ,
        OWNER,
        ["https://AKASH-API.POLKACHU.COM", "https://akash-api.polkachu.com"],
        _stub_get(responses),
    )
    assert snap is None  # only one usable source — can't reach two distinct ones


# ── TWO-ENDPOINT AGREEMENT (the core contract) ──────────────────────────────


def test_two_endpoints_agreeing_on_terminal_states_means_closed():
    responses = {
        "akash-api.polkachu.com": _terminal_lease("closed"),
        "rest.cosmos.directory/akash": _terminal_lease("closed"),
    }
    snap, sources = verifier.consensus(
        DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), _stub_get(responses)
    )
    assert snap is not None
    assert sources == verifier.DEFAULT_ENDPOINTS


def test_endpoint_disagreement_means_unverified():
    """Console says closed but chain RPC still shows active — the #952 defect.

    Single-channel agreement is not closure proof. The verifier must return
    None and refuse to set closed=true.
    """
    responses = {
        "akash-api.polkachu.com": _terminal_lease("closed"),  # Console-shaped
        "rest.cosmos.directory/akash": _active_lease(),  # chain still active
    }
    snap, _sources = verifier.consensus(
        DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), _stub_get(responses)
    )
    assert snap is None, "endpoint disagreement must NOT be reported as agreement"


def test_active_lease_on_chain_means_not_closed():
    responses = {
        "akash-api.polkachu.com": _active_lease(),
        "rest.cosmos.directory/akash": _active_lease(),
    }
    v = verifier.verdict(DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), _stub_get(responses))
    assert v["closed"] is False
    assert "active" in v["reason"].lower()


def test_one_endpoint_unavailable_means_unverified():
    def boom(_url):
        raise OSError("RPC unavailable")

    v = verifier.verdict(DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), boom)
    assert v["closed"] is False
    assert v["reason"] == "unverified"


def test_chain_lag_is_handled_by_bounded_retries():
    """Chain lag: first attempts fail to read one endpoint, eventually both agree terminal.

    The destroy retry loop has bounded patience; the verifier must too —
    `retries` controls the patience budget. A first-pass read that fails
    entirely (one endpoint transiently unavailable) is the precise case
    retries cover; an "active" lease is a positive observation that retries
    cannot change.
    """
    terminal = {
        "akash-api.polkachu.com": _terminal_lease("closed"),
        "rest.cosmos.directory/akash": _terminal_lease("closed"),
    }
    attempts = {"n": 0}

    def flaky_get(url: str):
        if "/deployments/info?" in url:
            return _closed_deployment()
        attempts["n"] += 1
        if attempts["n"] <= 2:  # first 2 attempts: cosmos.directory transiently unavailable
            if "cosmos.directory" in url:
                raise OSError("RPC unavailable")
            return terminal["akash-api.polkachu.com"]
        return terminal[
            "akash-api.polkachu.com" if "polkachu" in url else "rest.cosmos.directory/akash"
        ]

    v = verifier.verdict(DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), flaky_get, retries=10)
    assert v["closed"] is True, f"verifier must retry past lag, got {v!r}"
    assert attempts["n"] >= 4, "must retry past the lag window"


# ── INPUT VALIDATION ─────────────────────────────────────────────────────────


def test_dseq_must_be_ascii_digits():
    with pytest.raises(ValueError, match="dseq"):
        verifier.consensus("not-a-dseq", OWNER, [], lambda _u: None)


def test_owner_must_be_akash1_address():
    with pytest.raises(ValueError, match="owner"):
        verifier.consensus(DSEQ, "btc1not-an-akash-address", [], lambda _u: None)


def test_dseq_with_unicode_digits_is_rejected():
    """Arabic-Indic digits are not ASCII; the verifier must refuse."""
    with pytest.raises(ValueError, match="dseq"):
        verifier.consensus("١٢٣", OWNER, [], lambda _u: None)


# ── PAGINATION COMPLETENESS ─────────────────────────────────────────────────


def test_partial_read_is_treated_as_unverified():
    """A page that ends early (truncated pagination) must NOT report success.

    The harness's `page-truncation` mutant in PR #1024 broke this — it took a
    single page as 'complete' even when next_key was non-empty. This test pins
    the full-pagination invariant.
    """

    def truncated_get(url: str):
        return {
            "leases": [_terminal_lease()["leases"][0]],
            "pagination": {"next_key": "next-page"},
        }

    snap = verifier.lease_snapshot("https://akash-api.polkachu.com", DSEQ, OWNER, truncated_get)
    assert snap is None, "a partial read must be treated as no read"


def test_repeated_cursor_is_treated_as_infinite_loop():
    """A pagination cursor that loops must NOT report success."""

    def looping_get(url: str):
        return {"leases": [], "pagination": {"next_key": "same-cursor"}}

    snap = verifier.lease_snapshot("https://akash-api.polkachu.com", DSEQ, OWNER, looping_get)
    assert snap is None


def test_lease_with_ambiguous_state_is_rejected():
    """A lease whose state is not in the known set must fail the read, not silently pass."""

    def bad_state(_url: str):
        return _terminal_lease(state="in-between")

    snap = verifier.lease_snapshot("https://akash-api.polkachu.com", DSEQ, OWNER, bad_state)
    assert snap is None


def test_lease_missing_provider_is_rejected():
    def missing_provider(_url: str):
        doc = _terminal_lease()
        del doc["leases"][0]["lease"]["id"]["provider"]
        return doc

    snap = verifier.lease_snapshot("https://akash-api.polkachu.com", DSEQ, OWNER, missing_provider)
    assert snap is None


# ── TERMINAL STATE SET ───────────────────────────────────────────────────────


def test_insufficient_funds_is_a_terminal_state():
    """The verifier accepts both 'closed' and 'insufficient_funds' as terminal."""
    responses = {
        "akash-api.polkachu.com": _terminal_lease("insufficient_funds"),
        "rest.cosmos.directory/akash": _terminal_lease("insufficient_funds"),
    }
    v = verifier.verdict(DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), _stub_get(responses))
    assert v["closed"] is True
    assert "agreeing terminal" in v["reason"]


# ── VERDICT SHAPE ────────────────────────────────────────────────────────────


def test_verdict_returns_the_documented_shape():
    responses = {
        "akash-api.polkachu.com": _terminal_lease(),
        "rest.cosmos.directory/akash": _terminal_lease(),
    }
    v = verifier.verdict(DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), _stub_get(responses))
    assert set(v.keys()) == {"closed", "sources", "reason"}
    assert isinstance(v["closed"], bool)
    assert isinstance(v["sources"], list)
    assert isinstance(v["reason"], str)


@pytest.mark.parametrize(
    "alias", ["https://chain.example.", "https://CHAIN.example:443", "https://chain.example.:443"]
)
def test_host_alias_cannot_supply_second_confirmation(alias):
    calls = []

    def get(url):
        if "/deployments/info?" in url:
            return _closed_deployment()
        calls.append(url)
        return _terminal_lease()

    result = verifier.verdict(DSEQ, OWNER, ["https://chain.example", alias], get, retries=1)
    assert result["closed"] is False
    assert len(calls) == 1
    calls.clear()
    result = verifier.verdict(
        DSEQ, OWNER, ["https://chain.example", alias, "https://other.example"], get, retries=1
    )
    assert result["closed"] is True
    assert len(calls) == 2
    assert "other.example" in calls[1]


@pytest.mark.parametrize(
    "path,value",
    [
        (("deployment", "state"), "active"),
        (("deployment", "id", "owner"), "akash1" + "b" * 38),
        (("deployment", "id", "dseq"), "8"),
        (("escrow_account", "id", "scope"), "bid"),
        (("escrow_account", "id", "xid"), "wrong/7"),
        (("escrow_account", "state", "owner"), "akash1" + "b" * 38),
        (("escrow_account", "state", "state"), "open"),
        (("escrow_account",), None),
        (("deployment",), None),
    ],
)
@pytest.mark.parametrize("bad_endpoint", verifier.DEFAULT_ENDPOINTS)
def test_deployment_escrow_identity_and_state_are_required(path, value, bad_endpoint):
    doc = _closed_deployment()
    target = doc
    for key in path[:-1]:
        target = target[key]
    assert path[-1] in target
    target[path[-1]] = value
    calls = []

    def get(url):
        calls.append(url)
        if "/deployments/info?" in url:
            return doc if url.startswith(bad_endpoint) else _closed_deployment()
        return _terminal_lease()

    result = verifier.verdict(DSEQ, OWNER, verifier.DEFAULT_ENDPOINTS, get, retries=1)
    assert result["closed"] is False
    assert any("/deployments/info?" in url for url in calls)
    assert len([url for url in calls if "/leases/list?" in url]) == 2


# ── MULTI-LEASE AGGREGATION (A2) ─────────────────────────────────────────────
#
# An Akash deployment can win bids on multiple providers, leaving a single
# (owner, dseq) tuple with several leases. The shared closure contract must
# aggregate the COMPLETE identity-keyed map across providers, refuse to
# claim closed=true while any single provider is still active, and treat
# an asymmetric count between the two endpoints as unverified — a partial
# read on either side reads as agreement only by accident.


def test_two_leases_on_different_providers_both_terminal_means_closed():
    """Both endpoints return two leases on two providers, both closed.

    `lease_snapshot()` collects every row on the page into a single map
    keyed by the (owner, dseq, gseq, oseq, bseq, provider) identity tuple,
    so two providers with the same gseq/oseq/bseq collapse to distinct
    keys and the verdict's `all(states ⊆ TERMINAL_STATES)` predicate is
    forced to walk the whole map.
    """
    page = _multi_lease_page(
        [
            _lease("akashprovider1xyz", "closed"),
            _lease(PROVIDER_2, "closed"),
        ]
    )
    responses = {
        "akash-api.polkachu.com": page,
        "rest.cosmos.directory/akash": page,
    }
    snap, sources = verifier.consensus(
        DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), _stub_get(responses)
    )
    assert snap is not None
    # ⇒ Two distinct identity keys, both terminal.
    assert len(snap) == 2
    assert all(state == "closed" for state in snap.values())
    v = verifier.verdict(DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), _stub_get(responses))
    assert v["closed"] is True
    assert "agreeing terminal" in v["reason"]


def test_two_leases_one_active_means_not_closed():
    """One provider closed, one provider still active → closed=false.

    The whole-map predicate must not short-circuit on the first terminal
    state; if either provider still has an active lease, the deployment
    is not closed. A regression that walks only `min(states)` or `any()`
    passes the all-terminal test but breaks this one.
    """
    page = _multi_lease_page(
        [
            _lease("akashprovider1xyz", "closed"),
            _lease(PROVIDER_2, "active"),
        ]
    )
    responses = {
        "akash-api.polkachu.com": page,
        "rest.cosmos.directory/akash": page,
    }
    snap, _sources = verifier.consensus(
        DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), _stub_get(responses)
    )
    # ⇒ Endpoints agree on the mixed-state population, so consensus
    # succeeds but verdict refuses closed=true.
    assert snap is not None
    assert len(snap) == 2
    states = set(snap.values())
    assert states == {"closed", "active"}
    v = verifier.verdict(DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), _stub_get(responses))
    assert v["closed"] is False
    assert "active" in v["reason"].lower()


def test_two_leases_closed_and_insufficient_funds_means_closed():
    """`insufficient_funds` is in TERMINAL_STATES; mixed with closed is closed.

    Pinning the contract: a lease whose provider ran out of funds to keep
    the bid alive is on equal closure footing with a lease whose provider
    chose to close. The deployment is closed iff the whole identity map is
    ⊆ {closed, insufficient_funds}.
    """
    page = _multi_lease_page(
        [
            _lease("akashprovider1xyz", "closed"),
            _lease(PROVIDER_2, "insufficient_funds"),
        ]
    )
    responses = {
        "akash-api.polkachu.com": page,
        "rest.cosmos.directory/akash": page,
    }
    v = verifier.verdict(DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), _stub_get(responses))
    assert v["closed"] is True
    assert "agreeing terminal" in v["reason"]


def test_asymmetric_endpoint_counts_means_unverified():
    """Endpoint A returns two leases; endpoint B returns only one.

    A partial read on either side cannot establish agreement: the missing
    provider may be still active on the lagging endpoint, or already
    closed — the verifier has no way to tell, so the population is treated
    as unverified and verdict returns closed=false with `unverified`.

    A regression that returns agreement on the intersection (only the
    leases both endpoints happened to return) would read as closed=true
    for a deployment whose third provider is still active — the exact
    shape of the cross-host single-channel defect this verifier exists
    to refuse.
    """
    two_providers = _multi_lease_page(
        [
            _lease("akashprovider1xyz", "closed"),
            _lease(PROVIDER_2, "closed"),
        ]
    )
    one_provider = _multi_lease_page(
        [
            _lease("akashprovider1xyz", "closed"),
        ]
    )
    responses = {
        "akash-api.polkachu.com": two_providers,
        "rest.cosmos.directory/akash": one_provider,
    }
    snap, sources = verifier.consensus(
        DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), _stub_get(responses)
    )
    assert snap is None, (
        "asymmetric endpoint counts must NOT be reported as agreement; a partial read is no read"
    )
    assert sources == ()
    v = verifier.verdict(DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), _stub_get(responses))
    assert v["closed"] is False
    assert v["reason"] == "unverified"


def test_multi_page_pagination_aggregates_across_providers():
    """Provider A on page 1, provider B on page 2 — both endpoints paginate.

    Pagination must not lose leases; a regression that returned on the
    first page (page-truncation mutant in PR #1024) would treat the
    multi-lease deployment as single-lease, missing provider B entirely.
    Pinning the multi-page, multi-provider aggregation in one test.
    """
    page1 = _multi_lease_page([_lease("akashprovider1xyz", "closed")], next_key="cursor-1")
    page2 = _multi_lease_page([_lease(PROVIDER_2, "closed")])

    def paging_get(url: str):
        if "/deployments/info?" in url:
            return _closed_deployment()
        if "pagination.key=cursor-1" in url:
            return page2
        return page1

    snap = verifier.lease_snapshot("https://akash-api.polkachu.com", DSEQ, OWNER, paging_get)
    assert snap is not None
    assert len(snap) == 2
    assert all(state == "closed" for state in snap.values())


def test_all_leases_terminal_but_escrow_open_means_not_closed():
    """Lease-level agreement is necessary but not sufficient.

    A lease can read as `closed` while the deployment's escrow is still
    `open` (the lease closed but the account-side funds were not
    returned). The verdict's deployment/escrow gate is a second-line
    refusal that must fire when the lease-level predicate accepts but
    escrow-level does not.
    """
    page = _multi_lease_page(
        [
            _lease("akashprovider1xyz", "closed"),
            _lease(PROVIDER_2, "closed"),
        ]
    )

    def open_escrow_get(url: str):
        if "/deployments/info?" in url:
            return {
                "deployment": {"id": {"owner": OWNER, "dseq": DSEQ}, "state": "closed"},
                "escrow_account": {
                    "id": {"scope": "deployment", "xid": f"{OWNER}/{DSEQ}"},
                    "state": {"owner": OWNER, "state": "open"},
                },
            }
        return page

    snap, sources = verifier.consensus(
        DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), open_escrow_get
    )
    assert snap is not None
    v = verifier.verdict(DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), open_escrow_get, retries=1)
    assert v["closed"] is False
    assert "escrow" in v["reason"].lower() or "deployment or escrow" in v["reason"].lower()


def test_lease_on_different_dseq_is_excluded_from_aggregation():
    """A row whose identity does NOT match the requested (owner, dseq)
    is a structural defect, not a multi-lease row.

    `lease_snapshot()` filters at line `if numbers[0] != str(int(dseq))
    or owner_field != owner: return None` — a cross-dseq row inside a
    page is treated as unreadable rather than silently included. Pinning
    that filter here so a regression that drops it cannot quietly
    aggregate a sibling deployment's leases into the verdict.
    """

    def cross_dseq_get(url: str):
        if "/deployments/info?" in url:
            return _closed_deployment()
        return {
            "leases": [
                {
                    "lease": {
                        "id": {
                            "owner": OWNER,
                            "dseq": str(int(DSEQ) + 1),  # ← different dseq
                            "gseq": 1,
                            "oseq": 1,
                            "bseq": 0,
                            "provider": "akashprovider1xyz",
                        },
                        "state": "closed",
                        "price": {"denom": "uakt", "amount": "1000"},
                    }
                }
            ],
            "pagination": {"next_key": ""},
        }

    snap = verifier.lease_snapshot("https://akash-api.polkachu.com", DSEQ, OWNER, cross_dseq_get)
    assert snap is None, "a cross-dseq row inside the page must read as no read"


def test_lease_on_different_owner_is_excluded_from_aggregation():
    """A row whose owner field differs from the requested owner is
    a structural defect (cross-owner bleed), not a multi-lease row.
    Same shape as the cross-dseq case.
    """
    other_owner = "akash1" + "b" * 38

    def cross_owner_get(url: str):
        if "/deployments/info?" in url:
            return _closed_deployment()
        return {
            "leases": [
                {
                    "lease": {
                        "id": {
                            "owner": other_owner,  # ← different owner
                            "dseq": DSEQ,
                            "gseq": 1,
                            "oseq": 1,
                            "bseq": 0,
                            "provider": "akashprovider1xyz",
                        },
                        "state": "closed",
                        "price": {"denom": "uakt", "amount": "1000"},
                    }
                }
            ],
            "pagination": {"next_key": ""},
        }

    snap = verifier.lease_snapshot("https://akash-api.polkachu.com", DSEQ, OWNER, cross_owner_get)
    assert snap is None, "a cross-owner row inside the page must read as no read"


def test_asymmetric_page_counts_same_final_map_consensus():
    """Endpoint A paginates across 2 pages; endpoint B returns a single page.

    Both endpoints end with the same two-lease map (provider1 closed,
    provider2 closed). The COMPLETE map comparison must consensus; a
    regression that compared only the first page returned by either
    side would diverge here even though the population agrees.
    """
    page1 = _multi_lease_page([_lease("akashprovider1xyz", "closed")], next_key="cursor-1")
    page2 = _multi_lease_page([_lease(PROVIDER_2, "closed")])
    one_page = _multi_lease_page(
        [
            _lease("akashprovider1xyz", "closed"),
            _lease(PROVIDER_2, "closed"),
        ]
    )

    def paging_get(url: str):
        if "/deployments/info?" in url:
            return _closed_deployment()
        if "akash-api.polkachu.com" in url:
            if "pagination.key=cursor-1" in url:
                return page2
            return page1
        return one_page

    snap, sources = verifier.consensus(DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), paging_get)
    assert snap is not None, (
        "asymmetric page counts producing the same final map must consensus; "
        "otherwise chain lag on a paginating endpoint would permanently lock out close"
    )
    assert len(snap) == 2
    v = verifier.verdict(DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), paging_get)
    assert v["closed"] is True


def test_identity_collision_only_provider_differs_keeps_two_entries():
    """Two leases whose identity tuples differ ONLY in provider stay as two entries.

    The key tuple is `(owner, dseq, gseq, oseq, bseq, provider)`. A
    regression that dropped `provider` from the key would collapse the
    two leases into a single entry, and the verdict's `states ⊆
    TERMINAL_STATES` predicate would walk only one row — the same
    population-agreement defect the multi-lease path exists to refuse.
    """
    page = _multi_lease_page(
        [
            _lease("akashprovider1xyz", "closed"),
            _lease(PROVIDER_2, "closed"),
        ]
    )
    responses = {
        "akash-api.polkachu.com": page,
        "rest.cosmos.directory/akash": page,
    }
    snap, _sources = verifier.consensus(
        DSEQ, OWNER, list(verifier.DEFAULT_ENDPOINTS), _stub_get(responses)
    )
    assert snap is not None
    assert len(snap) == 2, (
        "two leases differing only in provider must stay as two identity keys; "
        "collapsing them to one silently closes a deployment whose second provider is still active"
    )
    keys = list(snap.keys())
    assert keys[0][-1] != keys[-1], "the two keys must differ in the provider slot"
