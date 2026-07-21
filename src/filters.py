"""Listing predicates — the server-side filters that let a Claude/MCP prompt like
'2-bed under $2500 near a subway, female-only' be pushed down to the actor instead
of done after the fact over a huge JSON blob.

Split by cost so the caller can apply them in the right order:
  1. `passes_basic`  — free (rent/beds/keywords on already-scraped fields)
  2. `within_point`  — cheap (one haversine; needs a geocoded target)
  3. `passes_amenities` — requires enrichment to have run first
"""

from __future__ import annotations

from .enrich import _haversine_m
from .models import Listing


def _haystack(m: Listing) -> str:
    return f"{m.title or ''} {m.description or ''}".lower()


def passes_basic(
    m: Listing,
    *,
    min_rent: int | None = None,
    max_rent: int | None = None,
    min_beds: float | None = None,
    max_beds: float | None = None,
    keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
) -> bool:
    """Rent / bedroom / keyword filters over fields already on the listing."""
    if min_rent is not None and (m.monthly_rent is None or m.monthly_rent < min_rent):
        return False
    if max_rent is not None and (m.monthly_rent is None or m.monthly_rent > max_rent):
        return False
    if min_beds is not None and (m.bedrooms is None or m.bedrooms < min_beds):
        return False
    if max_beds is not None and (m.bedrooms is None or m.bedrooms > max_beds):
        return False
    if keywords:
        hay = _haystack(m)
        # ALL keywords must appear (AND) — narrows, e.g. ["female", "parking"].
        if not all(k.lower() in hay for k in keywords):
            return False
    if exclude_keywords:
        hay = _haystack(m)
        if any(k.lower() in hay for k in exclude_keywords):
            return False
    return True


def within_point(m: Listing, lat: float, lng: float, radius_m: int) -> bool:
    """True if the listing is within `radius_m` straight-line of (lat, lng)."""
    if m.lat is None or m.lng is None:
        return False
    return _haversine_m(m.lat, m.lng, lat, lng) <= radius_m


def passes_amenities(
    m: Listing, near_amenities: list[str], max_distance_m: int
) -> bool:
    """True if EVERY requested amenity is within max_distance_m of the listing.

    Requires `amenity_distances_m` to be populated (enrichment must have run).
    """
    dists = m.amenity_distances_m or {}
    return all(
        a in dists and dists[a] <= max_distance_m for a in near_amenities
    )
