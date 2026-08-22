from unittest.mock import MagicMock

import pytest

from just_akash.wallet_pool import (
    _http_endpoint,
    _quorum_uact,
    configured_api_keys,
    select_client_for_create,
    select_client_for_dseq,
)


def test_wallet_lcd_boundary_rejects_non_http_schemes():
    with pytest.raises(RuntimeError, match="must use http or https"):
        _http_endpoint("file:///etc/passwd")


def test_wallet_allowance_requires_two_matching_height_pinned_readings():
    assert _quorum_uact([90_000_000, None, 90_000_000]) == 90_000_000
    with pytest.raises(RuntimeError, match="no height-pinned LCD quorum"):
        _quorum_uact([90_000_000, 85_000_000, None])


def test_configured_keys_accept_common_delimiters_and_deduplicate(monkeypatch):
    monkeypatch.setenv("AKASH_API_KEYS", "pool-a\npool-b,pool-a;pool-c")
    monkeypatch.setenv("AKASH_API_KEY", "fallback")

    assert configured_api_keys() == ["pool-a", "pool-b", "pool-c", "fallback"]


def test_single_key_keeps_the_zero_probe_compatibility_path(monkeypatch):
    monkeypatch.delenv("AKASH_API_KEYS", raising=False)
    monkeypatch.setenv("AKASH_API_KEY", "only")
    factory = MagicMock()
    credit = MagicMock()

    selection = select_client_for_create(5_000_000, client_factory=factory, credit_reader=credit)

    factory.assert_called_once_with("only")
    credit.assert_not_called()
    assert selection.client is factory.return_value


def test_multi_key_create_chooses_the_highest_measured_allowance(monkeypatch):
    monkeypatch.setenv("AKASH_API_KEYS", "low,rich")
    monkeypatch.delenv("AKASH_API_KEY", raising=False)
    clients = {"low": MagicMock(api_key="low"), "rich": MagicMock(api_key="rich")}
    clients["low"].account_address.return_value = "account-low"
    clients["rich"].account_address.return_value = "account-rich"

    selection = select_client_for_create(
        5_000_000,
        client_factory=lambda key: clients[key],
        credit_reader=lambda account: {
            "account-low": 6_000_000,
            "account-rich": 90_000_000,
        }[account],
    )

    assert selection.client is clients["rich"]
    assert selection.account == "account-rich"
    assert selection.available_uact == 90_000_000


def test_duplicate_keys_for_one_account_are_one_wallet(monkeypatch):
    monkeypatch.setenv("AKASH_API_KEYS", "alias-a,alias-b")
    monkeypatch.delenv("AKASH_API_KEY", raising=False)
    clients = {key: MagicMock(api_key=key) for key in ("alias-a", "alias-b")}
    for client in clients.values():
        client.account_address.return_value = "same-account"

    selection = select_client_for_create(
        1, client_factory=lambda key: clients[key], credit_reader=lambda _account: 20
    )

    assert selection.account == "same-account"
    assert selection.distinct_accounts == 1


def test_all_measured_wallets_below_floor_fail_before_create(monkeypatch):
    monkeypatch.setenv("AKASH_API_KEYS", "a,b")
    monkeypatch.delenv("AKASH_API_KEY", raising=False)
    clients = {key: MagicMock(api_key=key) for key in ("a", "b")}
    clients["a"].account_address.return_value = "account-a"
    clients["b"].account_address.return_value = "account-b"

    with pytest.raises(RuntimeError, match="no Console wallet can fund"):
        select_client_for_create(
            5_000_000,
            client_factory=lambda key: clients[key],
            credit_reader=lambda _account: 4_000_000,
        )


def test_dseq_operations_use_the_owner_not_the_richest_wallet(monkeypatch):
    monkeypatch.setenv("AKASH_API_KEYS", "rich,owner")
    monkeypatch.delenv("AKASH_API_KEY", raising=False)
    clients = {key: MagicMock(api_key=key) for key in ("rich", "owner")}
    clients["rich"].get_deployment.side_effect = RuntimeError("API Error (404): not yours")
    clients["owner"].get_deployment.return_value = {"deployment": {"dseq": "123"}}

    client = select_client_for_dseq("123", client_factory=lambda key: clients[key])

    assert client is clients["owner"]


def test_owner_lookup_failure_names_attempt_count_without_exposing_keys(monkeypatch):
    monkeypatch.setenv("AKASH_API_KEYS", "secret-a,secret-b")
    monkeypatch.delenv("AKASH_API_KEY", raising=False)
    clients = {key: MagicMock(api_key=key) for key in ("secret-a", "secret-b")}
    for client in clients.values():
        client.get_deployment.side_effect = RuntimeError("404")

    with pytest.raises(RuntimeError, match="under any of 2 configured Console wallets") as exc:
        select_client_for_dseq("123", client_factory=lambda key: clients[key])
    assert "secret-a" not in str(exc.value)
    assert "secret-b" not in str(exc.value)
