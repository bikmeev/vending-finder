"""
validate_buildings.py

Validates and cleans the raw building data collected by fetch_buildings.py.

Rules are based on real findings from manually inspecting Dubai Marina:
- building:flats is rarely present (~13% of buildings), but when it is,
  it's the most trustworthy source for apartment count.
- building:levels is present more often (~67%), but is sometimes wrong
  (example: "Marina Pinnacle" — levels=4 while the real building has ~55
  floors and 748 apartments).
- height is the best cross-validator: for Dubai's residential high-rises,
  height per level consistently falls in the ~2.5-6.0 m range. If
  height/levels falls outside that range, levels is most likely broken.
- flats_per_level for valid Marina high-rises falls in the ~1.5-12 range.
  Values like 100+ (as seen in the corrupted records) are a clear sign of
  bad data.

Output: one CSV row per building, with a `confidence` field
(high/suspect/low) and a best-effort `estimated_flats` value.

Usage:
    python validate_buildings.py
    python validate_buildings.py --input ../../data/processed/dubai_buildings_raw.json
"""

import argparse
import json
import logging
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("validate_buildings")

# Ranges calibrated by manual inspection of Dubai Marina (see docstring
# above). This is a starting point, not a final truth — once data from
# other districts (mid-rise, low-rise) is added, it's worth recalibrating
# per building-class instead of relying on one global coefficient.
VALID_HEIGHT_PER_LEVEL = (2.5, 6.0)
VALID_FLATS_PER_LEVEL = (1, 20)
DEFAULT_FLATS_PER_LEVEL_FALLBACK = 6.0  # median across clean Marina high-rises


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_one(tags: dict) -> dict:
    flats = safe_float(tags.get("building:flats"))
    levels = safe_float(tags.get("building:levels"))
    height = safe_float(tags.get("height"))

    result = {
        "name": tags.get("name"),
        "building_type": tags.get("building"),
        "raw_flats": flats,
        "raw_levels": levels,
        "raw_height": height,
        "confidence": None,
        "reason": None,
        "estimated_flats": None,
    }

    if flats is None and levels is None:
        result["confidence"] = "low"
        result["reason"] = "no_flats_no_levels"
        return result

    # Cross-validate levels via height, if both are present
    levels_trustworthy = True
    if levels and height:
        m_per_level = height / levels
        if not (VALID_HEIGHT_PER_LEVEL[0] <= m_per_level <= VALID_HEIGHT_PER_LEVEL[1]):
            levels_trustworthy = False

    if flats and levels:
        flats_per_level = flats / levels
        plausible_ratio = VALID_FLATS_PER_LEVEL[0] <= flats_per_level <= VALID_FLATS_PER_LEVEL[1]

        if levels_trustworthy and plausible_ratio:
            result["confidence"] = "high"
            result["reason"] = "flats_and_levels_consistent"
            result["estimated_flats"] = flats
        elif flats and not plausible_ratio:
            # flats_per_level out of range -> levels is most likely wrong,
            # while flats tends to be more verifiable (matches actual unit
            # sales/registrations)
            result["confidence"] = "suspect"
            result["reason"] = f"implausible_flats_per_level={flats_per_level:.1f} (levels likely wrong)"
            result["estimated_flats"] = flats  # trust flats over levels
        else:
            result["confidence"] = "suspect"
            result["reason"] = "height_levels_mismatch"
            result["estimated_flats"] = flats
        return result

    if flats and not levels:
        result["confidence"] = "high"
        result["reason"] = "flats_only"
        result["estimated_flats"] = flats
        return result

    if levels and not flats:
        if not levels_trustworthy:
            result["confidence"] = "suspect"
            result["reason"] = "levels_only_height_mismatch"
        else:
            result["confidence"] = "low"
            result["reason"] = "levels_only_estimated"
        result["estimated_flats"] = round(levels * DEFAULT_FLATS_PER_LEVEL_FALLBACK)
        return result

    return result


def run(input_path: Path, output_path: Path):
    elements = json.loads(input_path.read_text())
    log.info("Loaded %d buildings from %s", len(elements), input_path)

    rows = []
    for el in elements:
        tags = el.get("tags", {})
        center = el.get("center", {})
        row = {
            "osm_id": el.get("id"),
            "lat": center.get("lat"),
            "lon": center.get("lon"),
        }
        row.update(validate_one(tags))
        rows.append(row)

    df = pd.DataFrame(rows)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    log.info("Saved -> %s", output_path)

    print("\n=== Confidence summary ===")
    print(df["confidence"].value_counts(dropna=False).to_string())

    print("\n=== Reason summary ===")
    print(df["reason"].value_counts(dropna=False).to_string())

    total = len(df)
    with_estimate = df["estimated_flats"].notna().sum()
    print(f"\nTotal buildings: {total}")
    print(f"With an apartment-count estimate (any confidence): {with_estimate} ({with_estimate/total:.0%})")
    print(f"High confidence (confidence=high): {(df['confidence']=='high').sum()} ({(df['confidence']=='high').mean():.0%})")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    default_input = Path(__file__).resolve().parents[2] / "data" / "processed" / "dubai_buildings_raw.json"
    default_output = Path(__file__).resolve().parents[2] / "data" / "processed" / "dubai_buildings_validated.csv"
    parser.add_argument("--input", type=Path, default=default_input)
    parser.add_argument("--output", type=Path, default=default_output)
    args = parser.parse_args()

    run(args.input, args.output)


if __name__ == "__main__":
    main()
