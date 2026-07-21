"""End-to-end acceptance test: produce a validated GOLDEN SET of 10 listings.

Runs the real main.py pipeline against the live sites (both sources), exercising
filters -> dedup -> amenity enrichment -> cap, then validates every result and
prints the set. Uses the fake-Actor stub (no Apify SDK needed).

    .venv/bin/python golden_set.py
"""

import asyncio
import json
import sys
import types

# --- fake `apify` module so src.main imports without the SDK -----------------
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")


class _FakeActor:
    log = logging.getLogger("actor")

    def __init__(self):
        self.input, self.pushed, self.kv = {}, [], {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_input(self):
        return self.input

    async def create_proxy_configuration(self, actor_proxy_input=None):
        return None

    async def push_data(self, items):
        self.pushed.extend(items if isinstance(items, list) else [items])

    async def set_value(self, k, v):
        self.kv[k] = v


Actor = _FakeActor()
_m = types.ModuleType("apify")
_m.Actor = Actor
sys.modules["apify"] = _m

from src.main import main  # noqa: E402

# --- the golden-set query ----------------------------------------------------
Actor.input = {
    "sources": ["kijiji", "rentfaster"],
    "cities": ["Toronto", "Calgary"],
    "minBedrooms": 1,
    "maxBedrooms": 2,
    "maxRent": 2800,
    "maxResults": 10,
    "enrichAmenities": True,      # decorate the 10 with nearest-amenity distances
    "dedupe": True,
}

print("\nGOLDEN-SET QUERY:", json.dumps(Actor.input))
print("=" * 78)
asyncio.run(main())
print("=" * 78)

gold = Actor.pushed

# --- print the set -----------------------------------------------------------
print(f"\nGOLDEN SET — {len(gold)} listings\n")
for i, m in enumerate(gold, 1):
    am = m.get("amenity_distances_m") or {}
    am_str = ", ".join(f"{k}:{v}m" for k, v in sorted(am.items())[:4]) or "—"
    print(
        f"{i:2}. [{m['source']:10}] {m.get('city','?'):8} "
        f"${m.get('monthly_rent','?'):>5}/mo  {m.get('bedrooms','?')}bd "
        f"{m.get('bathrooms','?')}ba  {(m.get('property_type') or '?'):9} "
        f"| {(m.get('address') or m.get('title') or '')[:42]}"
    )
    print(f"     amenities: {am_str}")
    print(f"     {m['url']}")

# --- validate ----------------------------------------------------------------
print("\n" + "=" * 78)
print("VALIDATION")
checks = []


def chk(name, cond):
    checks.append((name, cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")


chk("returned <= maxResults (10)", len(gold) <= 10)
chk("returned a full set (>=8)", len(gold) >= 8)
chk("every listing has a source", all(m.get("source") for m in gold))
chk("every listing has a usable URL", all(str(m.get("url", "")).startswith("http") for m in gold))
chk("every listing has rent", all(isinstance(m.get("monthly_rent"), int) for m in gold))
chk("maxRent<=2800 respected", all(m.get("monthly_rent", 0) <= 2800 for m in gold))
chk("bedrooms in [1,2]", all(1 <= (m.get("bedrooms") or 0) <= 2 for m in gold))
chk("every listing has coordinates", all(m.get("lat") and m.get("lng") for m in gold))
srcs = sorted({m["source"] for m in gold})
chk(f"both sources represented {srcs}", len(srcs) == 2)
enriched = [m for m in gold if m.get("amenity_distances_m")]
chk(f"amenity-enriched ({len(enriched)}/{len(gold)})", len(enriched) >= max(1, len(gold) // 2))
chk("no duplicate URLs", len({m["url"] for m in gold}) == len(gold))

stats = Actor.kv.get("RUN_STATS", {})
print(f"\nRUN_STATS: {json.dumps(stats)}")
passed = sum(1 for _, c in checks if c)
print(f"\n{'✅ GOLDEN SET PASSED' if passed == len(checks) else '⚠️  SOME CHECKS FAILED'} "
      f"({passed}/{len(checks)})")
