# EZrelocate actor — Project Notes & Learnings

A running log of **empirical, non-obvious** things we've learned building this
actor — what sites are scrapable and how, what broke and why, decisions and
their rationale. The goal is to **not relearn the same lesson twice**.

Keep entries dated and concrete. This is for things you can't recover by reading the
code or git history — write down the *why* and the *what we ruled out*, not the *what*.

> The FastAPI backend / Next.js frontend this actor grew out of were removed from
> the repo in July 2026. Notes that only concerned them were dropped with it —
> see git history before 2026-07-21 for those.

---

## Cross-cutting seams — change these together

One concept, edited in more than one place. If you touch one, touch the others:

- **Amenity categories:** `src/enrich.py::AMENITY_FILTERS` ↔
  `.actor/input_schema.json` (`nearAmenities` enum) ↔
  `tools/build_poi_index.py::CATEGORIES` (and the bundled `src/data/pois_<cc>.npz`
  built from it).
- **Countries:** `src/sources/__init__.py` (`REGISTRY`/`COUNTRIES`/`CURRENCIES`)
  ↔ `.actor/input_schema.json` (`country` enum — only expose a country once it
  has a source) ↔ `src/normalize.py::REGIONS` + postal patterns ↔
  `src/data/pois_<cc>.npz` (build via `tools/build_poi_index.py --country <cc>`;
  a missing index silently degrades enrichment to slow Overpass calls).

---

## Data sourcing — what's viable per site

| Site | Country | Access | Status |
|---|---|---|---|
| Kijiji | CA | Parse search-page `__NEXT_DATA__` Apollo cache (~40/req, no detail fetches) | **In use** (src/sources/kijiji.py) |
| RentFaster.ca | CA | **Public JSON API** `GET /api/search.json?proximity_type=location-city&novacancy=0&cur_page=N`; scope via `lastcity=<prov>/<city>` cookie. Returns `{listings, query, total, total2}` | **In use** (src/sources/rentfaster.py) — see Cloudflare note below |
| OpenRent | GB | Search page embeds ALL result ids+coords as JS arrays; details via JSON `GET /search/propertiesbyid?ids=…` | **In use** (src/sources/openrent.py) — see 2026-07-21 note |
| rentals.ca | CA | Cloudflare Turnstile → 403 | Ruled out (needs headless/paid proxy) |
| Realtor.ca / CREA DDF | CA | Cloudflare + ToS (licensed brokerage only) | Ruled out |
| Facebook Marketplace | CA/US | Auth wall + heavy anti-bot; mostly dupes Kijiji | Ruled out |
| Domain.com.au | AU | 403 even with Chrome TLS impersonation (Kasada) | Ruled out (needs headless) |
| realestate.com.au | AU | Kasada (industry-known hard block) | Not attempted |

### 2026-06-27 — RentFaster API is behind a Cloudflare managed challenge
- Context: evaluated RentFaster as a second source (it has a clean JSON API, unlike
  rentals.ca which we'd already ruled out for Cloudflare Turnstile).
- Result: plain requests to `/api/search.json` now **403** (Cloudflare managed
  challenge, since ~2026-04). Fix confirmed in the wild: send browser-like headers —
  **`Referer: https://www.rentfaster.ca/`, `Origin: https://www.rentfaster.ca`,
  `X-Requested-With: XMLHttpRequest`** → HTTP 200. A residential proxy is the durable
  mitigation; header spoofing alone is brittle.
- Decision: RentFaster is the lowest-friction *second* source (structured JSON, no HTML
  parsing). Field names: `id, link, price, type, bedrooms, den, baths, sq_feet,
  latitude, longitude, address, city, availability, utilities_included, intro`.
  `id` repeats across a building's unit types — disambiguate with the link's trailing
  `_<n>` suffix.
- Why record: saves re-discovering the Cloudflare 403 and the exact unblock headers.

### 2026-07-21 — pyosmium trap: SimpleHandler callbacks fire per NODE, not per POI
- The first GB index build ran 2h40m without finishing: `SimpleHandler.node()`
  is invoked from C++ into Python for EVERY node in the file — the UK extract
  has ~350M, nearly all untagged way-geometry — so the build was ~99% Python
  call overhead. Canada never exposed this only because it ships as 13 smaller
  province files.
- Fix: `osmium.FileProcessor(...).with_locations(...).with_filter(
  osmium.filter.EmptyTagFilter())` (pyosmium ≥ 4) — untagged entities are
  dropped in C++ before crossing into Python, while the location cache still
  indexes every node so way centroids resolve. PEI verification: byte-identical
  npz, ~5x faster even on that small file; country-scale files go from hours to
  minutes. Any future pyosmium work should start from FileProcessor + filters,
  never a bare SimpleHandler.

### 2026-07-21 — UK launch: OpenRent viable (and pleasant), Domain.com.au is not
- **OpenRent two-stage JSON pipeline** (no HTML card parsing needed):
  1. `GET /properties-to-rent/{city-slug}` embeds the ENTIRE result set as
     parallel JS arrays — `PROPERTYIDS` + `PROPERTYLISTLATITUDES`/`LONGITUDES`
     (London ≈ 6.1k ids on one page). One request per city buys the full
     id→coordinate map; the 20 rendered cards are irrelevant.
  2. `GET /search/propertiesbyid?ids=…` (repeatable param, batches of 20)
     returns clean JSON: `title, rentPerMonth, rentPerWeek, details
     (["2 Bed","1 Bath","Furnished"]), letAgreed, isMultiRoom,
     maxRoomRentPerMonth`, description snippet. Param name is `ids`
     (`propertyIds` returns `[]`, silently).
- Cloudflare fronts it: Chrome TLS impersonation (curl_cffi) + sticky IP works
  from a home IP with no proxy — same recipe as RentFaster. Datacenter behavior
  on Apify untested; assume residential GB proxy for production.
- Gotchas baked into the source: skip `letAgreed`; HMO listings carry rent in
  `maxRoomRentPerMonth` (`rentPerMonth` is 0); `rentPerMonth` is site-computed
  so no pw→pcm conversion; titles end in the OUTWARD postcode only ("WC2N");
  `description` is a ~130-char snippet (full text costs a page fetch per
  listing — not worth it); studios may lack a bed entry in `details` (fall back
  to "studio" in the title); short URL `openrent.co.uk/{id}` 301s to canonical.
- `openrent.co.uk/{city}` slug = lowercase-hyphenated name; verified london +
  manchester live (3 + 2 listings parsed end-to-end via live_check).
- **Domain.com.au: 403 on the first request** even with curl_cffi Chrome
  impersonation (Kasada, not Cloudflare — impersonation doesn't help). AU needs
  a headless browser or a different portal; deferred.

### 2026-07-21 — Internationalization scaffolding (CA/US/GB/AU), no new sources yet
- The pipeline is now country-aware end-to-end (see the country seam above);
  adding a market = one scraper module + a registry entry + schema enum + POI
  index. Non-obvious choices, so they don't get relitigated:
  - **Weekly rents:** UK/AU listings quote per week; `parse_monthly_rent`
    converts ×52/12 (letting-industry convention, not ×4). A monthly marker
    wins when a listing quotes both ("£1,950 pcm (£450 pw)").
  - **US/AU region codes match UPPERCASE-only** in addresses — IN, OR, ME, WA…
    are ordinary lowercase words. CA stays case-insensitive (legacy behavior).
  - **US ZIP = last match** in the address (5-digit house numbers exist);
    **AU postcode anchors to end-of-string** (4 digits collide with years).
  - **GB `province` = nation codes** (ENG/SCT/WLS/NIR) — the UK has no state
    layer; postcodes carry the real geo signal there.
  - `tools/build_poi_index.py` now reads Geofabrik .pbf directly (pyosmium,
    dev-only dep) — the old backend Postgres path is gone. Verified against the
    PEI extract. US extract set is ~12 GB of downloads; use `--cache`.

### 2026-06-27 — Apify packaging decisions
- The actor repackages EZrelocate's scrapers as a *unified, geo-enriched* Canadian
  rentals dataset. Clean-room port (no DB / no Claude / Voyage) — pushes flat JSON
  to an Apify dataset.
- Scope decision: ship **Kijiji + RentFaster only**. Standalone Kijiji and FB
  Marketplace scrapers are already saturated on Apify Store (10+ each); the unique,
  defensible angles are (a) one normalized schema across sources, (b) cross-source
  dedup, (c) amenity-distance enrichment — none of which existing actors offer. FB
  Marketplace + rentals.ca deferred (saturated/blocked, and we'd already ruled both
  out before).
- **Apify actor dep pins are load-bearing.** `apify==2.7.3` pulls `crawlee==0.6.12`,
  which crashes at *runtime* (build succeeds, container dies on import) unless you
  pin `pydantic>=2.10,<2.12` (else "cannot specify both default and default_factory")
  and `browserforge==1.2.3` (1.2.4 renamed `download.DATA_FILES`). Verified set is in
  `requirements.txt`. The Apify *build* won't catch this — only a cloud *run* does.
- **Kijiji 403s Apify datacenter IPs (incl. default Apify Proxy).** First cloud run:
  rentfaster returned data, Kijiji got HTTP 403. Kijiji needs a RESIDENTIAL proxy
  group on Apify; rentfaster works on datacenter. The actor handles the block
  gracefully (logs, 0 listings, exit 0) rather than crashing. **Update:** rentfaster
  is flaky from ALL Apify IPs (Cloudflare *JS* managed challenge on `/api`, not a
  cookie problem — even the homepage 403s), so it's best-effort on Apify, reliable
  from a home IP. The real fix would be a headless browser; not worth it vs. Kijiji.
- **MCP-triggered runs crashed the actor on startup (apify 2.7.3 too old).** When the
  actor was run via Apify's MCP server, `meta.origin='MCP'` — a value the pinned SDK's
  `MetaOrigin` enum doesn't know — made the charging manager's pydantic validation
  blow up in `Actor.init()`, BEFORE any of our code. CLI/API/WEB origins worked, so it
  only bit the MCP path (the one we built toward). Band-aid: `src/_compat.py` injects
  the `MCP` member into the enum *before* `import apify` (the run_validator TypeAdapter
  bakes the value set at build time). **Durable fix: upgrade to apify 3.x** — which
  also drops the `pydantic<2.12` / `browserforge==1.2.3` pins. Two SDK-pin bites now;
  upgrading is overdue.
