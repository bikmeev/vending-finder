"""
iterative_verify.py

Runs Google grocery verification in multiple rounds instead of once,
recomputing the score in between each round.

Why this is necessary (not just nice-to-have):
Correcting the OSM-based distance for the initial top-50 can demote several
of them once we learn their "far from any supermarket" premise was simply
wrong (OSM didn't know about a nearby shop). That means previously-unranked
buildings will rise into the new top-50 — and we haven't checked THEM
against Google yet, so we don't know if their OSM distance is trustworthy
either. A single verification pass only cleans up the buildings we happened
to check; it doesn't tell you whether the newly-promoted buildings are
reliable. Running several rounds — verify, recompute, re-rank, verify the
newly-promoted ones — converges toward a ranking that's actually been
checked, not just the ranking that was checked once and never revisited.

Each round is a fresh batch (buildings already verified in an earlier
round are skipped), so N rounds of --per-round K costs N*K Google requests
total — e.g. the default (3 rounds x 50) is 150 requests, still
comfortably inside the Essentials-tier 10,000/month free allowance (see
verify_groceries_google.py's docstring for the billing details).

Usage:
    export GOOGLE_MAPS_API_KEY="your-key-here"
    python iterative_verify.py
    python iterative_verify.py --rounds 3 --per-round 50
"""

import argparse
import logging
import math
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from verify_groceries_google import query_nearby_groceries, haversine_m

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("iterative_verify")

MAX_USEFUL_DISTANCE_M = 2000  # matches score.py's default
W_POPULATION = 0.6
W_DISTANCE = 0.4


def minmax_norm(series: pd.Series) -> pd.Series:
    lo, hi = series.min(), series.max()
    if hi - lo < 1e-9:
        return pd.Series(0.5, index=series.index)
    return (series - lo) / (hi - lo)


def recompute_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Same formula as score.py, but driven by effective_dist_m (Google
    where we have it, OSM otherwise) instead of the OSM-only distance."""
    df = df.copy()
    has_estimate = df["estimated_flats"].notna() & df["effective_dist_m"].notna()
    scoreable = df[has_estimate].copy()

    pop_component = minmax_norm(np.log1p(scoreable["estimated_flats"]))
    clipped_dist = scoreable["effective_dist_m"].clip(upper=MAX_USEFUL_DISTANCE_M)
    dist_component = minmax_norm(clipped_dist)

    raw = W_POPULATION * pop_component + W_DISTANCE * dist_component
    scoreable["score"] = minmax_norm(raw) * 100

    df["score"] = np.nan
    df.loc[scoreable.index, "score"] = scoreable["score"]
    return df


def run(scored_path: Path, output_scored_path: Path, output_verified_path: Path,
        rounds: int, per_round: int, radius_m: float, api_key: str, sleep_between: float):
    df = pd.read_csv(scored_path)
    df["effective_dist_m"] = df["dist_nearest_supermarket_m"]
    df = recompute_scores(df)  # baseline, identical to score.py's original ranking

    verified_ids = set()
    verified_rows = []
    session = requests.Session()

    for round_num in range(1, rounds + 1):
        candidates = df[df["score"].notna() & ~df["osm_id"].isin(verified_ids)].sort_values("score", ascending=False)
        batch = candidates.head(per_round)
        if batch.empty:
            log.info("Round %d: no more unverified candidates, stopping early.", round_num)
            break

        log.info(
            "Round %d/%d: verifying %d buildings (top of current ranking, excluding %d already checked)",
            round_num, rounds, len(batch), len(verified_ids),
        )

        for _, row in batch.iterrows():
            places = query_nearby_groceries(row["lat"], row["lon"], radius_m, api_key, session)
            if places:
                nearest = places[0]
                loc = nearest["location"]
                dist = haversine_m(row["lat"], row["lon"], loc["latitude"], loc["longitude"])
                verified_rows.append({
                    "osm_id": row["osm_id"],
                    "name": row.get("name"),
                    "round_verified": round_num,
                    "dist_nearest_supermarket_m": row["dist_nearest_supermarket_m"],
                    "dist_nearest_grocery_google_m": round(dist, 1),
                    "nearest_grocery_google_name": nearest.get("displayName", {}).get("text"),
                    "nearest_grocery_google_lat": loc["latitude"],
                    "nearest_grocery_google_lon": loc["longitude"],
                    "osm_coverage_gap_m": round(row["dist_nearest_supermarket_m"] - dist, 1),
                })
            verified_ids.add(row["osm_id"])
            time.sleep(sleep_between)

        # Fold this round's results into effective_dist_m and re-rank before
        # picking next round's batch.
        verified_df = pd.DataFrame(verified_rows)
        if not verified_df.empty:
            dist_map = verified_df.set_index("osm_id")["dist_nearest_grocery_google_m"].to_dict()
            df["effective_dist_m"] = df.apply(
                lambda r: dist_map.get(r["osm_id"], r["effective_dist_m"]), axis=1
            )
        df = recompute_scores(df)

        n_this_round_found = sum(1 for v in verified_rows if v["round_verified"] == round_num)
        log.info("Round %d done: %d/%d buildings had a grocery found nearby.", round_num, n_this_round_found, len(batch))

    verified_df = pd.DataFrame(verified_rows)
    output_verified_path.parent.mkdir(parents=True, exist_ok=True)
    verified_df.to_csv(output_verified_path, index=False)
    log.info("Saved %d cumulative verification results -> %s", len(verified_df), output_verified_path)

    df = df.sort_values("score", ascending=False, na_position="last")
    df.to_csv(output_scored_path, index=False)
    log.info("Saved corrected scored table -> %s", output_scored_path)

    top20 = df[df["score"].notna()].head(20)
    print("\n=== Final top 20 after %d verification rounds ===" % rounds)
    cols = ["name", "score", "estimated_flats", "effective_dist_m", "dist_nearest_supermarket_m"]
    print(top20[cols].to_string(index=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    base = Path(__file__).resolve().parents[2] / "data" / "processed"
    parser.add_argument("--scored", type=Path, default=base / "dubai_buildings_scored.csv")
    parser.add_argument("--output-scored", type=Path, default=base / "dubai_buildings_scored.csv")
    parser.add_argument("--output-verified", type=Path, default=base / "dubai_top_buildings_google_verified.csv")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--per-round", type=int, default=50)
    parser.add_argument("--radius", type=float, default=1000.0)
    parser.add_argument("--sleep", type=float, default=0.1)
    parser.add_argument("--api-key", type=str, default=None)
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        raise SystemExit('No API key. Set it with: export GOOGLE_MAPS_API_KEY="your-key-here"')

    total_requests = args.rounds * args.per_round
    log.info("Plan: %d rounds x %d buildings = %d total Google Places requests.", args.rounds, args.per_round, total_requests)

    run(args.scored, args.output_scored, args.output_verified, args.rounds, args.per_round, args.radius, api_key, args.sleep)


if __name__ == "__main__":
    main()
