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
  `tools/build_poi_index.py::CATEGORIES` (and the bundled `src/data/pois_ca.npz`
  built from it).

---

## Data sourcing — what's viable per site

| Site | Access | Status |
|---|---|---|
| Kijiji | Parse search-page `__NEXT_DATA__` Apollo cache (~40/req, no detail fetches) | **In use** (src/sources/kijiji.py) |
| RentFaster.ca | **Public JSON API** `GET /api/search.json?proximity_type=location-city&novacancy=0&cur_page=N`; scope via `lastcity=<prov>/<city>` cookie. Returns `{listings, query, total, total2}` | **In use** (src/sources/rentfaster.py) — see Cloudflare note below |
| rentals.ca | Cloudflare Turnstile → 403 | Ruled out (needs headless/paid proxy) |
| Realtor.ca / CREA DDF | Cloudflare + ToS (licensed brokerage only) | Ruled out |
| Facebook Marketplace | Auth wall + heavy anti-bot; mostly dupes Kijiji | Ruled out |

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
