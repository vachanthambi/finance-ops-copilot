"""
Test the reconciliation engine against the planted defects.

The seed script wrote data/defects_manifest.json recording exactly which
transactions were sabotaged and how. These tests assert the engine rediscovers
them without ever being told where to look.

Run:  pytest -v
"""

import json
from pathlib import Path

import pytest

from engine.normalize import (month_diff, month_floor,
                              normalize_company_name, parse_messy_date)
from engine.reconciliation import get_engine, run_reconciliation

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def manifest():
    return json.loads((ROOT / "data" / "defects_manifest.json")
                      .read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def result():
    return run_reconciliation(get_engine())


@pytest.fixture(scope="session")
def counts(result):
    breaks = result["breaks"]
    return breaks["break_type"].value_counts().to_dict()


# ------------------------------------------------------------------
# Normalisation
# ------------------------------------------------------------------

class TestNormalize:

    @pytest.mark.parametrize("a,b", [
        ("Acme Ltd", "Acme Limited"),
        ("Nolan Inc", "Nolan Incorporated"),
        ("Smith and Sons", "Smith & Sons"),
        ("Vance Corp.", "Vance Corporation"),
        ("HOOPER GROUP", "Hooper Group"),
    ])
    def test_variants_collapse(self, a, b):
        assert normalize_company_name(a) == normalize_company_name(b)

    def test_distinct_names_stay_distinct(self):
        assert (normalize_company_name("Acme Ltd")
                != normalize_company_name("Apex Ltd"))

    def test_empty_input(self):
        assert normalize_company_name("") == ""
        assert normalize_company_name(None) == ""

    @pytest.mark.parametrize("raw", ["2024-03-15", "15/03/2024", "Mar 15, 2024"])
    def test_all_three_date_formats(self, raw):
        parsed = parse_messy_date(raw)
        assert parsed is not None
        assert (parsed.year, parsed.month, parsed.day) == (2024, 3, 15)

    def test_unparseable_returns_none(self):
        assert parse_messy_date("not a date") is None
        assert parse_messy_date(None) is None

    def test_month_helpers(self):
        d = parse_messy_date("2024-03-15")
        assert month_floor(d).day == 1
        assert month_diff(month_floor(d), parse_messy_date("2024-05-01")) == 2


# ------------------------------------------------------------------
# Entity resolution
# ------------------------------------------------------------------

class TestEntityResolution:

    def test_all_crm_accounts_resolve(self, result):
        xwalk = result["crosswalk"]
        unmatched = xwalk.crm_to_erp["customer_id"].isna().sum()
        assert unmatched == 0, f"{unmatched} CRM accounts did not resolve"

    def test_duplicates_found(self, result, manifest):
        expected = manifest["defects"]["duplicate_customer"]["count"]
        assert len(result["crosswalk"].duplicates) == expected

    def test_no_over_merging(self, result):
        """Distinct entities must not collapse into one another."""
        dups = result["crosswalk"].duplicates
        assert (dups["n_records"] == 2).all(), \
            "a customer resolved to more than two CRM records"


# ------------------------------------------------------------------
# Break detection
# ------------------------------------------------------------------

class TestBreakDetection:

    def test_missing_in_erp(self, counts, manifest):
        expected = manifest["defects"]["missing_in_erp"]["count"]
        assert counts.get("missing_in_erp", 0) == expected

    def test_timing(self, counts, manifest):
        expected = manifest["defects"]["timing"]["count"]
        assert counts.get("timing", 0) == expected

    def test_fx_variance(self, counts, manifest):
        expected = manifest["defects"]["fx_variance"]["count"]
        assert counts.get("fx_variance", 0) == expected

    def test_duplicate_customer(self, counts, manifest):
        expected = manifest["defects"]["duplicate_customer"]["count"]
        assert counts.get("duplicate_customer", 0) == expected

    def test_unexplained_is_small(self, counts, result):
        """
        A real reconciliation always leaves residue, but it should be a rounding
        error rather than a category we failed to model.
        """
        unexplained = counts.get("unexplained", 0)
        total = result["n_crm_transactions"]
        assert unexplained / total < 0.01, \
            f"{unexplained} unexplained breaks out of {total} transactions"


# ------------------------------------------------------------------
# Determinism
# ------------------------------------------------------------------

def test_repeatable():
    """Same database, same answer — this is the point of a deterministic engine."""
    a = run_reconciliation(get_engine())["breaks"]
    b = run_reconciliation(get_engine())["breaks"]
    assert len(a) == len(b)
    assert (a["break_type"].value_counts().to_dict()
            == b["break_type"].value_counts().to_dict())
