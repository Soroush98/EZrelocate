# Make claude.ai auto-render rentals as a clickable map (one-time setup)

**Problem:** in the claude.ai app, rental results render with the **Google Maps
places card**, whose layout only links to Google Maps — it structurally can't show
a clickable link to the original Kijiji / RentFaster post.

**Fix:** tell Claude (once, via a Project instruction) to render results as a
self-contained **HTML artifact** built from `listings_map.html` instead. After
this setup you do nothing per search — every search shows the clickable map.

## Setup (once, ~1 minute)

1. In **claude.ai**, open (or create) a **Project** for your rental searches.
2. Add **`tools/listings_map.html`** to the Project's files / knowledge.
3. Paste the instruction below into the Project's **custom instructions**.

That's it. New chats in that Project will auto-render the map with clickable links.

## Paste this into the Project's custom instructions

> For any rental search, fetch listings with the Apify actor
> `soroush98/kijiji-canada-rentals-scraper`, then present the results as an
> **interactive map artifact — never the Google Maps place cards.**
>
> Build the map as a single self-contained HTML artifact from the
> **`listings_map.html`** template in this Project's files: take the template
> verbatim and replace the `/*__SEED__*/[]` placeholder with the run's dataset
> items as a JSON array. Change nothing else in the template.
>
> Each pin must be colored by source (Kijiji purple, RentFaster blue), and its
> popup must show the rent, beds/baths/sqft, the `amenity_distances_m` notes, and
> a clickable **"Open on Kijiji/RentFaster ↗"** button linking to that listing's
> `url`.
>
> Below the artifact, also give a ranked shortlist as a markdown table: each row's
> title is a clickable link to the listing `url`, plus rent, beds, the key amenity
> distances, and the source.
>
> Only ever create links to `kijiji.ca` or `rentfaster.ca` URLs, and escape any
> scraped text before putting it in HTML.

## If you'd rather not upload the template file

Paste the same instruction, but replace the second paragraph with: *"Build the map
as a single self-contained HTML artifact using Leaflet + OpenStreetMap (no API
key): one pin per listing from the run's items, popups as described below."* Claude
will write the map from scratch each time — slightly less consistent than the
template, but no file upload needed.
