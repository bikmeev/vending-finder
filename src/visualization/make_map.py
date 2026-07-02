"""
make_map.py

Generates a single self-contained interactive HTML map with:
- An "Interactive score" layer (default view) — buildings colored by a
  score computed LIVE in the browser from two sliders (population weight,
  max useful distance). No need to re-run score.py to try different
  weights; drag a slider and the map recolors instantly. This layer uses
  the Google-verified distance for any building that's been spot-checked
  (see verify_groceries_google.py) and falls back to the OSM-derived
  distance otherwise — so corrections actually reach the map instead of
  sitting unused in a side CSV (an earlier version of this script had that
  exact bug: Google verification data was computed but never merged back
  in, so the map kept showing stale OSM distances).
- Static score-band layers computed by score.py's fixed weights, kept for
  reference/comparison (toggle-able, off by default).
- Grocery stores (OSM-sourced, supermarkets + convenience combined).
- Grocery stores (Google-verified, for buildings that were spot-checked).
- An approximate foot-traffic heatmap (residential density proxy).

Open the output file directly in any browser — no server needed.

A note on incomplete grocery coverage:
OpenStreetMap's shop=supermarket/convenience tagging has the same
crowd-sourcing gaps as the building tags we ran into earlier in this
project — some real supermarkets and especially small independent grocery
shops simply aren't mapped. verify_groceries_google.py spot-checks
specific buildings against Google Places to catch this; the interactive
layer here prefers that corrected distance whenever it's available.

Usage:
    python make_map.py
    python make_map.py --output my_map.html
"""

import argparse
import json
import logging
from pathlib import Path

import folium
import pandas as pd
from folium import Element
from folium.plugins import HeatMap, MarkerCluster

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("make_map")

DUBAI_CENTER = (25.10, 55.20)

# (min_score, max_score, label, color, shown_by_default)
SCORE_BANDS = [
    (80, 100.001, "🟢 Excellent (80-100)", "#1a9850", False),
    (60, 80, "🟡 Good (60-80)", "#91cf60", False),
    (40, 60, "🟠 Average (40-60)", "#fee08b", False),
    (20, 40, "🔶 Below average (20-40)", "#fc8d59", False),
    (0, 20, "🔴 Poor (0-20)", "#d73027", False),
]

GROCERY_CATEGORIES = {"supermarket", "convenience"}

DEFAULT_POP_WEIGHT_PCT = 60  # matches score.py's --w-population 0.6 default
DEFAULT_MAX_DIST_M = 2000    # matches score.py's MAX_USEFUL_DISTANCE_M default


def building_popup_html(row) -> str:
    name = row["name"] if pd.notna(row.get("name")) else f"Building #{int(row['osm_id'])}"
    score = f"{row['score']:.1f}" if pd.notna(row.get("score")) else "n/a"
    flats = f"{row['estimated_flats']:.0f}" if pd.notna(row.get("estimated_flats")) else "unknown"
    dist = f"{row['dist_nearest_supermarket_m']:.0f} m" if pd.notna(row.get("dist_nearest_supermarket_m")) else "unknown"
    competitors = int(row["competitor_vending_300m"]) if pd.notna(row.get("competitor_vending_300m")) else 0
    confidence = row.get("confidence", "unknown")
    return (
        f"<b>{name}</b><br>"
        f"Fixed-weight score: <b>{score}</b><br>"
        f"Estimated apartments: {flats} <i>({confidence} confidence)</i><br>"
        f"Distance to nearest supermarket (OSM): {dist}<br>"
        f"Competing vending machines within 300m: {competitors}"
    )


def merge_google_corrections(scored: pd.DataFrame, google_verified_path: Path) -> pd.DataFrame:
    """Adds effective_dist_m (Google-verified distance where we have it,
    OSM distance otherwise) and a google_verified flag. This is the fix for
    the bug where Google verification results sat in a side CSV and never
    influenced the map."""
    scored = scored.copy()
    scored["effective_dist_m"] = scored["dist_nearest_supermarket_m"].astype(float)
    scored["google_verified"] = False

    if not google_verified_path.exists():
        log.info("No Google-verified file at %s — interactive layer will use OSM distances only.", google_verified_path)
        return scored

    gdf = pd.read_csv(google_verified_path)
    gdf = gdf.dropna(subset=["dist_nearest_grocery_google_m"])[["osm_id", "dist_nearest_grocery_google_m"]]
    scored = scored.merge(gdf, on="osm_id", how="left")
    has_google = scored["dist_nearest_grocery_google_m"].notna()
    scored.loc[has_google, "effective_dist_m"] = scored.loc[has_google, "dist_nearest_grocery_google_m"]
    scored.loc[has_google, "google_verified"] = True
    log.info("Merged Google-verified distances for %d/%d buildings.", has_google.sum(), len(scored))
    return scored


def add_interactive_layer(fmap, scored: pd.DataFrame):
    """Embeds building data as JSON and renders a client-side-scored layer
    with two sliders (population weight, max useful distance) that
    recompute the score live in the browser — same formula as score.py,
    just re-run in JS instead of Python so it's instant and doesn't need a
    server."""
    has_estimate = scored["estimated_flats"].notna() & scored["effective_dist_m"].notna()
    subset = scored[has_estimate]

    records = [
        {
            "lat": round(row["lat"], 5),
            "lon": round(row["lon"], 5),
            "name": (row["name"] if pd.notna(row.get("name")) else f"Building #{int(row['osm_id'])}"),
            "flats": round(row["estimated_flats"]),
            "dist": round(row["effective_dist_m"], 1),
            "verified": bool(row["google_verified"]),
        }
        for _, row in subset.iterrows()
    ]
    data_json = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    map_var = fmap.get_name()

    n_verified = sum(1 for r in records if r["verified"])

    script = f"""
    <style>
      #score-control-panel {{
        position: absolute; top: 12px; left: 60px; z-index: 1000;
        background: white; padding: 10px 14px; border-radius: 6px;
        box-shadow: 0 1px 6px rgba(0,0,0,0.4); font-family: Arial, sans-serif;
        font-size: 13px; width: 250px;
      }}
      #score-control-panel b {{ font-size: 14px; }}
      #score-control-panel label {{ display: block; margin-top: 10px; }}
      #score-control-panel input[type=range] {{ width: 100%; }}
      #score-control-panel .val {{ font-weight: bold; float: right; }}
      #score-control-panel .note {{ margin-top: 8px; font-size: 11px; color: #555; line-height: 1.4; }}
    </style>
    <div id="score-control-panel">
      <b>🎛️ Interactive score</b>
      <label>Population weight <span class="val" id="popWeightVal">{DEFAULT_POP_WEIGHT_PCT}%</span>
        <input type="range" id="popWeightSlider" min="0" max="100" value="{DEFAULT_POP_WEIGHT_PCT}">
      </label>
      <label>Distance weight <span class="val" id="distWeightVal">{100 - DEFAULT_POP_WEIGHT_PCT}%</span></label>
      <label>"Far enough" cutoff <span class="val" id="maxDistVal">{DEFAULT_MAX_DIST_M}m</span>
        <input type="range" id="maxDistSlider" min="200" max="3000" step="50" value="{DEFAULT_MAX_DIST_M}">
      </label>
      <div class="note">
        {len(records)} buildings shown, {n_verified} with a Google-verified grocery distance (✅ in popup).
        Farther-than-cutoff distances are treated as equally "far" — raising the cutoff makes the model
        more sensitive to very remote buildings; lowering it makes anything past that point equally good.
      </div>
    </div>
    <script>
    window.addEventListener('load', function() {{
      const buildingsData = {data_json};
      const map_obj = {map_var};
      const canvasRenderer = L.canvas({{padding: 0.5}});
      const interactiveLayer = L.layerGroup().addTo(map_obj);

      function interpColor(c1, c2, t) {{
        const r = Math.round(c1[0] + (c2[0]-c1[0])*t);
        const g = Math.round(c1[1] + (c2[1]-c1[1])*t);
        const b = Math.round(c1[2] + (c2[2]-c1[2])*t);
        return `rgb(${{r}},${{g}},${{b}})`;
      }}
      function colorForScore(s) {{
        if (s < 50) return interpColor([215,48,39],[254,224,139], s/50);
        return interpColor([254,224,139],[26,152,80], (s-50)/50);
      }}

      function computeAndRender(popWeightPct, maxDist) {{
        const popWeight = popWeightPct/100, distWeight = 1-popWeight;
        const logFlats = buildingsData.map(b => Math.log1p(b.flats));
        const minLog = Math.min(...logFlats), maxLog = Math.max(...logFlats);
        const clippedDist = buildingsData.map(b => Math.min(b.dist, maxDist));
        const minDist = Math.min(...clippedDist), maxDistObs = Math.max(...clippedDist);

        const raw = buildingsData.map((b, i) => {{
          const popNorm = (maxLog-minLog<1e-9) ? 0.5 : (logFlats[i]-minLog)/(maxLog-minLog);
          const distNorm = (maxDistObs-minDist<1e-9) ? 0.5 : (clippedDist[i]-minDist)/(maxDistObs-minDist);
          return popWeight*popNorm + distWeight*distNorm;
        }});
        const minRaw = Math.min(...raw), maxRaw = Math.max(...raw);

        interactiveLayer.clearLayers();
        buildingsData.forEach((b, i) => {{
          const score = (maxRaw-minRaw<1e-9) ? 50 : 100*(raw[i]-minRaw)/(maxRaw-minRaw);
          const color = colorForScore(score);
          const marker = L.circleMarker([b.lat, b.lon], {{
            radius: 6, color: color, fillColor: color, fillOpacity: 0.85, weight: 1, renderer: canvasRenderer
          }});
          const verifiedTag = b.verified ? " ✅ Google-verified" : " (OSM only, may be incomplete)";
          marker.bindPopup(`<b>${{b.name}}</b><br>Score: <b>${{score.toFixed(1)}}</b><br>` +
            `Estimated apartments: ${{b.flats}}<br>Distance to grocery: ${{b.dist.toFixed(0)}}m${{verifiedTag}}`);
          marker.addTo(interactiveLayer);
        }});
      }}

      const popSlider = document.getElementById('popWeightSlider');
      const distSlider = document.getElementById('maxDistSlider');
      popSlider.addEventListener('input', function() {{
        document.getElementById('popWeightVal').innerText = this.value + '%';
        document.getElementById('distWeightVal').innerText = (100-this.value) + '%';
      }});
      popSlider.addEventListener('change', function() {{
        computeAndRender(parseInt(this.value), parseInt(distSlider.value));
      }});
      distSlider.addEventListener('input', function() {{
        document.getElementById('maxDistVal').innerText = this.value + 'm';
      }});
      distSlider.addEventListener('change', function() {{
        computeAndRender(parseInt(popSlider.value), parseInt(this.value));
      }});

      computeAndRender({DEFAULT_POP_WEIGHT_PCT}, {DEFAULT_MAX_DIST_M});
    }});
    </script>
    """
    fmap.get_root().html.add_child(Element(script))


def add_foot_traffic_heatmap(fmap, scored: pd.DataFrame):
    """Approximate foot traffic layer. We have no real pedestrian count
    data — this is a proxy built from residential density alone (buildings
    weighted by estimated_flats). Labeled clearly as approximate."""
    has_estimate = scored["estimated_flats"].notna()
    heat_data = scored.loc[has_estimate, ["lat", "lon", "estimated_flats"]].values.tolist()
    if not heat_data:
        return
    layer = folium.FeatureGroup(name="🔥 Estimated foot traffic (residential density proxy, approximate)", show=False)
    HeatMap(heat_data, radius=18, blur=22, max_zoom=13).add_to(layer)
    layer.add_to(fmap)


def add_score_band_layers(fmap, scored: pd.DataFrame):
    for lo, hi, label, color, show_default in SCORE_BANDS:
        band_df = scored[(scored["score"] >= lo) & (scored["score"] < hi)]
        if band_df.empty:
            continue
        layer = folium.FeatureGroup(name=f"[Fixed weights] {label} — {len(band_df)} buildings", show=show_default)
        cluster = MarkerCluster().add_to(layer)
        for _, row in band_df.iterrows():
            folium.CircleMarker(
                location=(row["lat"], row["lon"]),
                radius=6,
                color=color,
                fill=True,
                fill_color=color,
                fill_opacity=0.85,
                popup=folium.Popup(building_popup_html(row), max_width=280),
                tooltip=row["name"] if pd.notna(row.get("name")) else None,
            ).add_to(cluster)
        layer.add_to(fmap)


def add_combined_grocery_layer(fmap, pois: list, google_csv_path: Path):
    """Single unified grocery layer combining OSM-sourced supermarkets/
    convenience stores and Google-verified finds. Not deduplicated against
    each other (the same real shop could appear from both sources with
    slightly different coordinates/names) — shown as two visually distinct
    icon colors within the same toggle-able layer so you can still tell
    them apart, without needing two separate checkboxes."""
    osm_matching = [p for p in pois if p["category"] in GROCERY_CATEGORIES]

    google_rows = []
    if google_csv_path.exists():
        gdf = pd.read_csv(google_csv_path)
        google_rows = gdf.dropna(subset=["nearest_grocery_google_lat", "nearest_grocery_google_lon"]).to_dict("records")

    total = len(osm_matching) + len(google_rows)
    if total == 0:
        return

    layer = folium.FeatureGroup(name=f"🛒 Grocery stores — {total} (OSM: {len(osm_matching)}, Google-verified: {len(google_rows)})", show=True)
    cluster = MarkerCluster().add_to(layer)

    for poi in osm_matching:
        name = poi.get("name") or ("Supermarket" if poi["category"] == "supermarket" else "Convenience store")
        icon_color = "green" if poi["category"] == "supermarket" else "lightgreen"
        folium.Marker(
            location=(poi["lat"], poi["lon"]),
            popup=f"{name} <i>(source: OSM)</i>",
            tooltip=name,
            icon=folium.Icon(color=icon_color, icon="shopping-cart", prefix="fa"),
        ).add_to(cluster)

    for row in google_rows:
        name = row.get("nearest_grocery_google_name") or "Grocery store"
        popup = (
            f"<b>{name}</b> <i>(source: Google, verified)</i><br>"
            f"{row['dist_nearest_grocery_google_m']:.0f}m from "
            f"{row['name'] if pd.notna(row.get('name')) else 'checked building'}"
        )
        folium.Marker(
            location=(row["nearest_grocery_google_lat"], row["nearest_grocery_google_lon"]),
            popup=folium.Popup(popup, max_width=280),
            tooltip=name,
            icon=folium.Icon(color="blue", icon="shopping-cart", prefix="fa"),
        ).add_to(cluster)

    layer.add_to(fmap)


def run(scored_path: Path, pois_path: Path, google_verified_path: Path, output_path: Path, show_fixed_comparison: bool = False):
    df = pd.read_csv(scored_path)
    pois = json.loads(pois_path.read_text())
    log.info("Loaded %d buildings and %d POIs", len(df), len(pois))

    scored = df[df["score"].notna()].copy()
    if scored.empty:
        raise SystemExit("No scored buildings found — run score.py first.")

    scored = merge_google_corrections(scored, google_verified_path)

    fmap = folium.Map(location=DUBAI_CENTER, zoom_start=11, tiles="cartodbpositron")

    add_interactive_layer(fmap, scored)
    if show_fixed_comparison:
        add_score_band_layers(fmap, scored)
    add_foot_traffic_heatmap(fmap, scored)
    add_combined_grocery_layer(fmap, pois, google_verified_path)

    folium.LayerControl(collapsed=False).add_to(fmap)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fmap.save(str(output_path))
    log.info("Saved interactive map -> %s", output_path)
    log.info("Open it directly in a browser: file://%s", output_path.resolve())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    base = Path(__file__).resolve().parents[2] / "data" / "processed"
    parser.add_argument("--scored", type=Path, default=base / "dubai_buildings_scored.csv")
    parser.add_argument("--pois", type=Path, default=base / "dubai_pois.json")
    parser.add_argument("--google-verified", type=Path, default=base / "dubai_top_buildings_google_verified.csv")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / "map.html")
    parser.add_argument(
        "--show-fixed-comparison", action="store_true",
        help="Also add the old fixed-weight score-band layers, off by default. "
             "Warning: these sit at the same coordinates as the interactive layer "
             "and show stale OSM-only distances — easy to click the wrong marker "
             "and think the data didn't update. Only enable if you specifically "
             "want to compare fixed vs interactive scoring side by side.",
    )
    args = parser.parse_args()

    run(args.scored, args.pois, args.google_verified, args.output, args.show_fixed_comparison)


if __name__ == "__main__":
    main()
