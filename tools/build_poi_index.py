"""Build the bundled offline POI index shipped inside the Actor image.

Why: live Overpass enrichment is rate-limited (429s) and serialized at ~1s/listing
— the slow tail of any `nearAmenities` run. POIs are static infrastructure, so we
snapshot them once into a compact file the Actor loads at startup and queries
in-process (see src/amenities_local.py). Sub-second for hundreds of listings, zero
external dependency at run time.

Source: a Postgres `pois` table of already-classified OSM POIs (historically the
EZrelocate backend's, loaded offline from Geofabrik province extracts). We only
need (poi_type, lat, lng) — no names/tags — so the dump is tiny (~1 MB compressed
for all of Canada).

Run (from repo root, with any venv that has asyncpg + numpy; DATABASE_URL from
the environment or the repo-root .env):
    .venv/bin/python tools/build_poi_index.py

Re-run + `apify push` to refresh the snapshot.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
import numpy as np

# The Actor's amenity categories (must match src/enrich.AMENITY_FILTERS and the
# input_schema `nearAmenities` enum). The source `pois` table also has `lrt`,
# which the Actor doesn't expose, so we skip it.
CATEGORIES = [
    "subway", "train", "bus_stop", "grocery", "cafe", "pharmacy",
    "park", "school", "university", "library", "gym", "hospital",
]

OUT_NPZ = Path(__file__).resolve().parents[1] / "src" / "data" / "pois_ca.npz"
OUT_META = OUT_NPZ.with_suffix(".meta.json")

# rows pulled per category; lat/lng only, dropping anything without a location.
SQL = """
    SELECT ST_Y(location::geometry) AS lat, ST_X(location::geometry) AS lng
      FROM pois
     WHERE poi_type = $1 AND location IS NOT NULL
"""


def _load_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        # Fall back to the repo-root .env.
        env = Path(__file__).resolve().parents[1] / ".env"
        for line in env.read_text().splitlines() if env.exists() else []:
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not url:
        sys.exit("DATABASE_URL not set (env or repo-root .env)")
    return url


async def main() -> None:
    url = _load_database_url()
    conn = await asyncpg.connect(url)
    arrays: dict[str, np.ndarray] = {}
    counts: dict[str, int] = {}
    try:
        for cat in CATEGORIES:
            rows = await conn.fetch(SQL, cat)
            # [N, 2] float32 (lat, lng). float32 keeps ~0.5 m precision at city
            # scale and halves the bundle size.
            arr = np.array([(r["lat"], r["lng"]) for r in rows], dtype=np.float32)
            if arr.size == 0:
                arr = np.empty((0, 2), dtype=np.float32)
            arrays[cat] = arr
            counts[cat] = len(rows)
            print(f"  {cat:12} {len(rows):>7}")
    finally:
        await conn.close()

    OUT_NPZ.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT_NPZ, **arrays)

    total = sum(counts.values())
    allpts = np.concatenate([a for a in arrays.values() if len(a)]) if total else np.empty((0, 2))
    bbox = (
        {
            "lat": [float(allpts[:, 0].min()), float(allpts[:, 0].max())],
            "lng": [float(allpts[:, 1].min()), float(allpts[:, 1].max())],
        }
        if total
        else None
    )
    meta = {
        "source": "EZrelocate pois table (Geofabrik-derived)",
        "categories": CATEGORIES,
        "counts": counts,
        "total": total,
        "bbox": bbox,
        "dtype": "float32",
        "note": "Regenerate with tools/build_poi_index.py then `apify push`.",
    }
    OUT_META.write_text(json.dumps(meta, indent=2))
    size_kb = OUT_NPZ.stat().st_size / 1024
    print(f"\nwrote {OUT_NPZ}  ({size_kb:.0f} KB, {total} POIs)")
    print(f"wrote {OUT_META}")


if __name__ == "__main__":
    asyncio.run(main())
