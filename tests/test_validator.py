"""
Tests for the numeric validator.

No API calls, so these run free and fast. The validator is the last line of
defence before a figure reaches a human, so it needs to be trustworthy in its
own right.
"""

import pytest

from agents.validator import (extract_numbers, fallback_summary,
                              validate_commentary)

FINDINGS = """
VARIANCE ANALYSIS:
Cost centre 102, October 2023 missed plan by $170,896.94.
  Volume: -$156,036.31
  Mix:    -$14,212.56
  Price:  -$59,206.73
  FX:     +$58,558.66
Budget was $4,469,227.76 and actual was $4,298,330.82.

RECONCILIATION:
15 breaks of type missing_in_erp totalling $1,598,478.71.
"""


class TestExtraction:

    @pytest.mark.parametrize("text,expected", [
        ("$1,234.56", 1234.56),
        ("1234", 1234.0),
        ("-$156,036.31", -156036.31),
        ("$171k", 171000.0),
        ("2.4M", 2400000.0),
        ("$1.6 million", 1600000.0),
        ("450 thousand", 450000.0),
        ("$2.4 billion", 2400000000.0),
    ])
    def test_values(self, text, expected):
        assert extract_numbers(text)[0][1] == pytest.approx(expected)

    def test_percentage_flagged(self):
        assert extract_numbers("91%")[0][2] is True

    def test_plain_number_not_percentage(self):
        assert extract_numbers("$500")[0][2] is False

    def test_multiple_numbers(self):
        assert len(extract_numbers("Volume -$156,036 and mix -$14,212")) == 2

    def test_empty(self):
        assert extract_numbers("") == []

    def test_word_after_number_is_not_a_multiplier(self):
        """'October 2023 missed' must not parse as 2.023 billion."""
        assert extract_numbers("October 2023 missed plan")[0][1] == 2023.0

    @pytest.mark.parametrize("text", [
        "opportunity OPP-0002350 closed",
        "account ACC-00123 was duplicated",
        "invoice INV-2024-0001234 is open",
    ])
    def test_identifiers_are_not_amounts(self, text):
        assert extract_numbers(text) == []

    def test_spelled_magnitude_accepted(self):
        """'$1.6 million' is ordinary prose for 1,598,478.71, not an invention."""
        findings = "Missing in ERP: 15 transactions worth $1,598,478.71."
        assert validate_commentary("About $1.6 million is at risk.", findings).ok

    def test_wrong_spelled_magnitude_rejected(self):
        findings = "Missing in ERP: 15 transactions worth $1,598,478.71."
        assert not validate_commentary("About $9.9 million is at risk.",
                                       findings).ok

    def test_identifiers_do_not_fail_validation(self):
        findings = "Missed by $170,896.94 with volume -$156,036.31."
        text = ("Opportunity OPP-0002350 was affected; the miss was "
                "$170,896.94.")
        assert validate_commentary(text, findings).ok


class TestValidation:

    def test_supported_figures_pass(self):
        text = ("Cost centre 102 missed October 2023 plan by $170,896.94, "
                "driven by volume of -$156,036.31.")
        assert validate_commentary(text, FINDINGS).ok

    def test_rounded_figure_accepted(self):
        """$171k should match 170,896.94 — humans round, and that is fine."""
        assert validate_commentary("The miss was $171k.", FINDINGS).ok

    def test_invented_figure_caught(self):
        result = validate_commentary("An invoice of $327,000 was delayed.",
                                     FINDINGS)
        assert not result.ok
        assert result.unsupported

    def test_percentage_caught(self):
        result = validate_commentary("Volume drove 91% of the shortfall.",
                                     FINDINGS)
        assert not result.ok
        assert result.percentages

    def test_percentage_allowed_when_permitted(self):
        assert validate_commentary("Volume was 91% of it.", FINDINGS,
                                   allow_percentages=True).ok

    def test_years_allowed(self):
        assert validate_commentary("This happened in October 2023.", FINDINGS).ok

    def test_small_counts_allowed(self):
        assert validate_commentary("There are 4 drivers to consider.",
                                   FINDINGS).ok

    def test_near_miss_rejected(self):
        """A figure of similar magnitude but genuinely different must fail."""
        result = validate_commentary("The miss was $190,000.", FINDINGS)
        assert not result.ok

    def test_no_numbers_is_valid(self):
        result = validate_commentary("Performance was below expectations.",
                                     FINDINGS)
        assert result.ok
        assert result.checked == 0

    def test_message_is_useful(self):
        result = validate_commentary("We lost $327,000, some 91% of plan.",
                                     FINDINGS)
        assert "327" in result.message()
        assert "91%" in result.message()


class TestFallback:

    def test_fallback_contains_findings(self):
        out = fallback_summary(FINDINGS, "Finance Leader")
        assert "170,896.94" in out
        assert "could not be verified" in out

    def test_fallback_passes_own_validation(self):
        out = fallback_summary(FINDINGS, "Finance Leader")
        assert validate_commentary(out, FINDINGS, allow_percentages=True).ok