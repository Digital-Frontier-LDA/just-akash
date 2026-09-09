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
