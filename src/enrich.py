"""Optional amenity-distance enrichment — the geo-enrichment differentiator.

For each listing we attach `amenity_distances_m`: the straight-line distance (m)
to the nearest subway / grocery / school / etc. This is what no other rental
scraper on Apify ships.

Primary path is the bundled offline POI index (src/amenities_local.py) — fast,
free, no external calls. Only if that index can't be loaded do we fall back to
the live Overpass API, which is rate-limited and best-effort: we throttle hard
(1 request at a time) and cap how many listings we enrich.
"""

from __future__ import annotations

import asyncio
import math

import httpx

from .amenities_local import get_index
from .models import Listing

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Overpass rejects requests without a User-Agent (HTTP 406). Identify ourselves.
OVERPASS_HEADERS = {"User-Agent": "canadian-rentals-unified/0.1 (Apify actor)"}

# category -> Overpass element filters (OR'd together).
AMENITY_FILTERS: dict[str, list[str]] = {
    "subway": ['nwr["railway"="subway_entrance"]', 'nwr["station"="subway"]'],
    "train": ['nwr["railway"="station"]'],
    "bus_stop": ['nwr["highway"="bus_stop"]'],
    "grocery": ['nwr["shop"="supermarket"]', 'nwr["shop"="grocery"]'],
    "cafe": ['nwr["amenity"="cafe"]'],
    "pharmacy": ['nwr["amenity"="pharmacy"]'],
    "park": ['nwr["leisure"="park"]'],
    "school": ['nwr["amenity"="school"]'],
    "university": ['nwr["amenity"="university"]'],
    "library": ['nwr["amenity"="library"]'],
    "gym": ['nwr["leisure"="fitness_centre"]'],
    "hospital": ['nwr["amenity"="hospital"]'],
}


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _build_query(lat: float, lng: float, radius_m: int) -> str:
    parts = []
    for filters in AMENITY_FILTERS.values():
        for f in filters:
            parts.append(f"{f}(around:{radius_m},{lat},{lng});")
    # `out center` makes ways/relations (parks, campuses, hospitals — which are
    # polygons, not points) report a single centroid we can measure against.
    return f"[out:json][timeout:25];({''.join(parts)});out center;"


def _nearest_by_category(
    lat: float, lng: float, elements: list[dict]
) -> dict[str, dict]:
    """Nearest POI per category, located: {cat: {"m": dist, "lat": .., "lng": ..}}."""
    best: dict[str, dict] = {}
    for el in elements:
        # Nodes carry lat/lon directly; ways/relations report it under `center`.
        elat, elng = el.get("lat"), el.get("lon")
        if elat is None or elng is None:
            center = el.get("center") or {}
            elat, elng = center.get("lat"), center.get("lon")
        if elat is None or elng is None:
            continue
        tags = el.get("tags", {})
        for cat, filters in AMENITY_FILTERS.items():
            if _matches(tags, filters):
                d = _haversine_m(lat, lng, elat, elng)
                if cat not in best or d < best[cat]["m"]:
                    best[cat] = {"m": round(d), "lat": round(elat, 6), "lng": round(elng, 6)}
    return best


def _matches(tags: dict, filters: list[str]) -> bool:
    # filters look like 'nwr["amenity"="cafe"]' — extract key=value pairs.
    for f in filters:
        kv = f[f.index("[") :]
        key = kv.split('"')[1]
        val = kv.split('"')[3]
        if tags.get(key) == val:
            return True
    return False


async def enrich(
    listings: list[Listing],
    *,
    radius_m: int,
    max_enrich: int,
    log,
    country: str = "CA",
) -> int:
    """Mutate listings in place, attaching amenity_distances_m. Returns count.

    Primary path is the bundled offline POI index (src/amenities_local.py): a local
    nearest-neighbor query, sub-second for the whole batch, no network. Only if that
    index can't be loaded do we fall back to the live Overpass API (slow + rate
    limited) — so a missing data file degrades gracefully instead of failing.
    """
    targets = [m for m in listings if m.lat is not None and m.lng is not None][
        :max_enrich
    ]
    if not targets:
        return 0
    if len(listings) > max_enrich:
        log.warning(
            f"[enrich] capping at {max_enrich} of {len(listings)} listings "
            f"(raise maxEnrich to cover more)"
        )

    index = get_index(log, country=country)
    if index is not None:
        return _enrich_local(targets, index, radius_m)
    return await _enrich_overpass(targets, radius_m, log)


def _enrich_local(targets: list[Listing], index, radius_m: int) -> int:
    """Attach amenity_distances_m + located nearby_amenities from the bundled index."""
    import numpy as np

    lats = np.fromiter((m.lat for m in targets), dtype=np.float64, count=len(targets))
    lngs = np.fromiter((m.lng for m in targets), dtype=np.float64, count=len(targets))
    results = index.nearest_batch(lats, lngs, radius_m)
    for m, near in zip(targets, results):
        if near:
            m.amenity_distances_m = {cat: v["m"] for cat, v in near.items()}
            m.nearby_amenities = [
                {"t": cat, "lat": v["lat"], "lng": v["lng"], "m": v["m"]}
                for cat, v in near.items()
            ]
    return len(targets)


async def _enrich_overpass(targets: list[Listing], radius_m: int, log) -> int:
    """Fallback: live Overpass API, one throttled request per listing."""
    done = 0
    async with httpx.AsyncClient(timeout=40.0) as client:
        for m in targets:
            try:
                r = await client.post(
                    OVERPASS_URL,
                    data={"data": _build_query(m.lat, m.lng, radius_m)},
                    headers=OVERPASS_HEADERS,
                )
                r.raise_for_status()
                elements = r.json().get("elements", [])
                near = _nearest_by_category(m.lat, m.lng, elements)
                if near:
                    m.amenity_distances_m = {cat: v["m"] for cat, v in near.items()}
                    m.nearby_amenities = [
                        {"t": cat, "lat": v["lat"], "lng": v["lng"], "m": v["m"]}
                        for cat, v in near.items()
                    ]
                done += 1
            except Exception as e:  # noqa: BLE001 — enrichment is best-effort
                log.warning(f"[enrich] {m.source}:{m.source_id} failed ({e!r})")
            # Stay well under Overpass's fair-use limits.
            await asyncio.sleep(1.0)
            if done % 25 == 0 and done:
                log.info(f"[enrich] {done}/{len(targets)} listings enriched")
    return done
