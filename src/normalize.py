"""Field normalizers shared across sources.

Originally ported from the EZrelocate backend's ETL scrapers; now the canonical
copy. Kept dependency-free (regex only).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

# --- Regions (province / state / nation), per country ------------------------
# `Listing.province` holds the code from the run country's table below.

_CA_PROVINCES = {
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

_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC", "washington, d.c.": "DC",
}

_AU_STATES = {
    "new south wales": "NSW", "victoria": "VIC", "queensland": "QLD",
    "south australia": "SA", "western australia": "WA", "tasmania": "TAS",
    "northern territory": "NT", "australian capital territory": "ACT",
}

# GB has no state layer; the constituent nations (ISO 3166-2:GB) are the closest
# stable equivalent. UK addresses rarely carry one — postcodes do the geo work.
_GB_NATIONS = {
    "england": "ENG", "scotland": "SCT", "wales": "WLS",
    "northern ireland": "NIR",
}

REGIONS: dict[str, dict[str, str]] = {
    "CA": _CA_PROVINCES,
    "US": _US_STATES,
    "AU": _AU_STATES,
    "GB": _GB_NATIONS,
}


def _codes_pattern(table: dict[str, str], *, ignorecase: bool) -> re.Pattern:
    codes = "|".join(sorted(set(table.values()), key=len, reverse=True))
    return re.compile(rf"\b({codes})\b", re.IGNORECASE if ignorecase else 0)


# CA stays case-insensitive (legacy behavior, Kijiji addresses are clean).
# US/AU match UPPERCASE only: codes like IN, OR, ME, WA are ordinary words in
# lowercase, and addresses conventionally uppercase the region code anyway.
_REGION_IN_ADDR: dict[str, re.Pattern | None] = {
    "CA": _codes_pattern(_CA_PROVINCES, ignorecase=True),
    "US": _codes_pattern(_US_STATES, ignorecase=False),
    "AU": _codes_pattern(_AU_STATES, ignorecase=False),
    "GB": None,  # nation codes don't appear in UK addresses
}

_POSTAL_CA = re.compile(r"\b([A-Z]\d[A-Z])\s?(\d[A-Z]\d)\b", re.IGNORECASE)
# US: 5-digit house numbers exist, but the ZIP ends the address — take the LAST
# match (postal_from_address does). AU: 4 digits invite year/quantity false
# positives, so match only at the end of the string (the address convention).
_POSTAL_US = re.compile(r"\b(\d{5}(?:-\d{4})?)\b")
_POSTAL_AU = re.compile(r"\b(\d{4})\s*$")
_POSTAL_GB = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.IGNORECASE)

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


def normalise_region(value: str | None, country: str = "CA") -> str | None:
    """Full name or code -> the country's region code (ON, TX, NSW, SCT, ...)."""
    if not value:
        return None
    table = REGIONS.get(country, {})
    v = value.strip()
    if v.upper() in set(table.values()):
        return v.upper()
    # Legacy CA leniency: any alpha-2 token passes through as a code.
    if country == "CA" and len(v) == 2 and v.isalpha():
        return v.upper()
    return table.get(v.lower())


def region_from_address(addr: str | None, country: str = "CA") -> str | None:
    pattern = _REGION_IN_ADDR.get(country)
    m = pattern.search(addr or "") if pattern else None
    return m.group(1).upper() if m else None


def postal_from_address(addr: str | None, country: str = "CA") -> str | None:
    addr = addr or ""
    if country == "US":
        hits = _POSTAL_US.findall(addr)
        return hits[-1] if hits else None
    if country == "AU":
        m = _POSTAL_AU.search(addr)
        return m.group(1) if m else None
    if country == "GB":
        m = _POSTAL_GB.search(addr)
        return f"{m.group(1).upper()} {m.group(2).upper()}" if m else None
    m = _POSTAL_CA.search(addr)
    return f"{m.group(1).upper()} {m.group(2).upper()}" if m else None


# CA-flavored aliases, used by the Canadian sources.
def normalise_province(value: str | None) -> str | None:
    return normalise_region(value, "CA")


def province_from_address(addr: str | None) -> str | None:
    return region_from_address(addr, "CA")


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


# Rent-period markers. UK/AU listings habitually quote rent per week ("£450 pw",
# "$650 per week"); CA/US quote per month. A monthly marker wins when both appear
# ("£1,950 pcm (£450 pw)" quotes the same rent twice).
_WEEKLY_RE = re.compile(
    r"\b(?:p\.?/?w\b\.?|per\s*week|weekly|a\s*week|each\s*week)|/\s*w(?:ee)?k\b",
    re.IGNORECASE,
)
_MONTHLY_RE = re.compile(
    r"\b(?:pcm|p\.?/?m\b\.?|per\s*month|monthly|a\s*month)|/\s*mo(?:nth)?\b",
    re.IGNORECASE,
)


def parse_monthly_rent(value: Any) -> int | None:
    """parse_money plus rent-period detection: weekly quotes are converted to a
    calendar-month figure (x 52/12, the letting-industry convention)."""
    amount = parse_money(value)
    if amount is None:
        return None
    if (
        isinstance(value, str)
        and _WEEKLY_RE.search(value)
        and not _MONTHLY_RE.search(value)
    ):
        return round(amount * 52 / 12)
    return amount


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
