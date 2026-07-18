"""Per-listing nearest-amenity distances.

For each active listing, scan POIs within a 5km radius and store the minimum
distance to each poi_type in `listings.amenity_distances_m` (JSONB).

Designed to be re-run after every refresh (cheap thanks to the GIST index).

Run:
    cd backend && python -m etl.compute_amenity_distances
"""

import asyncio
import time

from etl._common import connect

RADIUS_M = 5000  # match the radius implied by amenity_distances_m semantics


SQL_UPDATE = f"""
WITH chunk_listings AS (
    SELECT id, location
      FROM listings
     WHERE status='active' AND location IS NOT NULL
       AND id = ANY($1::int[])
),
nearest AS (
    SELECT
        l.id AS listing_id,
        p.poi_type,
        MIN(ST_Distance(p.location::geography, l.location::geography))::int AS m
    FROM chunk_listings l
    JOIN pois p
      ON ST_DWithin(p.location::geography, l.location::geography, {RADIUS_M})
    GROUP BY l.id, p.poi_type
),
rolled AS (
    SELECT listing_id, jsonb_object_agg(poi_type, m) AS amenities
    FROM nearest
    GROUP BY listing_id
)
UPDATE listings l
   SET amenity_distances_m = COALESCE(r.amenities, '{{}}'::jsonb)
  FROM chunk_listings c
  LEFT JOIN rolled r ON r.listing_id = c.id
 WHERE l.id = c.id
   AND l.amenity_distances_m IS DISTINCT FROM COALESCE(r.amenities, '{{}}'::jsonb)
RETURNING l.id;
"""


async def main() -> None:
    t0 = time.time()
    async with connect() as conn:
        # Sanity check
        poi_count = await conn.fetchval("SELECT COUNT(*) FROM pois")
        active_listings = await conn.fetchval(
            "SELECT COUNT(*) FROM listings WHERE status='active' AND location IS NOT NULL"
        )
        if poi_count == 0:
            print("no POIs in the DB yet — run etl/load_osm_pois_geofabrik first")
            return
        print(f"computing distances · {active_listings} active listings · {poi_count} POIs")

        # Chunk listings so each spatial-join query stays under Supabase's
        # pooler statement_timeout. 100 listings/chunk × ~5km radius typically
        # finishes in 1–3s server-side. Each chunk writes the final value in
        # one pass — LEFT JOIN so listings with no POIs in range get '{}'
        # (removed categories don't linger), IS DISTINCT FROM so unchanged
        # rows are skipped (a listings write costs ~75ms in HNSW index
        # re-insert alone; a whole-table rewrite blows the 2min timeout).
        all_ids = [r["id"] for r in await conn.fetch(
            "SELECT id FROM listings WHERE status='active' AND location IS NOT NULL ORDER BY id"
        )]
        CHUNK = 100
        total_updated = 0
        for i in range(0, len(all_ids), CHUNK):
            batch = all_ids[i:i + CHUNK]
            rows = await conn.fetch(SQL_UPDATE, batch)
            total_updated += len(rows)
            print(f"  {i + len(batch):4d}/{len(all_ids)}  (+{len(rows)})", flush=True)

    print(f"updated {total_updated} changed listings in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
