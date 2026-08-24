"""Unit tests for just_akash.chain — the read-only LCD queries behind `balance`."""

from unittest.mock import patch

import pytest

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

    def test_rejects_singular_spend_limit_decoy(self):
        payload = _grants({"@type": _DEPOSIT, "spend_limit": {"denom": "uact", "amount": "42"}})
        with patch.object(chain, "_lcd_get", return_value=payload):
            assert chain.deploy_credit("akash1me") == {}

    def test_no_grant_returns_empty(self):
        with patch.object(chain, "_lcd_get", return_value={"grants": []}):
            assert chain.deploy_credit("akash1me") == {}

    def test_granted_uact_returns_none_when_quorum_is_missing(self):
        with patch.object(chain, "_lcd_get", side_effect=RuntimeError("offline")):
            assert chain.granted_uact("akash1me", quorum=("a", "b", "c"), height=100) is None

    def test_granted_uact_uses_max_agreeing_reading(self):
        payloads = [
            {
                "grants": [
                    {
                        "authorization": {
                            "@type": _DEPOSIT,
                            "spend_limits": [{"denom": "uact", "amount": "10"}],
                        }
                    }
                ]
            },
            {
                "grants": [
                    {
                        "authorization": {
                            "@type": _DEPOSIT,
                            "spend_limits": [{"denom": "uact", "amount": "10"}],
                        }
                    }
                ]
            },
            {
                "grants": [
                    {
                        "authorization": {
                            "@type": _DEPOSIT,
                            "spend_limits": [{"denom": "uact", "amount": "99"}],
                        }
                    }
                ]
            },
        ]
        with patch.object(chain, "_lcd_get", side_effect=payloads):
            assert chain.granted_uact("akash1me", quorum=("a", "b", "c"), height=100) == 10


class TestFreeUact:
    """Fix for #169 — `spend_limits` is already NET of locked escrow.

    The OLD expression `max(granted_uact - locked_uact, 0)` double-subtracts and
    reads 0 for a funded wallet when `locked > granted` (which is routine on
    real accounts — see issue body, disproof 1).

    Two payload-level observations prove `spend_limits` is the REMAINING
    allowance, NOT the gross grant:

    1. `locked > granted` is impossible on a gross grant (a deployment's
       escrow cannot exceed what was ever granted). It is routine here.
    2. `spend_limits` falls in exact 5 ACT steps as deployments are created
       (measured: 25.670005 -> 15.670001 = -10.000004 on 2 deposits). A
       deposit's escrow cost is 5 ACT; only a remaining allowance moves by
       that exact amount. A gross grant does not move on a deposit.

    The fix is `free_uact = granted_uact` (clamped to 0 defensively) — the
    helper `chain.free_uact` enforces this at the function boundary so the
    OLD expression cannot be re-introduced by a well-meaning caller.
    """

    def test_free_uact_returns_granted_value_directly(self):
        """#169: spend_limits is already NET — free == granted (clamped to >=0).

        Real Console data: spend_limit=170.62 ACT (170_623_558 uact), 165 ACT
        locked in escrow. The OLD expression would compute 5.62 ACT
        (170.62 - 165) — wrong, that is double-subtract.

        The helper returns the spend_limit value as-is. `locked_uact` is NOT
        a parameter — there is no subtraction to make.
        """
        # Realistic payload values from the cli.py comment (170.62 ACT grant,
        # 165 ACT locked) — exactly the dataset the OLD expression mishandles.
        granted_uact = 170_623_558  # 170.62 ACT in uact
        locked_uact = 165_000_000  # 165.00 ACT in uact (passed for parity)

        free = chain.free_uact(granted_uact)

        # The OLD expression `max(170_623_558 - 165_000_000, 0) = 5_623_558`
        # would FAIL this assertion.
        assert free == granted_uact, (
            f"free_uact must equal granted_uact (spend_limits is already NET). "
            f"Got {free}, expected {granted_uact}. The OLD expression "
            f"`max({granted_uact} - {locked_uact}, 0) = {granted_uact - locked_uact} "
            f"double-subtracts and would FAIL here."
        )
        assert free == 170_623_558  # NET, not 5_623_558
        assert free != 5_623_558  # The OLD's wrong answer — explicit guard.

    def test_free_uact_does_not_take_locked_uact_parameter(self):
        """The helper must take ONLY the spend_limit value.

        If it took `locked_uact` as a parameter, a future caller could be
        tempted to compute `max(g - l, 0)` — the OLD expression. The signature
        is the fence: locked_uact is not in scope.
        """
        import inspect

        sig = inspect.signature(chain.free_uact)
        params = list(sig.parameters)
        assert "locked_uact" not in params, (
            f"free_uact must not accept locked_uact — spend_limits is already "
            f"net, and a `locked_uact` parameter would let callers re-introduce "
            f"the OLD double-subtract. Current params: {params}."
        )
        assert params == ["granted_uact_value"], (
            f"free_uact must take a single parameter. Got {params}."
        )

    def test_free_uact_clamps_negative_inputs_to_zero(self):
        """Defensive clamp. A negative `spend_limits` reading (e.g. parse error)
        must not propagate as a deployable negative credit — `free_uact = 0`.
        """
        assert chain.free_uact(-1) == 0
        assert chain.free_uact(-1_000_000) == 0

    def test_free_uact_with_locked_greater_than_granted_does_not_clamp_to_zero(self):
        """#169 disproof 1: locked > granted is routine on real accounts.

        If the OLD `max(g - l, 0)` were re-introduced, this exact scenario
        clamps to 0 — hiding the wrongness. The helper must NOT clamp in this
        case; the spend_limit value is what it is, even when escrow locked
        exceeds it (which proves spend_limit is net, not gross).
        """
        # From the issue body, disproof 1:
        #   akash1n4uut3vxmkdp8wsrya3q0qyddgqey0rh9as4ee: granted=90.23 ACT,
        #   locked=346.43 ACT. locked > granted — impossible on a gross grant.
        granted_uact = 90_230_000  # 90.23 ACT
        locked_uact = 346_430_000  # 346.43 ACT

        free = chain.free_uact(granted_uact)

        # Must NOT be `max(90_230_000 - 346_430_000, 0) = 0` (the OLD's clamp).
        assert free != 0, (
            f"free_uact must not clamp to 0 when locked ({locked_uact}) > "
            f"granted ({granted_uact}) — that is the OLD expression's failure "
            f"mode (#169). spend_limits is NET, so the helper returns the "
            f"granted value as-is."
        )
        assert free == granted_uact


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


class TestRestUrl:
    def test_rejects_non_http_scheme(self, monkeypatch):
        monkeypatch.setenv("AKASH_REST_URL", "file:///etc/passwd")
        with pytest.raises(RuntimeError, match="http/https scheme"):
            chain.rest_url()

    def test_accepts_https_and_strips_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("AKASH_REST_URL", "https://akash.example/rpc/")
        assert chain.rest_url() == "https://akash.example/rpc"

    def test_defaults_to_public_lcd(self, monkeypatch):
        monkeypatch.delenv("AKASH_REST_URL", raising=False)
        assert chain.rest_url() == chain.DEFAULT_REST_URL


class TestMultiEndpointCreditReconciliation:
    """A lagging LCD must not be able to declare a funded account empty.

    Measured 2026-08-06 against one live account:

        api.akashnet.net           407.85 ACT   (expiration 2036-08-04)
        akash-api.polkachu.com     407.85 ACT   (expiration 2036-08-04)
        akash-rest.publicnode.com  246.19 ACT   (expiration 2036-07-14)   <- the default

    The default was $161 behind and still serving a grant that had already been
    replaced. Every credit gate — `balance --check --min-usd`, the Prometheus gauge,
    a CI preflight — would have read the account as short and taken the failure path
    while it held plenty. In CI that means paying for hosted runners with a funded
    wallet.
    """

    @staticmethod
    def _payload(uact):
        return _grants(
            {"@type": _DEPOSIT, "spend_limits": [{"denom": "uact", "amount": str(uact)}]}
        )

    def test_the_freshest_reading_wins(self):
        """MAX, never min or first: staleness can only lose a deposit, not invent one."""
        seen = []

        def fake(path, timeout=15, base=None):
            seen.append(base)
            return self._payload(246_190_000 if "publicnode" in (base or "") else 407_850_000)

        with patch.object(chain, "_lcd_get", side_effect=fake):
            assert chain.deploy_credit("akash1me") == {"uact": 407_850_000}
        assert len(seen) >= 2, "only one endpoint was consulted"

    def test_a_dead_endpoint_does_not_sink_the_reading(self):
        def fake(path, timeout=15, base=None):
            if "publicnode" in (base or ""):
                raise RuntimeError("connection refused")
            return self._payload(407_850_000)

        with patch.object(chain, "_lcd_get", side_effect=fake):
            assert chain.deploy_credit("akash1me") == {"uact": 407_850_000}

    def test_every_endpoint_down_raises_rather_than_reporting_zero(self):
        """Zero credit and 'could not ask' are different claims. Reporting zero here
        would make a network outage look like a drained wallet."""
        with (
            patch.object(chain, "_lcd_get", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match="no LCD endpoint"),
        ):
            chain.deploy_credit("akash1me")

    def test_a_genuinely_empty_account_is_still_empty(self):
        with patch.object(chain, "_lcd_get", return_value={"grants": []}):
            assert chain.deploy_credit("akash1me") == {}


class TestRestUrls:
    def test_default_fans_out_beyond_the_single_public_node(self, monkeypatch):
        monkeypatch.delenv("AKASH_REST_URL", raising=False)
        urls = chain.rest_urls()
        assert urls[0] == chain.DEFAULT_REST_URL, "the documented default stays first"
        assert len(urls) > 1, "a single lagging node must not be the only source"

    def test_an_explicit_pin_is_honoured_alone(self, monkeypatch):
        """Pinning AKASH_REST_URL is an operator decision — an air-gapped LCD, a node
        under test. Quietly querying public hosts anyway would defeat it."""
        monkeypatch.setenv("AKASH_REST_URL", "https://my-private-lcd.internal")
        assert chain.rest_urls() == ["https://my-private-lcd.internal"]

    def test_pin_still_rejects_non_http_schemes(self, monkeypatch):
        monkeypatch.setenv("AKASH_REST_URL", "file:///etc/passwd")
        with pytest.raises(RuntimeError):
            chain.rest_urls()
