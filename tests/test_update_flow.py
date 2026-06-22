"""Tests for deploy.update() (in-place PUT) and the shared _prepare_sdl_content."""

from unittest.mock import patch

import pytest

from just_akash.deploy import _prepare_sdl_content, update

SDL_YAML = """
version: "2.0"
services:
  web:
    image: python:3.13-slim
    expose:
      - port: 80
        as: 80
        to:
          - global: true
"""

SDL_WITH_SSH_PLACEHOLDER = SDL_YAML.replace(
    "image: python:3.13-slim",
    "image: python:3.13-slim\n    env:\n      - SSH_PUBKEY_B64=PLACEHOLDER_SSH_PUBKEY_B64",
)


# ── _prepare_sdl_content ─────────────────────────────────────────────


class TestPrepareSdlContent:
    def test_reads_and_returns(self, tmp_path):
        f = tmp_path / "s.yaml"
        f.write_text(SDL_YAML)
        out = _prepare_sdl_content(str(f))
        assert "python:3.13-slim" in out

    def test_missing_file_raises(self):
        with pytest.raises(RuntimeError, match="SDL file not found"):
            _prepare_sdl_content("/nope/missing.yaml")

    def test_invalid_yaml_raises(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text("::: not yaml :::\n- [")
        with pytest.raises(RuntimeError):
            _prepare_sdl_content(str(f))

    def test_image_override(self, tmp_path):
        f = tmp_path / "s.yaml"
        f.write_text(SDL_YAML)
        out = _prepare_sdl_content(str(f), image="myrepo/app:v2")
        assert "image: myrepo/app:v2" in out
        assert "python:3.13-slim" not in out

    def test_env_injection(self, tmp_path):
        f = tmp_path / "s.yaml"
        f.write_text(SDL_YAML)
        out = _prepare_sdl_content(str(f), env_vars=["FOO=bar"])
        assert "FOO=bar" in out

    def test_ssh_placeholder_without_key_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SSH_PUBKEY", raising=False)
        f = tmp_path / "s.yaml"
        f.write_text(SDL_WITH_SSH_PLACEHOLDER)
        with pytest.raises(RuntimeError, match="SSH_PUBKEY"):
            _prepare_sdl_content(str(f))

    def test_ssh_placeholder_with_key_injects_base64(self, tmp_path, monkeypatch):
        import base64

        monkeypatch.setenv("SSH_PUBKEY", "ssh-ed25519 AAAAKEY")
        f = tmp_path / "s.yaml"
        f.write_text(SDL_WITH_SSH_PLACEHOLDER)
        out = _prepare_sdl_content(str(f))
        assert "PLACEHOLDER_SSH_PUBKEY_B64" not in out
        assert base64.b64encode(b"ssh-ed25519 AAAAKEY").decode() in out


# ── update() ─────────────────────────────────────────────────────────


class TestUpdate:
    def test_requires_api_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AKASH_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="AKASH_API_KEY"):
            update(dseq="123", sdl_path=str(tmp_path / "s.yaml"))

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_update_calls_put_with_prepared_sdl(self, MockAPI, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        f = tmp_path / "s.yaml"
        f.write_text(SDL_YAML)
        client = MockAPI.return_value
        client.update_deployment.return_value = {"dseq": "123", "state": "active"}

        result = update(dseq="123", sdl_path=str(f), image="repo/app:v3")

        assert result["dseq"] == "123"
        client.update_deployment.assert_called_once()
        called_dseq, called_sdl = client.update_deployment.call_args[0]
        assert called_dseq == "123"
        assert "image: repo/app:v3" in called_sdl

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_update_wraps_api_error(self, MockAPI, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        f = tmp_path / "s.yaml"
        f.write_text(SDL_YAML)
        client = MockAPI.return_value
        client.update_deployment.side_effect = RuntimeError("API Error (409): conflict")

        with pytest.raises(RuntimeError, match="Failed to update deployment 123"):
            update(dseq="123", sdl_path=str(f))

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_update_invalid_sdl_rejected_before_api(self, MockAPI, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        f = tmp_path / "bad.yaml"
        f.write_text("not: [valid")
        client = MockAPI.return_value
        with pytest.raises(RuntimeError):
            update(dseq="123", sdl_path=str(f))
        client.update_deployment.assert_not_called()
