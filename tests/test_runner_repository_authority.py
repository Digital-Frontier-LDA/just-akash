"""Fixed hosted repository identity and registration authority without raw responses."""

import copy
import io
import traceback
import urllib.error
from datetime import datetime, timedelta, timezone
from http.client import HTTPMessage
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
