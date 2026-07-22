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
| Gumtree AU | AU | 403 on first impersonated request | Ruled out (needs headless) |
| Flatmates.com.au | AU | 429 on first request (REA-owned) | Ruled out (needs headless) |
| rent.com.au | AU | 403 (challenge page, ~150 KB shell) | Ruled out (needs headless) |
| Zumper | US | 200; `window.__PRELOADED_STATE__` embeds structured listables (address, city, state, lat/lng, amenity tags, urls); paginate with `?page=N` | **In use** (src/sources/zumper.py) — see 2026-07-21 note |
| HotPads | US | 200; `__PRELOADED_STATE__` present (Zillow-owned — higher ToS/likely-hardening risk) | Viable-looking, second choice |
| Craigslist | US | 200 but static rows carry only title/price/city — no coords/beds; bans hard; saturated on Apify | Weak candidate |

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

### 2026-07-21 — US launch: Zumper implementation notes
- Pagination is just `?page=N` on the search URL (each page server-renders a
  fresh `__PRELOADED_STATE__`, ~25-32 records); no XHR API needed. Stop when a
  page adds nothing new — featured records repeat across pages, so dedupe by
  `listing_id` per city.
- `currentSearch.listables` values are HETEROGENEOUS (lists of records mixed
  with ints/bools/None) — type-check while flattening.
- Records are per-FLOORPLAN within a building feed: `title` is a floorplan
  label ("A1"), `building_name` is the human name; compose "Building — Plan".
  `min_price` is the advertised "from" price; `min_bedrooms` 0 = studio.
- `property_type` is a numeric enum, only partially mapped (1/4 apartment,
  2 house, 16 room — sampled empirically); unknown values → None, don't guess.
- Plain httpx gets a ~3 KB JS shell (200, not 403!) — a "success" that isn't;
  check content, not just status. Chrome TLS impersonation returns the real
  page from a home IP. Apify datacenter behavior untested.
- `pets: []` means unknown, not "no pets" — only non-empty lists assert.

### 2026-07-21 — US/AU probe round: US open (Zumper), Australia blocked wall-to-wall
- One impersonated (curl_cffi Chrome) request per site, home IP:
  - **AU: every major portal blocked the FIRST request** — Domain 403 (Kasada),
    Gumtree AU 403, Flatmates 429, rent.com.au 403. AU is a different
    architecture class (headless browser + AU residential proxies); defer until
    there's demand, don't burn time re-probing with header tricks.
  - **US: Zumper 200** with `window.__PRELOADED_STATE__` →
    `currentSearch.listables` = dict of building-id → list of structured
    records (address, city, state, lat/lng, amenity_tags, pets, phone, url;
    prices/beds nest deeper — untangle when building the source). Parse with
    `json.JSONDecoder().raw_decode` from the first `{` (trailing JS follows the
    blob). Pagination hooks visible (`hasMoreListables`, `paginatedIds`) —
    likely an XHR API behind it; dig when implementing.
  - **HotPads 200** with `__PRELOADED_STATE__` (Zillow-owned; expect harder
    ToS posture / future hardening). **Craigslist 200** but the static page
    only carries title/price/city per row — no coords/beds — and CL is
    ban-happy + saturated on Apify.
- Phase-3 recommendation: US via Zumper first. US POI extract set is ~12 GB of
  Geofabrik downloads (51 files) — the parse is fast now, the download is the
  long pole; cache it.

### 2026-07-22 — POI names bundled for identity categories
- The offline indexes now carry each POI's OSM name (`<cat>__names` parallel
  arrays, UTF-8 truncated to 48 bytes) for subway/train/grocery/cafe/pharmacy/
  school/university/library/gym/hospital. bus_stop + park stay nameless on
  purpose — they're ~1M of 1.9M rows and identity rarely matters there.
- Cost: +4.5 MB across the three indexes (11.8 → 16.3 MB total; names compress
  well). Names flow index → `nearest_batch` → `nearby_amenities[{t,lat,lng,m,
  name?}]` → dataset + both map tooltips ("Caffè Nero — cafe · 82m"). The
  Overpass fallback emits names too, for parity.
- Verified: Trafalgar Sq → Caffè Nero/Boots/Charing Cross; Midtown → Breads
  Bakery/Whole Foods/Equinox; Toronto Union → Isabella's Donuts/Union Station.
- Subway ENTRANCE nodes often carry no OSM name (the station node does) — so
  `subway` hits may be nameless where `train` has the named station. A
  fall-back-to-parent-station lookup is a possible future builder tweak.
- CA index rebuilt from Geofabrik for the first time (was the June Postgres
  export): 225,157 → 259,886 POIs (~13 months of OSM growth).

### 2026-07-22 — Renamed to `rental-listings-scraper` (store positioning)
- Store survey: every competitor is single-site (`zumper-rental-scraper`,
  `openrent-property-scraper`, `zillow-rentals-scraper`…) and NOBODY owns the
  generic head terms — "rental listings" / "apartment scraper" return only
  site-specific actors. Multi-site/multi-country is our differentiator, so the
  generic slug is the play, esp. for MCP-driven discovery where Claude
  searches with the user's plain words ("find apartments for rent in …").
- Platform limits (learned the 400 way): actor title ≤ 63 chars, description
  ≤ 300 chars. Renaming safely = PUT the platform rename FIRST, then update
  `.actor/actor.json` — pushing a changed name creates a NEW actor instead.
- Old URL apify.com/soroush98/kijiji-canada-rentals-scraper is dead after the
  rename (actor ID Yy3if5Rg6khgygVAa is unchanged; by-ID references and run
  history survive).

### 2026-07-21 — SDK upgraded to apify 4.0.0: pins dropped, _compat.py deleted
- `apify==4.0.0` + `crawlee==1.8.3` (pinned as the verified pair) replace the
  2.7.3-era set; the `pydantic<2.12` and `browserforge==1.2.3` pins are gone
  (browserforge is no longer even a transitive dep).
- **`src/_compat.py` (MCP-origin enum shim) deleted**: apify 4.0 no longer
  validates `meta.origin` against a strict enum anywhere (no `MetaOrigin` /
  `ActorRun` TypeAdapter in the package), so unknown origins can't crash
  `Actor.init()`. Note `MetaOrigin` itself STILL lacks 'MCP' — irrelevant now,
  but don't reintroduce strict validation against it.
- API surface we use survived 2.7.3 → 4.0.0 unchanged (all still async;
  `Actor.charge`'s `count` became keyword-only — we already pass it by
  keyword). Verified live: openrent + rentfaster still clear Cloudflare with
  curl_cffi 0.15; full smoke suite green; cloud run on the platform.
- Local `python -m src` now fails at `create_proxy_configuration` with
  "Proxy external access isn't enabled" — an ACCOUNT limitation of running
  Apify Proxy from outside the platform, not an SDK bug. Local e2e = the
  harnesses (SDK stubbed); platform e2e = a cloud run.

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
  bakes the value set at build time). **Resolved 2026-07-21: upgraded to apify 4.0.0**,
  which drops the strict validation and the pydantic/browserforge pins — see the
  2026-07-21 SDK-upgrade entry.
