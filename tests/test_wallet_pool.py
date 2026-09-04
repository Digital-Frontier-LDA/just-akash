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
    clients["owner"].get_deployment.return_value = {"deployment": {"id": {"dseq": "123"}}}

    client = select_client_for_dseq("123", client_factory=lambda key: clients[key])

    assert client is clients["owner"]


def test_dseq_owner_requires_positive_matching_identity(monkeypatch):
    monkeypatch.setenv("AKASH_API_KEYS", "empty,wrong,owner")
    monkeypatch.delenv("AKASH_API_KEY", raising=False)
    clients = {key: MagicMock(api_key=key) for key in ("empty", "wrong", "owner")}
    clients["empty"].get_deployment.return_value = {}
    clients["wrong"].get_deployment.return_value = {"deployment": {"id": {"dseq": "999"}}}
    clients["owner"].get_deployment.return_value = {"dseq": "123"}

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


def test_unmeasurable_wallets_report_why_each_one_failed(monkeypatch):
    """ "could not measure any of 3" named the symptom and hid every cause.

    MEASURED in Borduas-Holdings/blazing job 101096063489: that line appeared six
    times and the run then classified itself PROVIDER_CAPACITY — "a market/capacity
    condition, not a code failure" — a verdict about the market reached without
    reading a single wallet. Auth failure, network failure, rate limit and a typo'd
    key all rendered identically, so no occurrence could be told from any other.
    """
    monkeypatch.setenv("AKASH_API_KEYS", "pool-a\npool-b")
    monkeypatch.delenv("AKASH_API_KEY", raising=False)

    def factory(key):
        client = MagicMock()
        client.account_address.side_effect = RuntimeError(f"401 Unauthorized ({key})")
        return client

    with pytest.raises(RuntimeError) as exc:
        select_client_for_create(5_000_000, client_factory=factory, credit_reader=MagicMock())

    message = str(exc.value)
    assert "could not measure any of 2 configured Console wallets" in message
    assert "wallet-0" in message and "wallet-1" in message
    assert message.count("401 Unauthorized") == 2, "each wallet's own reason must survive"


def test_a_failure_reason_never_carries_a_key_into_the_log(monkeypatch):
    """⛔ Reporting the cause must not cost the secret.

    The reasons come from a third-party HTTP client. A client that puts the request
    URL or an auth header into its exception message would carry a Console key
    straight into the run log, which is world-readable on a public Actions run. This
    module's contract is that key values are never logged (`configured_api_keys`).
    """
    monkeypatch.setenv("AKASH_API_KEYS", "sk-SECRET-AAA\nsk-SECRET-BBB")
    monkeypatch.delenv("AKASH_API_KEY", raising=False)

    def factory(key):
        client = MagicMock()
        client.account_address.side_effect = RuntimeError(f"401 for https://api/x?token={key}")
        return client

    with pytest.raises(RuntimeError) as exc:
        select_client_for_create(5_000_000, client_factory=factory, credit_reader=MagicMock())

    message = str(exc.value)
    assert "sk-SECRET-AAA" not in message and "sk-SECRET-BBB" not in message
    assert message.count("***") == 2, "each echoed key must be redacted, not dropped silently"
    assert "401 for https://api/x?token=" in message, "the cause must still be readable"
