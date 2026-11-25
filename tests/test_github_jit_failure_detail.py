"""df-cicd-hz4.1.5: a GitHub failure keeps its bare class, never its text.

Accounting run 45500532401 held one slot as "unexpected" with no HTTP status:
github_request dropped the status with `from None`. These pin the bare
attributes a hold carries (http_status, kind, request_id), the single bounded
retry of read-only GETs, the never-retried POST and rate limits, and the
exact-name lookup that precedes any mint after an unknown outcome.
"""

import http.client
import io
import urllib.error

import pytest

from just_akash import github_jit as jit

PATH = jit._ROOT + "/runner-groups/37"
MINT = jit._ROOT + "/runners/generate-jitconfig"
NAME = "dfci-grafana-123-1-rules-0"


def headers(**items):
    message = http.client.HTTPMessage()
    for key, value in items.items():
        message[key.replace("_", "-")] = value
    return message


def http_error(status, body=b"secret-body", **items):
    return urllib.error.HTTPError(
        jit.API + PATH, status, "fixture-reason", headers(**items), io.BytesIO(body)
    )


class Response(io.BytesIO):
    def __init__(self, raw=b'{"ok":true}', status=200, **items):
        super().__init__(raw)
        self.status = status
        self.headers = headers(**items)


class Network:
    """A scripted opener: each open() takes the next outcome, an exception or a response."""

    def __init__(self, monkeypatch, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.sleeps = []
        monkeypatch.setattr(jit.urllib.request, "build_opener", lambda _handler: self)
        monkeypatch.setattr(jit, "_sleep", self.sleeps.append)
        monkeypatch.setattr(jit, "_now", lambda: 1_000_000.0)

    def open(self, req, timeout):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def attributes(error):
    return (error.http_status, error.kind, error.request_id)


def leaks(error):
    text = repr(error) + str(error) + repr(vars(error))
    return any(s in text for s in ("secret-body", "installation-fixture", "fixture-reason"))


# --- read-only GETs: one bounded retry ----------------------------------------------------


def test_get_5xx_is_retried_once_then_returns(monkeypatch):
    net = Network(monkeypatch, http_error(502), Response())
    assert jit.github_request("GET", PATH, "installation-fixture") == {"ok": True}
    assert net.calls == 2
    assert net.sleeps == [jit._RETRY_DEFAULT_SECONDS]


def test_get_5xx_twice_holds_with_bare_status_kind_and_request_id(monkeypatch):
    net = Network(
        monkeypatch,
        http_error(503, x_github_request_id="AB12:3456:CDEF"),
        http_error(503, x_github_request_id="CD34:5678:ABCD"),
    )
    with pytest.raises(jit.JitHold) as error:
        jit.github_request("GET", PATH, "installation-fixture")
    assert type(error.value) is jit.JitHold
    assert net.calls == 2
    # The last attempt's evidence is the one recorded.
    assert attributes(error.value) == (503, "http", "CD34:5678:ABCD")
    assert not leaks(error.value)


def test_get_timeout_twice_holds_as_timeout_without_status(monkeypatch):
    net = Network(monkeypatch, TimeoutError(), TimeoutError())
    with pytest.raises(jit.JitHold) as error:
        jit.github_request("GET", PATH, "installation-fixture")
    assert net.calls == 2
    assert attributes(error.value) == (None, "timeout", None)


def test_get_urlerror_timeout_reason_is_a_timeout(monkeypatch):
    net = Network(monkeypatch, urllib.error.URLError(TimeoutError()), urllib.error.URLError(TimeoutError()))
    with pytest.raises(jit.JitHold) as error:
        jit.github_request("GET", PATH, "installation-fixture")
    assert net.calls == 2
    assert error.value.kind == "timeout"


def test_get_network_failure_holds_as_network(monkeypatch):
    net = Network(
        monkeypatch, urllib.error.URLError("secret-body"), urllib.error.URLError("secret-body")
    )
    with pytest.raises(jit.JitHold) as error:
        jit.github_request("GET", PATH, "installation-fixture")
    assert net.calls == 2
    assert attributes(error.value) == (None, "network", None)
    assert not leaks(error.value)


def test_get_network_failure_then_success_returns(monkeypatch):
    net = Network(monkeypatch, ConnectionResetError(), Response())
    assert jit.github_request("GET", PATH, "installation-fixture") == {"ok": True}
    assert net.calls == 2


@pytest.mark.parametrize("status", [400, 401, 404, 409, 422])
def test_get_other_4xx_is_not_retried(status, monkeypatch):
    net = Network(monkeypatch, http_error(status))
    with pytest.raises(jit.JitHold) as error:
        jit.github_request("GET", PATH, "installation-fixture")
    assert net.calls == 1
    assert net.sleeps == []
    assert attributes(error.value) == (status, "http", None)


def test_get_plain_403_is_not_retried_and_is_not_a_rate_limit(monkeypatch):
    net = Network(monkeypatch, http_error(403, x_ratelimit_remaining="4999"))
    with pytest.raises(jit.JitHold) as error:
        jit.github_request("GET", PATH, "installation-fixture")
    assert net.calls == 1
    assert attributes(error.value) == (403, "http", None)


@pytest.mark.parametrize(
    "status,items",
    [
        (403, {"x_ratelimit_remaining": "0", "x_ratelimit_reset": "1000002"}),
        (403, {"retry_after": "1"}),  # secondary rate limit
        (429, {}),
        (429, {"retry_after": "1"}),
        (429, {"x_ratelimit_remaining": "0", "x_ratelimit_reset": "1000001"}),
    ],
)
def test_rate_limit_is_never_retried_even_when_the_wait_is_short(status, items, monkeypatch):
    net = Network(monkeypatch, http_error(status, x_github_request_id="AB:CD", **items))
    with pytest.raises(jit.JitHold) as error:
        jit.github_request("GET", PATH, "installation-fixture")
    assert net.calls == 1
    assert net.sleeps == []
    assert attributes(error.value) == (status, "rate_limited", "AB:CD")


def test_retry_honours_retry_after(monkeypatch):
    net = Network(monkeypatch, http_error(503, retry_after="7"), Response())
    assert jit.github_request("GET", PATH, "installation-fixture") == {"ok": True}
    assert net.sleeps == [7.0]


def test_retry_honours_ratelimit_reset(monkeypatch):
    net = Network(monkeypatch, http_error(502, x_ratelimit_reset="1000004"), Response())
    assert jit.github_request("GET", PATH, "installation-fixture") == {"ok": True}
    assert net.sleeps == [4.0]


def test_retry_takes_the_longer_of_retry_after_and_reset(monkeypatch):
    net = Network(
        monkeypatch, http_error(502, retry_after="3", x_ratelimit_reset="1000006"), Response()
    )
    jit.github_request("GET", PATH, "installation-fixture")
    assert net.sleeps == [6.0]


@pytest.mark.parametrize(
    "items", [{"retry_after": "11"}, {"retry_after": "3600"}, {"x_ratelimit_reset": "1000060"}]
)
def test_a_wait_longer_than_the_cap_holds_without_retry(items, monkeypatch):
    net = Network(monkeypatch, http_error(503, **items))
    with pytest.raises(jit.JitHold) as error:
        jit.github_request("GET", PATH, "installation-fixture")
    assert net.calls == 1
    assert net.sleeps == []
    assert attributes(error.value) == (503, "http", None)


def test_the_cap_itself_is_still_waited(monkeypatch):
    net = Network(monkeypatch, http_error(503, retry_after="10"), Response())
    jit.github_request("GET", PATH, "installation-fixture")
    assert net.sleeps == [10.0]
    assert jit._RETRY_CAP_SECONDS == 10.0


@pytest.mark.parametrize("value", ["soon", "-5", "1e3", ""])
def test_unparseable_retry_after_falls_back_to_the_default_wait(value, monkeypatch):
    net = Network(monkeypatch, http_error(503, retry_after=value), Response())
    jit.github_request("GET", PATH, "installation-fixture")
    assert net.sleeps == [jit._RETRY_DEFAULT_SECONDS]


def test_a_past_reset_waits_nothing_extra(monkeypatch):
    net = Network(monkeypatch, http_error(503, x_ratelimit_reset="999990"), Response())
    jit.github_request("GET", PATH, "installation-fixture")
    assert net.sleeps == [jit._RETRY_DEFAULT_SECONDS]


def test_get_is_retried_at_most_once(monkeypatch):
    net = Network(monkeypatch, http_error(502), http_error(502), Response())
    with pytest.raises(jit.JitHold):
        jit.github_request("GET", PATH, "installation-fixture")
    assert net.calls == 2
    assert len(net.sleeps) == 1


@pytest.mark.parametrize(
    "response",
    [
        Response(b"not-json"),
        Response(b"[]"),
        Response(b"{}", status=204),
        Response(b"x" * (jit._MAX_BYTES + 1)),
    ],
)
def test_an_invalid_get_response_is_not_retried(response, monkeypatch):
    net = Network(monkeypatch, response)
    with pytest.raises(jit.JitHold) as error:
        jit.github_request("GET", PATH, "installation-fixture")
    assert net.calls == 1
    assert error.value.kind == "invalid"
    assert not leaks(error.value)


def test_an_unexpected_get_status_carries_it(monkeypatch):
    net = Network(monkeypatch, Response(b"{}", status=204, x_github_request_id="EF:01"))
    with pytest.raises(jit.JitHold) as error:
        jit.github_request("GET", PATH, "installation-fixture")
    assert net.calls == 1
    assert attributes(error.value) == (204, "invalid", "EF:01")


# --- the POST: never retried, unknown keeps its evidence ----------------------------------


@pytest.mark.parametrize(
    "failure,expected",
    [
        (lambda: http_error(502, x_github_request_id="AA:BB"), (502, "http", "AA:BB")),
        (lambda: http_error(503, retry_after="1"), (503, "http", None)),
        (lambda: http_error(422), (422, "http", None)),
        (lambda: http_error(403, x_ratelimit_remaining="0"), (403, "rate_limited", None)),
        (lambda: http_error(429), (429, "rate_limited", None)),
        (lambda: TimeoutError(), (None, "timeout", None)),
        (lambda: urllib.error.URLError("secret-body"), (None, "network", None)),
    ],
)
def test_post_is_never_retried_and_stays_unknown_with_evidence(failure, expected, monkeypatch):
    net = Network(monkeypatch, failure(), Response(status=201))
    with pytest.raises(jit.JitMintUnknown) as error:
        jit.github_request("POST", MINT, "installation-fixture", {"name": NAME})
    assert net.calls == 1
    assert net.sleeps == []
    assert attributes(error.value) == expected
    assert not leaks(error.value)


def test_mint_keeps_the_post_failure_evidence():
    def request(method, path, token, body=None):
        if method == "POST":
            raise jit.JitMintUnknown("x", http_status=502, kind="http", request_id="AB:CD")
        return GROUP.answer(path)

    with pytest.raises(jit.JitMintUnknown) as error:
        jit.mint_jit(POLICY, NAME, LABELS, "installation-fixture", request=request)
    assert attributes(error.value) == (502, "http", "AB:CD")


def test_mint_keeps_a_policy_read_failure_a_hold_with_its_evidence():
    posts = []

    def request(method, path, token, body=None):
        if method == "POST":
            posts.append(path)
        raise jit.JitHold("x", http_status=503, kind="http", request_id="AB:CD")

    with pytest.raises(jit.JitHold) as error:
        jit.mint_jit(POLICY, NAME, LABELS, "installation-fixture", request=request)
    assert type(error.value) is jit.JitHold
    assert attributes(error.value) == (503, "http", "AB:CD")
    assert posts == []


def test_a_corrupt_mint_response_is_unknown_and_invalid():
    def request(method, path, token, body=None):
        if method == "POST":
            return {"runner": None, "encoded_jit_config": "x"}
        return GROUP.answer(path)

    with pytest.raises(jit.JitMintUnknown) as error:
        jit.mint_jit(POLICY, NAME, LABELS, "installation-fixture", request=request)
    assert attributes(error.value) == (None, "invalid", None)


def test_an_invalid_controller_request_is_kind_invalid(monkeypatch):
    with pytest.raises(jit.JitHold) as error:
        jit.github_request("DELETE", PATH, "installation-fixture")
    assert attributes(error.value) == (None, "invalid", None)


# --- the attributes are bare -----------------------------------------------------------------


@pytest.mark.parametrize("cls", [jit.JitHold, jit.JitMintUnknown])
def test_attributes_are_bare_and_validated(cls):
    good = cls("m", http_status=502, kind="rate_limited", request_id="AB12:CD34")
    assert attributes(good) == (502, "rate_limited", "AB12:CD34")
    assert set(vars(good)) == {"http_status", "kind", "request_id"}
    bad = cls("m", http_status=True, kind="secret-body", request_id="bad id\nsecret-body")
    assert attributes(bad) == (None, None, None)
    for status in (99, 600, "502", 502.0):
        assert cls("m", http_status=status).http_status is None
    assert cls("m", request_id="A" * 81).request_id is None
    assert attributes(cls("m")) == (None, None, None)


def test_kinds_are_the_reviewed_set():
    assert jit.JIT_FAILURE_KINDS == frozenset(
        {"http", "timeout", "network", "invalid", "rate_limited", "name_exists"}
    )


def test_a_malformed_request_id_header_is_dropped(monkeypatch):
    Network(monkeypatch, http_error(404, x_github_request_id="secret body\twith spaces"))
    with pytest.raises(jit.JitHold) as error:
        jit.github_request("GET", PATH, "installation-fixture")
    assert error.value.request_id is None


# --- exact-name lookup before any mint after an unknown outcome -------------------------------

from just_akash.github_jit import JitPolicy  # noqa: E402

REPO = "Digital-Frontier-LDA/df-grafana"
WORKFLOW = REPO + "/.github/workflows/ci-observability.yml@" + "a" * 40
POLICY = JitPolicy("b" * 40, 37, 123, REPO, (WORKFLOW,))
LABELS = (NAME, "linux", "akash")


class _Group:
    group = {
        "id": 37,
        "visibility": "selected",
        "allows_public_repositories": False,
        "restricted_to_workflows": True,
        "selected_workflows": [WORKFLOW],
    }

    def answer(self, path):
        if "/repositories?" in path:
            page = int(path.rsplit("=", 1)[1])
            rows = [{"id": 123, "full_name": REPO, "private": True}] if page == 1 else []
            return {"total_count": 1, "repositories": rows}
        return dict(self.group)


GROUP = _Group()


class Runners:
    def __init__(self, rows, total=None):
        self.rows = rows
        self.total = len(rows) if total is None else total
        self.calls = []

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path))
        if method == "POST":
            import base64
            import json

            config = base64.b64encode(
                json.dumps({".runner": base64.b64encode(b"x").decode()}).encode()
            ).decode()
            return {"runner": {"id": 789, "name": NAME}, "encoded_jit_config": config}
        if path.startswith(jit._ROOT + "/runners?"):
            return {"total_count": self.total, "runners": [dict(r) for r in self.rows]}
        return GROUP.answer(path)


def test_lookup_reads_the_org_runner_list_filtered_by_exact_name():
    github = Runners([{"id": 55, "name": NAME, "busy": False, "status": "offline"}])
    found = jit.find_runner_by_name(NAME, "installation-fixture", request=github)
    assert found == jit.RunnerRegistration(55, NAME, False)
    assert github.calls == [("GET", jit._ROOT + "/runners?name=" + NAME + "&per_page=100")]


def test_lookup_ignores_a_different_name_and_returns_none_when_absent():
    github = Runners([{"id": 56, "name": NAME + "-other", "busy": False}])
    assert jit.find_runner_by_name(NAME, "installation-fixture", request=github) is None
    assert jit.find_runner_by_name(NAME, "installation-fixture", request=Runners([])) is None


@pytest.mark.parametrize(
    "rows,total",
    [
        ([{"id": 55, "name": NAME}, {"id": 57, "name": NAME}], None),  # ambiguous
        ([{"id": 55, "name": NAME}], 2),  # incomplete page
        ([{"id": True, "name": NAME}], None),
        ([{"id": 0, "name": NAME}], None),
        ([{"id": 55, "name": None}], None),
        (["row"], None),
        ([{"id": i, "name": f"x{i}"} for i in range(1, 102)], None),
    ],
)
def test_lookup_holds_on_an_unverifiable_population(rows, total):
    with pytest.raises(jit.JitHold):
        jit.find_runner_by_name(NAME, "installation-fixture", request=Runners(rows, total))


@pytest.mark.parametrize("name,token", [("bad name", "t"), ("other-1", "t"), (NAME, ""), (NAME, "a\nb")])
def test_lookup_validates_before_any_request(name, token):
    github = Runners([])
    with pytest.raises(jit.JitHold):
        jit.find_runner_by_name(name, token, request=github)
    assert github.calls == []


def test_mint_after_unknown_refuses_when_the_name_is_registered():
    github = Runners([{"id": 55, "name": NAME, "busy": False}])
    with pytest.raises(jit.JitHold) as error:
        jit.mint_jit(
            POLICY, NAME, LABELS, "installation-fixture", request=github, require_name_absent=True
        )
    assert type(error.value) is jit.JitHold
    assert error.value.kind == "name_exists"
    assert not any(method == "POST" for method, _ in github.calls)


def test_mint_after_unknown_mints_once_when_the_name_is_absent():
    github = Runners([])
    handoff = jit.mint_jit(
        POLICY, NAME, LABELS, "installation-fixture", request=github, require_name_absent=True
    )
    assert handoff.runner_id == 789
    methods = [m for m, _ in github.calls]
    assert methods.count("POST") == 1
    # The lookup is the last read before the POST.
    lookup = methods.index("POST") - 1
    assert github.calls[lookup][1].startswith(jit._ROOT + "/runners?name=")


def test_a_plain_mint_does_not_spend_a_lookup():
    github = Runners([{"id": 55, "name": NAME, "busy": False}])
    jit.mint_jit(POLICY, NAME, LABELS, "installation-fixture", request=github)
    assert not any(p.startswith(jit._ROOT + "/runners?") for _, p in github.calls)
