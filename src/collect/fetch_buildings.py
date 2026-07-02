"""
fetch_buildings.py

Collects data about residential buildings (building=apartments|residential)
across the entire Dubai emirate via the Overpass API.

Why a grid instead of one big query:
Dubai as a whole contains tens/hundreds of thousands of buildings. A single
query for the whole emirate almost certainly hits the public Overpass
server's timeout ("The server is probably too busy to handle your
request"). So the territory is split into small cells (0.03 degrees by
default, roughly 3x3 km) and each cell is queried separately.

Why resumability:
Public Overpass instances are unreliable (rate limits, overload). If the
script dies on cell 150 of 300, we don't want to start from scratch. Each
cell is saved to its own file, and on re-run, cells that already have a
file are skipped.

Usage:
    python fetch_buildings.py
    python fetch_buildings.py --grid-size 0.02 --sleep 3
    python fetch_buildings.py --bbox 25.0 55.1 25.2 55.3   # test on a sub-area

After all cells are collected, the script merges them into a single file:
    data/processed/dubai_buildings_raw.json
"""

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("fetch_buildings")

# Approximate bounding box of the main built-up area of Dubai emirate.
# Excludes the remote Hatta exclave (~24.8, 56.13) — it's geographically
# isolated and not relevant for dense urban residential-lobby vending.
DUBAI_BBOX = (24.75, 54.85, 25.35, 55.55)  # (min_lat, min_lon, max_lat, max_lon)

# Several public Overpass mirrors. Order matters: the first is primary, the
# rest are fallbacks on overload/error. The list of live public instances
# changes over time — see
# https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
]

USER_AGENT = "vending-finder-portfolio-project/0.1 (github research script)"

QUERY_TEMPLATE = (
    "[out:json][timeout:60];"
    'way["building"~"apartments|residential"]({s},{w},{n},{e});'
    "out tags center;"
)


@dataclass
class Cell:
    index: int
    south: float
    west: float
    north: float
    east: float


def build_grid(bbox, grid_size):
    min_lat, min_lon, max_lat, max_lon = bbox
    cells = []
    idx = 0
    lat = min_lat
    while lat < max_lat:
        lon = min_lon
        next_lat = round(min(lat + grid_size, max_lat), 6)
        while lon < max_lon:
            next_lon = round(min(lon + grid_size, max_lon), 6)
            cells.append(Cell(idx, lat, lon, next_lat, next_lon))
            idx += 1
            lon = next_lon
        lat = next_lat
    return cells


def fetch_cell(cell: Cell, session: requests.Session, max_retries=4, sleep_between=2.0):
    """Fetches a single grid cell, rotating across mirrors and retrying with
    exponential backoff when the server reports it's overloaded."""
    query = QUERY_TEMPLATE.format(s=cell.south, w=cell.west, n=cell.north, e=cell.east)

    for attempt in range(1, max_retries + 1):
        mirror = OVERPASS_MIRRORS[(attempt - 1) % len(OVERPASS_MIRRORS)]
        try:
            resp = session.post(
                mirror,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=90,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("elements", [])
            else:
                log.warning(
                    "Cell %d: HTTP %d from %s (attempt %d/%d)",
                    cell.index, resp.status_code, mirror, attempt, max_retries,
                )
        except (requests.RequestException, json.JSONDecodeError) as e:
            log.warning(
                "Cell %d: request error to %s (attempt %d/%d): %s",
                cell.index, mirror, attempt, max_retries, e,
            )

        backoff = sleep_between * (2 ** (attempt - 1))
        time.sleep(backoff)

    log.error("Cell %d: failed after %d attempts, skipping", cell.index, max_retries)
    return None  # None (failed) is distinct from [] (valid, empty cell)


def run(bbox, grid_size, out_dir: Path, sleep_between: float, limit_cells: int | None):
    cells_dir = out_dir / "raw" / "cells"
    cells_dir.mkdir(parents=True, exist_ok=True)

    grid = build_grid(bbox, grid_size)
    if limit_cells:
        grid = grid[:limit_cells]

    log.info("Bbox %s split into %d cells of size %.3f°", bbox, len(grid), grid_size)

    session = requests.Session()
    fetched, skipped, failed = 0, 0, 0

    for cell in grid:
        cell_path = cells_dir / f"cell_{cell.index:05d}.json"
        if cell_path.exists():
            skipped += 1
            continue

        elements = fetch_cell(cell, session, sleep_between=sleep_between)
        if elements is None:
            failed += 1
            continue

        cell_path.write_text(json.dumps(elements, ensure_ascii=False))
        fetched += 1
        log.info(
            "Cell %d/%d: %d buildings saved (%s)",
            cell.index + 1, len(grid), len(elements), cell_path.name,
        )
        time.sleep(sleep_between)  # be a good citizen on the public server

    log.info(
        "Done. New cells: %d, already present (skipped): %d, failed: %d",
        fetched, skipped, failed,
    )

    merge_cells(cells_dir, out_dir / "processed" / "dubai_buildings_raw.json")


def merge_cells(cells_dir: Path, output_path: Path):
    """Merges all cells into a single file, deduplicating buildings by OSM
    id (a building on a cell boundary can be returned by more than one
    cell's query)."""
    seen_ids = set()
    merged = []

    cell_files = sorted(cells_dir.glob("cell_*.json"))
    for cell_file in cell_files:
        try:
            elements = json.loads(cell_file.read_text())
        except json.JSONDecodeError:
            log.warning("Corrupted cell file, skipping: %s", cell_file)
            continue
        for el in elements:
            osm_id = el.get("id")
            if osm_id in seen_ids:
                continue
            seen_ids.add(osm_id)
            merged.append(el)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(merged, ensure_ascii=False, indent=None))
    log.info(
        "Merged %d cell files -> %d unique buildings -> %s",
        len(cell_files), len(merged), output_path,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--bbox", nargs=4, type=float, metavar=("MIN_LAT", "MIN_LON", "MAX_LAT", "MAX_LON"),
        default=DUBAI_BBOX, help="Bounding box to collect (default: the whole Dubai emirate)",
    )
    parser.add_argument("--grid-size", type=float, default=0.03, help="Grid cell size in degrees (default 0.03 ~ 3km)")
    parser.add_argument("--sleep", type=float, default=2.0, help="Pause between requests in seconds (default 2.0)")
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parents[2] / "data")
    parser.add_argument("--limit-cells", type=int, default=None, help="Limit number of cells (for a quick test run)")
    args = parser.parse_args()

    run(tuple(args.bbox), args.grid_size, args.out_dir, args.sleep, args.limit_cells)


if __name__ == "__main__":
    sys.exit(main())
