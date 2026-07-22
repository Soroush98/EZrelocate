"""Offline smoke test for the parsing + dedup logic (no network, no Apify SDK).

Run from the repo root:
    .venv/bin/python test_smoke.py
"""

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

import numpy as np

from src import normalize as N
from src.amenities_local import PoiIndex
from src.dedup import dedupe
from src.filters import passes_amenities, passes_basic, within_point
from src.models import Listing
from src.sources import kijiji, openrent, rentfaster, resolve_sources, sources_for, zumper

failures = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}: {name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        failures.append(name)


# --- normalize ---------------------------------------------------------------
check("province name", N.normalise_province("British Columbia"), "BC")
check("province code", N.normalise_province("on"), "ON")
check("US state name", N.normalise_region("California", "US"), "CA")
check("US state code", N.normalise_region("tx", "US"), "TX")
check("US unknown token (no CA leniency)", N.normalise_region("zz", "US"), None)
check("AU state name", N.normalise_region("New South Wales", "AU"), "NSW")
check("GB nation", N.normalise_region("Scotland", "GB"), "SCT")
check("US region from addr", N.region_from_address("600 Congress Ave, Austin, TX 78701", "US"), "TX")
check("US region needs uppercase", N.region_from_address("123 main st or nearby", "US"), None)
check("AU region from addr", N.region_from_address("5 George St, Sydney NSW 2000", "AU"), "NSW")
check("US zip is last match", N.postal_from_address("10250 Santa Monica Blvd, Los Angeles, CA 90067", "US"), "90067")
check("US zip+4", N.postal_from_address("1 Main St, Springfield, IL 62701-1234", "US"), "62701-1234")
check("GB postcode", N.postal_from_address("10 Downing Street, London sw1a 2aa", "GB"), "SW1A 2AA")
check("AU postcode end-anchored", N.postal_from_address("5 George St, Sydney NSW 2000", "AU"), "2000")
check("AU year not a postcode", N.postal_from_address("Built in 2019, Sydney NSW", "AU"), None)
check("money from string", N.parse_money("$2,450/mo"), 2450)
check("monthly rent passthrough", N.parse_monthly_rent("$2,450/mo"), 2450)
check("monthly rent plain number", N.parse_monthly_rent("2450"), 2450)
check("weekly rent pw", N.parse_monthly_rent("£450 pw"), 1950)
check("weekly rent per week", N.parse_monthly_rent("$650 per week"), 2817)
check("weekly rent /wk", N.parse_monthly_rent("450/wk"), 1950)
check("monthly marker beats weekly", N.parse_monthly_rent("£1,950 pcm (£450 pw)"), 1950)
check("sqft with commentary", N.parse_sqft("about 720 sq ft"), 720)
check("bedrooms bachelor", N.bedrooms_from_text("bachelor"), 0.5)
check("bedrooms + den", N.bedrooms_from_text("1 + Den"), 1.0)
check("available immediate", N.parse_available("Immediate", today=date(2026, 6, 27)), date(2026, 6, 27))
check("available month-day next year",
      N.parse_available("July 1", today=date(2026, 6, 27)), date(2026, 7, 1))
check("available negotiable", N.parse_available("Negotiable"), None)

# --- source registry ---------------------------------------------------------
check("registry: CA sources", sorted(sources_for("CA")), ["kijiji", "rentfaster"])
check("registry: GB sources", sources_for("GB"), ["openrent"])
check("registry: US sources", sources_for("US"), ["zumper"])
check("registry: empty request -> all for country", resolve_sources("ca", None), ["kijiji", "rentfaster"])
check("registry: GB default", resolve_sources("GB", None), ["openrent"])
check("registry: subset kept", resolve_sources("CA", ["kijiji"]), ["kijiji"])
check("registry: cross-country dropped", resolve_sources("CA", ["kijiji", "openrent"]), ["kijiji"])
for bad_call, label in [
    (lambda: resolve_sources("AU", None), "registry: AU has no sources yet"),
    (lambda: resolve_sources("XX", None), "registry: unknown country rejected"),
    (lambda: resolve_sources("CA", ["zillow"]), "registry: nothing usable rejected"),
]:
    try:
        bad_call()
        check(label, "no error", "ValueError")
    except ValueError:
        check(label, "ValueError", "ValueError")

# --- kijiji search-page parse ------------------------------------------------
apollo = {
    "id": "1700123456",
    "title": "Bright 2BR near subway",
    "url": "/v-apartments-condos/city-of-toronto/x/1700123456",
    "location": {
        "name": "Toronto",
        "address": "123 King St W, Toronto, ON M5V 1J5",
        "coordinates": {"latitude": 43.6453, "longitude": -79.3806},
    },
    "price": {"amount": 245000},
    "attributes": {"all": [
        {"canonicalName": "numberbedrooms", "canonicalValues": ["2"]},
        {"canonicalName": "numberbathrooms", "canonicalValues": ["10"]},
        {"canonicalName": "areainfeet", "canonicalValues": ["720"]},
        {"canonicalName": "unittype", "canonicalValues": ["apartment"]},
        {"canonicalName": "furnished", "canonicalValues": ["no"]},
        {"canonicalName": "petsallowed", "canonicalValues": ["limited"]},
        {"canonicalName": "heatincluded", "canonicalValues": ["1"]},
    ]},
    "description": "<p>Nice &amp; bright place</p>",
}
next_data = {"props": {"pageProps": {"__APOLLO_STATE__": {
    "RealEstateListing:1700123456": apollo,
    "SomethingElse:1": {"foo": "bar"},
}}}}
html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></body></html>'
kj = kijiji._listings_from_search(html)
check("kijiji count", len(kj), 1)
k = kj[0]
check("kijiji rent (cents->$)", k.monthly_rent, 2450)
check("kijiji beds", k.bedrooms, 2.0)
check("kijiji baths (x10)", k.bathrooms, 1.0)
check("kijiji sqft", k.sqft, 720)
check("kijiji province from addr", k.province, "ON")
check("kijiji postal", k.postal_code, "M5V 1J5")
check("kijiji furnished", k.furnished, False)
check("kijiji pets (limited)", k.pet_friendly, True)
check("kijiji utilities", k.utilities_included, ["heat"])
check("kijiji url absolutized", k.url.startswith("https://www.kijiji.ca/"), True)

# --- rentfaster parse --------------------------------------------------------
rf_raw = {
    "id": 123456,
    "link": "/rentals/listing/2br-king-st/123456",
    "title": "Bright 2BR near subway",
    "price": "2450",
    "type": "Apartment",
    "bedrooms": "2",
    "baths": "1",
    "sq_feet": "about 720",
    "latitude": "43.6454",
    "longitude": "-79.3807",
    "address": "123 King St W",
    "city": "Toronto",
    "availability": "July 1",
    "utilities_included": "Heat, Water",
    "intro": "Lovely unit close to transit",
    "cats": "1",
    "dogs": "0",
}
rf = rentfaster._parse(rf_raw, "ON")
check("rentfaster rent", rf.monthly_rent, 2450)
check("rentfaster beds", rf.bedrooms, 2.0)
check("rentfaster baths", rf.bathrooms, 1.0)
check("rentfaster sqft", rf.sqft, 720)
check("rentfaster type", rf.property_type, "apartment")
check("rentfaster province", rf.province, "ON")
check("rentfaster pets (cats)", rf.pet_friendly, True)
check("rentfaster utilities", sorted(rf.utilities_included), ["heat", "water"])
# "July 1" is parsed against the real today: this year if not yet past, else next.
_july1 = date(date.today().year, 7, 1)
if _july1 < date.today():
    _july1 = _july1.replace(year=_july1.year + 1)
check("rentfaster available", rf.available_from, _july1)
check("rentfaster source_id unique", rf.source_id, "123456_0")
check("rentfaster url", rf.url, "https://www.rentfaster.ca/rentals/listing/2br-king-st/123456")
check("rentfaster skip parking", rentfaster._parse({**rf_raw, "type": "Parking Spot"}, "ON"), None)

# --- openrent parse ----------------------------------------------------------
or_html = """<script>
var PROPERTYIDS = [ 111,222,333 ];
var PROPERTYLISTLATITUDES = [
51.5073,51.5074,51.5075];
var PROPERTYLISTLONGITUDES = [
-0.1302,-0.1303,-0.1304];
var NUMBEROFPROPERTIES = 3;
</script>"""
check("openrent search arrays", openrent._parse_search_arrays(or_html),
      [(111, 51.5073, -0.1302), (222, 51.5074, -0.1303), (333, 51.5075, -0.1304)])

or_rec = {
    "id": 222, "letAgreed": False, "title": "1 Bed Flat, Craven Street, WC2N",
    "description": "Spacious and stylish 1-bed in central London",
    "details": ["1 Bed", "1 Bath", "Furnished"],
    "rentPerMonth": 2750.0, "rentPerWeek": 634.6, "isMultiRoom": False,
}
ol = openrent._listing_from_record(or_rec, city="London", nation="ENG", lat=51.5074, lng=-0.1303)
check("openrent rent (site-monthly, no pw conversion)", ol.monthly_rent, 2750)
check("openrent beds", ol.bedrooms, 1.0)
check("openrent baths", ol.bathrooms, 1.0)
check("openrent furnished", ol.furnished, True)
check("openrent flat -> apartment", ol.property_type, "apartment")
check("openrent outward postcode", ol.postal_code, "WC2N")
check("openrent nation code", ol.province, "ENG")
check("openrent url from id", ol.url, "https://www.openrent.co.uk/222")
check("openrent letAgreed skipped",
      openrent._listing_from_record({**or_rec, "letAgreed": True}, city="London", nation="ENG"), None)
or_room = openrent._listing_from_record(
    {**or_rec, "title": "Room in a Shared House, Hackney, E8",
     "details": ["1 Room Available", "2 Bath", "Unfurnished"],
     "rentPerMonth": 0.0, "maxRoomRentPerMonth": 950.0},
    city="London", nation="ENG")
check("openrent shared -> room", or_room.property_type, "room")
check("openrent room beds", or_room.bedrooms, 1.0)
check("openrent unfurnished", or_room.furnished, False)
check("openrent HMO room rent", or_room.monthly_rent, 950)
or_studio = openrent._listing_from_record(
    {**or_rec, "title": "Studio Flat, London, SE1", "details": ["Studio", "1 Bath"]},
    city="London", nation="ENG")
check("openrent studio beds", or_studio.bedrooms, 0.5)
or_studio2 = openrent._listing_from_record(
    {**or_rec, "title": "Studio Flat, London, SE1", "details": ["1 Bath", "Furnished"]},
    city="London", nation="ENG")
check("openrent studio from title only", or_studio2.bedrooms, 0.5)
check("openrent country+currency", (ol.country, ol.currency), ("GB", "GBP"))

# --- zumper parse ------------------------------------------------------------
zp_state = {
    "currentSearch": {
        "listables": {
            "-1": [{
                "listing_id": 62671178, "building_id": 1835640,
                "building_name": "Highline Apartments", "title": "A1",
                "address": "13201 Legendary Dr", "city": "Austin", "state": "TX",
                "zipcode": "78727", "lat": 30.437264, "lng": -97.73076,
                "min_price": 1274, "max_price": 2576,
                "min_bedrooms": 1, "max_bedrooms": 3,
                "min_bathrooms": 1, "min_square_feet": 668,
                "property_type": 4, "pets": [1, 2],
                "date_available": "2026-08-01",
                "short_description": "Modern units near the domain",
                "url": "/apartment-buildings/p109486/highline-luxury-apartments-austin-tx",
            }],
            "-2": 7,
        }
    }
}
zp_html = f"<script>window.__PRELOADED_STATE__ = {json.dumps(zp_state)}; window.__GIT_SHA__ = 'x';</script>"
zp_recs = zumper._records_from_page(zp_html)
check("zumper state decode (trailing JS ok)", len(zp_recs), 1)
zl = zumper._listing_from_record(zp_recs[0], city="Austin", state="TX")
check("zumper rent = min_price", zl.monthly_rent, 1274)
check("zumper beds", zl.bedrooms, 1.0)
check("zumper sqft", zl.sqft, 668)
check("zumper zip", zl.postal_code, "78727")
check("zumper state code", zl.province, "TX")
check("zumper country+currency", (zl.country, zl.currency), ("US", "USD"))
check("zumper type 4 -> apartment", zl.property_type, "apartment")
check("zumper title = building — plan", zl.title, "Highline Apartments — A1")
check("zumper pets", zl.pet_friendly, True)
check("zumper available ISO", zl.available_from, date(2026, 8, 1))
check("zumper url absolutized", zl.url.startswith("https://www.zumper.com/"), True)
zl2 = zumper._listing_from_record(
    {**zp_recs[0], "min_bedrooms": 0, "pets": [], "property_type": 99}, city="Austin", state="TX")
check("zumper studio 0 -> 0.5", zl2.bedrooms, 0.5)
check("zumper empty pets -> unknown", zl2.pet_friendly, None)
check("zumper unknown type -> None", zl2.property_type, None)

# --- named POI index ---------------------------------------------------------
with tempfile.TemporaryDirectory() as _tmp:
    _npz = Path(_tmp) / "pois_test.npz"
    np.savez_compressed(
        _npz,
        cafe=np.array([[43.6500, -79.3800]], dtype=np.float32),
        cafe__names=np.array(["Sam's Cafe".encode()], dtype="S48"),
        bus_stop=np.array([[43.6501, -79.3801]], dtype=np.float32),  # unnamed category
    )
    _idx = PoiIndex(_npz)
    _hit = _idx.nearest_batch(np.array([43.6500]), np.array([-79.3800]), radius_m=500)[0]
    check("poi index: named category carries name", _hit["cafe"].get("name"), "Sam's Cafe")
    check("poi index: unnamed category has no name key", "name" in _hit["bus_stop"], False)

# --- cross-source dedup ------------------------------------------------------
deduped, merged = dedupe([k, rf])
check("dedupe merged count", merged, 1)
check("dedupe result count", len(deduped), 1)
kept = deduped[0]
check("dedupe keeps kijiji (priority)", kept.source, "kijiji")
check("dedupe records also_on", kept.also_on, ["rentfaster"])

# a genuinely different unit must NOT merge
other = Listing(source="rentfaster", source_id="999_0", url="x",
                city="Toronto", province="ON", lat=43.700, lng=-79.500,
                monthly_rent=3800, bedrooms=3.0)
deduped2, merged2 = dedupe([k, other])
check("dedupe leaves distinct units", (len(deduped2), merged2), (2, 0))

# --- to_item serialization (fresh object; k was mutated by dedupe above) -----
fresh = Listing(source="kijiji", source_id="z1", url="u", city="Toronto", monthly_rent=1000)
item = fresh.to_item()
check("to_item drops empty also_on", "also_on" in item, False)
check("to_item drops None bedrooms", "bedrooms" in item, False)
check("to_item has core fields", all(f in item for f in ("source", "monthly_rent", "city")), True)
check("to_item stamps scraped_at", bool(item.get("scraped_at")), True)
check("to_item carries country+currency", (item.get("country"), item.get("currency")), ("CA", "CAD"))

# --- filters -----------------------------------------------------------------
base = Listing(source="kijiji", source_id="f1", url="u", city="Toronto", province="ON",
               lat=43.6453, lng=-79.3806, monthly_rent=2200, bedrooms=2.0,
               title="Bright 2BR", description="Female only, parking included, no pets")

check("basic: within rent+beds", passes_basic(base, max_rent=2500, min_beds=2, max_beds=2), True)
check("basic: over max_rent", passes_basic(base, max_rent=2000), False)
check("basic: wrong beds", passes_basic(base, min_beds=3), False)
check("basic: keyword present (female)", passes_basic(base, keywords=["female"]), True)
check("basic: keyword ALL must match", passes_basic(base, keywords=["female", "pool"]), False)
check("basic: exclude keyword hit", passes_basic(base, exclude_keywords=["no pets"]), False)
check("basic: no filters -> pass", passes_basic(base), True)

# within_point: ~150 m away should pass a 800 m radius, ~5 km should not
check("within_point near", within_point(base, 43.6460, -79.3815, 800), True)
check("within_point far", within_point(base, 43.70, -79.50, 800), False)
check("within_point no coords", within_point(Listing(source="x", source_id="y", url="u"), 43.6, -79.3, 800), False)

# amenities filter
base.amenity_distances_m = {"subway": 320, "grocery": 900}
check("amenities: subway within 800", passes_amenities(base, ["subway"], 800), True)
check("amenities: grocery too far", passes_amenities(base, ["grocery"], 800), False)
check("amenities: needs ALL", passes_amenities(base, ["subway", "grocery"], 800), False)
check("amenities: missing key", passes_amenities(base, ["park"], 800), False)

print("\n" + ("ALL PASSED" if not failures else f"{len(failures)} FAILURES: {failures}"))
sys.exit(1 if failures else 0)
