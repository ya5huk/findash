"""Tests for legacy category compatibility semantics."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from findash_core.categories import flow_type_for


class CategoryCompatibilityTests(unittest.TestCase):
    def test_legacy_categories_are_case_and_whitespace_insensitive(self):
        self.assertEqual(flow_type_for(" Salary ", None, 100), "income")
        self.assertEqual(flow_type_for("CARD_PAYMENT", None, -100), "transfer")

    def test_legacy_fx_is_internal_investment_not_external_transfer(self):
        self.assertEqual(flow_type_for("fx", None, 100), "investment")


if __name__ == "__main__":
    unittest.main()
