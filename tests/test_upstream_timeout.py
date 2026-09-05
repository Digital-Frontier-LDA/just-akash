"""An upstream proxy timeout is not evidence about the code under test (#266).

⛔ EVERY HTTP ERROR ARRIVED AS THE SAME `RuntimeError`. A Cloudflare 524 — the
proxy giving up on Akash's origin after 120s — was indistinguishable from a 400
saying our SDL is wrong, except by substring-matching the message. So an
upstream outage read as a test failure, the E2E leg looked "flaky", and it got
re-run by hand at real Akash cost per round.

⚠️ AND THE OBVIOUS FIX IS THE DANGEROUS ONE. The issue proposes retrying on 524
because the body says `retryable: true`. That flag is Cloudflare describing its
OWN proxy semantics; it cannot describe whether Akash's origin committed the
transaction, because Cloudflare does not know. `deploy.py:290` records the
measured counter-case in this repo's own words: "a gateway 500, a PROXY TIMEOUT
or a dropped connection can land AFTER the transaction committed — measured
shape: HTTP 500 returned 103 SECONDS into the request." The #266 timeout arrived
at 125s.

So these tests pin the distinction that is safe (whose fault was it?) and pin
the absence of the one that is not (a blind re-POST).
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from just_akash.api import AkashAPIError

_CF_524 = json.dumps(
    {
        "message": "origin timeout",
        "retryable": True,
        "retry_after": 120,
        "error_name": "origin_response_timeout",
    }
)


def _raise_http(monkeypatch, client, code: int, body: str):
    """Make one _request call fail with a real HTTPError carrying `body`."""

    import io

    def _boom(*_a, **_k):
        raise urllib.error.HTTPError("u", code, "err", {}, io.BytesIO(body.encode()))  # type: ignore[arg-type]

    # Caught as RuntimeError ON PURPOSE — that is the type every caller in the
    # tree catches today, so catching the subclass here would stop proving it.
    # Tests needing the extra fields narrow with an explicit isinstance.
    monkeypatch.setattr("urllib.request.urlopen", _boom)
    with pytest.raises(RuntimeError) as exc:
        client._request("POST", "/v1/deployments", {"x": 1})
    return exc.value


@pytest.fixture
def client():
    from just_akash.api import AkashConsoleAPI

    return AkashConsoleAPI("not-a-real-key")


class TestTheErrorCarriesEnoughToApportionBlame:
    def test_a_524_is_identifiable_as_upstream(self, monkeypatch, client):
        err = _raise_http(monkeypatch, client, 524, _CF_524)
        assert isinstance(err, AkashAPIError)
        assert err.status == 524
        assert err.is_upstream_timeout()

    def test_a_400_is_not(self, monkeypatch, client):
        """Our own bad request must NOT be excusable as upstream — that is the
        direction where being wrong hides a real defect in the change."""
        err = _raise_http(monkeypatch, client, 400, json.dumps({"message": "bad sdl"}))
        assert isinstance(err, AkashAPIError)
        assert not err.is_upstream_timeout()

    def test_an_origin_response_timeout_counts_even_without_524(self, monkeypatch, client):
        body = json.dumps({"message": "x", "error_name": "origin_response_timeout"})
        err = _raise_http(monkeypatch, client, 500, body)
        assert isinstance(err, AkashAPIError)
        assert err.is_upstream_timeout()

    def test_the_upstreams_claim_is_recorded_not_believed(self, monkeypatch, client):
        """`retryable` is kept for the operator to read, and is NOT what
        is_upstream_timeout() consults — nothing may retry on the strength of a
        flag the proxy is not in a position to assert."""
        err = _raise_http(monkeypatch, client, 524, _CF_524)
        assert isinstance(err, AkashAPIError)
        assert err.retryable is True
        assert err.retry_after == 120
        no_flag = _raise_http(monkeypatch, client, 524, json.dumps({"message": "x"}))
        assert isinstance(no_flag, AkashAPIError)
        assert no_flag.retryable is None, "absent must stay absent, not become False"
        assert no_flag.is_upstream_timeout(), "classification must not depend on the flag"


class TestBackwardCompatibility:
    """⛔ Callers match on the TYPE and the STRING today. Both must survive, or
    this 'diagnostic improvement' silently breaks error handling across the tree
    — deploy.py's `"already exists" in str(e).lower()`, cleanup_stale's
    `_is_credit_error`, and every bare `except RuntimeError`."""

    def test_it_is_still_a_RuntimeError(self, monkeypatch, client):
        assert isinstance(_raise_http(monkeypatch, client, 524, _CF_524), RuntimeError)

    def test_the_message_format_is_unchanged(self, monkeypatch, client):
        err = _raise_http(monkeypatch, client, 409, json.dumps({"message": "already exists"}))
        assert str(err) == "API Error (409): already exists"
        assert "already exists" in str(err).lower(), "deploy.py's recovery matches on this"

    def test_a_non_json_body_still_raises_cleanly(self, monkeypatch, client):
        err = _raise_http(monkeypatch, client, 502, "<html>bad gateway</html>")
        assert isinstance(err, AkashAPIError)
        assert err.status == 502
        assert err.retryable is None


def test_no_automatic_retry_of_a_create_exists():
    """⛔ THE ABSENCE IS THE FEATURE, so it is asserted rather than assumed.

    A create is not idempotent, and a proxy timeout can land after the origin
    committed. Anything that re-POSTs on a 524 without a positive read-back
    double-spends escrow on exactly the failure that is hardest to observe.
    If a retry is added later it must be preceded by the orphan check, and this
    test should fail loudly rather than let it in quietly.
    """

    import inspect

    from just_akash import deploy

    src = inspect.getsource(deploy)
    hot = src[src.index("STEP 2: Creating deployment") :]
    hot = hot[: hot.index("STEP 3")] if "STEP 3" in hot else hot

    assert "is_upstream_timeout" in hot, "the create path must classify an upstream timeout"
    # No loop in the create path: a `while`/`for` around the POST is what a blind
    # retry looks like. The ONE existing retry is the "already exists" recovery,
    # which is a single re-POST after clearing a known leftover, not a loop.
    assert "while " not in hot, "a loop appeared in the create path — is this a blind retry?"
    assert hot.count("client.create_deployment(") <= 2, (
        "more than the initial POST and the one already-exists retry — a create is "
        "not idempotent and a proxy timeout can land after the origin committed"
    )
