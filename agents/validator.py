"""
Numeric validation of generated commentary.

Prompt rules reduce the rate at which a narrative agent invents or mangles a
figure; they cannot drive it to zero. This module closes the gap structurally:
every number appearing in the commentary is extracted and checked against the
verified findings. Anything unsupported is caught before a human reads it.

Nothing here calls a model. It is plain string handling and arithmetic, so it
is testable and cannot itself hallucinate.
"""

import re
from dataclasses import dataclass, field

# $1,234.56 | 1,234 | 45.2% | $171k | 2.4M | -156036.31
NUMBER = re.compile(
    r"""
    (?<![A-Za-z0-9_-])             # not part of an identifier: OPP-0002350 and
                                   # ACC-00123 are labels, not amounts
    (?P<sign>[-+\u2212])?          # optional sign, including a unicode minus
    \$?\s?
    (?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?   # grouped thousands
          | \d+\.\d+                        # decimal
          | \d+)                            # bare integer
    (?:
        (?P<suffix>[kKmMbB])(?![A-Za-z0-9])   # must abut the digits, and not
                                              # be the first letter of a word:
                                              # "2023 missed" is not 2.023bn
      | \s?(?P<pct>%)
      | \s(?P<word>thousand|million|billion|trillion)\b   # "$1.6 million"
    )?
    (?![A-Za-z0-9_-])              # and not the head of one either
    """,
    re.VERBOSE,
)

MULTIPLIER = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}

# Writing "$1.6 million" for 1,598,478.71 is ordinary business prose, not an
# invented figure. Without this the validator rejected the first draft every
# time a magnitude was spelled out, and burned a retry recovering from it.
WORD_MULTIPLIER = {
    "thousand": 1_000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
    "trillion": 1_000_000_000_000,
}

# Relative tolerance when matching a commentary figure to a findings figure.
# Generous enough to accept "$171k" for 170,896.94, tight enough to reject a
# different number of similar magnitude.
REL_TOLERANCE = 0.01

# Years and small counts are ordinary prose, not claims about the data.
YEAR_RANGE = range(1990, 2101)
SMALL_INT_CEILING = 12


@dataclass
class ValidationResult:
    ok: bool
    unsupported: list = field(default_factory=list)
    checked: int = 0
    percentages: list = field(default_factory=list)

    def message(self) -> str:
        parts = []
        if self.unsupported:
            parts.append("figures not present in the findings: "
                         + ", ".join(self.unsupported))
        if self.percentages:
            parts.append("percentages of signed components: "
                         + ", ".join(self.percentages))
        return "; ".join(parts)


def extract_numbers(text: str) -> list:
    """Return (raw_text, numeric_value, is_percentage) for every number found."""
    out = []
    for m in NUMBER.finditer(text or ""):
        raw = m.group(0).strip()
        value = float(m.group("num").replace(",", ""))
        suffix = (m.group("suffix") or "").lower()
        word = (m.group("word") or "").lower()
        is_pct = m.group("pct") == "%"
        if suffix in MULTIPLIER:
            value *= MULTIPLIER[suffix]
        elif word in WORD_MULTIPLIER:
            value *= WORD_MULTIPLIER[word]
        if m.group("sign") in ("-", "\u2212"):
            value = -value
        out.append((raw, value, is_pct))
    return out


def _is_supported(value: float, known: list, tol: float = REL_TOLERANCE) -> bool:
    """A figure is supported if it matches a findings figure within tolerance."""
    magnitude = abs(value)

    if magnitude in YEAR_RANGE and float(magnitude).is_integer():
        return True                      # a year, not a claim
    if magnitude <= SMALL_INT_CEILING and float(magnitude).is_integer():
        return True                      # counts, months, list positions

    for k in known:
        if k == 0:
            if magnitude < 1e-9:
                return True
            continue
        if abs(magnitude - abs(k)) / abs(k) <= tol:
            return True
    return False


def validate_commentary(commentary: str, findings: str,
                        allow_percentages: bool = False) -> ValidationResult:
    """
    Check the commentary against the findings it was built from.

    Percentages are treated separately: a share of a signed variance component
    is arithmetic the narrative agent should not be doing, and unsigned shares
    of signed components do not sum to 100. They are flagged even when the
    number happens to appear in the findings.
    """
    known = [v for _, v, _ in extract_numbers(findings)]
    unsupported, percentages, checked = [], [], 0

    for raw, value, is_pct in extract_numbers(commentary):
        checked += 1
        if is_pct:
            if not allow_percentages:
                percentages.append(raw)
            continue
        if not _is_supported(value, known):
            unsupported.append(raw)

    return ValidationResult(
        ok=not unsupported and not percentages,
        unsupported=unsupported,
        percentages=percentages,
        checked=checked,
    )


def fallback_summary(findings: str, persona: str) -> str:
    """
    Used when commentary cannot be verified after a retry.

    Returns the verified figures unadorned. Less readable than prose, but every
    number is one the engine produced, which is the property that matters.
    """
    return (
        f"Automated commentary could not be verified against the underlying "
        f"figures, so the verified findings are shown directly.\n\n"
        f"Persona: {persona}\n\n{findings.strip()}"
    )