"""
extract_from_pbf.py

Extracts residential building data from a local .osm.pbf file using
pyosmium — no network calls, no rate limits, no timeouts.

Why this exists alongside fetch_buildings.py:
The public Overpass API mirrors are rate-limited and frequently overloaded,
which makes a full-Dubai collection slow and flaky (see project history —
HTTP 429s and 504s even at 2 workers). Overpass is designed for small,
occasional queries, not bulk collection of an entire city.

The alternative: download a regional OSM extract once (a plain file, no
API involved) and process it locally as many times as you want, instantly,
for free. Geofabrik publishes a daily GCC-states extract that covers the
whole UAE (and neighboring Gulf countries):

    https://download.geofabrik.de/asia/gcc-states-latest.osm.pbf  (~240 MB)

Setup:
    pip install osmium
    curl -o data/raw/gcc-states-latest.osm.pbf \
        https://download.geofabrik.de/asia/gcc-states-latest.osm.pbf

Usage:
    python extract_from_pbf.py --pbf data/raw/gcc-states-latest.osm.pbf

Output format matches fetch_buildings.py's merged output exactly
(data/processed/dubai_buildings_raw.json), so validate_buildings.py works
unchanged regardless of which collection method you used.

Note on the "center" field:
Real Overpass API returns a proper polygon centroid for `out center`. Here
we approximate it as the plain average of the way's node coordinates. For
convex, roughly-rectangular residential buildings, this is a good enough
approximation for a distance-to-nearest-supermarket feature — it's off by
at most a few meters for realistic building shapes.
"""

import argparse
import json
import logging
import math
from pathlib import Path

import osmium

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("extract_from_pbf")

RESIDENTIAL_BUILDING_TYPES = {"apartments", "residential"}

# Same default bbox as fetch_buildings.py — keeps output comparable/
# swappable between the two collection methods.
DUBAI_BBOX = (24.75, 54.85, 25.35, 55.55)  # (min_lat, min_lon, max_lat, max_lon)


EARTH_RADIUS_M = 6_371_000


def polygon_area_sqm(lats, lons):
    """Approximate footprint area in square meters using the shoelace
    formula on an equirectangular projection centered at the polygon's
    mean latitude. This local flat-earth approximation is accurate to
    well under 1% error for building-sized polygons (tens of meters
    across) — nowhere near enough distance for Earth's curvature to
    matter at this scale."""
    if len(lats) < 3:
        return 0.0
    lat0 = sum(lats) / len(lats)
    cos_lat0 = math.cos(math.radians(lat0))

    xs = [math.radians(lon) * cos_lat0 * EARTH_RADIUS_M for lon in lons]
    ys = [math.radians(lat) * EARTH_RADIUS_M for lat in lats]

    area = 0.0
    n = len(xs)
    for i in range(n):
        j = (i + 1) % n
        area += xs[i] * ys[j] - xs[j] * ys[i]
    return abs(area) / 2.0


class BuildingHandler(osmium.SimpleHandler):
    def __init__(self, bbox):
        super().__init__()
        self.bbox = bbox
        self.buildings = []
        self.skipped_open_way = 0
        self.skipped_no_location = 0
        self.skipped_out_of_bbox = 0

    def way(self, w):
        building_type = w.tags.get("building")
        if building_type not in RESIDENTIAL_BUILDING_TYPES:
            return

        if not w.is_closed():
            self.skipped_open_way += 1
            return

        lats, lons = [], []
        for n in w.nodes:
            if n.location.valid():
                lats.append(n.location.lat)
                lons.append(n.location.lon)

        if not lats:
            self.skipped_no_location += 1
            return

        center_lat = sum(lats) / len(lats)
        center_lon = sum(lons) / len(lons)

        min_lat, min_lon, max_lat, max_lon = self.bbox
        if not (min_lat <= center_lat <= max_lat and min_lon <= center_lon <= max_lon):
            self.skipped_out_of_bbox += 1
            return

        # Drop the duplicated closing node (first == last in a closed way)
        # before computing area, otherwise the shoelace sum degenerates.
        ring_lats = lats[:-1] if len(lats) > 1 and lats[0] == lats[-1] and lons[0] == lons[-1] else lats
        ring_lons = lons[:-1] if len(lats) > 1 and lats[0] == lats[-1] and lons[0] == lons[-1] else lons
        footprint_sqm = polygon_area_sqm(ring_lats, ring_lons)

        # Match fetch_buildings.py's element shape, plus one extra field
        # (footprint_sqm) that the Overpass path doesn't provide — Overpass
        # elements simply won't have this key, and validate_buildings.py
        # treats it as optional.
        self.buildings.append({
            "type": "way",
            "id": w.id,
            "center": {"lat": center_lat, "lon": center_lon},
            "footprint_sqm": round(footprint_sqm, 1),
            "tags": dict(w.tags),
        })


def run(pbf_path: Path, bbox, output_path: Path):
    if not pbf_path.exists():
        raise SystemExit(
            f"PBF file not found: {pbf_path}\n"
            f"Download it first, e.g.:\n"
            f"  curl -o {pbf_path} https://download.geofabrik.de/asia/gcc-states-latest.osm.pbf"
        )

    log.info("Reading %s ...", pbf_path)
    handler = BuildingHandler(bbox)
    # locations=True resolves node coordinates for each way as it streams
    # through the file — required to compute a center point.
    handler.apply_file(str(pbf_path), locations=True)

    log.info(
        "Found %d residential buildings in bbox. Skipped: %d (not closed way), "
        "%d (missing node locations), %d (outside bbox)",
        len(handler.buildings), handler.skipped_open_way,
        handler.skipped_no_location, handler.skipped_out_of_bbox,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(handler.buildings, ensure_ascii=False))
    log.info("Saved -> %s", output_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    default_out = Path(__file__).resolve().parents[2] / "data" / "processed" / "dubai_buildings_raw.json"
    parser.add_argument("--pbf", type=Path, required=True, help="Path to a local .osm.pbf file (e.g. gcc-states-latest.osm.pbf)")
    parser.add_argument(
        "--bbox", nargs=4, type=float, metavar=("MIN_LAT", "MIN_LON", "MAX_LAT", "MAX_LON"),
        default=DUBAI_BBOX, help="Only keep buildings whose center falls in this bbox (default: Dubai emirate)",
    )
    parser.add_argument("--output", type=Path, default=default_out)
    args = parser.parse_args()

    run(args.pbf, tuple(args.bbox), args.output)


if __name__ == "__main__":
    main()
