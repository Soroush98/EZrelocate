"""Shared scraping helpers: polite HTTP client, listing upsert, ID extraction.

Both Kijiji and rentals.ca scrapers use these. The HTTP client throttles concurrent
requests (semaphore) and inserts a per-request jitter so we don't hammer sites.
"""

import asyncio
import os
import random
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from etl._common import connect

# Override via SCRAPER_USER_AGENT in production with a real contact address.
# The default is intentionally generic to avoid PII in this public repo.
USER_AGENT = os.environ.get(
    "SCRAPER_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


@dataclass
class ScrapedListing:
    source: str
    source_id: str
    url: str
    title: str | None = None
    address: str | None = None
    city: str = ""
    province: str = ""
    postal_code: str | None = None
    lat: float | None = None
    lng: float | None = None
    monthly_rent: int | None = None
    bedrooms: float | None = None
    bathrooms: float | None = None
    sqft: int | None = None
    property_type: str | None = None
    furnished: bool | None = None
    pet_friendly: bool | None = None
    utilities_included: list[str] = field(default_factory=list)
    lease_length_months: int | None = None
    available_from: date | None = None
    description: str | None = None


def _is_retryable(exc: BaseException) -> bool:
    """Retry only on transient failures: 429, 5xx, and network/timeout errors.

    A 403 (bot/IP block) or 404 won't change on retry, so we let it propagate
    immediately rather than burning the retry budget on a lost cause.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, httpx.TransportError | httpx.TimeoutException)


_expo_wait = wait_exponential(min=2, max=15)


def _wait_with_retry_after(retry_state) -> float:
    """Honor a server's Retry-After header on 429, else exponential backoff."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError):
        ra = exc.response.headers.get("Retry-After", "")
        if ra.isdigit():
            return min(float(ra), 60.0)
    return _expo_wait(retry_state)


class PoliteClient:
    """Async HTTP client with a concurrency cap and per-request jitter."""

    def __init__(
        self,
        *,
        max_concurrency: int = 3,
        min_delay_ms: int = 500,
        max_delay_ms: int = 1500,
        timeout: float = 30.0,
    ) -> None:
        self._sem = asyncio.Semaphore(max_concurrency)
        self._min = min_delay_ms / 1000
        self._max = max_delay_ms / 1000
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-CA,en;q=0.9",
            },
            follow_redirects=True,
        )

    async def __aenter__(self) -> "PoliteClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self._client.aclose()

    @retry(
        stop=stop_after_attempt(3),
        wait=_wait_with_retry_after,
        # Only retry transient failures. A 403/404 won't fix itself on retry —
        # failing fast both saves time and lets the caller see the real status.
        retry=retry_if_exception(_is_retryable),
        # Propagate the underlying HTTPStatusError instead of wrapping it in an
        # opaque RetryError, so callers can read .response.status_code and log
        # *why* a request failed (block vs throttle vs server error).
        reraise=True,
    )
    async def get(self, url: str) -> httpx.Response:
        async with self._sem:
            await asyncio.sleep(random.uniform(self._min, self._max))
            r = await self._client.get(url)
            r.raise_for_status()
            return r


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


def normalise_province(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return _PROVINCE_NAMES.get(v.lower())


_PRICE_RE = re.compile(r"[\d,]+(?:\.\d{1,2})?")


def parse_money(value: Any) -> int | None:
    """Pull a $-amount out of a string or number, return as integer dollars."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return int(value)
    m = _PRICE_RE.search(str(value).replace(",", ""))
    if not m:
        return None
    try:
        return int(float(m.group()))
    except ValueError:
        return None


async def upsert_listings(rows: list[ScrapedListing]) -> tuple[int, int]:
    """Bulk-upsert scraped listings. Returns (inserted, updated_existing)."""
    if not rows:
        return 0, 0

    sql = """
        INSERT INTO listings (
            source, source_id, url, title, address, city, province, postal_code,
            location, monthly_rent, bedrooms, bathrooms, sqft, property_type,
            furnished, pet_friendly, utilities_included, lease_length_months,
            available_from, description, first_seen_at, last_seen_at, status
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8,
            CASE WHEN $9::float8 IS NOT NULL AND $10::float8 IS NOT NULL
                 THEN ST_SetSRID(ST_MakePoint($9, $10), 4326)
                 ELSE NULL
            END,
            $11, $12, $13, $14, $15,
            $16, $17, $18, $19, $20, $21,
            NOW(), NOW(), 'active'
        )
        ON CONFLICT (source, source_id) DO UPDATE SET
            url                 = EXCLUDED.url,
            title               = EXCLUDED.title,
            address             = EXCLUDED.address,
            monthly_rent        = EXCLUDED.monthly_rent,
            bedrooms            = EXCLUDED.bedrooms,
            bathrooms           = EXCLUDED.bathrooms,
            sqft                = EXCLUDED.sqft,
            property_type       = EXCLUDED.property_type,
            furnished           = EXCLUDED.furnished,
            pet_friendly        = EXCLUDED.pet_friendly,
            utilities_included  = EXCLUDED.utilities_included,
            lease_length_months = EXCLUDED.lease_length_months,
            available_from      = EXCLUDED.available_from,
            description         = EXCLUDED.description,
            last_seen_at        = NOW(),
            status              = 'active'
        RETURNING (xmax = 0) AS inserted
    """

    inserted = updated = 0
    async with connect() as conn, conn.transaction():
        for r in rows:
            row = await conn.fetchrow(
                sql,
                r.source,
                r.source_id,
                r.url,
                r.title,
                r.address,
                r.city,
                r.province,
                r.postal_code,
                r.lng,
                r.lat,
                r.monthly_rent,
                r.bedrooms,
                r.bathrooms,
                r.sqft,
                r.property_type,
                r.furnished,
                r.pet_friendly,
                r.utilities_included,
                r.lease_length_months,
                r.available_from,
                r.description,
            )
            if row["inserted"]:
                inserted += 1
            else:
                updated += 1
    return inserted, updated


async def mark_stale(source: str, hours: int = 72, batch: int = 250) -> int:
    """Flip listings we haven't re-seen recently to status='stale'.

    Batched so each UPDATE stays under Supabase's 2-minute statement_timeout:
    status is indexed, so every flipped row re-inserts into all listings
    indexes, and the HNSW one costs ~75ms/row — 250 rows ≈ 20s.
    """
    total = 0
    async with connect() as conn:
        while True:
            result = await conn.execute(
                """
                UPDATE listings
                   SET status = 'stale'
                 WHERE id IN (
                        SELECT id FROM listings
                         WHERE source = $1
                           AND status = 'active'
                           AND last_seen_at < NOW() - ($2::int * INTERVAL '1 hour')
                         LIMIT $3
                       )
                """,
                source,
                hours,
                batch,
            )
            # asyncpg returns "UPDATE N"
            n = int(result.split()[-1])
            total += n
            if n < batch:
                return total
