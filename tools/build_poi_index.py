"""Build a country's bundled offline POI index shipped inside the Actor image.

Why: live Overpass enrichment is rate-limited (429s) and serialized at ~1s/listing
— the slow tail of any `nearAmenities` run. POIs are static infrastructure, so we
snapshot them once into a compact file per country the Actor loads at startup and
queries in-process (see src/amenities_local.py). Sub-second for hundreds of
listings, zero external dependency at run time.

Source: Geofabrik `.osm.pbf` extracts, streamed with pyosmium and classified by
the same OSM-tag rules the EZrelocate backend used (ported from its
etl/load_osm_pois_geofabrik.py before the backend left this repo). We only keep
(poi_type, lat, lng) — no names/tags — so even the US dump stays small.

Run (any venv with numpy + httpx + `pip install osmium`; osmium is a dev-only
dep, deliberately NOT in requirements.txt — the Actor image never parses .pbf):
    .venv/bin/python tools/build_poi_index.py --country CA
    .venv/bin/python tools/build_poi_index.py --country AU --pbf /path/australia.osm.pbf

Downloads go to --cache (default: a temp dir, so pass --cache to keep them across
runs; the US state set totals ~12 GB). Re-run + `apify push` to refresh a snapshot.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx
import numpy as np

try:
    import osmium
except ImportError:
    sys.exit("pyosmium missing — this build tool needs it: pip install osmium")

# The Actor's amenity categories (must match src/enrich.AMENITY_FILTERS and the
# input_schema `nearAmenities` enum), each with Overpass-style OSM tag filters.
# classify() checks them in declaration order and the first match wins, so e.g.
# a subway entrance never gets labelled bus_stop. (The old backend also had an
# `lrt` category; the Actor doesn't expose it, so it's dropped here.)
CATEGORIES: list[tuple[str, list[str]]] = [
    ("subway",     ['["railway"="subway_entrance"]',
                    '["public_transport"="station"]["subway"="yes"]',
                    '["station"="subway"]']),
    ("train",      ['["railway"="station"]["station"!="subway"]["station"!="light_rail"]',
                    '["railway"="halt"]']),
    ("bus_stop",   ['["highway"="bus_stop"]']),
    ("grocery",    ['["shop"="supermarket"]',
                    '["shop"="convenience"]']),
    ("cafe",       ['["amenity"="cafe"]',
                    '["shop"="coffee"]']),
    ("pharmacy",   ['["amenity"="pharmacy"]']),
    ("park",       ['["leisure"="park"]',
                    '["leisure"="playground"]']),
    ("school",     ['["amenity"="school"]',
                    '["amenity"="kindergarten"]',
                    '["amenity"="childcare"]']),
    ("university", ['["amenity"="university"]',
                    '["amenity"="college"]']),
    ("library",    ['["amenity"="library"]']),
    ("gym",        ['["leisure"="fitness_centre"]',
                    '["leisure"="sports_centre"]']),
    ("hospital",   ['["amenity"="hospital"]',
                    '["amenity"="clinic"]']),
]

GEOFABRIK = "https://download.geofabrik.de"

# Country -> Geofabrik extract paths. Multi-file sets (CA provinces, US states)
# keep peak RAM/disk bounded vs. one continent-scale file.
EXTRACTS: dict[str, list[str]] = {
    "CA": [
        f"north-america/canada/{slug}" for slug in (
            "alberta", "british-columbia", "manitoba", "new-brunswick",
            "newfoundland-and-labrador", "northwest-territories", "nova-scotia",
            "nunavut", "ontario", "prince-edward-island", "quebec",
            "saskatchewan", "yukon",
        )
    ],
    "US": [
        f"north-america/us/{slug}" for slug in (
            "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
            "connecticut", "delaware", "district-of-columbia", "florida",
            "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
            "kansas", "kentucky", "louisiana", "maine", "maryland",
            "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
            "montana", "nebraska", "nevada", "new-hampshire", "new-jersey",
            "new-mexico", "new-york", "north-carolina", "north-dakota", "ohio",
            "oklahoma", "oregon", "pennsylvania", "rhode-island",
            "south-carolina", "south-dakota", "tennessee", "texas", "utah",
            "vermont", "virginia", "washington", "west-virginia", "wisconsin",
            "wyoming",
        )
    ],
    "GB": ["europe/united-kingdom"],
    "AU": ["australia-oceania/australia"],
}

# Disk-backed node-location index so a big extract can't blow RAM while we
# resolve way geometries.
_INDEX_TYPE = "sparse_file_array"


def classify(tags: dict[str, str]) -> str | None:
    for poi_type, stanzas in CATEGORIES:
        for stanza in stanzas:
            if _stanza_matches(stanza, tags):
                return poi_type
    return None


def _stanza_matches(stanza: str, tags: dict[str, str]) -> bool:
    """Parse a filter like '["amenity"="cafe"]' and check tags.

    Supports k="v" equality and k!="v" inequality (no regex, no fancy stuff).
    """
    for raw in stanza.strip("[]").split("]["):
        if "!=" in raw:
            k, v = raw.split("!=", 1)
            if tags.get(k.strip('"')) == v.strip('"'):
                return False
        elif "=" in raw:
            k, v = raw.split("=", 1)
            if tags.get(k.strip('"')) != v.strip('"'):
                return False
        elif raw.strip('"') not in tags:  # tag-presence only, e.g. ["wheelchair"]
            return False
    return True


class _POIHandler(osmium.SimpleHandler):
    """Collect classified POI coordinates from nodes and ways.

    Ways (parks, schools, hospitals mapped as polygons) get a centroid averaged
    from their node coordinates — exact enough for amenity-proximity scoring.
    Relations are skipped: almost all POIs we care about are nodes or single
    closed ways. `seen` is shared across extracts so border overlaps between
    adjacent Geofabrik files don't double-count an element.
    """

    def __init__(self, coords: dict[str, list[tuple[float, float]]], seen: set) -> None:
        super().__init__()
        self.coords = coords
        self.seen = seen

    def node(self, n) -> None:
        if n.location.valid():
            self._consider(n.tags, ("n", n.id), n.location.lat, n.location.lon)

    def way(self, w) -> None:
        try:
            pts = [(nd.lat, nd.lon) for nd in w.nodes if nd.location.valid()]
        except osmium.InvalidLocationError:
            pts = []
        if not pts:
            return
        lat = sum(p[0] for p in pts) / len(pts)
        lon = sum(p[1] for p in pts) / len(pts)
        self._consider(w.tags, ("w", w.id), lat, lon)

    def _consider(self, tags, key: tuple, lat: float, lon: float) -> None:
        td = {t.k: t.v for t in tags}
        poi_type = classify(td)
        if poi_type and key not in self.seen:
            self.seen.add(key)
            self.coords[poi_type].append((lat, lon))


def _download(path: str, dest_dir: Path) -> Path:
    url = f"{GEOFABRIK}/{path}-latest.osm.pbf"
    out = dest_dir / f"{path.rsplit('/', 1)[-1]}-latest.osm.pbf"
    if out.exists() and out.stat().st_size > 0:
        print(f"  using cached {out} ({out.stat().st_size / 1e6:.0f} MB)")
        return out
    print(f"  downloading {url}")
    with httpx.stream("GET", url, follow_redirects=True, timeout=600) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=1 << 20):
                f.write(chunk)
    print(f"  saved {out} ({out.stat().st_size / 1e6:.0f} MB)")
    return out


def build(country: str, pbfs: list[Path], out_npz: Path) -> None:
    coords: dict[str, list[tuple[float, float]]] = {c: [] for c, _ in CATEGORIES}
    seen: set = set()
    for pbf in pbfs:
        print(f"  extracting {pbf.name} ...")
        handler = _POIHandler(coords, seen)
        with tempfile.NamedTemporaryFile(suffix=".idx") as idx_file:
            handler.apply_file(
                str(pbf), locations=True, idx=f"{_INDEX_TYPE},{idx_file.name}"
            )
        print(f"    running total: {sum(len(v) for v in coords.values())} POIs")

    arrays = {
        cat: np.asarray(pts, dtype=np.float32).reshape(-1, 2)
        for cat, pts in coords.items()
    }
    total = int(sum(len(a) for a in arrays.values()))
    if not total:
        sys.exit("no POIs extracted — wrong extract, or a broken .pbf?")

    out_npz.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_npz, **arrays)
    stacked = np.vstack([a for a in arrays.values() if len(a)])
    meta = {
        "source": f"Geofabrik {country} extracts ({len(pbfs)} file(s))",
        "categories": [c for c, _ in CATEGORIES],
        "counts": {cat: int(len(a)) for cat, a in arrays.items()},
        "total": total,
        "bbox": {
            "lat": [float(stacked[:, 0].min()), float(stacked[:, 0].max())],
            "lng": [float(stacked[:, 1].min()), float(stacked[:, 1].max())],
        },
        "dtype": "float32",
        "note": "Regenerate with tools/build_poi_index.py then `apify push`.",
    }
    out_npz.with_suffix("").with_suffix(".meta.json").write_text(
        json.dumps(meta, indent=2) + "\n"
    )
    size_mb = out_npz.stat().st_size / 1e6
    print(f"\nwrote {out_npz} ({size_mb:.1f} MB): {total} POIs")
    for cat, a in arrays.items():
        print(f"  {cat:<11} {len(a)}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--country", required=True, choices=sorted(EXTRACTS),
                   help="ISO code of the index to build")
    p.add_argument("--pbf", action="append", type=Path, default=[],
                   help="local .osm.pbf file(s) to use instead of downloading; repeatable")
    p.add_argument("--cache", type=Path,
                   help="directory to keep downloaded extracts (default: temp, deleted)")
    p.add_argument("--out", type=Path,
                   help="output .npz path (default: src/data/pois_<cc>.npz)")
    args = p.parse_args()

    cc = args.country.upper()
    out = args.out or (
        Path(__file__).resolve().parents[1] / "src" / "data" / f"pois_{cc.lower()}.npz"
    )

    if args.pbf:
        missing = [str(f) for f in args.pbf if not f.exists()]
        if missing:
            sys.exit(f"missing .pbf file(s): {missing}")
        build(cc, args.pbf, out)
        return

    if args.cache:
        args.cache.mkdir(parents=True, exist_ok=True)
        cache_dir, cleanup = args.cache, None
    else:
        cleanup = tempfile.TemporaryDirectory(prefix="geofabrik-")
        cache_dir = Path(cleanup.name)
    try:
        paths = EXTRACTS[cc]
        print(f"=== {cc}: {len(paths)} Geofabrik extract(s) ===")
        pbfs = [_download(path, cache_dir) for path in paths]
        build(cc, pbfs, out)
    finally:
        if cleanup:
            cleanup.cleanup()


if __name__ == "__main__":
    # Allow `python tools/build_poi_index.py` from anywhere; paths are absolute.
    os.chdir(Path(__file__).resolve().parents[1])
    main()
