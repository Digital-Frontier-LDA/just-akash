"""Local recovery is retained even when the durable response ACK is unavailable."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_scanner_jit_v2 import API, Recorder  # noqa: E402
from test_scanner_v2_controller import environment  # noqa: E402

from just_akash import runner_image, scanner_lease_provenance  # noqa: E402
from just_akash import scanner_v2_controller as subject  # noqa: E402
from just_akash.scanner_jit_v2 import mint_scanner_jit  # noqa: E402


def test_returned_candidate_survives_later_ack_failure_but_secret_sdl_is_removed(
    monkeypatch, tmp_path
):
    recorder = Recorder()
    api = API(recorder)
    paths = {}
    module = types.ModuleType("just_akash.scanner_durable_pipe")
    module.PipeDurableRecorder = lambda: recorder
    monkeypatch.setitem(sys.modules, "just_akash.scanner_durable_pipe", module)
    monkeypatch.setattr(subject, "admit_native", lambda *args: None)
    monkeypatch.setattr(
        subject, "mint_scanner_jit", lambda b, t, **kw: mint_scanner_jit(b, t, request=api, **kw)
    )
    monkeypatch.setattr(
        runner_image, "read_pull_credentials", lambda *args, **kw: ("jobordu", "PUBLIC_READER")
    )
    monkeypatch.setattr(runner_image, "verify_native_reader_role", lambda *args: None)

    def deployment(**kwargs):
        paths["sdl"] = Path(kwargs["sdl_path"])
        paths["receipt"] = Path(kwargs["receipt_path"])
        paths["receipt"].write_text(
            json.dumps(dict(state="candidate", dseq="456", owner="PUBLIC_RESOLVED_OWNER"))
        )
        assert paths["sdl"].exists()
        raise RuntimeError("PUBLIC_SYNTHETIC_LATE_ACK_FAILURE")

    monkeypatch.setattr(scanner_lease_provenance, "deploy_scanner_v2", deployment)
    bundle = tmp_path / "synthetic-reader"
    bundle.write_text("PUBLIC_FIXTURE_NOT_CIPHERTEXT")
    env = environment()
    env.update(
        GH_TOKEN="PUBLIC_HOSTED_TOKEN",
        AKASH_API_KEY="PUBLIC_API_KEY",
        SOPS_AGE_KEY="PUBLIC_AGE_KEY",
        SCANNER_REGISTRY_SOPS_PATH=str(bundle),
        RUNNER_TEMP=str(tmp_path),
    )
    with pytest.raises(RuntimeError, match="PUBLIC_SYNTHETIC_LATE_ACK_FAILURE"):
        subject.provision(env)
    assert paths["receipt"].is_file(), (
        "A known returned deployment candidate must not be erased on ACK failure"
    )
    assert json.loads(paths["receipt"].read_text())["dseq"] == "456"
    assert not paths["sdl"].exists(), "Encoded JIT and registry credential SDL must be removed"
    assert [r["phase"] for r in recorder.records] == ["NO_WIRE", "UNKNOWN", "ACK"]
