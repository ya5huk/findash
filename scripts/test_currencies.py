"""Unit tests for deterministic money and Yahoo quote normalization.

Run: python3 scripts/test_currencies.py
"""

from decimal import Decimal
import unittest

from findash_core import currencies


class CurrencyMetadataTests(unittest.TestCase):
    def test_common_and_exception_iso_scales(self):
        self.assertEqual(currencies.minor_unit_exponent("USD"), 2)
        self.assertEqual(currencies.minor_unit_exponent("ils"), 2)
        self.assertEqual(currencies.minor_unit_exponent("JPY"), 0)
        self.assertEqual(currencies.minor_unit_exponent("KWD"), 3)
        self.assertEqual(currencies.minor_unit_exponent("CLF"), 4)

    def test_non_currency_iso_codes_fail_loudly(self):
        with self.assertRaisesRegex(ValueError, "no ISO minor unit"):
            currencies.minor_unit_exponent("XAU")

    def test_invalid_currency_code_fails(self):
        with self.assertRaises(ValueError):
            currencies.canonical_currency("US")

    def test_quote_aliases_normalize_to_iso(self):
        self.assertEqual(currencies.canonical_currency("GBp"), "GBP")
        self.assertEqual(currencies.canonical_currency("GBX"), "GBP")
        self.assertEqual(currencies.canonical_currency("ILA"), "ILS")

    def test_gbp_major_and_gbp_pence_remain_distinct(self):
        self.assertFalse(currencies.yahoo_quote_unit("GBP").is_minor)
        self.assertTrue(currencies.yahoo_quote_unit("GBp").is_minor)


class MoneyConversionTests(unittest.TestCase):
    def test_major_to_minor_uses_financial_half_up_rounding(self):
        self.assertEqual(currencies.major_to_minor("10.005", "USD"), 1001)
        self.assertEqual(currencies.major_to_minor("-10.005", "USD"), -1001)

    def test_major_minor_round_trip_is_exact(self):
        self.assertEqual(currencies.minor_to_major(12345, "USD"), Decimal("123.45"))
        self.assertEqual(currencies.minor_to_major(12345, "KWD"), Decimal("12.345"))

    def test_non_finite_amounts_are_rejected(self):
        for value in (float("nan"), float("inf"), "-Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                currencies.major_to_minor(value, "USD")

    def test_bool_is_not_treated_as_money(self):
        with self.assertRaises(TypeError):
            currencies.major_to_minor(True, "USD")

    def test_generic_fx_conversion_accounts_for_both_scales(self):
        # KWD has three decimals; JPY has none.
        self.assertEqual(currencies.convert_minor(1234, "KWD", "JPY", "500"), 617)

    def test_fx_conversion_rounds_only_at_target_minor_unit(self):
        self.assertEqual(currencies.convert_minor(12345, "USD", "ILS", "3.7"), 45677)
        self.assertEqual(currencies.convert_minor(-12345, "USD", "ILS", "3.7"), -45677)

    def test_to_ils_passes_ils_through_and_requires_foreign_rate(self):
        self.assertEqual(currencies.to_ils_minor(123, "ILS"), 123)
        self.assertEqual(currencies.to_ils_minor(123, "ILA"), 123)
        with self.assertRaisesRegex(ValueError, "missing USD/ILS"):
            currencies.to_ils_minor(123, "USD")

    def test_invalid_fx_rate_is_rejected(self):
        for rate in (0, -1, float("nan")):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                currencies.convert_minor(100, "USD", "ILS", rate)


class YahooQuoteTests(unittest.TestCase):
    def test_usd_major_quote_becomes_cents(self):
        self.assertEqual(
            currencies.yahoo_price_to_minor("123.456", "USD", expected_currency="USD"),
            12346,
        )

    def test_lse_pence_quote_is_already_minor(self):
        self.assertEqual(
            currencies.yahoo_price_to_minor("1075.4", "GBp", expected_currency="GBP"),
            1075,
        )
        self.assertEqual(
            currencies.yahoo_amount_to_major("1075.4", "GBp"),
            Decimal("10.754"),
        )
        self.assertEqual(
            currencies.yahoo_price_to_minor("1075.4", "GBX", expected_currency="GBP"),
            1075,
        )

    def test_tase_agorot_quote_is_already_minor(self):
        self.assertEqual(
            currencies.yahoo_price_to_minor("8450", "ILA", expected_currency="ILS"),
            8450,
        )
        self.assertEqual(
            currencies.yahoo_amount_to_major("8450", "ILA"),
            Decimal("84.5"),
        )

    def test_generic_major_quote_uses_its_iso_scale(self):
        self.assertEqual(
            currencies.yahoo_price_to_minor("42.75", "EUR", expected_currency="EUR"),
            4275,
        )
        self.assertEqual(
            currencies.yahoo_price_to_minor("1200", "JPY", expected_currency="JPY"),
            1200,
        )

    def test_provider_and_database_currency_mismatch_fails(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            currencies.yahoo_price_to_minor("100", "USD", expected_currency="EUR")


if __name__ == "__main__":
    unittest.main()
