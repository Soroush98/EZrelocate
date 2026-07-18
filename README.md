# EZrelocate

Canada-wide rental search. You describe what you're looking for in plain English;
the system returns real rental listings on a map, with reasoning that cites each
pick by id.

The interesting part isn't the chat — it's the hybrid retrieval pipeline running
over a continuously-refreshed national rental index.

![EZrelocate architecture](architecture.png)

## Stack

| Layer | Choice |
|---|---|
| DB | Postgres 17 + PostGIS 3 + pgvector |
| Backend | FastAPI 0.136 (Python 3.12), asyncpg |
| LLM | Anthropic SDK 0.102 — Claude for query parsing + recommendation generation |
| Embeddings | Voyage AI `voyage-3-large` (1024-dim) |
| Frontend | Next.js 16.2 + React 19 + Tailwind v4 + MapLibre GL 5 (Carto Voyager basemap) |
| Scraper | httpx + selectolax, polite rate-limited, per-city round-robin |

## Data sources

| Source | Coverage | Why |
|---|---|---|
| **Kijiji** | National apartments + houses for rent (~54k inventory) | High volume, plain-HTTP scrapable, structured data in Apollo cache (`__NEXT_DATA__` → `__APOLLO_STATE__.RealEstateListing:<id>`) |

**Tried and rejected:**
- **rentals.ca** — Cloudflare bot challenge (HTTP 403 + JS-required Turnstile).
  Needs Playwright or a paid proxy.
- **Realtor.ca / CREA DDF** — Cloudflare-blocked, ToS-prohibited, requires licensed
  brokerage sponsorship.
- **Facebook Marketplace** — auth wall + heavy anti-bot + ToS. Most FB rentals are
  duplicated to Kijiji anyway.
- **RentFaster.ca** — works from residential IPs but Cloudflare's JS challenge
  blocks datacenter/cloud IPs, so it's not in the nightly pipeline. The
  standalone [apify-actor/](apify-actor/) supports it best-effort alongside
  Kijiji in a single normalized feed.

Scraping is **batch, not per-query.** A scheduled job re-scrapes Kijiji every
night and writes the results to Postgres. User queries hit the warm
pgvector / PostGIS index, so the chat side never waits on the network. Listings
not re-seen in 72 hours auto-flip to `status='stale'` and stop appearing in
results. See [Nightly refresh](#nightly-refresh) below.

## Repository layout

```
.
├── .github/workflows/
│   ├── ci.yml                           # GitHub Actions — lint + tests + typecheck
│   ├── refresh.yml                      # GitHub Actions — nightly Kijiji refresh
│   └── osm-pois.yml                     # GitHub Actions — weekly OSM POI load
├── infra/
│   ├── Dockerfile                       # postgres:17-bookworm + postgis + pgvector
│   ├── docker-compose.yml
│   └── init/01-extensions.sql           # CREATE EXTENSION postgis, vector, pg_trgm
├── db/
│   └── schema.sql                       # single source of truth: listings, pois,
│                                        #   auth/billing/usage (+ HNSW & GIST indexes)
├── backend/
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py                      # FastAPI app + lifespan
│   │   ├── config.py                    # pydantic-settings
│   │   ├── db.py                        # asyncpg pool
│   │   ├── models.py                    # Pydantic request/response types
│   │   ├── routes/                      # query, billing, nearby, stats
│   │   └── services/
│   │       ├── auth.py                  # Supabase JWT verify + client IP
│   │       ├── quota.py                 # per-tier query quota gates
│   │       ├── query_log.py             # full-text query logging
│   │       ├── embeddings.py            # Voyage AI wrapper
│   │       ├── llm.py                   # Claude parse + generate
│   │       └── retrieval.py             # Hybrid SQL + pgvector + PostGIS
│   ├── etl/
│   │   ├── _common.py                   # Shared asyncpg connection helper
│   │   ├── _scrape.py                   # PoliteClient, ScrapedListing, upsert, mark_stale
│   │   ├── scrape_kijiji.py             # National per-city round-robin scraper
│   │   ├── load_osm_pois_geofabrik.py   # Offline OSM POI loader (Geofabrik .pbf)
│   │   ├── compute_amenity_distances.py # Per-listing nearest-amenity distances
│   │   └── embed_all.py                 # Voyage backfill for active listings
│   ├── tests/                           # Hermetic unit tests (no DB / network)
│   └── scripts/
│       ├── init_db.sh                   # Apply db/schema.sql
│       └── refresh.sh                   # cron/launchd entrypoint
├── frontend/                            # Next.js 16 · Tailwind v4 · MapLibre · Geist
│   └── src/
│       ├── app/{layout,page}.tsx
│       ├── components/{Map,QueryPanel,ListingCard,AccountBar,AuthModal,...}.tsx
│       └── lib/{types,auth,supabase,amenityIcons}.ts
├── apify-actor/                         # Standalone Apify actor: Kijiji + RentFaster
│                                        #   in one normalized, deduplicated feed
│                                        #   (own README; not part of the nightly job)
└── .env.example
```

## Run it locally

### 0. Prereqs

- Docker
- Python 3.12 + [uv](https://docs.astral.sh/uv/) (or any pip/venv tool)
- Node 20+ (npm or pnpm)
- Anthropic API key, Voyage AI API key (Voyage requires payment-method-on-file
  for usable rate limits; you still get 200M free tokens)

### 1. Bring up the database

```bash
cp .env.example .env       # fill in keys
cd infra && docker compose up -d --build
cd ..
./backend/scripts/init_db.sh   # applies db/schema.sql
```

### 2. Install the backend

```bash
cd backend
uv venv && source .venv/bin/activate
uv pip install -e .
```

### 3. First crawl (~30–60 min for ~4,400 balanced listings)

```bash
# Dry-run first — confirms the parser still matches Kijiji's __NEXT_DATA__ shape.
python -m etl.scrape_kijiji --per-city 2 --dry-run

# Real crawl — round-robin across 22 cities, ~250 each.
python -m etl.scrape_kijiji --per-city 250

# Embed everything (needs VOYAGE_API_KEY in .env).
python -m etl.embed_all
```

### 4. Start the API

```bash
uvicorn app.main:app --reload
# http://localhost:8000/health
# http://localhost:8000/docs
```

### 5. Start the frontend

```bash
cd ../frontend
npm install
npm run dev
# http://localhost:3000
```

### 6. Schedule the nightly refresh

See the [Nightly refresh](#nightly-refresh) section below for the full
explanation. In short: GitHub Actions runs the pipeline every night for free;
you only need to add a few secrets to the repo.

## API

`POST /api/query`

```json
{ "query": "Toronto, $2500/mo, 1 bedroom, pet-friendly, near a subway station" }
```

Response:

```json
{
  "query": "...",
  "parsed": {
    "city": "Toronto",
    "province": "ON",
    "max_rent": 2500,
    "min_bedrooms": 1,
    "pet_friendly": true,
    "lifestyle_query": "near a subway station"
  },
  "listings": [
    {
      "id": 1042,
      "source": "kijiji",
      "url": "https://www.kijiji.ca/v-apartments-condos/.../12345",
      "monthly_rent": 2350,
      "bedrooms": 1, "bathrooms": 1, "sqft": 580,
      "pet_friendly": true,
      "utilities_included": ["heat", "water"],
      "city": "City of Toronto", "province": "ON",
      "lat": 43.66, "lng": -79.33,
      "score": 0.78
    }
  ],
  "reasoning": "Listing 1042 in Leslieville fits because..."
}
```

## Nightly refresh

This is the part that keeps the listings fresh. It runs once a night, on its
own, and you do not need to touch it.

### What it does, in plain English

Every night the system wakes up and does three things in order:

1. **Scrape Kijiji.** Walk through 22 Canadian cities and grab up to 500
   apartment / house listings from each (round-robin, so coverage stays
   balanced across the country). New listings are added; listings we have seen
   before just have their "last seen" timestamp bumped. Small cities exhaust
   well before 500 and stop early, so the realistic nightly haul is around
   10,000 listings.
2. **Recompute walking distances.** For every listing, work out how far it is
   to the nearest subway, park, school, and so on. These numbers are stored
   right on the listing row, so the search side never has to compute them
   live.
3. **Embed only the new listings.** Send the brand-new listings to Voyage AI to
   turn their text into vectors. Old listings keep the vectors they already
   have, so this step is fast and cheap.

The map data itself — OpenStreetMap points of interest (subway stations, parks,
grocery stores, schools, etc.) — is static infrastructure, so it loads on a
**separate weekly schedule** ([.github/workflows/osm-pois.yml](.github/workflows/osm-pois.yml))
rather than every night. It reads offline Geofabrik province extracts instead of
the rate-limited public Overpass API, so it never gets throttled.

Anything that has not been seen on Kijiji for 72 hours is marked `stale` and
silently drops out of search results.

**How long does it take?** Roughly 2–2.5 hours end-to-end. The scraper is
intentionally polite (2 concurrent requests, 0.4–1s jitter between them) so
Kijiji does not rate-limit us. The workflow has a 5-hour safety timeout.

**What if I want to crawl everything?** Kijiji has 50k+ active listings
nationally. At polite rates that would take 10–18 hours and risk getting the
runner's IP blocked, so we crawl a balanced ~10k slice each night instead.
Listings turn over slowly, so within a few nights you have effectively
nationwide coverage of anything that has been on the market recently.

### Why the embeddings refresh is easy

Embeddings are the part people usually worry about, because re-embedding a
whole database is slow and costs money. The trick here is simple:

- Each listing row has a vector column that starts out `NULL`.
- The embed step (`backend/etl/embed_all.py`) only looks at rows where the
  vector is `NULL` and the listing is still active.
- A listing is embedded exactly once and then never again — even if its text
  is later edited on Kijiji. That's a deliberate trade-off: descriptions
  rarely change materially after posting, and skipping re-embeds keeps the
  step cheap.

So a typical night embeds the slice of listings that are genuinely new —
usually one to two thousand rows out of an active inventory of tens of
thousands. That costs a handful of cents on Voyage. The first crawl is the
only one that ever pays the full embedding bill.

### Where the schedule lives

The schedule lives in [.github/workflows/refresh.yml](.github/workflows/refresh.yml).
It runs on GitHub's free Actions runners every night at 09:00 UTC (around 4
or 5 AM Eastern). You can also start a run by hand from the repo's **Actions**
tab → **Nightly refresh** → **Run workflow**.

To make it work after forking the repo, add these three secrets at
**Settings → Secrets and variables → Actions**:

| Secret | Where to get it |
|---|---|
| `DATABASE_URL` | Your Postgres connection string (Supabase, Neon, RDS, etc.) |
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `VOYAGE_API_KEY` | voyageai.com |

That's it. GitHub will run the job on schedule and you can watch the logs
right in the Actions tab.

### Running the refresh locally (optional)

If you would rather run it on your own machine, the same pipeline lives in
[backend/scripts/refresh.sh](backend/scripts/refresh.sh). Trigger it from cron,
launchd, or by hand:

```bash
./backend/scripts/refresh.sh
```

## A note on scraping

Kijiji's ToS restricts scraping. The crawler here is intentionally polite —
2 concurrent requests, 0.4–1 second of jitter between them, capped at a few
hundred listings per city per refresh. Please don't redistribute the data and
don't run this at commercial scale without negotiated access. If you ever
deploy a busy public instance, expect to need a residential-proxy provider
(BrightData, Apify) to avoid IP-level rate limits.
