"""Kijiji rentals source.

Ported from EZrelocate's etl/scrape_kijiji.py. The clever bit is preserved:
listings are parsed straight out of each search-results page's embedded
`__NEXT_DATA__` Apollo cache (~40 per request) — no per-listing detail fetches,
which is exactly what kept the original under Kijiji's 429 rate limiter.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator

import httpx
from selectolax.parser import HTMLParser

from ..models import Listing
from ..normalize import (
    bedrooms_from_text,
    normalise_property_type,
    parse_money,
    parse_sqft,
    postal_from_address,
    province_from_address,
    safe_float,
    strip_html,
    yes_no,
)
from ..polite_client import PoliteClient

BASE = "https://www.kijiji.ca"

# (display_name, search_path_without_page, province). The location-id suffix
# (l1700xxx) scopes to that city; without it Kijiji returns recency-sorted
# national results skewed to whatever was just posted in ON/QC.
CITIES: list[tuple[str, str, str]] = [
    ("Toronto",            "/b-apartments-condos/city-of-toronto/c37l1700273",   "ON"),
    ("Montreal",           "/b-apartments-condos/city-of-montreal/c37l1700281",  "QC"),
    ("Mississauga",        "/b-apartments-condos/mississauga/c37l1700276",       "ON"),
    ("Ottawa",             "/b-apartments-condos/ottawa/c37l1700185",            "ON"),
    ("Kitchener-Waterloo", "/b-apartments-condos/kitchener-waterloo/c37l1700209", "ON"),
    ("Hamilton",           "/b-apartments-condos/hamilton/c37l1700212",          "ON"),
    ("Edmonton",           "/b-apartments-condos/edmonton/c37l1700203",          "AB"),
    ("Calgary",            "/b-apartments-condos/calgary/c37l1700199",           "AB"),
    ("London",             "/b-apartments-condos/london/c37l1700214",            "ON"),
    ("Winnipeg",           "/b-apartments-condos/winnipeg/c37l1700192",          "MB"),
    ("Quebec City",        "/b-apartments-condos/quebec-city/c37l1700124",       "QC"),
    ("Halifax",            "/b-apartments-condos/halifax/c37l1700321",           "NS"),
    ("Saskatoon",          "/b-apartments-condos/saskatoon/c37l1700197",         "SK"),
    ("Vancouver",          "/b-apartments-condos/city-of-vancouver/c37l1700287", "BC"),
    ("Regina",             "/b-apartments-condos/regina/c37l1700196",            "SK"),
    ("Victoria",           "/b-apartments-condos/victoria/c37l1700173",          "BC"),
    ("Burnaby",            "/b-apartments-condos/burnaby/c37l1700288",           "BC"),
    ("St. John's",         "/b-apartments-condos/st-johns/c37l1700113",          "NL"),
    ("Surrey",             "/b-apartments-condos/surrey/c37l1700290",            "BC"),
    ("Richmond",           "/b-apartments-condos/richmond/c37l1700289",          "BC"),
    ("Moncton",            "/b-apartments-condos/moncton/c37l1700064",           "NB"),
    ("Fredericton",        "/b-apartments-condos/fredericton/c37l1700061",       "NB"),
]

HARD_PAGE_CAP = 100  # safety stop per city

UTILITY_KEYS = {
    "heatincluded": "heat",
    "hydroincluded": "hydro",
    "electricityincluded": "hydro",
    "waterincluded": "water",
    "internetincluded": "internet",
    "cableincluded": "cable",
}


def cities_for(names: list[str] | None) -> list[tuple[str, str, str]]:
    """Filter CITIES by requested names (case-insensitive). Empty -> all."""
    if not names:
        return CITIES
    wanted = {n.strip().lower() for n in names}
    picked = [c for c in CITIES if c[0].lower() in wanted]
    return picked or CITIES


def _city_page_url(path: str, page: int) -> str:
    """Insert /page-N/ before the final /c37l... segment."""
    base, _, cat = path.rpartition("/")
    return f"{BASE}{base}/page-{page}/{cat}"


def _kijiji_id_from_url(url: str) -> str | None:
    m = re.search(r"/(\d{8,})\b", url)
    return m.group(1) if m else None


def _extract_next_data(tree: HTMLParser) -> dict | None:
    node = tree.css_first("script#__NEXT_DATA__")
    if not node or not node.text():
        return None
    try:
        return json.loads(node.text())
    except json.JSONDecodeError:
        return None


def _flatten_attrs(attrs) -> dict[str, str]:
    """Kijiji v2: {all: [{canonicalName, canonicalValues}]}. Older: flat list."""
    if attrs is None:
        return {}
    if isinstance(attrs, dict):
        items = attrs.get("all") or []
    elif isinstance(attrs, list):
        items = attrs
    else:
        return {}
    flat: dict[str, str] = {}
    for a in items:
        if not isinstance(a, dict):
            continue
        key = (
            a.get("canonicalName") or a.get("machineKey") or a.get("name") or ""
        ).lower().replace("_", "")
        vals = a.get("canonicalValues") or a.get("values")
        val = (
            vals[0]
            if isinstance(vals, list) and vals
            else (a.get("machineValue") or a.get("value"))
        )
        if key and val is not None:
            flat[key] = str(val).lower()
    return flat


def _bathrooms_from_attr(v) -> float | None:
    """Kijiji encodes baths as integer x10: '15' -> 1.5, '20' -> 2.0."""
    if v is None:
        return None
    try:
        return float(v) / 10.0
    except (TypeError, ValueError):
        return None


def _parse_utilities(attrs: dict[str, str]) -> list[str]:
    out: list[str] = []
    for key, label in UTILITY_KEYS.items():
        if attrs.get(key) in ("1", "true", "yes") and label not in out:
            out.append(label)
    return out


def _listing_from_apollo(listing: dict, url: str | None = None) -> Listing | None:
    url = url or listing.get("url") or ""
    if url and url.startswith("/"):
        url = BASE + url
    lid = listing.get("id")
    source_id = str(lid) if lid not in (None, "") else _kijiji_id_from_url(url)
    if not source_id:
        return None

    loc = listing.get("location") or {}
    coords = loc.get("coordinates") or {}
    address_full = loc.get("address") or ""
    province = province_from_address(address_full) or ""

    price_blob = listing.get("price") or {}
    cents = price_blob.get("amount") if isinstance(price_blob, dict) else None
    monthly_rent = (
        int(cents / 100) if isinstance(cents, (int, float)) else parse_money(price_blob)
    )

    attrs = _flatten_attrs(listing.get("attributes"))
    return Listing(
        source="kijiji",
        source_id=source_id,
        url=url,
        title=listing.get("title"),
        address=address_full or None,
        city=loc.get("name") or "",
        province=province,
        postal_code=postal_from_address(address_full),
        lat=safe_float(coords.get("latitude")),
        lng=safe_float(coords.get("longitude")),
        monthly_rent=monthly_rent,
        bedrooms=bedrooms_from_text(attrs.get("numberbedrooms")),
        bathrooms=_bathrooms_from_attr(attrs.get("numberbathrooms")),
        sqft=parse_sqft(attrs.get("areainfeet")),  # "0" -> None, not 0
        property_type=normalise_property_type(attrs.get("unittype")),
        furnished=yes_no(attrs.get("furnished")),
        pet_friendly=yes_no(attrs.get("petsallowed")),
        utilities_included=_parse_utilities(attrs),
        lease_length_months=None,
        description=strip_html(listing.get("description")),
    )


def _listings_from_search(html: str) -> list[Listing]:
    tree = HTMLParser(html)
    data = _extract_next_data(tree)
    if not data:
        return []
    state = data.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {})
    out: list[Listing] = []
    for key, obj in state.items():
        if key.startswith("RealEstateListing:"):
            parsed = _listing_from_apollo(obj)
            if parsed:
                out.append(parsed)
    return out


async def scrape(
    client: PoliteClient,
    *,
    cities: list[str] | None,
    max_per_city: int,
    log,
) -> AsyncIterator[Listing]:
    """Yield normalized Kijiji listings, round-robin across the requested cities."""
    for name, path, province in cities_for(cities):
        collected = 0
        page = 1
        log.info(f"[kijiji] {name} ({province}) — up to {max_per_city}")
        while collected < max_per_city and page <= HARD_PAGE_CAP:
            url = _city_page_url(path, page)
            try:
                r = await client.get(url)
            except httpx.HTTPStatusError as e:
                ra = e.response.headers.get("Retry-After")
                extra = f", Retry-After={ra}" if ra else ""
                log.warning(
                    f"[kijiji] {name} page {page} HTTP {e.response.status_code}{extra}"
                )
                break
            except Exception as e:  # noqa: BLE001 — log and move to next city
                log.warning(f"[kijiji] {name} page {page} fetch failed ({e!r})")
                break

            listings = _listings_from_search(r.text)
            if not listings:
                break

            added = 0
            for parsed in listings:
                if collected >= max_per_city:
                    break
                # Keep only usable rows: priced + located, and stamp the city's
                # province when the address didn't carry one.
                if parsed.monthly_rent and parsed.city:
                    if not parsed.province:
                        parsed.province = province
                    collected += 1
                    added += 1
                    yield parsed

            if added == 0 and collected > 0:
                break  # small city exhausted (Kijiji recycles instead of 404ing)
            page += 1
        log.info(f"[kijiji] {name}: {collected} listings")
