"""OpenRent source — the UK's largest direct-from-landlord lettings site.

Two-stage, JSON-first (probed 2026-07-21, see docs/PROJECT_NOTES.md):

1. GET /properties-to-rent/{city-slug} — the page embeds the ENTIRE result set
   as parallel JS arrays (`PROPERTYIDS` + `PROPERTYLISTLATITUDES`/`LONGITUDES`;
   London is ~6k ids), not just the 20 rendered cards. One request buys the
   whole city's id/coordinate map.
2. GET /search/propertiesbyid?ids=... in batches — clean JSON per listing:
   title, rentPerMonth (site-computed, so no pw->pcm conversion needed here),
   details ("2 Bed" / "1 Bath" / "Furnished"), letAgreed, description snippet.

Cloudflare fronts the site; Chrome TLS impersonation (curl_cffi) on a sticky IP
clears it — same treatment as RentFaster (the caller sets the client mode from
the registry).

Known limits: `description` is the search-index snippet (~130 chars), not the
full text — the full version would cost one page fetch per listing;
`postal_code` is the outward code only ("WC2N") — UK listings don't publish the
full postcode before enquiry; `available_from` isn't in the search payload.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import urlencode

import httpx

from ..models import Listing
from ..normalize import normalise_property_type, safe_float, strip_html
from ..polite_client import PoliteClient

SITE = "https://www.openrent.co.uk"
API = f"{SITE}/search/propertiesbyid"

# (display_name, search slug, GB nation code). Slugs are OpenRent search terms:
# /properties-to-rent/<slug>.
CITIES: list[tuple[str, str, str]] = [
    ("London",              "london",              "ENG"),
    ("Birmingham",          "birmingham",          "ENG"),
    ("Manchester",          "manchester",          "ENG"),
    ("Leeds",               "leeds",               "ENG"),
    ("Liverpool",           "liverpool",           "ENG"),
    ("Sheffield",           "sheffield",           "ENG"),
    ("Bristol",             "bristol",             "ENG"),
    ("Nottingham",          "nottingham",          "ENG"),
    ("Leicester",           "leicester",           "ENG"),
    ("Newcastle upon Tyne", "newcastle-upon-tyne", "ENG"),
    ("Brighton",            "brighton",            "ENG"),
    ("Oxford",              "oxford",              "ENG"),
    ("Cambridge",           "cambridge",           "ENG"),
    ("Reading",             "reading",             "ENG"),
    ("Southampton",         "southampton",         "ENG"),
    ("Portsmouth",          "portsmouth",          "ENG"),
    ("Coventry",            "coventry",            "ENG"),
    ("Edinburgh",           "edinburgh",           "SCT"),
    ("Glasgow",             "glasgow",             "SCT"),
    ("Cardiff",             "cardiff",             "WLS"),
    ("Swansea",             "swansea",             "WLS"),
    ("Belfast",             "belfast",             "NIR"),
]

_BATCH = 20  # ids per propertiesbyid call — matches what the site itself does

_ARRAY_RES = {
    "ids": re.compile(r"var PROPERTYIDS = \[(.*?)\]", re.DOTALL),
    "lats": re.compile(r"var PROPERTYLISTLATITUDES = \[(.*?)\]", re.DOTALL),
    "lngs": re.compile(r"var PROPERTYLISTLONGITUDES = \[(.*?)\]", re.DOTALL),
}
# Outward postcode at the end of the listing title ("1 Bed Flat, London, WC2N").
_OUTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*$")
_LEADING_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def cities_for(names: list[str] | None) -> list[tuple[str, str, str]]:
    if not names:
        return CITIES
    wanted = {n.strip().lower() for n in names}
    picked = [c for c in CITIES if c[0].lower() in wanted]
    return picked or CITIES


def _parse_search_arrays(html: str) -> list[tuple[int, float | None, float | None]]:
    """The search page's parallel JS arrays -> [(id, lat, lng), ...]."""
    cols: dict[str, list[str]] = {}
    for key, rx in _ARRAY_RES.items():
        m = rx.search(html)
        cols[key] = [s for s in (m.group(1) if m else "").split(",") if s.strip()]
    out = []
    for i, raw_id in enumerate(cols["ids"]):
        try:
            pid = int(raw_id.strip())
        except ValueError:
            continue
        lat = safe_float(cols["lats"][i].strip()) if i < len(cols["lats"]) else None
        lng = safe_float(cols["lngs"][i].strip()) if i < len(cols["lngs"]) else None
        out.append((pid, lat, lng))
    return out


def _property_type(title: str) -> str | None:
    t = title.lower()
    if t.startswith("room in") or "shared" in t:
        return "room"
    return normalise_property_type(t)


def _listing_from_record(
    rec: dict, *, city: str, nation: str,
    lat: float | None = None, lng: float | None = None,
) -> Listing | None:
    if not isinstance(rec, dict) or rec.get("letAgreed"):
        return None
    rid = rec.get("id")
    if rid in (None, ""):
        return None

    title = str(rec.get("title") or "").strip()
    beds = baths = furnished = None
    for d in rec.get("details") or []:
        dl = str(d).strip().lower()
        num = _LEADING_NUM_RE.search(dl)
        if dl == "furnished":
            furnished = True
        elif dl == "unfurnished":
            furnished = False
        elif dl == "studio":
            beds = 0.5
        elif ("bed" in dl or "room" in dl) and beds is None and num:
            beds = float(num.group())
        elif "bath" in dl and baths is None and num:
            baths = float(num.group())

    # Studios often carry no bed entry in `details`; the title still says so.
    if beds is None and "studio" in title.lower():
        beds = 0.5

    # Site-computed monthly figure; multi-room (HMO) listings carry the room
    # price in maxRoomRentPerMonth instead.
    rent = rec.get("rentPerMonth") or rec.get("maxRoomRentPerMonth")
    monthly = round(float(rent)) if rent else None

    m = _OUTCODE_RE.search(title)
    return Listing(
        source="openrent",
        source_id=str(rid),
        url=f"{SITE}/{rid}",  # 301s to the canonical slug URL
        title=title or None,
        city=city,
        province=nation,
        country="GB",  # set here, not just stamped by main — the harnesses
        currency="GBP",  # bypass main.py and would otherwise default to CA/CAD

        postal_code=m.group(1) if m else None,
        lat=lat,
        lng=lng,
        monthly_rent=monthly,
        bedrooms=beds,
        bathrooms=baths,
        property_type=_property_type(title),
        furnished=furnished,
        description=strip_html(rec.get("description")),
    )


async def scrape(
    client: PoliteClient,
    *,
    cities: list[str] | None,
    max_per_city: int,
    log,
) -> AsyncIterator[Listing]:
    """Yield normalized OpenRent listings for the requested cities."""
    for name, slug, nation in cities_for(cities):
        log.info(f"[openrent] {name} ({nation}) — up to {max_per_city}")
        try:
            r = await client.get(f"{SITE}/properties-to-rent/{slug}")
        except httpx.HTTPStatusError as e:
            log.warning(f"[openrent] {name} search HTTP {e.response.status_code}")
            continue
        except Exception as e:  # noqa: BLE001 — log and move to next city
            log.warning(f"[openrent] {name} search failed ({e!r})")
            continue

        found = _parse_search_arrays(r.text)
        if not found:
            log.warning(f"[openrent] {name}: no embedded result arrays — layout change?")
            continue
        coords = {pid: (lat, lng) for pid, lat, lng in found}
        ids = [pid for pid, _, _ in found]
        log.info(f"[openrent] {name}: {len(ids)} ids on the search page")

        collected = 0
        for start in range(0, len(ids), _BATCH):
            if collected >= max_per_city:
                break
            chunk = ids[start : start + _BATCH]
            url = f"{API}?{urlencode([('ids', i) for i in chunk])}"
            try:
                rows = (await client.get(url)).json()
            except httpx.HTTPStatusError as e:
                log.warning(f"[openrent] {name} batch HTTP {e.response.status_code}")
                break
            except Exception as e:  # noqa: BLE001
                log.warning(f"[openrent] {name} batch failed ({e!r})")
                break
            for rec in rows if isinstance(rows, list) else []:
                if collected >= max_per_city:
                    break
                pid = rec.get("id") if isinstance(rec, dict) else None
                lat, lng = coords.get(pid, (None, None))
                parsed = _listing_from_record(rec, city=name, nation=nation, lat=lat, lng=lng)
                if parsed and parsed.monthly_rent:
                    collected += 1
                    yield parsed
        log.info(f"[openrent] {name}: {collected} listings")
