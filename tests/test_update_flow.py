"""Tests for deploy.update() (in-place PUT) and the shared _prepare_sdl_content."""

from unittest.mock import patch

import pytest

from just_akash.deploy import _prepare_sdl_content, deploy, update

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

    @pytest.mark.parametrize("bad", ["NOTAPAIR", "=value", "="])
    def test_malformed_env_var_raises(self, tmp_path, bad):
        # No '=' at all, or an empty key (e.g. "=value") both violate KEY=VALUE.
        f = tmp_path / "s.yaml"
        f.write_text(SDL_YAML)
        with pytest.raises(RuntimeError, match="expected KEY=VALUE"):
            _prepare_sdl_content(str(f), env_vars=[bad])

    def test_image_override_does_not_target_comment_line(self, tmp_path):
        # The override regex is `image:\s+[^\n]+` with count=1, so it replaces
        # the FIRST occurrence of "image: " anywhere in the file. A YAML comment
        # that mentions "image:" (e.g. "# bump the image: see notes") sits above
        # the real `image:` key, so the comment gets rewritten and the actual
        # container image is left untouched — the override silently no-ops.
        sdl = """version: "2.0"
services:
  web:
    # remember to bump the image: see release notes
    image: python:3.13-slim
    expose:
      - port: 80
        as: 80
        to:
          - global: true
"""
        f = tmp_path / "s.yaml"
        f.write_text(sdl)
        out = _prepare_sdl_content(str(f), image="myrepo/app:v9")
        # The real container image must be overridden...
        assert "image: python:3.13-slim" not in out
        # ...and the comment line must NOT have been hijacked as the target.
        assert "# remember to bump the image: see release notes" in out

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


class TestUpdateKeepsTheCharterStamp:
    """Review (fallback, a1eab31): deploy() renames a chartered fleet-/flynn- placement to
    `<key>.ch-<id>`. update() from the same SDL file must PUT the SAME group name, or the
    provider rejects the manifest."""

    ID = "ch-0123456789"

    def _sdl(self, tmp_path, key="fleet-serve-qwen"):
        from tests._charter_harness import sdl_text

        f = tmp_path / "s.yaml"
        f.write_text(sdl_text(key))
        return str(f)

    @staticmethod
    def _onchain(name):
        return {"deployment": {"id": {"dseq": "123"}}, "groups": [{"group_spec": {"name": name}}]}

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_the_charter_id_is_read_back_and_restamped(self, MockAPI, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        client.get_deployment.return_value = self._onchain(f"fleet-serve-qwen.{self.ID}")
        update(dseq="123", sdl_path=self._sdl(tmp_path))
        _, sent = client.update_deployment.call_args[0]
        assert f"fleet-serve-qwen.{self.ID}:" in sent
        assert "fleet-serve-qwen:" not in sent

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_an_explicit_charter_id_wins_without_a_read(self, MockAPI, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        update(dseq="123", sdl_path=self._sdl(tmp_path), charter_id=self.ID)
        client.get_deployment.assert_not_called()
        _, sent = client.update_deployment.call_args[0]
        assert sent.count(f"fleet-serve-qwen.{self.ID}:") == 2

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_an_uncharted_lease_is_left_alone(self, MockAPI, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        client.get_deployment.return_value = self._onchain("fleet-serve-qwen")
        update(dseq="123", sdl_path=self._sdl(tmp_path))
        _, sent = client.update_deployment.call_args[0]
        assert ".ch-" not in sent

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_several_charter_ids_refuse_before_the_put(self, MockAPI, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        client.get_deployment.return_value = {
            "groups": [
                {"group_spec": {"name": "fleet-a.ch-0123456789"}},
                {"group_spec": {"name": "fleet-b.ch-abcdefabcd"}},
            ]
        }
        with pytest.raises(RuntimeError, match="several charter ids"):
            update(dseq="123", sdl_path=self._sdl(tmp_path))
        client.update_deployment.assert_not_called()

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_a_malformed_charter_id_is_a_runtime_error_before_the_put(
        self, MockAPI, tmp_path, monkeypatch
    ):
        """Review (fallback, 0759ccb): stamp_charter's ValueError escaped cli.py, which
        catches RuntimeError only, as a traceback."""
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        with pytest.raises(RuntimeError, match="not a charter id") as exc:
            update(dseq="123", sdl_path=self._sdl(tmp_path), charter_id="ch-NOTHEX")
        assert not isinstance(exc.value, ValueError)
        client.update_deployment.assert_not_called()

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_a_placement_not_ours_gets_no_charter_stamp(self, MockAPI, tmp_path, monkeypatch):
        # openmix-me5w: a just-akash- key IS read now, for its run stamp
        # (TestUpdateKeepsTheRunStamp), but never charter-stamped
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        client.get_deployment.return_value = self._onchain(f"fleet-serve-qwen.{self.ID}")
        update(dseq="123", sdl_path=self._sdl(tmp_path, key="just-akash-runner"))
        _, sent = client.update_deployment.call_args[0]
        assert ".ch-" not in sent


class TestUpdateKeepsTheRunStamp:
    """openmix-me5w: deploy() run-stamps every just-akash-* placement key, so on chain the group
    is `<key>.<run>`. update() from the same SDL file must PUT THAT name, not one stamped with its
    own fresh run id, or the provider rejects the manifest (smoke run 41180113005: update failed
    on 3 of 3 providers once #493 moved the probe from `akash` to `just-akash-smoke`)."""

    RUN = "0123456789ab"

    def _sdl(self, tmp_path, key="just-akash-smoke"):
        from tests._charter_harness import sdl_text

        f = tmp_path / "s.yaml"
        f.write_text(sdl_text(key))
        return str(f)

    @staticmethod
    def _onchain(*names):
        return {
            "deployment": {"id": {"dseq": "123"}},
            "groups": [{"group_spec": {"name": n}} for n in names],
        }

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_the_run_id_is_read_back_and_restamped_at_both_occurrences(
        self, MockAPI, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        client.get_deployment.return_value = self._onchain(f"just-akash-smoke.{self.RUN}")
        update(dseq="123", sdl_path=self._sdl(tmp_path))
        _, sent = client.update_deployment.call_args[0]
        # profiles.placement.<KEY> and deployment.<svc>.<KEY> both move
        assert sent.count(f"just-akash-smoke.{self.RUN}:") == 2
        assert "just-akash-smoke:" not in sent

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_a_lease_minted_before_run_scoping_is_left_alone(self, MockAPI, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        client.get_deployment.return_value = self._onchain("just-akash-smoke")
        update(dseq="123", sdl_path=self._sdl(tmp_path))
        _, sent = client.update_deployment.call_args[0]
        assert sent.count("just-akash-smoke:") == 2

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_another_keys_run_id_is_not_borrowed(self, MockAPI, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        client.get_deployment.return_value = self._onchain(f"just-akash-runner.{self.RUN}")
        update(dseq="123", sdl_path=self._sdl(tmp_path))
        _, sent = client.update_deployment.call_args[0]
        assert self.RUN not in sent

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_several_run_ids_refuse_before_the_put(self, MockAPI, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        client.get_deployment.return_value = self._onchain(
            f"just-akash-smoke.{self.RUN}", "just-akash-smoke.abcdefabcdef"
        )
        with pytest.raises(RuntimeError, match="several run ids"):
            update(dseq="123", sdl_path=self._sdl(tmp_path))
        client.update_deployment.assert_not_called()

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_an_unreadable_deployment_refuses_before_the_put(self, MockAPI, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        client.get_deployment.side_effect = RuntimeError("HTTP 503")
        with pytest.raises(RuntimeError, match="recover its run id"):
            update(dseq="123", sdl_path=self._sdl(tmp_path))
        client.update_deployment.assert_not_called()

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_a_placement_without_our_prefix_is_never_read(self, MockAPI, tmp_path, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        client = MockAPI.return_value
        update(dseq="123", sdl_path=self._sdl(tmp_path, key="dfci-runner"))
        client.get_deployment.assert_not_called()


class TestDeployDepositGuard:
    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_nan_deposit_rejected_before_api(self, MockAPI, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        with pytest.raises(RuntimeError, match="Invalid deposit"):
            deploy(sdl_path="ignored.yaml", deposit=float("nan"))
        MockAPI.return_value.create_deployment.assert_not_called()

    @patch("just_akash.deploy.AkashConsoleAPI")
    def test_non_positive_deposit_rejected(self, MockAPI, monkeypatch):
        monkeypatch.setenv("AKASH_API_KEY", "k")
        with pytest.raises(RuntimeError, match="Invalid deposit"):
            deploy(sdl_path="ignored.yaml", deposit=0)
        MockAPI.return_value.create_deployment.assert_not_called()


class TestResolveSdlPath:
    def test_gpu_uses_variant_when_present(self, tmp_path):
        from just_akash.deploy import _resolve_sdl_path

        (tmp_path / "app.yaml").write_text("base")
        (tmp_path / "app-gpu.yaml").write_text("gpu")
        assert _resolve_sdl_path(str(tmp_path / "app.yaml"), gpu=True) == str(
            tmp_path / "app-gpu.yaml"
        )

    def test_gpu_falls_back_when_no_variant(self, tmp_path):
        from just_akash.deploy import _resolve_sdl_path

        (tmp_path / "app.yaml").write_text("base")
        base = str(tmp_path / "app.yaml")
        assert _resolve_sdl_path(base, gpu=True) == base

    def test_no_gpu_returns_original(self):
        from just_akash.deploy import _resolve_sdl_path

        assert _resolve_sdl_path("/x/app.yaml", gpu=False) == "/x/app.yaml"
