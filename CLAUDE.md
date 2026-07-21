# EZrelocate — Apify actor

Apify actor scraping rental listings (currently Canada: Kijiji + RentFaster.ca)
into one normalized, deduplicated dataset with offline nearest-amenity
enrichment. The pipeline is country-aware (CA/US/GB/AU scaffolding; only CA has
sources so far — the registry in `src/sources/__init__.py` is the seam).
Python, no database — listings go straight to the Apify dataset. Deploy is
manual via `apify push`.

The FastAPI backend / Next.js frontend this grew out of were removed from the
repo in July 2026 — see git history before then if you need them.

## Before working here, read

- **[docs/PROJECT_NOTES.md](docs/PROJECT_NOTES.md)** — running log of empirical
  learnings: which sites are scrapable and how, proxy/Cloudflare gotchas,
  load-bearing dependency pins, and key decisions. **Check it before
  scraper/dependency/schema work, and append to it when you learn something
  worth remembering.**

## Conventions

- Engineering practices come from the public user-level Claude skills
  (engineering-principles, python-practices, etc.) — there are intentionally no
  project-local copies in `.claude/skills/`.
