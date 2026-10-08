"""Installed capability checks do not authenticate financial effect authority."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "release_wheel_contract",
    Path(__file__).parents[1] / ".github/scripts/verify_release_wheel_contract.py",
)
assert SPEC is not None and SPEC.loader is not None
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def test_actual_installed_dependency_financial_data_contract():
    m.verify(m.metadata.version("just-akash"))


def test_wrong_sdk_release_metadata_is_refused():
    with pytest.raises(RuntimeError, match="SDK release version mismatch"):
        m.verify("0.0.0-inert-fixture")


@pytest.mark.parametrize("version", ["0.17.0", "0.18.1", "0.18.0-inert-fixture"])
def test_wrong_core_release_metadata_is_refused(monkeypatch, version):
    actual_version = m.metadata.version
    monkeypatch.setattr(
        m.metadata,
        "version",
        lambda package: version if package == "akash-lease-core" else actual_version(package),
    )
    with pytest.raises(RuntimeError, match="core release version mismatch"):
        m.verify(actual_version("just-akash"))


def test_changed_loaded_core_version_is_refused(monkeypatch):
    monkeypatch.setattr(m.core, "__version__", "0.17.0")
    with pytest.raises(RuntimeError, match="core release version mismatch"):
        m.verify(m.metadata.version("just-akash"))


def test_missing_execution_adapter_is_refused(monkeypatch):
    monkeypatch.setattr(m, "observe_execution", None)
    with pytest.raises(RuntimeError, match="execution adapter missing"):
        m.verify(m.metadata.version("just-akash"))


def test_missing_transaction_fee_data_purpose_is_refused(monkeypatch):
    monkeypatch.setattr(m.core, "FinancialEffectPurpose", type("MissingFeePurpose", (), {}))
    with pytest.raises(RuntimeError, match="transaction-fee DATA purpose missing"):
        m.verify(m.metadata.version("just-akash"))


def test_noncanonical_decoder_cannot_pass_release_check(monkeypatch):
    actual_decode = m.core.decode_financial_data

    def weakened_decode(raw, expected):
        if raw.endswith(b" "):
            return actual_decode(raw.rstrip(), expected)
        return actual_decode(raw, expected)

    monkeypatch.setattr(m.core, "decode_financial_data", weakened_decode)
    with pytest.raises(RuntimeError, match="accepted noncanonical data"):
        m.verify(m.metadata.version("just-akash"))


def test_workflow_checks_the_real_installed_wheel_from_outside_checkout():
    workflow = (Path(__file__).parents[1] / ".github/workflows/release.yml").read_text()
    assert (
        '"-I", str(pathlib.Path(".github/scripts/verify_release_wheel_contract.py").resolve())'
        in (workflow)
    )
    assert 'cwd="/", check=True' in workflow
