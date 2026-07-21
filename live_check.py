"""Live end-to-end check WITHOUT the Apify SDK — hits the real sites, runs the
real parsers, prints a few normalized listings. This is the fastest way to tell
whether the scrapers still work against the current site layout.

Run from the repo root:
    .venv/bin/python live_check.py
    .venv/bin/python live_check.py --source rentfaster --city Calgary --n 3

NOTE: from a home/datacenter IP, Kijiji may block (403) — that means "use a
residential proxy", not "the code is broken"; the real Kijiji test is
`apify run`/`apify push` with Apify Proxy. RentFaster, by contrast, clears its
Cloudflare challenge via Chrome TLS impersonation (curl_cffi) and works locally
with no proxy, so `--source rentfaster` is a real end-to-end check here.
"""

import argparse
import asyncio
import json
import logging

from src.polite_client import PoliteClient
from src.sources import kijiji, rentfaster

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("live_check")

SOURCES = {"kijiji": kijiji.scrape, "rentfaster": rentfaster.scrape}


async def run(source: str, city: str, n: int) -> None:
    got = []
    client_kwargs = {"max_concurrency": 2, "min_delay_ms": 400, "max_delay_ms": 1000}
    if source == "rentfaster":
        # Chrome TLS impersonation clears RentFaster's Cloudflare challenge even from
        # a plain home IP — so this path is testable locally without a proxy.
        client_kwargs["impersonate"] = "chrome"
    async with PoliteClient(**client_kwargs) as client:
        async for listing in SOURCES[source](client, cities=[city], max_per_city=n, log=log):
            got.append(listing)
            if len(got) >= n:
                break
    print(f"\n=== {source}: parsed {len(got)} listings from {city} ===")
    for m in got:
        print(json.dumps(m.to_item(), indent=2, ensure_ascii=False))
    if not got:
        print("No listings — likely an IP block (403/429) or a layout change. "
              "Re-test with a residential proxy via `apify run`.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", choices=list(SOURCES), default="kijiji")
    p.add_argument("--city", default="Toronto")
    p.add_argument("--n", type=int, default=3)
    args = p.parse_args()
    asyncio.run(run(args.source, args.city, args.n))
