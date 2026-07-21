# Kijiji & RentFaster Scraper — Canada Rental Listings + Geo Data 🇨🇦🏠

One **normalized, deduplicated** feed of Canadian rental listings from **Kijiji**
and **RentFaster.ca**, with optional **nearest-amenity distances** (transit,
grocery, school, …) attached to every listing.

Most rental scrapers give you one site in that site's own ad-hoc shape. This
Actor gives you **all sources in a single schema**, collapses the same unit
posted to multiple sites into one row, and can tell you *how far each place is
from the subway* — a signal no other rental scraper on Apify ships.

## Why this is different

| | Typical single-site scraper | This Actor |
|---|---|---|
| Sources | One | Kijiji + RentFaster (more coming) |
| Schema | Per-site, ad-hoc | One unified schema across sources |
| Duplicates | You dedupe yourself | Cross-source dedup built in (`also_on`) |
| Location intelligence | Lat/lng only | Nearest-amenity distances, **included free** |

## Input

**Scope**

| Field | Type | Default | Notes |
|---|---|---|---|
| `sources` | array | `["kijiji","rentfaster"]` | Sites to scrape |
| `cities` | array | `[]` (all) | City names, e.g. `["Toronto","Calgary"]` |
| `maxPerCity` | int | `100` | Cap per city, per source |
| `dedupe` | bool | `true` | Merge cross-source duplicates |

**Filters** (all optional — applied cheapest-first)

| Field | Type | Notes |
|---|---|---|
| `minRent` / `maxRent` | int | Monthly rent bounds, e.g. `maxRent: 2500` |
| `minBedrooms` / `maxBedrooms` | int | For exactly 2-bed, set both to `2`. `0` = include bachelor |
| `keywords` | array | Title/description must contain **ALL** (case-insensitive). e.g. `["female"]`, `["parking","balcony"]` |
| `excludeKeywords` | array | Drop if title/description contains **ANY**. e.g. `["no pets"]` |
| `nearAmenities` | array | Keep listings within `maxAmenityDistanceM` of **each** type: `subway, train, bus_stop, grocery, cafe, pharmacy, park, school, university, library, gym, hospital`. Auto-enables enrichment |
| `maxAmenityDistanceM` | int | Default `800` (≈10-min walk) |
| `nearAddress` | string | A specific place to anchor on, e.g. `"200 Bay St, Toronto"`. Geocoded, then listings kept within `nearAddressRadiusM`. **Use this for "near my workplace"** |
| `nearAddressRadiusM` | int | Default `2000` |

**Enrichment & infra**

| Field | Type | Default | Notes |
|---|---|---|---|
| `enrichAmenities` | bool | `false` | Attach `amenity_distances_m` even without a `nearAmenities` filter (fast, free) |
| `enrichRadiusM` | int | `1500` | Amenity search radius |
| `maxEnrich` | int | `200` | Cap on listings enriched per run |
| `proxyConfiguration` | object | Apify Proxy on | **Recommended** — see below |

Examples:

```json
// minimal
{ "sources": ["kijiji", "rentfaster"], "cities": ["Toronto"], "maxPerCity": 50 }

// "2-bed under $2500 in Toronto, near a subway, female-only"
{
  "cities": ["Toronto"], "minBedrooms": 2, "maxBedrooms": 2,
  "maxRent": 2500, "nearAmenities": ["subway"], "maxAmenityDistanceM": 800,
  "keywords": ["female"]
}

// "1-bed within 2 km of my office at 200 Bay St"
{ "cities": ["Toronto"], "maxBedrooms": 1, "nearAddress": "200 Bay St, Toronto", "nearAddressRadiusM": 2000 }
```

### "Near X" — two different mechanisms

- **Near a *type* of place** (subway, grocery, university…) → `nearAmenities`. Backed by OpenStreetMap; great coverage in Canadian cities.
- **Near a *specific* place** (your office, a named landmark) → `nearAddress`. Geocoded to one point. Amenity categories can't find a specific employer (OSM may not have "Company X"), so use `nearAddress` for that.

### Description questions (e.g. "female only", "no pets")

The full `description` is always in the output. For **literal** phrases, use `keywords` / `excludeKeywords` (deterministic, server-side). For **nuanced** interpretation, let an LLM read the returned descriptions — e.g. via [Apify's MCP server](https://docs.apify.com/platform/integrations/mcp), Claude can run this Actor and reason over the results in chat. Note: keyword matching is literal, so it depends on how the lister phrased it, and gender-restricted *whole-unit* ads may run into provincial human-rights rules (shared/roommate situations are typically exempt) — that's on the data, not the filter.

## Output

Each dataset item (empty fields omitted):

```json
{
  "source": "kijiji",
  "also_on": ["rentfaster"],
  "source_id": "1700123456",
  "url": "https://www.kijiji.ca/v-apartments-condos/...",
  "title": "Bright 2BR near subway",
  "address": "123 King St W, Toronto, ON",
  "city": "Toronto",
  "province": "ON",
  "postal_code": "M5V 1J5",
  "lat": 43.6453,
  "lng": -79.3806,
  "monthly_rent": 2450,
  "bedrooms": 2.0,
  "bathrooms": 1.0,
  "sqft": 720,
  "property_type": "apartment",
  "furnished": false,
  "pet_friendly": true,
  "utilities_included": ["heat", "water"],
  "available_from": "2026-07-01",
  "description": "…",
  "amenity_distances_m": { "subway": 320, "grocery": 150, "park": 410 },
  "scraped_at": "2026-06-27T12:00:00+00:00"
}
```

`bedrooms: 0.5` means bachelor/studio. `also_on` lists other sites the same
unit was found on (only when `dedupe` is enabled).

## Use it from Claude (MCP) 🤖

This Actor is built to be driven by an AI agent, not just a form. Connect
**[Apify's MCP server](https://docs.apify.com/platform/integrations/mcp)** to
Claude (Desktop, Claude Code, or any MCP client) and Claude can run it from a
plain-English request, then reason over the results — no code.

1. Add the hosted MCP server `https://mcp.apify.com` (OAuth), or run
   `@apify/actors-mcp-server` locally with your `APIFY_TOKEN`.
2. In Claude, just ask:
   > *"Run the Canada rentals actor for Toronto — 2-bed under $2500 near a subway — and recommend the best 3."*

Claude maps that straight onto the inputs —
`{cities:["Toronto"], minBedrooms:2, maxBedrooms:2, maxRent:2500, nearAmenities:["subway"]}` —
runs the Actor, reads the dataset, and answers in chat. Because the filters
(`maxRent`, `minBedrooms`, `keywords`, `nearAmenities`, `nearAddress`) are
pushed *into* the Actor, Claude isn't post-filtering a huge blob — it gets a
short, correct set back. The `amenity_distances_m` field is what lets it answer
*"near a subway / grocery / school"* precisely instead of guessing from text.

## Proxy — please read

- **RentFaster.ca** sits behind a Cloudflare managed challenge that fingerprints
  the TLS handshake, so browser-like *headers* alone get `403`. The Actor forges a
  real Chrome TLS/HTTP2 fingerprint (via `curl_cffi`) to clear it; a **residential
  Canadian proxy** is still recommended for a clean IP.
- **Kijiji** rate-limits and blocks datacenter IPs aggressively.

Use **Apify Proxy** (residential group) for production runs. The default input
already enables Apify Proxy.

## Amenity enrichment

Nearest-amenity distances come from a **bundled offline POI index** — ~225k
Canadian POIs (OpenStreetMap, via Geofabrik) shipped inside the Actor and queried
in-process. No external API, no rate limits: enriching hundreds of listings takes
well under a second, and it's **included free** (no per-listing enrichment charge).
The snapshot is refreshed periodically; POIs are static infrastructure so it
doesn't need to be live.

## Legal & fair use

Scrapes only **publicly visible** listing data — no logins, no private data.
You are responsible for your use of the output. Kijiji and RentFaster each have
Terms of Use that restrict automated access; review them and your jurisdiction's
rules, run politely (low concurrency, sensible caps), and don't redistribute in
ways those terms prohibit. This Actor is provided for research and personal use.

## Roadmap

- Facebook Marketplace + rentals.ca sources (best-effort; both are anti-bot).
- Listing-level change tracking (price drops, relistings).

---

Built from the data pipeline behind **EZrelocate**, a Canada-wide rental
recommender (Postgres + PostGIS + pgvector, Claude + Voyage embeddings).
