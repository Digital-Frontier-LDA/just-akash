"""Real HTTPX/MockTransport; synthetic hosted environment, never live tokens."""

import base64
import io
import json
import logging
import traceback
from types import SimpleNamespace

import httpx
import pytest

from just_akash import hosted_gha_oidc as oidc
from just_akash.hosted_legacy_cli import HostedGHAPrincipals

REAL_CLIENT = httpx.Client
URL = "https://job-service.actions.githubusercontent.com/opaque/job%2Ftoken?api-version=2.0&nonce=a%2Bb&empty=&bare&x=a+b"
AUD = "https://sentinel.digitalfrontier.so/legacy"
REQUEST_TOKEN = "SYNTHETIC_REQUEST_TOKEN_SENTINEL"
READ_TOKEN = "SYNTHETIC_ACTIONS_READ_TOKEN_SENTINEL"


def segment(value):
    raw = value if type(value) is bytes else json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def token(now, *, claims=None, header=None):
    h = {"alg": "RS256", "typ": "JWT", "kid": "public-synthetic-key"}
    p = {"iss": oidc.ISSUER, "aud": AUD, "iat": now, "nbf": now, "exp": now + 300}
    if header is not None:
        h = header
    if claims is not None:
        p.update(claims)
    return ".".join((segment(h), segment(p), segment(b"s" * 256)))


@pytest.fixture
def world(monkeypatch):
    w = SimpleNamespace(
        mono=100.0,
        utc=1800000000,
        calls=[],
        constructors=[],
        reads=0,
        response_closes=0,
        client_closes=0,
        body=None,
        headers=None,
        status=200,
        construct_late=False,
        body_late=False,
        close_late=False,
        client_late=False,
        body_error=False,
        close_error=False,
        client_error=False,
        env_change=False,
        foreign_url=False,
        response_hook=None,
    )
    monkeypatch.setenv(oidc._URL_ENV, URL)
    monkeypatch.setenv(oidc._REQUEST_CREDENTIAL_NAME, REQUEST_TOKEN)
    monkeypatch.setattr(oidc.time, "monotonic", lambda: w.mono)
    monkeypatch.setattr(oidc.time, "time", lambda: w.utc)
    policy = oidc.HostedOIDCPolicy(AUD)
    monkeypatch.setattr(oidc, "_reviewed_hosted_oidc_policy", lambda: policy)
    w.policy = policy

    class Stream(httpx.SyncByteStream):
        def __init__(self, raw):
            self.raw = raw

        def __iter__(self):
            w.reads += 1
            if w.body_late:
                w.mono += 5
            if w.body_error:
                raise httpx.ReadError("SYNTHETIC_SECRET_READ_URL_" + URL)
            yield self.raw

        def close(self):
            w.response_closes += 1
            if w.close_late:
                w.mono += 5
            if w.env_change:
                monkeypatch.setenv(oidc._REQUEST_CREDENTIAL_NAME, "DIFFERENT_REQUEST_TOKEN")
            if w.close_error:
                raise OSError("SYNTHETIC_SECRET_CLOSE_TOKEN_" + REQUEST_TOKEN)

    class ForeignResponse(httpx.Response):
        @property
        def url(self):
            return httpx.URL("https://foreign.invalid/path")

    def handle(request):
        w.calls.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer " + REQUEST_TOKEN
        assert READ_TOKEN not in str(request.headers)
        assert request.headers["accept-encoding"] == "identity"
        # Exercise actual root-handler propagation, including a newly named child.
        logging.getLogger("httpcore.connection").debug("private-response-header-%s", REQUEST_TOKEN)
        logging.getLogger("httpcore.late-created-child").debug("private-opaque-url-%s", URL)
        if w.response_hook is not None:
            w.response_hook()
        raw = w.body if w.body is not None else json.dumps({"value": token(w.utc)}).encode()
        headers = (
            w.headers
            if w.headers is not None
            else [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(raw)).encode()),
            ]
        )
        cls = ForeignResponse if w.foreign_url else httpx.Response
        return cls(w.status, headers=headers, stream=Stream(raw))

    class Client(REAL_CLIENT):
        def __exit__(self, *args):
            result = super().__exit__(*args)
            w.client_closes += 1
            if w.client_late:
                w.mono += 5
            if w.client_error:
                raise OSError("SYNTHETIC_SECRET_CLIENT_RETIRE_" + REQUEST_TOKEN)
            return result

    def factory(**kwargs):
        w.constructors.append(kwargs)
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        assert kwargs["verify"] is True and kwargs["http2"] is False
        assert 0 < kwargs["timeout"] <= 5
        client = Client(transport=httpx.MockTransport(handle), **kwargs)
        if w.construct_late:
            w.mono += 5
        return client

    monkeypatch.setattr(oidc.httpx, "Client", factory)
    w.acquire = lambda **kw: oidc.make_hosted_oidc_acquirer(
        policy=policy,
        actions_read_token=READ_TOKEN,
        end=kw.get("end", 1000.0),
        original_deadline_at=kw.get("until", w.utc + 1000),
    )
    return w


def assert_sealed(w, acquire):
    calls = len(w.calls)
    already_sealed = acquire._sealed
    with pytest.raises(oidc.HostedOIDCUnknown) as error:
        acquire()
    assert len(w.calls) - calls <= (0 if already_sealed else 1)
    after_first_refusal = len(w.calls)
    with pytest.raises(oidc.HostedOIDCUnknown):
        acquire()
    assert len(w.calls) == after_first_refusal
    rendered = "".join(traceback.format_exception(error.value))
    assert REQUEST_TOKEN not in rendered and READ_TOKEN not in rendered and URL not in rendered


def test_actual_success_preserves_opaque_query_and_caps_raw_validity(world):
    w = world
    acquire = w.acquire()
    result = acquire()
    assert type(result) is HostedGHAPrincipals and result.actions_read_token == READ_TOKEN
    assert result.valid_until == w.utc + 30
    assert len(w.calls) == 1 and w.response_closes == w.client_closes == 1
    expected = URL + "&audience=https%3A%2F%2Fsentinel.digitalfrontier.so%2Flegacy"
    assert str(w.calls[0].url) == expected
    assert w.calls[0].url.raw_path.startswith(
        b"/opaque/job%2Ftoken?api-version=2.0&nonce=a%2Bb&empty=&bare&x=a+b&"
    )
    assert REQUEST_TOKEN not in repr(acquire) and READ_TOKEN not in repr(acquire)
    assert result.oidc not in repr(result)


@pytest.mark.parametrize(
    "url",
    [
        "https://job-service.actions.githubusercontent.com:443/opaque?z=a%2fb",
        "https://job-service.actions.githubusercontent.com/opaque",
        "https://job-service.actions.githubusercontent.com/opaque?",
        URL + "&audience=" + "https%3A%2F%2Fsentinel.digitalfrontier.so%2Flegacy",
    ],
)
def test_admitted_opaque_shapes_preserved_without_audience_duplication(world, monkeypatch, url):
    monkeypatch.setenv(oidc._URL_ENV, url)
    w = world
    acquire = w.acquire()
    acquire()
    assert len(w.calls) == 1
    assert str(w.calls[0].url).count("audience=") == 1
    assert w.calls[0].url.raw_path.startswith(httpx.URL(url).raw_path.rstrip(b"?"))


@pytest.mark.parametrize(
    "url",
    [
        "http://job-service.actions.githubusercontent.com/x",
        "https://actions.githubusercontent.com/x",
        "https://evil.invalid/x",
        "https://job-service.actions.githubusercontent.com.evil.invalid/x",
        "https://a.b.actions.githubusercontent.com/x",
        "https://127.0.0.1/x",
        "https://[::1]/x",
        "https://user@job-service.actions.githubusercontent.com/x",
        "https://job-service.actions.githubusercontent.com:444/x",
        "https://job-service.actions.githubusercontent.com:/x",
        "https://job-service.actions.githubusercontent.com/x#fragment",
        "https://job-service.actions.githubusercontent.com",
        "https://job-service.actions.githubusercontent.com//other",
        "https://job-service.actions.githubusercontent.com/a/../x",
        "https://job-service.actions.githubusercontent.com/x?bad=%Q0",
        "https://job-service.actions.githubusercontent.com/x?audience=foreign",
        URL + "&audience=" + oidc.quote(AUD, safe="") + "&%61udience=" + oidc.quote(AUD, safe=""),
        "https://Job-Service.actions.githubusercontent.com/x",
    ],
)
def test_bad_environment_endpoint_refuses_before_client_or_wire(world, monkeypatch, url):
    monkeypatch.setenv(oidc._URL_ENV, url)
    with pytest.raises(oidc.HostedOIDCUnknown):
        world.acquire()
    assert not world.calls and not world.constructors


@pytest.mark.parametrize(
    "kind",
    [
        "no_policy",
        "foreign_policy",
        "no_request_permission",
        "no_read",
        "expired_end",
        "expired_UTC",
        "bool_lifetime",
    ],
)
def test_missing_source_permission_or_original_bounds_have_no_fallback(world, monkeypatch, kind):
    w = world
    if kind == "no_policy":
        monkeypatch.setattr(oidc, "_reviewed_hosted_oidc_policy", lambda: None)
    elif kind == "foreign_policy":
        monkeypatch.setattr(
            oidc, "_reviewed_hosted_oidc_policy", lambda: oidc.HostedOIDCPolicy(AUD)
        )
    elif kind == "no_request_permission":
        monkeypatch.delenv(oidc._REQUEST_CREDENTIAL_NAME)
    elif kind == "bool_lifetime":
        monkeypatch.setattr(
            oidc, "_reviewed_hosted_oidc_policy", lambda: oidc.HostedOIDCPolicy(AUD, True)
        )
    with pytest.raises(oidc.HostedOIDCUnknown):
        if kind == "no_read":
            oidc.make_hosted_oidc_acquirer(
                policy=w.policy,
                actions_read_token=None,
                end=1000.0,
                original_deadline_at=w.utc + 1000,
            )
        else:
            w.acquire(
                end=w.mono if kind == "expired_end" else 1000.0,
                until=w.utc if kind == "expired_UTC" else w.utc + 1000,
            )
    assert not w.calls and not w.constructors


@pytest.mark.parametrize("change", ["url", "token", "policy"])
def test_environment_and_policy_snapshot_cannot_change_before_call(world, monkeypatch, change):
    w = world
    acquire = w.acquire()
    if change == "url":
        monkeypatch.setenv(oidc._URL_ENV, URL + "&later=changed")
    elif change == "token":
        monkeypatch.setenv(oidc._REQUEST_CREDENTIAL_NAME, "CHANGED")
    else:
        monkeypatch.setattr(oidc, "_reviewed_hosted_oidc_policy", lambda: None)
    assert_sealed(w, acquire)
    assert not w.calls and not w.constructors


@pytest.mark.parametrize(
    "case",
    [
        "forbidden",
        "redirect",
        "foreign_final",
        "duplicate_JSON",
        "truncated",
        "nonfinite",
        "wrong_value",
        "extra_field",
        "bad_JWT",
        "invalid_UTF8",
        "oversize",
        "missing_length",
        "duplicate_header",
        "Link",
        "malformed_header",
        "large_header",
        "length_mismatch",
    ],
)
def test_actual_transport_refusals_are_terminal_one_get(world, case):
    w = world
    if case == "forbidden":
        w.status = 403
    elif case == "redirect":
        w.status = 302
        w.headers = [(b"location", b"https://foreign.invalid/secret")]
    elif case == "foreign_final":
        w.foreign_url = True
    elif case == "duplicate_JSON":
        w.body = b'{"value":"x","value":"y"}'
    elif case == "truncated":
        w.body = b'{"value":'
    elif case == "nonfinite":
        w.body = b'{"value":NaN}'
    elif case == "wrong_value":
        w.body = b'{"value":true}'
    elif case == "extra_field":
        w.body = json.dumps({"value": token(w.utc), "other": 1}).encode()
    elif case == "bad_JWT":
        w.body = b'{"value":"x.y.z"}'
    elif case == "invalid_UTF8":
        w.body = b'{"value":"\xff"}'
    elif case == "oversize":
        w.body = b"x" * (oidc.MAX_BODY + 1)
        w.headers = [(b"content-type", b"application/json"), (b"content-length", b"1")]
    elif case == "missing_length":
        w.headers = [(b"content-type", b"application/json")]
    elif case == "duplicate_header":
        w.headers = [
            (b"content-type", b"application/json"),
            (b"content-length", b"1"),
            (b"Content-Length", b"1"),
        ]
    elif case == "Link":
        w.headers = [
            (b"content-type", b"application/json"),
            (b"content-length", b"1"),
            (b"Link", b"malformed"),
        ]
    elif case == "malformed_header":
        w.headers = [(b"bad name", b"bad\r\nvalue")]
    elif case == "large_header":
        w.headers = [(b"x-long", b"a" * oidc.MAX_HEADERS)]
    elif case == "length_mismatch":
        w.headers = [(b"content-type", b"application/json"), (b"content-length", b"1")]
    acquire = w.acquire()
    assert_sealed(w, acquire)
    assert len(w.calls) == 1
    assert w.response_closes == w.client_closes == 1
    assert_sealed(w, acquire)


@pytest.mark.parametrize(
    "changes",
    [
        {"aud": "other"},
        {"iss": "https://foreign.invalid"},
        {"iat": True},
        {"iat": 1800000001},
        {"iat": 1799999969},
        {"nbf": 1800000001},
        {"exp": 1800000000},
    ],
)
def test_local_JWT_screen_is_strict_freshness_not_signature_authority(world, changes):
    w = world
    w.body = json.dumps({"value": token(w.utc, claims=changes)}).encode()
    acquire = w.acquire()
    assert_sealed(w, acquire)
    assert len(w.calls) == 1


@pytest.mark.parametrize(
    "kind",
    [
        "construct_late",
        "body_late",
        "close_late",
        "client_late",
        "body_error",
        "close_error",
        "client_error",
        "env_change",
    ],
)
def test_all_late_or_ambiguous_retirements_seal_without_refresh(world, kind):
    w = world
    setattr(w, kind, True)
    acquire = w.acquire()
    assert_sealed(w, acquire)
    assert len(w.calls) == (0 if kind == "construct_late" else 1)
    assert w.client_closes == 1
    assert_sealed(w, acquire)


def test_original_end_shrinks_5s_timeout_and_caps_returned_raw_epoch(world):
    w = world
    acquire = w.acquire(end=w.mono + 2, until=w.utc + 9)
    result = acquire()
    assert w.constructors[0]["timeout"] == 2
    assert result.valid_until == w.utc + 9
    w.mono += 2
    assert_sealed(w, acquire)
    assert len(w.calls) == 1


def test_exact19_operation_budget_is_not_per_phase_or_refilled(world):
    w = world
    acquire = w.acquire()
    for _ in range(19):
        acquire()
    assert len(w.calls) == 19 and acquire._calls == 19
    assert_sealed(w, acquire)
    assert len(w.calls) == 19
    assert_sealed(w, acquire)


@pytest.mark.parametrize("fail", [False, True])
def test_real_root_logging_handlers_hide_URL_headers_restore_other_logs(world, monkeypatch, fail):
    w = world
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    root = logging.getLogger()
    root.addHandler(handler)
    loggers = [
        logging.getLogger("httpx"),
        logging.getLogger("httpcore.connection"),
        logging.getLogger("httpcore.late-created-child"),
        logging.getLogger("owned.other"),
    ]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.DEBUG)
    originals = {id(x): tuple(x.filters) for x in [root, handler, *loggers]}

    def during():
        logging.getLogger("owned.other").warning("other-namespace-retained")
        record = logging.LogRecord(
            "httpcore.connection", logging.WARNING, "public", 1, "other-thread-retained", (), None
        )
        record.thread = -1
        logging.getLogger("httpcore.connection").handle(record)

    w.response_hook = during
    if fail:
        w.client_error = True
    try:
        acquire = w.acquire()
        if fail:
            assert_sealed(w, acquire)
        else:
            acquire()
        captured = output.getvalue()
        assert "other-namespace-retained" in captured and "other-thread-retained" in captured
        assert (
            URL not in captured
            and REQUEST_TOKEN not in captured
            and "HTTP Request" not in captured
        )
        for target in [root, handler, *loggers]:
            assert tuple(target.filters) == originals[id(target)]
    finally:
        root.removeHandler(handler)
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)


def test_overlapping_acquisition_seals_both_without_second_wire(world):
    w = world
    acquire = w.acquire()

    def overlapping():
        with pytest.raises(oidc.HostedOIDCUnknown):
            acquire()

    w.response_hook = overlapping
    assert_sealed(w, acquire)
    assert len(w.calls) == 1 and acquire._calls == 1
    assert w.response_closes == w.client_closes == 1
