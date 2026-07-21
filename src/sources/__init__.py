"""Source registry — the single seam for adding a scraper or a country.

Every source module declares which country it serves here; main.py resolves the
run's `country` input against this registry. Adding a country end-to-end also
requires: an entry in `.actor/input_schema.json`'s country enum, region/postal
tables in normalize.py, and a POI index at `src/data/pois_<cc>.npz` for amenity
enrichment (see tools/build_poi_index.py).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import NamedTuple

from ..models import Listing
from . import kijiji, rentfaster

# ISO 3166-1 alpha-2 -> display name. The pipeline is written against these;
# only countries that also have sources below are exposed in the input schema.
COUNTRIES = {
    "CA": "Canada",
    "US": "United States",
    "GB": "United Kingdom",
    "AU": "Australia",
}

CURRENCIES = {"CA": "CAD", "US": "USD", "GB": "GBP", "AU": "AUD"}


class Source(NamedTuple):
    scrape: Callable[..., AsyncIterator[Listing]]
    country: str  # ISO 3166-1 alpha-2, key into COUNTRIES


REGISTRY: dict[str, Source] = {
    "kijiji": Source(kijiji.scrape, "CA"),
    "rentfaster": Source(rentfaster.scrape, "CA"),
}


def sources_for(country: str) -> list[str]:
    return [name for name, spec in REGISTRY.items() if spec.country == country]


def resolve_sources(country: str, requested: list[str] | None) -> list[str]:
    """Default/validate the run's sources for its country.

    Empty request -> every source for that country. Requested sources from other
    countries are dropped (the caller logs them); raises ValueError with an
    actionable message when nothing usable remains, rather than silently
    scraping the wrong country.
    """
    country = (country or "CA").strip().upper()
    if country not in COUNTRIES:
        raise ValueError(
            f"Unknown country {country!r}. Supported: {sorted(COUNTRIES)}"
        )
    available = sources_for(country)
    if not available:
        implemented = sorted({spec.country for spec in REGISTRY.values()})
        raise ValueError(
            f"No sources implemented for {COUNTRIES[country]} yet. "
            f"Countries with sources: {implemented}"
        )
    if not requested:
        return available
    picked = [s for s in requested if s in available]
    if not picked:
        raise ValueError(
            f"None of sources={requested} serve {COUNTRIES[country]} ({country}). "
            f"Available: {available}"
        )
    return picked
