"""Field normalizers shared across sources.

Originally ported from the EZrelocate backend's ETL scrapers; now the canonical
copy. Kept dependency-free (regex only).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

_PROVINCE_NAMES = {
    "alberta": "AB",
    "british columbia": "BC",
    "manitoba": "MB",
    "new brunswick": "NB",
    "newfoundland and labrador": "NL",
    "nova scotia": "NS",
    "ontario": "ON",
    "prince edward island": "PE",
    "quebec": "QC",
    "québec": "QC",
    "saskatchewan": "SK",
    "northwest territories": "NT",
    "nunavut": "NU",
    "yukon": "YT",
}

_PROVINCE_IN_ADDR = re.compile(
    r"\b(AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\b", re.IGNORECASE
)
_POSTAL_RE = re.compile(r"\b([A-Z]\d[A-Z])\s?(\d[A-Z]\d)\b", re.IGNORECASE)
_PRICE_RE = re.compile(r"[\d,]+(?:\.\d{1,2})?")
_SQFT_RE = re.compile(r"[\d,]+")

PROPERTY_TYPE_NORMAL = {
    "apartment": "apartment",
    "condo": "condo",
    "townhouse": "townhouse",
    "town house": "townhouse",
    "house": "house",
    "basement": "basement",
    "room": "room",
    "duplex": "duplex",
    "main floor": "house",
    "loft": "apartment",
}


def normalise_province(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return _PROVINCE_NAMES.get(v.lower())


def province_from_address(addr: str | None) -> str | None:
    m = _PROVINCE_IN_ADDR.search(addr or "")
    return m.group(1).upper() if m else None


def postal_from_address(addr: str | None) -> str | None:
    m = _POSTAL_RE.search(addr or "")
    return f"{m.group(1).upper()} {m.group(2).upper()}" if m else None


def parse_money(value: Any) -> int | None:
    """Pull a $-amount out of a string or number, return as integer dollars."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = _PRICE_RE.search(str(value).replace(",", ""))
    if not m:
        return None
    try:
        return int(float(m.group()))
    except ValueError:
        return None


def parse_sqft(value: Any) -> int | None:
    """Square footage may carry commentary ('about 750 sq ft'); pull the number."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) or None
    m = _SQFT_RE.search(str(value).replace(",", ""))
    if not m:
        return None
    try:
        n = int(m.group())
        return n or None
    except ValueError:
        return None


def normalise_property_type(value: str | None) -> str | None:
    if not value:
        return None
    s = str(value).lower().strip()
    for needle, normalised in PROPERTY_TYPE_NORMAL.items():
        if needle in s:
            return normalised
    return None


def strip_html(s: str | None) -> str | None:
    if not s:
        return None
    text = re.sub(r"<[^>]+>", " ", s)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def safe_float(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def safe_int(v: Any) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def yes_no(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "limited"):
        return True
    if s in ("0", "false", "no", "n", ""):
        return False
    return None


def bedrooms_from_text(v: Any) -> float | None:
    """'0'/'bachelor'/'studio' -> 0.5; otherwise the leading number.

    Handles rentfaster's '1 + Den' and Kijiji's numeric codes alike.
    """
    if v is None:
        return None
    s = str(v).strip().lower().replace(" + den", "")
    if s in ("0", "bachelor", "studio", "none"):
        return 0.5
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def lease_months_from_text(v: Any) -> int | None:
    if v is None:
        return None
    s = str(v).lower()
    if "month-to-month" in s or "monthtomonth" in s or "month to month" in s:
        return 1
    m = re.search(r"(\d+)\s*(?:month|mo)", s)
    return int(m.group(1)) if m else None


def parse_available(value: Any, today: date | None = None) -> date | None:
    """Parse a free-text availability string into a date where possible.

    rentfaster ships 'Immediate' / 'Negotiable' / 'Call for Availability' /
    'No Vacancy' / 'Month Day' (e.g. 'July 1'). Anything we can't pin to a real
    date returns None rather than guessing.
    """
    if value is None:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s or s.lower() in ("negotiable", "call for availability", "no vacancy"):
        return None
    today = today or datetime.utcnow().date()
    if s.lower() == "immediate":
        return today
    # ISO date already?
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    for fmt in ("%B %d", "%b %d", "%B %d %Y", "%b %d %Y"):
        try:
            dt = datetime.strptime(s, fmt)
            year = dt.year if "%Y" in fmt else today.year
            parsed = date(year, dt.month, dt.day)
            # A bare 'July 1' that's already past this year means next year.
            if "%Y" not in fmt and parsed < today:
                parsed = parsed.replace(year=today.year + 1)
            return parsed
        except ValueError:
            continue
    return None
