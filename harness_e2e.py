"""End-to-end harness: runs the REAL main.py orchestration against the LIVE sites,
stubbing only the thin Apify-platform calls (get_input / push_data / proxy / log).

This exercises everything the actor actually does — input parsing, scraping both
sources, basic filters, cross-source dedup, (optional) geocode + enrichment, and
the push payload — without needing the Apify SDK installed. The real platform
plumbing is what `apify push` + a cloud run validates.

    .venv/bin/python harness_e2e.py
"""

import asyncio
import json
import logging
import sys
import types

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

# --- inject a fake `apify` module BEFORE importing src.main -------------------
class _FakeActor:
    log = logging.getLogger("actor")

    def __init__(self):
        self.input: dict = {}
        self.pushed: list = []
        self.kv: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_input(self):
        return self.input

    async def create_proxy_configuration(self, actor_proxy_input=None):
        return None  # no proxy locally

    async def push_data(self, items):
        self.pushed.extend(items if isinstance(items, list) else [items])

    async def set_value(self, key, value):
        self.kv[key] = value


Actor = _FakeActor()
_mod = types.ModuleType("apify")
_mod.Actor = Actor
sys.modules["apify"] = _mod

from src.main import main  # noqa: E402  (must follow the stub injection)

# --- the test run -------------------------------------------------------------
Actor.input = {
    "sources": ["kijiji", "rentfaster"],
    "cities": ["Calgary"],          # both sources cover Calgary well -> tests dedup
    "maxPerCity": 12,
    "maxRent": 2500,
    "minBedrooms": 1,
    "maxResults": 8,
    "dedupe": True,
}

print(f"\nINPUT: {json.dumps(Actor.input)}\n" + "=" * 70)
asyncio.run(main())
print("=" * 70)
print(f"RUN_STATS: {json.dumps(Actor.kv.get('RUN_STATS'), indent=2)}")
print(f"\nPUSHED {len(Actor.pushed)} listings. First 2:\n")
for item in Actor.pushed[:2]:
    print(json.dumps(item, indent=2, ensure_ascii=False))

# sanity assertions on the output
bad_rent = [m for m in Actor.pushed if m.get("monthly_rent", 0) > 2500]
bad_beds = [m for m in Actor.pushed if m.get("bedrooms", 9) < 1]
print(f"\nCHECK maxRent<=2500 respected: {'OK' if not bad_rent else f'FAIL ({len(bad_rent)})'}")
print(f"CHECK minBedrooms>=1 respected: {'OK' if not bad_beds else f'FAIL ({len(bad_beds)})'}")
print(f"CHECK has both sources: {sorted({m['source'] for m in Actor.pushed})}")
print(f"CHECK cross-source dups found: {sum(1 for m in Actor.pushed if m.get('also_on'))}")
