"""
compute_distances.py

Adds distance-based features to the validated buildings table:
- dist_nearest_supermarket_m: distance to the nearest real grocery option
  (supermarket or greengrocer) — the main "would residents just walk
  there instead" signal.
- dist_nearest_shop_m: distance to the nearest supermarket OR convenience
  store (a looser, more permissive version of the same idea).
- competitor_vending_300m: count of existing vending machines within 300m
  — direct competition.

Approach: project both buildings and POIs into local flat (equirectangular)
meters around Dubai's centroid, then use a KD-tree (scipy) for fast nearest-
neighbor and radius queries. Brute-force distance (11k buildings x a few
thousand POIs) would be ~tens of millions of comparisons — the KD-tree
turns that into a near-instant lookup.

Usage:
    python compute_distances.py
    python compute_distances.py --buildings ../../data/processed/dubai_buildings_validated.csv \
                                 --pois ../../data/processed/dubai_pois.json
"""

import argparse
import json
import logging
import math
from pathlib import Path

import pandas as pd
from scipy.spatial import cKDTree

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("compute_distances")

EARTH_RADIUS_M = 6_371_000
COMPETITOR_RADIUS_M = 300


def project_to_meters(lats, lons, lat0):
    """Same local equirectangular approximation used for footprint area in
    extract_from_pbf.py — accurate to well under 1% for a city-sized area
    like Dubai (~50km across)."""
    cos_lat0 = math.cos(math.radians(lat0))
    xs = [math.radians(lon) * cos_lat0 * EARTH_RADIUS_M for lon in lons]
    ys = [math.radians(lat) * EARTH_RADIUS_M for lat in lats]
    return xs, ys


def run(buildings_path: Path, pois_path: Path, output_path: Path):
    df = pd.read_csv(buildings_path)
    pois = json.loads(pois_path.read_text())
    log.info("Loaded %d buildings and %d POIs", len(df), len(pois))

    df = df.dropna(subset=["lat", "lon"]).reset_index(drop=True)

    lat0 = df["lat"].mean()
    b_xs, b_ys = project_to_meters(df["lat"].tolist(), df["lon"].tolist(), lat0)
    building_coords = list(zip(b_xs, b_ys))

    by_category = {}
    for poi in pois:
        by_category.setdefault(poi["category"], []).append(poi)

    def poi_tree(category_list):
        if not category_list:
            return None
        lats = [p["lat"] for p in category_list]
        lons = [p["lon"] for p in category_list]
        xs, ys = project_to_meters(lats, lons, lat0)
        return cKDTree(list(zip(xs, ys)))

    supermarket_pois = by_category.get("supermarket", [])
    shop_pois = by_category.get("supermarket", []) + by_category.get("convenience", [])
    competitor_pois = by_category.get("competitor_vending", [])

    supermarket_tree = poi_tree(supermarket_pois)
    shop_tree = poi_tree(shop_pois)
    competitor_tree = poi_tree(competitor_pois)

    if supermarket_tree is not None:
        dist, _ = supermarket_tree.query(building_coords, k=1)
        df["dist_nearest_supermarket_m"] = [round(d, 1) for d in dist]
    else:
        log.warning("No supermarket POIs found — dist_nearest_supermarket_m will be empty")
        df["dist_nearest_supermarket_m"] = None

    if shop_tree is not None:
        dist, _ = shop_tree.query(building_coords, k=1)
        df["dist_nearest_shop_m"] = [round(d, 1) for d in dist]
    else:
        df["dist_nearest_shop_m"] = None

    if competitor_tree is not None:
        counts = competitor_tree.query_ball_point(building_coords, r=COMPETITOR_RADIUS_M, return_length=True)
        df["competitor_vending_300m"] = counts
    else:
        df["competitor_vending_300m"] = 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    log.info("Saved -> %s", output_path)

    print("\n=== dist_nearest_supermarket_m ===")
    print(df["dist_nearest_supermarket_m"].describe().to_string())
    print("\n=== competitor_vending_300m ===")
    print(df["competitor_vending_300m"].value_counts().sort_index().to_string())


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    base = Path(__file__).resolve().parents[2] / "data" / "processed"
    parser.add_argument("--buildings", type=Path, default=base / "dubai_buildings_validated.csv")
    parser.add_argument("--pois", type=Path, default=base / "dubai_pois.json")
    parser.add_argument("--output", type=Path, default=base / "dubai_buildings_with_distances.csv")
    args = parser.parse_args()

    run(args.buildings, args.pois, args.output)


if __name__ == "__main__":
    main()
