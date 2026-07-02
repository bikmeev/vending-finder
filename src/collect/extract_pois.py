"""
extract_pois.py

Extracts points of interest relevant to the vending-machine placement
score from a local .osm.pbf file: supermarkets, convenience stores, and
existing vending machines (direct competitors). Same offline approach as
extract_from_pbf.py — no network calls, no rate limits.

POIs in OSM can be mapped as either a single node (a point) or a way (a
building outline tagged directly with the shop, common for large
supermarkets/hypermarkets). This handles both, using the same node-average
center approximation as extract_from_pbf.py for ways.

Usage:
    python extract_pois.py --pbf data/raw/gcc-states-latest.osm.pbf
"""

import argparse
import json
import logging
from pathlib import Path

import osmium

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("extract_pois")

DUBAI_BBOX = (24.75, 54.85, 25.35, 55.55)  # (min_lat, min_lon, max_lat, max_lon)

# Tag -> category mapping. A POI is classified by the first matching rule.
# shop=supermarket/greengrocer are "real" grocery trips; convenience stores
# are smaller but still a walk-instead-of-vending-machine alternative;
# vending_machine is a direct competitor.
CATEGORY_RULES = [
    ("supermarket", "shop", {"supermarket", "greengrocer"}),
    ("convenience", "shop", {"convenience"}),
    ("competitor_vending", "amenity", {"vending_machine"}),
    ("competitor_vending", "shop", {"vending_machine"}),
]


def classify(tags: dict):
    for category, key, values in CATEGORY_RULES:
        if tags.get(key) in values:
            return category
    return None


class PoiHandler(osmium.SimpleHandler):
    def __init__(self, bbox):
        super().__init__()
        self.bbox = bbox
        self.pois = []

    def _in_bbox(self, lat, lon):
        min_lat, min_lon, max_lat, max_lon = self.bbox
        return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon

    def _add(self, osm_type, osm_id, lat, lon, category, tags):
        if not self._in_bbox(lat, lon):
            return
        self.pois.append({
            "type": osm_type,
            "id": osm_id,
            "lat": lat,
            "lon": lon,
            "category": category,
            "name": tags.get("name"),
        })

    def node(self, n):
        category = classify(n.tags)
        if category is None:
            return
        if not n.location.valid():
            return
        self._add("node", n.id, n.location.lat, n.location.lon, category, n.tags)

    def way(self, w):
        category = classify(w.tags)
        if category is None:
            return
        lats, lons = [], []
        for node in w.nodes:
            if node.location.valid():
                lats.append(node.location.lat)
                lons.append(node.location.lon)
        if not lats:
            return
        self._add("way", w.id, sum(lats) / len(lats), sum(lons) / len(lons), category, w.tags)


def run(pbf_path: Path, bbox, output_path: Path):
    if not pbf_path.exists():
        raise SystemExit(f"PBF file not found: {pbf_path}")

    log.info("Reading %s ...", pbf_path)
    handler = PoiHandler(bbox)
    handler.apply_file(str(pbf_path), locations=True)

    by_category = {}
    for poi in handler.pois:
        by_category[poi["category"]] = by_category.get(poi["category"], 0) + 1

    log.info("Found %d POIs in bbox: %s", len(handler.pois), by_category)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(handler.pois, ensure_ascii=False))
    log.info("Saved -> %s", output_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    default_out = Path(__file__).resolve().parents[2] / "data" / "processed" / "dubai_pois.json"
    parser.add_argument("--pbf", type=Path, required=True)
    parser.add_argument(
        "--bbox", nargs=4, type=float, metavar=("MIN_LAT", "MIN_LON", "MAX_LAT", "MAX_LON"),
        default=DUBAI_BBOX,
    )
    parser.add_argument("--output", type=Path, default=default_out)
    args = parser.parse_args()

    run(args.pbf, tuple(args.bbox), args.output)


if __name__ == "__main__":
    main()
