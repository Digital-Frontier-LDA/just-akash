"""Fixed hosted repository identity and registration authority without raw responses."""

import copy
import io
import json
import traceback
import urllib.error
from datetime import datetime, timedelta, timezone
from http.client import BadStatusLine, HTTPMessage, IncompleteRead
from unittest.mock import Mock

import pytest

from just_akash import runner_repository as repository

REPO = {"id": 1074974924, "full_name": "Borduas-Holdings/blazing", "private": True}


@pytest.mark.parametrize("registration", [False, True])
def test_transport_is_fixed_no_redirect_and_role_only(monkeypatch, registration):
    monkeypatch.setenv("GH_TOKEN", "PATCANARY")
    response = Mock()
    response.status = 201 if registration else 200
    response.read.return_value = b"{}"
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=None)
    opener = Mock()
    opener.open.return_value = response
    builder = Mock(return_value=opener)
    monkeypatch.setattr(repository.urllib.request, "build_opener", builder)
    assert repository._native_repository_request(registration=registration) == {}
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://api.github.com/repos/Borduas-Holdings/blazing" + (
        "/actions/runners/registration-token" if registration else ""
    )
    assert request.method == ("POST" if registration else "GET")
    assert request.get_header("Authorization") == "Bearer PATCANARY"
    assert isinstance(builder.call_args.args[0], repository._GroupNoRedirect)
    assert opener.open.call_args.kwargs["timeout"] == 20


@pytest.mark.parametrize("raw", [b'{"id":1,"id":2}', b"[]", b"not-json", b" " * (1024 * 1024 + 1)])
def test_untrusted_repository_response_fails_closed(monkeypatch, raw):
    monkeypatch.setenv("GH_TOKEN", "PATCANARY")
    response = Mock(status=200)
    response.read.return_value = raw
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=None)
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(repository.urllib.request, "build_opener", Mock(return_value=opener))
    with pytest.raises(repository.NativeReaderRepositoryError):
        repository._native_repository_request()


def authority(monkeypatch, *, identity=None, grant=None):
    calls = []
    document = {
        "token": "REGISTRATIONCANARY",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    if grant is not None:
        document = grant

    def request(*, registration=False):
        calls.append(registration)
        return copy.deepcopy(
            document if registration else identity if identity is not None else REPO
        )

    monkeypatch.setattr(repository, "_native_repository_request", request)
    return calls


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.HTTPError(
            "https://echo-canary", 403, "echo-canary", HTTPMessage(), io.BytesIO(b"echo-canary")
        ),
        BadStatusLine("echo-canary"),
        OSError("echo-canary"),
        TimeoutError("echo-canary"),
    ],
)
def test_transport_errors_withhold_bodies_causes_and_credentials(monkeypatch, failure):
    monkeypatch.setenv("GH_TOKEN", "PATCANARY")
    opener = Mock()
    opener.open.side_effect = failure
    monkeypatch.setattr(repository.urllib.request, "build_opener", Mock(return_value=opener))
    with pytest.raises(repository.NativeReaderRepositoryError) as caught:
        repository._native_repository_request(registration=True)
    rendered = "".join(traceback.format_exception(caught.value))
    assert "echo-canary" not in rendered and "PATCANARY" not in rendered
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "grant",
    [
        {},
        {"token": "x", "expires_at": "2000-01-01T00:00:00Z"},
        {"token": "x", "expires_at": "2099-01-01T00:00:00Z"},
        {"token": "echo\ncanary", "expires_at": "2099-01-01T00:00:00Z"},
        {"token": "x", "expires_at": "echo-canary"},
        {"token": True, "expires_at": "2099-01-01T00:00:00Z"},
        {"token": "x" * 4097, "expires_at": "2099-01-01T00:00:00Z"},
    ],
)
def test_read_access_without_valid_registration_post_is_held(monkeypatch, grant):
    authority(monkeypatch, grant=grant)
    with pytest.raises(repository.NativeReaderRepositoryError):
        repository.verify_native_reader_repository()


@pytest.mark.parametrize(
    "patch",
    [
        {"private": False},
        {"private": None},
        {"id": True},
        {"id": 1071436278},
        {"full_name": "Borduas-Holdings/Blazing-Back"},
        {"full_name": "Other/blazing"},
    ],
)
def test_repository_identity_refusal_never_requests_token(monkeypatch, patch):
    calls = authority(monkeypatch, identity=REPO | patch)
    with pytest.raises(repository.NativeReaderRepositoryError):
        repository.verify_native_reader_repository()
    assert calls == [False]


def test_bounded_positive_registration_grant_is_accepted(monkeypatch):
    calls = authority(monkeypatch)
    repository.verify_native_reader_repository()
    assert calls == [False, True, False]


def test_partial_registration_response_is_withheld_without_exception_context(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "PATCANARY")
    response = Mock(status=201)
    response.read.side_effect = IncompleteRead(b'{"token":"PARTIALRESPONSECANARY"}', 100)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=None)
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(repository.urllib.request, "build_opener", Mock(return_value=opener))
    with pytest.raises(repository.NativeReaderRepositoryError) as caught:
        repository._native_repository_request(registration=True)
    rendered = "".join(traceback.format_exception(caught.value))
    assert "PARTIALRESPONSECANARY" not in rendered and "PATCANARY" not in rendered
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "change", [{"private": False}, {"id": 1071436278}, {"full_name": "Other/blazing"}]
)
def test_repository_changed_during_mint_is_held(monkeypatch, change):
    calls = []
    grant = {
        "token": "REGISTRATIONCANARY",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }

    def request(*, registration=False):
        calls.append(registration)
        if registration:
            return grant
        return REPO if len(calls) == 1 else REPO | change

    monkeypatch.setattr(repository, "_native_repository_request", request)
    with pytest.raises(repository.NativeReaderRepositoryError):
        repository.verify_native_reader_repository()
    assert calls == [False, True, False]


@pytest.mark.parametrize(
    "expiry",
    [
        "2026-01-02T00:30:00Z",
        "2026-01-02T00:30:00+00:00",
        "2026-01-02T00:30:00-00:00",
        "2026-01-02T06:00:00+05:30",
        "2026-01-01T16:30:00-08:00",
        "2026-01-03T00:29:00+23:59",
        "2026-01-02T00:30:00.123Z",
        "2026-01-02T06:00:00.123456789+05:30",
    ],
)
def test_rfc3339_expiries_represent_the_same_bounded_utc_grant(expiry):
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert repository._registration_expiry_valid(expiry, now)
    parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    assert parsed.astimezone(timezone.utc).replace(microsecond=0) == now + timedelta(minutes=30)


@pytest.mark.parametrize(
    "expiry",
    [
        None,
        True,
        1,
        "",
        "echo-canary",
        "2026-01-02T00:30:00",
        "2026-01-02 00:30:00Z",
        "２０２６-01-02T00:30:00Z",
        "2026-01-02T00:30:00Z\necho-canary",
        "2026-01-02T00:30:00.1234567890Z",
        "2026-01-02T00:30:00+24:00",
        "2026-01-02T00:30:00+01:60",
        "2026-01-02T00:30:00+0100",
        "2026-01-02T00:30:60Z",
        "2026-02-30T00:30:00Z",
        "0000-01-02T00:30:00Z",
        "9999-12-31T23:59:59.999999999-23:59",
        "2026-01-02T00:00:00Z",
        "2026-01-01T23:59:59.999999999Z",
        "2026-01-02T01:05:00.000000001Z",
        "2026-01-02T09:05:00.000000001+08:00",
        "2099-01-01T00:00:00Z",
        "x" * 41,
    ],
)
def test_invalid_naive_expired_or_excessive_rfc3339_grants_are_held(expiry):
    assert not repository._registration_expiry_valid(
        expiry, datetime(2026, 1, 2, tzinfo=timezone.utc)
    )


def test_fractional_expiry_preserves_both_exact_ttl_boundaries():
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert repository._registration_expiry_valid("2026-01-02T00:00:00.000000001Z", now)
    assert repository._registration_expiry_valid("2026-01-02T01:05:00Z", now)
    assert not repository._registration_expiry_valid("2026-01-02T01:05:00.000000001Z", now)


@pytest.mark.parametrize(
    "offset",
    [timezone.utc, timezone(timedelta(hours=-8)), timezone(timedelta(hours=5, minutes=30))],
)
def test_actual_fixed_transport_composes_get_post_get_with_offset_expiry(monkeypatch, offset):
    monkeypatch.setenv("GH_TOKEN", "PATCANARY")
    grant = {
        "token": "REGISTRATIONCANARY",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30))
        .astimezone(offset)
        .isoformat(),
    }
    responses = []
    for status, document in [(200, REPO), (201, grant), (200, REPO)]:
        response = Mock(status=status)
        response.read.return_value = json.dumps(document).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        responses.append(response)
    opener = Mock()
    opener.open.side_effect = responses
    monkeypatch.setattr(repository.urllib.request, "build_opener", Mock(return_value=opener))
    repository.verify_native_reader_repository()
    calls = opener.open.call_args_list
    assert len(calls) == 3
    assert [call.args[0].method for call in calls] == ["GET", "POST", "GET"]
    assert [call.args[0].full_url for call in calls] == [
        "https://api.github.com/repos/Borduas-Holdings/blazing",
        "https://api.github.com/repos/Borduas-Holdings/blazing/actions/runners/registration-token",
        "https://api.github.com/repos/Borduas-Holdings/blazing",
    ]


def test_invalid_expiry_echo_is_quiet_and_does_not_skip_to_final_identity(monkeypatch):
    calls = authority(
        monkeypatch, grant={"token": "REGISTRATIONCANARY", "expires_at": "echo-canary"}
    )
    with pytest.raises(repository.NativeReaderRepositoryError) as caught:
        repository.verify_native_reader_repository()
    assert calls == [False, True]
    assert caught.value.__context__ is None
    assert "echo-canary" not in "".join(traceback.format_exception(caught.value))
