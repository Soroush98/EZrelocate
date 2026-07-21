"""RentFaster.ca source — the white-space gap (no competing Apify actor).

RentFaster exposes a JSON search API that returns ~fully structured listings, so
there's no HTML parsing — but since ~2026 it sits behind a Cloudflare "managed
challenge" that 403s plain requests. The challenge fingerprints the TLS
ClientHello (JA3), so browser-like *headers* alone no longer clear it (they did
until ~2026-06). The working fix is a browser-like TLS+HTTP2 *fingerprint*: the
caller hands us a PoliteClient in `impersonate="chrome"` mode (curl_cffi), which
forges Chrome's fingerprint. We still send the browser-like headers below and a
homepage warmup to collect the __cf_bm cookie — belt and suspenders.

Endpoint:  https://www.rentfaster.ca/api/search.json
Params:    proximity_type=location-city, novacancy=0, cur_page={0-indexed}
Scoping:   `lastcity` cookie = "<province>/<city>" (lowercased)
Response:  {"listings": [...], "query": {...}, "total": N, "total2": pageCount}
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import httpx

from ..models import Listing
from ..normalize import (
    bedrooms_from_text,
    normalise_property_type,
    normalise_province,
    parse_available,
    parse_money,
    parse_sqft,
    postal_from_address,
    safe_float,
    strip_html,
    yes_no,
)
from ..polite_client import PoliteClient

API = "https://www.rentfaster.ca/api/search.json"
SITE = "https://www.rentfaster.ca"

# Default city set, mirroring the Kijiji coverage so the two sources line up for
# dedup. (city_name, province) — rentfaster scopes by the `lastcity` cookie.
CITIES: list[tuple[str, str]] = [
    ("Toronto", "ON"), ("Montreal", "QC"), ("Mississauga", "ON"), ("Ottawa", "ON"),
    ("Kitchener", "ON"), ("Hamilton", "ON"), ("Edmonton", "AB"), ("Calgary", "AB"),
    ("London", "ON"), ("Winnipeg", "MB"), ("Quebec City", "QC"), ("Halifax", "NS"),
    ("Saskatoon", "SK"), ("Vancouver", "BC"), ("Regina", "SK"), ("Victoria", "BC"),
    ("Burnaby", "BC"), ("Surrey", "BC"), ("Richmond", "BC"), ("Moncton", "NB"),
    ("Fredericton", "NB"), ("St. John's", "NL"),
]

# Headers that get past Cloudflare's managed challenge on the API host.
CF_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": f"{SITE}/",
    "Origin": SITE,
    "X-Requested-With": "XMLHttpRequest",
}

HARD_PAGE_CAP = 100  # rentfaster pages are ~10-50 listings; this is a safety stop

# Non-housing listing types we drop (rentfaster mixes these in).
SKIP_TYPES = {"office space", "parking spot", "storage", "shared"}

UTILITY_LABELS = ["electricity", "water", "heat", "internet", "cable"]


def cities_for(names: list[str] | None) -> list[tuple[str, str]]:
    if not names:
        return CITIES
    wanted = {n.strip().lower() for n in names}
    picked = [c for c in CITIES if c[0].lower() in wanted]
    return picked or CITIES


def _source_id(raw_id, link: str) -> str | None:
    """Unique id. rentfaster reuses one `id` across a building's unit types and
    disambiguates with a `_<n>` suffix on the link; fold that into the id."""
    tail = (link or "").rstrip("/").rsplit("/", 1)[-1]
    m = re.fullmatch(r"(\d+)(?:_(\d+))?", tail)
    if m:
        return f"{m.group(1)}_{m.group(2)}" if m.group(2) else f"{m.group(1)}_0"
    if raw_id not in (None, ""):
        return f"{raw_id}_0"
    return None


def _utilities(raw: object) -> list[str]:
    """`utilities_included` is a free-text string; pull the known labels out."""
    if not raw:
        return []
    s = str(raw).lower()
    return [u for u in UTILITY_LABELS if u in s]


def _furnished(listing: dict) -> bool | None:
    # rentfaster keys vary; check the likely ones.
    for key in ("furnishing", "furnished"):
        if key in listing:
            v = str(listing[key]).lower()
            if "unfurnished" in v or v in ("no", "false", "0", ""):
                return False
            if "furnished" in v or v in ("yes", "true", "1"):
                return True
    return None


def _pet_friendly(listing: dict) -> bool | None:
    cats = yes_no(listing.get("cats"))
    dogs = yes_no(listing.get("dogs"))
    pets = yes_no(listing.get("pets") or listing.get("pet"))
    vals = [v for v in (cats, dogs, pets) if v is not None]
    return any(vals) if vals else None


def _parse(listing: dict, province: str) -> Listing | None:
    if not isinstance(listing, dict):
        return None
    if str(listing.get("type", "")).strip().lower() in SKIP_TYPES:
        return None

    link = listing.get("link") or ""
    if link.startswith("/"):
        url = SITE + link
    elif link.startswith("http"):
        url = link
    else:
        url = f"{SITE}/{link}" if link else SITE
    source_id = _source_id(listing.get("id"), link)
    if not source_id:
        return None

    address = listing.get("address") or None
    prov = (
        normalise_province(listing.get("province"))
        or normalise_province(province)
        or province
    )
    return Listing(
        source="rentfaster",
        source_id=source_id,
        url=url,
        title=listing.get("title") or listing.get("intro"),
        address=address,
        city=listing.get("city") or "",
        province=prov or "",
        postal_code=postal_from_address(address),
        lat=safe_float(listing.get("latitude") or listing.get("lat")),
        lng=safe_float(listing.get("longitude") or listing.get("lng")),
        monthly_rent=parse_money(listing.get("price")),
        bedrooms=bedrooms_from_text(listing.get("bedrooms") or listing.get("beds")),
        bathrooms=_baths(listing.get("baths")),
        sqft=parse_sqft(listing.get("sq_feet")),
        property_type=normalise_property_type(listing.get("type")),
        furnished=_furnished(listing),
        pet_friendly=_pet_friendly(listing),
        utilities_included=_utilities(listing.get("utilities_included")),
        lease_length_months=None,
        available_from=parse_available(listing.get("availability")),
        description=strip_html(listing.get("intro") or listing.get("description")),
    )


def _baths(v) -> float | None:
    if v in (None, "", "none"):
        return None
    try:
        return float(str(v).split()[0])
    except (TypeError, ValueError):
        return None


async def _warmup(client: PoliteClient, log) -> None:
    """Load the homepage so Cloudflare hands the client a `cf_clearance` cookie.

    The `/api/search.json` endpoint is behind a Cloudflare managed challenge that
    cold API hits often fail. A real browser passes it by loading the site first;
    we mimic that — the persistent client keeps the resulting cookie and replays
    it on the API calls. Requires a sticky IP (the caller pins one), so the cookie
    and the IP that earned it stay paired.
    """
    try:
        await client.get(SITE + "/")
        log.info("[rentfaster] warmup OK — homepage loaded (cf_clearance acquired)")
    except Exception as e:  # noqa: BLE001 — warmup is best-effort
        log.warning(f"[rentfaster] warmup failed ({e!r}); API calls may be 403'd")


async def scrape(
    client: PoliteClient,
    *,
    cities: list[str] | None,
    max_per_city: int,
    log,
) -> AsyncIterator[Listing]:
    await _warmup(client, log)
    for name, province in cities_for(cities):
        collected = 0
        page = 0
        seen: set[str] = set()
        cookies = {"lastcity": f"{province.lower()}/{name.lower()}"}
        log.info(f"[rentfaster] {name} ({province}) — up to {max_per_city}")
        while collected < max_per_city and page <= HARD_PAGE_CAP:
            try:
                r = await client.get(
                    API,
                    headers=CF_HEADERS,
                    params={
                        "proximity_type": "location-city",
                        "novacancy": "0",
                        "cur_page": page,
                    },
                    cookies=cookies,
                )
                data = r.json()
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                hint = (
                    " (Cloudflare — TLS impersonation should clear this; check the "
                    "client is in impersonate mode and the proxy IP isn't burned)"
                    if code == 403
                    else ""
                )
                log.warning(f"[rentfaster] {name} page {page} HTTP {code}{hint}")
                break
            except Exception as e:  # noqa: BLE001
                log.warning(f"[rentfaster] {name} page {page} failed ({e!r})")
                break

            rows = data.get("listings") or []
            if not rows:
                break

            added = 0
            for raw in rows:
                if collected >= max_per_city:
                    break
                parsed = _parse(raw, province)
                if not parsed or parsed.source_id in seen:
                    continue
                if not (parsed.monthly_rent and parsed.city):
                    continue
                seen.add(parsed.source_id)
                collected += 1
                added += 1
                yield parsed

            if added == 0:
                break
            page += 1
        log.info(f"[rentfaster] {name}: {collected} listings")
