"""Suite-wide fixtures."""

from __future__ import annotations

import pytest

#: A wallet that is NOT one of Flynn's: deploy() refuses a Flynn wallet without a provider
#: allowlist (FLYNN_WALLETS), and most deploy tests run with no allowlist and a mocked Console
#: client whose account cannot be resolved. Tests about the Flynn rule override this.
NON_FLYNN_TEST_WALLET = "akash1testwalletnotflynnxxxxxxxxxxxxxxxxxxxx"


@pytest.fixture(autouse=True)
def _a_non_flynn_wallet(monkeypatch):
    from just_akash import deploy as deploy_mod

    monkeypatch.setattr(
        deploy_mod, "_wallet_account", lambda wallet, client: NON_FLYNN_TEST_WALLET
    )
