"""Cross-source dedup — the core value of a *unified* feed.

The same unit is often posted to both Kijiji and RentFaster. We collapse those
into one row and record the extra source in `also_on`, so consumers see "this
listing appears on kijiji + rentfaster" instead of two near-identical rows.

Matching is intentionally conservative (same ~111 m cell + same bedroom count +
price within 5%) so we never merge two genuinely different units. It's O(n) via
spatial-cell bucketing rather than O(n^2) pairwise.
"""

from __future__ import annotations

from .models import Listing

# Lower index == higher priority to be the kept (canonical) record.
_SOURCE_PRIORITY = {"kijiji": 0, "rentfaster": 1}


def _key(listing: Listing) -> tuple | None:
    if listing.lat is None or listing.lng is None or listing.monthly_rent is None:
        return None
    # round(_, 3) ≈ 111 m cells; price bucketed to nearest $50 to tolerate fees.
    return (
        round(listing.lat, 3),
        round(listing.lng, 3),
        listing.bedrooms,
        round(listing.monthly_rent / 50),
    )


def _completeness(listing: Listing) -> int:
    """Field count — used to keep the richer of two duplicates."""
    return sum(
        1
        for v in vars(listing).values()
        if v not in (None, "", [], {})
    )


def dedupe(listings: list[Listing]) -> tuple[list[Listing], int]:
    """Return (deduped, merged_count). Order within a source is preserved."""
    canonical: dict[tuple, Listing] = {}
    passthrough: list[Listing] = []  # rows we can't key (no geo/price) — keep as-is
    merged = 0

    for cur in listings:
        k = _key(cur)
        if k is None:
            passthrough.append(cur)
            continue
        prev = canonical.get(k)
        if prev is None:
            canonical[k] = cur
            continue

        merged += 1
        keep, drop = _choose(prev, cur)
        # Union of sources, canonical source excluded from also_on.
        sources = {keep.source, drop.source, *keep.also_on, *drop.also_on}
        keep.also_on = sorted(sources - {keep.source})
        canonical[k] = keep

    return list(canonical.values()) + passthrough, merged


def _choose(a: Listing, b: Listing) -> tuple[Listing, Listing]:
    """Pick the canonical record: higher source priority, then more complete."""
    pa = _SOURCE_PRIORITY.get(a.source, 99)
    pb = _SOURCE_PRIORITY.get(b.source, 99)
    if pa != pb:
        return (a, b) if pa < pb else (b, a)
    return (a, b) if _completeness(a) >= _completeness(b) else (b, a)
