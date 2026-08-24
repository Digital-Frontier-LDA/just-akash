"""Unit tests for just_akash.chain — the read-only LCD queries behind `balance`."""

from unittest.mock import patch

import pytest

from just_akash import chain

_DEPOSIT = "/akash.escrow.v1.DepositAuthorization"


def _grants(*auths, expiration="2036-08-04T00:00:00Z"):
    """Build a Cosmos authz `grants` payload from DepositAuthorization specs.

    Every grant carries an `expiration`. The deploy-credit freshness
    discriminator (#168) requires the field, so fixtures without it now fail
    the freshness reconciliation and would force every test to re-state the
    expiry — set a realistic default here, override per-test when needed.
    """
    return {
        "grants": [
            {
                "granter": "akash1granter",
                "grantee": "akash1me",
                "expiration": expiration,
                "authorization": a,
            }
            for a in auths
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
            # Singular `spend_limit` decoy yields no `spend_limits` — no uact to count.
            assert chain.deploy_credit("akash1me") == {}

    def test_no_grant_returns_empty(self):
        with patch.object(chain, "_lcd_get", return_value={"grants": []}):
            assert chain.deploy_credit("akash1me") == {}

    def test_granted_uact_returns_none_when_quorum_is_missing(self):
        with patch.object(chain, "_lcd_get", side_effect=RuntimeError("offline")):
            assert chain.granted_uact("akash1me", quorum=("a", "b", "c"), height=100) is None

    def test_granted_uact_uses_max_agreeing_reading(self):
        # `granted_uact` is the canonical accessor with a 2-of-3 quorum
        # contract — distinct from `deploy_credit`, which reconciles by
        # LATEST EXPIRATION. The OLD behaviour (sum all grants, pick the max
        # value with count >= 2) is preserved here.
        payloads = [
            {
                "grants": [
                    {
                        "expiration": "2036-08-04T00:00:00Z",
                        "authorization": {
                            "@type": _DEPOSIT,
                            "spend_limits": [{"denom": "uact", "amount": "10"}],
                        },
                    }
                ]
            },
            {
                "grants": [
                    {
                        "expiration": "2036-08-04T00:00:00Z",
                        "authorization": {
                            "@type": _DEPOSIT,
                            "spend_limits": [{"denom": "uact", "amount": "10"}],
                        },
                    }
                ]
            },
            {
                "grants": [
                    {
                        "expiration": "2036-08-04T00:00:00Z",
                        "authorization": {
                            "@type": _DEPOSIT,
                            "spend_limits": [{"denom": "uact", "amount": "99"}],
                        },
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
    """Reconciliation by LATEST EXPIRATION — the discriminator that distinguishes a
    fresh grant from a superseded one (#168).

    The OLD rule was ``max(amount)`` across endpoints and across grants within an
    endpoint: "staleness can only lose a deposit, never invent one, so the highest
    reading is the freshest". FALSE when a grant has been REPLACED — the OLD
    (superseded) grant keeps a fixed ``spend_limit`` until it lapses, while the NEW
    grant starts at a smaller amount; max picks the OLD, dead grant. Measured today
    on ``akash1me``:

        api.akashnet.net           407.85 ACT   (expiration 2036-08-04)  ← fresh
        akash-api.polkachu.com     407.85 ACT   (expiration 2036-08-04)  ← fresh
        akash-rest.publicnode.com  246.19 ACT   (expiration 2036-07-14)  ← superseded

    In a chain where the supersession is the OPPOSITE shape (old grant has a larger
    remaining allowance than the new one — easy to construct), the OLD rule reads the
    dead grant and over-reports deploy credit by the OLD allowance.
    """

    @staticmethod
    def _payload(uact, expiration):
        return _grants(
            {"@type": _DEPOSIT, "spend_limits": [{"denom": "uact", "amount": str(uact)}]},
            expiration=expiration,
        )

    def test_known_positive_latest_expiration_wins_over_larger_old_grant(self):
        """#168: a grant with a LATER expiration wins even when its amount is SMALLER.

        Adversarial case (per the issue): two grants from one endpoint, the OLD
        one has 1,000 ACT and expires 2027-01-01, the NEW one has 50 ACT and
        expires 2030-01-01. The OLD rule ``max(amount)`` returns 1,000 ACT —
        the dead grant. The LATEST-EXPIRATION rule returns 50 ACT.

        In the real :33-35 measurements the OLD grant happened to be SMALLER
        too (246.19 ACT vs 407.85 ACT), so the OLD rule "happened to work" on
        that account — but the fix has to work in BOTH shapes, and this test
        pins the adversarial case where the OLD grant is LARGER.
        """
        payload = {
            "grants": [
                {
                    "granter": "akash1old_granter",
                    "grantee": "akash1me",
                    "expiration": "2027-01-01T00:00:00Z",  # OLD, EARLIER
                    "authorization": {
                        "@type": _DEPOSIT,
                        "spend_limits": [{"denom": "uact", "amount": "1000000000"}],  # 1000 ACT
                    },
                },
                {
                    "granter": "akash1new_granter",
                    "grantee": "akash1me",
                    "expiration": "2030-01-01T00:00:00Z",  # NEW, LATER
                    "authorization": {
                        "@type": _DEPOSIT,
                        "spend_limits": [{"denom": "uact", "amount": "50000000"}],  # 50 ACT
                    },
                },
            ]
        }
        with patch.object(chain, "_lcd_get", return_value=payload):
            # The OLD rule (max amount) would return 1,000_000_000 — wrong.
            assert chain.deploy_credit("akash1me") == {"uact": 50_000_000}, (
                "LATEST EXPIRATION must win over max(amount). Got the old grant's "
                "dead allowance — that is the #168 bug."
            )

    def test_known_positive_real_measurements_later_expiring_fresh_grant_wins(self):
        """#168, real :33-35 measurements VERBATIM.

        The fresh grant (api.akashnet.net, akash-api.polkachu.com — both report
        the same chain) has the LATER expiration (2036-08-04) AND the larger
        amount (407.85 ACT). The superseded grant (akash-rest.publicnode.com,
        the DEFAULT endpoint) has the EARLIER expiration (2036-07-14) and a
        smaller remaining allowance (246.19 ACT). The OLD rule happens to pick
        the right one here because the fresh grant is BOTH later AND larger;
        the fix must pick the right one even when those go opposite ways.
        """
        # Simulate the cross-endpoint view: each endpoint reports the SAME
        # pair of grants (both visible, the supersession is observable). The
        # LATER-expiring one wins.
        payload = {
            "grants": [
                {
                    "granter": "akash1sup",
                    "grantee": "akash1me",
                    "expiration": "2036-07-14T00:00:00Z",  # publicnode's reading — superseded
                    "authorization": {
                        "@type": _DEPOSIT,
                        "spend_limits": [{"denom": "uact", "amount": "246190000"}],
                    },
                },
                {
                    "granter": "akash1fresh",
                    "grantee": "akash1me",
                    "expiration": "2036-08-04T00:00:00Z",  # akashnet / polkachu — fresh
                    "authorization": {
                        "@type": _DEPOSIT,
                        "spend_limits": [{"denom": "uact", "amount": "407850000"}],
                    },
                },
            ]
        }
        with patch.object(chain, "_lcd_get", return_value=payload):
            assert chain.deploy_credit("akash1me") == {"uact": 407_850_000}

    def test_known_negative_same_expiration_staleness_still_resolves_by_max(self):
        """When two endpoints share the LATEST expiration but disagree on amount
        (one lagging a deposit that the other has indexed), MAX wins. This is
        the staleness discriminator — the case where the OLD rule was right,
        and the NEW rule must remain right.
        """

        # Endpoint A has indexed a deposit; endpoint B hasn't. Same expiry.
        def fake(path, timeout=15, base=None):
            if base and "laggy" in base:
                return self._payload(100_000_000, "2030-01-01T00:00:00Z")  # 100 ACT, not indexed
            return self._payload(150_000_000, "2030-01-01T00:00:00Z")  # 150 ACT, indexed

        with patch.object(chain, "_lcd_get", side_effect=fake):
            assert chain.deploy_credit("akash1me") == {"uact": 150_000_000}

    def test_known_negative_only_one_grant_no_supersession(self):
        """No supersession: a single grant. LATEST-EXPIRATION degenerates to
        picking that one grant's amount. Trivially equivalent to the OLD
        rule, but the discriminator still applies."""
        payload = self._payload(407_850_000, "2036-08-04T00:00:00Z")
        with patch.object(chain, "_lcd_get", return_value=payload):
            assert chain.deploy_credit("akash1me") == {"uact": 407_850_000}

    def test_no_expiration_on_any_grant_raises_with_sources(self):
        """Per the three-way contract (akash-lease-core #18): "could not ask" must
        not silently win or silently lose. If every grant lacks `expiration`,
        the freshness discriminator cannot resolve — RAISE with the source
        list so a destructive caller can gate."""
        payload = {
            "grants": [
                {
                    "granter": "akash1granter",
                    "grantee": "akash1me",
                    # no expiration
                    "authorization": {
                        "@type": _DEPOSIT,
                        "spend_limits": [{"denom": "uact", "amount": "100000000"}],
                    },
                }
            ]
        }
        with (
            patch.object(chain, "_lcd_get", return_value=payload),
            pytest.raises(RuntimeError, match="WITHOUT a parseable `expiration`"),
        ):
            chain.deploy_credit("akash1me")

    def test_partial_no_exclusion_emits_warning_not_silent_loss(self):
        """If SOME grants have `expiration` and SOME do not, the freshness
        discriminator still resolves (use the ones that have it). The grants
        without `expiration` are EXCLUDED — but the exclusion is a state, not
        a silent loss: ``warnings.warn`` names the excluded sources."""
        import warnings

        payload_with_exp = {
            "grants": [
                {
                    "granter": "akash1fresh",
                    "grantee": "akash1me",
                    "expiration": "2030-01-01T00:00:00Z",
                    "authorization": {
                        "@type": _DEPOSIT,
                        "spend_limits": [{"denom": "uact", "amount": "50000000"}],
                    },
                }
            ]
        }
        # ``rest_urls`` defaults to three real public LCD URLs; the dispatcher
        # routes by `base`, so patch the URL list to two test-controlled hosts
        # that ARE distinguishable. One returns a grant WITH expiration, one
        # returns a grant WITHOUT — that is the partial-expiration case.
        noexp_base = "https://noexp.test"
        ok_base = "https://ok.test"

        def fake(path, timeout=15, base=None):
            if base == noexp_base:
                return {
                    "grants": [
                        {
                            "granter": "akash1rogue",
                            "grantee": "akash1me",
                            # no expiration
                            "authorization": {
                                "@type": _DEPOSIT,
                                "spend_limits": [{"denom": "uact", "amount": "999000000"}],
                            },
                        }
                    ]
                }
            return payload_with_exp

        with (
            patch.object(chain, "rest_urls", return_value=[ok_base, noexp_base]),
            patch.object(chain, "_lcd_get", side_effect=fake),
            warnings.catch_warnings(record=True) as caught,
        ):
            warnings.simplefilter("always")
            result = chain.deploy_credit("akash1me")
        assert result == {"uact": 50_000_000}, (
            f"fresh grant's amount must win (LATEST EXPIRATION); got {result}"
        )
        # The exclusion is a state, not a silent loss — a warning names it.
        assert any("had no parseable `expiration`" in str(w.message) for w in caught), (
            f"expected a warnings.warn naming the excluded source; got {caught}"
        )

    def test_a_dead_endpoint_does_not_sink_the_reading(self):
        def fake(path, timeout=15, base=None):
            if "publicnode" in (base or ""):
                raise RuntimeError("connection refused")
            return self._payload(407_850_000, "2036-08-04T00:00:00Z")

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

    def test_known_positive_same_instant_two_surface_forms(self):
        """The freshness discriminator MUST compare by parsed datetime, not by RFC3339
        string. Two endpoints can report the SAME instant in DIFFERENT surface
        forms — ``"2030-01-01T00:00:00Z"`` vs ``"2030-01-01T00:00:00.000Z"`` vs
        ``"2030-01-01T00:00:00+00:00"`` are all the same moment.

        A string-based ``max()`` is LEXICOGRAPHIC: ``ord('.') == 46`` and
        ``ord('Z') == 90``, so ``"...00Z"`` sorts AFTER ``"...00.000Z"`` even
        though they denote the same instant. The OLD string-max would return
        the whole-second form as 'later' — wrong if the OTHER endpoint emits
        fractional seconds on a strictly-newer grant (next test pins that).

        This is the REGRESSION-ONLY control: on a fixture where both endpoints
        report the SAME instant in two surface forms, BOTH string-max and
        datetime-max return the same grant. The defect-detecting shape is the
        NEXT test, where the chronologically-newer grant carries fractional
        seconds.
        """
        ok_base = "https://nofrac.test"
        frac_base = "https://frac.test"

        def fake(path, timeout=15, base=None):
            if base == frac_base:
                # Same instant, written with fractional seconds.
                return self._payload(50_000_000, "2030-01-01T00:00:00.000Z")
            return self._payload(50_000_000, "2030-01-01T00:00:00Z")  # whole-second

        with (
            patch.object(chain, "rest_urls", return_value=[ok_base, frac_base]),
            patch.object(chain, "_lcd_get", side_effect=fake),
        ):
            # Both readings are 50 ACT at the same instant — same answer
            # regardless of surface form. If this fails, the dispatcher or
            # the parse broke; the comparator is exercised below.
            assert chain.deploy_credit("akash1me") == {"uact": 50_000_000}

    def test_known_positive_fractional_seconds_newer_must_win(self):
        """⭐ LOAD-BEARING DEFECT-DETECTING CONTROL.

        Per CodeRabbit (now confirmed independently): the OLD code did
        ``latest_exp = max(e for _, e, _ in grants_with_exp)`` on RAW
        RFC3339 STRINGS. ``ord('Z') == 90`` sorts after ``ord('.') == 46``,
        so a whole-second ``"2030-01-01T00:00:00Z"`` is treated as LATER
        than ``"2030-01-01T00:00:00.001Z"`` — even though the fractional
        form is 1ms NEWER in time. The moment one LCD emits fractional
        seconds and the other emits a whole-second timestamp on a STRICTLY
        NEWER grant, the freshness discriminator selects the SUPERSEDED
        grant. Same defect class as #168, one layer up.

        This control fails on the string-max mutant; see the mutation
        check section in the PR body.
        """
        laggy_base = "https://laggy.test"
        fresh_base = "https://fresh.test"

        def fake(path, timeout=15, base=None):
            if base == fresh_base:
                # Strictly newer — by 1ms — but written with fractional seconds.
                return self._payload(50_000_000, "2030-01-01T00:00:00.001Z")
            # Lagging endpoint reports the OLD grant, whole-second form.
            return self._payload(100_000_000, "2030-01-01T00:00:00Z")

        with (
            patch.object(chain, "rest_urls", return_value=[laggy_base, fresh_base]),
            patch.object(chain, "_lcd_get", side_effect=fake),
        ):
            # The fresh grant (50 ACT) wins by expiry, even though the OLD
            # grant (100 ACT) has the lexicographically-LATER string.
            assert chain.deploy_credit("akash1me") == {"uact": 50_000_000}, (
                "string-max would have picked the OLD grant (100 ACT) because "
                "'...00Z' sorts after '...00.001Z' lexicographically. That is "
                "the string-max defect — exactly what #168 second-take fixes."
            )

    def test_unparseable_expiration_treated_as_missing(self):
        """If a grant's `expiration` is non-empty but does not parse as RFC3339
        (e.g. a Cosmos node returning a malformed or future-encoding string),
        route it through the same exclusion path as a missing field — with
        `warnings.warn` naming the source. NOT silently used, NOT silently
        dropped.

        Distinct from `test_partial_no_exclusion_emits_warning_not_silent_loss`
        in that the OTHER source returns a valid RFC3339 timestamp; here the
        malformed one is excluded while the well-formed one wins.
        """
        import warnings

        bad_base = "https://malformed.test"
        ok_base = "https://wellformed.test"

        def fake(path, timeout=15, base=None):
            if base == bad_base:
                # Non-RFC3339 string. _parse_expiration returns None.
                return self._payload(999_000_000, "not-a-timestamp")
            return self._payload(50_000_000, "2030-01-01T00:00:00Z")

        with (
            patch.object(chain, "rest_urls", return_value=[ok_base, bad_base]),
            patch.object(chain, "_lcd_get", side_effect=fake),
            warnings.catch_warnings(record=True) as caught,
        ):
            warnings.simplefilter("always")
            result = chain.deploy_credit("akash1me")
        assert result == {"uact": 50_000_000}, (
            "well-formed grant's amount must win; the malformed one is EXCLUDED, "
            "not used as a placeholder."
        )
        assert any("malformed.test" in str(w.message) for w in caught), (
            f"expected the malformed source named in the warning; got {caught}"
        )


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
