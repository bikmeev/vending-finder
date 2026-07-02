"""
verify_groceries_google.py

Spot-checks grocery coverage for the top-N scored buildings using the
Google Places API (New) Nearby Search endpoint, instead of a full-city
grid sweep. This is a deliberate scope choice, not a technical limitation:

- OSM's supermarket/convenience tagging is known to be incomplete (see
  make_map.py's note on the same issue). A full-city Google sweep would
  fix that everywhere, but at real request volume and real cost.
- What actually matters for a placement decision is grocery completeness
  specifically around the buildings we're seriously considering — so we
  only spend API calls on the top-N candidates already surfaced by
  score.py, not the other ~4,700 buildings we're not going to act on
  anyway.
- Only Essentials-tier fields are requested (displayName, location, types)
  to bill at the cheapest available SKU, which carries a 10,000
  free-calls/month pay-as-you-go allowance — a run over 50-100 buildings
  costs nothing.

Requires a Google Cloud project with the Places API (New) enabled and an
API key. Pass the key via the GOOGLE_MAPS_API_KEY environment variable
(preferred — keeps it out of shell history and out of git) or --api-key.

Usage:
    export GOOGLE_MAPS_API_KEY="your-key-here"
    python verify_groceries_google.py
    python verify_groceries_google.py --top 50 --radius 1000
"""

import argparse
import json
import logging
import math
import os
import time
from pathlib import Path

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("verify_groceries_google")

PLACES_API_URL = "https://places.googleapis.com/v1/places:searchNearby"
EARTH_RADIUS_M = 6_371_000

# Essentials-tier fields only, to bill at the cheapest SKU (see docstring).
FIELD_MASK = "places.displayName,places.location,places.types"

GROCERY_TYPES = ["supermarket", "grocery_store", "convenience_store"]

DEFAULT_MAX_BUILDINGS = 100  # hard safety ceiling, see --top validation below


def haversine_m(lat1, lon1, lat2, lon2):
    r1, r2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def query_nearby_groceries(lat, lon, radius_m, api_key, session):
    body = {
        "includedTypes": GROCERY_TYPES,
        "maxResultCount": 5,
        "rankPreference": "DISTANCE",
        "locationRestriction": {
            "circle": {"center": {"latitude": lat, "longitude": lon}, "radius": radius_m}
        },
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    resp = requests.post(PLACES_API_URL, headers=headers, data=json.dumps(body), timeout=15)
    if resp.status_code != 200:
        log.warning("Places API error %d for (%.5f, %.5f): %s", resp.status_code, lat, lon, resp.text[:300])
        return []
    return resp.json().get("places", [])


def run(scored_path: Path, output_path: Path, top_n: int, radius_m: float, api_key: str, sleep_between: float):
    if top_n > DEFAULT_MAX_BUILDINGS:
        raise SystemExit(
            f"--top {top_n} exceeds the safety ceiling of {DEFAULT_MAX_BUILDINGS}. "
            f"This script is meant for spot-checking top candidates, not a full-city sweep "
            f"(that needs a different, much more expensive approach — see the script docstring). "
            f"If you really want more, edit DEFAULT_MAX_BUILDINGS in the script."
        )

    df = pd.read_csv(scored_path)
    scored = df[df["score"].notna()].sort_values("score", ascending=False)
    targets = scored.head(top_n).copy()
    log.info("Verifying grocery coverage for top %d buildings (%d Places API requests)", len(targets), len(targets))

    session = requests.Session()
    google_dist, google_name, google_lat, google_lon, google_extra_count = [], [], [], [], []

    for _, row in targets.iterrows():
        places = query_nearby_groceries(row["lat"], row["lon"], radius_m, api_key, session)
        if not places:
            google_dist.append(None)
            google_name.append(None)
            google_lat.append(None)
            google_lon.append(None)
            google_extra_count.append(0)
        else:
            nearest = places[0]  # rankPreference=DISTANCE guarantees sorted order
            loc = nearest["location"]
            dist = haversine_m(row["lat"], row["lon"], loc["latitude"], loc["longitude"])
            google_dist.append(round(dist, 1))
            google_name.append(nearest.get("displayName", {}).get("text"))
            google_lat.append(loc["latitude"])
            google_lon.append(loc["longitude"])
            google_extra_count.append(len(places))
        time.sleep(sleep_between)

    targets["dist_nearest_grocery_google_m"] = google_dist
    targets["nearest_grocery_google_name"] = google_name
    targets["nearest_grocery_google_lat"] = google_lat
    targets["nearest_grocery_google_lon"] = google_lon
    targets["groceries_found_google"] = google_extra_count

    # Flag cases where Google found something much closer than OSM did —
    # these are exactly the "OSM coverage gap" cases this script exists to catch.
    both_known = targets["dist_nearest_grocery_google_m"].notna() & targets["dist_nearest_supermarket_m"].notna()
    targets["osm_coverage_gap_m"] = None
    targets.loc[both_known, "osm_coverage_gap_m"] = (
        targets.loc[both_known, "dist_nearest_supermarket_m"] - targets.loc[both_known, "dist_nearest_grocery_google_m"]
    ).round(1)

    # IMPORTANT: recompute score using the corrected (Google) distance where
    # we have one. The scoring rationale rewards distance from a grocery
    # store (farther = more captive audience) — if OSM's distance was wrong
    # (too large because it simply didn't know about a nearby shop), the
    # original score for that building is inflated and misleading. This
    # gives an honest "corrected_score" next to the original for comparison.
    from math import log1p
    effective_dist = targets["dist_nearest_grocery_google_m"].where(
        targets["dist_nearest_grocery_google_m"].notna(), targets["dist_nearest_supermarket_m"]
    )
    MAX_USEFUL_DISTANCE_M = 2000
    clipped = effective_dist.clip(upper=MAX_USEFUL_DISTANCE_M)
    pop_component = targets["estimated_flats"].apply(log1p)
    pop_norm = (pop_component - pop_component.min()) / max(pop_component.max() - pop_component.min(), 1e-9)
    dist_norm = (clipped - clipped.min()) / max(clipped.max() - clipped.min(), 1e-9)
    corrected = 0.6 * pop_norm + 0.4 * dist_norm
    targets["corrected_score"] = (100 * (corrected - corrected.min()) / max(corrected.max() - corrected.min(), 1e-9)).round(1)
    targets = targets.sort_values("corrected_score", ascending=False)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    targets.to_csv(output_path, index=False)
    log.info("Saved -> %s", output_path)

    n_found = targets["dist_nearest_grocery_google_m"].notna().sum()
    n_gap = (targets["osm_coverage_gap_m"].fillna(0) > 200).sum()  # Google found something 200m+ closer than OSM knew about
    print(f"\nGoogle found a grocery store within {radius_m:.0f}m for {n_found}/{len(targets)} buildings.")
    print(f"{n_gap} buildings had an OSM coverage gap of 200m+ (Google found something OSM missed).")

    cols = ["name", "score", "corrected_score", "dist_nearest_supermarket_m", "dist_nearest_grocery_google_m", "nearest_grocery_google_name", "osm_coverage_gap_m"]
    print("\n" + targets[cols].to_string(index=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    base = Path(__file__).resolve().parents[2] / "data" / "processed"
    parser.add_argument("--scored", type=Path, default=base / "dubai_buildings_scored.csv")
    parser.add_argument("--output", type=Path, default=base / "dubai_top_buildings_google_verified.csv")
    parser.add_argument("--top", type=int, default=50)
    parser.add_argument("--radius", type=float, default=1000.0, help="Search radius in meters (default 1000)")
    parser.add_argument("--sleep", type=float, default=0.1, help="Pause between requests in seconds")
    parser.add_argument("--api-key", type=str, default=None, help="Falls back to GOOGLE_MAPS_API_KEY env var")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise SystemExit(
            "No API key. Set it with: export GOOGLE_MAPS_API_KEY=\"your-key-here\"\n"
            "or pass --api-key directly (not recommended — ends up in shell history)."
        )

    run(args.scored, args.output, args.top, args.radius, api_key, args.sleep)


if __name__ == "__main__":
    main()
