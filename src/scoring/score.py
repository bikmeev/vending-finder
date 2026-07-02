"""
score.py

Combines the features computed so far (estimated apartment count, distance
to nearest supermarket, competitor density) into a single vending-machine
placement score per building, and outputs a ranked list.

Scoring logic:
- More estimated residents -> more potential customers -> higher score.
- Farther from a supermarket -> residents have less convenient access to a
  full grocery run -> more likely to rely on a lobby vending machine for
  snacks/drinks -> higher score. (Note: an earlier version of this
  methodology, back when the project was just plans on paper, mistakenly
  used *inverse* distance here — rewarding buildings *close* to a
  supermarket. That contradicted the stated rationale and has been
  corrected here: distance is rewarded directly, not inverted.)
- More existing vending machines nearby -> more competition for the same
  captive audience -> lower score.

Buildings with no apartment-count estimate at all (confidence="low",
reason="no_flats_no_levels" — the 58% of Dubai with no size tags) are
excluded from ranking rather than guessed at with a fabricated default;
they're kept in the output CSV with score=NaN and flagged, so nothing is
silently dropped.

Usage:
    python score.py
    python score.py --top 50
    python score.py --w-population 0.5 --w-distance 0.35 --w-competition 0.15
"""

import argparse
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("score")

# Distances beyond this are clipped before normalizing. Anything past ~2km
# from the nearest supermarket in an urban area like Dubai is very likely a
# bbox-edge/desert-fringe data artifact rather than a meaningful "captive
# market" signal, and would otherwise dominate the normalization range.
MAX_USEFUL_DISTANCE_M = 2000


def minmax_norm(series: pd.Series) -> pd.Series:
    lo, hi = series.min(), series.max()
    if hi - lo < 1e-9:
        return pd.Series(0.5, index=series.index)  # no variation -> neutral
    return (series - lo) / (hi - lo)


def run(input_path: Path, output_path: Path, top_n: int, w_population: float, w_distance: float, w_competition: float):
    df = pd.read_csv(input_path)
    log.info("Loaded %d buildings from %s", len(df), input_path)

    has_estimate = df["estimated_flats"].notna()
    n_scoreable = has_estimate.sum()
    log.info(
        "%d/%d buildings (%.0f%%) have an apartment estimate and will be scored; "
        "the rest are kept in output with score=NaN.",
        n_scoreable, len(df), 100 * n_scoreable / len(df),
    )

    scoreable = df[has_estimate].copy()

    # Population component: log1p compresses the huge range between a
    # 4-unit low-rise and a 700-unit tower so the tower doesn't completely
    # dominate the ranking on size alone.
    population_component = minmax_norm(np.log1p(scoreable["estimated_flats"]))

    # Distance component: clipped, then rewarded directly (farther = higher
    # score) per the corrected rationale above.
    clipped_dist = scoreable["dist_nearest_supermarket_m"].clip(upper=MAX_USEFUL_DISTANCE_M)
    distance_component = minmax_norm(clipped_dist)

    # Competition component: more nearby vending machines -> penalty.
    competition_component = minmax_norm(scoreable["competitor_vending_300m"])

    score = (
        w_population * population_component
        + w_distance * distance_component
        - w_competition * competition_component
    )
    # Renormalize to a clean 0-100 scale for readability.
    scoreable["score"] = minmax_norm(score) * 100

    df["score"] = np.nan
    df.loc[scoreable.index, "score"] = scoreable["score"]

    df = df.sort_values("score", ascending=False, na_position="last")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    log.info("Saved -> %s", output_path)

    top = df[df["score"].notna()].head(top_n)
    print(f"\n=== Top {min(top_n, len(top))} buildings ===")
    cols = ["name", "lat", "lon", "estimated_flats", "confidence", "dist_nearest_supermarket_m", "competitor_vending_300m", "score"]
    print(top[cols].to_string(index=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    base = Path(__file__).resolve().parents[2] / "data" / "processed"
    parser.add_argument("--input", type=Path, default=base / "dubai_buildings_with_distances.csv")
    parser.add_argument("--output", type=Path, default=base / "dubai_buildings_scored.csv")
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--w-population", type=float, default=0.5)
    parser.add_argument("--w-distance", type=float, default=0.35)
    parser.add_argument("--w-competition", type=float, default=0.15)
    args = parser.parse_args()

    run(args.input, args.output, args.top, args.w_population, args.w_distance, args.w_competition)


if __name__ == "__main__":
    main()
