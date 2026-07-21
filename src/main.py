"""Actor entrypoint: Canadian Rentals — Unified & Geo-Enriched.

Scrapes Kijiji + RentFaster into one normalized schema, applies optional filters
(rent / beds / keywords / near-an-address / near-an-amenity), collapses
cross-source duplicates, attaches nearest-amenity distances, then pushes the
result to the Apify dataset.

Filters run cheapest-first so the expensive ones touch the fewest listings:
  scrape → basic (rent/beds/keywords) → dedupe → nearAddress (geocode) →
  enrich + nearAmenities → push
"""

from __future__ import annotations

from datetime import datetime, timezone

from apify import Actor

from .dedup import dedupe
from .enrich import enrich
from .filters import passes_amenities, passes_basic, within_point
from .geocode import geocode
from .models import Listing
from .polite_client import DEFAULT_USER_AGENT, PoliteClient
from .sources import kijiji, rentfaster

SOURCES = {"kijiji": kijiji.scrape, "rentfaster": rentfaster.scrape}


def _opt_int(cfg: dict, key: str) -> int | None:
    v = cfg.get(key)
    return int(v) if v not in (None, "") else None


def _opt_num(cfg: dict, key: str) -> float | None:
    v = cfg.get(key)
    return float(v) if v not in (None, "") else None


def _interleave_by_source(listings: list[Listing]) -> list[Listing]:
    """Round-robin listings across their sources, preserving per-source order.

    Sources are scraped one after another, so a small maxResults cap on the raw
    order would return only the first source. Interleaving makes the cap return a
    balanced mix — which is the whole point of a *unified* feed.
    """
    from collections import deque

    groups: dict[str, deque] = {}
    for m in listings:
        groups.setdefault(m.source, deque()).append(m)
    queues = list(groups.values())
    out: list[Listing] = []
    while queues:
        for q in queues:
            if q:
                out.append(q.popleft())
        queues = [q for q in queues if q]
    return out


async def _charge(event: str, count: int = 1) -> None:
    """Charge one pay-per-event unit; safely no-ops when the Actor isn't monetized.

    Wrapped defensively so non-PPE runs — and the local test harness, which stubs
    Actor without a charging manager — don't break.
    """
    if count <= 0:
        return
    try:
        await Actor.charge(event_name=event, count=count)
    except Exception as e:  # noqa: BLE001 — not PPE / not monetized / stubbed
        Actor.log.debug(f"charge skipped: {event} x{count} ({e!r})")


async def main() -> None:
    async with Actor:
        cfg = await Actor.get_input() or {}

        sources = [s for s in (cfg.get("sources") or ["kijiji", "rentfaster"]) if s in SOURCES]
        if not sources:
            raise ValueError(f"No valid sources. Choose from: {sorted(SOURCES)}")
        cities = cfg.get("cities") or []
        max_per_city = int(cfg.get("maxPerCity", 100))
        do_dedupe = bool(cfg.get("dedupe", True))
        do_enrich = bool(cfg.get("enrichAmenities", False))

        # Filter inputs
        min_rent, max_rent = _opt_int(cfg, "minRent"), _opt_int(cfg, "maxRent")
        min_beds, max_beds = _opt_num(cfg, "minBedrooms"), _opt_num(cfg, "maxBedrooms")
        keywords = cfg.get("keywords") or []
        exclude_keywords = cfg.get("excludeKeywords") or []
        near_amenities = cfg.get("nearAmenities") or []
        max_amenity_m = int(cfg.get("maxAmenityDistanceM", 800))
        near_address = (cfg.get("nearAddress") or "").strip()
        near_address_radius = int(cfg.get("nearAddressRadiusM", 2000))
        mr = _opt_int(cfg, "maxResults")
        max_results = mr if mr is not None else 50  # total output cap
        has_basic = any(
            v is not None for v in (min_rent, max_rent, min_beds, max_beds)
        ) or bool(keywords or exclude_keywords)

        proxy_cfg = await Actor.create_proxy_configuration(
            actor_proxy_input=cfg.get("proxyConfiguration")
        )
        # Pass the URL *factory* (not a single URL) so PoliteClient rotates the
        # residential exit IP on every request.
        proxy_new_url = proxy_cfg.new_url if proxy_cfg else None

        Actor.log.info(
            f"sources={sources} cities={cities or 'ALL'} max_per_city={max_per_city} "
            f"max_results={max_results} "
            f"filters(rent={min_rent}-{max_rent}, beds={min_beds}-{max_beds}, "
            f"keywords={keywords}, exclude={exclude_keywords}, "
            f"nearAmenities={near_amenities}@{max_amenity_m}m, "
            f"nearAddress={near_address!r}@{near_address_radius}m) "
            f"dedupe={do_dedupe} "
            f"proxy={'on (kijiji rotates, rentfaster sticky+chrome-TLS)' if proxy_cfg else 'off'}"
        )

        # 1. Scrape ------------------------------------------------------------
        collected: list[Listing] = []
        per_source: dict[str, int] = {}
        for name in sources:
            before = len(collected)
            # Per-source proxy strategy:
            #   rentfaster -> STICKY single IP + persistent cookie jar + Chrome TLS
            #                 impersonation (curl_cffi), so its Cloudflare cookie
            #                 stays paired with one IP and the JA3 fingerprint clears
            #                 the managed challenge that plain httpx is 403'd by.
            #   kijiji     -> ROTATING IP per request (spreads load, dodges
            #                 per-IP rate limits on a plain HTML site).
            if name == "rentfaster":
                sticky = (
                    await proxy_cfg.new_url(session_id="rentfaster")
                    if proxy_cfg
                    else None
                )
                proxy_kwargs = {"proxy": sticky, "impersonate": "chrome"}
            else:
                proxy_kwargs = {"proxy_new_url": proxy_new_url}
            async with PoliteClient(
                max_concurrency=int(cfg.get("maxConcurrency", 3)),
                min_delay_ms=int(cfg.get("minDelayMs", 500)),
                max_delay_ms=int(cfg.get("maxDelayMs", 1500)),
                user_agent=cfg.get("userAgent") or DEFAULT_USER_AGENT,
                **proxy_kwargs,
            ) as client:
                async for listing in SOURCES[name](
                    client, cities=cities, max_per_city=max_per_city, log=Actor.log
                ):
                    collected.append(listing)
            per_source[name] = len(collected) - before
        Actor.log.info(f"scraped {len(collected)} raw listings: {per_source}")

        # 2. Basic filters (free) ---------------------------------------------
        if has_basic:
            kept = [
                m for m in collected
                if passes_basic(
                    m,
                    min_rent=min_rent, max_rent=max_rent,
                    min_beds=min_beds, max_beds=max_beds,
                    keywords=keywords, exclude_keywords=exclude_keywords,
                )
            ]
            Actor.log.info(f"basic filters: {len(collected)} -> {len(kept)} kept")
            collected = kept

        # 3. Cross-source dedupe ----------------------------------------------
        merged = 0
        if do_dedupe:
            collected, merged = dedupe(collected)
            Actor.log.info(f"deduped: {merged} cross-source duplicates merged")

        # Balance sources so a maxResults cap returns a mix, not just source #1.
        collected = _interleave_by_source(collected)

        # 4. nearAddress — geocode a specific place, keep listings within radius
        if near_address:
            point = await geocode(near_address)
            if point:
                before = len(collected)
                collected = [
                    m for m in collected
                    if within_point(m, point[0], point[1], near_address_radius)
                ]
                Actor.log.info(
                    f"nearAddress {near_address!r} -> {point}: "
                    f"{before} -> {len(collected)} within {near_address_radius}m"
                )
            else:
                Actor.log.warning(
                    f"nearAddress {near_address!r} did not geocode — skipping that filter"
                )

        # 5. Trim to the output cap. When we're NOT amenity-filtering, nothing
        #    downstream drops listings, so trimming now also bounds how many we
        #    enrich (the expensive step). With an amenity filter we keep the full
        #    candidate set so the filter has enough to choose from, then cap last.
        if not near_amenities and len(collected) > max_results:
            Actor.log.info(f"capping to maxResults={max_results} (pre-enrich)")
            collected = collected[:max_results]

        # 6. Enrich + nearAmenities filter (expensive — runs on survivors) ----
        enriched = 0
        need_enrich = do_enrich or bool(near_amenities)
        if need_enrich:
            radius = max(int(cfg.get("enrichRadiusM", 1500)), max_amenity_m)
            enriched = await enrich(
                collected,
                radius_m=radius,
                max_enrich=int(cfg.get("maxEnrich", 200)),
                log=Actor.log,
            )
        if near_amenities:
            before = len(collected)
            collected = [
                m for m in collected
                if passes_amenities(m, near_amenities, max_amenity_m)
            ]
            Actor.log.info(
                f"nearAmenities {near_amenities}@{max_amenity_m}m: "
                f"{before} -> {len(collected)} kept"
            )
            # Final cap (amenity filter may have left more than maxResults).
            if len(collected) > max_results:
                Actor.log.info(f"capping to maxResults={max_results}")
                collected = collected[:max_results]

        # 7. Push --------------------------------------------------------------
        stamp = datetime.now(timezone.utc).isoformat()
        items = []
        for m in collected:
            m.scraped_at = stamp
            items.append(m.to_item())
        if items:
            await Actor.push_data(items)
        # Pay-per-event billing: one charge per returned listing. Geo-enrichment is
        # free (now a ~0.5 ms local lookup, not a paid Overpass call), so it is not
        # billed; `enriched` is kept only for RUN_STATS. There is no actor-start fee.
        await _charge("listing-result", len(items))

        # 8. Interactive map ---------------------------------------------------
        # Build a self-contained map of this run and host it on the public
        # key-value store, then surface the link in the run status. End users drive
        # the Actor from the connector/console and won't wire up anything; this gives
        # them one click to a map with clickable Kijiji/RentFaster pins, no setup.
        map_url = await _publish_map(items)

        await Actor.set_value(
            "RUN_STATS",
            {
                "scraped_per_source": per_source,
                "final_count": len(collected),
                "duplicates_merged": merged,
                "amenity_enriched": enriched,
                "map_url": map_url,
                "finished_at": stamp,
            },
        )
        # OUTPUT + status message are what the Apify connector surfaces back to the
        # user, so put the map link in both.
        await Actor.set_value("OUTPUT", {"listings": len(items), "mapUrl": map_url})
        status = f"Done — {len(items)} listings."
        if map_url:
            status += f" 🗺️ Interactive map (clickable listing links): {map_url}"
        await Actor.set_status_message(status)
        Actor.log.info(f"done — pushed {len(items)} listings" + (f" · map: {map_url}" if map_url else ""))


async def _publish_map(items: list[dict]) -> str | None:
    """Render the run's listings to an HTML map, store it on the run's key-value
    store, and return a SIGNED public URL for the map record. Best-effort.

    SECURITY: we return a per-record signed URL and deliberately do NOT make the
    whole store public. A store-wide public setting would expose every record in the
    run store — INPUT (search params + proxyConfiguration), OUTPUT, RUN_STATS — to
    anyone holding the map link, since the link reveals the store ID. The signed URL
    grants access to the `map` record only; everything else stays private."""
    if not items:
        return None
    try:
        from .map_output import render_map

        kvs = await Actor.open_key_value_store()
        html = render_map(items, title=f"Canada Rentals — {len(items)} listings")
        await kvs.set_value("map", html, content_type="text/html; charset=utf-8")
        # Anonymously readable the instant the run ends; scoped to this record only.
        return await kvs.get_public_url("map")
    except Exception as e:  # noqa: BLE001 — the map is a bonus, not the product
        Actor.log.warning(f"map publish skipped ({e!r})")
        return None
