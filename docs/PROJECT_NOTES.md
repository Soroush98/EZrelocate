# EZrelocate — Project Notes & Learnings

A running log of **empirical, non-obvious** things we've learned building this
project — model experiments (what was slow / inaccurate / expensive), decisions and
their rationale, and cross-cutting gotchas. The goal is to **not relearn the same
lesson twice**.

Keep entries dated and concrete. This is for things you can't recover by reading the
code or git history — write down the *why* and the *what we ruled out*, not the *what*.

---

## Models in use (current)

| Component | Model | Dim / params | Where set | Notes |
|---|---|---|---|---|
| Query parsing (NL → filters) | `claude-opus-4-7` | `max_tokens=512` | `ANTHROPIC_MODEL` (config.py / .env / workflows) | 30s request deadline |
| Recommendation generation | `claude-opus-4-7` | `max_tokens=900` | same | 45s request deadline |
| Embeddings (listing + query) | `voyage-3-large` | **1024-dim** | `VOYAGE_MODEL` | 20s deadline on the query path |

> ⚠️ **Embedding dimension is load-bearing.** `voyage-3-large` outputs 1024-dim
> vectors, which must match `desc_embed VECTOR(1024)` in `db/schema.sql`. Changing the
> embedding model means changing the column type **and** re-embedding every listing.

---

## Model experiments & evaluation log

Append a dated entry whenever we try a model/prompt/param and learn something —
especially when something was **slow, inaccurate, or too expensive** and we backed it
out. Template:

```
### YYYY-MM-DD — <what we tried>
- Context: <task — parsing / generation / embedding>
- Result: <slow? inaccurate? costly? how measured>
- Decision: <kept / reverted / switched to X>
- Why: <the reasoning so we don't retry it blindly>
```

### 2026-06-14 — Local Ollama model (self-hosted LLM)
- Context: evaluated running the LLM locally via Ollama instead of hosted Claude.
- Result: **too slow on Mac** — local inference latency wasn't acceptable for the
  interactive query path.
- Decision: reverted; use hosted Claude (`claude-opus-4-7`) for parsing + generation.
- Why: query latency is user-facing, and local Ollama on Mac dev hardware couldn't
  meet it. Don't retry a self-hosted LLM on the live request path on dev hardware.
  _(Which Ollama model + rough latency: fill in if remembered.)_

<!-- TODO: still to record — where Claude mis-parses queries, any embedding models
     that under-retrieved, cost/latency numbers. Tell me and I'll log them. -->

---

## Known inaccuracies & limitations

- _(to fill in)_ Query-parsing failure modes — phrasings the parser mis-maps
  (e.g. named-landmark vs. generic-amenity confusion, province/city edge cases).
- _(to fill in)_ Retrieval gaps — where the lifestyle embedding rerank under- or
  over-weights vs. the hard SQL filters.
- `get_client_ip` trusts the leftmost `X-Forwarded-For`; spoofable if someone hits
  the Fly URL directly instead of going through Vercel. Accepted for portfolio-scale
  rate limiting (`app/services/auth.py`).
- Supabase JWTs live in browser storage (XSS-readable), not httpOnly cookies — a
  known tradeoff for this app's scale.

---

## Cross-cutting seams — change these together

One concept, edited in more than one place. If you touch one, touch the others:

- **Amenity categories:** `app/models.py::AmenityCategory` ↔
  `etl/load_osm_pois_geofabrik.py::CATEGORIES` ↔ `frontend/src/lib/types.ts`.
- **Embedding dimension:** `db/schema.sql` (`VECTOR(1024)`) ↔ the Voyage model choice.
- **Default model ids:** `app/config.py` ↔ `.env.example` ↔ both GitHub workflows
  (`refresh.yml`, `osm-pois.yml`).

---

## Decisions & rationale

- **POI ingest is weekly (Geofabrik offline `.pbf`), not nightly.** The public
  Overpass API hard-rate-limits CI IPs; POIs are static infra that changes slowly, so
  the nightly run only recomputes distances against POIs already in the DB.
- **Quota gate runs before any LLM/embedding spend** so a rejected request costs zero
  tokens (`app/services/query.py` → `enforce_query_quota`).
- **Per-request deadlines on every model/embedding call** (added 2026-06-14) so a slow
  upstream returns a retryable 503 instead of hanging or 500ing.

---

## Data sourcing — what's viable per site

| Site | Access | Status |
|---|---|---|
| Kijiji | Parse search-page `__NEXT_DATA__` Apollo cache (~40/req, no detail fetches) | **In use** (etl/scrape_kijiji.py) |
| RentFaster.ca | **Public JSON API** `GET /api/search.json?proximity_type=location-city&novacancy=0&cur_page=N`; scope via `lastcity=<prov>/<city>` cookie. Returns `{listings, query, total, total2}` | **Viable** — see Cloudflare note below |
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

### 2026-06-27 — Apify packaging decision (apify-actor/)
- Built a standalone Apify actor (`apify-actor/`) that repackages the scrapers as a
  *unified, geo-enriched* Canadian rentals dataset. It's a clean-room port (no DB / no
  Claude / Voyage) — pushes flat JSON to an Apify dataset.
- Scope decision: ship **Kijiji + RentFaster only**. Standalone Kijiji and FB
  Marketplace scrapers are already saturated on Apify Store (10+ each); the unique,
  defensible angles are (a) one normalized schema across sources, (b) cross-source
  dedup, (c) amenity-distance enrichment — none of which existing actors offer. FB
  Marketplace + rentals.ca deferred (saturated/blocked, and we'd already ruled both
  out for the main app).
- **Apify actor dep pins are load-bearing.** `apify==2.7.3` pulls `crawlee==0.6.12`,
  which crashes at *runtime* (build succeeds, container dies on import) unless you
  pin `pydantic>=2.10,<2.12` (else "cannot specify both default and default_factory")
  and `browserforge==1.2.3` (1.2.4 renamed `download.DATA_FILES`). Verified set is in
  `apify-actor/requirements.txt`. The Apify *build* won't catch this — only a cloud
  *run* does.
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

### 2026-07-12 — Nightly refresh failed 6 nights straight: mark_stale vs statement_timeout
- Root cause chain: Kijiji rate-limits (429s) shrank nightly scrape coverage → the
  72h-unseen backlog grew → `mark_stale`'s single bulk UPDATE outgrew Supabase's
  2-minute `statement_timeout` → rollback each night → backlog grew further
  (self-reinforcing; 8.3k rows by 2026-07-12).
- **Bulk UPDATEs that touch `listings.status` cost ~75ms/row** (measured): `status`
  is in `listings_filter_idx`, so no HOT updates — every flipped row re-inserts into
  ALL listings indexes, and the HNSW `desc_embed` index dominates. Any future bulk
  write to an indexed listings column has the same trap.
- Fix: `mark_stale` now batches (250 rows/UPDATE ≈ 20s, comfortably under the 2min
  pooler timeout). Same pattern as `compute_amenity_distances`' 100-listing chunks.
- Why record: the ~75ms/row HNSW write cost and the 2min Supabase statement_timeout
  are invisible locally and only bite on bulk ETL writes.

### 2026-07-18 — Nightly refresh failed 3 nights: compute_amenity_distances vs statement_timeout
- Second bite of the 2026-07-12 trap, one step later in the same workflow. Once
  `mark_stale` was batched, the nightly advanced to `compute_amenity_distances`,
  whose "reset amenity_distances_m to '{}'" pre-pass was ONE bulk UPDATE over all
  ~10.6k active listings. Non-HOT writes at ~75ms/row ≈ 13min → killed at 2min.
  The spatial join itself was already chunked; only the reset pass wasn't.
- Fix: dropped the reset pass entirely. The chunked UPDATE now LEFT JOINs the
  chunk's listings against the rolled-up distances, so listings with no POIs in
  range get `'{}'` in the same statement (removed categories still can't linger),
  and `IS DISTINCT FROM` skips rows whose value is unchanged. POIs and listing
  locations are static, so steady-state nights write almost nothing — the job
  went from rewriting every active row twice to writing only real changes.
- Lesson: when auditing for the statement_timeout trap, grep every `UPDATE listings`
  in the ETL, including "cheap-looking" reset/housekeeping statements — per-row
  index cost (~75ms, HNSW-dominated) makes row count the only thing that matters.
