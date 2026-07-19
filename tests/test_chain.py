"""Unit tests for just_akash.chain — the read-only LCD queries behind `balance`."""

from unittest.mock import patch

from just_akash import chain

_DEPOSIT = "/akash.escrow.v1.DepositAuthorization"


def _grants(*auths):
    return {
        "grants": [
            {"granter": "akash1granter", "grantee": "akash1me", "authorization": a} for a in auths
        ]
    }


class TestDeployCredit:
    def test_sums_spend_limits_from_deposit_authorization(self):
        # Realistic Console shape: a DepositAuthorization lists EVERY spend limit
        # under spend_limits — the real uact credit plus the zero-uakt filler that
        # rides alongside it (dropped by describe_coins for display).
        payload = _grants(
            {
                "@type": _DEPOSIT,
                "spend_limits": [
                    {"denom": "uakt", "amount": "0"},
                    {"denom": "uact", "amount": "170623558"},
                ],
            }
        )
        with patch.object(chain, "_lcd_get", return_value=payload):
            assert chain.deploy_credit("akash1me") == {"uakt": 0, "uact": 170623558}

    def test_ignores_non_deposit_authorizations(self):
        payload = _grants(
            {
                "@type": "/cosmos.bank.v1beta1.SendAuthorization",
                "spend_limit": [{"denom": "uakt", "amount": "999"}],
            },
            {"@type": _DEPOSIT, "spend_limits": [{"denom": "uact", "amount": "500"}]},
        )
        with patch.object(chain, "_lcd_get", return_value=payload):
            # Only the escrow DepositAuthorization counts — the SendAuthorization is skipped.
            assert chain.deploy_credit("akash1me") == {"uact": 500}

    def test_tolerates_singular_spend_limit(self):
        payload = _grants({"@type": _DEPOSIT, "spend_limit": {"denom": "uact", "amount": "42"}})
        with patch.object(chain, "_lcd_get", return_value=payload):
            assert chain.deploy_credit("akash1me") == {"uact": 42}

    def test_no_grant_returns_empty(self):
        with patch.object(chain, "_lcd_get", return_value={"grants": []}):
            assert chain.deploy_credit("akash1me") == {}


class TestCreditGrantDetail:
    def test_returns_granter_and_expiration(self):
        payload = {
            "grants": [
                {
                    "granter": "akash1console",
                    "grantee": "akash1me",
                    "expiration": "2036-07-08T11:54:24Z",
                    "authorization": {
                        "@type": _DEPOSIT,
                        "spend_limits": [{"denom": "uact", "amount": "1"}],
                    },
                }
            ]
        }
        with patch.object(chain, "_lcd_get", return_value=payload):
            d = chain.credit_grant_detail("akash1me")
        assert d == {
            "granter": "akash1console",
            "grantee": "akash1me",
            "expiration": "2036-07-08T11:54:24Z",
        }

    def test_none_when_no_deposit_grant(self):
        payload = _grants({"@type": "/cosmos.bank.v1beta1.SendAuthorization"})
        with patch.object(chain, "_lcd_get", return_value=payload):
            assert chain.credit_grant_detail("akash1me") is None


class TestBankBalances:
    def test_parses_balances(self):
        with patch.object(
            chain, "_lcd_get", return_value={"balances": [{"denom": "uakt", "amount": "1500000"}]}
        ):
            assert chain.bank_balances("akash1me") == {"uakt": 1500000}

    def test_empty_account(self):
        with patch.object(chain, "_lcd_get", return_value={"balances": []}):
            assert chain.bank_balances("akash1me") == {}


class TestCoinsMap:
    def test_tolerates_decimal_and_integer_strings(self):
        # authz reports "170623558"; some nodes decimal-format as "170623558.000..."
        coins = [
            {"denom": "uact", "amount": "170623558.000000000000000000"},
            {"denom": "uakt", "amount": "5"},
        ]
        assert chain._coins_map(coins) == {"uact": 170623558, "uakt": 5}

    def test_skips_malformed_and_sums_duplicates(self):
        coins = [
            {"denom": "uact", "amount": "10"},
            {"denom": "uact", "amount": "5"},
            {"denom": "uakt"},  # no amount
            {"amount": "9"},  # no denom
            {"denom": "uact", "amount": "notanumber"},
        ]
        assert chain._coins_map(coins) == {"uact": 15}


class TestFormatting:
    def test_format_known_denom(self):
        assert chain.format_amount("uact", 170623558) == "170.62 ACT"
        assert chain.format_amount("uakt", 5000000) == "5.00 AKT"

    def test_format_unknown_denom_passes_through(self):
        assert chain.format_amount("ibc/ABC", 123) == "123 ibc/ABC"

    def test_usd_estimate_only_for_pegged(self):
        assert chain.usd_estimate("uact", 170623558) == 170.62  # ACT is USD-pegged
        assert chain.usd_estimate("uakt", 5000000) is None  # AKT floats — never guess

    def test_describe_coins_drops_zeros_and_sorts_desc(self):
        rows = chain.describe_coins({"uakt": 0, "uact": 170623558})
        # zero-uakt (the DepositAuthorization filler) dropped; uact leads
        assert [r["denom"] for r in rows] == ["uact"]
        assert rows[0]["display"] == "170.62 ACT"
        assert rows[0]["usd_estimate"] == 170.62
        assert rows[0]["micro"] == 170623558

    def test_describe_coins_orders_multiple_by_size(self):
        rows = chain.describe_coins({"uact": 10, "uakt": 999})
        assert [r["denom"] for r in rows] == ["uakt", "uact"]


class TestLcdGet:
    def test_rejects_non_json(self):
        import just_akash.chain as c

        class _Resp:
            def read(self):
                return b"<html>502</html>"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch.object(c.urllib.request, "urlopen", return_value=_Resp()):
            try:
                c._lcd_get("/x")
                raise AssertionError("expected RuntimeError")
            except RuntimeError as e:
                assert "non-JSON" in str(e)
