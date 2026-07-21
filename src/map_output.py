"""Render an interactive HTML map of a run's listings, for hosting on the Actor's
key-value store.

Why this exists: end users drive the Actor through the Apify connector / console and
won't wire up anything client-side. So the run itself produces a self-contained map
(Leaflet + OpenStreetMap, no API key) and `main.py` saves it to the public
key-value store + links it from the run status — one click, clickable Kijiji /
RentFaster pins, no setup.

`TEMPLATE` is the single source of truth; `tools/listings_map.html` (the manual
"load any run's JSON" viewer) is regenerated from it via `python -m src.map_output`.
"""

from __future__ import annotations

import json

# Tokens replaced by render(): /*__SEED__*/[]  ->  the run's items;  __TITLE__ -> title.
TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="" />
<style>
  :root { --kijiji:#7c3aed; --rentfaster:#0ea5e9; --ink:#0f172a; --muted:#64748b; }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; font: 14px/1.45 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; color: var(--ink); }
  #app { display: flex; flex-direction: column; height: 100%; }
  header { display: flex; align-items: center; gap: 14px; padding: 10px 14px; border-bottom: 1px solid #e2e8f0; flex-wrap: wrap; }
  header h1 { font-size: 15px; margin: 0; font-weight: 650; }
  .count { color: var(--muted); }
  .spacer { flex: 1; }
  label.flt { display: inline-flex; align-items: center; gap: 5px; cursor: pointer; user-select: none; }
  .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
  button, .filebtn { font: inherit; border: 1px solid #cbd5e1; background: #fff; border-radius: 8px; padding: 6px 11px; cursor: pointer; }
  button:hover, .filebtn:hover { background: #f1f5f9; }
  #map { flex: 1; }
  input[type=file] { display: none; }
  .lp { min-width: 220px; max-width: 280px; }
  .lp .t { font-weight: 650; font-size: 14px; margin: 0 0 4px; }
  .lp .t a, .lp a.open { color: #2563eb; text-decoration: none; }
  .lp .t a:hover, .lp a.open:hover { text-decoration: underline; }
  .lp .rent { font-size: 16px; font-weight: 700; }
  .lp .row { color: #334155; margin: 3px 0; }
  .lp .am { color: var(--muted); font-size: 12px; margin: 5px 0; }
  .lp .addr { color: var(--muted); font-size: 12px; margin: 4px 0 8px; }
  .badge { display: inline-block; font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 999px; color: #fff; }
  .b-kijiji { background: var(--kijiji); } .b-rentfaster { background: var(--rentfaster); }
  .lp a.open { display: inline-block; margin-top: 6px; font-weight: 600; border: 1px solid #bfdbfe; background: #eff6ff; padding: 6px 10px; border-radius: 8px; }
  .alsoon { color: var(--muted); font-size: 11px; margin-left: 6px; }
  .hint { padding: 6px 14px; color: var(--muted); font-size: 12px; border-top: 1px solid #e2e8f0; }
  .legend { background: #fff; padding: 7px 9px; border-radius: 8px; box-shadow: 0 1px 5px rgba(0,0,0,.25); font-size: 11px; line-height: 1.8; max-width: 168px; }
  .legend b { display: block; margin-bottom: 3px; }
  .legend .li { display: inline-flex; align-items: center; gap: 4px; margin-right: 9px; white-space: nowrap; }
  .legend .li i { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
</style>
</head>
<body>
<div id="app">
  <header>
    <h1>__TITLE__</h1>
    <span class="count" id="count">&mdash;</span>
    <label class="flt"><input type="checkbox" id="f-kijiji" checked><span class="dot" style="background:var(--kijiji)"></span>Kijiji</label>
    <label class="flt"><input type="checkbox" id="f-rentfaster" checked><span class="dot" style="background:var(--rentfaster)"></span>RentFaster</label>
    <div class="spacer"></div>
    <label class="filebtn">Load run JSON<input type="file" id="file" accept=".json,application/json"></label>
    <button id="paste">Paste JSON&hellip;</button>
  </header>
  <div id="map"></div>
  <div class="hint">Click a pin to open the original Kijiji / RentFaster post. Filter by source above.</div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script>
const SEED = /*__SEED__*/[];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Only link out to the two sources we trust; blocks javascript:/data: URIs.
function safeUrl(u) {
  try {
    const url = new URL(u);
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    if (/(^|\.)kijiji\.ca$/.test(url.hostname) || /(^|\.)rentfaster\.ca$/.test(url.hostname))
      return url.href;
  } catch (e) {}
  return null;
}

const money = (n) => (n == null || n === "") ? null : "$" + Number(n).toLocaleString();
const beds = (b) => b == null ? null : (b === 0.5 ? "studio" : b + " bd");

function amenities(obj) {
  if (!obj || typeof obj !== "object") return "";
  return Object.entries(obj).sort((a, b) => a[1] - b[1])
    .map(([k, v]) => `${esc(k.replace(/_/g, " "))} ${v}m`).join(" · ");
}

function popupHtml(it) {
  const src = it.source === "kijiji" ? "kijiji" : it.source === "rentfaster" ? "rentfaster" : "other";
  const label = src === "kijiji" ? "Kijiji" : src === "rentfaster" ? "RentFaster" : esc(it.source);
  const url = safeUrl(it.url);
  const title = esc(it.title || it.address || "Listing");
  const titleHtml = url ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${title} ↗</a>` : title;
  const facts = [beds(it.bedrooms), it.bathrooms != null ? it.bathrooms + " ba" : null,
                 it.sqft ? it.sqft + " sqft" : null, esc(it.property_type || "")].filter(Boolean).join(" · ");
  const am = amenities(it.amenity_distances_m);
  const also = Array.isArray(it.also_on) && it.also_on.length ? `<span class="alsoon">also on ${esc(it.also_on.join(", "))}</span>` : "";
  const open = url ? `<a class="open" href="${url}" target="_blank" rel="noopener noreferrer">Open on ${label} ↗</a>`
                   : `<span class="am">(no link in this record)</span>`;
  return `<div class="lp">
    <p class="t">${titleHtml}</p>
    <div><span class="badge b-${src}">${label}</span>${also}</div>
    <div class="row"><span class="rent">${money(it.monthly_rent) || "—"}</span>${facts ? " · " + facts : ""}</div>
    ${am ? `<div class="am">${am}</div>` : ""}
    ${it.address ? `<div class="addr">${esc(it.address)}${it.city ? ", " + esc(it.city) : ""}</div>` : ""}
    ${open}
  </div>`;
}

const map = L.map("map", { scrollWheelZoom: true }).setView([56, -96], 4);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: "© OpenStreetMap contributors"
}).addTo(map);

// distinct color per amenity type (kept clear of the listing-pin colors)
const AMENITY_COLORS = {
  subway: "#d6336c", train: "#7048e8", bus_stop: "#f59f00", grocery: "#2f9e44",
  cafe: "#a9743b", pharmacy: "#e8590c", park: "#66a80f", school: "#1c7ed6",
  university: "#364fc7", library: "#0ca678", gym: "#f03e3e", hospital: "#ae3ec9"
};
const amenityLabel = (t) => String(t).replace(/_/g, " ");

let layer = L.layerGroup().addTo(map);
let amenityLayer = L.layerGroup().addTo(map);
let DATA = [];

function clearAmenities() { amenityLayer.clearLayers(); }

// Plot one listing's nearest amenities as colored dots, with a faint line back to
// the listing and a "type · 120m" tooltip. Called when its popup opens.
function showAmenities(it) {
  clearAmenities();
  const home = [Number(it.lat), Number(it.lng)];
  for (const a of (Array.isArray(it.nearby_amenities) ? it.nearby_amenities : [])) {
    const lat = Number(a.lat), lng = Number(a.lng);
    if (!isFinite(lat) || !isFinite(lng)) continue;
    const color = AMENITY_COLORS[a.t] || "#64748b";
    L.polyline([home, [lat, lng]], { color, weight: 2, opacity: 0.4 }).addTo(amenityLayer);
    L.circleMarker([lat, lng], { radius: 6, color: "#fff", weight: 1.5, fillColor: color, fillOpacity: 0.95 })
      .bindTooltip(`${esc(amenityLabel(a.t))} · ${a.m}m`, { direction: "top" })
      .addTo(amenityLayer);
  }
}

function render() {
  layer.clearLayers();
  clearAmenities();
  const show = { kijiji: document.getElementById("f-kijiji").checked,
                 rentfaster: document.getElementById("f-rentfaster").checked };
  const pts = [];
  let skipped = 0;
  for (const it of DATA) {
    const lat = Number(it.lat), lng = Number(it.lng);
    if (!isFinite(lat) || !isFinite(lng)) { skipped++; continue; }
    if (it.source in show && !show[it.source]) continue;
    const color = it.source === "kijiji" ? "#7c3aed" : it.source === "rentfaster" ? "#0ea5e9" : "#64748b";
    const m = L.circleMarker([lat, lng], { radius: 8, color: "#fff", weight: 1.5, fillColor: color, fillOpacity: 0.95 })
      .bindPopup(popupHtml(it), { maxWidth: 300 });
    const r = money(it.monthly_rent);
    if (r) m.bindTooltip(r, { direction: "top", offset: [0, -6] });
    m.on("popupopen", () => showAmenities(it));
    m.on("popupclose", clearAmenities);
    m.addTo(layer);
    pts.push([lat, lng]);
  }
  document.getElementById("count").textContent =
    `${pts.length} shown${skipped ? ` · ${skipped} without coordinates` : ""}`;
  if (pts.length) map.fitBounds(pts, { padding: [40, 40], maxZoom: 14 });
}

function load(items) {
  if (!Array.isArray(items)) { alert("Expected a JSON array of listing items."); return; }
  DATA = items; render();
}

document.getElementById("f-kijiji").onchange = render;
document.getElementById("f-rentfaster").onchange = render;
document.getElementById("file").onchange = (e) => {
  const f = e.target.files[0]; if (!f) return;
  const rd = new FileReader();
  rd.onload = () => { try { load(JSON.parse(rd.result)); } catch (err) { alert("Invalid JSON: " + err.message); } };
  rd.readAsText(f);
};
document.getElementById("paste").onclick = () => {
  const txt = prompt("Paste the run's JSON array of items:");
  if (!txt) return;
  try { load(JSON.parse(txt)); } catch (err) { alert("Invalid JSON: " + err.message); }
};

// amenity color legend (bottom-left)
const legend = L.control({ position: "bottomleft" });
legend.onAdd = function () {
  const div = L.DomUtil.create("div", "legend");
  div.innerHTML = "<b>Amenities (click a listing)</b>" + Object.entries(AMENITY_COLORS)
    .map(([t, c]) => `<span class="li"><i style="background:${c}"></i>${esc(amenityLabel(t))}</span>`).join("");
  return div;
};
legend.addTo(map);

load(SEED);
</script>
</body>
</html>
"""


def render_map(items: list[dict], *, title: str = "Canada Rentals — Map") -> str:
    """Return a self-contained HTML map with `items` baked in as the seed data."""
    seed = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    # SECURITY: the seed is embedded inside a <script> block, so a scraped value
    # containing "</script>" would otherwise break out of the script and run as
    # markup (stored XSS). json.dumps does NOT escape these. Escape the three HTML-
    # significant chars and the two JS line terminators to \uXXXX — valid JS escapes
    # that decode back to the original char but can't terminate the <script>.
    bs = chr(92)  # backslash, built unambiguously
    for raw, escaped in (
        ("<", bs + "u003c"), (">", bs + "u003e"), ("&", bs + "u0026"),
        (chr(0x2028), bs + "u2028"), (chr(0x2029), bs + "u2029"),
    ):
        seed = seed.replace(raw, escaped)
    # Plain str.replace (not .format) so the JS braces/${} survive untouched.
    return TEMPLATE.replace("/*__SEED__*/[]", "/*__SEED__*/" + seed).replace(
        "__TITLE__", title
    )


if __name__ == "__main__":
    # Regenerate the standalone manual viewer from this single template.
    import pathlib

    out = pathlib.Path(__file__).resolve().parents[1] / "tools" / "listings_map.html"
    out.write_text(render_map([], title="Canada Rentals — Map"))
    print(f"wrote {out}")
