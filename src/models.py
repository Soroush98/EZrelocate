"""The unified listing record every source normalizes into.

This is the actor's public contract — the shape pushed to the Apify dataset:
the core listing fields plus `source`, `scraped_at`, cross-source dedup
metadata, and amenity distances.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone


@dataclass
class Listing:
    # Provenance
    source: str  # "kijiji" | "rentfaster" | ...
    source_id: str  # stable id within that source
    url: str

    # Core fields
    title: str | None = None
    address: str | None = None
    city: str = ""
    province: str = ""  # 2-letter code (ON, BC, ...)
    postal_code: str | None = None
    lat: float | None = None
    lng: float | None = None

    # Rental specifics
    monthly_rent: int | None = None  # CAD/month
    bedrooms: float | None = None  # 0.5 == bachelor/studio
    bathrooms: float | None = None
    sqft: int | None = None
    property_type: str | None = None
    furnished: bool | None = None
    pet_friendly: bool | None = None
    utilities_included: list[str] = field(default_factory=list)
    lease_length_months: int | None = None
    available_from: date | None = None
    description: str | None = None

    # Enrichment (optional, populated only when --enrichAmenities is on)
    amenity_distances_m: dict[str, int] | None = None
    # The located nearest amenity per type — same data as amenity_distances_m but
    # with coordinates, so a map can plot each POI. List of {t, lat, lng, m}.
    nearby_amenities: list[dict] | None = None

    # Cross-source dedup metadata (populated by dedup.py)
    also_on: list[str] = field(default_factory=list)

    # Stamped at push time
    scraped_at: str = ""

    def to_item(self) -> dict:
        """Serialize for Actor.push_data — JSON-friendly, no empty noise."""
        d = asdict(self)
        if isinstance(self.available_from, date):
            d["available_from"] = self.available_from.isoformat()
        if not self.scraped_at:
            d["scraped_at"] = datetime.now(timezone.utc).isoformat()
        # Drop keys that are None or empty containers to keep dataset rows lean.
        return {
            k: v
            for k, v in d.items()
            if v is not None and v != [] and v != {}
        }
