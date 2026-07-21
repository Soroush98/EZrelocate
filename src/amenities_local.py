"""In-process nearest-amenity lookup against the bundled offline POI index.

Replaces the live, rate-limited Overpass calls (see enrich.py) with a local
nearest-neighbor query over POIs snapshotted into src/data/pois_ca.npz at build
time (tools/build_poi_index.py). Sub-second for hundreds of listings, no network.

Method: each POI's (lat, lng) is mapped to a 3-D unit vector on the sphere, so the
nearest POI by great-circle distance is the one with the largest dot product with
the listing's unit vector — a single vectorised matmul + argmax per category. We
then recompute that one pair's distance with the exact haversine formula (avoids
the acos precision loss near dot≈1) and keep it only if within the radius, matching
Overpass's "found within radius" semantics.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

_EARTH_R_M = 6_371_000.0
_DATA = Path(__file__).resolve().parent / "data" / "pois_ca.npz"


def _to_unit_xyz(latlng: np.ndarray) -> np.ndarray:
    """[N,2] (lat,lng) degrees -> [N,3] unit vectors (float64).

    float64 is required, not an optimization: nearest-neighbor here is argmax of the
    dot product between unit vectors, and at city scale competing POIs sit at dot
    ~0.99999999. float32 (~1e-7 relative precision near 1.0) collapses them to the
    same value and argmax returns an arbitrary one; float64 (~1e-16) resolves them.
    """
    lat = np.radians(latlng[:, 0].astype(np.float64))
    lng = np.radians(latlng[:, 1].astype(np.float64))
    cl = np.cos(lat)
    return np.column_stack((cl * np.cos(lng), cl * np.sin(lng), np.sin(lat)))


def _haversine_m(lat1, lng1, lat2, lng2) -> np.ndarray:
    """Vectorised great-circle distance in metres (float64 inputs in degrees)."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = np.radians(lat2 - lat1)
    dl = np.radians(lng2 - lng1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * _EARTH_R_M * np.arcsin(np.sqrt(a))


class PoiIndex:
    """Loaded once and reused; holds per-category POI coordinates + unit vectors."""

    def __init__(self, path: Path = _DATA) -> None:
        with np.load(path) as data:
            # cat -> (latlng [N,2] float32, xyz [N,3] float32)
            self._cats: dict[str, tuple[np.ndarray, np.ndarray]] = {}
            self.total = 0
            for cat in data.files:
                latlng = data[cat]
                self._cats[cat] = (latlng, _to_unit_xyz(latlng))
                self.total += len(latlng)
        self.categories = list(self._cats)

    def nearest_batch(
        self,
        lats: np.ndarray,
        lngs: np.ndarray,
        radius_m: float,
        categories: list[str] | None = None,
    ) -> list[dict[str, dict]]:
        """For each (lat,lng), the nearest POI per category within radius_m, located.
        Returns one dict per input listing: {category: {"m": dist, "lat": .., "lng": ..}}."""
        lats = np.asarray(lats, dtype=np.float64)
        lngs = np.asarray(lngs, dtype=np.float64)
        m = len(lats)
        out: list[dict[str, dict]] = [dict() for _ in range(m)]
        if m == 0:
            return out
        listing_xyz = _to_unit_xyz(np.column_stack((lats, lngs)))  # [M,3]
        wanted = categories or self.categories
        # Chunk listings so the transient [N_cat, chunk] dot matrix stays bounded
        # (~200 MB worst case for bus_stop) regardless of how many we enrich.
        chunk = 256
        for cat in wanted:
            entry = self._cats.get(cat)
            if entry is None or len(entry[0]) == 0:
                continue
            latlng, xyz = entry
            for s in range(0, m, chunk):
                e = min(s + chunk, m)
                # [N, c] dot products; nearest POI per listing = argmax over POIs.
                dots = xyz @ listing_xyz[s:e].T
                idx = np.argmax(dots, axis=0)  # [c]
                nlat, nlng = latlng[idx, 0].astype(np.float64), latlng[idx, 1].astype(np.float64)
                dist = _haversine_m(lats[s:e], lngs[s:e], nlat, nlng)
                for k in np.nonzero(dist <= radius_m)[0]:
                    out[s + int(k)][cat] = {
                        "m": int(round(float(dist[k]))),
                        "lat": round(float(nlat[k]), 6),
                        "lng": round(float(nlng[k]), 6),
                    }
        return out


_INDEX: PoiIndex | None = None
_LOAD_FAILED = False


def get_index(log=None) -> PoiIndex | None:
    """Lazily load the bundled index once. Returns None (logged once) if the data
    file is missing or unreadable, so the caller can fall back to Overpass."""
    global _INDEX, _LOAD_FAILED
    if _INDEX is not None or _LOAD_FAILED:
        return _INDEX
    try:
        _INDEX = PoiIndex()
        if log:
            log.info(
                f"[enrich] loaded bundled POI index: {_INDEX.total} POIs across "
                f"{len(_INDEX.categories)} categories"
            )
    except Exception as e:  # noqa: BLE001 — degrade to the Overpass fallback
        _LOAD_FAILED = True
        if log:
            log.warning(f"[enrich] bundled POI index unavailable ({e!r}); using Overpass")
    return _INDEX
