"""Kijiji rentals scraper — national, apartments + houses for rent.

Architecture:
    1. Walk paginated search-result pages (b-apartments-condos/canada).
    2. For each result card, extract source_id + listing URL.
    3. Fetch each detail page (politely) and parse the structured fields.
    4. Upsert into `listings` with source='kijiji'. Re-runs just refresh
       last_seen_at, so this is safe to schedule.

Caveats:
    - Kijiji's HTML changes periodically. The CSS selectors below match the
      current layout; if a release breaks them, inspect a listing page and
      update the selectors. The architecture (pagination → upsert) is stable.
    - Be polite. Default config: 2 concurrent requests, 0.4-1s jitter, retries.
    - Kijiji robots.txt is restrictive. For a personal portfolio crawl this
      is the standard gray area; do not redistribute or build a commercial
      product on this data without negotiated access.

Run:
    cd backend && python -m etl.scrape_kijiji --max-listings 200
    cd backend && python -m etl.scrape_kijiji --max-listings 50 --dry-run
"""

import argparse
import asyncio
import json
import re

import httpx
from selectolax.parser import HTMLParser

from etl._scrape import (
    PoliteClient,
    ScrapedListing,
    mark_stale,
    parse_money,
    upsert_listings,
)

BASE = "https://www.kijiji.ca"
# c37 = apartments & condos for rent; l0 = all of Canada.
# c43 = houses for rent. To cover both, scrape c30349001l0 = "real estate for rent" parent.
SEARCH_PATH_TEMPLATE = "/b-apartments-condos/canada/page-{page}/c37l0"

# Per-city URLs. The location-id suffix (l1700xxx) scopes results to that city;
# without it Kijiji returns recency-sorted national results which heavily skew
# toward whatever's been posted in Ontario + Quebec in the last hour.
#
# Each entry: (display_name, url_path_without-page, expected_province).
# Verified live against Kijiji's category pages — counts shown are listings
# available at validation time.
CITIES: list[tuple[str, str, str]] = [
    # Major metros (>1k listings each)
    ("Toronto",           "/b-apartments-condos/city-of-toronto/c37l1700273",  "ON"),
    ("Montreal",          "/b-apartments-condos/city-of-montreal/c37l1700281", "QC"),
    ("Mississauga",       "/b-apartments-condos/mississauga/c37l1700276",      "ON"),
    ("Ottawa",            "/b-apartments-condos/ottawa/c37l1700185",           "ON"),
    ("Kitchener-Waterloo","/b-apartments-condos/kitchener-waterloo/c37l1700209", "ON"),
    ("Hamilton",          "/b-apartments-condos/hamilton/c37l1700212",         "ON"),
    ("Edmonton",          "/b-apartments-condos/edmonton/c37l1700203",         "AB"),
    ("Calgary",           "/b-apartments-condos/calgary/c37l1700199",          "AB"),
    ("London",            "/b-apartments-condos/london/c37l1700214",           "ON"),
    ("Winnipeg",          "/b-apartments-condos/winnipeg/c37l1700192",         "MB"),
    ("Quebec City",       "/b-apartments-condos/quebec-city/c37l1700124",      "QC"),
    # Mid-tier
    ("Halifax",           "/b-apartments-condos/halifax/c37l1700321",          "NS"),
    ("Saskatoon",         "/b-apartments-condos/saskatoon/c37l1700197",        "SK"),
    ("Vancouver",         "/b-apartments-condos/city-of-vancouver/c37l1700287","BC"),
    ("Regina",            "/b-apartments-condos/regina/c37l1700196",           "SK"),
    ("Victoria",          "/b-apartments-condos/victoria/c37l1700173",         "BC"),
    ("Burnaby",           "/b-apartments-condos/burnaby/c37l1700288",          "BC"),
    ("St. John's",        "/b-apartments-condos/st-johns/c37l1700113",         "NL"),
    ("Surrey",            "/b-apartments-condos/surrey/c37l1700290",           "BC"),
    ("Richmond",          "/b-apartments-condos/richmond/c37l1700289",         "BC"),
    ("Moncton",           "/b-apartments-condos/moncton/c37l1700064",          "NB"),
    ("Fredericton",       "/b-apartments-condos/fredericton/c37l1700061",      "NB"),
]


def _city_page_url(path: str, page: int) -> str:
    """Insert /page-N/ before the final /c37l... segment."""
    base, _, cat = path.rpartition("/")
    return f"{BASE}{base}/page-{page}/{cat}"

PROPERTY_TYPE_NORMAL = {
    "apartment": "apartment",
    "condo": "condo",
    "townhouse": "townhouse",
    "house": "house",
    "basement": "basement",
    "room": "room",
    "duplex": "duplex",
}


def _kijiji_id_from_url(url: str) -> str | None:
    # Kijiji URLs end with /<numeric-id>
    m = re.search(r"/(\d{8,})\b", url)
    return m.group(1) if m else None


def _parse_listing(url: str, html: str) -> ScrapedListing | None:
    """Parse a Kijiji listing detail page.

    Strategy: prefer the embedded JSON-LD / __NEXT_DATA__ blob over CSS selectors —
    less brittle to layout tweaks. Fall back to visible-text scraping if missing.
    """
    source_id = _kijiji_id_from_url(url)
    if not source_id:
        return None

    tree = HTMLParser(html)

    next_data = _extract_next_data(tree)
    if next_data:
        listing = _from_next_data(url, source_id, next_data)
        if listing:
            return listing

    # Fallback: pull what we can from rendered HTML.
    return _from_html(url, source_id, tree)


def _extract_next_data(tree: HTMLParser) -> dict | None:
    node = tree.css_first("script#__NEXT_DATA__")
    if not node or not node.text():
        return None
    try:
        return json.loads(node.text())
    except json.JSONDecodeError:
        return None


def _from_next_data(url: str, source_id: str, data: dict) -> ScrapedListing | None:
    """Parse Kijiji's __NEXT_DATA__ blob from a single listing detail page.

    Real-estate listings live in Apollo's normalized cache at:
        data.props.pageProps.__APOLLO_STATE__["RealEstateListing:<id>"]
    """
    state = (
        data.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {})
    )
    listing = next(
        (v for k, v in state.items() if k.startswith("RealEstateListing:")),
        None,
    )
    if not listing:
        return None
    return _listing_from_apollo(listing, url)


def _listings_from_search(html: str) -> list[ScrapedListing]:
    """Every listing embedded in a search-results page's __NEXT_DATA__.

    Kijiji's search page carries the full RealEstateListing objects for all ~40
    cards in its Apollo cache, so we parse them directly from the one search
    request — no per-listing detail fetch. Those detail fetches (≈37 extra
    requests per page) were exactly what tripped Kijiji's 429 rate limiter.
    """
    tree = HTMLParser(html)
    data = _extract_next_data(tree)
    if not data:
        return []
    state = data.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {})
    out: list[ScrapedListing] = []
    for key, obj in state.items():
        if key.startswith("RealEstateListing:"):
            listing = _listing_from_apollo(obj)
            if listing:
                out.append(listing)
    return out


def _listing_from_apollo(
    listing: dict, url: str | None = None
) -> ScrapedListing | None:
    """Build a ScrapedListing from one Apollo `RealEstateListing` object.

    The same object shape appears on a detail page (one per page) and embedded
    in a search-results page (~40 per page), so this single parser serves both.
    """
    url = url or listing.get("url") or ""
    lid = listing.get("id")
    source_id = str(lid) if lid not in (None, "") else _kijiji_id_from_url(url)
    if not source_id:
        return None

    loc = listing.get("location") or {}
    coords = loc.get("coordinates") or {}
    address_full = loc.get("address") or ""
    province = _province_from_address(address_full) or ""

    # price.amount is in cents.
    price_blob = listing.get("price") or {}
    cents = price_blob.get("amount") if isinstance(price_blob, dict) else None
    monthly_rent = int(cents / 100) if isinstance(cents, int | float) else parse_money(price_blob)

    attrs = _flatten_attrs(listing.get("attributes"))

    return ScrapedListing(
        source="kijiji",
        source_id=source_id,
        url=url,
        title=listing.get("title"),
        address=address_full or None,
        city=loc.get("name") or "",
        province=province,
        postal_code=_postal_from_address(address_full),
        lat=_safe_float(coords.get("latitude")),
        lng=_safe_float(coords.get("longitude")),
        monthly_rent=monthly_rent,
        bedrooms=_bedrooms_from_attr(attrs.get("numberbedrooms")),
        bathrooms=_bathrooms_from_attr(attrs.get("numberbathrooms")),
        sqft=_safe_int(attrs.get("areainfeet")),
        property_type=_normalise_pt(attrs.get("unittype")),
        furnished=_yesno(attrs.get("furnished")),
        pet_friendly=_yesno(attrs.get("petsallowed")),
        utilities_included=_parse_utilities(attrs),
        lease_length_months=_lease_months(attrs.get("agreementtype") or attrs.get("leaseterm")),
        description=_strip_html(listing.get("description")),
    )


_PROVINCE_IN_ADDR = re.compile(
    r"\b(AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\b", re.IGNORECASE
)
_POSTAL_RE = re.compile(r"\b([A-Z]\d[A-Z])\s?(\d[A-Z]\d)\b", re.IGNORECASE)


def _province_from_address(addr: str) -> str | None:
    m = _PROVINCE_IN_ADDR.search(addr or "")
    return m.group(1).upper() if m else None


def _postal_from_address(addr: str) -> str | None:
    m = _POSTAL_RE.search(addr or "")
    return f"{m.group(1).upper()} {m.group(2).upper()}" if m else None


def _strip_html(s: str | None) -> str | None:
    if not s:
        return None
    text = re.sub(r"<[^>]+>", " ", s)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _bedrooms_from_attr(v) -> float | None:
    """Kijiji's numberbedrooms is a string code: '0' = bachelor, '1', '2', ..."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("0", "bachelor", "studio"):
        return 0.5
    try:
        return float(s)
    except ValueError:
        return None


def _bathrooms_from_attr(v) -> float | None:
    """Kijiji encodes bathrooms as integer × 10: '15' = 1.5, '20' = 2.0, '25' = 2.5."""
    if v is None:
        return None
    try:
        return float(v) / 10.0
    except (TypeError, ValueError):
        return None


def _lease_months(v) -> int | None:
    if v is None:
        return None
    s = str(v).lower()
    if "month-to-month" in s or "monthtomonth" in s:
        return 1
    m = re.search(r"(\d+)\s*(?:month|mo)", s)
    return int(m.group(1)) if m else None


def _from_html(url: str, source_id: str, tree: HTMLParser) -> ScrapedListing:
    def text(sel: str) -> str | None:
        node = tree.css_first(sel)
        return node.text(strip=True) if node else None

    title = text("h1") or text("[data-testid='vip-title']")
    rent = parse_money(text("[data-testid='vip-price']") or text(".priceContainer"))
    address = text("[data-testid='vip-address']") or text(".address")
    description = text("[data-testid='vip-description']") or text(".descriptionContainer")
    return ScrapedListing(
        source="kijiji",
        source_id=source_id,
        url=url,
        title=title,
        address=address,
        city="",
        province="",
        monthly_rent=rent,
        description=description,
    )


def _flatten_attrs(attrs) -> dict[str, str]:
    """Kijiji v2 ships attributes as {all: [{canonicalName, canonicalValues, values}]}.
    Older shapes use a flat list of {machineKey, machineValue}. Handle both."""
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


def _safe_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _safe_int(v) -> int | None:
    try:
        return int(float(v)) if v is not None else None
    except (TypeError, ValueError):
        return None


def _yesno(v) -> bool | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "limited"):
        return True
    if s in ("0", "false", "no", "n"):
        return False
    return None


def _normalise_pt(v) -> str | None:
    if not v:
        return None
    s = str(v).lower().strip()
    for needle, normalised in PROPERTY_TYPE_NORMAL.items():
        if needle in s:
            return normalised
    return None


UTILITY_KEYS = {
    "heatincluded": "heat",
    "hydroincluded": "hydro",
    "electricityincluded": "hydro",
    "waterincluded": "water",
    "internetincluded": "internet",
    "cableincluded": "cable",
}


def _parse_utilities(attrs: dict[str, str]) -> list[str]:
    out = []
    for key, label in UTILITY_KEYS.items():
        if attrs.get(key) in ("1", "true", "yes"):
            out.append(label)
    return out


FLUSH_EVERY = 100  # upsert in batches so a long crawl is crash-resilient
HARD_PAGE_CAP = 100  # safety stop per scope (recency=all-canada, per-city=one city)


async def _crawl(
    client: PoliteClient,
    label: str,
    next_url,
    max_count: int,
    dry_run: bool,
    batch: list[ScrapedListing],
    flush_cb,
) -> int:
    """Drive one search-result paginator. `next_url(page) -> str` builds each page URL.

    Appends parsed listings to `batch`. When `len(batch) >= FLUSH_EVERY` and not
    dry_run, calls `flush_cb()` to upsert + reset. Returns count collected this run.
    """
    collected = 0
    page = 1
    while collected < max_count and page <= HARD_PAGE_CAP:
        url = next_url(page)
        try:
            r = await client.get(url)
        except httpx.HTTPStatusError as e:
            # Surface the status so we can tell *why*: 403 = bot/IP block,
            # 429 = rate-limited, 5xx = server-side. (Page 1 often succeeds
            # while page 2+ gets blocked — that pattern points to throttling.)
            ra = e.response.headers.get("Retry-After")
            extra = f", Retry-After={ra}" if ra else ""
            print(f"  [{label}] page {page} HTTP {e.response.status_code}{extra} "
                  f"({e.request.url})")
            break
        except Exception as e:
            print(f"  [{label}] page {page} fetch failed ({e!r})")
            break

        # Parse every listing straight out of the search page's embedded data —
        # one request per page, no per-listing detail fetches. This is what keeps
        # us under Kijiji's 429 rate limit (and it's ~40× fewer requests).
        listings = _listings_from_search(r.text)
        if not listings:
            break

        added = 0
        for parsed in listings:
            if collected >= max_count:
                break
            if parsed.monthly_rent and parsed.city:
                batch.append(parsed)
                added += 1
                collected += 1

        print(f"  [{label}] page {page}: +{added} (city total {collected}/{max_count})")
        if not dry_run and len(batch) >= FLUSH_EVERY:
            await flush_cb(batch)
        # Small cities exhaust quickly — Kijiji often keeps returning duplicates
        # or out-of-city listings instead of a 404. If we just paid for a page
        # and got nothing useful, stop.
        if added == 0 and collected > 0:
            break
        page += 1
    return collected


async def scrape(
    *,
    max_listings: int | None,
    per_city: int | None,
    dry_run: bool,
) -> None:
    """Run either recency mode (single national paginator) or per-city round-robin."""
    batch: list[ScrapedListing] = []
    totals = {"parsed": 0, "inserted": 0, "updated": 0}

    async def flush(b: list[ScrapedListing]) -> None:
        if not b:
            return
        ins, upd = await upsert_listings(b)
        totals["parsed"] += len(b)
        totals["inserted"] += ins
        totals["updated"] += upd
        print(f"    flushed: +{ins} new, {upd} refreshed "
              f"(running: {totals['inserted']} new / {totals['updated']} refreshed)")
        b.clear()

    # Now that listings come straight from the search page's embedded data, a
    # whole city is just ~13 page requests instead of ~500 detail fetches — so
    # the old 429 storm is gone and a modest delay keeps us well under the limit.
    async with PoliteClient(
        max_concurrency=2, min_delay_ms=400, max_delay_ms=1000
    ) as client:
        if per_city is not None:
            print(f"=== per-city round-robin: {per_city}/city × {len(CITIES)} cities ===\n")
            for name, path, province in CITIES:
                print(f"--- {name} ({province}) ---")
                await _crawl(
                    client,
                    label=name,
                    next_url=lambda p, _path=path: _city_page_url(_path, p),
                    max_count=per_city,
                    dry_run=dry_run,
                    batch=batch,
                    flush_cb=flush,
                )
        else:
            assert max_listings is not None
            print(f"=== recency mode: {max_listings} listings ===\n")
            await _crawl(
                client,
                label="canada",
                next_url=lambda p: BASE + SEARCH_PATH_TEMPLATE.format(page=p),
                max_count=max_listings,
                dry_run=dry_run,
                batch=batch,
                flush_cb=flush,
            )

    if dry_run:
        print(f"\ndry-run: parsed {len(batch)} listings (none written)")
        for listing in batch[:3]:
            print(json.dumps(listing.__dict__, indent=2, default=str))
        return

    await flush(batch)
    print(f"\nfinal: {totals['parsed']} parsed · "
          f"{totals['inserted']} new · {totals['updated']} refreshed")
    stale = await mark_stale("kijiji", hours=72)
    print(f"marked stale: {stale}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--per-city",
        type=int,
        metavar="N",
        help="Round-robin: take up to N listings from each city in CITIES. "
             "Balanced national coverage. Recommended.",
    )
    mode.add_argument(
        "--max-listings",
        type=int,
        metavar="N",
        help="Recency mode: take N listings from Kijiji's national feed (sorted by "
             "recency). Faster but heavily skewed toward whatever's been posted lately.",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.per_city is None and args.max_listings is None:
        args.per_city = 250  # sensible default
    asyncio.run(scrape(
        max_listings=args.max_listings,
        per_city=args.per_city,
        dry_run=args.dry_run,
    ))
