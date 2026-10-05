"""Fresh issuer roles are required before masking and before native delivery."""

import base64
import json
import traceback
from http.client import BadStatusLine, HTTPMessage, IncompleteRead
from io import BytesIO
from urllib.error import HTTPError

import pytest

from just_akash import runner_image as image
from tests.test_runner_native_reader import TOKEN, decrypt, options, scope, template

ROLE_GATE = image.verify_native_reader_role
ECHO = "UNTRUSTEDROLECANARY"


def encode(value):
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def session(claims, *, header=None, signature=b"fixture-signature"):
    return ".".join((encode(header or {"alg": "HS256"}), encode(claims), encode(signature)))


class Response:
    status = 200

    def __init__(self, raw, status=200):
        self.raw, self.status, self.reads = raw, status, []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, limit):
        self.reads.append(limit)
        if isinstance(self.raw, BaseException):
            raise self.raw
        return self.raw


def issuer(monkeypatch, *, claims=None, raw=None, status=200, error=None, responses=None):
    if raw is None:
        raw = json.dumps({"access_token": session(claims or {"scope": "repo:read"})}).encode()
    pending = list(responses) if responses is not None else [Response(raw, status)]
    calls = []

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request, timeout))
            if error is not None:
                raise error
            return pending.pop(0)

    def build(handler):
        assert isinstance(handler, image._GroupNoRedirect)
        assert (
            handler.redirect_request(
                image.urllib.request.Request("https://hub.docker.com/v2/auth/token"),
                BytesIO(),
                302,
                "Found",
                HTTPMessage(),
                "https://evil.test",
            )
            is None
        )
        return Opener()

    monkeypatch.setattr(image.urllib.request, "build_opener", build)
    return calls


@pytest.mark.parametrize(
    "claims",
    [
        {"scope": "repo:read"},
        {"scopes": ["repo:read"]},
        {"access_token_scope": "repo:read"},
        {"scope": "repo:read", "scopes": ["repo:read"]},
    ],
)
def test_exact_fresh_issuer_reader_role_uses_only_fixed_bounded_post(monkeypatch, claims):
    calls = issuer(monkeypatch, claims=claims)
    ROLE_GATE("jobordu", TOKEN)
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == "https://hub.docker.com/v2/auth/token"
    assert request.get_method() == "POST" and timeout == 20
    assert json.loads(request.data) == {"identifier": "jobordu", "secret": TOKEN}
    assert set(request.headers) == {"Content-type", "Accept"}


@pytest.mark.parametrize(
    "claims",
    [
        {"scope": "repo:read repo:write"},
        {"scope": "repo:read repo:admin"},
        {"scope": "repo:write"},
        {"scope": "repo:admin"},
        {"scope": "repo:read", "scopes": ["repo:write"]},
        {"scope": "repo:read", "access_token_scope": ["repo:admin"]},
        {"scope": "repo:read", "scopes": True},
        {"scope": "repo:read", "scopes": None},
        {"scope": "repo:read", "scopes": []},
        {"scope": "repo:read", "scopes": [1]},
        {"scope": "repo:read", "access_token_scope": {}},
        {"scope": "repo:read", "access_token_scope": ""},
        {"scope": "repo:read unknown"},
        {},
    ],
)
def test_unknown_writer_admin_and_malformed_present_aliases_fail_quietly(monkeypatch, claims):
    raw = json.dumps({"access_token": session(claims)}).encode()
    issuer(monkeypatch, raw=raw)
    with pytest.raises(image.NativeReaderRoleError) as caught:
        ROLE_GATE("jobordu", TOKEN)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json-" + ECHO.encode(),
        b"{}",
        b'{"access_token":1}',
        b'{"access_token":"bad","access_token":"duplicate"}',
        json.dumps(
            {"access_token": session(b'{"scope":"repo:read","scope":"repo:admin"}')}
        ).encode(),
        json.dumps(
            {"access_token": session({"scope": "repo:read"}, header={"alg": "none"})}
        ).encode(),
        json.dumps({"access_token": session({"scope": "repo:read"}, signature=b"")}).encode(),
        json.dumps({"access_token": "a.b.c.extra"}).encode(),
        json.dumps({"access_token": "A" * 49155}).encode(),
        b"X" * 65537,
        IncompleteRead(ECHO.encode(), 100),
        BadStatusLine(ECHO),
    ],
)
def test_malformed_bounded_issuer_response_never_escapes_body_or_context(monkeypatch, raw):
    issuer(monkeypatch, raw=raw)
    with pytest.raises(image.NativeReaderRoleError) as caught:
        ROLE_GATE("jobordu", TOKEN)
    formatted = "".join(traceback.format_exception(caught.value))
    assert ECHO not in formatted and TOKEN not in formatted
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize("status", [201, 301, 302, 401, 403, 429, 500])
def test_non_success_issuer_status_has_no_role_authority(monkeypatch, status):
    issuer(monkeypatch, status=status)
    with pytest.raises(image.NativeReaderRoleError):
        ROLE_GATE("jobordu", TOKEN)


@pytest.mark.parametrize(
    "error",
    [
        OSError(ECHO),
        BadStatusLine(ECHO),
        HTTPError("https://hub.docker.com/v2/auth/token", 302, ECHO, HTTPMessage(), None),
    ],
)
def test_network_redirect_and_header_errors_are_fixed_without_remote_context(monkeypatch, error):
    issuer(monkeypatch, error=error)
    with pytest.raises(image.NativeReaderRoleError) as caught:
        ROLE_GATE("jobordu", TOKEN)
    assert ECHO not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize("scopes", ["repo:read repo:write", "repo:read repo:admin"])
def test_writer_or_admin_sops_bundle_is_rejected_before_mask_and_template_mutation(
    tmp_path, monkeypatch, capsys, scopes
):
    scope(monkeypatch)
    monkeypatch.setattr(image, "verify_native_reader_role", ROLE_GATE)
    cipher, _ = decrypt(tmp_path, monkeypatch)
    calls = issuer(monkeypatch, claims={"scope": scopes})
    path = template(tmp_path)
    before = path.read_bytes()
    monkeypatch.setenv("RUNNER_REGISTRY_PASSWORD", "")
    monkeypatch.setattr(
        "sys.argv", ["runner_image", "--sdl", str(path), "--sops-env-file", str(cipher)]
    )
    assert image.main() == 1
    assert path.read_bytes() == before and len(calls) == 1
    output = capsys.readouterr().out
    assert "NATIVE_READER_ROLE_UNQUALIFIED" in output and "::add-mask::" not in output
    assert TOKEN not in output


def test_reader_role_change_before_final_insertion_leaves_template_unchanged(
    tmp_path, monkeypatch
):
    scope(monkeypatch)
    monkeypatch.setattr(image, "verify_native_reader_role", ROLE_GATE)
    cipher, _ = decrypt(tmp_path, monkeypatch)
    calls = issuer(
        monkeypatch,
        responses=[
            Response(json.dumps({"access_token": session({"scope": "repo:read"})}).encode()),
            Response(
                json.dumps({"access_token": session({"scope": "repo:read repo:write"})}).encode()
            ),
        ],
    )
    path = template(tmp_path)
    before = path.read_bytes()
    username, token = image.read_pull_credentials(
        cipher, username="jobordu", password="", native_reader=True
    )
    with pytest.raises(image.NativeReaderRoleError):
        image.configure(
            path,
            image=options()["image"],
            host=options()["host"],
            username=username,
            password=token,
            native_reader=True,
            reader_from_sops=True,
        )
    assert len(calls) == 2 and path.read_bytes() == before


def test_direct_native_configure_cannot_bypass_fresh_role_authority(tmp_path, monkeypatch):
    scope(monkeypatch)
    monkeypatch.setattr(image, "verify_native_reader_role", ROLE_GATE)
    calls = issuer(monkeypatch, claims={"scope": "repo:read repo:admin"})
    path = template(tmp_path)
    before = path.read_bytes()
    with pytest.raises(image.NativeReaderRoleError):
        image.configure(path, **options(), native_reader=True, reader_from_sops=True)
    assert len(calls) == 1 and path.read_bytes() == before


def test_sdl_only_configuration_never_invokes_new_role_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(
        image, "verify_native_reader_role", lambda *_: pytest.fail("role gate reached off mode")
    )
    path = template(tmp_path)
    image.configure(path, **options())
    assert json.loads(json.dumps(options()))["image"] in path.read_text()


def test_actual_sops_native_delivery_requires_two_fresh_reader_authorities(
    tmp_path, monkeypatch, capsys
):
    scope(monkeypatch)
    monkeypatch.setattr(image, "verify_native_reader_role", ROLE_GATE)
    cipher, _ = decrypt(tmp_path, monkeypatch)
    reader = json.dumps({"access_token": session({"scope": "repo:read"})}).encode()
    calls = issuer(monkeypatch, responses=[Response(reader), Response(reader)])
    path = template(tmp_path)
    monkeypatch.setenv("RUNNER_REGISTRY_PASSWORD", "")
    monkeypatch.setattr(
        "sys.argv", ["runner_image", "--sdl", str(path), "--sops-env-file", str(cipher)]
    )
    assert image.main() == 0
    assert len(calls) == 2
    import yaml

    runner = yaml.safe_load(path.read_text())["services"]["runner"]
    fields = dict(value.split("=", 1) for value in runner["env"])
    assert fields["DOCKERHUB_PULL_USERNAME"] == "jobordu"
    assert fields["DOCKERHUB_PULL_TOKEN"] == TOKEN
    assert runner["credentials"]["password"] == TOKEN
    assert path.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().out == "::add-mask::" + TOKEN.replace("%", "%25") + "\n"


def test_foreign_reader_identity_never_reaches_issuer(monkeypatch):
    calls = issuer(monkeypatch)
    with pytest.raises(image.NativeReaderRoleError):
        ROLE_GATE("other", TOKEN)
    assert calls == []
