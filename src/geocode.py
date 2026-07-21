"""Geocode a free-text place/address to one coordinate, for 'near X' filtering.

Used by the `nearAddress` input ("near my office at 200 Bay St, Toronto"). This
is the right mechanism for a *specific* place — unlike amenity categories, you
can't assume OSM has a given employer tagged, so we resolve the address itself
to a point and measure distance to it.

Uses OSM's free Nominatim geocoder (1 req/run here, well within its usage policy).
"""

from __future__ import annotations

import httpx

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "canadian-rentals-unified/0.1 (Apify actor)"}


async def geocode(query: str, *, country: str = "ca") -> tuple[float, float] | None:
    """Return (lat, lng) for the best match, or None if nothing resolves."""
    if not query or not query.strip():
        return None
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            NOMINATIM_URL,
            params={
                "q": query,
                "format": "json",
                "countrycodes": country,
                "limit": 1,
            },
            headers=HEADERS,
        )
        r.raise_for_status()
        results = r.json()
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])
