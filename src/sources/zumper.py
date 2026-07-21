"""Zumper source — US rentals (probed 2026-07-21, see docs/PROJECT_NOTES.md).

Server-rendered JSON-first: every search page embeds `window.__PRELOADED_STATE__`
whose `currentSearch.listables` maps building/group id -> a list of records, one
per floorplan/unit — min/max price, beds, baths, sqft, zipcode, lat/lng,
date_available, short_description, canonical url, unique listing_id. Pagination
is plain `?page=N` (each page re-renders the state; ~25-32 records/page).

Bot posture: plain httpx gets a ~3 KB JS shell; Chrome TLS impersonation
(curl_cffi) on a sticky IP returns the full page — the registry marks this
source sticky_tls accordingly.

Records are building-feed groups, so a big complex yields several rows (one per
floorplan) sharing an address; `monthly_rent` is the floorplan's advertised
"from" price (min_price). `property_type` is a numeric enum only partially
mapped (empirically: 1/4 apartment, 2 house, 16 room) — unknown values become
None rather than a guess.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from ..models import Listing
from ..normalize import parse_available, safe_float, safe_int, strip_html
from ..polite_client import PoliteClient

SITE = "https://www.zumper.com"

# (display_name, search slug, state code). Slug pattern: {city}-{st} under
# /apartments-for-rent/ (which carries all property types; the houses/condos/
# rooms pages are filtered views of the same data).
CITIES: list[tuple[str, str, str]] = [
    ("New York",      "new-york-ny",      "NY"),
    ("Los Angeles",   "los-angeles-ca",   "CA"),
    ("Chicago",       "chicago-il",       "IL"),
    ("Houston",       "houston-tx",       "TX"),
    ("Phoenix",       "phoenix-az",       "AZ"),
    ("Philadelphia",  "philadelphia-pa",  "PA"),
    ("San Antonio",   "san-antonio-tx",   "TX"),
    ("San Diego",     "san-diego-ca",     "CA"),
    ("Dallas",        "dallas-tx",        "TX"),
    ("Austin",        "austin-tx",        "TX"),
    ("San Jose",      "san-jose-ca",      "CA"),
    ("San Francisco", "san-francisco-ca", "CA"),
    ("Seattle",       "seattle-wa",       "WA"),
    ("Denver",        "denver-co",        "CO"),
    ("Boston",        "boston-ma",        "MA"),
    ("Washington",    "washington-dc",    "DC"),
    ("Miami",         "miami-fl",         "FL"),
    ("Atlanta",       "atlanta-ga",       "GA"),
    ("Minneapolis",   "minneapolis-mn",   "MN"),
    ("Portland",      "portland-or",      "OR"),
    ("Charlotte",     "charlotte-nc",     "NC"),
    ("Nashville",     "nashville-tn",     "TN"),
]

HARD_PAGE_CAP = 100  # safety stop per city

_STATE_MARK = "window.__PRELOADED_STATE__"

# Empirical (2026-07-21): sampled across apartments/houses/condos/rooms views.
_PROPERTY_TYPES = {1: "apartment", 2: "house", 4: "apartment", 16: "room"}


def cities_for(names: list[str] | None) -> list[tuple[str, str, str]]:
    if not names:
        return CITIES
    wanted = {n.strip().lower() for n in names}
    picked = [c for c in CITIES if c[0].lower() in wanted]
    return picked or CITIES


def _records_from_page(html: str) -> list[dict]:
    """Decode __PRELOADED_STATE__ and flatten currentSearch.listables.

    The state object is followed by more inline JS, so raw_decode from the
    first '{' rather than regexing to a closing brace.
    """
    at = html.find(_STATE_MARK)
    if at < 0:
        return []
    try:
        start = html.index("{", at)
        state, _ = json.JSONDecoder().raw_decode(html[start:])
    except (ValueError, json.JSONDecodeError):
        return []
    groups = (state.get("currentSearch") or {}).get("listables") or {}
    return [
        rec
        for grp in groups.values()
        if isinstance(grp, list)
        for rec in grp
        if isinstance(rec, dict)
    ]


def _title(rec: dict) -> str | None:
    """building_name is the human name; `title` is a floorplan label ('A1')."""
    building = (rec.get("building_name") or "").strip()
    plan = (rec.get("title") or "").strip()
    if building and plan and plan.lower() != building.lower():
        return f"{building} — {plan}"
    return building or plan or None


def _listing_from_record(rec: dict, *, city: str, state: str) -> Listing | None:
    rid = rec.get("listing_id")
    if rid in (None, ""):
        return None
    url = rec.get("url") or ""
    pets = rec.get("pets")
    return Listing(
        source="zumper",
        source_id=str(rid),
        url=SITE + url if url.startswith("/") else (url or SITE),
        title=_title(rec),
        address=rec.get("address") or None,
        city=rec.get("city") or city,
        province=(rec.get("state") or state or "").upper(),
        country="US",
        postal_code=rec.get("zipcode") or None,
        lat=safe_float(rec.get("lat")),
        lng=safe_float(rec.get("lng")),
        monthly_rent=safe_int(rec.get("min_price")),  # advertised "from" price
        currency="USD",
        bedrooms=0.5 if rec.get("min_bedrooms") == 0 else safe_float(rec.get("min_bedrooms")),
        bathrooms=safe_float(rec.get("min_bathrooms")),
        sqft=safe_int(rec.get("min_square_feet")),
        property_type=_PROPERTY_TYPES.get(rec.get("property_type")),
        pet_friendly=True if pets else None,  # empty/absent = unknown, not "no"
        available_from=parse_available(rec.get("date_available")),
        description=strip_html(rec.get("short_description")),
    )


def _clean_page_url(slug: str, page: int) -> str:
    base = f"{SITE}/apartments-for-rent/{slug}"
    return base if page == 1 else f"{base}?page={page}"


async def scrape(
    client: PoliteClient,
    *,
    cities: list[str] | None,
    max_per_city: int,
    log,
) -> AsyncIterator[Listing]:
    """Yield normalized Zumper listings for the requested cities."""
    for name, slug, state in cities_for(cities):
        collected = 0
        page = 1
        seen: set[str] = set()
        log.info(f"[zumper] {name} ({state}) — up to {max_per_city}")
        while collected < max_per_city and page <= HARD_PAGE_CAP:
            try:
                r = await client.get(_clean_page_url(slug, page))
            except httpx.HTTPStatusError as e:
                log.warning(f"[zumper] {name} page {page} HTTP {e.response.status_code}")
                break
            except Exception as e:  # noqa: BLE001 — log and move to next city
                log.warning(f"[zumper] {name} page {page} failed ({e!r})")
                break

            records = _records_from_page(r.text)
            if not records:
                if page == 1:
                    log.warning(f"[zumper] {name}: no embedded state — layout change?")
                break

            added = 0
            for rec in records:
                if collected >= max_per_city:
                    break
                parsed = _listing_from_record(rec, city=name, state=state)
                if not parsed or parsed.source_id in seen:
                    continue
                if not (parsed.monthly_rent and parsed.city):
                    continue
                seen.add(parsed.source_id)
                collected += 1
                added += 1
                yield parsed

            if added == 0:
                break  # page recycled featured records only — city exhausted
            page += 1
        log.info(f"[zumper] {name}: {collected} listings")
