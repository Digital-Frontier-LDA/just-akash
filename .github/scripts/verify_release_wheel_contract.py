"""Check installed release capabilities; financial DATA never grants authority."""

from __future__ import annotations

import sys
from importlib import metadata

import akash_lease_core as core

from just_akash.execution_observation import ExecutionObservation, observe_execution


def verify(expected_sdk_version: str) -> None:
    if metadata.version("just-akash") != expected_sdk_version:
        raise RuntimeError("installed SDK release version mismatch")
    if metadata.version("akash-lease-core") != "0.17.0" or core.__version__ != "0.17.0":
        raise RuntimeError("installed core release version mismatch")
    if not callable(observe_execution) or not isinstance(ExecutionObservation, type):
        raise RuntimeError("installed execution adapter missing")
    if not isinstance(core.ExecutionClosure, type) or not isinstance(
        core.SettlementEvidence, type
    ):
        raise RuntimeError("installed legacy core contract missing")
    # This inert codec fixture has no owner, operation, quote, policy or authority.
    value = core.NativeLiabilityVector((core.NativeLiability("uakt", 0),))
    encoded = core.encode_financial_data(value)
    if core.decode_financial_data(encoded, core.NativeLiabilityVector) != value:
        raise RuntimeError("installed financial data roundtrip failed")
    for malformed in (encoded + b" ", b'{"schema_version":1,"schema_version":1}'):
        try:
            core.decode_financial_data(malformed, core.NativeLiabilityVector)
        except core.FinancialDecodeError:
            continue
        raise RuntimeError("installed financial decoder accepted noncanonical data")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("expected exact release version")
    verify(sys.argv[1])
