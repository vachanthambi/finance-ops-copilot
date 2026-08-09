"""
Small, pure text-cleaning helpers.

Everything downstream depends on these, so they stay deliberately boring:
no database access, no side effects, easy to unit test.
"""

import re
from datetime import date, datetime

# Variant spellings collapsed to one canonical token so that
# "Acme Ltd" and "Acme Limited" normalise identically.
SUFFIX_CANONICAL = {
    "ltd": "limited",
    "inc": "incorporated",
    "corp": "corporation",
    "co": "company",
    "grp": "group",
    "llc": "llc",
    "plc": "plc",
    "gmbh": "gmbh",
}

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")

CRM_DATE_FORMATS = [
    "%Y-%m-%d",      # 2024-03-15
    "%d/%m/%Y",      # 15/03/2024
    "%b %d, %Y",     # Mar 15, 2024
]


def normalize_company_name(name: str) -> str:
    """
    Reduce a company name to a comparable form.

    Lowercases, drops punctuation, turns '&' into 'and', and canonicalises
    legal suffixes. Returns '' for empty input.
    """
    if not name:
        return ""
    s = str(name).lower().strip()
    s = s.replace("&", " and ")
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()

    tokens = [SUFFIX_CANONICAL.get(t, t) for t in s.split()]
    return " ".join(tokens)


def parse_messy_date(value) -> date | None:
    """
    Parse a CRM date stored as text in one of three inconsistent formats.

    Returns None rather than raising, so a single bad row cannot halt a load.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    s = str(value).strip()
    if not s:
        return None

    for fmt in CRM_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def month_floor(d) -> date | None:
    """First day of the month containing d."""
    if d is None:
        return None
    if isinstance(d, datetime):
        d = d.date()
    return date(d.year, d.month, 1)


def month_diff(a: date, b: date) -> int:
    """Whole months from a to b. Positive means b is later."""
    return (b.year - a.year) * 12 + (b.month - a.month)
