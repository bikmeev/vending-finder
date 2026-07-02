"""
validate_buildings.py

Validates and cleans raw building data collected by fetch_buildings.py or
extract_from_pbf.py, and produces an apartment-count estimate for as many
buildings as possible.

Tag-based rules are based on real findings from manually inspecting Dubai
Marina:
- building:flats is rarely present, but when it is, it's the most
  trustworthy source for apartment count.
- building:levels is present more often, but is sometimes wrong (example:
  "Marina Pinnacle" — levels=4 while the real building has ~55 floors and
  748 apartments).
- height is a cross-validator: for Dubai's residential high-rises, height
  per level consistently falls in the ~2.5-6.0 m range. If height/levels
  falls outside that range, levels is most likely broken.
- flats_per_level for valid Marina high-rises falls in the ~1.5-12 range.
  Values like 100+ (as seen in corrupted records) are a clear sign of bad
  data.

IMPORTANT — Marina is not representative of Dubai as a whole:
Running this on the full city (11,395 buildings) instead of just Marina's
142 named towers showed only 0.2% of buildings have consistent
flats+levels tags, and 58% have neither tag at all. Marina towers are a
showcase district that enthusiastic OSM contributors tagged carefully
(most even have a wikidata link) — that is not the norm.

To cope with that, this script adds a second, tag-independent signal:
building footprint area (in m², computed from the polygon geometry by
extract_from_pbf.py — present for virtually every building, regardless of
tagging quality). We calibrate an "average sqm per apartment" figure from
the small set of buildings where both footprint area AND a trustworthy
flats count are known, then apply that ratio to any building that has a
footprint area and a level count, even when building:flats is missing.
This meaningfully increases coverage beyond the tag-only approach.

Output: one CSV row per building, with a `confidence` field
(high/medium/suspect/low) and a best-effort `estimated_flats` value.

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
# above). These hold up fine as sanity-check bounds for high-rises
# generally; they're intentionally wide, not a precision instrument.
VALID_HEIGHT_PER_LEVEL = (2.5, 6.0)
VALID_FLATS_PER_LEVEL = (1, 20)
DEFAULT_FLATS_PER_LEVEL_FALLBACK = 6.0  # used only if area-based calibration is unavailable
MIN_CALIBRATION_SAMPLES = 5  # don't trust an area calibration built from fewer buildings than this


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


def calibrate_sqm_per_flat(df: pd.DataFrame) -> float | None:
    """Derives an 'average sqm of footprint per apartment' figure from
    buildings where we trust both the flats count AND have a footprint
    area. total_floor_area = footprint * levels approximates gross floor
    area across all floors; dividing by flats gives sqm/flat. We use the
    median across the calibration set to resist outliers."""
    calib = df[
        (df["confidence"] == "high")
        & df["footprint_sqm"].notna() & (df["footprint_sqm"] > 0)
        & df["raw_levels"].notna() & (df["raw_levels"] > 0)
        & df["raw_flats"].notna() & (df["raw_flats"] > 0)
    ].copy()

    if len(calib) < MIN_CALIBRATION_SAMPLES:
        log.warning(
            "Only %d buildings available for area calibration (need >= %d) — "
            "falling back to the fixed flats_per_level coefficient instead.",
            len(calib), MIN_CALIBRATION_SAMPLES,
        )
        return None

    calib["total_floor_area"] = calib["footprint_sqm"] * calib["raw_levels"]
    calib["sqm_per_flat"] = calib["total_floor_area"] / calib["raw_flats"]
    ratio = calib["sqm_per_flat"].median()
    log.info(
        "Calibrated area-based estimate from %d buildings: median %.1f sqm of "
        "gross floor area per apartment (range %.1f-%.1f)",
        len(calib), ratio, calib["sqm_per_flat"].min(), calib["sqm_per_flat"].max(),
    )
    return ratio


def apply_area_estimate(row, sqm_per_flat: float | None):
    """For buildings where the tag-only pass could only guess via the
    fixed flats_per_level coefficient (or flagged a levels/height
    mismatch), prefer a footprint-area-based estimate when possible — it's
    specific to that building's actual size instead of a city-wide
    average."""
    if row["reason"] not in ("levels_only_estimated", "levels_only_height_mismatch"):
        return row["estimated_flats"], row["confidence"], row["reason"]

    if sqm_per_flat is None or pd.isna(row["footprint_sqm"]) or row["footprint_sqm"] <= 0:
        return row["estimated_flats"], row["confidence"], row["reason"]

    total_floor_area = row["footprint_sqm"] * row["raw_levels"]
    area_estimate = round(total_floor_area / sqm_per_flat)
    # "medium" sits between "high" (tag-verified) and "low"/"suspect"
    # (single unverified tag or fixed-coefficient guess) — it's a
    # per-building calibrated estimate, but still unverified against any
    # ground truth for this specific building.
    return area_estimate, "medium", "levels_and_footprint_area_calibrated"


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
            "footprint_sqm": el.get("footprint_sqm"),  # absent (NaN) for Overpass-collected data
        }
        row.update(validate_one(tags))
        rows.append(row)

    df = pd.DataFrame(rows)

    # Second pass: for buildings where the tag-only estimate used the fixed
    # global flats_per_level coefficient, try to replace it with a
    # per-building estimate calibrated from footprint area instead.
    sqm_per_flat = calibrate_sqm_per_flat(df)
    if sqm_per_flat is not None:
        updated = df.apply(lambda r: apply_area_estimate(r, sqm_per_flat), axis=1, result_type="expand")
        updated.columns = ["estimated_flats", "confidence", "reason"]
        n_upgraded = (df["reason"] != updated["reason"]).sum()
        df[["estimated_flats", "confidence", "reason"]] = updated
        log.info("Upgraded %d buildings from fixed-coefficient to area-calibrated estimates", n_upgraded)

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
    print(f"Medium confidence (area-calibrated): {(df['confidence']=='medium').sum()} ({(df['confidence']=='medium').mean():.0%})")


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
