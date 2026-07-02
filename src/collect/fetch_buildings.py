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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import requests
from tqdm import tqdm

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
    # overpass.openstreetmap.fr dropped — consistently returns HTTP 403 for
    # this client, not worth the retry time.
]

# Overpass's usage policy asks clients not to run more than ~2 concurrent
# queries per server. We enforce that with one semaphore per mirror,
# regardless of how many worker threads are configured — extra workers just
# let a second mirror be used at the same time, they never overload a
# single instance.
MIRROR_SEMAPHORES = {mirror: threading.Semaphore(2) for mirror in OVERPASS_MIRRORS}

_thread_local = threading.local()


def get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
    return _thread_local.session


USER_AGENT = "vending-finder-portfolio-project/0.1 (github research script)"

QUERY_TEMPLATE = (
    "[out:json][timeout:90];"
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


def fetch_cell(cell: Cell, max_retries=4, sleep_between=2.0):
    """Fetches a single grid cell, rotating across mirrors and retrying with
    exponential backoff when the server reports it's overloaded. The
    semaphore acquire only wraps the request itself — the backoff sleep
    happens outside it, so other threads can use the freed slot while this
    one waits."""
    query = QUERY_TEMPLATE.format(s=cell.south, w=cell.west, n=cell.north, e=cell.east)
    session = get_session()

    for attempt in range(1, max_retries + 1):
        mirror = OVERPASS_MIRRORS[(attempt - 1) % len(OVERPASS_MIRRORS)]
        with MIRROR_SEMAPHORES[mirror]:
            try:
                resp = session.post(
                    mirror,
                    data={"data": query},
                    headers={"User-Agent": USER_AGENT},
                    timeout=150,
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


def fetch_and_save(cell: Cell, cells_dir: Path, max_retries: int, sleep_between: float):
    """Worker task: fetch one cell and write it to its own file. Runs in a
    thread pool — safe because each cell writes to a distinct file."""
    cell_path = cells_dir / f"cell_{cell.index:05d}.json"
    elements = fetch_cell(cell, max_retries=max_retries, sleep_between=sleep_between)
    if elements is None:
        return cell.index, "failed", 0
    cell_path.write_text(json.dumps(elements, ensure_ascii=False))
    return cell.index, "fetched", len(elements)


def run(bbox, grid_size, out_dir: Path, sleep_between: float, limit_cells: int | None,
        workers: int, max_retries: int):
    cells_dir = out_dir / "raw" / "cells"
    cells_dir.mkdir(parents=True, exist_ok=True)

    grid = build_grid(bbox, grid_size)
    if limit_cells:
        grid = grid[:limit_cells]

    pending = [c for c in grid if not (cells_dir / f"cell_{c.index:05d}.json").exists()]
    skipped = len(grid) - len(pending)

    log.info(
        "Bbox %s split into %d cells of size %.3f° (%d already done, %d to fetch, %d workers across %d mirrors)",
        bbox, len(grid), grid_size, skipped, len(pending), workers, len(OVERPASS_MIRRORS),
    )

    fetched, failed, total_buildings = 0, 0, 0

    if pending:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(fetch_and_save, cell, cells_dir, max_retries, sleep_between): cell
                for cell in pending
            }
            with tqdm(total=len(pending), desc="Fetching cells", unit="cell") as pbar:
                for future in as_completed(futures):
                    idx, status, n_buildings = future.result()
                    if status == "fetched":
                        fetched += 1
                        total_buildings += n_buildings
                    else:
                        failed += 1
                    pbar.set_postfix(fetched=fetched, failed=failed, buildings=total_buildings)
                    pbar.update(1)

    log.info(
        "Done. New cells fetched: %d (%d buildings), already present: %d, failed: %d",
        fetched, total_buildings, skipped, failed,
    )
    if failed:
        log.info("Re-run the same command to retry only the %d failed cells.", failed)

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
    parser.add_argument("--sleep", type=float, default=2.0, help="Backoff base (seconds) used between retries on a failed request (default 2.0)")
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parents[2] / "data")
    parser.add_argument("--limit-cells", type=int, default=None, help="Limit number of cells (for a quick test run)")
    parser.add_argument(
        "--workers", type=int, default=4,
        help="Number of concurrent worker threads (default 4). Actual load per Overpass "
             "mirror is still capped at 2 concurrent requests regardless of this value, "
             "per Overpass's usage policy — raising this beyond ~2x the mirror count "
             "mostly just adds idle threads waiting on the semaphore.",
    )
    parser.add_argument("--max-retries", type=int, default=5, help="Retries per cell before giving up (default 5)")
    args = parser.parse_args()

    run(tuple(args.bbox), args.grid_size, args.out_dir, args.sleep, args.limit_cells, args.workers, args.max_retries)


if __name__ == "__main__":
    sys.exit(main())
